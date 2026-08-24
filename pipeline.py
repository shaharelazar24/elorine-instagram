#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ELORINE — Instagram content automation (cloud runner)
=====================================================

רץ ב-GitHub Actions. לא דורש שום מחשב מקומי.

שתי פקודות:

    python pipeline.py generate --count 8
        Shopify -> בחירת שמלות שטרם פורסמו -> Gemini (Nano Banana)
        -> תמונת Feed 1080x1350 -> posts/<תאריך>/ + manifest.json

    python pipeline.py publish
        קורא את ה-manifest של היום, מוודא שהתמונות זמינות ב-raw.githubusercontent,
        ומפרסם לאינסטגרם. מסמן ב-state/posted_state.json.

בין שתי הפקודות ה-workflow עושה commit + push, כדי שלאינסטגרם
יהיה URL ציבורי אמיתי למשוך ממנו את התמונה.
"""

import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent
BRAND_KIT = json.loads((ROOT / "brand_kit.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state" / "posted_state.json"
POSTS_DIR = ROOT / "posts"

# --- Shopify ---
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-07")

# שתי דרכי הזדהות נתמכות:
#   1. Client credentials (Dev Dashboard) — הדרך הנוכחית.
#      טוקן זמני ל-24 שעות, נשלף אוטומטית בכל הרצה.
#   2. טוקן קבוע shpat_ (custom app ישן) — עדיין עובד אם יש לך כזה.
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_STATIC_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "")

# --- Gemini / Nano Banana ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

# --- Instagram ---
IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
IG_GRAPH = "https://graph.instagram.com/v21.0"

# --- GitHub (מסופק אוטומטית ע"י Actions) ---
GH_REPO = os.getenv("GITHUB_REPOSITORY", "")          # "owner/repo"
GH_BRANCH = os.getenv("GITHUB_REF_NAME", "main")
RAW_BASE = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

FEED_W, FEED_H = BRAND_KIT["output_spec"]["pixels"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ==========================================================================
# State
# ==========================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"posted": {}, "seeded_names": [], "updated_at": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def dress_name(title: str) -> str:
    return re.sub(r"^שמלת\s+", "", title.split("|")[0].strip()).strip()


def already_posted(product: dict, state: dict) -> bool:
    return (product["handle"] in state["posted"]
            or dress_name(product["title"]) in state.get("seeded_names", []))


# ==========================================================================
# Shopify
# ==========================================================================

SHOPIFY_QUERY = """
query Dresses($after: String) {
  products(first: 50, after: $after,
           query: "product_type:שמלה AND status:active",
           sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id handle title vendor createdAt description onlineStoreUrl tags
      media(first: 8) { edges { node { ... on MediaImage {
        image { url width height } } } } }
      priceRangeV2 { minVariantPrice { amount currencyCode } }
    } }
  }
}
"""


_token_cache = {"value": None}


def shopify_token() -> str:
    """מחזיר Access Token תקף.

    Client credentials: הטוקן תקף ל-24 שעות, ונשלף מחדש בכל הרצה.
    לא נשמר בשום מקום — רק בזיכרון של ההרצה.
    """
    if _token_cache["value"]:
        return _token_cache["value"]

    if SHOPIFY_STATIC_TOKEN:
        _token_cache["value"] = SHOPIFY_STATIC_TOKEN
        return SHOPIFY_STATIC_TOKEN

    if not (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        sys.exit("✗ חסרים SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET "
                 "(או SHOPIFY_ADMIN_TOKEN)")

    r = requests.post(
        f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
        json={"client_id": SHOPIFY_CLIENT_ID,
              "client_secret": SHOPIFY_CLIENT_SECRET,
              "grant_type": "client_credentials"},
        timeout=45,
    )
    if r.status_code != 200:
        sys.exit(f"✗ Shopify auth נכשל ({r.status_code}): {r.text[:300]}\n"
                 "  אם השגיאה היא shop_not_permitted — האפליקציה והחנות "
                 "לא באותו ארגון ב-Dev Dashboard.")

    payload = r.json()
    _token_cache["value"] = payload["access_token"]
    log(f"✓ Shopify: טוקן התקבל, scopes={payload.get('scope', '?')}")
    return _token_cache["value"]


def shopify_dresses() -> list:
    if not SHOPIFY_STORE:
        sys.exit("✗ חסר SHOPIFY_STORE")

    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": shopify_token(),
               "Content-Type": "application/json"}
    products, after = [], None

    while True:
        r = requests.post(url, headers=headers,
                          json={"query": SHOPIFY_QUERY,
                                "variables": {"after": after}}, timeout=45)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            sys.exit(f"✗ Shopify GraphQL: {data['errors']}")

        block = data["data"]["products"]
        for edge in block["edges"]:
            n = edge["node"]
            imgs = [m["node"]["image"] for m in n["media"]["edges"]
                    if m.get("node") and m["node"].get("image")]
            if not imgs:
                continue
            products.append({
                "handle": n["handle"], "title": n["title"],
                "vendor": n["vendor"], "created_at": n["createdAt"],
                "description": n["description"] or "",
                "url": n["onlineStoreUrl"], "tags": n["tags"],
                "images": imgs,
                "price": n["priceRangeV2"]["minVariantPrice"]["amount"],
            })

        if not block["pageInfo"]["hasNextPage"]:
            return products
        after = block["pageInfo"]["endCursor"]


def best_source_image(product: dict) -> dict:
    def score(img):
        w, h = img.get("width") or 1, img.get("height") or 1
        ratio_fit = 1.0 - min(abs(h / w - 1.5) / 1.5, 1.0)
        return ratio_fit * 2 + min(w * h / 8_000_000, 1.0)
    return max(product["images"], key=score)


# ==========================================================================
# מנוע הפרומפט
# ==========================================================================

COLOUR_HINTS = {
    "שחור": "black", "לבן": "white", "שנהב": "ivory", "בורדו": "burgundy",
    "אדום": "red", "ורוד": "pink", "תכלת": "powder blue", "כחול": "blue",
    "צהוב": "yellow", "ירוק": "green", "זית": "olive", "בז'": "beige",
    "חום": "brown", "שוקולד": "chocolate", "זהב": "gold", "כסף": "silver",
    "סגול": "purple", "קרם": "cream", "אפור": "grey", "תות": "mulberry",
}
FABRIC_HINTS = {
    "תחרה": "lace", "סאטן": "satin", "משי": "silk", "קטיפה": "velvet",
    "פאייטים": "sequin", "נצנצים": "glitter sequin", "טול": "tulle",
    "מש": "mesh", "ג'רזי": "jersey", "שיפון": "chiffon", "כותנה": "cotton",
    "דמוי עור": "faux leather", "בנדאז'": "bandage knit", "סריג": "knit",
    "ג'ורג'ט": "georgette", "טוויל": "twill", "פליסה": "pleated",
}
SILHOUETTE_HINTS = {
    "מקסי": "floor-length maxi", "מידי": "midi length", "מיני": "mini length",
    "מחוך": "corset bodice", "קולר": "halter neck", "סטרפלס": "strapless",
    "גב פתוח": "open back", "שסע": "high slit", "כתף אחת": "one shoulder",
}


def pick_background(product: dict) -> dict:
    bgs = BRAND_KIT["backgrounds"]
    digest = hashlib.sha256(product["handle"].encode("utf-8")).hexdigest()
    return bgs[int(digest[:8], 16) % len(bgs)]


HEB = "֐-׿"


def _mentions(blob: str, needle: str) -> bool:
    """התאמת מילה שלמה — כדי ש'מש' לא ייתפס בתוך 'שמש'.

    תחיליות עברית (ב, ל, מ, ה, ו, ש, כ) מותרות רק למילים בנות 3 אותיות ומעלה,
    אחרת 'שמש' היה נקרא כ-ש+'מש'.
    """
    prefix = "[בלמהושכ]?" if len(needle) >= 3 else ""
    pattern = rf"(?<![{HEB}]){prefix}{re.escape(needle)}(?![{HEB}])"
    return re.search(pattern, blob) is not None


def describe_dress(product: dict) -> str:
    blob = f"{product['title']} {product['description']} {' '.join(product['tags'])}"
    found = []
    for table in (COLOUR_HINTS, FABRIC_HINTS, SILHOUETTE_HINTS):
        for he, en in table.items():
            if en not in found and _mentions(blob, he):
                found.append(en)
    return ", ".join(found) if found else "elegant evening dress"


def build_prompt(product: dict, bg: dict) -> str:
    bk, vd = BRAND_KIT, BRAND_KIT["visual_dna"]
    nl = chr(10)
    return f"""You are an editorial fashion retoucher for ELORINE, a quiet-luxury womenswear label.

TASK
Take the attached product photograph and place the SAME garment, worn the SAME way, into a new editorial scene. This is a background and lighting replacement — not a redesign.

THE GARMENT (must be preserved exactly)
{describe_dress(product)}.
{nl.join('- ' + r for r in bk['fidelity_rules'])}

NEW SCENE
{bg['prompt']}.

FRAMING
{nl.join('- ' + r for r in bk['framing_rules'])}

GRADE AND FINISH
- Mood: {', '.join(vd['mood'])}.
- Contrast: {vd['contrast']}. Saturation: {vd['saturation']}.
- Texture: {vd['grain']}.
- Neutral palette anchored around {', '.join(vd['palette_neutrals'][:4])}.
- The result must read as a real photograph taken by a fashion photographer on a real location.

OUTPUT
- Aspect ratio {bk['output_spec']['aspect_ratio']}, vertical, for an Instagram feed post.

DO NOT
{nl.join('- ' + r for r in bk['negative_rules'])}
"""


# ==========================================================================
# Gemini
# ==========================================================================

def gemini_edit(image_bytes: bytes, mime: str, prompt: str) -> bytes:
    if not GEMINI_API_KEY:
        sys.exit("✗ חסר GEMINI_API_KEY")

    body = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime,
                             "data": base64.b64encode(image_bytes).decode()}},
            {"text": prompt},
        ]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    url = GEMINI_URL.format(model=GEMINI_MODEL)
    err = None

    for attempt in range(3):
        r = requests.post(url, json=body, timeout=180,
                          headers={"x-goog-api-key": GEMINI_API_KEY,
                                   "Content-Type": "application/json"})
        if r.status_code == 200:
            for cand in r.json().get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    blob = part.get("inline_data") or part.get("inlineData")
                    if blob and blob.get("data"):
                        return base64.b64decode(blob["data"])
            err = "Gemini החזיר תשובה ללא תמונה"
        else:
            err = f"Gemini {r.status_code}: {r.text[:300]}"
        log(f"   … ניסיון {attempt + 1} נכשל: {err}")
        time.sleep(4 * (attempt + 1))

    raise RuntimeError(err)


def to_feed_format(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    target = FEED_W / FEED_H
    current = w / h

    if current > target:
        nw = int(h * target)
        img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    elif current < target:
        nh = int(w / target)
        top = int((h - nh) * 0.35)
        img = img.crop((0, top, w, top + nh))

    img = img.resize((FEED_W, FEED_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=BRAND_KIT["output_spec"]["quality"],
             optimize=True, progressive=True)
    return buf.getvalue()


# ==========================================================================
# Caption
# ==========================================================================

TAG_MAP = [
    ("שמלת מקסי", "#שמלת_מקסי"), ("שמלת מידי", "#שמלת_מידי"),
    ("שמלת מיני", "#שמלת_מיני"), ("מקסי", "#שמלת_מקסי"),
    ("מידי", "#שמלת_מידי"), ("מיני", "#שמלת_מיני"),
    ("תחרה", "#תחרה"), ("סאטן", "#סאטן"), ("קטיפה", "#קטיפה"),
    ("פאייטים", "#פאייטים"), ("נצנצים", "#נצנצים"), ("טול", "#טול"),
    ("מחוך", "#מחוך"), ("קולר", "#קולר"), ("סטרפלס", "#סטרפלס"),
    ("גב פתוח", "#גב_פתוח"), ("שסע", "#שסע"), ("כתף אחת", "#כתף_אחת"),
    ("שמלת ערב", "#שמלת_ערב"), ("אירועים", "#שמלת_ערב"),
    ("שחור", "#שמלה_שחורה"), ("בורדו", "#בורדו"), ("שנהב", "#שנהב"),
    ("ירוק", "#ירוק"), ("אדום", "#אדום"), ("תכלת", "#תכלת"),
]
BASE_TAGS = ["#ELORINE", "#אופנה_ישראלית", "#אלגנטיות"]
CTA = "🤍 לצפייה באתר – הקישור בביו."


def hook_line(product: dict) -> str:
    parts = [p.strip() for p in re.split(r"(?<=\.)\s+",
                                         product["description"].strip()) if p.strip()]
    if len(parts) >= 2:
        line = parts[1]
    elif parts:
        line = re.sub(r"^[^-]*-\s*", "", parts[0])
    else:
        line = "גזרה נקייה שנופלת בדיוק כמו שצריך"
    line = line.rstrip(". ").strip()
    if len(line) > 130:
        line = line[:127].rsplit(" ", 1)[0] + "…"
    return line + "."


def build_hashtags(product: dict, limit: int = 6) -> list:
    blob = f"{product['title']} {product['description']} {' '.join(product['tags'])}"
    tags = list(BASE_TAGS)
    for needle, tag in TAG_MAP:
        if needle in blob and tag not in tags:
            tags.append(tag)
        if len(tags) >= limit:
            break
    return tags[:limit]


def build_caption(product: dict) -> str:
    return (f"שמלת {dress_name(product['title'])} · ELORINE\n"
            f"{hook_line(product)}\n{CTA}\n{' '.join(build_hashtags(product))}")


def build_alt_text(product: dict, bg: dict) -> str:
    return (f"שמלת {dress_name(product['title'])} של ELORINE, "
            f"{describe_dress(product)}, על רקע {bg['he']}.")[:1000]


# ==========================================================================
# Instagram
# ==========================================================================

def ig_quota_left() -> int:
    r = requests.get(f"{IG_GRAPH}/{IG_USER_ID}/content_publishing_limit",
                     params={"fields": "quota_usage,config",
                             "access_token": IG_ACCESS_TOKEN}, timeout=30)
    r.raise_for_status()
    row = r.json()["data"][0]
    return row["config"]["quota_total"] - row["quota_usage"]


def wait_for_public_url(url: str, tries: int = 12) -> bool:
    """מוודא שהתמונה באמת זמינה ציבורית לפני שאינסטגרם מנסה למשוך אותה."""
    for i in range(tries):
        try:
            r = requests.head(url, timeout=20, allow_redirects=True)
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
        log(f"   … ממתין ל-CDN ({i + 1}/{tries})")
    return False


def ig_publish(image_url: str, caption: str, alt_text: str = "") -> str:
    create = requests.post(f"{IG_GRAPH}/{IG_USER_ID}/media",
                           data={"image_url": image_url, "caption": caption,
                                 "alt_text": alt_text,
                                 "access_token": IG_ACCESS_TOKEN}, timeout=60)
    if create.status_code != 200:
        raise RuntimeError(f"container failed: {create.text[:300]}")
    creation_id = create.json()["id"]

    for _ in range(20):
        st = requests.get(f"{IG_GRAPH}/{creation_id}",
                          params={"fields": "status_code",
                                  "access_token": IG_ACCESS_TOKEN},
                          timeout=30).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"container ERROR: {st}")
        time.sleep(3)

    pub = requests.post(f"{IG_GRAPH}/{IG_USER_ID}/media_publish",
                        data={"creation_id": creation_id,
                              "access_token": IG_ACCESS_TOKEN}, timeout=60)
    if pub.status_code != 200:
        raise RuntimeError(f"publish failed: {pub.text[:300]}")
    return pub.json()["id"]


# ==========================================================================
# פקודות
# ==========================================================================

def today_dir() -> Path:
    return POSTS_DIR / datetime.now(timezone.utc).strftime("%Y-%m-%d")


def cmd_generate(count: int) -> None:
    state = load_state()
    products = shopify_dresses()
    queue = [p for p in products if not already_posted(p, state)]

    log(f"שמלות פעילות: {len(products)}  |  בתור: {len(queue)}")
    if not queue:
        log("אין שמלות חדשות. מסיים.")
        (today_dir()).mkdir(parents=True, exist_ok=True)
        (today_dir() / "manifest.json").write_text("[]", encoding="utf-8")
        return

    out = today_dir()
    out.mkdir(parents=True, exist_ok=True)
    manifest = []

    for product in queue[:count]:
        name = dress_name(product["title"])
        bg = pick_background(product)
        log(f"\n▶ {name}  ({product['handle']})  רקע: {bg['he']}")

        try:
            src = best_source_image(product)
            raw = requests.get(src["url"], timeout=60)
            raw.raise_for_status()
            mime = mimetypes.guess_type(src["url"].split("?")[0])[0] or "image/jpeg"

            edited = gemini_edit(raw.content, mime, build_prompt(product, bg))
            feed = to_feed_format(edited)
        except Exception as exc:                       # noqa: BLE001
            log(f"   ✗ נכשל, מדלג: {exc}")
            continue

        rel = f"posts/{out.name}/{product['handle']}.jpg"
        (ROOT / rel).write_bytes(feed)
        log(f"   ✓ {rel}  ({len(feed) // 1024} KB)")

        entry = {
            "handle": product["handle"], "title": product["title"],
            "dress_name": name, "product_url": product["url"],
            "price_ils": product["price"], "background": bg["id"],
            "background_he": bg["he"], "image_path": rel,
            "source_image": src["url"],
            "prompt": build_prompt(product, bg),
            "caption": build_caption(product),
            "alt_text": build_alt_text(product, bg),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "published": False,
        }
        manifest.append(entry)
        (ROOT / rel).with_suffix(".json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\nנוצרו {len(manifest)} פוסטים ב-{out.name}")


def cmd_publish() -> None:
    if not (IG_USER_ID and IG_ACCESS_TOKEN):
        sys.exit("✗ חסרים IG_USER_ID / IG_ACCESS_TOKEN")
    if not GH_REPO:
        sys.exit("✗ חסר GITHUB_REPOSITORY — הפקודה הזו רצה רק בתוך Actions")

    manifest_path = today_dir() / "manifest.json"
    if not manifest_path.exists():
        log("אין manifest להיום. מסיים.")
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pending = [e for e in manifest if not e.get("published")]
    if not pending:
        log("אין פוסטים ממתינים.")
        return

    left = ig_quota_left()
    log(f"מכסת פרסום שנותרה: {left}")
    pending = pending[:left]

    state = load_state()
    for entry in pending:
        url = RAW_BASE.format(repo=GH_REPO, branch=GH_BRANCH,
                              path=entry["image_path"])
        log(f"\n▶ {entry['dress_name']}\n   {url}")

        if not wait_for_public_url(url):
            log("   ✗ התמונה לא זמינה ציבורית, מדלג")
            continue

        try:
            media_id = ig_publish(url, entry["caption"], entry["alt_text"])
        except Exception as exc:                       # noqa: BLE001
            log(f"   ✗ פרסום נכשל: {exc}")
            continue

        entry["published"] = True
        entry["ig_media_id"] = media_id
        entry["published_at"] = datetime.now(timezone.utc).isoformat()
        state["posted"][entry["handle"]] = {
            "name": entry["dress_name"], "media_id": media_id,
            "at": entry["published_at"],
        }
        log(f"   ✓ פורסם: {media_id}")
        time.sleep(5)

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    save_state(state)
    done = sum(1 for e in manifest if e.get("published"))
    log(f"\nפורסמו {done}/{len(manifest)}")


def cmd_list() -> None:
    state = load_state()
    queue = [p for p in shopify_dresses() if not already_posted(p, state)]
    log(f"{len(queue)} שמלות בתור:")
    for p in queue:
        log(f"  · {dress_name(p['title']):<14} {p['handle']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="ELORINE Instagram automation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="ייצור תמונות + קפשנים")
    g.add_argument("--count", type=int, default=int(os.getenv("POSTS_PER_RUN", "8")))

    sub.add_parser("publish", help="פרסום ה-manifest של היום")
    sub.add_parser("list", help="הצגת התור")

    args = ap.parse_args()
    {"generate": lambda: cmd_generate(args.count),
     "publish": cmd_publish, "list": cmd_list}[args.cmd]()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ELORINE — Instagram + Facebook content automation
=================================================

Shopify → Google Gemini (Nano Banana) → Instagram + Facebook

תוכנית יומית (5 פוסטים):
    1 × קרוסלה   — שמלה רב-צבעונית, תמונה לכל צבע
    1 × אווירה    — ללא שם מותג, ללא בגדים, ללא דוגמניות
    3 × שמלה      — פוסט בודד לכל אחת

פקודות:
    python pipeline.py generate     בונה את פוסטי היום ושומר ל-posts/<תאריך>/
    python pipeline.py publish      מפרסם לאינסטגרם ולפייסבוק
    python pipeline.py list         מציג מה בתור
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
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent
BRAND_KIT = json.loads((ROOT / "brand_kit.json").read_text(encoding="utf-8"))
ATMOSPHERE = json.loads((ROOT / "atmosphere.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state" / "posted_state.json"
POSTS_DIR = ROOT / "posts"

# --- הרכב ההרצה היומית ---
CAROUSELS_PER_RUN = int(os.getenv("CAROUSELS_PER_RUN", "1"))
ATMOSPHERE_PER_RUN = int(os.getenv("ATMOSPHERE_PER_RUN", "1"))
SINGLES_PER_RUN = int(os.getenv("SINGLES_PER_RUN", "3"))
MAX_CAROUSEL_ITEMS = 10          # מגבלת אינסטגרם

# --- Shopify ---
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-07")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_STATIC_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "")

# --- Gemini ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

# --- Meta (טוקן אחד לאינסטגרם ולפייסבוק) ---
GRAPH = "https://graph.facebook.com/v21.0"
META_TOKEN = os.getenv("META_ACCESS_TOKEN", "") or os.getenv("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.getenv("IG_USER_ID", "")
FB_PAGE_ID = os.getenv("FB_PAGE_ID", "")
PUBLISH_TO_FACEBOOK = os.getenv("PUBLISH_TO_FACEBOOK", "true").lower() != "false"

# --- GitHub ---
GH_REPO = os.getenv("GITHUB_REPOSITORY", "")
GH_BRANCH = os.getenv("GITHUB_REF_NAME", "main")
RAW_BASE = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

FEED_W, FEED_H = BRAND_KIT["output_spec"]["pixels"]
NL = chr(10)


def log(msg: str) -> None:
    print(msg, flush=True)


# ==========================================================================
# State
# ==========================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        s = {"posted": {}, "seeded_names": []}
    s.setdefault("posted", {})
    s.setdefault("seeded_names", [])
    s.setdefault("atmosphere_used", [])
    return s


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
  products(first: 40, after: $after,
           query: "product_type:שמלה AND status:active",
           sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      handle title vendor createdAt description onlineStoreUrl tags
      media(first: 8) { edges { node { ... on MediaImage {
        image { url width height } } } } }
      variants(first: 60) { edges { node {
        selectedOptions { name value }
        image { url width height }
      } } }
      priceRangeV2 { minVariantPrice { amount currencyCode } }
    } }
  }
}
"""

COLOUR_OPTION_NAMES = {"צבע", "color", "colour"}
_token_cache = {"value": None}


def shopify_token() -> str:
    if _token_cache["value"]:
        return _token_cache["value"]
    if SHOPIFY_STATIC_TOKEN:
        _token_cache["value"] = SHOPIFY_STATIC_TOKEN
        return SHOPIFY_STATIC_TOKEN
    if not (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        sys.exit("✗ חסרים SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET")

    r = requests.post(f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
                      json={"client_id": SHOPIFY_CLIENT_ID,
                            "client_secret": SHOPIFY_CLIENT_SECRET,
                            "grant_type": "client_credentials"}, timeout=45)
    if r.status_code != 200:
        sys.exit(f"✗ Shopify auth נכשל ({r.status_code}): {r.text[:300]}")
    payload = r.json()
    _token_cache["value"] = payload["access_token"]
    log(f"✓ Shopify: טוקן התקבל, scopes={payload.get('scope', '?')}")
    return _token_cache["value"]


def extract_colours(node: dict) -> list:
    """מחזיר [{'name': 'צהוב', 'image': url}] — צבע אחד לכל תמונה ייחודית."""
    seen, colours = set(), []
    for edge in node.get("variants", {}).get("edges", []):
        v = edge["node"]
        if not v.get("image"):
            continue
        colour = next((o["value"] for o in v.get("selectedOptions", [])
                       if o["name"].strip().lower() in COLOUR_OPTION_NAMES), None)
        if not colour or colour in seen:
            continue
        seen.add(colour)
        colours.append({"name": colour, "image": v["image"]["url"],
                        "width": v["image"].get("width"),
                        "height": v["image"].get("height")})
    return colours


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
                                "variables": {"after": after}}, timeout=60)
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
                "images": imgs, "colours": extract_colours(n),
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
HEB = "֐-׿"


def _mentions(blob: str, needle: str) -> bool:
    pattern = rf"(?<![{HEB}])[בלמהושכ]?{re.escape(needle)}(?![{HEB}])"
    return re.search(pattern, blob) is not None


def describe_dress(product: dict, colour: str = "") -> str:
    blob = f"{product['title']} {product['description']} {' '.join(product['tags'])}"
    found = []
    if colour:
        found.append(COLOUR_HINTS.get(colour.split()[0], colour))
    for table in (COLOUR_HINTS, FABRIC_HINTS, SILHOUETTE_HINTS):
        # כשצבע ידוע — לא שואבים צבעים אחרים מהתיאור
        if colour and table is COLOUR_HINTS:
            continue
        for he, en in table.items():
            if en not in found and _mentions(blob, he):
                found.append(en)
    return ", ".join(found) if found else "elegant evening dress"


def pick_background(key: str) -> dict:
    bgs = BRAND_KIT["backgrounds"]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return bgs[int(digest[:8], 16) % len(bgs)]


def build_prompt(product: dict, bg: dict, colour: str = "") -> str:
    bk, vd = BRAND_KIT, BRAND_KIT["visual_dna"]
    return f"""You are an editorial fashion retoucher for ELORINE, a quiet-luxury womenswear label.

TASK
Take the attached product photograph and place the SAME garment, worn the SAME way, into a new editorial scene. This is a background and lighting replacement — not a redesign.

THE GARMENT (must be preserved exactly)
{describe_dress(product, colour)}.
{NL.join('- ' + r for r in bk['fidelity_rules'])}

NEW SCENE
{bg['prompt']}.

FRAMING
{NL.join('- ' + r for r in bk['framing_rules'])}

GRADE AND FINISH
- Mood: {', '.join(vd['mood'])}.
- Contrast: {vd['contrast']}. Saturation: {vd['saturation']}.
- Texture: {vd['grain']}.
- Neutral palette anchored around {', '.join(vd['palette_neutrals'][:4])}.
- The result must read as a real photograph taken by a fashion photographer on a real location.

COLOUR ISOLATION — READ THIS TWICE
{NL.join('- ' + r for r in bk['colour_isolation'])}

OUTPUT
- Aspect ratio {bk['output_spec']['aspect_ratio']}, vertical, for an Instagram feed post.

DO NOT
{NL.join('- ' + r for r in bk['negative_rules'])}

FINAL CHECK BEFORE YOU OUTPUT
Compare your result against the attached source, region by region: neckline, straps, bodice seams, pleats, waist, hem, fabric colour. Any region that does not match the source is a mistake. The background is the only thing you were asked to invent.
"""


def build_atmosphere_prompt(scene: dict) -> str:
    bk, vd = BRAND_KIT, BRAND_KIT["visual_dna"]
    return f"""You are a still-life and atmosphere photographer for ELORINE, a quiet-luxury womenswear label.

TASK
Create one original photograph of the following scene. There is no product and no person in this image — it is pure mood.

SCENE
{scene['prompt']}.

RULES
{NL.join('- ' + r for r in ATMOSPHERE['rules'])}

GRADE AND FINISH
- Mood: {', '.join(vd['mood'])}.
- Contrast: {vd['contrast']}.
- Texture: {vd['grain']}.
- Palette anchored around {', '.join(vd['palette_neutrals'][:4])}, with restrained accents of {', '.join(vd['palette_accents'])}.

OUTPUT
- Aspect ratio {bk['output_spec']['aspect_ratio']}, vertical, for an Instagram feed post.

DO NOT
{NL.join('- ' + r for r in bk['negative_rules'])}
"""


# ==========================================================================
# Gemini
# ==========================================================================

def _gemini_call(parts: list) -> bytes:
    if not GEMINI_API_KEY:
        sys.exit("✗ חסר GEMINI_API_KEY")
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]}}
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
            err = f"Gemini {r.status_code}: {r.text[:250]}"
        log(f"   … ניסיון {attempt + 1} נכשל: {err}")
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(err)


def gemini_edit(image_bytes: bytes, mime: str, prompt: str) -> bytes:
    return _gemini_call([
        {"inline_data": {"mime_type": mime,
                         "data": base64.b64encode(image_bytes).decode()}},
        {"text": prompt},
    ])


def gemini_create(prompt: str) -> bytes:
    return _gemini_call([{"text": prompt}])


def to_feed_format(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    target, current = FEED_W / FEED_H, w / h
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


def render(source_url: str | None, prompt: str) -> bytes:
    """מייצר תמונת פיד — עם מקור (עריכה) או בלי (יצירה)."""
    if source_url:
        raw = requests.get(source_url, timeout=60)
        raw.raise_for_status()
        mime = mimetypes.guess_type(source_url.split("?")[0])[0] or "image/jpeg"
        return to_feed_format(gemini_edit(raw.content, mime, prompt))
    return to_feed_format(gemini_create(prompt))


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


def build_carousel_caption(product: dict, colours: list) -> str:
    names = [c["name"] for c in colours]
    if len(names) > 2:
        colour_line = f"אותה שמלה ב-{len(names)} גוונים — {', '.join(names[:-1])} ו{names[-1]}."
    else:
        colour_line = f"בשני גוונים — {names[0]} ו{names[1]}."
    return (f"שמלת {dress_name(product['title'])} · ELORINE\n"
            f"{hook_line(product)}\n{colour_line} איזה שלך? גללי ▶️\n"
            f"{CTA}\n{' '.join(build_hashtags(product))}")


def build_atmosphere_caption(scene: dict) -> str:
    t = ATMOSPHERE["caption_template"]
    return (f"{t['opener']}\n{scene['caption']}\n{t['cta']}\n"
            f"{' '.join(t['hashtags'])}")


def build_alt_text(product: dict, bg: dict, colour: str = "") -> str:
    c = f" בגוון {colour}," if colour else ""
    return (f"שמלת {dress_name(product['title'])} של ELORINE,{c} "
            f"{describe_dress(product, colour)}, על רקע {bg['he']}.")[:1000]


# ==========================================================================
# פרסום — אינסטגרם
# ==========================================================================

def _meta_check() -> None:
    if not META_TOKEN:
        sys.exit("✗ חסר META_ACCESS_TOKEN")
    if not IG_USER_ID:
        sys.exit("✗ חסר IG_USER_ID")


def ig_quota_left() -> int:
    r = requests.get(f"{GRAPH}/{IG_USER_ID}/content_publishing_limit",
                     params={"fields": "quota_usage,config",
                             "access_token": META_TOKEN}, timeout=30)
    r.raise_for_status()
    row = r.json()["data"][0]
    return row["config"]["quota_total"] - row["quota_usage"]


def _ig_container(**params) -> str:
    params["access_token"] = META_TOKEN
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", data=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"container: {r.text[:300]}")
    return r.json()["id"]


def _ig_wait(creation_id: str, tries: int = 25) -> None:
    for _ in range(tries):
        st = requests.get(f"{GRAPH}/{creation_id}",
                          params={"fields": "status_code",
                                  "access_token": META_TOKEN},
                          timeout=30).json()
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"container ERROR: {st}")
        time.sleep(3)


def _ig_publish(creation_id: str) -> str:
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media_publish",
                      data={"creation_id": creation_id,
                            "access_token": META_TOKEN}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"publish: {r.text[:300]}")
    return r.json()["id"]


def ig_post_single(image_url: str, caption: str, alt_text: str = "") -> str:
    cid = _ig_container(image_url=image_url, caption=caption, alt_text=alt_text)
    _ig_wait(cid)
    return _ig_publish(cid)


def ig_post_carousel(image_urls: list, caption: str) -> str:
    children = []
    for url in image_urls[:MAX_CAROUSEL_ITEMS]:
        cid = _ig_container(image_url=url, is_carousel_item="true")
        _ig_wait(cid)
        children.append(cid)
    parent = _ig_container(media_type="CAROUSEL",
                           children=",".join(children), caption=caption)
    _ig_wait(parent)
    return _ig_publish(parent)


# ==========================================================================
# פרסום — פייסבוק
# ==========================================================================

def fb_post_single(image_url: str, caption: str) -> str:
    r = requests.post(f"{GRAPH}/{FB_PAGE_ID}/photos",
                      data={"url": image_url, "caption": caption,
                            "access_token": META_TOKEN}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"fb photo: {r.text[:300]}")
    return r.json().get("post_id") or r.json().get("id", "")


def fb_post_multi(image_urls: list, caption: str) -> str:
    media_ids = []
    for url in image_urls:
        r = requests.post(f"{GRAPH}/{FB_PAGE_ID}/photos",
                          data={"url": url, "published": "false",
                                "access_token": META_TOKEN}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"fb upload: {r.text[:300]}")
        media_ids.append(r.json()["id"])

    attached = json.dumps([{"media_fbid": m} for m in media_ids])
    r = requests.post(f"{GRAPH}/{FB_PAGE_ID}/feed",
                      data={"message": caption, "attached_media": attached,
                            "access_token": META_TOKEN}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"fb feed: {r.text[:300]}")
    return r.json()["id"]


def wait_for_public_url(url: str, tries: int = 12) -> bool:
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


# ==========================================================================
# generate
# ==========================================================================

def today_dir() -> Path:
    return POSTS_DIR / datetime.now(timezone.utc).strftime("%Y-%m-%d")


def pick_atmosphere(state: dict) -> dict:
    """בוחר סצנה שלא הייתה לאחרונה — סבב מלא לפני חזרה."""
    scenes = ATMOSPHERE["scenes"]
    used = state.get("atmosphere_used", [])
    fresh = [s for s in scenes if s["id"] not in used]
    if not fresh:
        used, fresh = [], scenes
    idx = int(hashlib.sha256(date.today().isoformat().encode()).hexdigest()[:8], 16)
    scene = fresh[idx % len(fresh)]
    state["atmosphere_used"] = used + [scene["id"]]
    return scene


def cmd_generate() -> None:
    state = load_state()
    products = shopify_dresses()
    queue = [p for p in products if not already_posted(p, state)]
    multi = [p for p in queue if len(p["colours"]) >= 2]

    log(f"שמלות פעילות: {len(products)}  |  בתור: {len(queue)}  "
        f"|  רב-צבעוניות: {len(multi)}")

    out = today_dir()
    out.mkdir(parents=True, exist_ok=True)
    manifest, used = [], set()

    # ---------- קרוסלות ----------
    for product in multi[:CAROUSELS_PER_RUN]:
        name = dress_name(product["title"])
        colours = product["colours"][:MAX_CAROUSEL_ITEMS]
        # רקע אחיד לכל הקרוסלה — הצבע הוא ההשוואה, לא הסביבה
        bg = pick_background(product["handle"])
        log(f"\n▶ קרוסלה: {name} — {len(colours)} צבעים  רקע: {bg['he']}")
        paths = []
        for colour in colours:
            try:
                feed = render(colour["image"],
                              build_prompt(product, bg, colour["name"]))
            except Exception as exc:                       # noqa: BLE001
                log(f"   ✗ {colour['name']} נכשל, מדלג: {exc}")
                continue
            rel = f"posts/{out.name}/{product['handle']}__{len(paths) + 1}.jpg"
            (ROOT / rel).write_bytes(feed)
            paths.append({"colour": colour["name"], "path": rel,
                          "background": bg["id"], "background_he": bg["he"]})
            log(f"   ✓ {colour['name']}  →  {rel}  ({len(feed) // 1024} KB)")

        if len(paths) < 2:
            log("   ✗ פחות משתי תמונות — לא קרוסלה, מדלג")
            continue

        used.add(product["handle"])
        manifest.append({
            "type": "carousel", "handle": product["handle"],
            "title": product["title"], "dress_name": name,
            "product_url": product["url"], "price_ils": product["price"],
            "items": paths,
            "caption": build_carousel_caption(product, colours[:len(paths)]),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "published_ig": False, "published_fb": False,
        })

    # ---------- אווירה ----------
    for _ in range(ATMOSPHERE_PER_RUN):
        scene = pick_atmosphere(state)
        log(f"\n▶ אווירה: {scene['he']}")
        try:
            feed = render(None, build_atmosphere_prompt(scene))
        except Exception as exc:                           # noqa: BLE001
            log(f"   ✗ נכשל, מדלג: {exc}")
            continue
        rel = f"posts/{out.name}/atmosphere__{scene['id']}.jpg"
        (ROOT / rel).write_bytes(feed)
        log(f"   ✓ {rel}  ({len(feed) // 1024} KB)")
        manifest.append({
            "type": "atmosphere", "scene": scene["id"],
            "scene_he": scene["he"], "image_path": rel,
            "caption": build_atmosphere_caption(scene),
            "alt_text": f"תמונת אווירה של ELORINE — {scene['he']}.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "published_ig": False, "published_fb": False,
        })

    # ---------- שמלות בודדות ----------
    singles = [p for p in queue if p["handle"] not in used]
    for product in singles[:SINGLES_PER_RUN]:
        name = dress_name(product["title"])
        bg = pick_background(product["handle"])
        log(f"\n▶ {name}  ({product['handle']})  רקע: {bg['he']}")
        src = best_source_image(product)
        try:
            feed = render(src["url"], build_prompt(product, bg))
        except Exception as exc:                           # noqa: BLE001
            log(f"   ✗ נכשל, מדלג: {exc}")
            continue
        rel = f"posts/{out.name}/{product['handle']}.jpg"
        (ROOT / rel).write_bytes(feed)
        log(f"   ✓ {rel}  ({len(feed) // 1024} KB)")
        manifest.append({
            "type": "single", "handle": product["handle"],
            "title": product["title"], "dress_name": name,
            "product_url": product["url"], "price_ils": product["price"],
            "background": bg["id"], "background_he": bg["he"],
            "image_path": rel, "caption": build_caption(product),
            "alt_text": build_alt_text(product, bg),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "published_ig": False, "published_fb": False,
        })

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save_state(state)

    kinds = {}
    for e in manifest:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    log(f"\nנוצרו {len(manifest)} פוסטים ב-{out.name}: "
        + ", ".join(f"{v} {k}" for k, v in kinds.items()))


# ==========================================================================
# publish
# ==========================================================================

def raw_url(rel_path: str) -> str:
    return RAW_BASE.format(repo=GH_REPO, branch=GH_BRANCH, path=rel_path)


def cmd_publish() -> None:
    _meta_check()
    if not GH_REPO:
        sys.exit("✗ הפקודה הזו רצה רק בתוך GitHub Actions")

    mpath = today_dir() / "manifest.json"
    if not mpath.exists():
        log("אין manifest להיום.")
        return
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    pending = [e for e in manifest if not e.get("published_ig")]
    if not pending:
        log("אין פוסטים ממתינים.")
        return

    left = ig_quota_left()
    log(f"מכסת אינסטגרם שנותרה: {left}")
    pending = pending[:left]
    fb_on = PUBLISH_TO_FACEBOOK and bool(FB_PAGE_ID)
    if not fb_on:
        log("פייסבוק: מדולג (חסר FB_PAGE_ID או כבוי).")

    state = load_state()
    for entry in pending:
        label = entry.get("dress_name") or entry.get("scene_he") or entry["type"]
        log(f"\n▶ {entry['type']}: {label}")

        if entry["type"] == "carousel":
            urls = [raw_url(i["path"]) for i in entry["items"]]
        else:
            urls = [raw_url(entry["image_path"])]

        if not all(wait_for_public_url(u) for u in urls):
            log("   ✗ תמונה לא זמינה ציבורית, מדלג")
            continue

        # --- אינסטגרם ---
        try:
            if entry["type"] == "carousel":
                mid = ig_post_carousel(urls, entry["caption"])
            else:
                mid = ig_post_single(urls[0], entry["caption"],
                                     entry.get("alt_text", ""))
            entry["published_ig"] = True
            entry["ig_media_id"] = mid
            log(f"   ✓ אינסטגרם: {mid}")
        except Exception as exc:                           # noqa: BLE001
            log(f"   ✗ אינסטגרם נכשל: {exc}")
            continue

        # --- פייסבוק ---
        if fb_on:
            try:
                fid = (fb_post_multi(urls, entry["caption"]) if len(urls) > 1
                       else fb_post_single(urls[0], entry["caption"]))
                entry["published_fb"] = True
                entry["fb_post_id"] = fid
                log(f"   ✓ פייסבוק: {fid}")
            except Exception as exc:                       # noqa: BLE001
                log(f"   ✗ פייסבוק נכשל (אינסטגרם כן עלה): {exc}")

        if entry.get("handle"):
            state["posted"][entry["handle"]] = {
                "name": entry.get("dress_name"), "type": entry["type"],
                "media_id": entry.get("ig_media_id"),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        entry["published_at"] = datetime.now(timezone.utc).isoformat()
        time.sleep(8)

    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    save_state(state)
    ig_done = sum(1 for e in manifest if e.get("published_ig"))
    fb_done = sum(1 for e in manifest if e.get("published_fb"))
    log(f"\nאינסטגרם: {ig_done}/{len(manifest)}  |  פייסבוק: {fb_done}/{len(manifest)}")


def cmd_list() -> None:
    state = load_state()
    queue = [p for p in shopify_dresses() if not already_posted(p, state)]
    multi = [p for p in queue if len(p["colours"]) >= 2]
    log(f"{len(queue)} בתור, מתוכן {len(multi)} רב-צבעוניות\n")
    log("— רב-צבעוניות (מועמדות לקרוסלה) —")
    for p in multi:
        cols = ", ".join(c["name"] for c in p["colours"])
        log(f"  · {dress_name(p['title']):<14} {len(p['colours'])} צבעים: {cols}")
    log(f"\n— חד-צבעוניות — {len(queue) - len(multi)} שמלות")


def main() -> None:
    ap = argparse.ArgumentParser(description="ELORINE social automation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="בניית פוסטי היום")
    sub.add_parser("publish", help="פרסום לאינסטגרם ולפייסבוק")
    sub.add_parser("list", help="הצגת התור")
    args = ap.parse_args()
    {"generate": cmd_generate, "publish": cmd_publish, "list": cmd_list}[args.cmd]()


if __name__ == "__main__":
    main()

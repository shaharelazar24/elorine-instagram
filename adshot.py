#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""פוסטר קמפיין לשמלה מהחנות.

לכל שמלה: פוזה חדשה, רקע משלה, ומיתוג ELORINE בתוך הסצנה עצמה.
הכיתוב בעברית לא נוצר כאן — הוא מולבש על התמונה אחר כך, כדי שהאותיות
יהיו נכונות ולא "כמעט עברית" של מודל תמונה.

    python adshot.py "<sources>" "<poses>" "<scenes>" "<brands>" [tag] [ratio]

sources מופרדים בפסיק — או handle של שמלה מהחנות, או נתיב לתמונה
בריפו (למשל studio/navy.png). השאר מופרדים ב-||, לפי אותו סדר.
פחות תיאורים משמלות — האחרון חוזר על עצמו.
ratio: 4:5 (פיד, ברירת מחדל), 9:16 (סטורי מסך מלא), 3:4, 1:1.
"""

import mimetypes
import sys

import requests

import pipeline as P

#  יחסי מסך נתמכים → גודל הפלט בפיקסלים.
RATIOS = {
    "4:5": (1600, 2000),
    "9:16": (1440, 2560),     # סטורי / רילס — תופס את כל מסך הטלפון
    "3:4": (1536, 2048),
    "1:1": (1600, 1600),
}

PROMPT = """You are a fashion photographer shooting a paid advertising campaign
for ELORINE, an Israeli quiet-luxury dress label.

You are given one product photograph. Make ONE new photograph of THE SAME dress
worn by THE SAME model.

THE DRESS DOES NOT CHANGE
Same design, same colour, same fabric, same length, same neckline, same sleeves,
same straps, same trim, same every detail of its construction. This is a real
garment being advertised — inventing or altering any part of it is a failure.

THE MODEL
The same woman: the same face, the same skin tone, the same build, the same hair
colour. Real and natural. Do not beautify her, slim her or smooth her skin.

WHAT IS NEW
POSE — {pose}
SCENE — {scene}
ELORINE IN THE SCENE — {brand}
The name must read exactly ELORINE: E-L-O-R-I-N-E, seven letters, spelled
correctly, sharp and legible, in a clean elegant serif. It belongs to the place,
lit by the same light as everything else. No other words anywhere in the picture,
no other brand, no watermark, no caption, no price, no logo but that one.

FRAMING — {ratio} VERTICAL, FULL BLEED
The photograph fills the ENTIRE frame, edge to edge, corner to corner. There must
be NO border, NO frame, NO letterbox, NO black or grey band across the top or the
bottom, and no strip where the picture changes into a flat empty surface. The real
scene continues all the way to all four edges.
She is head to hem inside the frame with nothing cut off, placed a little
off-centre, and the area named in the SCENE is kept clear for text.

QUALITY
It must look like one real photograph taken on a professional camera: real depth
of field, real light, real shadows, real skin and fabric texture. Not CGI, not a
3D render, nothing that reads as AI-generated."""


def arg(i: int, default: str = "") -> str:
    return sys.argv[i] if len(sys.argv) > i else default


def split(text: str) -> list:
    return [x.strip() for x in text.split("||") if x.strip()]


def pick(items: list, i: int) -> str:
    return items[min(i, len(items) - 1)] if items else ""


IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")


def is_path(src: str) -> bool:
    """נתיב לתמונה בריפו, להבדיל מ-handle של שמלה בחנות."""
    return "/" in src or src.lower().endswith(IMAGE_EXT)


def load_source(src: str, products: dict):
    """מחזיר (bytes, mime, slug) — מקובץ בריפו או מתמונת מוצר בשופיפיי."""
    if is_path(src):
        path = P.ROOT / src
        if not path.exists():
            raise FileNotFoundError(f"אין קובץ כזה בריפו: {src}")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return path.read_bytes(), mime, path.stem
    product = products.get(src)
    if product is None:
        raise KeyError(f"אין שמלה פעילה עם handle '{src}'")
    P.log(f"   · {product['title']}")
    url = P.front_candidates(product)[0]["url"]
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    mime = mimetypes.guess_type(url.split("?")[0])[0] or "image/jpeg"
    return resp.content, mime, P.file_slug(src)


def main() -> None:
    sources = [h.strip() for h in arg(1).split(",") if h.strip()]
    poses, scenes, brands = split(arg(2)), split(arg(3)), split(arg(4))
    tag = arg(5) or "ad"
    ratio = arg(6).strip() or "4:5"
    if not sources:
        sys.exit("✗ לא נמסרו מקורות")
    if ratio not in RATIOS:
        sys.exit(f"✗ יחס מסך לא נתמך: {ratio} (אפשרי: {', '.join(RATIOS)})")

    #  משנים את יחס המסך לכל ההרצה: גם מה שמבקשים מ-Gemini וגם החיתוך
    #  הסופי. pipeline קורא את שניהם מהמשתנים האלה.
    P.BRAND_KIT["output_spec"]["aspect_ratio"] = ratio
    P.FEED_W, P.FEED_H = RATIOS[ratio]

    out = P.ROOT / "studio" / "out"
    out.mkdir(parents=True, exist_ok=True)
    P.log(f"פוסטרי קמפיין — {len(sources)} תמונות, יחס {ratio} "
          f"({P.FEED_W}x{P.FEED_H})")

    #  פונים לשופיפיי רק אם באמת יש handle ברשימה.
    products = ({} if all(is_path(s) for s in sources)
                else {p["handle"]: p for p in P.shopify_dresses()})

    made = 0
    for i, src in enumerate(sources):
        P.log(f"\n▶ {i + 1}/{len(sources)}  {src}")
        try:
            raw, mime, slug = load_source(src, products)
            #  pose == RAW: הסצנה היא הפרומפט המלא, בלי התבנית של קמפיין
            #  שמלה. מתאים לפריימים שאינם צילום אופנה כלל.
            if pick(poses, i).strip().upper() == "RAW":
                prompt = pick(scenes, i)
            else:
                prompt = PROMPT.format(pose=pick(poses, i),
                                       scene=pick(scenes, i),
                                       brand=pick(brands, i), ratio=ratio)
            data, _ = P.to_feed_format(P.gemini_edit(raw, mime, prompt))
        except P.GeminiCreditsExhausted:
            raise
        except Exception as exc:                           # noqa: BLE001
            P.log(f"   ✗ נכשל: {exc}")
            continue
        path = out / f"ad-{slug}__{tag}.jpg"
        path.write_bytes(data)
        made += 1
        P.log(f"   ✓ {path.relative_to(P.ROOT)}  ({len(data) // 1024} KB)")

    P.log(f"\nנוצרו {made}/{len(sources)} פוסטרים ב-studio/out/")
    if not made:
        sys.exit(1)


if __name__ == "__main__":
    main()

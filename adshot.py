#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""פוסטר קמפיין לשמלה מהחנות.

לכל שמלה: פוזה חדשה, רקע משלה, ומיתוג ELORINE בתוך הסצנה עצמה.
הכיתוב בעברית לא נוצר כאן — הוא מולבש על התמונה אחר כך, כדי שהאותיות
יהיו נכונות ולא "כמעט עברית" של מודל תמונה.

    python adshot.py "<handles>" "<poses>" "<scenes>" "<brands>" [tag]

handles מופרדים בפסיק; השאר מופרדים ב-||, לפי אותו סדר.
פחות תיאורים משמלות — האחרון חוזר על עצמו.
"""

import mimetypes
import sys

import requests

import pipeline as P

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

FRAMING
Vertical 4:5, head to hem, nothing cut off. Place her a little off-centre and keep
the top third of the frame calm and uncluttered — that area is reserved for text
that will be added later.

QUALITY
It must look like one real photograph taken on a professional camera: real depth
of field, real light, real shadows, real skin and fabric texture. Not CGI, not a
3D render, nothing that reads as AI-generated. No other people in the frame."""


def arg(i: int, default: str = "") -> str:
    return sys.argv[i] if len(sys.argv) > i else default


def split(text: str) -> list:
    return [x.strip() for x in text.split("||") if x.strip()]


def pick(items: list, i: int) -> str:
    return items[min(i, len(items) - 1)] if items else ""


def main() -> None:
    handles = [h.strip() for h in arg(1).split(",") if h.strip()]
    poses, scenes, brands = split(arg(2)), split(arg(3)), split(arg(4))
    tag = arg(5) or "ad"
    if not handles:
        sys.exit("✗ לא נמסרו handles")

    out = P.ROOT / "studio" / "out"
    out.mkdir(parents=True, exist_ok=True)
    P.log(f"פוסטרי קמפיין — {len(handles)} שמלות, רקע ופוזה לכל אחת")
    products = {p["handle"]: p for p in P.shopify_dresses()}

    made = 0
    for i, handle in enumerate(handles):
        product = products.get(handle)
        if product is None:
            P.log(f"⚠ אין שמלה פעילה עם handle '{handle}' — מדלגים.")
            continue
        P.log(f"\n▶ {i + 1}/{len(handles)}  {product['title']}")
        try:
            url = P.front_candidates(product)[0]["url"]
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            mime = mimetypes.guess_type(url.split("?")[0])[0] or "image/jpeg"
            prompt = PROMPT.format(pose=pick(poses, i), scene=pick(scenes, i),
                                   brand=pick(brands, i))
            data, _ = P.to_feed_format(P.gemini_edit(resp.content, mime, prompt))
        except P.GeminiCreditsExhausted:
            raise
        except Exception as exc:                           # noqa: BLE001
            P.log(f"   ✗ נכשל: {exc}")
            continue
        path = out / f"ad-{P.file_slug(handle)}__{tag}.jpg"
        path.write_bytes(data)
        made += 1
        P.log(f"   ✓ {path.relative_to(P.ROOT)}  ({len(data) // 1024} KB)")

    P.log(f"\nנוצרו {made}/{len(handles)} פוסטרים ב-studio/out/")
    if not made:
        sys.exit(1)


if __name__ == "__main__":
    main()

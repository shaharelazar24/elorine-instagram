#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""מפרסם קרוסלה אחת מתמונות מוכנות בריפו לאינסטגרם ולפייסבוק.

    python publish_carousel.py "<paths>" "<handle>" ["<caption>"]

paths   — נתיבי התמונות בריפו, מופרדים בפסיק, לפי סדר הקרוסלה (2–10).
handle  — handle של השמלה. הכיתוב נבנה מהמוצר בחנות אם לא נמסר כיתוב ידני.
caption — אופציונלי: כיתוב ידני לפוסט, גובר על האוטומטי.
"""

import sys
from datetime import datetime, timezone

import pipeline as P


def arg(i: int, default: str = "") -> str:
    return sys.argv[i] if len(sys.argv) > i else default


def main() -> None:
    paths = [p.strip() for p in arg(1).split(",") if p.strip()]
    handle = arg(2).strip()
    caption_in = arg(3).strip()

    if len(paths) < 2:
        sys.exit("✗ צריך לפחות שתי תמונות לקרוסלה")

    P._meta_check()
    if not P.GH_REPO:
        sys.exit("✗ הפקודה הזו רצה רק בתוך GitHub Actions")

    left = P.ig_quota_left()
    P.log(f"מכסת אינסטגרם שנותרה: {left}")
    if left < 1:
        sys.exit("✗ אין מכסת פרסום פנויה")

    product = None
    if handle:
        product = {p["handle"]: p for p in P.shopify_dresses()}.get(handle)

    urls = []
    for path in paths:
        url = P.raw_url(path)
        if not P.wait_for_public_url(url):
            P.log(f"   ✗ לא זמינה ציבורית: {path} — מדלגים")
            continue
        urls.append(url)

    if len(urls) < 2:
        sys.exit("✗ פחות משתי תמונות זמינות ציבורית")

    if caption_in:
        caption = caption_in
    elif product:
        caption = P.build_caption(product)
    else:
        caption = P.CTA

    P.log(f"\n▶ קרוסלה של {len(urls)} תמונות")
    mid = P.ig_post_carousel(urls, caption)
    P.log(f"   ✓ אינסטגרם: {mid}")

    if P.PUBLISH_TO_FACEBOOK and P.FB_PAGE_ID:
        try:
            fid = P.fb_post_multi(urls, caption)
            P.log(f"   ✓ פייסבוק: {fid}")
        except Exception as exc:                           # noqa: BLE001
            P.log(f"   ✗ פייסבוק נכשל (אינסטגרם כן עלה): {exc}")
    else:
        P.log("פייסבוק: מדולג.")

    if handle:
        state = P.load_state()
        state["posted"][handle] = {
            "name": product["title"] if product else handle,
            "type": "carousel", "media_id": mid,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        P.save_state(state)

    P.log("\nהקרוסלה פורסמה.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""מפרסם תמונות מוכנות מהריפו לאינסטגרם ולפייסבוק.

    python publish_images.py "<paths>" "<handles>" ["<captions>"]

paths   — נתיבים לתמונות בריפו, מופרדים בפסיק, לפי סדר הפרסום.
handles — handle של השמלה לכל תמונה, באותו סדר. הכיתוב נבנה מהמוצר בחנות,
          והשמלה מסומנת כ"פורסמה" כדי שהפעימה היומית לא תחזור עליה.
captions — אופציונלי: כיתוב ידני לכל תמונה, מופרד ב-||, גובר על הכיתוב
          האוטומטי. פחות כיתובים מתמונות — השאר אוטומטיים.

כל תמונה עולה כפוסט בודד, עם המתנה בין פוסט לפוסט.
"""

import sys
import time
from datetime import datetime, timezone

import pipeline as P

GAP_SECONDS = int(__import__("os").getenv("GAP_SECONDS", "20"))


def arg(i: int, default: str = "") -> str:
    return sys.argv[i] if len(sys.argv) > i else default


def main() -> None:
    paths = [p.strip() for p in arg(1).split(",") if p.strip()]
    handles = [h.strip() for h in arg(2).split(",") if h.strip()]
    captions = [c.strip() for c in arg(3).split("||") if c.strip()]
    if not paths:
        sys.exit("✗ לא נמסרו נתיבי תמונות")
    if handles and len(handles) != len(paths):
        sys.exit("✗ מספר ה-handles לא תואם למספר התמונות")

    P._meta_check()
    if not P.GH_REPO:
        sys.exit("✗ הפקודה הזו רצה רק בתוך GitHub Actions")

    left = P.ig_quota_left()
    P.log(f"מכסת אינסטגרם שנותרה: {left}")
    if left < len(paths):
        P.log(f"⚠ המכסה מספיקה ל-{left} פוסטים בלבד — מפרסמים את הראשונים.")
        paths, handles = paths[:left], handles[:left]

    products = ({} if not handles
                else {p["handle"]: p for p in P.shopify_dresses()})
    state = P.load_state()
    fb_on = P.PUBLISH_TO_FACEBOOK and bool(P.FB_PAGE_ID)
    if not fb_on:
        P.log("פייסבוק: מדולג (חסר FB_PAGE_ID או כבוי).")

    done = 0
    for i, path in enumerate(paths):
        handle = handles[i] if handles else ""
        product = products.get(handle)
        label = product["title"] if product else path
        P.log(f"\n▶ {i + 1}/{len(paths)}  {label}")

        url = P.raw_url(path)
        if not P.wait_for_public_url(url):
            P.log("   ✗ התמונה לא זמינה ציבורית עדיין — מדלגים")
            continue

        if i < len(captions):
            caption = captions[i]
        elif product:
            caption = P.build_caption(product)
        else:
            caption = P.CTA
        alt = (P.build_alt_text(product, {"he": "בריכה בווילה יוקרתית"})
               if product else "")

        try:
            mid = P.ig_post_single(url, caption, alt)
            P.log(f"   ✓ אינסטגרם: {mid}")
        except Exception as exc:                           # noqa: BLE001
            P.log(f"   ✗ אינסטגרם נכשל: {exc}")
            continue
        done += 1

        if fb_on:
            try:
                fid = P.fb_post_single(url, caption)
                P.log(f"   ✓ פייסבוק: {fid}")
            except Exception as exc:                       # noqa: BLE001
                P.log(f"   ✗ פייסבוק נכשל (אינסטגרם כן עלה): {exc}")

        if handle:
            state["posted"][handle] = {
                "name": product["title"] if product else handle,
                "type": "single", "media_id": mid,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        if i < len(paths) - 1:
            time.sleep(GAP_SECONDS)

    P.save_state(state)
    P.log(f"\nפורסמו {done}/{len(paths)} פוסטים.")
    if not done:
        sys.exit(1)


if __name__ == "__main__":
    main()

# ELORINE — אוטומציית תוכן לאינסטגרם

Shopify → Google Gemini (Nano Banana) → Instagram Feed
רץ לגמרי בענן, על שרתי GitHub. **המחשב שלך לא מעורב בכלל.**

---

## איך זה עובד

בכל יום ב-10:00 (שעון ישראל), GitHub Actions מריץ:

```
1. generate   Shopify → בחירת שמלות שטרם פורסמו
              → Gemini מחליף רקע ותאורה, השמלה נשארת 1:1
              → חיתוך ל-4:5, JPEG 1080×1350
              → posts/<תאריך>/<handle>.jpg + .json

2. commit     דחיפה ל-repo — כך התמונה מקבלת URL ציבורי:
              raw.githubusercontent.com/<owner>/<repo>/main/posts/...

3. publish    אימות שה-URL באמת חי → Instagram Graph API
              → container → media_publish

4. state      עדכון state/posted_state.json ודחיפה חזרה
```

אם שלב כלשהו נכשל על שמלה מסוימת — היא מדולגת וההרצה ממשיכה. היא תחזור לתור מחר.

---

## הקמה — פעם אחת, ~15 דקות

### 1. צור repo

repo **ציבורי** בשם `elorine-instagram`, והעלה אליו את כל הקבצים מכאן.

> **למה ציבורי:** אינסטגרם צריך למשוך את התמונה מ-URL ציבורי בלי הזדהות. ב-repo פרטי הקישורים חסומים. המפתחות עצמם **לעולם לא נמצאים ב-repo** — הם ב-GitHub Secrets, שמוצפנים גם ב-repo ציבורי. מה שיהיה גלוי: התמונות והקפשנים — כלומר חומר שיווקי שגם ככה מיועד לפרסום. אם זה מפריע, ראה "אחסון חלופי" למטה.

### 2. הוצאת מפתחות

| מפתח | מאיפה |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → Create API key |
| `SHOPIFY_ADMIN_TOKEN` | Shopify Admin → Settings → Apps and sales channels → Develop apps → Create an app → Admin API scopes → סמן `read_products` → Install → העתק את ה-Admin API access token |
| `IG_ACCESS_TOKEN` | Meta for Developers → האפליקציה שלך → Instagram → Generate token עבור @elorine.il. ודא שזה **long-lived** token |

### 3. הגדרה ב-GitHub

**Settings → Secrets and variables → Actions**

בלשונית **Secrets** הוסף:
```
SHOPIFY_ADMIN_TOKEN
GEMINI_API_KEY
IG_ACCESS_TOKEN
GH_PAT                  ← רק אם רוצה רענון טוקן אוטומטי (ראה למטה)
```

בלשונית **Variables** הוסף:
```
SHOPIFY_STORE     = elorine.myshopify.com
IG_USER_ID        = 27628554153419571
GEMINI_MODEL      = gemini-2.5-flash-image
POSTS_PER_RUN     = 8
```

### 4. הרצת בדיקה

Actions → **ELORINE — daily Instagram post** → Run workflow
בשדה `publish` בחר **false**.

זה ייצר 8 תמונות ויעלה אותן ל-`posts/`, **בלי לפרסם**. תיכנס לתיקייה, תסתכל על התוצאות. אם הן טובות — תריץ שוב עם `publish: true`.

מהרגע הזה זה רץ לבד כל יום.

---

## מצב נוכחי

| | |
|---|---|
| שמלות פעילות בחנות | 138 |
| כבר קיבלו פוסט (מופו מ-124 פוסטים קיימים) | 80 |
| **בתור** | **~58** |
| חשבון | @elorine.il · Business · 712 עוקבים |
| מכסת פרסום | 100 / 24 שעות, בשימוש 0 |

---

## שליטה יומיומית

```bash
# מה בתור?  (מקומית, עם המפתחות ב-env)
python pipeline.py list

# להשהות זמנית
Actions → daily-post → ⋯ → Disable workflow

# לשנות קצב
Variables → POSTS_PER_RUN

# לשנות שעה
ערוך את ה-cron ב-.github/workflows/daily-post.yml
07:00 UTC = 10:00 קיץ · 08:00 UTC = 10:00 חורף
```

---

## שפת המותג

`brand_kit.json` הוא הלב. שם:

- **12 רקעים** — טרוורטין, סטודיו טיח, לובי מלון, חוף אבן, מרפסת זיתים, מסדרון קשתות, וילון פשתן, מדרגות שיש, מרפסת דמדומים, דיונת חול, קיר גלריה, גן לילי.
  הרקע נבחר לפי hash של ה-handle — כל שמלה מקבלת רקע אחר, וקבוע בין הרצות.
- **6 חוקי נאמנות** — אוסרים על המודל לגעת בצבע, בד, טקסטורה, גזרה, אורך, מחשוף, שסע, גב או תפרים.
- **8 חוקי שלילה** — בלי לוגו, בלי טקסט, בלי ידיים מעוותות, בלי עור פלסטי, בלי HDR, בלי CGI, בלי מראה AI.
- **פלטה, מסגור, גריידינג** — שנהב חם, עצם, חול, טאופ. 85mm f/2.0, השמלה 55-70% מהפריים.

לשינוי סגנון עורכים רק את ה-JSON. הקוד לא זז.

---

## פורמט ה-Caption

תואם למה שכבר קיים בפיד:

```
שמלת {שם} · ELORINE
{משפט מתוך תיאור המוצר בחנות}
🤍 לצפייה באתר – הקישור בביו.
#ELORINE #אופנה_ישראלית #אלגנטיות +3 תגיות מהמוצר
```

---

## רענון טוקן אינסטגרם

טוקן ארוך-טווח פג אחרי **60 יום**. ה-workflow `refresh-ig-token.yml` מרענן אותו ב-1 בכל חודש ומעדכן את ה-Secret בעצמו.

כדי להפעיל: צור [Personal Access Token](https://github.com/settings/tokens?type=beta) עם הרשאת **Secrets: Read and write** על ה-repo הזה, ושמור אותו כ-Secret בשם `GH_PAT`.

בלי זה — המערכת תעבוד, אבל תיעצר בשקט אחרי חודשיים. אל תדלג על זה.

---

## עלויות

| | |
|---|---|
| GitHub Actions | חינם (repo ציבורי = דקות ללא הגבלה) |
| Gemini image | ~$0.04 לתמונה |
| 8 ליום | **~$10 לחודש** |
| 3 ליום | ~$4 לחודש |

---

## אחסון חלופי (אם repo ציבורי מפריע)

החלף את `RAW_BASE` ב-`pipeline.py` והוסף שלב העלאה:

- **Cloudflare R2** — 10GB חינם, bucket ציבורי, S3 API
- **Backblaze B2** — 10GB חינם
- **Shopify Files** — התמונות יושבות באותו CDN של החנות (דורש הרשאת `write_files`)

---

## מבנה

```
elorine-instagram/
├── .github/workflows/
│   ├── daily-post.yml          המשימה היומית
│   └── refresh-ig-token.yml    רענון טוקן חודשי
├── pipeline.py                 generate / publish / list
├── brand_kit.json              שפת המותג — עורכים כאן
├── requirements.txt
├── state/
│   └── posted_state.json       מי כבר קיבל פוסט
└── posts/
    └── 2026-08-24/
        ├── manifest.json
        ├── {handle}.jpg
        └── {handle}.json       פרומפט, caption, alt, metadata
```

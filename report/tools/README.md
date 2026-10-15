# בניית הדוח

מקורות הדוח: `report/01_*.md`, `02_*.md`, `03_*.md`. התוצרים נבנים מהם, כך שכל תיקון טקסט נעשה ב-Markdown ואז בונים מחדש.

```bash
# 1. תמונות (דורש node, playwright עם Chromium, ו-`npm i mermaid@10.9.3` ליד diagrams.js; ו-pygments ל-Python)
node report/tools/diagrams.js report/figures                       # תרשימי בלוקים / מצבים / זרימה / רצף
python report/tools/code_images.py . report/figures <תיקיית פלט pytest/simulate>   # קטעי קוד ופלט טרמינל

# 2. מסמך Word (דורש pandoc ו-python-docx)
python report/tools/build_report.py report report/figures report/HomeGuard_Report.docx

# 3. רשימת השלמות שנותרו
python report/tools/list_placeholders.py report/HomeGuard_Report.docx report/TODO_before_submission.md

# 4. אחרי שיוצאים PDF סופי מ-Word: חבילת הגשה
report/tools/package_submission.sh <HomeGuard_Report.pdf> HomeGuard_Submission.zip
```

* כל תמונה `figN.png` / `codeN.png` שקיימת בתיקיית התמונות מחליפה אוטומטית את הסימון `[הכנס ...]` המתאים (לפי מספר האיור/הקוד בכיתוב).
  כדי להחליף תמונה בצילום מסך אמיתי: שמרו אותו בשם הזה בתיקייה ובנו מחדש, או הדביקו ב-Word ידנית.
* התרשימים מצוירים מתיאור טקסטואלי של הקוד האמיתי (`diagrams.js`); אם הקוד משתנה — עדכנו שם.
* קטעי הקוד נוצרים ישירות מקבצי המקור, עם מספרי השורות המקוריים. הערות ארוכות מקוצרות ל-`...`.

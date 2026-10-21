# בניית הדוח

מקור הדוח: `report/HomeGuard_Report.md`. הציטוטים נכתבים שם כ-`[@מפתח]` והמספור נוצר אוטומטית לפי סדר ההופעה, מתוך `report/references.json`. כל תיקון טקסט נעשה ב-Markdown ואז בונים מחדש.

```bash
# 1. תמונות (דורש node, playwright עם Chromium, `npm i mermaid@10.9.3` ליד diagrams.js, ו-pygments ל-Python)
node report/tools/diagrams.js report/figures                 # איור 1 (מבנה), איור 2 (תרשים זרימה) ואיור 4 (מצבים)
python report/tools/code_images.py . report/figures          # קוד 1-3, מצולמים מקבצי המקור

# 2. מסמך Word (דורש pandoc ו-python-docx)
python report/tools/build_report.py report report/figures report/HomeGuard_Report.docx

# 3. רשימת השלמות שנותרו
python report/tools/list_placeholders.py report/HomeGuard_Report.docx report/TODO_before_submission.md

# 4. אחרי שיוצאים PDF סופי מ-Word: חבילת הגשה
report/tools/package_submission.sh <HomeGuard_Report.pdf> HomeGuard_Submission.zip [HomeGuard_Report.docx]   # קבצים נוספים אחרי שם ה-ZIP נכנסים לתיקייה העליונה
```

* תמונה בשם `figN.png` / `codeN.png` בתיקיית התמונות מחליפה אוטומטית את הסימון `[הכנס ...]` עם אותו מספר איור/קוד בכיתוב. איור 7 (פלט Serial Monitor) הוא הצילום היחיד שחסר; כדי להחליף ב-Word, מדביקים ידנית במקום הסימון הצהוב. צילומים (איורים 3, 5, 6, 8) מעבדים לפני ההכנסה: טשטוש פרטים מזהים וחיתוך שורת הסטטוס של הטלפון. הגובה המרבי של כל איור נקבע ב-`FIG_MAX_H` ב-`build_report.py`.
* קטעי הקוד נוצרים ישירות מקבצי המקור, עם מספרי השורות המקוריים. הערות ארוכות מקוצרות ל-`...`.

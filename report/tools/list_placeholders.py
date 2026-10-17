"""Lists every placeholder still left in the built report, grouped by section.

    python list_placeholders.py <report.docx> <out.md>
"""
import re
import sys

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

PH = re.compile(r"\[(הכנס|להשלים|לאמת|הוסף קישור|תאריך)[^\]]*\]|dd/mm/yy")

doc = Document(sys.argv[1])
body = doc.element.body
items = []
heading = "(שער)"
last_caption = ""
pending = None  # placeholder paragraph waiting for its caption

blocks = []
for el in body.iterchildren():
    if el.tag.endswith("}p"):
        blocks.append(Paragraph(el, doc))
    elif el.tag.endswith("}tbl"):
        blocks.append(Table(el, doc))

for i, b in enumerate(blocks):
    if isinstance(b, Paragraph):
        t = b.text.strip()
        if b.style.name.startswith("Heading"):
            heading = t
        elif re.match(r"^(טבלה) \d+:", t):
            last_caption = t
        elif t.startswith("[הכנס"):
            cap = ""
            for nb in blocks[i + 1:i + 3]:
                if isinstance(nb, Paragraph) and re.match(r"^(איור|קוד) \d+:", nb.text.strip()):
                    cap = nb.text.strip()
                    break
            items.append((heading, cap or "(ללא כיתוב)", t))
        elif PH.search(t) and not re.match(r"^\[\d+\]", t):
            items.append((heading, "", t))
    else:
        found = []
        for row in b.rows:
            for c in row.cells:
                for m in PH.finditer(c.text):
                    found.append(m.group(0))
        if found:
            kinds = {}
            for f in found:
                kinds[f] = kinds.get(f, 0) + 1
            summary = ", ".join(f"{k} ×{v}" for k, v in kinds.items())
            items.append((heading, last_caption or "(טבלה)", f"תאים לא מלאים בטבלה: {summary}"))

out = ["# רשימת השלמות לפני ההגשה", "",
       "נוצר אוטומטית מקובץ ה-Word (`python report/tools/list_placeholders.py`). "
       "בכל סעיף: הסעיף בדוח, הכיתוב/הטבלה הרלוונטיים, והנוסח של הסימון שיש להחליף. "
       "בקובץ ה-Word כל סימון כזה מודגש בצהוב (חיפוש: `[הכנס`, `[להשלים`, `[לאמת`, `[הוסף קישור`).", ""]
cur = None
for h, cap, text in items:
    if h != cur:
        out += ["", f"## {h}"]
        cur = h
    short = text if len(text) < 260 else text[:257] + "..."
    out.append(f"- [ ] **{cap}** — {short}" if cap else f"- [ ] {short}")
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(out) + "\n")
print(len(items), "items")

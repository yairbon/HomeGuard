"""Renders the report's code excerpts ("Code 1..3") as PNG, straight from the real source files.

    python code_images.py <repo_root> <out_dir>

Needs `pygments` and Chromium (via shot.js). Excerpts keep the ORIGINAL line
numbers; runs of 3+ comment-only lines and long docstrings are collapsed to a
single "..." line so the excerpt stays readable at page width.
"""
import html
import os
import subprocess
import sys
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import CppLexer, DartLexer, PythonLexer

REPO = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
HERE = Path(__file__).resolve().parent

# name -> (file, [(first, last), ...]) with 1-based inclusive line numbers
SNIPPETS = {
    # distance sensor: several abnormal readings in a row are an intrusion
    "code1": ("esp32/ultrasonic_sensor/ultrasonic_sensor.ino", [(404, 405), (413, 434)]),
    # keypad scan: one row at a time, read the columns
    "code2": ("esp32/door_keypad_controller/door_keypad_controller.ino", [(627, 644)]),
    # door board: the 30-second entry delay (time-out -> alarm, PIN -> disarm)
    "code3": ("esp32/door_keypad_controller/door_keypad_controller.ino", [(1110, 1142)]),
}

LEXERS = {".ino": CppLexer(), ".py": PythonLexer(), ".dart": DartLexer()}


def is_comment(stripped: str) -> bool:
    return stripped.startswith(("//", "///", "/*", "*/")) or (
        stripped.startswith("*") and not stripped.startswith("*/") and len(stripped) > 1 and stripped[1] == " "
    ) or stripped.startswith("#") and not stripped.startswith("#include") and not stripped.startswith("#define")


def collapse(lines):
    """lines: list[(lineno, text)] -> same, with long comment/docstring runs elided."""
    out, i = [], 0
    while i < len(lines):
        no, text = lines[i]
        st = text.strip()
        # python docstring block
        if st.startswith('"""') and not (st.count('"""') >= 2 and len(st) > 6):
            j = i + 1
            while j < len(lines) and '"""' not in lines[j][1]:
                j += 1
            if j - i >= 3 and j < len(lines):
                indent = text[: len(text) - len(text.lstrip())]
                out.append((no, indent + '"""  ...  """'))
                i = j + 1
                continue
        # run of comment-only lines
        if is_comment(st):
            j = i
            while j < len(lines) and is_comment(lines[j][1].strip()):
                j += 1
            if j - i >= 3:
                indent = text[: len(text) - len(text.lstrip())]
                marker = "#" if text.strip().startswith("#") else "//"
                out.append((no, f"{indent}{marker} ..."))
                i = j
                continue
        out.append((no, text))
        i += 1
    return out


CSS = """
body{margin:0;background:#fff}
#box{display:inline-block;max-width:880px;background:#fafafa;border:1px solid #bbb;padding:10px 14px;
     font-family:'DejaVu Sans Mono',monospace;font-size:14px;line-height:1.42}
.hl .c,.hl .c1,.hl .cm,.hl .cp,.hl .cs,.hl .ch,.hl .sd{color:#6a737d;font-style:italic}
.hl .k,.hl .kd,.hl .kt,.hl .kr,.hl .kc,.hl .kn,.hl .kp{color:#0033b3;font-weight:bold}
.hl .s,.hl .s1,.hl .s2,.hl .sc,.hl .se{color:#067d17}
.hl .mi,.hl .mf,.hl .mh,.hl .mo{color:#1750eb}
.hl .nf,.hl .fm{color:#00627a}.hl .nc,.hl .nn{color:#00627a}
.hl .cpf{color:#067d17}
table{border-collapse:collapse}
td.n{color:#9aa0a6;text-align:right;padding-right:12px;user-select:none;vertical-align:top}
td.t{white-space:pre-wrap;direction:ltr;unicode-bidi:embed;word-break:break-word}
tr.gap td{color:#9aa0a6;text-align:center;padding:2px 0}
"""


def render(name, rel, ranges):
    path = REPO / rel
    src = path.read_text(encoding="utf-8").splitlines()
    lexer = LEXERS[path.suffix]
    rows, prev_end = [], None
    for first, last in ranges:
        chunk = collapse([(n, src[n - 1]) for n in range(first, last + 1)])
        if prev_end is not None:
            rows.append(("gap", None, None))
        for no, text in chunk:
            code = highlight(text, lexer, HtmlFormatter(nowrap=True)).rstrip("\n")
            rows.append(("row", no, code or "&nbsp;"))
        prev_end = last
    body = []
    for kind, no, code in rows:
        if kind == "gap":
            body.append('<tr class="gap"><td class="n">&#8942;</td><td class="t"></td></tr>')
        else:
            body.append(f'<tr><td class="n">{no}</td><td class="t hl">{code}</td></tr>')
    doc = (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
           f"<div id='box'><table>{''.join(body)}</table></div></body></html>")
    hp = OUT / f"{name}.html"
    hp.write_text(doc, encoding="utf-8")
    subprocess.run(["node", str(HERE / "shot.js"), str(hp), str(OUT / f"{name}.png")], check=True,
                   env={**os.environ, "PLAYWRIGHT_MODULE": os.environ.get("PLAYWRIGHT_MODULE", "playwright")})
    hp.unlink()
    print("rendered", name, f"({len(rows)} rows)")


if __name__ == "__main__":
    for nm, (rel, rng) in SNIPPETS.items():
        render(nm, rel, rng)

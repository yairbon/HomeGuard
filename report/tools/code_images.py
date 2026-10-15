"""Renders the report's code excerpts ("Code 1..15") and terminal outputs
(figures 26, 27) as PNG, straight from the real source files / saved output.

    python code_images.py <repo_root> <out_dir> [terminal_dir]

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
TERM = Path(sys.argv[3]) if len(sys.argv) > 3 else None
OUT.mkdir(parents=True, exist_ok=True)
HERE = Path(__file__).resolve().parent

# name -> (file, [(first, last), ...]) with 1-based inclusive line numbers
SNIPPETS = {
    "code1": ("esp32/ultrasonic_sensor/ultrasonic_sensor.ino", [(207, 223)]),
    "code2": ("esp32/ultrasonic_sensor/ultrasonic_sensor.ino", [(230, 282)]),
    "code3": ("esp32/ultrasonic_sensor/ultrasonic_sensor.ino", [(404, 446)]),
    "code4": ("esp32/vibration_sensor/vibration_sensor.ino", [(54, 62), (93, 93), (166, 191)]),
    "code5": ("esp32/door_keypad_controller/door_keypad_controller.ino", [(783, 845)]),
    "code6": ("esp32/door_keypad_controller/door_keypad_controller.ino", [(324, 354)]),
    "code7": ("esp32/door_keypad_controller/door_keypad_controller.ino", [(500, 543)]),
    "code8": ("esp32/esp32_cam/esp32_cam.ino", [(209, 230), (249, 270)]),
    "code9": ("esp32/esp32_cam/esp32_cam.ino", [(113, 125), (313, 344)]),
    "code10": ("python/backend/state.py", [(1163, 1231)]),
    "code11": ("python/backend/face_recognition_service.py", [(534, 572), (645, 666)]),
    "code12": ("python/backend/main.py", [(666, 721)]),
    "code13": ("python/backend/notifier.py", [(80, 114)]),
    "code14": ("app/lib/services/http_backend_service.dart", [(157, 179), (199, 205)]),
    "code15": ("app/lib/services/mjpeg_stream.dart", [(30, 65)]),
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


def terminal(name, text, title):
    esc = html.escape(text)
    doc = (f"<html><head><meta charset='utf-8'><style>body{{margin:0;background:#fff}}"
           f"#box{{display:inline-block;background:#0f1411;color:#d7e0d4;padding:12px 16px;border-radius:6px;"
           f"font-family:'DejaVu Sans Mono',monospace;font-size:13px;line-height:1.4}}"
           f".t{{color:#8b9a8e;margin-bottom:8px}}pre{{margin:0;white-space:pre}}"
           f".ok{{color:#43c285}}</style></head><body><div id='box'><div class='t'>{html.escape(title)}</div>"
           f"<pre>{esc}</pre></div></body></html>")
    hp = OUT / f"{name}.html"
    hp.write_text(doc, encoding="utf-8")
    subprocess.run(["node", str(HERE / "shot.js"), str(hp), str(OUT / f"{name}.png")], check=True,
                   env={**os.environ, "PLAYWRIGHT_MODULE": os.environ.get("PLAYWRIGHT_MODULE", "playwright")})
    hp.unlink()
    print("rendered", name)


if __name__ == "__main__":
    for nm, (rel, rng) in SNIPPETS.items():
        render(nm, rel, rng)
    if TERM:
        p = TERM / "pytest_out.txt"
        if p.exists():
            terminal("fig26", p.read_text(encoding="utf-8").replace(str(TERM), "...").strip(), "$ cd python && pytest -q")
        s = TERM / "simulate_out.txt"
        if s.exists():
            lines = s.read_text(encoding="utf-8").splitlines()
            terminal("fig27", "\n".join(lines[-32:]), "$ cd python && python simulate.py   (last 32 lines of the output)")

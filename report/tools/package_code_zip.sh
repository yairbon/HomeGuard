#!/usr/bin/env bash
# Packs only the code (tracked files; no secrets, no .claude/CLAUDE.md, no report/, no PDFs) into a ZIP
# that can be embedded in the report PDF with embed_zip.py.
#
#   report/tools/package_code_zip.sh <out.zip> [--light]
#
# --light leaves out the face-recognition model files (python/models/*.onnx, ~37 MB), which are
# third-party downloads; use it when the submission system limits the file size.
set -euo pipefail
out="${1:?usage: package_code_zip.sh <out.zip> [--light]}"
root="$(git rev-parse --show-toplevel)"
excludes=(':(exclude).claude' ':(exclude)CLAUDE.md' ':(exclude)report' ':(exclude)docs/*.pdf' ':(exclude)submission')
if [ "${2:-}" = "--light" ]; then
  excludes+=(':(exclude)python/models/*.onnx')
fi
out_abs="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"
rm -f "$out_abs"
git -C "$root" archive --format=zip --prefix=HomeGuard_Code/ -o "$out_abs" HEAD -- . "${excludes[@]}"
unzip -l "$out_abs" | tail -1

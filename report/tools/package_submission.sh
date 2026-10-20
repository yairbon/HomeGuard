#!/usr/bin/env bash
# Packs the submission ZIP: the final PDF + all source code (tracked files only,
# so secrets.h / state.json never get in) + docs.
#
#   report/tools/package_submission.sh <final_report.pdf> <out.zip>
#
# Excluded on purpose: .claude/ and CLAUDE.md (editor-agent settings and notes,
# not part of the project) and report/ (the report's own build sources; the PDF
# is added at the top level instead).
set -euo pipefail
pdf="${1:?usage: package_submission.sh <report.pdf> <out.zip>}"
out="${2:?usage: package_submission.sh <report.pdf> <out.zip>}"
root="$(git rev-parse --show-toplevel)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/HomeGuard_Submission/code"
cp "$pdf" "$stage/HomeGuard_Submission/$(basename "$pdf")"
git -C "$root" archive HEAD -- . ':(exclude).claude' ':(exclude)CLAUDE.md' ':(exclude)report' | tar -x -C "$stage/HomeGuard_Submission/code"
# a 3D-model folder is expected by the course; create it only if there is something to put in it
if [ -d "$root/3d" ]; then cp -r "$root/3d" "$stage/HomeGuard_Submission/3d"; fi
out_abs="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"
rm -f "$out_abs"
(cd "$stage" && zip -qr "$out_abs" HomeGuard_Submission)
unzip -l "$out_abs" | tail -1

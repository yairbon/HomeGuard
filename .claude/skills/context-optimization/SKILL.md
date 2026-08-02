---
name: context-optimization
description: How to work efficiently in the HomeGuard codebase — a 3-language, multi-service project where naive full-file reads and redundant re-verification waste real time and tokens. Use when planning how to approach a multi-file task here.
---

# Context Optimization (HomeGuard)

This project spans Flutter/Dart, Python, and C++ (3 separate ESP32
sketches), plus a non-obvious environment setup. Working efficiently here
means not re-deriving things that are already known, and not reading more
than the task needs.

## Check before re-deriving

- **`CLAUDE.md`** has the environment gotchas (AF_UNIX/WSL2, the Provider
  `lazy` bug, build-cache mixing) already diagnosed in detail. If a
  symptom matches, don't re-investigate from first principles — that's
  the whole reason it's written down.
- **`python/README.md`** and **`esp32/README.md`** have current, real
  status (not aspirational) for their respective pieces, including known
  unfixed gaps. Read these before assuming something isn't built yet.
- **`python/simulate.py`** already exercises every backend endpoint. Run
  it instead of hand-writing a fresh set of curl commands to check
  whether the backend still works after a change.

## Read narrowly, not exhaustively

- `backend_service.dart` (Dart) and `models.py` (Python) are the actual
  API contract between app and backend — mirror each other field-for-
  field. When changing one, that's the one other file you need to check,
  not the whole tree.
- The three ESP32 sketches are independent (separate boards, no shared
  state) — a change to one has zero implications for the other two.
  Don't re-read all three just because one changed.
- For a UI-only change, `theme/app_theme.dart` + the specific screen file
  is usually sufficient context — you don't need to re-read every screen
  to add one new one, just skim an existing similar screen as a pattern
  reference (e.g. `settings_screen.dart` for another form-heavy screen).

## Don't re-verify what didn't change

- A documentation-only pass doesn't need a live simulation re-run to
  prove logic is intact — `flutter analyze`/`dart analyze`, `flutter
  test`, a Python import check, and `arduino-cli compile` are enough to
  prove nothing broke syntactically. Save the live `simulate.py` run for
  changes that touch actual logic.
- If `flutter analyze` / a compile check already passed for a set of
  files, don't re-open and re-read them "just to be sure" without a
  specific reason — trust the tool result.

## Parallelize independent work

When a task decomposes into genuinely independent pieces touching
disjoint files (e.g. documenting Python backend vs. Flutter services vs.
ESP32 firmware — no shared state, no ordering dependency), that's a
signal to fan out rather than work through them one at a time in a single
long sequential pass.

## Don't guess at external facts

Version numbers, checksums, API endpoint shapes, and marketplace/plugin
manifests should be fetched and verified (or checked against an existing
file in the repo), not assumed from memory — this project has already
been burned once by a stale remembered download URL (the Android
cmdline-tools version mismatch). A wrong guess costs more tokens to
recover from than a single verification fetch would have cost upfront.

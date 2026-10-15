"""Keeps the keypad PIN in `esp32/*/secrets.h` in step with the live one.

## Why this exists, and what it is *not*

`secrets.h` is a C++ header. `KEYPAD_PIN` is compiled into the firmware
binary, so rewriting the file has **no effect on a board that is already
running** — it only changes what the *next* flash will contain. The live PIN
lives in the door controller's NVS (see `loadKeypadPin()` in the sketch), and
that is what `esp32_client.send_keypad_pin` updates.

So this module has exactly one job: stop a reflash from silently reverting
the PIN to an old value. Without it, someone reflashing the board months
later would hand it back the PIN from whenever `secrets.h` was last edited by
hand, and the app would show a PIN the hardware no longer accepts.

## Off unless asked for

Disabled unless `HOMEGUARD_SECRETS_H` names the file, because writing into a
source tree is only meaningful on a development machine — a deployed backend
has no `esp32/` directory, and shouldn't be able to write to one. Set it to
the door controller's header:

    HOMEGUARD_SECRETS_H=C:\\dev\\HomeGuard\\esp32\\door_keypad_controller\\secrets.h

Only the `KEYPAD_PIN` line is touched; every other byte of the file is
preserved, including the WiFi password and device key sitting beside it.
Callers must pass a PIN that has already cleared `is_valid_keypad_pin` —
digits only — which is what makes it safe to interpolate into a `#define`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("secrets_file")

SECRETS_FILE = os.environ.get("HOMEGUARD_SECRETS_H", "")

# Matches the whole `#define KEYPAD_PIN "…"` line, capturing the parts around
# the value so spacing and any trailing comment survive the rewrite.
_PIN_LINE = re.compile(r'^(\s*#\s*define\s+KEYPAD_PIN\s+")[^"\n]*("\s*.*)$', re.MULTILINE)


# Just the value, for reading it back (see read_keypad_pin).
_PIN_VALUE = re.compile(r'^\s*#\s*define\s+KEYPAD_PIN\s+"([^"\n]*)"', re.MULTILINE)


def is_enabled() -> bool:
    """Whether secrets-file syncing is switched on for this install."""
    return bool(SECRETS_FILE)


def read_keypad_pin() -> str:
    """The `KEYPAD_PIN` currently written in the configured header, or "" if
    syncing is off or the file or line can't be read.

    Used once, on a brand-new install, so the app's first password is the PIN
    the door keypad was compiled with instead of a separate well-known
    default. The caller must still validate the value: this returns whatever
    sits between the quotes.
    """
    if not SECRETS_FILE:
        return ""
    try:
        text = Path(SECRETS_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read secrets header %s: %s", SECRETS_FILE, exc)
        return ""
    match = _PIN_VALUE.search(text)
    return match.group(1) if match else ""


def update_keypad_pin(pin: str) -> bool:
    """Rewrites `KEYPAD_PIN` in the configured header. Returns whether it did.

    Every failure path returns False rather than raising: this runs as a
    side-effect of a PIN change that has already been applied to the board,
    and a source file that couldn't be updated must not turn a successful
    change into an error. The caller surfaces the result so the app can say
    the sync didn't happen.
    """
    if not SECRETS_FILE:
        return False

    path = Path(SECRETS_FILE)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read secrets header %s: %s", path, exc)
        return False

    updated, count = _PIN_LINE.subn(rf"\g<1>{pin}\g<2>", original)
    if count == 0:
        logger.warning("No KEYPAD_PIN line found in %s — leaving it untouched.", path)
        return False

    try:
        # Same temp-then-replace dance as state.py's _persist: a half-written
        # secrets.h would break the next compile, and this file also holds
        # the WiFi password and device key.
        temp = path.with_suffix(".h.tmp")
        temp.write_text(updated, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("Could not write secrets header %s: %s", path, exc)
        return False

    # The path, never the PIN — this logger's output is not privileged.
    logger.info("Updated KEYPAD_PIN in %s", path)
    return True

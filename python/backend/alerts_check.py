"""Sends a real test alert over every channel in alerts.local.json and prints the
outcome: `python -m backend.alerts_check` (or scripts\\test-alerts.ps1).

The same code path as Settings -> SEND TEST ALERT, without needing a login.
Prints reasons only, never a password, key or number.
"""

from __future__ import annotations

import asyncio
import sys

from . import alerts


def main() -> int:
    config = alerts.load_config()
    print("Set up:", config.describe())
    if not config.any_channel(escalation=True):
        print("Nothing to test yet: run scripts\\set-alert-email.ps1 first.")
        return 1
    results = asyncio.run(
        alerts.deliver(
            config,
            "HomeGuard test",
            "Test alert - this is how a real alarm will reach you.",
            escalation=True,
        )
    )
    failed = False
    for channel, reason in results.items():
        if reason:
            failed = True
            print(f"  {channel}: FAILED - {reason}")
        else:
            print(f"  {channel}: sent - check your phone")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

---
name: systematic-debugging
description: Use for any bug, test failure, or unexpected behavior in HomeGuard — before proposing a fix. Root-cause first, fix second. References this project's own past incidents as worked examples.
---

# Systematic Debugging (HomeGuard)

Don't guess-and-patch. Find the actual root cause, confirm it with
evidence, then fix — in that order. Two real incidents from this project
are worked examples of why the order matters.

## The 4 phases

**1. Reproduce and observe.** Get the exact failure in front of you
before theorizing. Read the actual error/log/stack trace — don't
paraphrase from memory. If it's intermittent or environment-specific,
that's data, not noise: note exactly when it does/doesn't happen.

**2. Isolate.** Cut the problem down to the smallest thing that still
fails. Strip away everything not implicated. Test one variable at a time
— when the native-Windows Gradle build failed, isolating meant testing
*independent JDK vendors* (ojdkbuild vs. Temurin) before testing *JDK
versions*, because changing both at once wouldn't have told us which one
mattered.

**3. Find the actual root cause — don't stop at the first plausible
explanation.** The first theory is often wrong or incomplete. Worked
example: `flutter build apk` failed with `Unable to establish loopback
connection`. The lazy fix would've been "try a different JDK." The real
investigation went: different JDK vendor → same failure → ruled out
vendor-specific bug → checked Winsock catalog directly → found AF_UNIX
registered for `SOCK_DGRAM` only, no `SOCK_STREAM` → *that* was root
cause, not "something's wrong with Java." Confirmed via `netsh winsock
show catalog`, not assumption.

**4. Fix the root cause, verify with evidence, document if it's
non-obvious.** A fix isn't done until you've actually run the thing that
was failing and watched it succeed — not "this should work now." If the
root cause was subtle enough that a future session might re-investigate
it from scratch, write it down (see `CLAUDE.md`'s known-issues section).

## Don't stop at "it compiles" or "the code looks right"

The bypassed-sensor bug (`trigger_alert`/`triggerAlert` not checking the
`bypassed` flag) didn't throw errors, didn't fail any type check, and the
code read as reasonable at a glance. It was only caught by asking "does
this behavior actually match what Home mode is supposed to do?" and
tracing the logic by hand. Static checks (analyze/compile) prove the code
is *valid*, not that it's *correct* — those are different questions.

## When it's genuinely a platform/environment issue, not your code

Sometimes the root cause really is outside the codebase (see the AF_UNIX
saga above, or the WSL2 hang that turned out to be a stale process, not a
WSL bug). Signs you're dealing with this: the same failure reproduces
across unrelated code changes, or across different tool versions/vendors.
Don't keep rewriting application code against an environment bug — verify
the environment first (as above), then work around *that*, specifically.

## Applying this in HomeGuard

- Check `CLAUDE.md`'s known-traps section *first* — if a failure matches
  something already diagnosed (AF_UNIX, the Provider `lazy` gotcha, the
  build-cache-mixing Dart errors), don't re-investigate from scratch.
- For backend bugs, `python/simulate.py` is the fast way to confirm a fix
  actually holds — it's a real regression suite, not just a smoke test.
- For Flutter bugs, `flutter analyze` catches type errors but not logic
  bugs (see the bypass example) — trace the actual runtime behavior too.

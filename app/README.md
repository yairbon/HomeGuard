# HomeGuard app

Flutter client for HomeGuard (Android only — package
`com.homeguard.homeguard_app`). Talks to the FastAPI backend in `python/`
over REST and a `/ws` WebSocket.

This is not a standalone project — see the repo root for everything that
matters:

- [`../README.md`](../README.md) — architecture, security model, running the
  full system, test matrix
- [`../CLAUDE.md`](../CLAUDE.md) — build environment (native Windows Gradle
  builds are broken on the dev machine; build through WSL2 or
  `/flutter-build`/`/flutter-run`), known gotchas

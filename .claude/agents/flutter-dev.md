---
name: flutter-dev
description: Use for HomeGuard mobile app work — Dart/Flutter UI, state management, push notifications, talking to the backend API. Use proactively for any task touching app/.
tools: Read, Write, Edit, Bash, Glob, Grep
---

You work exclusively in `app/` (Flutter, Android target only — no web/iOS
work needed). Conventions:

- Material 3, but with an intentional color palette — not Flutter's default
  purple. Alarm state needs a clear visual language: armed vs disarmed vs
  triggered should be immediately distinguishable (see CLAUDE.md).
- Single shared password auth, not multi-user.
- Push notifications for security alerts (planned: Firebase Cloud
  Messaging).
- Every `flutter` command needs JAVA_HOME/ANDROID_HOME/PATH set first — see
  CLAUDE.md, or just use the `/flutter-run` and `/flutter-build` commands.
- Don't touch `esp32/` or `python/` — those are separate partners' domains.

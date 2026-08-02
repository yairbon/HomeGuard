---
description: Build the HomeGuard Flutter app debug APK (via WSL2 — native Windows builds are broken on this machine)
---

Native Windows Gradle builds fail on this machine (known Winsock/AF_UNIX
defect — see CLAUDE.md). Build inside WSL2 instead, using the Bash or
PowerShell tool:

```bash
wsl -d Ubuntu -- bash -c '
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export ANDROID_HOME=/opt/android-sdk
export PATH="/opt/flutter/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/cmdline-tools/latest/bin:$PATH"
cd /mnt/c/dev/HomeGuard/app
flutter build apk --debug
'
```

The built APK lands at `C:\dev\HomeGuard\app\build\app\outputs\flutter-apk\app-debug.apk`.
Install it on a connected device with the Windows-side adb (already
installed): `C:\dev\android-sdk\platform-tools\adb.exe install <path>`.

If a build fails with unrelated-looking Dart parse errors (e.g. "method X
isn't defined" cascades), it's likely a stale build cache from mixing
native-Windows and WSL2 builds — run `flutter clean && flutter pub get`
first, inside the same WSL2 invocation, before re-debugging from scratch.

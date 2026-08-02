---
description: Run/install the HomeGuard Flutter app on a connected Android device
---

Native Windows Gradle builds are broken on this machine (known Winsock/
AF_UNIX defect — see CLAUDE.md), so building happens in WSL2, but a
connected Android device is only visible to Windows-side adb. Workflow:

1. Build via `/flutter-build` (runs inside WSL2).
2. **Before** installing/launching, set up the USB reverse tunnel so the
   app's `http://localhost:8000` calls (see `main.dart`) actually reach
   the backend on this PC — doing this *after* launch causes the app's
   first WebSocket connection attempt to fail silently (REST calls like
   arm/disarm still succeed, but live state never reflects it):
```powershell
& "C:\dev\android-sdk\platform-tools\adb.exe" reverse tcp:8000 tcp:8000
```
3. Install and launch using the Windows-side adb:
```powershell
& "C:\dev\android-sdk\platform-tools\adb.exe" install -r "C:\dev\HomeGuard\app\build\app\outputs\flutter-apk\app-debug.apk"
& "C:\dev\android-sdk\platform-tools\adb.exe" shell monkey -p com.homeguard.homeguard_app -c android.intent.category.LAUNCHER 1
```

A LAN-IP connection (backend machine's real WiFi IP instead of the USB
tunnel) was also tried and failed on this network specifically — a raw
TCP test from the phone (`adb shell curl`/`nc`, not `ping` — Windows
blocks ICMP by default on the Public profile independent of any TCP
allow rule) showed the connection just timing out, pointing at
router-level client isolation or a non-Windows-Defender firewall layer.
The USB-reverse approach is what actually works here; don't re-try the
LAN-IP route without checking with the user first.

Live hot-reload via `flutter run` isn't wired up yet — that needs WSL2 to
see the device (via `usbipd-win` USB passthrough, or `adb connect` over
TCP/IP to the phone's IP). Ask the user which they'd prefer if this comes
up, rather than assuming.

If no physical device is connected, ask the user whether to target
`flutter run -d windows` or `-d chrome` instead (both work natively on
Windows, no Android toolchain needed) for UI iteration without a device.

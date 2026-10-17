# HomeGuard: demo video shot list

Goal: show that every must-have works, then the extras. About 5 minutes. Film the
room with one camera, and record the phone screen (or film the phone) for the app.
Keep the siren short: disarm within a few seconds each time.

## Before you press record (2 minutes)

- Backend running (`scripts\start-backend.ps1`), **do not restart it while filming**.
- All three boards online in the app: door/keypad, ultrasonic + force, camera.
- Ultrasonic sensor aimed at open space 1.5 to 3.5 m away, nobody in front of it
  when it was powered up (otherwise it rejects its calibration).
- Door board and the camera powered at the door (power bank or cable); reed
  switch and magnet aligned (door closed reads "closed").
- App: logged in on the phone, **Settings -> "Send alerts by email" ON**, your
  address saved, then press SEND TEST ALERT once and see it arrive.
- Face enrolled (Faces screen) and "face disarm" switched on in the Camera tab if
  you will show it. Good light on your face, 70 cm from the camera.
- Phone volume up, so the email notification is audible; keep the phone in frame
  for the email shot.

## Shots (what to do, what to say, what it proves)

1. **The system (10 s).** Pan over: the door board with keypad and buzzers, the
   ultrasonic + force sensor board at the window, the camera, the laptop, the
   phone. Say: "Three ESP32 boards report to a Python backend; this phone app
   controls it."
2. **Everything online (10 s).** App dashboard: three boards online, Disarmed.
3. **Arm from the keypad (15 s).** Press `*`: you hear a click. Open the door,
   close it: short-short-long "armed" sound; app turns to ARMED. Proves: keypad
   arming, sound feedback, live app.
4. **Entry delay and disarm with the PIN (30 s).** Open the door: app shows the
   30-second countdown, the board gives quiet silence then speeding-up beeps.
   Type a wrong PIN + `#`: one long buzz. Type the right PIN + `#`: long-short-short
   "disarmed" sound, app DISARMED. Say that beeps pause while you type. Proves:
   physical keypad disarm, entry delay, wrong-PIN handling.
5. **Intrusion by timeout, with email (40 s).** Arm again, open the door and wait
   the 30 seconds: siren, app TRIGGERED, and the **email arrives on the phone**
   (show it, with the time). Disarm with the PIN: siren stops at once. Proves:
   audible alarm, alarm email.
6. **Ultrasonic tripwire (30 s).** Arm Away from the app (3-second arming, armed
   sound). Walk through the beam: siren, app TRIGGERED, event in the log. Disarm
   from the app: siren cuts, "disarmed" sound. Proves: remote arm/disarm,
   intrusion detection by distance.
7. **Forced entry (20 s).** Arm Away, knock on the window where the force sensor
   is taped: siren, TRIGGERED. Disarm. Proves: forced-entry detection.
8. **Home mode (15 s, optional).** Arm Home, knock on the window: nothing, because
   Home mode ignores that sensor. Open the door: the entry countdown starts.
9. **Arming refused (10 s).** Leave the door open and press `*` on the keypad: rapid
   four-pulse refusal sound. In the app, tap Arm: "Can't arm: the door is open".
10. **Camera and face disarm (40 s).** Camera tab: live picture. Arm, open the
    door, look at the camera: it disarms in about 3 seconds and the log says
    "Disarmed by Face ID". Say it is convenience, with a liveness check, not
    security.
11. **Event log and settings (30 s).** Events tab with the history and the
    filter. Settings: change the PIN (digits only) and say the keypad is updated
    over WiFi; show the email recipients and the "Send alerts by email" switch.
12. **Close (10 s).** One sentence: ESP32 firmware (C++), FastAPI backend with face
    recognition, Flutter app; alarm, camera, email, keypad.

## Practical

- Do one dry run without recording; the first siren is always a surprise.
- If a take goes wrong, disarm and start the shot again; the log keeps the history.
- Show the phone and the physical board in the same shot when something happens
  (the siren and the app turning red together is the proof).

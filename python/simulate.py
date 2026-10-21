"""Exhaustive live simulation against the real running backend
(http://localhost:8000). Not a unit test suite (no mocking) — this hits
the actual server the Flutter app talks to, to prove every function works
end-to-end.

Complements `tests/` (pytest, in-process, fast): this one runs against a real
uvicorn process over a real socket, so it also exercises the ASGI server, the
WebSocket transport and real wall-clock timing. Run the pytest suite for quick
feedback while editing; run this before believing the system actually works.

Requires the backend to be running with a known device key:

    HOMEGUARD_DEVICE_KEY=simulation-device-key \\
        python -m uvicorn backend.main:app --port 8000

Also expects a fresh state file (the login tests assume the default password),
so delete `state.json` first if a previous run changed it.
"""

import os
import time

import httpx

BASE = os.environ.get("HOMEGUARD_BASE_URL", "http://localhost:8000")
DEVICE_KEY = os.environ.get("HOMEGUARD_DEVICE_KEY", "simulation-device-key")

# The factory-default password on a fresh install (DEFAULT_PASSWORD in
# state.py) — free-form, so it predates the digit-only constraint
# `change_password` now enforces on any *new* value (see that method: the
# login password and the physical keypad's PIN are now one shared secret).
# Valid as an old_password forever; can never be set again as a new_password
# once changed away from — which is exactly what this script now does below,
# so PIN_A/PIN_B (not DEFAULT_PASSWORD) are what its password-change checks
# actually cycle between from that point on.
DEFAULT_PASSWORD = "homeguard"
PIN_A = "24681"
PIN_B = "97531"

# trust_env=False so an HTTP(S)_PROXY set in the environment doesn't send
# localhost traffic through a proxy that can't route back to it.
c = httpx.Client(base_url=BASE, timeout=10.0, trust_env=False)
# Separate client for the ESP32 boards' credential — they authenticate with
# the shared device key, not an app session token.
device = httpx.Client(
    base_url=BASE, timeout=10.0, trust_env=False, headers={"X-Device-Key": DEVICE_KEY}
)

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def section(title):
    print(f"\n=== {title} ===")


def authenticate(password=DEFAULT_PASSWORD):
    """Logs in and installs the returned session token on `c` for every
    later request. Returns the token."""
    token = c.post("/login", json={"password": password}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return token


# ---- Health / auth enforcement ------------------------------------------
section("Health & auth enforcement")
r = c.get("/health")
check("GET /health (public)", r.status_code == 200 and r.json()["ok"] is True)

# Every one of these used to return 200 with no credentials at all — anyone
# who could reach the backend could disarm an armed alarm. These checks are
# the regression test for that.
anon = httpx.Client(base_url=BASE, timeout=10.0, trust_env=False)
check("GET /state without a token is rejected", anon.get("/state").status_code == 401)
check("POST /disarm without a token is rejected", anon.post("/disarm").status_code == 401)
check(
    "POST /arm without a token is rejected",
    anon.post("/arm", json={"mode": "away"}).status_code == 401,
)
check(
    "POST /change-password without a token is rejected",
    anon.post(
        "/change-password", json={"old_password": DEFAULT_PASSWORD, "new_password": "1234"}
    ).status_code
    == 401,
)
check(
    "forged sensor event without the device key is rejected",
    anon.post("/esp32/sensor-event", params={"sensor": "door", "reason": "forged"}).status_code
    == 401,
)
check(
    "forged 'system disarmed' report without the device key is rejected",
    anon.post("/esp32/state-change", json={"armed": False}).status_code == 401,
)
check(
    "wrong device key is rejected",
    httpx.Client(base_url=BASE, timeout=10.0, trust_env=False)
    .post("/esp32/heartbeat", params={"board": "ultrasonic"}, headers={"X-Device-Key": "wrong"})
    .status_code
    == 401,
)

# ---- Auth -----------------------------------------------------------------
section("Auth")
r = c.post("/login", json={"password": "wrong-password"})
body = r.json()
check("wrong password rejected", body["ok"] is False)
check("no token issued on failure", body["token"] is None, body)
check("attempts_remaining reported", body["attempts_remaining"] == 2, body)

r = c.post("/login", json={"password": DEFAULT_PASSWORD})
body = r.json()
check("correct password accepted", body["ok"] is True)
check("token issued on success", isinstance(body["token"], str) and len(body["token"]) > 20)
c.headers["Authorization"] = f"Bearer {body['token']}"

r = c.get("/state")
check("token grants access to /state", r.status_code == 200)

# Fail fast, and legibly, if this isn't a fresh server.
#
# Several checks below assert on state that only holds on a clean start — a
# board that has never reported, the default password. Running against a
# server left over from a previous run produces a confusing failure a hundred
# lines later (which is exactly what happened once: a stale uvicorn still held
# port 8000, so a new one silently failed to bind and this script talked to
# the old process).
if any(b["last_seen"] is not None for b in r.json()["boards"]):
    raise SystemExit(
        "Backend is not freshly started — some boards have already reported.\n"
        "Stop the running backend, delete state.json, and start it again."
    )

state = r.json()
check("state has 3 sensors", len(state["sensors"]) == 3, state["sensors"])
check(
    "sensor types are ultrasonic/door/vibration",
    {s["type"] for s in state["sensors"]} == {"ultrasonic", "door", "vibration"},
)

r = c.post("/change-password", json={"old_password": DEFAULT_PASSWORD, "new_password": PIN_A})
body = r.json()
check("change password (correct old pw)", body["ok"] is True)
# /settings/keypad-pin was folded into this route — same two extra outcomes
# it used to report on its own, now reported here on every credential
# change. No door controller or HOMEGUARD_SECRETS_H is configured for this
# script, so both are expected False rather than absent.
check("response reports the hardware-ack outcome", body["hardware"] is False, body)
check("response reports the secrets.h-sync outcome", body["secrets_updated"] is False, body)
check("own token survives a password change", c.get("/state").status_code == 200)

r = c.post("/login", json={"password": DEFAULT_PASSWORD})
check("old password now rejected", r.json()["ok"] is False)

r = c.post("/login", json={"password": PIN_A})
check("new password works", r.json()["ok"] is True)

authenticate(PIN_A)
r = c.post("/change-password", json={"old_password": PIN_A, "new_password": PIN_B})
# Not "changed back to DEFAULT_PASSWORD" — that value can never be a
# new_password again once the account has moved into the digits-only regime
# (see the comment on DEFAULT_PASSWORD above), so this cycles to a second
# PIN instead, which is what the rest of this section now assumes as the
# account's password/PIN for the remainder of the script.
check("changed password to a second value", r.json()["ok"] is True)

# A second device's token must stop working once the password changes.
other = httpx.Client(base_url=BASE, timeout=10.0, trust_env=False)
other_token = other.post("/login", json={"password": PIN_B}).json()["token"]
other.headers["Authorization"] = f"Bearer {other_token}"
check("second device can read state", other.get("/state").status_code == 200)
authenticate(PIN_B)
c.post("/change-password", json={"old_password": PIN_B, "new_password": PIN_B})
check(
    "other devices' tokens are revoked by a password change",
    other.get("/state").status_code == 401,
)

# Logout must invalidate server-side, not just locally.
throwaway = httpx.Client(base_url=BASE, timeout=10.0, trust_env=False)
tok = throwaway.post("/login", json={"password": PIN_B}).json()["token"]
throwaway.headers["Authorization"] = f"Bearer {tok}"
check("token works before logout", throwaway.get("/state").status_code == 200)
throwaway.post("/logout")
check("token is dead after logout", throwaway.get("/state").status_code == 401)

# Lockout: 3 wrong attempts in a row
for i in range(3):
    r = c.post("/login", json={"password": f"nope{i}"})
final = r.json()
check("locked out after 3 wrong attempts", final["locked_out"] is True, final)
r = c.post("/login", json={"password": PIN_B})
check("correct password still rejected while locked out", r.json()["ok"] is False)
print(f"  (waiting {final['lockout_remaining_seconds']}s for lockout to clear...)")
time.sleep(final["lockout_remaining_seconds"] + 1)
r = c.post("/login", json={"password": PIN_B})
check("login works again after lockout expires", r.json()["ok"] is True)
authenticate(PIN_B)

# /change-password used to be an unthrottled password oracle: it enforced no
# attempt limit at all, so the 3-attempt lockout could be sidestepped entirely
# by guessing there instead.
#
# new_password below ("0000") is a validly-*shaped* decoy on purpose:
# old_password is checked, and found wrong, before new_password's
# digits-only shape is ever considered (see AppState.change_password), so
# this still exercises the lockout regardless of what new_password looks
# like — a caller can't dodge it by pairing each guess with a value that
# would 422 anyway.
for i in range(3):
    r = c.post("/change-password", json={"old_password": f"guess{i}", "new_password": "0000"})
check(
    "wrong old-password guesses trip the same lockout",
    c.post("/login", json={"password": PIN_B}).json()["locked_out"] is True,
)
print("  (waiting out that lockout...)")
time.sleep(31)
authenticate(PIN_B)

# ---- Arm / disarm timing --------------------------------------------------
section("Arm / disarm")
r = c.post("/disarm")  # ensure known baseline
r = c.post("/arm", json={"mode": "away"})
check("arm away accepted", r.status_code == 200)
state = c.get("/state").json()
check(
    "state is transitional right after arm",
    state["alarm_state"] == "transitional",
    state["alarm_state"],
)
time.sleep(3.5)
state = c.get("/state").json()
check(
    "state is armed after exit delay", state["alarm_state"] == "armed", state["alarm_state"]
)
r = c.post("/disarm")
state = c.get("/state").json()
check("disarm works", state["alarm_state"] == "disarmed")

# ---- Home mode + bypass ---------------------------------------------------
section("Home mode sensor bypass")
c.post("/arm", json={"mode": "home"})
time.sleep(3.5)
state = c.get("/state").json()
vib = next(s for s in state["sensors"] if s["type"] == "vibration")
check("vibration sensor is bypassed in Home mode", vib["bypassed"] is True, vib)
dist = next(s for s in state["sensors"] if s["type"] == "ultrasonic")
check("distance sensor is bypassed in Home mode", dist["bypassed"] is True, dist)

r = c.post(
    "/trigger-alert", json={"source": "vibration", "reason": "test vibration while bypassed"}
)
state = c.get("/state").json()
check(
    "bypassed vibration sensor does NOT escalate to alarm",
    state["alarm_state"] == "armed",
    f"expected still 'armed', got {state['alarm_state']}",
)

r = c.post(
    "/trigger-alert", json={"source": "ultrasonic", "reason": "test distance while bypassed"}
)
state = c.get("/state").json()
check(
    "bypassed distance sensor does NOT escalate to alarm",
    state["alarm_state"] == "armed",
    f"expected still 'armed', got {state['alarm_state']}",
)

r = c.post("/trigger-alert", json={"source": "door", "reason": "test door while armed home"})
state = c.get("/state").json()
check(
    "non-bypassed door sensor DOES escalate to alarm",
    state["alarm_state"] == "triggered",
    state["alarm_state"],
)
c.post("/disarm")

# The controller reporting "armed" used to lose Home mode's bypass entirely,
# because that path had no idea which mode had been requested.
section("Home-mode bypass survives a controller state report")
c.post("/disarm")
c.post("/arm", json={"mode": "home"})
device.post("/esp32/state-change", json={"armed": True})  # controller wins the race
state = c.get("/state").json()
check(
    "controller report armed the system", state["alarm_state"] == "armed", state["alarm_state"]
)
vib = next(s for s in state["sensors"] if s["type"] == "vibration")
check("vibration is STILL bypassed after the controller's report", vib["bypassed"] is True, vib)
c.post(
    "/trigger-alert", json={"source": "vibration", "reason": "vibration after controller report"}
)
state = c.get("/state").json()
check(
    "bypassed vibration still does not escalate after a controller report",
    state["alarm_state"] == "armed",
    state["alarm_state"],
)
c.post("/disarm")

# ---- Sensor-attributed alerts (all 3 sensors, Away mode) --------------------
section("Sensor-attributed alerts (Away mode, all sensors)")
for sensor in ["ultrasonic", "door", "vibration"]:
    c.post("/arm", json={"mode": "away"})
    time.sleep(3.5)
    c.post("/trigger-alert", json={"source": sensor, "reason": f"{sensor} breach test"})
    state = c.get("/state").json()
    check(f"{sensor} breach triggers alarm", state["alarm_state"] == "triggered")
    reading = next(s for s in state["sensors"] if s["type"] == sensor)
    check(f"{sensor} marked not-ok after breach", reading["ok"] is False)
    latest_event = state["events"][0]
    check(f"{sensor} breach logged with correct category", latest_event["category"] == "alert")
    c.post("/disarm")

# ---- Faces ------------------------------------------------------------------
section("Faces CRUD")
r = c.get("/faces")
initial_count = len(r.json())
check("a fresh install has no approved faces", initial_count == 0, initial_count)

r = c.post("/faces", json={"name": "Simulation Test Person"})
new_face = r.json()
check("POST /faces creates a face", new_face["name"] == "Simulation Test Person")

r = c.get("/faces")
check("new face appears in list", len(r.json()) == initial_count + 1)

r = c.delete(f"/faces/{new_face['id']}")
check("DELETE /faces/{id} works", r.json()["ok"] is True)

r = c.get("/faces")
check("deleted face no longer in list", len(r.json()) == initial_count)

# Face ids must never be recycled — a reused id would attach a new person to
# the previous holder's training photos on disk.
a = c.post("/faces", json={"name": "Id Reuse A"}).json()
c.delete(f"/faces/{a['id']}")
b = c.post("/faces", json={"name": "Id Reuse B"}).json()
check("face ids are not recycled after a delete", a["id"] != b["id"], f"{a['id']} vs {b['id']}")
c.delete(f"/faces/{b['id']}")

# ---- Face ID toggle + camera recognize (no-face image) ----------------------
section("Face ID / camera")
r = c.post("/face-id-enabled", json={"enabled": False})
check("disable face ID", r.json()["ok"] is True)
r = c.post("/face-id-enabled", json={"enabled": True})
check("re-enable face ID", r.json()["ok"] is True)

# A genuinely valid 1x1 RGB PNG — decodable, just has no face in it.
#
# The previous version of this constant had a corrupt IDAT CRC, so OpenCV
# could never decode it. The check still passed, because the old recognizer
# returned early on "not trained yet" before attempting to decode — so this
# was only ever asserting that an unreadable image and an untrained model
# together produce no match. Both halves of that are now tested separately,
# with an image that actually decodes.
tiny_png = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63606060000000040001f61738550000000049454e44ae426082"
)
r = c.post("/camera/recognize", files={"photo": ("blank.png", tiny_png, "image/png")})
check("recognize on faceless image returns matched:false", r.json()["matched"] is False, r.json())

# An undecodable upload must be a clean 422, not a 500 with a traceback.
corrupt = b"\x89PNG\r\n\x1a\n" + b"this is not actually a png"
r = c.post("/camera/recognize", files={"photo": ("corrupt.png", corrupt, "image/png")})
check(
    "corrupt image to /camera/recognize returns 422, not 500", r.status_code == 422, r.status_code
)
face_for_upload = c.post("/faces", json={"name": "Upload Probe"}).json()
r = c.post(
    f"/faces/{face_for_upload['id']}/photo",
    files={"photo": ("corrupt.png", corrupt, "image/png")},
)
check("corrupt training photo returns 422, not 500", r.status_code == 422, r.status_code)
r = c.post(
    f"/faces/{face_for_upload['id']}/photo",
    files={"photo": ("blank.png", tiny_png, "image/png")},
)
check("faceless training photo returns 422", r.status_code == 422, r.status_code)
c.delete(f"/faces/{face_for_upload['id']}")

# The old demo endpoint that faked a recognition (and a log entry) is gone.
r = c.post("/debug/simulate-face-recognition")
check("the demo recognition endpoint no longer exists", r.status_code in (404, 405), r.status_code)

# ---- Settings ----------------------------------------------------------------
section("Settings")
c.post("/settings/night-mode", json={"enabled": True})
state = c.get("/state").json()
check("night_mode persisted", state["night_mode"] is True)
c.post("/settings/night-mode", json={"enabled": False})
state = c.get("/state").json()
check("night_mode toggled back off", state["night_mode"] is False)

c.post("/settings/emergency-number", json={"number": "100"})
state = c.get("/state").json()
check("emergency_number persisted", state["emergency_number"] == "100")

c.post("/settings/police-call-delay", json={"seconds": 45})
state = c.get("/state").json()
check("police_call_delay_seconds persisted", state["police_call_delay_seconds"] == 45)
c.post("/settings/police-call-delay", json={"seconds": 60})  # restore default

# ---- Entry delay ---------------------------------------------------------------
section("Entry delay")
c.post("/disarm")
c.post("/arm", json={"mode": "away"})
time.sleep(3.5)
r = device.post("/esp32/entry-delay", json={"seconds": 25})
check("entry-delay report accepted", r.status_code == 200, r.status_code)
state = c.get("/state").json()
check("state becomes entryDelay", state["alarm_state"] == "entryDelay", state["alarm_state"])
check(
    "countdown is exposed to the app",
    0 < state["entry_delay_seconds_remaining"] <= 25,
    state["entry_delay_seconds_remaining"],
)
# The controller's door alert, arriving when its countdown expires, must be
# able to escalate from entryDelay — not only from armed.
c.post("/trigger-alert", json={"source": "door", "reason": "not disarmed in time"})
state = c.get("/state").json()
check("entry delay escalates to the alarm", state["alarm_state"] == "triggered")
c.post("/disarm")
state = c.get("/state").json()
check("disarming clears the countdown", state["entry_delay_seconds_remaining"] == 0)

# ---- Face disarm ---------------------------------------------------------------
section("Face ID disarm (opt-in)")
state = c.get("/state").json()
check("face disarm is off by default", state["face_disarm_enabled"] is False)

# The old demo endpoint that faked a recognition is gone, and a live check of
# "a recognized face disarms" would need a real enrolled face in front of the
# camera. That decision (face disarm on/off x live/not live x armed/not armed,
# attribution in the log) is covered in-process by tests/test_face_disarm_and_schedule.py.
r = c.post("/face-disarm-enabled", json={"enabled": True})
check("face disarm can be enabled", r.json()["ok"] is True)
state = c.get("/state").json()
check("face disarm shows as enabled in /state", state["face_disarm_enabled"] is True)
c.post("/face-disarm-enabled", json={"enabled": False})

# ---- Notifications / night mode --------------------------------------------------
section("Notifications & night-mode schedule")
r = c.post("/settings/ntfy", json={"server": "https://ntfy.example", "topic": "sim-topic"})
check("ntfy settings accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check("ntfy settings persisted", state["ntfy_topic"] == "sim-topic", state["ntfy_topic"])
check(
    "the topic is not leaked into the event log",
    all("sim-topic" not in e["message"] for e in state["events"]),
)
r = c.post("/settings/alerts/test")
check("test alert reports configured", r.json()["ok"] is True, r.json())
c.post("/settings/ntfy", json={"server": "https://ntfy.sh", "topic": ""})
r = c.post("/settings/alerts/test")
check("test alert reports not-configured when off", r.json()["ok"] is False, r.json())

r = c.post("/settings/night-mode-hours", json={"arm_hour": 1, "disarm_hour": 9})
check("night-mode hours accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check(
    "night-mode hours persisted",
    (state["night_mode_arm_hour"], state["night_mode_disarm_hour"]) == (1, 9),
    (state["night_mode_arm_hour"], state["night_mode_disarm_hour"]),
)
c.post("/settings/night-mode-hours", json={"arm_hour": 99, "disarm_hour": -5})
state = c.get("/state").json()
check(
    "out-of-range hours are clamped, not rejected",
    (state["night_mode_arm_hour"], state["night_mode_disarm_hour"]) == (23, 0),
    (state["night_mode_arm_hour"], state["night_mode_disarm_hour"]),
)
c.post("/settings/night-mode-hours", json={"arm_hour": 23, "disarm_hour": 7})

# ---- ESP32 endpoints -----------------------------------------------------------
section("ESP32 endpoints")
r = device.post("/esp32/heartbeat", params={"board": "ultrasonic"})
check("heartbeat accepted with device key", r.json()["ok"] is True)
state = c.get("/state").json()
check("esp32_online becomes true after heartbeat", state["esp32_online"] is True)

check("state reports all three boards", len(state["boards"]) == 3, state["boards"])
by_name = {b["name"]: b for b in state["boards"]}
check(
    "heartbeating board is online",
    by_name["ultrasonic"]["online"] is True,
    by_name["ultrasonic"],
)
check(
    "boards that never reported are offline",
    by_name["vibration"]["online"] is False and by_name["vibration"]["last_seen"] is None,
    by_name["vibration"],
)

c.post("/arm", json={"mode": "away"})
time.sleep(3.5)
r = device.post(
    "/esp32/sensor-event", params={"sensor": "door", "reason": "physical door sensor test"}
)
check("esp32 sensor-event accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check("esp32 sensor-event triggers alarm", state["alarm_state"] == "triggered")

r = device.post("/esp32/state-change", json={"armed": False})
check("esp32 state-change accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check("esp32-reported disarm reflected in state", state["alarm_state"] == "disarmed")

r = device.post("/esp32/state-change", json={"armed": True})
state = c.get("/state").json()
check(
    "esp32-reported arm reflected in state (no exit delay — controller already did its own)",
    state["alarm_state"] == "armed",
)
c.post("/disarm")

# A board that stops reporting must age out. This is what the old single
# latching `esp32_online` flag could never do — once set, it stayed true
# forever, so a dead board still showed as ONLINE in the app.
section("Board liveness ages out (takes ~35s)")
device.post("/esp32/heartbeat", params={"board": "vibration"})
state = c.get("/state").json()
check(
    "vibration board online right after its heartbeat",
    next(b for b in state["boards"] if b["name"] == "vibration")["online"] is True,
)
print("  (waiting 35s for the heartbeat to go stale...)")
time.sleep(35)
state = c.get("/state").json()
check(
    "vibration board goes offline once heartbeats stop",
    next(b for b in state["boards"] if b["name"] == "vibration")["online"] is False,
    state["boards"],
)

print(f"\n{'=' * 50}\n{passed} passed, {failed} failed\n{'=' * 50}")
raise SystemExit(1 if failed else 0)

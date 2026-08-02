"""Exhaustive live simulation against the real running backend
(http://localhost:8000). Not a unit test suite (no mocking) — this hits
the actual server the Flutter app talks to, to prove every function
works end-to-end, including the bypassed-sensor bug fix.
"""

import time
import httpx

BASE = "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=10.0)

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


# ---- Health / state -----------------------------------------------------
section("Health & state")
r = c.get("/health")
check("GET /health", r.status_code == 200 and r.json()["ok"] is True)

r = c.get("/state")
check("GET /state", r.status_code == 200)
state = r.json()
check("state has 3 sensors", len(state["sensors"]) == 3, state["sensors"])
check("sensor types are ultrasonic/door/vibration",
      {s["type"] for s in state["sensors"]} == {"ultrasonic", "door", "vibration"})

# ---- Auth -----------------------------------------------------------------
section("Auth")
r = c.post("/login", json={"password": "wrong-password"})
body = r.json()
check("wrong password rejected", body["ok"] is False)
check("attempts_remaining reported", body["attempts_remaining"] == 2, body)

r = c.post("/login", json={"password": "homeguard"})
check("correct password accepted", r.json()["ok"] is True)

r = c.post("/change-password", json={"old_password": "homeguard", "new_password": "homeguard2"})
check("change password (correct old pw)", r.json()["ok"] is True)

r = c.post("/login", json={"password": "homeguard"})
check("old password now rejected", r.json()["ok"] is False)

r = c.post("/login", json={"password": "homeguard2"})
check("new password works", r.json()["ok"] is True)

r = c.post("/change-password", json={"old_password": "homeguard2", "new_password": "homeguard"})
check("changed password back to homeguard", r.json()["ok"] is True)

# Lockout: 3 wrong attempts in a row
for i in range(3):
    r = c.post("/login", json={"password": f"nope{i}"})
final = r.json()
check("locked out after 3 wrong attempts", final["locked_out"] is True, final)
r = c.post("/login", json={"password": "homeguard"})
check("correct password still rejected while locked out", r.json()["ok"] is False)
print(f"  (waiting {final['lockout_remaining_seconds']}s for lockout to clear...)")
time.sleep(final["lockout_remaining_seconds"] + 1)
r = c.post("/login", json={"password": "homeguard"})
check("login works again after lockout expires", r.json()["ok"] is True)

# ---- Arm / disarm timing --------------------------------------------------
section("Arm / disarm")
r = c.post("/disarm")  # ensure known baseline
r = c.post("/arm", json={"mode": "away"})
check("arm away accepted", r.status_code == 200)
state = c.get("/state").json()
check("state is transitional right after arm", state["alarm_state"] == "transitional", state["alarm_state"])
time.sleep(3.5)
state = c.get("/state").json()
check("state is armed after exit delay", state["alarm_state"] == "armed", state["alarm_state"])
r = c.post("/disarm")
state = c.get("/state").json()
check("disarm works", state["alarm_state"] == "disarmed")

# ---- Home mode + bypass bug fix (the important one) ------------------------
section("Home mode sensor bypass (regression test for the bug I fixed)")
c.post("/arm", json={"mode": "home"})
time.sleep(3.5)
state = c.get("/state").json()
vib = next(s for s in state["sensors"] if s["type"] == "vibration")
check("vibration sensor is bypassed in Home mode", vib["bypassed"] is True, vib)

r = c.post("/trigger-alert", json={"source": "vibration", "reason": "test vibration while bypassed"})
state = c.get("/state").json()
check(
    "bypassed vibration sensor does NOT escalate to alarm",
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
check("GET /faces returns seeded faces", initial_count >= 2)

r = c.post("/faces", json={"name": "Simulation Test Person"})
new_face = r.json()
check("POST /faces creates a face", new_face["name"] == "Simulation Test Person")

r = c.get("/faces")
check("new face appears in list", len(r.json()) == initial_count + 1)

r = c.delete(f"/faces/{new_face['id']}")
check("DELETE /faces/{id} works", r.json()["ok"] is True)

r = c.get("/faces")
check("deleted face no longer in list", len(r.json()) == initial_count)

# ---- Face ID toggle + camera recognize (no-face image) ----------------------
section("Face ID / camera")
r = c.post("/face-id-enabled", json={"enabled": False})
check("disable face ID", r.json()["ok"] is True)
r = c.post("/face-id-enabled", json={"enabled": True})
check("re-enable face ID", r.json()["ok"] is True)

# 1x1 black pixel PNG — valid image, no face in it
tiny_png = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a4944415478da6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
r = c.post("/camera/recognize", files={"photo": ("blank.png", tiny_png, "image/png")})
check("recognize on faceless image returns matched:false", r.json()["matched"] is False, r.json())

r = c.post("/debug/simulate-face-recognition")
check("debug simulate-face-recognition works", r.json()["ok"] is True, r.json())
state = c.get("/state").json()
check("simulated recognition logged as faceId event", state["events"][0]["category"] == "faceId")

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

# ---- ESP32 endpoints -----------------------------------------------------------
section("ESP32 endpoints")
r = c.post("/esp32/heartbeat")
check("heartbeat accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check("esp32_online becomes true after heartbeat", state["esp32_online"] is True)

c.post("/arm", json={"mode": "away"})
time.sleep(3.5)
r = c.post("/esp32/sensor-event", params={"sensor": "door", "reason": "physical door sensor test"})
check("esp32 sensor-event accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check("esp32 sensor-event triggers alarm", state["alarm_state"] == "triggered")

r = c.post("/esp32/state-change", json={"armed": False})
check("esp32 state-change accepted", r.json()["ok"] is True)
state = c.get("/state").json()
check("esp32-reported disarm reflected in state", state["alarm_state"] == "disarmed")

r = c.post("/esp32/state-change", json={"armed": True})
state = c.get("/state").json()
check("esp32-reported arm reflected in state (no exit delay — controller already did its own)",
      state["alarm_state"] == "armed")
c.post("/disarm")

print(f"\n{'='*50}\n{passed} passed, {failed} failed\n{'='*50}")

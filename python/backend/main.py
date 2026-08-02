"""HomeGuard backend — unifies communication with the three ESP32 boards
(ultrasonic sensor, vibration sensor, door/keypad controller) and face
recognition, and serves the Flutter app. See README.md in this directory
for how to run it, and esp32/ for the three board sketches.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import esp32_client
from .face_recognition_service import FaceRecognitionService
from .models import (
    AddFaceRequest,
    ApprovedFace,
    ArmRequest,
    ChangePasswordRequest,
    Esp32StateChangeRequest,
    FullState,
    LoginRequest,
    LoginResponse,
    SensorType,
    SetEmergencyNumberRequest,
    SetFaceIdEnabledRequest,
    SetNightModeRequest,
    SetPoliceCallDelayRequest,
    TriggerAlertRequest,
)
from .state import state

logging.basicConfig(level=logging.INFO)

# This file lives at python/backend/main.py, so two levels up is python/ —
# where faces/ (training photos) and models/ (the Haar cascade) live,
# siblings of backend/ rather than nested inside it.
BASE_DIR = Path(__file__).resolve().parent.parent
# Created once at import time (not per-request) since loading the cascade
# and training the recognizer are relatively expensive — and so a missing
# cascade file fails the app at startup instead of on the first camera
# request.
faces = FaceRecognitionService(
    faces_dir=BASE_DIR / "faces",
    cascade_path=BASE_DIR / "models" / "haarcascade_frontalface_default.xml",
)

app = FastAPI(title="HomeGuard Backend")

# Local dev / same-network app access only — this isn't exposed to the
# internet, so a permissive CORS policy is fine here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Bare liveness check — used to confirm the backend process is up
    and reachable, independent of any app/ESP32/camera state."""
    return {"ok": True}


@app.get("/state", response_model=FullState)
async def get_state():
    """Full state snapshot over plain HTTP. The app calls this once on
    startup (before/instead of the WebSocket being connected); after
    that it relies on /ws pushes rather than polling this.
    """
    return state.snapshot()


# ---- Auth -------------------------------------------------------------------


@app.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """Attempts login with the given password. Always returns the
    current lockout/attempts-remaining fields alongside `ok` — even on
    success — so the app has fresh values without a second round trip
    (e.g. right after a lockout expires, or right before it triggers).
    """
    ok = await state.login(body.password)
    return LoginResponse(
        ok=ok,
        locked_out=state.is_locked_out,
        lockout_remaining_seconds=state.lockout_remaining_seconds,
        attempts_remaining=state.attempts_remaining,
    )


@app.post("/change-password")
async def change_password(body: ChangePasswordRequest):
    """Changes the login password; see AppState.change_password for the
    validation rules (old password must match, new one can't be blank)."""
    ok = await state.change_password(body.old_password, body.new_password)
    return {"ok": ok}


# ---- Arm / disarm -------------------------------------------------------------


@app.post("/arm")
async def arm(body: ArmRequest):
    """Starts arming: updates our own local state first (so the app gets
    an immediate `transitional` response/broadcast), then separately
    tells the door controller board to arm. NOTE: these two steps aren't
    atomic — if the request to the board fails (offline, unreachable),
    local state still proceeds through its own exit-delay timer to
    `armed` regardless, so the app's view can end up ahead of what the
    real hardware is doing. The controller's own state reports (via
    /esp32/state-change) are what ultimately reconcile this.
    """
    await state.arm(body.mode)
    await esp32_client.send_arm(body.mode)
    return {"ok": True}


@app.post("/disarm")
async def disarm():
    """Disarms: same local-state-then-hardware-command pattern (and same
    caveat about the two not being atomic) as `arm` above."""
    await state.disarm()
    await esp32_client.send_disarm()
    return {"ok": True}


@app.post("/trigger-alert")
async def trigger_alert(body: TriggerAlertRequest):
    """Demo/testing entry point — mirrors the app's "simulate intrusion"
    button. Real sensor triggers come through /esp32/sensor-event.
    """
    await state.trigger_alert(body.source, body.reason)
    return {"ok": True}


# ---- Faces --------------------------------------------------------------------


@app.get("/faces", response_model=list[ApprovedFace])
async def list_faces():
    """Lists approved faces (id + name only, no photos)."""
    return state.faces


@app.post("/faces", response_model=ApprovedFace)
async def add_face(body: AddFaceRequest):
    """Creates a new approved-face record by name. This only creates the
    record — it has no training photos yet, so recognition won't match it
    until at least one photo is uploaded via /faces/{face_id}/photo.
    """
    return await state.add_face(body.name)


@app.delete("/faces/{face_id}")
async def remove_face(face_id: str):
    """Removes an approved face entirely: deletes its trained photos from
    disk and retrains the recognizer (`faces.remove_face`), and drops its
    record from app state (`state.remove_face`). Two separate subsystems
    (recognizer storage vs. app-facing state) with no shared transaction,
    but both are called unconditionally, so this face is fully forgotten
    either way.
    """
    faces.remove_face(face_id)
    await state.remove_face(face_id)
    return {"ok": True}


@app.post("/faces/{face_id}/photo")
async def upload_face_photo(face_id: str, photo: UploadFile):
    """Adds one training photo for an existing approved face. 404s if
    `face_id` isn't a known approved face (guards against uploading
    training data for a face that was never created, or was already
    deleted). 422s if no face could be detected in the uploaded image —
    a common user-facing case (bad angle/lighting/no face at all), hence
    the more specific "unprocessable entity" status rather than a generic
    error. Retrains the recognizer synchronously right after (a small,
    CPU-bound `cv2` call) so the new photo is usable immediately; fine at
    this dataset size, but would become a blocking cost worth moving off
    the event loop if the face set ever grew large.
    """
    if not any(f.id == face_id for f in state.faces):
        raise HTTPException(status_code=404, detail="No approved face with that id")
    image_bytes = await photo.read()
    detected = faces.register_face(face_id, image_bytes)
    if not detected:
        raise HTTPException(status_code=422, detail="No face detected in that photo")
    faces.retrain()
    return {"ok": True}


@app.post("/face-id-enabled")
async def set_face_id_enabled(body: SetFaceIdEnabledRequest):
    """Toggles whether /camera/recognize performs recognition at all."""
    await state.set_face_id_enabled(body.enabled)
    return {"ok": True}


# ---- Settings -----------------------------------------------------------------


@app.post("/settings/night-mode")
async def set_night_mode(body: SetNightModeRequest):
    """Toggles night mode (see AppState.set_night_mode for a note on the
    auto-arm behavior this implies but doesn't yet implement)."""
    await state.set_night_mode(body.enabled)
    return {"ok": True}


@app.post("/settings/emergency-number")
async def set_emergency_number(body: SetEmergencyNumberRequest):
    """Sets the number that would be auto-dialed on a triggered alarm
    (see AppState.set_emergency_number — that auto-dial itself isn't
    implemented yet, this just stores the setting)."""
    await state.set_emergency_number(body.number)
    return {"ok": True}


@app.post("/settings/police-call-delay")
async def set_police_call_delay(body: SetPoliceCallDelayRequest):
    """Sets the delay (seconds) before the not-yet-implemented
    auto-dial-emergency-number behavior would fire."""
    await state.set_police_call_delay(body.seconds)
    return {"ok": True}


# ---- ESP32 -------------------------------------------------------------------
# Three separate boards call into these (see esp32/*/README.md for which):
# ultrasonic + vibration sensor boards call heartbeat + sensor-event only;
# the door/keypad controller (the arm/disarm authority) calls all three.


@app.post("/esp32/heartbeat")
async def esp32_heartbeat():
    """Called periodically by the boards just to mark connectivity as
    alive; carries no other data. See AppState.set_esp32_online for the
    caveat that this is one shared flag for all three boards.
    """
    await state.set_esp32_online(True)
    return {"ok": True}


# `sensor` and `reason` are plain scalar-typed parameters (not a Pydantic
# model, unlike most other POST routes here). FastAPI only reads a
# parameter from the request body when its type is a Pydantic model (or
# it's explicitly wrapped in Body(...)); plain str/enum/int parameters
# default to being read from the query string instead. So this endpoint
# expects `POST /esp32/sensor-event?sensor=door&reason=...`, not a JSON
# body — simpler for the ESP32 firmware, which can just append query
# params to a fixed URL instead of building/serializing a JSON payload.
@app.post("/esp32/sensor-event")
async def esp32_sensor_event(sensor: SensorType, reason: str):
    """Reports a sensor firing, from the ultrasonic/vibration/door
    boards. Marks the ESP32 flag online (a successful call proves at
    least one board is reachable) and forwards to
    AppState.trigger_alert, which itself no-ops unless the system is
    currently armed and the reporting sensor isn't bypassed.
    """
    await state.set_esp32_online(True)
    await state.trigger_alert(sensor, reason)
    return {"ok": True}


@app.post("/esp32/state-change")
async def esp32_state_change(body: Esp32StateChangeRequest):
    """Called only by the door/keypad controller (the arm/disarm
    authority) whenever its own armed state changes, whether from the
    physical keypad or from it acting on our /arm /disarm relay. See
    AppState.report_state_from_esp32 for why this cancels any in-progress
    local arming countdown.
    """
    await state.set_esp32_online(True)
    await state.report_state_from_esp32(body.armed)
    return {"ok": True}


# ---- Camera / face recognition -------------------------------------------------


@app.post("/camera/recognize")
async def recognize_frame(photo: UploadFile):
    """Runs face recognition on a single camera frame. Marks the camera
    online unconditionally (a frame arrived, whether or not it matched),
    then short-circuits to "not matched" if face ID is toggled off —
    lets a camera worker keep streaming frames without paying the
    recognition cost while the feature is disabled. Looks the recognized
    face_id up against `state.faces` (not just the recognizer's own
    label map) so a face that was deleted from state, but whose trained
    photos linger for any reason, doesn't get reported as a match.
    """
    await state.set_camera_online(True)
    if not state.face_id_enabled:
        return {"matched": False}

    image_bytes = await photo.read()
    face_id = faces.recognize(image_bytes)
    if face_id is None:
        return {"matched": False}

    match = next((f for f in state.faces if f.id == face_id), None)
    if match is None:
        return {"matched": False}

    await state.log_face_recognized(match.name)
    return {"matched": True, "face_id": match.id, "name": match.name}


@app.post("/debug/simulate-face-recognition")
async def simulate_face_recognition():
    """Demo/testing entry point — mirrors the app's "simulate recognition"
    button. Picks a random approved face, same idea as /trigger-alert for
    sensors.
    """
    if not state.faces:
        return {"ok": False}
    face = random.choice(state.faces)
    await state.log_face_recognized(face.name)
    return {"ok": True, "name": face.name}


# ---- WebSocket: real-time state push to the app --------------------------------


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time push channel: accepts the connection, registers it for
    AppState._broadcast, sends one initial snapshot immediately (so the
    app doesn't have to wait for the next state change to get data), then
    just blocks reading incoming text until the client disconnects. The
    `finally` ensures `unregister` always runs — even if `receive_text`
    raises something other than WebSocketDisconnect — so a dropped
    connection can never linger in the broadcast set.
    """
    await websocket.accept()
    await state.register(websocket)
    try:
        await websocket.send_text(state.snapshot().model_dump_json())
        while True:
            # The app doesn't need to send anything — this just keeps the
            # connection open and detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.unregister(websocket)

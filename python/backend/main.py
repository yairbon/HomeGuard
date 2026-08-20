"""HomeGuard backend — unifies communication with the three ESP32 boards
(ultrasonic sensor, vibration sensor, door/keypad controller) and face
recognition, and serves the Flutter app. See README.md in this directory
for how to run it, and esp32/ for the three board sketches.

Every route here is behind one of two credentials — see `require_app` and
`require_device` below, and `security.py` for what those credentials are and
why there are two of them. The only unauthenticated routes are `/health`
(liveness, returns no state) and `/login` (which is how you get a credential
in the first place).
"""

from __future__ import annotations

import hmac
import logging
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from . import esp32_client, notifier
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
    SetEntryDelayRequest,
    SetFaceDisarmEnabledRequest,
    SetFaceIdEnabledRequest,
    SetNightModeHoursRequest,
    SetNightModeRequest,
    SetNtfyRequest,
    SetPoliceCallDelayRequest,
    TriggerAlertRequest,
)
from .state import state

logging.basicConfig(level=logging.INFO)

# This file lives at python/backend/main.py, so two levels up is python/ —
# where faces/ (training photos) and models/ (the detector/recognizer model
# files) live, siblings of backend/ rather than nested inside it.
BASE_DIR = Path(__file__).resolve().parent.parent
# Created once at import time (not per-request) since loading the models and
# training the recognizer are relatively expensive — and so a missing model
# file fails the app at startup instead of on the first camera request.
faces = FaceRecognitionService(
    faces_dir=BASE_DIR / "faces",
    models_dir=BASE_DIR / "models",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the board-liveness monitor once the event loop is running, and
    stops it cleanly on shutdown.

    The monitor can't be started from `AppState.__init__` because that runs
    at import time, before there's a loop to schedule a task on.
    """
    state.start_monitor()
    yield
    await state.stop_monitor()
    await notifier.aclose()


app = FastAPI(title="HomeGuard Backend", lifespan=lifespan)

# The app is a native Android client, which is not a browser and so is not
# subject to CORS at all — these origins exist only for the FastAPI `/docs`
# page and any local browser-based debugging. Restricted to localhost rather
# than `*` because a wildcard here, combined with credentialed requests from
# a browser, would let any web page the user visits drive this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Authentication dependencies -----------------------------------------------


def _bearer_token(authorization: str | None) -> str | None:
    """Pulls the token out of an `Authorization: Bearer <token>` header,
    returning None for a missing or non-Bearer header rather than raising —
    callers decide what a missing credential means."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def require_app(authorization: str | None = Header(default=None)) -> str:
    """Dependency for routes the *app* calls. Rejects anything without a
    live session token from `/login`, and returns the token so handlers that
    need to know who's calling (currently `/change-password`, which spares
    the caller's own session when revoking the others) can use it.

    401 with a `WWW-Authenticate` header, per RFC 9110 — the status that
    means "you didn't authenticate", as opposed to 403's "you did, but you
    still can't".
    """
    token = _bearer_token(authorization)
    if not state.tokens.is_valid(token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def require_device(
    x_device_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
) -> None:
    """Dependency for routes the *ESP32 boards* call. Accepts the shared
    device key either as an `X-Device-Key` header or as a `?key=` query
    parameter.

    Both forms are supported because the sketches already build their sensor
    reports as fixed query-string URLs (see `/esp32/sensor-event`), so a query
    parameter costs them nothing, while the header keeps the key out of access
    logs for the boards that POST a JSON body anyway. Compared with
    `hmac.compare_digest` to avoid leaking the key through response timing.
    """
    presented = x_device_key or key or ""
    if not hmac.compare_digest(presented, state.device_key):
        raise HTTPException(status_code=401, detail="Missing or invalid device key")


async def require_app_or_device(
    authorization: str | None = Header(default=None),
    x_device_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
) -> None:
    """Dependency for routes either kind of caller legitimately uses.

    Only `/camera/recognize` needs this: frames come from a camera worker
    (device key) during normal operation, but the app posts frames too when
    testing face enrollment from the phone.
    """
    if state.tokens.is_valid(_bearer_token(authorization)):
        return
    presented = x_device_key or key or ""
    if hmac.compare_digest(presented, state.device_key):
        return
    raise HTTPException(status_code=401, detail="Missing or invalid credentials")


@app.get("/health")
async def health():
    """Bare liveness check — used to confirm the backend process is up
    and reachable, independent of any app/ESP32/camera state.

    Deliberately unauthenticated and deliberately empty: it reveals only that
    something is listening, which a port scan would establish anyway.
    """
    return {"ok": True}


@app.get("/state", response_model=FullState, dependencies=[Depends(require_app)])
async def get_state():
    """Full state snapshot over plain HTTP. The app calls this once on
    startup (before/instead of the WebSocket being connected); after
    that it relies on /ws pushes rather than polling this.
    """
    return state.snapshot()


# ---- Auth -------------------------------------------------------------------


@app.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """Attempts login with the given password and, on success, mints the
    session token every other route requires.

    Always returns the current lockout/attempts-remaining fields alongside
    `ok` — even on success — so the app has fresh values without a second
    round trip (e.g. right after a lockout expires, or right before it
    triggers).
    """
    token = await state.login(body.password)
    return LoginResponse(
        ok=token is not None,
        token=token,
        locked_out=state.is_locked_out,
        lockout_remaining_seconds=state.lockout_remaining_seconds,
        attempts_remaining=state.attempts_remaining,
    )


@app.post("/logout")
async def logout(token: str = Depends(require_app)):
    """Revokes the caller's own session token. Without this, signing out in
    the app only cleared a local flag while the token stayed valid on the
    server until it expired hours later."""
    state.tokens.revoke(token)
    return {"ok": True}


@app.post("/change-password")
async def change_password(body: ChangePasswordRequest, token: str = Depends(require_app)):
    """Changes the login password; see AppState.change_password for the
    validation rules and for why every *other* session is revoked.

    Requires a valid session as well as the old password: this route used to
    be reachable by anyone and enforced no attempt limit, which made it an
    unthrottled oracle for guessing the password that `/login`'s 3-attempt
    lockout was supposed to protect.
    """
    ok = await state.change_password(body.old_password, body.new_password, current_token=token)
    return {"ok": ok}


# ---- Arm / disarm -------------------------------------------------------------


@app.post("/arm", dependencies=[Depends(require_app)])
async def arm(body: ArmRequest):
    """Starts arming: updates our own local state first (so the app gets
    an immediate `transitional` response/broadcast), then separately
    tells the door controller board to arm. NOTE: these two steps aren't
    atomic — if the request to the board fails (offline, unreachable),
    local state still proceeds through its own exit-delay timer to
    `armed` regardless, so the app's view can end up ahead of what the
    real hardware is doing. The controller's own state reports (via
    /esp32/state-change) are what ultimately reconcile this. `hardware`
    in the response says whether the board actually acknowledged, so the
    app can surface the difference instead of assuming success.
    """
    await state.arm(body.mode)
    reached = await esp32_client.send_arm(body.mode)
    return {"ok": True, "hardware": reached}


@app.post("/disarm", dependencies=[Depends(require_app)])
async def disarm():
    """Disarms: same local-state-then-hardware-command pattern (and same
    caveat about the two not being atomic) as `arm` above."""
    await state.disarm()
    reached = await esp32_client.send_disarm()
    return {"ok": True, "hardware": reached}


@app.post("/trigger-alert", dependencies=[Depends(require_app)])
async def trigger_alert(body: TriggerAlertRequest):
    """Demo/testing entry point — mirrors the app's "simulate intrusion"
    button. Real sensor triggers come through /esp32/sensor-event.
    """
    await state.trigger_alert(body.source, body.reason)
    return {"ok": True}


# ---- Faces --------------------------------------------------------------------


@app.get("/faces", response_model=list[ApprovedFace], dependencies=[Depends(require_app)])
async def list_faces():
    """Lists approved faces (id + name only, no photos)."""
    return state.faces


@app.post("/faces", response_model=ApprovedFace, dependencies=[Depends(require_app)])
async def add_face(body: AddFaceRequest):
    """Creates a new approved-face record by name. This only creates the
    record — it has no training photos yet, so recognition won't match it
    until at least one photo is uploaded via /faces/{face_id}/photo.
    """
    return await state.add_face(body.name)


@app.delete("/faces/{face_id}", dependencies=[Depends(require_app)])
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


@app.post("/faces/{face_id}/photo", dependencies=[Depends(require_app)])
async def upload_face_photo(face_id: str, photo: UploadFile):
    """Adds one training photo for an existing approved face. 404s if
    `face_id` isn't a known approved face (guards against uploading
    training data for a face that was never created, or was already
    deleted). 422s if no face could be detected in the uploaded image —
    a common user-facing case (bad angle/lighting/no face at all), hence
    the more specific "unprocessable entity" status rather than a generic
    error. Retrains the recognizer synchronously right after so the new
    photo is usable immediately; fine at this dataset size, but would
    become a blocking cost worth moving off the event loop if the face set
    ever grew large.
    """
    if not any(f.id == face_id for f in state.faces):
        raise HTTPException(status_code=404, detail="No approved face with that id")
    image_bytes = await photo.read()
    try:
        detected = faces.register_face(face_id, image_bytes)
    except ValueError as exc:
        # The upload wasn't a decodable image at all (truncated, corrupt, or
        # not an image). Same 422 family as "no face found" — the request was
        # well-formed, its payload wasn't usable — rather than letting the
        # ValueError escape as an opaque 500.
        raise HTTPException(status_code=422, detail=f"Unreadable image: {exc}") from exc
    if not detected:
        raise HTTPException(status_code=422, detail="No face detected in that photo")
    faces.retrain()
    return {"ok": True}


@app.post("/face-id-enabled", dependencies=[Depends(require_app)])
async def set_face_id_enabled(body: SetFaceIdEnabledRequest):
    """Toggles whether /camera/recognize performs recognition at all."""
    await state.set_face_id_enabled(body.enabled)
    return {"ok": True}


@app.post("/face-disarm-enabled", dependencies=[Depends(require_app)])
async def set_face_disarm_enabled(body: SetFaceDisarmEnabledRequest):
    """Toggles whether a recognized face is allowed to disarm the system.

    Distinct from /face-id-enabled: that one controls whether recognition
    runs, this controls whether a match may turn the alarm off. Off by
    default — see AppState.log_face_recognized for the liveness caveat that
    makes this a convenience feature rather than a security one.
    """
    await state.set_face_disarm_enabled(body.enabled)
    return {"ok": True}


# ---- Settings -----------------------------------------------------------------


@app.post("/settings/night-mode", dependencies=[Depends(require_app)])
async def set_night_mode(body: SetNightModeRequest):
    """Toggles night mode."""
    await state.set_night_mode(body.enabled)
    return {"ok": True}


@app.post("/settings/emergency-number", dependencies=[Depends(require_app)])
async def set_emergency_number(body: SetEmergencyNumberRequest):
    """Sets the number that would be auto-dialed on a triggered alarm
    (see AppState.set_emergency_number — that auto-dial itself isn't
    implemented yet, this just stores the setting)."""
    await state.set_emergency_number(body.number)
    return {"ok": True}


@app.post("/settings/night-mode-hours", dependencies=[Depends(require_app)])
async def set_night_mode_hours(body: SetNightModeHoursRequest):
    """Sets when night mode arms and disarms (whole hours, 0-23)."""
    await state.set_night_mode_hours(body.arm_hour, body.disarm_hour)
    return {"ok": True}


@app.post("/settings/ntfy", dependencies=[Depends(require_app)])
async def set_ntfy(body: SetNtfyRequest):
    """Configures push notifications (see backend/notifier.py) — the channel
    that makes an alert reach the phone with the app closed."""
    await state.set_ntfy(body.server, body.topic)
    return {"ok": True}


@app.post("/settings/ntfy/test", dependencies=[Depends(require_app)])
async def test_ntfy():
    """Sends a test push, so the topic can be verified before a real
    break-in depends on it. `ok: false` means no topic is configured;
    delivery itself isn't waited on."""
    sent = await state.send_test_notification()
    return {"ok": sent}


@app.post("/settings/police-call-delay", dependencies=[Depends(require_app)])
async def set_police_call_delay(body: SetPoliceCallDelayRequest):
    """Sets the delay (seconds) before the not-yet-implemented
    auto-dial-emergency-number behavior would fire."""
    await state.set_police_call_delay(body.seconds)
    return {"ok": True}


# ---- ESP32 -------------------------------------------------------------------
# Three separate boards call into these (see esp32/README.md for which):
# ultrasonic + vibration sensor boards call heartbeat + sensor-event only;
# the door/keypad controller (the arm/disarm authority) calls all three.
#
# All of them require the shared device key (see `require_device`). Without
# it, anything that could reach the network could forge a sensor breach — or,
# worse, forge a `/esp32/state-change` claiming the system is disarmed.


@app.post("/esp32/heartbeat", dependencies=[Depends(require_device)])
async def esp32_heartbeat(board: str = Query(...)):
    """Called every 10 seconds by each board to report that it's alive.

    `board` identifies which one, so the app can show all three separately.
    It's required rather than optional: an unnamed heartbeat would have to be
    attributed to some default board, which would show a board as online that
    might in fact be the dead one.
    """
    await state.note_board_seen(board)
    return {"ok": True}


# `sensor` and `reason` are plain scalar-typed parameters (not a Pydantic
# model, unlike most other POST routes here). FastAPI only reads a
# parameter from the request body when its type is a Pydantic model (or
# it's explicitly wrapped in Body(...)); plain str/enum/int parameters
# default to being read from the query string instead. So this endpoint
# expects `POST /esp32/sensor-event?sensor=door&reason=...`, not a JSON
# body — simpler for the ESP32 firmware, which can just append query
# params to a fixed URL instead of building/serializing a JSON payload.
@app.post("/esp32/sensor-event", dependencies=[Depends(require_device)])
async def esp32_sensor_event(
    sensor: SensorType, reason: str, board: str | None = Query(default=None)
):
    """Reports a sensor firing, from the ultrasonic/vibration/door
    boards. Marks the reporting board alive (a successful call proves it's
    reachable) and forwards to AppState.trigger_alert, which itself no-ops
    unless the system is currently armed and the reporting sensor isn't
    bypassed.
    """
    await state.note_board_seen(board or sensor.value)
    await state.trigger_alert(sensor, reason)
    return {"ok": True}


@app.post("/esp32/entry-delay", dependencies=[Depends(require_device)])
async def esp32_entry_delay(body: SetEntryDelayRequest):
    """Called by the door controller when it starts its disarm countdown.

    Without this the board ran that window silently and reported only the
    outcome, so the app jumped from ARMED straight to TRIGGERED — no
    countdown, no warning, no chance for someone watching remotely to see
    that a person had just walked in.
    """
    await state.note_board_seen("door_controller")
    await state.begin_entry_delay(body.seconds)
    return {"ok": True}


@app.post("/esp32/state-change", dependencies=[Depends(require_device)])
async def esp32_state_change(body: Esp32StateChangeRequest):
    """Called only by the door/keypad controller (the arm/disarm
    authority) whenever its own armed state changes, whether from the
    physical keypad or from it acting on our /arm /disarm relay. See
    AppState.report_state_from_esp32 for why this cancels any in-progress
    local arming countdown.
    """
    await state.note_board_seen("door_controller")
    await state.report_state_from_esp32(body.armed)
    return {"ok": True}


# ---- Camera / face recognition -------------------------------------------------

# The most recent frame any camera worker uploaded, kept only in memory (not
# persisted — a restarted backend has no last frame, same as a restarted
# camera would). Lets /camera/latest serve something closer to a live view
# than the app's old static placeholder, without a real video stream.
_latest_camera_frame: bytes | None = None


@app.post("/camera/recognize", dependencies=[Depends(require_app_or_device)])
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

    # Read (and cache for /camera/latest) before the face-ID toggle check
    # below — the live view should keep working even with recognition
    # itself switched off, since those are two independent things a user
    # might want.
    image_bytes = await photo.read()
    global _latest_camera_frame
    _latest_camera_frame = image_bytes

    if not state.face_id_enabled:
        return {"matched": False}

    try:
        face_id = faces.recognize(image_bytes)
    except ValueError as exc:
        # A corrupt or non-image frame is a bad request, not a server fault.
        # This used to escape as a 500 with a traceback — and it was masked
        # for a long time because the old recognizer checked "am I trained?"
        # before it ever tried to decode, so an undecodable frame only blew
        # up once at least one face had been enrolled.
        raise HTTPException(status_code=422, detail=f"Unreadable image: {exc}") from exc
    if face_id is None:
        return {"matched": False}

    match = next((f for f in state.faces if f.id == face_id), None)
    if match is None:
        return {"matched": False}

    await state.log_face_recognized(match.name)
    return {"matched": True, "face_id": match.id, "name": match.name}


@app.get("/camera/latest", dependencies=[Depends(require_app_or_device)])
async def latest_camera_frame():
    """Returns the most recent frame a camera worker uploaded, as a raw
    JPEG — what the app's camera screen polls for something closer to a
    live view than a static placeholder. 404 before any frame has ever
    arrived; there's nothing to serve yet.
    """
    if _latest_camera_frame is None:
        raise HTTPException(status_code=404, detail="No camera frame received yet")
    return Response(content=_latest_camera_frame, media_type="image/jpeg")


# The ESP32-CAM's own /stream endpoint (esp32/esp32_cam/esp32_cam.ino,
# startCameraServer()), e.g. "http://192.168.1.61/stream". Find the board's
# IP the same way as the door controller: check its Serial Monitor output
# after boot.
CAMERA_STREAM_URL = os.environ.get("HOMEGUARD_CAMERA_STREAM_URL", "")

# A dedicated client, deliberately separate from esp32_client.py's `_client`
# (tuned for short 3s command POSTs to the door controller) and from any
# client used for /camera/recognize. A stream connection sits open for
# minutes, not seconds, so it needs its own timeout/pool profile: no read
# timeout (MJPEG frame gaps are irregular — the default 5s read timeout is
# the single most common way a naive port of this pattern breaks), and a
# small connection cap (the ESP32-CAM's own tiny web server can only serve
# one or two concurrent stream clients anyway).
_cam_stream_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
    limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
)


@app.get("/camera/stream", dependencies=[Depends(require_app)])
async def proxy_camera_stream():
    """Proxies the ESP32-CAM's own MJPEG multipart stream through to the
    app, chunk by chunk, without buffering a frame (let alone the whole
    stream) in memory and without blocking this process's single event
    loop for anyone else on it (the WebSocket broadcast included) — every
    call in this chain is async, so unlike camera_worker.py (a separate
    *process*, specifically because cv2.VideoCapture.read() blocks) this
    genuinely belongs in the main backend process.

    Uses httpx's manual streaming mode (send(..., stream=True)) rather than
    the `async with client.stream(...)` context manager, on purpose: it
    awaits the upstream status line/headers but defers the body read, so a
    bad status can still become a clean HTTPException here. Once
    StreamingResponse exists, Starlette sends the response status
    unconditionally before ever asking the body generator for its first
    chunk — a bad-status check done *inside* the generator would be too
    late, the 200 is already committed to the app.
    """
    if not CAMERA_STREAM_URL:
        raise HTTPException(status_code=503, detail="Camera stream not configured")

    headers = {"X-Device-Key": state.device_key}
    try:
        request = _cam_stream_client.build_request("GET", CAMERA_STREAM_URL, headers=headers)
        upstream = await _cam_stream_client.send(request, stream=True)
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=502, detail=f"Camera unreachable: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"Camera timed out: {exc}") from exc

    if upstream.status_code != 200:
        await upstream.aclose()
        raise HTTPException(status_code=502, detail=f"Camera returned HTTP {upstream.status_code}")

    # Forwarded verbatim: the ESP32-CAM's own multipart boundary token is
    # baked into every "--<boundary>" delimiter already present in the byte
    # stream, so substituting a different Content-Type here would desync
    # from the bytes and break the app's parser.
    content_type = upstream.headers.get(
        "content-type", "multipart/x-mixed-replace; boundary=frame"
    )

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.StreamError:
            # Upstream dropped mid-frame. Headers are already committed (the
            # 200 was sent), so there's nothing left to report — just stop;
            # `background` below still closes the upstream connection.
            return

    return StreamingResponse(
        relay(), media_type=content_type, background=BackgroundTask(upstream.aclose)
    )


@app.post("/debug/simulate-face-recognition", dependencies=[Depends(require_app)])
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
async def websocket_endpoint(websocket: WebSocket, token: str | None = Query(default=None)):
    """Real-time push channel: authenticates, accepts the connection,
    registers it for AppState._broadcast, sends one initial snapshot
    immediately (so the app doesn't have to wait for the next state change to
    get data), then just blocks reading incoming text until the client
    disconnects.

    The token arrives as a query parameter rather than an `Authorization`
    header because the browser WebSocket API — and therefore
    `web_socket_channel` on the app side — provides no portable way to set
    headers on the handshake. The connection is refused with code 1008
    ("policy violation") *before* `accept()`, so an unauthenticated client is
    never registered for broadcasts and never receives a single state frame.
    """
    if not state.tokens.is_valid(token):
        await websocket.close(code=1008)
        return

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

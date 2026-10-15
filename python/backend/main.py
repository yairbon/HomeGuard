"""HomeGuard backend — unifies communication with the four ESP32 boards
(ultrasonic sensor, vibration sensor, door/keypad controller, ESP32-CAM) and
face recognition, and serves the Flutter app. See README.md in this directory
for how to run it, and esp32/ for the four board sketches.

Every route here is behind one of two credentials — see `require_app` and
`require_device` below, and `security.py` for what those credentials are and
why there are two of them. The only unauthenticated routes are `/health`
(liveness, returns no state) and `/login` (which is how you get a credential
in the first place).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
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
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from . import alerts, esp32_client, notifier
from .camera_hub import CameraHub
from .face_recognition_service import FaceRecognitionService
from .http_clients import build_client
from .ip_check import log_stale_board_ips
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
    SetAlertEmailsRequest,
    SetAlertsEnabledRequest,
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
from .state import BOARD_FOR_SENSOR, STATE_FILE, state

logging.basicConfig(level=logging.INFO)


class _QuietRoutineTraffic(logging.Filter):
    """Keeps the console readable: the boards' heartbeats and the camera's frame
    pushes arrive many times a minute and would bury everything that matters.
    Set HOMEGUARD_LOG_ALL_REQUESTS=1 to see every request again (for diagnosing)."""

    _ROUTINE = ("/esp32/heartbeat", "/camera/recognize", "/camera/latest")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        path = args[2] if isinstance(args, tuple) and len(args) >= 3 else ""
        return not (isinstance(path, str) and path.startswith(self._ROUTINE))


class _RedactSessionToken(logging.Filter):
    """The WebSocket takes its session token in the URL (browsers cannot set
    headers on it), and uvicorn logs that URL. A token on a console that gets
    filmed or shared is a login, so it never reaches a log line."""

    _token = re.compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._token.sub(r"\1***", str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._token.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


for _name in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_name).addFilter(_RedactSessionToken())
if os.environ.get("HOMEGUARD_LOG_ALL_REQUESTS", "").strip().lower() not in ("1", "true", "yes"):
    logging.getLogger("uvicorn.access").addFilter(_QuietRoutineTraffic())

# This file lives at python/backend/main.py, so two levels up is python/ —
# where faces/ (training photos) and models/ (the detector/recognizer model
# files) live, siblings of backend/ rather than nested inside it.
BASE_DIR = Path(__file__).resolve().parent.parent
# Created once at import time (not per-request) since loading the models and
# training the recognizer are relatively expensive — and so a missing model
# file fails the app at startup instead of on the first camera request.

def default_faces_dir(state_file: Path, base_dir: Path) -> Path:
    """Where the enrolled face photos live.

    Next to the state file: the photos and the list of approved faces are one
    dataset (ids in the list name folders on disk), so they must always be
    chosen together. This used to be fixed at python/faces, which let a second
    backend started with its own temporary state (a test or simulation) share
    the real photo folder, and its test faces, which reuse small ids, deleted a
    real person's enrolment. `HOMEGUARD_FACES_DIR` overrides it explicitly.
    """
    explicit = os.environ.get("HOMEGUARD_FACES_DIR")
    if explicit:
        return Path(explicit)
    return state_file.parent / "faces" if state_file.parent != base_dir else base_dir / "faces"


faces = FaceRecognitionService(
    faces_dir=default_faces_dir(STATE_FILE, BASE_DIR),
    models_dir=BASE_DIR / "models",
)

# Serializes every call into `faces`.
#
# Needed because those calls now run in worker threads (`asyncio.to_thread`)
# instead of on the event loop, and the event loop was the thing that used to
# guarantee they never overlapped. They cannot safely overlap: `retrain`
# rebuilds the embedding map in place, so a recognition running concurrently
# can iterate a dict that is being repopulated ("dictionary changed size
# during iteration"), and the underlying cv2 detector/recognizer objects are
# shared, stateful and not thread-safe — `_detect` calls `setInputSize` on the
# shared detector immediately before using it, so two differently-sized frames
# in flight at once is a genuine race, not a theoretical one.
#
# Recognition is ~21ms and the camera sends 2 frames a second, so serializing
# costs nothing that matters. The point of the threads is to keep the *event
# loop* free (it is also pushing alarm state to every connected app), not to
# run recognitions in parallel.
_recognizer_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warns about any board still configured with a stale IP (see
    `ip_check.py`), then starts the board-liveness monitor once the event
    loop is running, and stops it cleanly on shutdown.

    The monitor can't be started from `AppState.__init__` because that runs
    at import time, before there's a loop to schedule a task on.
    """
    log_stale_board_ips()
    state.start_monitor()
    yield
    await state.stop_monitor()
    # Every outbound client this process owns, closed on the way out. Leaving
    # them to the garbage collector means sockets to the door controller and
    # the camera that nothing ever tore down, plus httpx's "client was never
    # closed" warnings on every shutdown.
    await notifier.aclose()
    await esp32_client.aclose()
    await _camera_hub.aclose()
    await _cam_stream_client.aclose()


# The interactive API docs (/docs, /redoc, /openapi.json) are not behind a
# credential, so they would hand the full API schema to anyone on the network.
# Off unless HOMEGUARD_DOCS=1, for development.
_docs_enabled = os.environ.get("HOMEGUARD_DOCS", "").strip().lower() in ("1", "true", "yes")
app = FastAPI(
    title="HomeGuard Backend",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

_cors_logger = logging.getLogger("cors")


def extra_cors_origins(raw: str) -> list[str]:
    """Parses `HOMEGUARD_CORS_ORIGINS` — a comma-separated list of exact
    origins, e.g. `http://localhost:5000,http://100.64.0.7:5000` — into the
    extra browser origins the API should answer.

    Needed only when the Flutter *web* build is served from its own port
    (`flutter run -d web-server`), because the browser then treats the API as
    cross-origin. A bare `*` is refused, for the reason given at the
    middleware below, rather than silently honored.
    """
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            _cors_logger.warning(
                "HOMEGUARD_CORS_ORIGINS: ignoring '*' — list exact origins instead."
            )
            continue
        origins.append(origin)
    return origins


# The app is a native Android client, which is not a browser and so is not
# subject to CORS at all — these origins exist only for the FastAPI `/docs`
# page and any local browser-based debugging. Restricted to localhost rather
# than `*` because a wildcard here, combined with credentialed requests from
# a browser, would let any web page the user visits drive this API.
# `HOMEGUARD_CORS_ORIGINS` adds exact origins on top, opt-in, for the web build.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        *extra_cors_origins(os.environ.get("HOMEGUARD_CORS_ORIGINS", "")),
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Web app (optional) ----------------------------------------------------------


def app_stream_content_type(upstream_content_type: str) -> str:
    """Relabels the camera stream so a browser hands the raw bytes to the app.

    Safari (and every Apple networking stack) recognises
    `multipart/x-mixed-replace` and takes the stream apart itself, delivering
    the image parts without the "--boundary" lines. The app's own parser needs
    those lines (it is what finds where each frame starts and ends), so on an
    iPhone the live view stayed black. Any other media type is left alone by
    that machinery. The boundary parameter is kept untouched: it is how the
    parser knows what to look for in the bytes. Only the app reads this route."""
    media, separator, parameters = upstream_content_type.partition(";")
    if media.strip().lower() != "multipart/x-mixed-replace":
        return upstream_content_type
    return "application/octet-stream" + separator + parameters


class _AlwaysRevalidateStaticFiles(StaticFiles):
    """Serves the web app with `Cache-Control: no-cache`.

    Without any Cache-Control, a browser may keep using a file for a while on
    its own guess (a fraction of the time since it last changed), so after a
    rebuild a phone could keep showing the previous app for hours. `no-cache`
    means "check with the server every time"; the ETag makes that check a cheap
    304 when nothing changed, so a normal reload always picks up a new build.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def mount_web_app(target: FastAPI, directory: Path) -> bool:
    """Serves the built Flutter web app (`flutter build web`, see
    `scripts/build-web.ps1`) from `/`, so the page and the API share one
    origin and one port: no CORS list to maintain, and nothing to type into
    the app's "Backend connection" box, because the web build defaults to the
    address it was loaded from.

    Must be called after every route is registered: a mount at `/` matches
    anything, but Starlette tries routes in order, so everything declared
    before it still wins. Unknown paths get a plain 404, not the index page.

    The files are the public, compiled app — no state, secrets or data — so
    serving them without a credential is deliberate; every API route is still
    behind `require_app` / `require_device` exactly as before. Returns whether
    anything was mounted; with no build present (CI, API-only use) the backend
    behaves exactly as it did.
    """
    if not (directory / "index.html").is_file():
        return False
    target.mount("/", _AlwaysRevalidateStaticFiles(directory=directory, html=True), name="web-app")
    return True


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
    """Changes the one shared secret that is both this app's login password
    and the physical keypad's disarm PIN — see AppState.change_password for
    the full validation rules, why every *other* session is revoked, and the
    security trade-off accepted by merging what used to be two separate
    credentials (this route, and a since-removed `/settings/keypad-pin`)
    into one.

    Requires a valid session as well as the old value: this route used to be
    reachable by anyone and enforced no attempt limit, which made it an
    unthrottled oracle for guessing the password that `/login`'s 3-attempt
    lockout was supposed to protect. That lockout still applies here, keyed
    off `old_password` — three wrong guesses lock the account out regardless
    of what `new_password` looks like.

    A `new_password` that isn't 4-8 digits — the one shape a physical keypad
    can type, now the binding constraint on both credentials — is rejected
    with 422 rather than folded into `ok: false`, so the app can tell "the
    current value was wrong" from "that new value can never be typed on the
    keypad" apart.

    `hardware` and `secrets_updated` report the same two outcomes
    `/settings/keypad-pin` used to (this route absorbed it): whether the
    door controller acknowledged the new PIN, and whether
    `esp32/.../secrets.h` was rewritten to match. Both are `False` whenever
    `ok` is `False` — a rejected change never reaches the hardware at all.
    """
    try:
        ok, hardware, secrets_updated = await state.change_password(
            body.old_password, body.new_password, current_token=token
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": ok, "hardware": hardware, "secrets_updated": secrets_updated}


# ---- Arm / disarm -------------------------------------------------------------


@app.post("/arm", dependencies=[Depends(require_app)])
async def arm(body: ArmRequest):
    """Starts arming. The door controller board is asked first, because it
    can refuse: with its door open, arming would start the entry countdown the
    moment it finished, so it answers 409 and nothing is armed (409 here too,
    with a sentence the app shows as is).

    Otherwise our own state starts its exit delay. NOTE: arming here and on the
    board aren't atomic — if the board is offline or unreachable, local state
    still proceeds through its own exit-delay timer to `armed` regardless, so
    the app's view can end up ahead of what the real hardware is doing. The
    controller's own state reports (via /esp32/state-change) are what
    ultimately reconcile this. `hardware` in the response says whether the
    board actually acknowledged, so the app can surface the difference instead
    of assuming success.
    """
    outcome = await esp32_client.send_arm(body.mode)
    if outcome == "door_open":
        raise HTTPException(
            status_code=409, detail="Can't arm: the door is open. Close it and try again."
        )
    await state.arm(body.mode)
    return {"ok": True, "hardware": outcome == "ok"}


@app.post("/disarm", dependencies=[Depends(require_app)])
async def disarm():
    """Disarms: same local-state-then-hardware-command pattern (and same
    caveat about the two not being atomic) as `arm` above."""
    await state.disarm()
    reached = await esp32_client.send_disarm()
    return {"ok": True, "hardware": reached}


@app.post("/trigger-alert", dependencies=[Depends(require_app)])
async def trigger_alert(body: TriggerAlertRequest):
    """Manual alarm trigger for an authenticated caller (`simulate.py` uses
    it; the app no longer has a button for it). Real sensor triggers come
    through /esp32/sensor-event. Does nothing unless the system is armed.
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

    `faces.remove_face` deletes files and then retrains, which is measurably
    slow (~10ms per enrolled photo — half a second at 50), so it runs in a
    worker thread under [_recognizer_lock] rather than on the event loop.
    """
    async with _recognizer_lock:
        await asyncio.to_thread(faces.remove_face, face_id)
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
    error. Retrains the recognizer right after so the new photo is usable
    immediately.

    Both steps run in a worker thread, and both under the same hold of
    [_recognizer_lock] so nothing can recognize against a half-rebuilt model
    in between. `retrain` re-embeds every enrolled photo — measured at ~10ms
    each, so 556ms at 50 photos — which is far too long to spend on the event
    loop that is also pushing alarm state to every connected app.
    """
    if not any(f.id == face_id for f in state.faces):
        raise HTTPException(status_code=404, detail="No approved face with that id")
    image_bytes = await photo.read()
    async with _recognizer_lock:
        try:
            detected = await asyncio.to_thread(faces.register_face, face_id, image_bytes)
        except ValueError as exc:
            # The upload wasn't a decodable image at all (truncated, corrupt,
            # or not an image). Same 422 family as "no face found" — the
            # request was well-formed, its payload wasn't usable — rather than
            # letting the ValueError escape as an opaque 500.
            raise HTTPException(status_code=422, detail=f"Unreadable image: {exc}") from exc
        if not detected:
            raise HTTPException(status_code=422, detail="No face detected in that photo")
        await asyncio.to_thread(faces.retrain)
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


@app.post("/settings/alert-emails", dependencies=[Depends(require_app)])
async def set_alert_emails(body: SetAlertEmailsRequest):
    """Sets who is emailed when an alarm triggers (`alert_to`) and who is added
    when it stays uncleared (`escalation_to`). A malformed address is a 422
    naming it, never silently dropped."""
    alert_to, bad_alert = alerts.parse_addresses(body.alert_to)
    escalation_to, bad_escalation = alerts.parse_addresses(body.escalation_to)
    invalid = bad_alert + bad_escalation
    if invalid:
        raise HTTPException(
            status_code=422,
            detail="These do not look like email addresses: " + ", ".join(invalid),
        )
    await state.set_alert_emails(list(alert_to), list(escalation_to))
    return {"ok": True}


@app.post("/settings/alerts-enabled", dependencies=[Depends(require_app)])
async def set_alerts_enabled(body: SetAlertsEnabledRequest):
    """Master switch for messages that leave the house (email, WhatsApp, ntfy).
    The alarm itself is unaffected."""
    await state.set_alerts_enabled(body.enabled)
    return {"ok": True}


@app.post("/settings/ntfy", dependencies=[Depends(require_app)])
async def set_ntfy(body: SetNtfyRequest):
    """Configures push notifications (see backend/notifier.py) — the channel
    that makes an alert reach the phone with the app closed."""
    await state.set_ntfy(body.server, body.topic)
    return {"ok": True}


@app.post("/settings/alerts/test", dependencies=[Depends(require_app)])
async def test_alerts():
    """Sends a test alert over every configured channel (email, WhatsApp,
    ntfy) and reports what happened, so setup can be verified before a real
    break-in depends on it. Waits for the outcome, so it can take a few
    seconds. `ok` is true only if every configured channel delivered;
    `summary` is a sentence the app shows as is, never containing a secret."""
    results = await state.send_test_notification()
    if not results:
        return {
            "ok": False,
            "results": {},
            "summary": "No alert channel is set up yet. Fill in alerts.local.json "
            "on the laptop (see python/README.md), then try again.",
        }
    failures = {name: reason for name, reason in results.items() if reason}
    if failures:
        summary = "; ".join(f"{name}: {reason}" for name, reason in failures.items())
    else:
        summary = "Test sent via " + ", ".join(results) + ". Check your phone."
    if not state.alerts_enabled:
        summary += " (Alerts are switched off, so a real alarm will NOT send anything.)"
    return {"ok": not failures, "results": results, "summary": summary}


@app.post("/settings/police-call-delay", dependencies=[Depends(require_app)])
async def set_police_call_delay(body: SetPoliceCallDelayRequest):
    """Sets how long (seconds) an alarm may stay uncleared before the
    "still not cleared" alert goes out to the emergency contacts (see
    `_escalate_after_delay`). Nothing dials a phone."""
    await state.set_police_call_delay(body.seconds)
    return {"ok": True}


# ---- ESP32 -------------------------------------------------------------------
# Three of the four boards call into these (see esp32/README.md for which):
# ultrasonic + vibration sensor boards call heartbeat + sensor-event only;
# the door/keypad controller (the arm/disarm authority) calls all four routes.
# The fourth board, the ESP32-CAM, talks to /camera/* instead and never
# appears here.
#
# All of them require the shared device key (see `require_device`). Without
# it, anything that could reach the network could forge a sensor breach — or,
# worse, forge a `/esp32/state-change` claiming the system is disarmed.


@app.post("/esp32/heartbeat", dependencies=[Depends(require_device)])
async def esp32_heartbeat(board: str = Query(...)):
    """Called every 10 seconds by each board to report that it's alive.

    `board` identifies which one, so the app can show each of the three
    heartbeating boards separately. It's required rather than optional: an
    unnamed heartbeat would have to be attributed to some default board, which
    would show a board as online that might in fact be the dead one. A name
    that isn't one of the three is accepted and then ignored — see
    AppState.note_board_seen.
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

    The board name is looked up from the sensor when the caller didn't send
    one. It used to fall back to `sensor.value` directly, which is right for
    two of the three and wrong for the important one: the door sensor belongs
    to `door_controller`, so a door breach recorded liveness under the name
    "door" — a key no board is called and `board_statuses` never reads. The
    fallback therefore marked nothing at all online.
    """
    await state.note_board_seen(board or BOARD_FOR_SENSOR[sensor])
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
# Lets an alarm email attach the newest picture (see AppState._alert_photo).
state.camera_frame_source = lambda: _latest_camera_frame


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

    A match is also checked for liveness (`faces.is_live`) before being
    handed to `state.log_face_recognized` — see that method's docstring —
    so a face-disarm decision accounts for the motion heuristic, not just
    embedding similarity.
    """
    await state.note_camera_frame()

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
        # ~21ms of OpenCV per frame, and the ESP32-CAM pushes two frames a
        # second — 4% of this process's only event loop, spent where it also
        # has to broadcast alarm state, answer the boards and hold every
        # WebSocket. The lock is what keeps a concurrent enrolment from
        # rebuilding the model underneath this; see [_recognizer_lock].
        async with _recognizer_lock:
            face_id = await asyncio.to_thread(faces.recognize, image_bytes)
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

    live = faces.is_live(match.id)
    await state.log_face_recognized(match.name, live=live)
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
#
# Separate profile, shared trust store: `build_client` is what stops this from
# parsing the system CA bundle a third time at import, and it also supplies
# the `trust_env=False` this client was missing — the camera is a LAN IP, and
# an HTTP_PROXY in the environment would send the stream request to a proxy
# with no route to it.
_cam_stream_client = build_client(
    timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
    limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
)


async def _open_camera_stream() -> httpx.Response:
    """Opens the ESP32-CAM's own MJPEG stream (the hub's one upstream connection).

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
    return upstream


# One connection to the camera, shared by every viewer (see camera_hub.py): the
# camera can serve only one stream at a time, so each app opening the Camera tab
# used to push the previous one off.
_camera_hub = CameraHub(_open_camera_stream)


@app.get("/camera/stream", dependencies=[Depends(require_app)])
async def proxy_camera_stream():
    """Relays the ESP32-CAM's MJPEG multipart stream to the app, chunk by chunk,
    without buffering a frame (let alone the whole stream) in memory and without
    blocking this process's single event loop for anyone else on it (the
    WebSocket broadcast included) — every call in this chain is async, so unlike
    camera_worker.py (a separate *process*, specifically because
    cv2.VideoCapture.read() blocks) this genuinely belongs in the main backend
    process.

    Any number of viewers can watch at once: they all share the hub's single
    upstream connection. The bytes are forwarded verbatim — the camera's own
    multipart boundary token is baked into every "--<boundary>" delimiter
    already present in the stream, so the boundary parameter must stay exactly as
    the camera sent it. Only the media type is relabelled, see
    `app_stream_content_type`.
    """
    content_type, chunks = await _camera_hub.subscribe()
    return StreamingResponse(chunks, media_type=app_stream_content_type(content_type))


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


# Last, on purpose — see mount_web_app. HOMEGUARD_WEB_DIR overrides where the
# build is looked for; the default is where `flutter build web` puts it.
_web_dir = Path(
    os.environ.get("HOMEGUARD_WEB_DIR")
    or Path(__file__).resolve().parents[2] / "app" / "build" / "web"
)
if mount_web_app(app, _web_dir):
    logging.getLogger("web").info("Serving the web app from %s", _web_dir)

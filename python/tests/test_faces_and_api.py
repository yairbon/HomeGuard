"""Face enrolment/recognition plumbing, and the API-level contracts the
Flutter app relies on.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from backend.face_recognition_service import FaceRecognitionService

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"


def _png(width: int = 1, height: int = 1) -> bytes:
    """Builds a genuinely valid RGB PNG.

    Written out rather than hardcoded as a hex blob because the hardcoded one
    this replaced had a corrupt IDAT CRC — OpenCV could never decode it, and
    the test that used it passed only because the old code returned early on
    "not trained yet" before it ever tried.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


CORRUPT_IMAGE = b"\x89PNG\r\n\x1a\n" + b"definitely not a real png body"


# ---- Recognition service ---------------------------------------------------


def test_dnn_backend_is_selected_when_models_are_present(tmp_path):
    """The whole point of the upgrade: if the ONNX models are there, the
    modern recognizer is what runs — not the legacy fallback."""
    service = FaceRecognitionService(faces_dir=Path(tmp_path) / "f", models_dir=MODELS_DIR)
    assert service.backend_name == "sface"


def test_falls_back_to_lbph_without_the_onnx_models(tmp_path):
    """A checkout missing the ONNX files should still work, just less well —
    not fail to start."""
    models = Path(tmp_path) / "models"
    models.mkdir()
    # Only the cascade, no ONNX.
    cascade = MODELS_DIR / "haarcascade_frontalface_default.xml"
    (models / cascade.name).write_bytes(cascade.read_bytes())

    service = FaceRecognitionService(faces_dir=Path(tmp_path) / "f", models_dir=models)
    assert service.backend_name == "lbph"


def test_missing_every_model_fails_at_startup(tmp_path):
    """Fail loudly at boot rather than on the first camera frame at 3am."""
    empty = Path(tmp_path) / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        FaceRecognitionService(faces_dir=Path(tmp_path) / "f", models_dir=empty)


def test_corrupt_image_raises_value_error(tmp_path):
    service = FaceRecognitionService(faces_dir=Path(tmp_path) / "f", models_dir=MODELS_DIR)
    with pytest.raises(ValueError):
        service.register_face("1", CORRUPT_IMAGE)


def test_faceless_image_is_not_registered(tmp_path):
    """A blank image has no face, so there's nothing to enrol — and the
    detector must not hallucinate one."""
    service = FaceRecognitionService(faces_dir=Path(tmp_path) / "f", models_dir=MODELS_DIR)
    assert service.register_face("1", _png(64, 64)) is False


def test_untrained_recognizer_matches_nothing(tmp_path):
    service = FaceRecognitionService(faces_dir=Path(tmp_path) / "f", models_dir=MODELS_DIR)
    assert service.recognize(_png(64, 64)) is None


# ---- API contracts ---------------------------------------------------------


async def test_corrupt_frame_is_422_not_500(auth_client):
    """An undecodable upload is a bad request, not a server fault. This
    escaped as a 500 with a traceback, and was masked for a long time because
    the old recognizer checked "am I trained?" before it ever decoded — so it
    only blew up once a face had actually been enrolled.
    """
    response = await auth_client.post(
        "/camera/recognize", files={"photo": ("bad.png", CORRUPT_IMAGE, "image/png")}
    )
    assert response.status_code == 422


async def test_corrupt_training_photo_is_422_not_500(auth_client):
    face = (await auth_client.post("/faces", json={"name": "Probe"})).json()
    response = await auth_client.post(
        f"/faces/{face['id']}/photo", files={"photo": ("bad.png", CORRUPT_IMAGE, "image/png")}
    )
    assert response.status_code == 422


async def test_faceless_training_photo_is_422(auth_client):
    face = (await auth_client.post("/faces", json={"name": "Probe"})).json()
    response = await auth_client.post(
        f"/faces/{face['id']}/photo", files={"photo": ("blank.png", _png(64, 64), "image/png")}
    )
    assert response.status_code == 422


async def test_photo_upload_for_unknown_face_is_404(auth_client):
    response = await auth_client.post(
        "/faces/does-not-exist/photo",
        files={"photo": ("blank.png", _png(64, 64), "image/png")},
    )
    assert response.status_code == 404


async def test_recognize_returns_no_match_on_a_faceless_frame(auth_client):
    response = await auth_client.post(
        "/camera/recognize", files={"photo": ("blank.png", _png(64, 64), "image/png")}
    )
    assert response.status_code == 200
    assert response.json()["matched"] is False


async def test_recognize_short_circuits_when_face_id_is_disabled(auth_client):
    await auth_client.post("/face-id-enabled", json={"enabled": False})
    response = await auth_client.post(
        "/camera/recognize", files={"photo": ("bad.png", CORRUPT_IMAGE, "image/png")}
    )
    # Disabled means the frame is never decoded at all, so even a corrupt one
    # comes back as a clean "no match".
    assert response.status_code == 200
    assert response.json()["matched"] is False


async def test_face_ids_are_never_recycled(auth_client):
    """A recycled id would silently attach a new person to the previous
    holder's training photos on disk."""
    first = (await auth_client.post("/faces", json={"name": "A"})).json()
    await auth_client.delete(f"/faces/{first['id']}")
    second = (await auth_client.post("/faces", json={"name": "B"})).json()
    assert second["id"] != first["id"]


async def test_faces_crud_round_trip(auth_client):
    before = len((await auth_client.get("/faces")).json())

    created = (await auth_client.post("/faces", json={"name": "Temporary"})).json()
    assert created["name"] == "Temporary"
    assert len((await auth_client.get("/faces")).json()) == before + 1

    assert (await auth_client.delete(f"/faces/{created['id']}")).json()["ok"] is True
    assert len((await auth_client.get("/faces")).json()) == before


async def test_arm_reports_whether_hardware_acknowledged(auth_client):
    """With no door controller configured, the request still succeeds but
    `hardware` is False — that difference is what lets the app tell the user
    its view may be ahead of the physical panel."""
    body = (await auth_client.post("/arm", json={"mode": "away"})).json()
    assert body["ok"] is True
    assert body["hardware"] is False


async def test_state_shape_matches_the_flutter_client(auth_client):
    """`backend/models.py` mirrors `app/lib/services/backend_service.dart`
    field-for-field. A field renamed on one side and not the other is a
    runtime crash in the app's JSON decoding, not a compile error — so the
    contract is pinned here.
    """
    body = (await auth_client.get("/state")).json()
    assert set(body) == {
        "alarm_state",
        "sensors",
        "events",
        "faces",
        "esp32_online",
        "boards",
        "camera_online",
        "face_id_enabled",
        "night_mode",
        "emergency_number",
        "police_call_delay_seconds",
    }
    assert set(body["sensors"][0]) == {"type", "label", "ok", "bypassed"}
    assert set(body["boards"][0]) == {"name", "online", "last_seen"}


async def test_websocket_requires_a_token(app_module):
    """An unauthenticated socket must be refused before `accept()`, so it is
    never registered for broadcasts and never receives a single state frame —
    which would otherwise leak the whole system state, event log included.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app_module.app) as http:
        with pytest.raises(WebSocketDisconnect):
            with http.websocket_connect("/ws") as ws:
                ws.receive_text()

        with pytest.raises(WebSocketDisconnect):
            with http.websocket_connect("/ws?token=forged") as ws:
                ws.receive_text()


async def test_websocket_pushes_state_to_an_authenticated_client(app_module):
    import json

    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as http:
        token = http.post("/login", json={"password": "homeguard"}).json()["token"]
        with http.websocket_connect(f"/ws?token={token}") as ws:
            snapshot = json.loads(ws.receive_text())
    assert snapshot["alarm_state"] == "disarmed"
    assert len(snapshot["boards"]) == 3

"""Face detection + recognition using OpenCV's built-in Haar cascade
detector and LBPH (Local Binary Patterns Histograms) recognizer.

Deliberately not using the popular `face_recognition` package — it wraps
dlib, which needs a CMake/Visual-Studio native build on Windows. Given
this machine's history with native toolchain pain (see CLAUDE.md), LBPH
is the pragmatic choice: pip-installable, no native build step, and
accurate enough for a small household's approved-faces list.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("face_recognition")

FACE_SIZE = (200, 200)
# LBPH's predict() returns a *distance* — lower is a better match. This is
# a starting point; tune against real photos once the camera is wired up.
MATCH_CONFIDENCE_THRESHOLD = 80.0


class FaceRecognitionService:
    """Owns the on-disk training photos (one folder per face_id under
    `faces_dir`) and the trained LBPH recognizer built from them. One
    instance is created at backend startup (see main.py) and shared
    across all requests.
    """

    def __init__(self, faces_dir: Path, cascade_path: Path):
        """Prepares `faces_dir` (creating it if needed) and loads the
        Haar cascade used for face *detection* (finding a face's bounding
        box) — separate from the LBPH recognizer used for face
        *identification* (deciding whose face it is). Raises
        FileNotFoundError immediately if the cascade file is missing,
        so a misconfigured install fails at startup rather than on the
        first request. Calls `retrain()` at the end so any photos already
        on disk (from a previous run) are loaded right away.
        """
        self.faces_dir = faces_dir
        self.faces_dir.mkdir(parents=True, exist_ok=True)

        if not cascade_path.exists():
            raise FileNotFoundError(
                f"Haar cascade not found at {cascade_path} — see README for the download step."
            )
        self._detector = cv2.CascadeClassifier(str(cascade_path))
        self._recognizer = cv2.face.LBPHFaceRecognizer_create()
        self._label_to_face_id: dict[int, str] = {}
        self._trained = False
        self.retrain()

    def _detect_face(self, image: np.ndarray) -> np.ndarray | None:
        """Finds the largest face in `image` (a decoded BGR image) and
        returns it as a grayscale FACE_SIZE crop ready for the LBPH
        recognizer, or None if no face was found. Both the cascade
        detector and LBPH operate on grayscale, hence the conversion.
        `scaleFactor`/`minNeighbors` are OpenCV's standard detectMultiScale
        tuning knobs (image pyramid step size / how many overlapping
        detections are required to confirm a face) — these are its usual
        default-ish values, not specifically tuned for this project.
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = self._detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
        if len(faces) == 0:
            return None
        # Largest detected face — most likely the intended subject.
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return cv2.resize(gray[y : y + h, x : x + w], FACE_SIZE)

    def register_face(self, face_id: str, image_bytes: bytes) -> bool:
        """Detects a face in the given image and adds it to face_id's
        training set. Returns False if no face was found. Call retrain()
        after adding images (register_faces_batch does this for you).
        """
        image = _decode_image(image_bytes)
        face = self._detect_face(image)
        if face is None:
            return False

        person_dir = self.faces_dir / face_id
        person_dir.mkdir(parents=True, exist_ok=True)
        existing = list(person_dir.glob("*.png"))
        cv2.imwrite(str(person_dir / f"{len(existing)}.png"), face)
        return True

    def remove_face(self, face_id: str) -> None:
        """Deletes all training photos for `face_id` from disk and
        retrains so the recognizer immediately forgets that person. A
        no-op (still retrains) if the folder doesn't exist.

        Uses rmtree rather than unlinking `*.png` and calling rmdir: rmdir
        raises on a non-empty directory, so a single stray file the glob
        didn't match (a `.DS_Store`/`Thumbs.db` from a synced folder, a
        half-written upload) would make this raise. main.py calls this
        *before* `state.remove_face`, so that raise would leave the photos
        deleted but the face still listed in the app — the worst of both
        outcomes for a delete that's supposed to forget someone entirely.
        """
        person_dir = self.faces_dir / face_id
        if person_dir.exists():
            shutil.rmtree(person_dir, ignore_errors=True)
        self.retrain()

    def retrain(self) -> None:
        """Rebuilds the LBPH recognizer from every photo currently on
        disk under `faces_dir`. Called after any add/remove so the
        recognizer never runs against stale data.

        LBPH needs small sequential *integer* labels, not the arbitrary
        string face_ids used everywhere else in this app, so this
        reassigns a fresh int label per person (via `enumerate`) on every
        call and rebuilds `_label_to_face_id` to map back to the real
        face_id afterwards. Because labels are reassigned from scratch
        each time, a given face_id's label is not stable across retrains
        — always go through `_label_to_face_id`, never assume a label
        value means anything on its own.
        """
        images: list[np.ndarray] = []
        labels: list[int] = []
        self._label_to_face_id = {}

        for label, person_dir in enumerate(sorted(self.faces_dir.iterdir())):
            if not person_dir.is_dir():
                continue
            face_id = person_dir.name
            self._label_to_face_id[label] = face_id
            for img_path in person_dir.glob("*.png"):
                img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    images.append(img)
                    labels.append(label)

        if not images:
            self._trained = False
            logger.info("No training images yet — recognizer left untrained.")
            return

        self._recognizer.train(images, np.array(labels))
        self._trained = True
        logger.info("Retrained on %d images across %d people.", len(images), len(self._label_to_face_id))

    def recognize(self, image_bytes: bytes) -> str | None:
        """Returns the matching face_id, or None if no face was found or
        no approved face matched confidently enough.
        """
        if not self._trained:
            return None
        image = _decode_image(image_bytes)
        face = self._detect_face(image)
        if face is None:
            return None

        label, confidence = self._recognizer.predict(face)
        if confidence > MATCH_CONFIDENCE_THRESHOLD:
            return None
        return self._label_to_face_id.get(label)


def _decode_image(image_bytes: bytes) -> np.ndarray:
    """Decodes raw image bytes (e.g. straight from an UploadFile.read())
    into an OpenCV BGR image array. Raises ValueError instead of letting
    cv2.imdecode return None silently, so callers get a clear error for
    corrupt/non-image uploads instead of a confusing crash further down.
    """
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode image data")
    return image

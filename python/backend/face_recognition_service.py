"""Face detection + recognition, built on the DNN models that ship with
OpenCV's own model zoo: YuNet for detection and SFace for recognition.

Both are ONNX files loaded by `opencv-contrib-python`, which this project
already depends on. That matters more than it might sound: it means upgrading
the recognition quality cost zero new dependencies and, critically, zero
native build steps. The obvious "better" alternatives all fail that test —
`face_recognition` wraps dlib, and `insightface` ships a Cython extension;
both require a working MSVC/CMake toolchain on Windows, which is precisely
the class of problem this project has already lost days to (see CLAUDE.md).

What changed and why
--------------------
The previous implementation used a Haar cascade for detection and LBPH for
recognition. Both are from the 2000s and both are weak in the conditions a
home security camera actually operates in:

- Haar cascades miss faces that are turned, partly occluded, or small in
  frame, and produce false positives on textured backgrounds. YuNet is a
  small CNN that handles pose and scale far better, and returns a confidence
  score plus facial landmarks.
- LBPH compares local texture histograms and is very sensitive to lighting
  and alignment — the exact things that vary between an enrollment photo
  taken indoors and a doorway camera at night. SFace produces a 128-dimension
  embedding trained for face *identity*, and comparison is a cosine distance
  between embeddings.

The landmarks are what make the biggest practical difference: SFace's
`alignCrop` uses them to warp each face to a canonical pose before embedding
it, so a head tilt no longer changes the descriptor much.

The old path is kept as an automatic fallback (see `_LbphBackend`) so a
checkout without the ONNX files still works rather than failing outright.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("face_recognition")

# --- YuNet (detection) tuning ---------------------------------------------
# Minimum confidence for a detection to count. 0.9 is deliberately strict:
# a false positive here becomes a face embedding trained into someone's
# identity, which is worse than missing a frame — the camera sends many.
DETECT_SCORE_THRESHOLD = 0.9
# Non-maximum-suppression IoU threshold; OpenCV's documented default.
DETECT_NMS_THRESHOLD = 0.3
DETECT_TOP_K = 5000

# --- SFace (recognition) tuning -------------------------------------------
# Cosine similarity above which two embeddings are considered the same
# person. 0.363 is the threshold OpenCV publishes for this exact model,
# calibrated on LFW — not a number invented here. Higher = stricter.
#
# Note the polarity is the opposite of the LBPH threshold this replaced:
# cosine similarity is "higher is more similar", where LBPH's distance was
# "lower is more similar". Getting these backwards would match everyone.
MATCH_SIMILARITY_THRESHOLD = 0.363

# Size the LBPH fallback normalizes faces to. Unused by the SFace path,
# which does its own landmark-based alignment.
LBPH_FACE_SIZE = (200, 200)
LBPH_MATCH_DISTANCE_THRESHOLD = 80.0

# --- Liveness (motion-based) tuning ----------------------------------------
# YuNet returns 5 facial landmarks with every detection — already computed
# on every recognize() call, for alignCrop's benefit, so reusing them here
# costs nothing extra. The idea: a live face has small, involuntary motion
# (breathing, postural sway, a blink) even when someone is trying to hold
# still; a printed photo or a phone screen held up to the camera does not,
# unless it's being visibly waved around. Track a short rolling window of
# where those 5 points sit relative to the face's own bounding box, and
# require some minimum spread across that window before calling a match
# "live".
#
# This is a first line of defense against the easy case — a flat, static
# photo — not a real anti-spoofing system. It does nothing against a video
# replay, and it can likely be defeated by deliberately jiggling a printed
# photo. A stronger, model-based anti-spoofing check is tracked separately
# as future work; this only closes the trivial case.
LIVENESS_WINDOW = timedelta(seconds=2.5)
LIVENESS_MIN_SAMPLES = 3
# Minimum average landmark movement across the window, as a fraction of face
# size (so it scales with how close the subject is to the camera). Unlike
# MATCH_SIMILARITY_THRESHOLD above, this is *not* a published, calibrated
# number — there is no equivalent industry constant for "how much does a
# real face wobble in 2.5 seconds of webcam-grade landmarks". 0.008 (0.8% of
# face size) is a starting guess from reasoning about pixel-scale jitter,
# not measurement against real footage, and should be expected to need
# retuning once it's actually exercised against a live camera.
LIVENESS_JITTER_THRESHOLD = 0.008


def is_live_landmark_sequence(
    samples: Sequence[Sequence[tuple[float, float]]],
    *,
    min_samples: int = LIVENESS_MIN_SAMPLES,
    threshold: float = LIVENESS_JITTER_THRESHOLD,
) -> bool:
    """Decides whether a short history of landmark positions shows enough
    natural motion to belong to a live face rather than a static photo.

    Pure and OpenCV-free by design, so it can be unit-tested against
    hand-built coordinate arrays without decoding an actual image — see
    test_liveness.py. `samples` is one entry per detection, in any order
    (order doesn't matter here, only spread does); each entry is the same
    5 (x, y) landmark positions YuNet detects, already normalized to be
    resolution/distance-independent (see `_normalized_landmarks`) and in
    the same landmark order from sample to sample.

    Fails closed: fewer than `min_samples` returns False — "not yet
    established as live", not "assumed live" — so a match that has only
    been seen once or twice doesn't get the benefit of the doubt.

    The verdict itself is the (max - min) range of each landmark's x and
    its y across the window, averaged over both axes and all landmarks
    into a single scalar, checked against `threshold`. A live face's
    micro-movement produces a small but non-zero range; a rigidly held
    photo produces close to none.
    """
    if len(samples) < min_samples:
        return False

    landmark_count = len(samples[0])
    if landmark_count == 0 or any(len(sample) != landmark_count for sample in samples):
        return False

    per_landmark_ranges = []
    for i in range(landmark_count):
        xs = [sample[i][0] for sample in samples]
        ys = [sample[i][1] for sample in samples]
        per_landmark_ranges.append((max(xs) - min(xs)) + (max(ys) - min(ys)))

    jitter = sum(per_landmark_ranges) / len(per_landmark_ranges) / 2
    return jitter >= threshold


# --- Liveness (model-based anti-spoofing) tuning ---------------------------
# MiniFASNetV2 (python/models/minifasnet_v2.onnx, see MINIFASNET_LICENSE) is
# the second, independent liveness check, combined with — not substituted
# for — the motion heuristic above (see _DnnBackend.is_live: both must
# pass). The two fail differently, which is the point: a rigidly held
# printed photo has near-zero landmark jitter and never gets past the
# motion check at all, but a *jiggled* photo or a phone/screen replay has
# enough incidental hand-tremor jitter to clear LIVENESS_JITTER_THRESHOLD —
# exactly what got a spoof past the motion check alone in a live test
# (see the commit that wired this in) — while still carrying the
# texture/frequency-domain signature of a print or a screen, which is what
# this model was trained to see instead of motion.
#
# This model's contract, established by introspecting the ONNX graph
# directly rather than trusting generic MiniFASNetV2 docs (there is no
# first-party spec for this specific conversion — see the commit that
# added the file for its provenance as a third-party export):
#   - input: float32 NCHW (1, 3, 80, 80) — one 80x80 crop.
#   - output: float32 (1, 3) — *raw logits*, not softmax. The graph's last
#     op is a bare MatMul straight into the output tensor with nothing
#     after it, so softmax has to happen on this side of the boundary
#     (see is_live_spoof_logits).
#   - which of the 3 output classes means "real" isn't recoverable from the
#     graph at all — it's a training-time label convention, not a tensor
#     shape. Verified empirically instead: this checkout's own already-
#     enrolled face crops (python/faces/*/0.png, real people, not spoofs)
#     both score above 0.97 on class index 1 when fed as raw BGR float32 in
#     [0, 255] — no /255 normalization, the same convention this file
#     already uses feeding SFace — and do *not* score that way under
#     [0, 1]-normalized input, which instead produces near-identical output
#     whether fed as BGR or RGB, a strong tell that that variant sits
#     outside the model's trained input distribution rather than that it's
#     making a considered call. Index 1 = real also matches
#     minivision-ai/Silent-Face-Anti-Spoofing's own published convention
#     (label 1 = real; {0, 2} = print/replay attack), which is a strong
#     second signal this conversion preserved the original label order.
ANTI_SPOOF_INPUT_SIZE = (80, 80)
ANTI_SPOOF_REAL_CLASS_INDEX = 1
# Softmax probability of ANTI_SPOOF_REAL_CLASS_INDEX a crop must clear to
# count as real. Comfortably below the >0.97 this project's own enrollment
# photos scored (see above) to leave margin for the production crop being
# wider and background-inclusive (see ANTI_SPOOF_CROP_SCALE) rather than
# the tight enrollment crop this was measured against, and for ordinary
# camera/lighting variation — but still above 0.5, which (see
# is_live_spoof_logits) is what makes clearing this threshold also
# guarantee "real" is the single winning class rather than merely having
# cleared some floor while a spoof class still scored higher. Like
# LIVENESS_JITTER_THRESHOLD above, this is a starting estimate reasoned
# from the numbers above, not a published constant, and should be expected
# to need retuning once it's actually exercised against a real spoofing
# attempt rather than only genuine faces.
ANTI_SPOOF_REAL_THRESHOLD = 0.6
# How far to expand YuNet's face box before handing it to the model, as a
# multiple of the box's longer side. Unlike SFace's tight alignCrop, this
# family of models is trained to look at what *surrounds* the face — a
# photo's paper edge, a phone's bezel, screen glare — which a tight crop
# would discard along with the evidence of a spoof (see expand_face_box).
# This number is this project's best-effort match to
# "4_0_0_80x80_MiniFASNetV2", the checkpoint name this architecture is
# paired with in the original repo's own two-model ensemble — the actual
# crop algorithm used at training time isn't recoverable from the ONNX file
# (it's a preprocessing choice, not part of the graph), and this
# conversion's provenance doesn't document it either. If this model needs
# retuning once exercised against a real spoofing attempt, this crop scale
# is a more likely culprit than ANTI_SPOOF_REAL_THRESHOLD above, which at
# least rests on a measurement against real photos.
ANTI_SPOOF_CROP_SCALE = 4.0


def is_live_spoof_logits(
    logits: Sequence[float],
    *,
    threshold: float = ANTI_SPOOF_REAL_THRESHOLD,
) -> bool:
    """Decides whether MiniFASNetV2's raw 3-class output for one face crop
    looks like a real face rather than a print/replay spoof.

    Pure and model-free by design, mirroring is_live_landmark_sequence
    above — see test_liveness.py. `logits` is the *un-softmaxed* output
    _DnnBackend reads back from the ONNX graph's own final layer — see the
    tuning comment above for why softmax isn't baked into the graph.

    Fails closed: anything other than exactly 3 logits returns False
    rather than guessing at a differently-shaped output.

    `threshold` is compared against the softmax probability of
    ANTI_SPOOF_REAL_CLASS_INDEX, not a raw logit — logit magnitudes drift
    with lighting/framing in a way a probability doesn't. See
    ANTI_SPOOF_REAL_THRESHOLD's own comment for why clearing it also
    guarantees "real" is the winning class, not just above some floor.
    """
    if len(logits) != 3:
        return False
    largest = max(logits)
    exp_values = [math.exp(value - largest) for value in logits]
    probability_real = exp_values[ANTI_SPOOF_REAL_CLASS_INDEX] / sum(exp_values)
    return probability_real >= threshold


class FaceRecognitionService:
    """Owns the on-disk training photos (one folder per face_id under
    `faces_dir`) and the recognizer built from them. One instance is created
    at backend startup (see main.py) and shared across all requests.

    Picks its backend at construction time: the DNN path if both ONNX models
    are present in `models_dir`, otherwise the legacy Haar+LBPH path. Callers
    don't need to know which is active — `register_face`, `retrain` and
    `recognize` behave the same either way.
    """

    def __init__(self, faces_dir: Path, models_dir: Path):
        """Prepares `faces_dir` (creating it if needed) and selects a
        backend. Raises FileNotFoundError only if *neither* backend's model
        files are available, so a misconfigured install fails at startup
        rather than on the first camera request.
        """
        self.faces_dir = faces_dir
        self.faces_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = models_dir

        yunet = models_dir / "yunet.onnx"
        sface = models_dir / "sface.onnx"
        cascade = models_dir / "haarcascade_frontalface_default.xml"
        # Not required the way yunet/sface are — see _DnnBackend.__init__ —
        # because it strengthens face-disarm's liveness check rather than
        # being load-bearing for detection/recognition itself; a checkout
        # missing just this file should still recognize faces, just with
        # face-disarm's model-based check failing closed rather than the
        # whole service refusing to start.
        antispoof = models_dir / "minifasnet_v2.onnx"

        if yunet.exists() and sface.exists():
            self._backend: _Backend = _DnnBackend(yunet, sface, antispoof)
        elif cascade.exists():
            logger.warning(
                "yunet.onnx/sface.onnx not found in %s — falling back to the legacy "
                "Haar+LBPH recognizer, which is significantly less accurate. "
                "See python/README.md for how to fetch the ONNX models.",
                models_dir,
            )
            self._backend = _LbphBackend(cascade)
        else:
            raise FileNotFoundError(
                f"No face models found in {models_dir} — expected yunet.onnx + sface.onnx "
                "(preferred) or haarcascade_frontalface_default.xml. See python/README.md."
            )

        self.retrain()

    def register_face(self, face_id: str, image_bytes: bytes) -> bool:
        """Detects a face in the given image and adds it to face_id's
        training set. Returns False if no face was found. Call retrain()
        afterwards so the new photo actually affects recognition.

        Stores the *normalized* face (aligned crop for the DNN backend,
        grayscale resize for LBPH) rather than the original photo: it's what
        the recognizer consumes, it's much smaller, and it means no raw
        photograph of anyone is kept on disk beyond what recognition needs.
        """
        image = _decode_image(image_bytes)
        face = self._backend.prepare(image)
        if face is None:
            return False

        person_dir = self.faces_dir / face_id
        person_dir.mkdir(parents=True, exist_ok=True)
        # Named by count rather than index-of-last so a previously deleted
        # file in the middle can't cause a collision that overwrites a photo.
        existing = len(list(person_dir.glob("*.png")))
        cv2.imwrite(str(person_dir / f"{existing}.png"), face)
        return True

    def remove_face(self, face_id: str) -> None:
        """Deletes all training photos for `face_id` from disk and retrains
        so the recognizer immediately forgets that person. A no-op (but still
        retrains) if the folder doesn't exist.
        """
        person_dir = self.faces_dir / face_id
        if person_dir.exists():
            for f in person_dir.glob("*.png"):
                f.unlink()
            # Only removes the directory if it's now empty; a stray non-PNG
            # file left by a user would otherwise raise OSError here.
            try:
                person_dir.rmdir()
            except OSError:
                logger.warning("%s still has other files — left in place.", person_dir)
        self.retrain()

    def retrain(self) -> None:
        """Rebuilds the recognizer from every photo currently on disk under
        `faces_dir`. Called after any add/remove so the recognizer never runs
        against stale data.
        """
        training: dict[str, list[np.ndarray]] = {}
        for person_dir in sorted(self.faces_dir.iterdir()):
            if not person_dir.is_dir():
                continue
            images = [
                img
                for img_path in sorted(person_dir.glob("*.png"))
                if (img := cv2.imread(str(img_path), cv2.IMREAD_UNCHANGED)) is not None
            ]
            if images:
                training[person_dir.name] = images

        self._backend.train(training)
        total = sum(len(v) for v in training.values())
        if total:
            logger.info(
                "Retrained on %d images across %d people (%s backend).",
                total,
                len(training),
                self._backend.name,
            )
        else:
            logger.info("No training images yet — recognizer left untrained.")

    def recognize(self, image_bytes: bytes) -> str | None:
        """Returns the matching face_id, or None if no face was found or no
        approved face matched confidently enough.
        """
        return self._backend.recognize(_decode_image(image_bytes))

    def is_live(self, face_id: str) -> bool:
        """True if face_id's recent recognitions show enough landmark
        motion to be treated as a live face rather than a static photo.

        Only meaningful right after a matching `recognize()` call, since
        that call is what feeds the window this checks — see
        `_DnnBackend.is_live`. The LBPH fallback has no landmarks to
        measure and always returns True (`_LbphBackend.is_live`); it's
        already the documented lower-fidelity path, and this doesn't add
        complexity there.
        """
        return self._backend.is_live(face_id)

    @property
    def backend_name(self) -> str:
        """Which recognition backend is active ("sface" or "lbph"), so the
        README's claims and any diagnostics can be checked against reality
        rather than assumed."""
        return self._backend.name


class _Backend:
    """Interface the two recognition implementations share.

    `prepare` turns a full camera frame into the single normalized face image
    that gets stored and compared; `train` rebuilds from stored faces;
    `recognize` maps a frame to a face_id or None; `is_live` says whether
    that face_id's recent matches look like a live face rather than a
    static photo.
    """

    name = "base"

    def prepare(self, image: np.ndarray) -> np.ndarray | None:
        raise NotImplementedError

    def train(self, training: dict[str, list[np.ndarray]]) -> None:
        raise NotImplementedError

    def recognize(self, image: np.ndarray) -> str | None:
        raise NotImplementedError

    def is_live(self, face_id: str) -> bool:
        raise NotImplementedError


class _DnnBackend(_Backend):
    """YuNet detection + SFace embeddings, compared by cosine similarity.

    Unlike LBPH there is no "model" to fit: training is just computing one
    embedding per enrolled photo and keeping them in memory. Recognition
    embeds the incoming face and finds the nearest enrolled embedding. That
    makes enrollment cheap and, more usefully, makes adding one photo
    incremental rather than a full retrain of a statistical model.
    """

    name = "sface"

    def __init__(self, yunet_path: Path, sface_path: Path, antispoof_path: Path):
        # Input size is a placeholder — `setInputSize` is called per frame in
        # `_detect`, because YuNet needs the exact dimensions of the image
        # it's about to see and camera frames aren't all one size.
        self._detector = cv2.FaceDetectorYN.create(
            str(yunet_path),
            "",
            (320, 320),
            DETECT_SCORE_THRESHOLD,
            DETECT_NMS_THRESHOLD,
            DETECT_TOP_K,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(sface_path), "")
        if antispoof_path.exists():
            self._antispoof: cv2.dnn.Net | None = cv2.dnn.readNetFromONNX(str(antispoof_path))
        else:
            # Fails closed, not open: is_live() below treats "no model
            # loaded" the same as "model said spoof", not "skip this check
            # and fall back to the motion check alone" — a face-disarm that
            # silently downgraded its own security bar because a file went
            # missing would be worse than one that stops disarming at all
            # until the file is restored.
            logger.warning(
                "minifasnet_v2.onnx not found in %s — face-disarm's model-based "
                "anti-spoofing check will fail closed (never live), so a recognized "
                "face can no longer disarm at all until this file is restored. See "
                "python/README.md.",
                antispoof_path.parent,
            )
            self._antispoof = None
        # face_id -> list of embeddings, one per enrolled photo.
        self._embeddings: dict[str, list[np.ndarray]] = {}
        # face_id -> recent (timestamp, normalized-landmarks) samples, the
        # rolling window is_live() judges. Pruned to LIVENESS_WINDOW on every
        # read and write (see _prune_landmark_history) rather than capped by
        # count, since frame rate isn't guaranteed.
        self._landmark_history: dict[str, deque[tuple[datetime, list[tuple[float, float]]]]] = {}
        # face_id -> whether the *most recent* recognize() match for that id
        # looked real to MiniFASNetV2. Unlike landmark history this needs no
        # window/deque: it's a single-frame classification, not a sequence
        # one, and is_live() is only ever called immediately after a
        # recognize() call that just matched this exact id (see main.py's
        # recognize_frame), so "most recent" is always "from this request".
        self._spoof_verdicts: dict[str, bool] = {}

    def _detect(self, image: np.ndarray) -> np.ndarray | None:
        """Returns YuNet's row for the largest detected face, or None.

        Each row is [x, y, w, h, 5 landmark x/y pairs..., score] — the
        landmarks are the part `alignCrop` needs, which is why this returns
        the whole row rather than just a bounding box.
        """
        height, width = image.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(image)
        if faces is None or len(faces) == 0:
            return None
        # Largest detected face — most likely the intended subject.
        return max(faces, key=lambda f: f[2] * f[3])

    def _detect_and_align(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        """Runs YuNet once and returns both its raw detection row (bounding
        box + landmarks + score) and the landmark-aligned crop SFace embeds.

        Factored out so `recognize()` can get at the landmarks `is_live`
        needs without detecting a second time — `prepare()` used to run
        `_detect` itself and hand back only the crop, which was fine as
        long as nothing else needed the row it discarded.
        """
        face = self._detect(image)
        if face is None:
            return None
        return face, self._recognizer.alignCrop(image, face)

    def prepare(self, image: np.ndarray) -> np.ndarray | None:
        """Detects and returns a landmark-aligned 112x112 BGR crop.

        The alignment is the point: `alignCrop` warps the face so the eyes
        and mouth land in canonical positions, which is what lets SFace
        compare an enrollment photo against a camera frame taken at a
        different angle.
        """
        detected = self._detect_and_align(image)
        return None if detected is None else detected[1]

    def train(self, training: dict[str, list[np.ndarray]]) -> None:
        """Computes and caches an embedding for every stored face image.

        Stored images are already aligned crops (see
        FaceRecognitionService.register_face), so they go straight to
        `feature` without re-detection — re-running the detector on a
        112x112 crop would often fail, since the face fills the whole frame.
        """
        self._embeddings = {}
        for face_id, images in training.items():
            features = []
            for img in images:
                # Stored PNGs are 3-channel BGR, but a hand-placed grayscale
                # file would break `feature` — convert rather than crash.
                if img.ndim == 2:
                    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                features.append(self._recognizer.feature(img))
            if features:
                self._embeddings[face_id] = features

    def recognize(self, image: np.ndarray) -> str | None:
        """Embeds the largest face in `image` and returns the face_id of the
        most similar enrolled face, provided it clears
        [MATCH_SIMILARITY_THRESHOLD].

        Takes the best match across all enrolled photos of a person (rather
        than, say, their average) so one good frontal enrollment photo is
        enough to match, even if their other photos are poor.

        A successful match also feeds this detection's landmarks into that
        face_id's liveness window (see `is_live`) and runs the model-based
        anti-spoofing check against this same frame (see `_is_real_face`),
        both piggybacking on the detection this call already had to run
        rather than detecting a second time.
        """
        if not self._embeddings:
            return None
        detected = self._detect_and_align(image)
        if detected is None:
            return None
        face_row, aligned = detected

        probe = self._recognizer.feature(aligned)
        best_id: str | None = None
        best_score = -1.0
        for face_id, features in self._embeddings.items():
            for feature in features:
                score = self._recognizer.match(
                    probe, feature, cv2.FaceRecognizerSF_FR_COSINE
                )
                if score > best_score:
                    best_score = score
                    best_id = face_id

        if best_score < MATCH_SIMILARITY_THRESHOLD:
            return None
        self._record_landmark_sample(best_id, face_row)
        self._spoof_verdicts[best_id] = self._is_real_face(image, face_row)
        return best_id

    def _record_landmark_sample(self, face_id: str, face_row: np.ndarray) -> None:
        """Appends this detection's normalized landmarks to face_id's
        rolling liveness window, then prunes anything now outside
        LIVENESS_WINDOW. A degenerate zero-size bounding box (not expected
        in practice, but `_normalized_landmarks` would divide by it) is
        skipped rather than recorded.
        """
        normalized = _normalized_landmarks(face_row)
        if normalized is None:
            return
        now = datetime.now()
        self._landmark_history.setdefault(face_id, deque()).append((now, normalized))
        self._prune_landmark_history(face_id, now)

    def _prune_landmark_history(self, face_id: str, now: datetime | None = None) -> None:
        """Drops samples older than LIVENESS_WINDOW for face_id.

        Called on every write (so a stale sample never lingers past its
        window) and on every `is_live` read (so a face that hasn't been
        seen in a while, then reappears, is judged on fresh samples only —
        jitter measured seconds or minutes apart no longer describes
        *micro*-movement).
        """
        history = self._landmark_history.get(face_id)
        if not history:
            return
        cutoff = (now or datetime.now()) - LIVENESS_WINDOW
        while history and history[0][0] < cutoff:
            history.popleft()

    def _is_real_face(self, image: np.ndarray, face_row: np.ndarray) -> bool:
        """Runs MiniFASNetV2 on the region `face_row` describes within
        `image` and returns whether it looks like a real face rather than a
        print/replay spoof — see the ANTI_SPOOF_* tuning comment above
        `is_live_spoof_logits` for the model's input/output contract and
        why the crop is wider than the face itself.

        Fails closed: no loaded model (see __init__), a degenerate crop, or
        any error out of the forward pass itself all return False — the
        same posture as `is_live_landmark_sequence` returning False on too
        few samples. An infrastructure problem here must never be mistaken
        for "didn't check, so assume real".
        """
        if self._antispoof is None:
            return False
        height, width = image.shape[:2]
        box = tuple(float(v) for v in face_row[0:4])
        left, top, right, bottom = expand_face_box(box, (width, height), ANTI_SPOOF_CROP_SCALE)
        if right <= left or bottom <= top:
            return False
        crop = image[top:bottom, left:right]
        try:
            # scalefactor=1.0 and no mean subtraction: this conversion
            # expects raw [0, 255] BGR floats, not the [0, 1]-normalized
            # convention a PyTorch `ToTensor()` export usually implies —
            # established empirically, not assumed; see the tuning comment
            # above ANTI_SPOOF_INPUT_SIZE for how.
            blob = cv2.dnn.blobFromImage(
                crop,
                scalefactor=1.0,
                size=ANTI_SPOOF_INPUT_SIZE,
                mean=(0, 0, 0),
                swapRB=False,
            )
            self._antispoof.setInput(blob)
            logits = self._antispoof.forward()[0]
        except cv2.error:
            logger.warning("MiniFASNetV2 forward pass failed on a face crop.", exc_info=True)
            return False
        return is_live_spoof_logits([float(v) for v in logits])

    def is_live(self, face_id: str) -> bool:
        """See FaceRecognitionService.is_live. True only if *both*
        independent checks agree: `is_live_landmark_sequence` over the
        pruned LIVENESS_WINDOW of recent landmark samples, and this face
        id's most recent MiniFASNetV2 verdict (see `_is_real_face`, run
        once per matching frame from `recognize`). Defense in depth, not
        redundancy — see the ANTI_SPOOF_* tuning comment for what each one
        catches that the other doesn't.

        Short-circuits on the motion check first: it's a dict/deque lookup
        against already-computed numbers, versus a cached result of a CNN
        forward pass that already ran in `recognize` — cheaper, and no
        reason to even consult the spoof verdict for a face with no fresh
        motion history at all.
        """
        self._prune_landmark_history(face_id)
        history = self._landmark_history.get(face_id)
        if not history:
            return False
        if not is_live_landmark_sequence([landmarks for _, landmarks in history]):
            return False
        return self._spoof_verdicts.get(face_id, False)


def _normalized_landmarks(face_row: np.ndarray) -> list[tuple[float, float]] | None:
    """Extracts YuNet's 5 landmarks from a detection row (see
    `_DnnBackend._detect`'s docstring for the row layout) and expresses each
    relative to the face's own bounding box: origin at its top-left corner,
    scaled by its width/height. Returns None for a zero-size box, which
    normalizing against would divide by zero — not expected from a real
    detection, but cheap to guard against.

    The bounding-box-relative frame, not just the scaling, is what makes
    this usable for liveness at all. Scaling alone would still leave the
    *origin* at the frame's own pixel coordinates, so a printed photo
    jiggled by hand — the whole rigid set of 5 points translating together —
    would move exactly like the jitter this is trying to detect, since
    translation and liveness would be indistinguishable. Landmarks expressed
    relative to the box the detector drew around *this* frame's face
    instead only move when the geometry *within* the face changes — a
    blink, a small head turn — which a flat image moved to a new position
    or distance does not do, even if the hand holding it is not perfectly
    steady.
    """
    box_x, box_y, box_w, box_h = (float(v) for v in face_row[0:4])
    if box_w <= 0 or box_h <= 0:
        return None
    landmarks = face_row[4:14].reshape(5, 2)
    return [((float(lx) - box_x) / box_w, (float(ly) - box_y) / box_h) for lx, ly in landmarks]


def expand_face_box(
    box: tuple[float, float, float, float],
    image_size: tuple[int, int],
    scale: float,
) -> tuple[int, int, int, int]:
    """Expands a YuNet (x, y, w, h) face box to a square `scale` times the
    length of its longer side, centered on the same point as the original
    box, and clamped to stay inside an image of `image_size` = (width,
    height) — by shifting the box back into frame first and only shrinking
    it as a last resort, so a face near an edge still gets a full-size, if
    off-center, crop rather than a smaller and stretched one.

    Pure and OpenCV-free, like `is_live_landmark_sequence` and
    `is_live_spoof_logits` above — see test_liveness.py. Returns (left,
    top, right, bottom) pixel bounds; callers must still check for a
    degenerate (zero-area) result, which happens if `box` itself has zero
    width or height.
    """
    x, y, w, h = box
    width, height = image_size
    center_x, center_y = x + w / 2, y + h / 2
    # Can't ask for a crop bigger than the frame itself.
    side = min(max(w, h) * scale, width, height)
    half = side / 2
    left, top = center_x - half, center_y - half
    right, bottom = center_x + half, center_y + half
    if left < 0:
        right -= left
        left = 0.0
    if top < 0:
        bottom -= top
        top = 0.0
    if right > width:
        left -= right - width
        right = float(width)
    if bottom > height:
        top -= bottom - height
        bottom = float(height)
    return int(left), int(top), int(right), int(bottom)


class _LbphBackend(_Backend):
    """The original Haar cascade + LBPH path, kept as a fallback for a
    checkout that doesn't have the ONNX models.

    Retained rather than deleted so the backend still starts and recognizes
    *something* without them — but it is meaningfully less accurate, and the
    constructor logs a warning when this path is selected.
    """

    name = "lbph"

    def __init__(self, cascade_path: Path):
        self._detector = cv2.CascadeClassifier(str(cascade_path))
        self._recognizer = cv2.face.LBPHFaceRecognizer_create()
        self._label_to_face_id: dict[int, str] = {}
        self._trained = False

    def prepare(self, image: np.ndarray) -> np.ndarray | None:
        """Finds the largest face and returns it as a grayscale
        LBPH_FACE_SIZE crop. `scaleFactor`/`minNeighbors` are OpenCV's
        standard detectMultiScale knobs (image pyramid step / how many
        overlapping detections confirm a face) at their usual values.
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        faces = self._detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return cv2.resize(gray[y : y + h, x : x + w], LBPH_FACE_SIZE)

    def train(self, training: dict[str, list[np.ndarray]]) -> None:
        """Rebuilds the LBPH model.

        LBPH needs small sequential *integer* labels, not the arbitrary
        string face_ids used everywhere else, so this assigns a fresh int
        label per person on every call and rebuilds `_label_to_face_id` to
        map back. Because labels are reassigned from scratch each time, a
        given face_id's label is not stable across retrains — always go
        through `_label_to_face_id`, never assume a label means anything.
        """
        images: list[np.ndarray] = []
        labels: list[int] = []
        self._label_to_face_id = {}

        for label, (face_id, person_images) in enumerate(sorted(training.items())):
            self._label_to_face_id[label] = face_id
            for img in person_images:
                images.append(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img)
                labels.append(label)

        if not images:
            self._trained = False
            return
        self._recognizer.train(images, np.array(labels))
        self._trained = True

    def recognize(self, image: np.ndarray) -> str | None:
        """Predicts a face_id. LBPH's `predict` returns a *distance*, so
        lower is a better match — the opposite polarity from SFace's cosine
        similarity above.
        """
        if not self._trained:
            return None
        face = self.prepare(image)
        if face is None:
            return None
        label, distance = self._recognizer.predict(face)
        if distance > LBPH_MATCH_DISTANCE_THRESHOLD:
            return None
        return self._label_to_face_id.get(label)

    def is_live(self, face_id: str) -> bool:
        """Always True. This fallback path has no facial landmarks to
        measure motion from — see the module docstring's note that LBPH is
        kept only as a lower-fidelity fallback, not developed further. A
        liveness signal for it would need a different mechanism entirely,
        which is out of scope for what is already the degraded path.
        """
        return True


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

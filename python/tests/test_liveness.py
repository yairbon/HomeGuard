"""The liveness heuristics in face_recognition_service.py — motion-based and
model-based — each factored out as a standalone decision over plain data
rather than images, so both are testable without a camera, a face YuNet can
actually detect, or (for the motion check) even OpenCV.

Deliberately tests only `is_live_landmark_sequence`, `is_live_spoof_logits`
and `expand_face_box` here, not the `_DnnBackend` plumbing around them (the
rolling per-face history, running the ONNX model, wiring both checks into
recognize()/is_live()). That plumbing needs a face YuNet can actually
detect and a real MiniFASNetV2 forward pass, and this codebase already
avoids building one where it can (see test_faces_and_api.py's blank-image
tests, which exist precisely because a *detectable* face image is hard to
construct by hand).
"""

from __future__ import annotations

import math

from backend.face_recognition_service import (
    ANTI_SPOOF_REAL_THRESHOLD,
    LIVENESS_JITTER_THRESHOLD,
    LIVENESS_MIN_SAMPLES,
    expand_face_box,
    is_live_landmark_sequence,
    is_live_spoof_logits,
)

# A plausible set of 5 normalized landmark positions (right eye, left eye,
# nose tip, right mouth corner, left mouth corner) — roughly where YuNet
# would place them on a frontal face, expressed relative to the face's own
# bounding box, the same shape `_normalized_landmarks` produces. The exact
# values don't matter to the function under test; this is just a believable
# base to perturb per sample.
_BASE_LANDMARKS = [
    (0.30, 0.35),
    (0.70, 0.35),
    (0.50, 0.55),
    (0.35, 0.75),
    (0.65, 0.75),
]


def _samples(deltas: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """One sample per (dx, dy) in `deltas`, with every landmark in
    `_BASE_LANDMARKS` nudged by it — standing in for the same face held at a
    series of slightly different positions across a few detections.
    """
    return [[(x + dx, y + dy) for x, y in _BASE_LANDMARKS] for dx, dy in deltas]


def test_too_few_samples_fails_closed():
    """Fewer than LIVENESS_MIN_SAMPLES must read as "not yet live", not
    "assumed live" — even when the samples present would, on their own,
    look like obvious motion. A spoofing attempt caught on its very first
    detection must not get the benefit of the doubt.
    """
    samples = _samples([(0.0, 0.0), (0.2, 0.2)])
    assert len(samples) < LIVENESS_MIN_SAMPLES
    assert is_live_landmark_sequence(samples) is False


def test_near_identical_points_are_not_live():
    """A rigidly held photo: the detector's landmarks barely move between
    detections, well under the threshold."""
    deltas = [(0.0, 0.0), (0.0001, -0.0001), (0.00005, 0.0002), (-0.0001, 0.0)]
    samples = _samples(deltas)
    assert len(samples) >= LIVENESS_MIN_SAMPLES
    assert is_live_landmark_sequence(samples) is False


def test_realistic_small_jitter_is_live():
    """A live face's natural micro-movement — breathing, postural sway — is
    small, but clears the threshold."""
    deltas = [(0.0, 0.0), (0.012, -0.008), (-0.006, 0.014), (0.009, 0.006)]
    samples = _samples(deltas)
    assert is_live_landmark_sequence(samples) is True


def test_threshold_is_compared_inclusively_at_the_boundary():
    """Documents the >= in the implementation: jitter exactly equal to the
    threshold counts as live, not just jitter strictly above it. Uses a
    single landmark so the arithmetic is easy to check by hand: two
    unmoving samples plus one that has moved by exactly 2 * threshold in x
    give a range of exactly `threshold` once averaged.
    """
    samples = [
        [(0.5, 0.5)],
        [(0.5, 0.5)],
        [(0.5 + 2 * LIVENESS_JITTER_THRESHOLD, 0.5)],
    ]
    assert is_live_landmark_sequence(samples) is True


def test_mismatched_landmark_counts_fail_closed():
    """Samples have to carry the same number of landmarks to be comparable
    at all; a caller bug that mixes counts must not produce a silent,
    arbitrary verdict."""
    samples = [
        [(0.1, 0.1), (0.2, 0.2)],
        [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)],
        [(0.1, 0.1), (0.2, 0.2)],
    ]
    assert is_live_landmark_sequence(samples) is False


def test_samples_with_no_landmarks_fail_closed():
    """An empty landmark list per sample (never produced by
    `_normalized_landmarks` in practice, but a caller could pass anything)
    must not be treated as zero jitter clearing the threshold."""
    assert is_live_landmark_sequence([[], [], []]) is False


def test_min_samples_and_threshold_are_configurable():
    """Callers can override the module-level tuning constants rather than
    being stuck with them — used here to pin the exact comparisons, not
    just the module defaults."""
    deltas = [(0.0, 0.0), (0.02, 0.0)]
    samples = _samples(deltas)
    assert is_live_landmark_sequence(samples, min_samples=2) is True
    assert is_live_landmark_sequence(samples, min_samples=2, threshold=1.0) is False


# --- is_live_spoof_logits ----------------------------------------------------
# MiniFASNetV2's raw 3-class output (real=index 1, see the ANTI_SPOOF_* tuning
# comment in face_recognition_service.py) turned into a real/spoof verdict.


def test_dominant_real_logit_is_live():
    """A confident "real" verdict from the model: index 1 far ahead of the
    other two classes, the shape a genuine, well-lit face crop should
    produce."""
    assert is_live_spoof_logits([0.0, 10.0, 0.0]) is True


def test_dominant_print_attack_logit_is_not_live():
    """A confident "print attack" verdict (index 0): must not be waved
    through just because *some* probability mass sits elsewhere."""
    assert is_live_spoof_logits([10.0, 0.0, 0.0]) is False


def test_dominant_replay_attack_logit_is_not_live():
    """Same as the print-attack case but for index 2 (the model's other
    spoof class, e.g. a screen replay) — both spoof indices must be
    rejected, not just index 0."""
    assert is_live_spoof_logits([0.0, 0.0, 10.0]) is False


def test_wrong_logit_count_fails_closed():
    """The model contract is exactly 3 classes; a caller passing anything
    else (a bug, a differently-shaped model swapped in by mistake) must not
    produce a silent, arbitrary verdict."""
    assert is_live_spoof_logits([1.0, 2.0]) is False
    assert is_live_spoof_logits([1.0, 2.0, 3.0, 4.0]) is False
    assert is_live_spoof_logits([]) is False


def test_spoof_threshold_is_compared_inclusively_at_the_boundary():
    """Documents the >= in the implementation, mirroring
    test_threshold_is_compared_inclusively_at_the_boundary above. Logits
    [0, ln(2), 0] softmax to exactly 0.5 at index 1 by hand (e^ln(2) = 2,
    so 2 / (1 + 2 + 1) = 0.5) — landing exactly on an overridden
    threshold of 0.5 must still count as live, not fall just short of it.
    """
    logits = [0.0, math.log(2), 0.0]
    assert is_live_spoof_logits(logits, threshold=0.5) is True


def test_spoof_threshold_is_configurable():
    """Same override pattern as the motion check's min_samples/threshold
    test above — a caller isn't stuck with ANTI_SPOOF_REAL_THRESHOLD."""
    logits = [0.0, math.log(2), 0.0]  # exactly 0.5 at index 1, see above
    assert is_live_spoof_logits(logits, threshold=0.4) is True
    assert is_live_spoof_logits(logits, threshold=0.6) is False


def test_default_threshold_is_the_module_constant():
    """The module-level default isn't just documentation — it's what
    actually gets used when a caller doesn't override it: omitting
    `threshold` must agree with passing ANTI_SPOOF_REAL_THRESHOLD
    explicitly, for logits on both sides of it."""
    for logits in ([0.0, 10.0, 0.0], [0.0, 0.0, 0.0], [10.0, 0.0, 0.0]):
        assert is_live_spoof_logits(logits) == is_live_spoof_logits(
            logits, threshold=ANTI_SPOOF_REAL_THRESHOLD
        )
    assert is_live_spoof_logits([0.0, 0.0, 0.0]) is False  # even odds across 3 classes


# --- expand_face_box ---------------------------------------------------------
# The wider, model-facing crop around a YuNet face box — deliberately bigger
# than SFace's tight alignCrop, since MiniFASNetV2 is trained to look at what
# *surrounds* the face (paper edge, phone bezel, screen glare) too.


def test_expand_face_box_centered_when_room_allows():
    """A box well away from every edge just grows around its own center —
    no clamping needed, so this pins the unclamped arithmetic on its own."""
    # 20x20 box centered at (50, 50); scale=2.0 over the 20px side gives a
    # 40x40 crop, i.e. 20px in every direction from the same center.
    result = expand_face_box((40.0, 40.0, 20.0, 20.0), (200, 200), 2.0)
    assert result == (30, 30, 70, 70)


def test_expand_face_box_shifts_instead_of_shrinking_at_top_left():
    """A box against the top-left corner still gets a *full-size* crop,
    shifted into frame rather than shrunk — the docstring's explicit
    contract ("shifting the box back into frame first and only shrinking it
    as a last resort"). A shrunk, off-center crop would feed the model less
    context right when a spoof (paper edge, phone bezel) is most likely to
    be visible near that same edge.
    """
    # 20x20 box right at the origin; scale=4.0 wants an 80x80 crop, which
    # comfortably fits inside the 200x200 frame — it just can't stay
    # centered on (10, 10) without going negative.
    result = expand_face_box((0.0, 0.0, 20.0, 20.0), (200, 200), 4.0)
    assert result == (0, 0, 80, 80)


def test_expand_face_box_shifts_instead_of_shrinking_at_bottom_right():
    """Mirror of the top-left case, against the opposite corner."""
    result = expand_face_box((180.0, 180.0, 20.0, 20.0), (200, 200), 4.0)
    assert result == (120, 120, 200, 200)


def test_expand_face_box_clamps_to_frame_size_when_scale_would_exceed_it():
    """A requested crop larger than the frame itself falls back to the
    frame's own size — a face filling almost the whole image with a large
    scale factor must not ask for pixels outside it."""
    result = expand_face_box((20.0, 20.0, 10.0, 10.0), (50, 50), 10.0)
    assert result == (0, 0, 50, 50)


def test_expand_face_box_zero_size_box_is_degenerate():
    """A zero-width/height box (never produced by YuNet in practice, but
    the docstring calls this case out explicitly) must come back as a
    zero-area result the caller can detect — not a crash, and not a
    silently valid-looking crop `_is_real_face` would then feed the model.
    """
    left, top, right, bottom = expand_face_box((50.0, 50.0, 0.0, 0.0), (200, 200), 4.0)
    assert right <= left or bottom <= top

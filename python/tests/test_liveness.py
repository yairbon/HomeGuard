"""The motion-based liveness heuristic in face_recognition_service.py — the
standalone decision of whether a short window of facial-landmark positions
looks like a live face rather than a rigidly held printed photo or
phone-screen photo.

Deliberately tests only `is_live_landmark_sequence` here, not the
`_DnnBackend` plumbing around it (the rolling per-face history, pruning by
LIVENESS_WINDOW, wiring into recognize()). That plumbing needs a face YuNet
can actually detect, and this codebase already avoids building one where it
can (see test_faces_and_api.py's blank-image tests, which exist precisely
because a *detectable* face image is hard to construct by hand). This
function takes plain coordinate data instead, which is the point of having
factored it out on its own: the liveness *decision* is fully testable
without a camera, a model, or even OpenCV.
"""

from __future__ import annotations

from backend.face_recognition_service import (
    LIVENESS_JITTER_THRESHOLD,
    LIVENESS_MIN_SAMPLES,
    is_live_landmark_sequence,
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

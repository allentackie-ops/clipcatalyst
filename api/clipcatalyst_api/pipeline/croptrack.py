"""Crop-track builder: turns noisy face detections into a calm camera move.

Direct port of ``lib/studio/croptrack.ts``. Detection is per-engine (face-api in
the browser, OpenCV here) but the *motion* must be identical in both engines, so
all of the judgement lives in this one algorithm: pick a subject, hold through
dropouts, smooth, refuse to chase jitter, and cap how fast the frame may travel.

Units: every horizontal value is normalized to the SOURCE width (0..1), so the
same track drives a 540x960 canvas and a 1080x1920 ffmpeg render.

Pure, deterministic, stdlib only -- no I/O, no clock, no randomness. The
TypeScript module is frozen; this file follows it line for line, including the
JavaScript rounding and sort semantics (see ``_js_round``). Keep the two in step:
``api/tests/test_croptrack.py`` cross-checks them by running both.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FaceSample:
    """One detected face in one sampled frame."""

    t: float
    """Seconds from clip start."""
    cx: float
    """Face-center x, normalized 0..1 of source width."""
    size: float
    """Face width, normalized 0..1 of source width. Used to pick the subject."""
    score: float = 1.0
    """Detector confidence 0..1. OpenCV Haar has none -- pass 1."""


@dataclass(frozen=True)
class CropKeyframe:
    t: float
    cx: float


@dataclass(frozen=True)
class CropTrack:
    keyframes: list[CropKeyframe] = field(default_factory=list)
    """Ordered, deduplicated. Always has at least one entry."""
    is_static: bool = True
    """True when the subject never moved enough to warrant panning."""
    coverage: float = 0.0
    """Fraction of samples where a face was actually found (0..1)."""


@dataclass(frozen=True)
class CropTrackOptions:
    duration: float
    """Clip length in seconds."""
    target_aspect: float
    """Output aspect (9/16 -> 0.5625). Sets how wide the crop window is."""
    source_aspect: float
    """Source frame aspect (width/height), e.g. 16/9 -> 1.7778."""


# --- Tunables. Changing these changes how the camera "feels". ---------------
# Mirrored verbatim from croptrack.ts -- do not re-tune on one side only.

MIN_SCORE = 0.45
"""Detections below this confidence are ignored entirely."""
DEADBAND = 0.018
"""Ignore target moves smaller than this (fraction of width) -- anti-jitter."""
MAX_PAN_PER_SEC = 0.14
"""Ceiling on camera speed (fraction of width per second) -- glide, not snap."""
SMOOTH_TAU = 0.55
"""Exponential smoothing time constant, seconds. Higher = lazier camera."""
HOLD_S = 1.2
"""Keep the last known position this long after losing the face."""
RECENTER_TAU = 2.0
"""After HOLD_S with no face, ease back to center over this long."""
SUBJECT_SWITCH_RATIO = 1.45
"""A new face must be this much larger to steal focus from the current one."""
SUBJECT_SWITCH_HOLD_S = 0.8
"""...and must stay larger for this long, so two speakers don't ping-pong."""
STATIC_RANGE = 0.035
"""Total horizontal travel below this => treat the shot as static."""
MAX_KEYFRAMES = 48
"""Upper bound on emitted keyframes (ffmpeg expressions get unwieldy)."""
KEYFRAME_EPSILON = 0.004
"""Drop a keyframe whose value differs from the interpolation by less."""


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _js_round(x: float) -> float:
    """``Math.round`` semantics: nearest integer, ties toward +infinity.

    Python's ``round`` is banker's rounding (0.5 -> 0, 1.5 -> 2), which would
    make the two implementations disagree on exactly-representable halves such
    as ``0.5``, ``2.5`` and ``t * 1000`` grid points. ``floor(x + 0.5)`` is the
    usual shortcut but breaks on ``0.49999999999999994`` (adding 0.5 rounds up
    to 1.0), so compare the fraction explicitly instead.
    """
    if not math.isfinite(x):
        return x
    f = math.floor(x)
    frac = x - f
    if frac < 0.5:
        return float(f)
    return float(f + 1)


def _sign(v: float) -> float:
    if v > 0:
        return 1.0
    if v < 0:
        return -1.0
    return 0.0


def crop_half_width(target_aspect: float, source_aspect: float) -> float:
    """Half-width of the crop window, normalized to source width.

    A 9:16 window cut from a 16:9 frame keeps the full height, so its width is
    (target_aspect / source_aspect) of the source. Clamped to 1 for sources that
    are already portrait (nothing to crop -- the window is the whole frame).
    """
    if not (source_aspect > 0) or not (target_aspect > 0):
        return 0.5
    return min(1.0, target_aspect / source_aspect) / 2


def _group_by_time(samples: Iterable[FaceSample]) -> dict[float, list[FaceSample]]:
    """Group samples that share a timestamp (multi-face frames)."""
    by_time: dict[float, list[FaceSample]] = {}
    for s in samples:
        if not math.isfinite(s.t) or not math.isfinite(s.cx):
            continue
        if s.score < MIN_SCORE:
            continue
        key = _js_round(s.t * 1000) / 1000
        bucket = by_time.get(key)
        if bucket is not None:
            bucket.append(s)
        else:
            by_time[key] = [s]
    return by_time


def _pick_subjects(by_time: Mapping[float, list[FaceSample]]) -> list[FaceSample]:
    """Choose one face per frame, sticky across frames.

    Without hysteresis a two-person podcast flips the crop between speakers on
    whichever face the detector liked best that frame. We keep the incumbent
    unless a rival is clearly bigger for a sustained stretch.
    """
    times = sorted(by_time.keys())
    chosen: list[FaceSample] = []
    current: FaceSample | None = None
    rival_since: float | None = None

    for t in times:
        faces = by_time[t]
        anchor = current
        # Nearest to the incumbent wins ties; size breaks the rest. Python's
        # sorted() is stable, like Array.prototype.sort, so ties keep input
        # order in both implementations.
        if anchor is not None:
            by_nearest = sorted(faces, key=lambda f: abs(f.cx - anchor.cx))
        else:
            by_nearest = sorted(faces, key=lambda f: -f.size)
        incumbent = by_nearest[0]
        largest = sorted(faces, key=lambda f: -f.size)[0]

        if current is None:
            current = largest
            rival_since = None
        elif largest is not incumbent and largest.size > incumbent.size * SUBJECT_SWITCH_RATIO:
            # A bigger face is on screen -- start (or continue) its audition.
            if rival_since is None:
                rival_since = t
            if t - rival_since >= SUBJECT_SWITCH_HOLD_S:
                current = largest
                rival_since = None
            else:
                current = incumbent
        else:
            rival_since = None
            current = incumbent
        picked = current
        chosen.append(FaceSample(t=t, cx=picked.cx, size=picked.size, score=picked.score))
    return chosen


def _simulate(subjects: list[FaceSample], duration: float, half: float) -> list[CropKeyframe]:
    """Walk a fixed timeline, easing the camera toward the subject.

    Three behaviours stack: a deadband so micro-movement never moves the frame,
    exponential smoothing so motion is gradual, and a hard velocity cap so the
    camera can never lurch. When the face is lost we hold, then drift to center.
    """
    center = 0.5
    lo = half
    hi = 1 - half
    # Sources narrower than the target window can't pan at all.
    if hi <= lo:
        return [CropKeyframe(t=0.0, cx=center)]

    step = 1 / 15  # simulate at 15 Hz; keyframes are thinned afterwards
    frames: list[CropKeyframe] = []

    cam = _clamp(subjects[0].cx, lo, hi) if subjects else center
    target = cam
    idx = 0
    last_seen = subjects[0].t if subjects else -math.inf

    t = 0.0
    while t <= duration + 1e-9:
        # Advance to the most recent detection at or before t.
        while idx < len(subjects) and subjects[idx].t <= t:
            s = subjects[idx]
            desired = _clamp(s.cx, lo, hi)
            # Deadband: only re-aim when the subject has genuinely moved.
            if abs(desired - target) > DEADBAND:
                target = desired
            last_seen = s.t
            idx += 1

        gap = t - last_seen
        aim = target
        tau = SMOOTH_TAU
        if gap > HOLD_S:
            # Face lost for a while: give up gracefully rather than freeze forever.
            aim = center
            tau = RECENTER_TAU

        alpha = 1 - math.exp(-step / tau)
        nxt = cam + (aim - cam) * alpha

        # Velocity ceiling -- the one rule that keeps this from looking robotic.
        max_delta = MAX_PAN_PER_SEC * step
        delta = nxt - cam
        if abs(delta) > max_delta:
            nxt = cam + _sign(delta) * max_delta

        cam = _clamp(nxt, lo, hi)
        # Round first, then re-clamp: rounding alone can land a hair outside the
        # legal range, which would hand the renderer a negative crop offset.
        frames.append(
            CropKeyframe(
                t=_js_round(t * 1000) / 1000,
                cx=_clamp(_js_round(cam * 10000) / 10000, lo, hi),
            )
        )
        t += step
    return frames


def _thin(frames: list[CropKeyframe], epsilon: float) -> list[CropKeyframe]:
    """Thin a dense frame list to keyframes a renderer can interpolate.

    Ramer-Douglas-Peucker against linear interpolation: drop any point the
    renderer would have reconstructed anyway, then enforce a hard cap.
    """
    if len(frames) <= 2:
        return frames
    keep = [False] * len(frames)
    keep[0] = True
    keep[len(frames) - 1] = True

    stack: list[tuple[int, int]] = [(0, len(frames) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        fa = frames[a]
        fb = frames[b]
        span = (fb.t - fa.t) or 1
        worst = -1.0
        worst_idx = -1
        for i in range(a + 1, b):
            lerped = fa.cx + ((frames[i].t - fa.t) / span) * (fb.cx - fa.cx)
            err = abs(frames[i].cx - lerped)
            if err > worst:
                worst = err
                worst_idx = i
        if worst > epsilon and worst_idx > 0:
            keep[worst_idx] = True
            stack.append((a, worst_idx))
            stack.append((worst_idx, b))
    return [f for i, f in enumerate(frames) if keep[i]]


def build_crop_track(
    samples: Sequence[FaceSample] | Iterable[FaceSample],
    options: CropTrackOptions | Mapping[str, float],
) -> CropTrack:
    """Build the camera move for one clip. Safe on empty input (returns center)."""
    if isinstance(options, Mapping):
        options = CropTrackOptions(**options)
    duration = max(0.1, options.duration)
    half = crop_half_width(options.target_aspect, options.source_aspect)

    by_time = _group_by_time(samples)
    subjects = _pick_subjects(by_time)
    expected_frames = max(1.0, _js_round(duration * 2))
    coverage = min(1.0, len(subjects) / expected_frames)

    if not subjects:
        return CropTrack(keyframes=[CropKeyframe(t=0.0, cx=0.5)], is_static=True, coverage=0.0)

    frames = _simulate(subjects, duration, half)
    values = [f.cx for f in frames]
    rng = max(values) - min(values)

    if rng < STATIC_RANGE:
        # Barely moved: one fixed, well-placed crop beats a drifting one.
        mean = sum(values) / len(values)
        return CropTrack(
            keyframes=[CropKeyframe(t=0.0, cx=_js_round(mean * 10000) / 10000)],
            is_static=True,
            coverage=coverage,
        )

    keyframes = _thin(frames, KEYFRAME_EPSILON)
    # Enforce the cap by relaxing the tolerance until it fits.
    epsilon = KEYFRAME_EPSILON
    while len(keyframes) > MAX_KEYFRAMES and epsilon < 0.25:
        epsilon *= 1.6
        keyframes = _thin(frames, epsilon)
    if len(keyframes) > MAX_KEYFRAMES:
        stride = math.ceil(len(keyframes) / MAX_KEYFRAMES)
        last = len(keyframes) - 1
        keyframes = [k for i, k in enumerate(keyframes) if i % stride == 0 or i == last]

    return CropTrack(keyframes=keyframes, is_static=False, coverage=coverage)


def crop_center_at(track: CropTrack, t: float) -> float:
    """Camera x at time t (linear between keyframes, clamped at the ends)."""
    ks = track.keyframes
    if not ks:
        return 0.5
    if len(ks) == 1 or t <= ks[0].t:
        return ks[0].cx
    last = ks[len(ks) - 1]
    if t >= last.t:
        return last.cx
    lo = 0
    hi = len(ks) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ks[mid].t <= t:
            lo = mid
        else:
            hi = mid
    a = ks[lo]
    b = ks[hi]
    span = b.t - a.t
    if span <= 0:
        return b.cx
    return a.cx + ((t - a.t) / span) * (b.cx - a.cx)

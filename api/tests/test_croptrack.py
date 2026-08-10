"""Behavioural tests for the shared crop-track math.

The first twenty-one assertions mirror, one for one, the TypeScript suite that
guards ``lib/studio/croptrack.ts`` (the reference implementation). The last two
tests are the ones that keep the port honest over time: they compile the
TypeScript, run both implementations over an identical sample set, and demand
the crop centers agree. If they ever fail, the Python side is the side that
moves.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clipcatalyst_api.pipeline.croptrack import (
    CropTrackOptions,
    FaceSample,
    build_crop_track,
    crop_center_at,
    crop_half_width,
)

OPTS = CropTrackOptions(duration=10, target_aspect=9 / 16, source_aspect=16 / 9)
HALF = crop_half_width(9 / 16, 16 / 9)
LO = HALF
HI = 1 - HALF

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_SOURCE = REPO_ROOT / "lib" / "studio" / "croptrack.ts"


def _opts(duration: float) -> CropTrackOptions:
    """``{ ...OPTS, duration }`` -- same aspects, different clip length."""
    return CropTrackOptions(duration=duration, target_aspect=9 / 16, source_aspect=16 / 9)


def _steps(stop: float, step: float, start: float = 0.0) -> list[float]:
    """Reproduce ``for (let t = start; t <= stop; t += step)`` accumulation exactly.

    The JS suite accumulates its loop variable, so 0.05-second steps drift the
    same way in both languages. Using a multiply-by-index range here would feed
    the two implementations subtly different timestamps.
    """
    out: list[float] = []
    t = start
    while t <= stop:
        out.append(t)
        t += step
    return out


def _steps_lt(stop: float, step: float, start: float = 0.0) -> list[float]:
    """Same, for the exclusive ``t < stop`` loops."""
    out: list[float] = []
    t = start
    while t < stop:
        out.append(t)
        t += step
    return out


def _travel(tr, stop: float, step: float, start: float = 0.0) -> float:
    """Total width of the camera's excursion over a time window."""
    centers = [crop_center_at(tr, t) for t in _steps(stop, step, start)]
    return max(centers) - min(centers)


def _max_speed(tr, stop: float, step: float, start: float = 0.0) -> float:
    """Fastest the camera moves over a window, in fraction of width per second."""
    return max(
        abs(crop_center_at(tr, t + step) - crop_center_at(tr, t)) / step
        for t in _steps_lt(stop, step, start)
    )


# --- 1. geometry -----------------------------------------------------------


def test_crop_half_width_for_9x16_from_16x9() -> None:
    # (0.5625 / 1.7778) / 2 = 0.1582
    assert abs(HALF - 0.1582) < 0.001, f"got {HALF:.4f}"


# --- 2. no detections at all -> centered, static, zero coverage ------------


def test_empty_input_is_centered_and_static() -> None:
    tr = build_crop_track([], OPTS)
    assert tr.is_static
    assert len(tr.keyframes) == 1
    assert tr.keyframes[0].cx == 0.5
    assert tr.coverage == 0


# --- 3. perfectly still subject -> static single keyframe near the subject --


def test_still_subject_is_static() -> None:
    s = [FaceSample(t=t, cx=0.35, size=0.2, score=0.9) for t in _steps(10, 0.5)]
    tr = build_crop_track(s, OPTS)
    assert tr.is_static
    assert len(tr.keyframes) == 1


def test_still_subject_parks_near_subject() -> None:
    s = [FaceSample(t=t, cx=0.35, size=0.2, score=0.9) for t in _steps(10, 0.5)]
    tr = build_crop_track(s, OPTS)
    assert abs(tr.keyframes[0].cx - 0.35) < 0.03, f"got {tr.keyframes[0].cx}"


# --- 4. jitter within the deadband must NOT produce a moving camera --------


def test_sub_deadband_jitter_stays_static() -> None:
    s = [
        FaceSample(t=t, cx=0.5 + (0.008 if t % 1 == 0 else -0.008), size=0.2, score=0.9)
        for t in _steps(10, 0.5)
    ]
    tr = build_crop_track(s, OPTS)
    assert tr.is_static, f"keyframes={len(tr.keyframes)}"


# --- 5. subject walks left -> right ---------------------------------------


def _walking_subject() -> list[FaceSample]:
    return [
        FaceSample(t=t, cx=0.2 + (t / 10) * 0.6, size=0.2, score=0.9) for t in _steps(10, 0.5)
    ]


def test_moving_subject_is_not_static() -> None:
    tr = build_crop_track(_walking_subject(), OPTS)
    assert not tr.is_static


def test_camera_travels_rightward() -> None:
    tr = build_crop_track(_walking_subject(), OPTS)
    start = crop_center_at(tr, 0)
    end = crop_center_at(tr, 10)
    assert end > start + 0.1, f"{start:.3f} -> {end:.3f}"


def test_respects_pan_speed_ceiling() -> None:
    tr = build_crop_track(_walking_subject(), OPTS)
    max_v = _max_speed(tr, 10, 0.1)
    assert max_v <= 0.145, f"maxV={max_v:.4f}"


# --- 6. the crop window must never leave the frame ------------------------


def test_crop_window_stays_inside_the_frame() -> None:
    s = [
        FaceSample(t=t, cx=0.02 if t < 5 else 0.98, size=0.2, score=0.9)
        for t in _steps(10, 0.5)
    ]
    tr = build_crop_track(s, OPTS)
    in_bounds = True
    for t in _steps(10, 0.05):
        cx = crop_center_at(tr, t)
        if cx < LO - 1e-6 or cx > HI + 1e-6:
            in_bounds = False
            break
    assert in_bounds, f"bounds {LO:.3f}..{HI:.3f}"


# --- 7. sparse coverage: too little signal to pan -------------------------
# Hold the framing we actually saw rather than inventing a drift.


def _sparse_coverage_track():
    s = [FaceSample(t=t, cx=0.75, size=0.2, score=0.9) for t in _steps(3, 0.5)]
    return build_crop_track(s, _opts(12))


def test_sparse_coverage_gives_a_locked_frame() -> None:
    tr = _sparse_coverage_track()
    assert tr.is_static, f"keyframes={len(tr.keyframes)} coverage={tr.coverage:.2f}"


def test_locked_frame_sits_on_the_subject() -> None:
    tr = _sparse_coverage_track()
    got = crop_center_at(tr, 6)
    assert abs(got - 0.75) < 0.02, f"got {got:.3f}"


# --- 7b. REGRESSION: a motionless subject with an ordinary detector dropout
#         must not produce visible movement. This shipped at 47% of the frame.


def test_dropout_on_a_still_subject_shows_no_visible_drift() -> None:
    s = [
        FaceSample(t=t, cx=0.25, size=0.2, score=0.9)
        for t in _steps(30, 0.5)
        # 3s dropout, entirely routine on real footage
        if not (t > 10 and t < 13)
    ]
    tr = build_crop_track(s, _opts(30))
    travel = _travel(tr, 30, 0.1)
    assert travel < 0.02, f"travel={travel:.4f} of width"


# --- 7c. a genuinely long absence still releases toward center -- but gently.


def test_recenter_drift_is_slower_than_a_real_pan() -> None:
    s = [FaceSample(t=t, cx=0.75, size=0.2, score=0.9) for t in _steps(14, 0.5)]
    s += [FaceSample(t=t, cx=0.75, size=0.2, score=0.9) for t in _steps(30, 0.5, start=20)]
    tr = build_crop_track(s, _opts(30))
    max_v = _max_speed(tr, 20, 0.1, start=14)
    assert max_v <= 0.036, f"maxV={max_v:.4f} (cap 0.035)"


# --- 7d. a stray single detection must never define the clip's framing -----


def test_one_stray_detection_is_ignored_entirely() -> None:
    tr = build_crop_track([FaceSample(t=20, cx=0.85, size=0.09, score=0.95)], _opts(30))
    got = crop_center_at(tr, 0)
    assert tr.is_static
    assert abs(got - 0.5) < 1e-6, f"cx={got:.4f}"


# --- 7e. hard cut: the new speaker must be in frame almost immediately -----
#         not after a multi-second glide.


def test_cut_puts_subject_in_frame_within_about_1_5s() -> None:
    s = [FaceSample(t=t, cx=0.22, size=0.2, score=0.9) for t in _steps(8, 0.5)]
    s += [FaceSample(t=t, cx=0.78, size=0.2, score=0.9) for t in _steps(20, 0.5, start=8.5)]
    tr = build_crop_track(s, _opts(20))
    # Allow the 2-sample confirmation window, then the subject must be in frame.
    cam = crop_center_at(tr, 10.0)
    assert abs(cam - 0.78) <= HALF, f"cam={cam:.3f} needs |cam-0.78|<={HALF:.3f}"


# --- 7f. ...but a single outlier detection must NOT trigger a cut-snap -----


def test_single_outlier_does_not_fling_the_frame() -> None:
    s = [
        FaceSample(t=t, cx=0.9 if abs(t - 10) < 0.01 else 0.25, size=0.2, score=0.9)
        for t in _steps(20, 0.5)
    ]
    tr = build_crop_track(s, _opts(20))
    max_dev = max(abs(crop_center_at(tr, t) - 0.25) for t in _steps(20, 0.1))
    assert max_dev < 0.05, f"maxDev={max_dev:.4f}"


# --- 8. two faces: detector flip-flopping must not make the camera pan -----


def test_two_similar_faces_do_not_ping_pong() -> None:
    s: list[FaceSample] = []
    for t in _steps(10, 0.5):
        # steady speaker
        s.append(FaceSample(t=t, cx=0.3, size=0.20, score=0.9))
        # rival, similar size
        s.append(FaceSample(t=t, cx=0.7, size=0.21 + math.sin(t * 7) * 0.02, score=0.9))
    tr = build_crop_track(s, OPTS)
    max_swing = 0.0
    for t in _steps_lt(10, 0.25):
        max_swing = max(max_swing, abs(crop_center_at(tr, t + 0.25) - crop_center_at(tr, t)))
    assert max_swing < 0.05, f"maxSwing={max_swing:.4f}"


# --- 9. low-confidence detections are ignored -----------------------------


def test_low_confidence_samples_ignored() -> None:
    s = [FaceSample(t=t, cx=0.9, size=0.2, score=0.1) for t in _steps(10, 0.5)]
    tr = build_crop_track(s, OPTS)
    assert tr.coverage == 0
    assert tr.keyframes[0].cx == 0.5


# --- 10. keyframe cap under pathological input ----------------------------


def _thrashing_track():
    s = [
        FaceSample(t=t, cx=0.5 + 0.35 * math.sin(t * 2.3), size=0.2, score=0.9)
        for t in _steps(60, 0.2)
    ]
    return build_crop_track(s, _opts(60))


def test_keyframe_count_capped() -> None:
    tr = _thrashing_track()
    assert len(tr.keyframes) <= 48, f"got {len(tr.keyframes)}"


def test_keyframes_strictly_ordered() -> None:
    ks = _thrashing_track().keyframes
    assert all(k.t > ks[i - 1].t for i, k in enumerate(ks) if i > 0)


# --- 11. portrait source (nothing to crop) must not blow up ---------------


def test_portrait_source_is_centered_without_crashing() -> None:
    tr = build_crop_track(
        [FaceSample(t=0, cx=0.5, size=0.3, score=0.9)],
        CropTrackOptions(duration=5, target_aspect=9 / 16, source_aspect=9 / 16),
    )
    assert len(tr.keyframes) >= 1
    assert abs(tr.keyframes[0].cx - 0.5) < 1e-6


# --- cross-implementation check -------------------------------------------

_DRIVER = """
import { buildCropTrack, cropCenterAt } from "./croptrack.mjs";
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
const track = buildCropTrack(input.samples, input.options);
process.stdout.write(JSON.stringify({
  isStatic: track.isStatic,
  coverage: track.coverage,
  keyframes: track.keyframes,
  centers: input.times.map((t) => cropCenterAt(track, t)),
}));
"""


@pytest.fixture(scope="session")
def js_module(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the reference TypeScript to an importable ES module, or skip."""
    tmp_path = tmp_path_factory.mktemp("croptrack-ts")
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available; cannot cross-check against croptrack.ts")
    target = tmp_path / "croptrack.mjs"

    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    if tsc.exists() and TS_SOURCE.exists():
        proc = subprocess.run(
            [
                str(tsc),
                str(TS_SOURCE),
                "--outDir",
                str(tmp_path),
                "--module",
                "es2020",
                "--target",
                "es2020",
                "--skipLibCheck",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        emitted = tmp_path / "croptrack.js"
        if not emitted.exists():
            pytest.skip(f"tsc did not emit croptrack.js: {proc.stdout or proc.stderr}")
        target.write_text(emitted.read_text(encoding="utf8"), encoding="utf8")
        _write_driver(tmp_path)
        return target

    prebuilt = os.environ.get("CROPTRACK_JS")
    if prebuilt and Path(prebuilt).exists():
        target.write_text(Path(prebuilt).read_text(encoding="utf8"), encoding="utf8")
        _write_driver(tmp_path)
        return target
    pytest.skip("no TypeScript compiler and no prebuilt croptrack.js to compare against")


def _write_driver(tmp_path: Path) -> None:
    (tmp_path / "driver.mjs").write_text(_DRIVER, encoding="utf8")


def _run_ts(module: Path, samples: list[FaceSample], opts: CropTrackOptions, times: list[float]):
    """Run the TypeScript build over the same input and return its track."""
    payload = {
        "samples": [{"t": s.t, "cx": s.cx, "size": s.size, "score": s.score} for s in samples],
        "options": {
            "duration": opts.duration,
            "targetAspect": opts.target_aspect,
            "sourceAspect": opts.source_aspect,
        },
        "times": times,
    }
    src = module.parent / "input.json"
    src.write_text(json.dumps(payload), encoding="utf8")
    proc = subprocess.run(
        [shutil.which("node") or "node", str(module.parent / "driver.mjs"), str(src)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(module.parent),
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _moving_subject_samples() -> list[FaceSample]:
    """A deliberately awkward pan: two faces, a wobble, and a dropout.

    The dropout in [8, 9.6) exercises hold-then-recenter, the rival face
    exercises the subject hysteresis, and the wobble keeps the deadband and the
    velocity cap in play -- so the comparison covers the whole pipeline, not
    just a straight line.
    """
    samples: list[FaceSample] = []
    for t in _steps(12, 0.5):
        if 8.0 <= t < 9.6:
            continue  # face lost
        cx = 0.18 + (t / 12) * 0.64 + 0.02 * math.sin(t * 3.1)
        samples.append(FaceSample(t=t, cx=cx, size=0.22, score=0.9))
        if 2.0 <= t <= 7.0:
            samples.append(FaceSample(t=t, cx=0.25, size=0.17, score=0.8))
    return samples


def test_matches_typescript_implementation(js_module: Path) -> None:
    samples = _moving_subject_samples()
    duration = 12.0
    opts = CropTrackOptions(duration=duration, target_aspect=9 / 16, source_aspect=16 / 9)
    times = [i * duration / 19 for i in range(20)]

    ts = _run_ts(js_module, samples, opts, times)
    py = build_crop_track(samples, opts)

    # A moving-subject case is only a real test if it actually moves.
    assert not py.is_static
    assert py.is_static == ts["isStatic"]
    assert abs(py.coverage - ts["coverage"]) < 1e-9

    py_centers = [crop_center_at(py, t) for t in times]
    for t, mine, theirs in zip(times, py_centers, ts["centers"]):
        assert abs(mine - theirs) < 1e-3, f"t={t:.3f}: python={mine!r} ts={theirs!r}"
    assert max(py_centers) - min(py_centers) > 0.1, "sample set should pan meaningfully"

    # Same keyframes, not merely the same curve: a drift in the thinning would
    # show up here long before it grew big enough to move a crop center.
    assert len(py.keyframes) == len(ts["keyframes"])
    for a, b in zip(py.keyframes, ts["keyframes"]):
        assert abs(a.t - b["t"]) < 1e-9
        assert abs(a.cx - b["cx"]) < 1e-9


def test_matches_typescript_on_a_hard_cut(js_module: Path) -> None:
    """Cross-check the escapes from the smoothing model, not just the smoothing.

    A cut exercises jump confirmation, the instant re-aim and the acceleration
    limiter on the far side of it -- the newest and most order-sensitive part of
    ``simulate``. A port that got the confirmation counter or the overshoot
    guard subtly wrong would still track a straight pan correctly.
    """
    samples = [FaceSample(t=t, cx=0.22, size=0.2, score=0.9) for t in _steps(8, 0.5)]
    samples += [
        FaceSample(t=t, cx=0.78, size=0.2, score=0.9) for t in _steps(20, 0.5, start=8.5)
    ]
    opts = CropTrackOptions(duration=20, target_aspect=9 / 16, source_aspect=16 / 9)
    times = [i * 0.25 for i in range(81)]

    ts = _run_ts(js_module, samples, opts, times)
    py = build_crop_track(samples, opts)

    assert py.is_static == ts["isStatic"]
    for t, mine, theirs in zip(times, (crop_center_at(py, t) for t in times), ts["centers"]):
        assert abs(mine - theirs) < 1e-9, f"t={t:.3f}: python={mine!r} ts={theirs!r}"
    assert len(py.keyframes) == len(ts["keyframes"])
    for a, b in zip(py.keyframes, ts["keyframes"]):
        assert abs(a.t - b["t"]) < 1e-9
        assert abs(a.cx - b["cx"]) < 1e-9


def test_matches_typescript_when_the_subject_is_lost_near_the_end(
    js_module: Path,
) -> None:
    """Cross-check the tail branch, which once diverged silently.

    When the hold expires inside the final ``TAIL_HOLD_S`` the camera freezes
    where it stands rather than recentering *or* re-aiming at the stale target.
    Both engines gained that branch at different moments, and every existing
    cross-check kept passing while they disagreed: the straight pan and the hard
    cut both re-acquire the subject, so neither ever reaches this code. A clip
    whose subject simply stops being detected before the end does.
    """
    samples = [
        FaceSample(t=t, cx=0.20 + (t / 20) * 0.12, size=0.2, score=0.9)
        for t in _steps(15, 0.5)
    ]
    opts = CropTrackOptions(duration=20, target_aspect=9 / 16, source_aspect=16 / 9)
    times = [i * 0.25 for i in range(81)]

    ts = _run_ts(js_module, samples, opts, times)
    py = build_crop_track(samples, opts)

    assert py.is_static == ts["isStatic"]
    for t, theirs in zip(times, ts["centers"]):
        assert abs(crop_center_at(py, t) - theirs) < 1e-9, f"t={t:.3f}"

    # And the behaviour itself: the last second must hold still. Not bitwise
    # frozen -- keyframe thinning leaves a residual of order 1e-4 -- but far
    # below anything an eye could catch (5e-4 of width is ~1px at 1920).
    tail = [crop_center_at(py, 19.0 + i * 0.05) for i in range(21)]
    assert max(tail) - min(tail) < 5e-4, f"camera moved in the final second: {tail}"


def test_matches_typescript_rounding_of_coverage(js_module: Path) -> None:
    """Pin ``Math.round``'s ties-to-+infinity rule, which coverage depends on.

    ``expectedFrames = Math.round(duration * 2)``, so a duration ending in .25
    or .75 lands the argument exactly on a half. Python's built-in ``round`` is
    banker's rounding and would answer 20 where JavaScript answers 21 -- a
    silent 4% coverage drift that no amount of moving-subject sampling exposes.
    """
    opts = CropTrackOptions(duration=10.25, target_aspect=9 / 16, source_aspect=16 / 9)
    samples = [FaceSample(t=i * 0.25, cx=0.4, size=0.2, score=0.9) for i in range(15)]

    ts = _run_ts(js_module, samples, opts, [0.0])
    py = build_crop_track(samples, opts)

    assert py.coverage == pytest.approx(15 / 21)  # not 15/20
    assert py.coverage == pytest.approx(ts["coverage"], abs=1e-12)

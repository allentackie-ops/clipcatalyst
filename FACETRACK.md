# Face tracking — build spec

Today both renderers center-crop to 9:16. If the speaker sits off-center they
drift out of frame, which is the difference between a clip someone posts and a
clip someone re-edits. This adds real subject tracking to **both** engines.

## Architecture — detection is per-engine, motion is shared

```
frames ──▶ detect faces ──▶ FaceSample[] ──▶ buildCropTrack() ──▶ CropTrack
                                              (SHARED, DONE)         │
                                                                     ▼
                                    renderer reads cropCenterAt(t) per frame
```

`lib/studio/croptrack.ts` is **written, tested (16 cases), and frozen** — read
it first; it is the contract. It owns every judgement call: subject choice with
hysteresis (so a two-person podcast doesn't ping-pong), a deadband (micro-
movement never moves the frame), exponential smoothing, a hard pan-speed
ceiling, hold-then-recenter when the face is lost, a static-shot fast path, and
keyframe thinning with a hard cap of 48.

**Do not re-tune the constants or reimplement the motion logic.** Detection
feeds it `FaceSample[]`; renderers consume `cropCenterAt(track, t)`.

Normalization contract, everywhere: horizontal values are fractions of the
**source width** (0..1). `cx` is the face center, `size` the face width.

## Detector choice (already validated — do not substitute)

| Engine | Detector | Why |
|---|---|---|
| Browser | `@vladmandic/face-api` tiny_face_detector | Ships its own weights (189 KB) — no CDN, works with the static export and CSP |
| Cloud | OpenCV `haarcascade_frontalface_default.xml` | Bundled inside opencv-python-headless **4.x** — zero downloads |

Verified in this sandbox: the npm package really does contain the weights, and
Haar really does fire on a synthetic face (see the test recipe below).
opencv-python-headless **must be pinned `<5`** — 5.x dropped the bundled
cascades.

## Modules

### 1. `api/clipcatalyst_api/pipeline/croptrack.py` — port of croptrack.ts
Direct, faithful port. Same constants, same algorithm, same results. Public:
`build_crop_track(samples, options) -> CropTrack`, `crop_center_at(track, t)`,
`crop_half_width(target_aspect, source_aspect)`. Dataclasses `FaceSample`,
`CropKeyframe`, `CropTrack` (snake_case fields: `is_static`, `coverage`).
Mirror all 16 TS test cases in `api/tests/test_croptrack.py`, and add a
cross-check: feed both implementations the same samples (run the TS via
`node`, or hard-code expected values captured from it) and assert agreement
within 1e-3 on at least the moving-subject case.

### 2. `api/clipcatalyst_api/pipeline/facetrack.py`
`detect_faces(video_path, start, end, settings, on_progress=None) -> list[FaceSample]`
- Sample ~2 Hz across `[start, end]`. Decode with ffmpeg piping raw frames
  (`-ss start -to end -vf fps=2,scale=320:-1 -f rawvideo -pix_fmt bgr24 -`),
  reading frame-by-frame — never buffer the whole clip.
- Detect per frame with the Haar cascade (`cv2.data.haarcascades`), grayscale +
  `equalizeHist`, `scaleFactor=1.1`, `minNeighbors=4`, `minSize` ≈ 8% of width.
- Emit one `FaceSample` per detected face per frame: `t` relative to clip start,
  `cx`/`size` normalized to source width, `score=1.0` (Haar gives none).
- Never raise for detection problems — a clip with no detectable face must
  return `[]` so the track falls back to center. Only raise if ffmpeg itself
  fails to run.
- `settings.face_tracking` (new, `CC_FACE_TRACKING`, default `"on"`) turns it
  off; return `[]` when off.

### 3. `api/clipcatalyst_api/pipeline/render.py` — time-varying crop
Replace the fixed center crop with the track.
- Static track (`is_static`) → constant `crop=w:h:x:0` with x from the single
  keyframe. Keep today's fast path.
- Moving track → build a **piecewise-linear** ffmpeg expression for `x` over
  `t`, using nested `if(lt(t,..))` with `lerp()` between keyframes. ≤48
  keyframes is guaranteed by the track builder. Escape it correctly for the
  filtergraph (commas inside expressions need care — prefer `\,`).
- x must be clamped to `[0, iw-ow]` in the expression as a final guard.
- **Verify by rendering, not by inspection.** Test recipe: build a source video
  with a hard left/right colour split (e.g. `ffmpeg -f lavfi -i color=red:...`
  hstacked with blue), feed a hand-built track panning 0.2→0.8, render, then
  sample output frames with OpenCV and assert the dominant colour transitions
  red→blue. That proves the pan actually happens in the encoded file.

### 4. `api/clipcatalyst_api/worker.py` — pipeline wiring
Insert a `reframe` stage between `analyze` and `render`: for each plan, call
`detect_faces` then `build_crop_track`, and pass the track into `render_clip`.
Report progress per clip. Detection failure for one clip must degrade to a
centered track, never fail the job.

### 5. `lib/studio/facetrack.ts` — browser detection
`detectFaces(video, plan, onProgress, signal?) -> Promise<FaceSample[]>`
- Lazy-import `@vladmandic/face-api` (`await import(...)`) so it is **not** in
  the initial bundle — it costs ~1.3 MB and must only load when Studio actually
  reframes.
- Load weights from `${basePath}/models` via `nets.tinyFaceDetector.loadFromUri`.
  Load once, module-level memoized.
- Sample ~2 Hz: seek the hidden `<video>`, draw to a small offscreen canvas
  (~320 px wide) and detect on that — detection cost scales with pixels.
- Return `[]` on any failure (model fetch, WebGL, unsupported) — reframing is
  an enhancement, never a reason a clip fails to render. Log once to console.
- Honour `signal` for abort; report progress 0..1.

### 6. `lib/studio/render.ts` — consume the track
`renderClip` gains an optional `track?: CropTrack` in its options. Per drawn
frame, compute source x from `cropCenterAt(track, t)` (fall back to centered
when absent) and use it as the `drawImage` source rectangle. Keep the existing
cover-crop maths for the vertical axis. No other behaviour changes.

### 7. `components/studio/useStudioPipeline.ts` + `StageChecklist.tsx`
`StageId` already includes `"reframe"` (added in `lib/studio/types.ts`).
Between analyze and render, per clip: detect → `buildCropTrack` → pass to
`renderClip`. Report stage `"reframe"` with `clipIndex`/`clipCount`. Add the
row "Reframe on the speaker" to the checklist. Cloud mode is unaffected.

### 8. Assets + config
- Copy `tiny_face_detector_model.bin` and
  `tiny_face_detector_model-weights_manifest.json` from
  `node_modules/@vladmandic/face-api/model/` into `public/models/` and **commit
  them** (they are the reason this works without a CDN).
- Add `@vladmandic/face-api` to `package.json` dependencies.
- `next.config.ts`: when `GITHUB_PAGES=true`, also expose
  `env: { NEXT_PUBLIC_BASE_PATH: "/clipcatalyst" }` so the model URL resolves
  under the Pages sub-path. Default `""`.
- `api/requirements.txt`: `opencv-python-headless<5`.

## Non-negotiables

- The Pages build must still work with no network at runtime beyond the site's
  own origin.
- Face tracking is **best-effort**: any failure degrades to today's center crop.
  A clip must never fail to render because detection had a bad day.
- All existing tests stay green (51 Python, the TS e2e suites, croptrack's 16).

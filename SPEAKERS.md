# Speaker diarization — build spec

The blueprint promises "Multi-Speaker Detection: identifies and color-codes
each speaker." This adds it to both engines: every transcript word gains a
speaker index, and captions highlight the active word in that speaker's color.

## Architecture — embeddings per engine, judgement shared

```
PCM ──▶ segment embeddings (per-engine MFCC stats) ─┐
transcript words ──▶ buildSpeechSegments ───────────┼─▶ assignSpeakers
                                                    │   (SHARED, DONE)
                                                    ▼
                word.speaker → caption colors in both renderers
```

`lib/studio/diarize.ts` is **written, tested (20 cases), mutation-verified,
and frozen**. It owns segmentation, deterministic clustering, the
one-vs-many-speakers decision (separation guard placed empirically at 2.8 in
the measured gap between one-voice clouds and real voice pairs), tiny-speaker
absorption, smoothing, and labeling (speaker 0 = most speech). Do NOT re-tune
it. Engines feed it one embedding per speech segment; renderers read
`word.speaker` and `SPEAKER_COLORS`.

`TranscriptWord.speaker?: number` and StageId `"diarize"` already exist in
lib/studio/types.ts. `SPEAKER_COLORS` (violet, amber, sky, rose) is exported
from diarize.ts — speaker 0 keeps the exact violet used today, so single-voice
clips render byte-identically.

## The embedding recipe (identical DEFINITION in both engines)

Per speech segment (from `buildSpeechSegments`, capped at the first 4 s):
1. Take the segment's mono 16 kHz PCM.
2. Frames of 400 samples (25 ms), hop 320 (20 ms), Hamming window.
3. Magnitude spectrum via 512-point FFT.
4. 24 triangular mel filters spanning 80–7600 Hz; log of filter energies
   (floor 1e-10).
5. DCT-II, keep coefficients c1..c12 (drop c0 — energy is mic distance, not
   identity).
6. Skip near-silent frames (frame RMS < 0.008); if fewer than 5 voiced
   frames remain, return an EMPTY embedding (the shared core treats it as
   unusable and the segment inherits a neighbour).
7. Embedding = [mean(c1..c12), std(c1..c12)] → 24 dims, L2-normalized.

Exact numeric parity across languages is NOT required (numpy FFT vs JS FFT
differ in float error); each engine is validated in-engine against synthetic
voices instead. The CLUSTERING layer is bit-exact and cross-checked.

Synthetic voice recipe for tests (works — validated for the fixtures): voice A
= sawtooth ~110 Hz through a lowpass around 900 Hz; voice B = square ~280 Hz
highpassed around 400 Hz (ffmpeg `aevalsrc`/`sine`+filters). Alternate them in
3–5 s turns with 1 s silences. The embeddings must cluster A-segments with
A-segments (cosine), and a single-voice fixture must yield speakerCount 1.

## Modules

### 1. `api/clipcatalyst_api/pipeline/diarize.py` — port of diarize.ts
Faithful port of the SHARED CORE only (segmentation, clustering, guards,
labeling). Mirror all 20 cases from `scripts/diarize.test.mjs` into
`api/tests/test_diarize.py`, plus a bit-exact cross-check vs the TS (node
subprocess, same pattern as `api/tests/test_croptrack.py::_run_ts`) on fixed
embedding fixtures — clustering decisions must agree exactly.

### 2. `api/clipcatalyst_api/pipeline/speaker_embed.py` — cloud embeddings
`segment_embeddings(path, segments, settings) -> list[list[float]]` following
the recipe above. PCM via one streaming ffmpeg pass (f32le mono 16k), numpy
for FFT/mel/DCT (numpy ships with opencv-python-headless — already a dep).
Never raises for detection-quality problems; unusable segments → [].
`CC_DIARIZATION` setting ("on"/"off", default on) — off returns all-empty.
Tests: synthetic two-voice file → A-segments cluster apart from B-segments
(cosine distance across voices > within), and through the shared core →
speakerCount 2 with correct turn labels; single-voice file → speakerCount 1.

### 3. Cloud wiring — `worker.py`, `captions.py`, `models.py`
- worker.py: a `diarize` stage between `transcribe` and `analyze`: build
  segments from transcript words, embed, `assign_speakers`, write
  `word.speaker` onto the transcript words (plans re-base words; speaker
  carries through automatically). Failure → log + all words unassigned, never
  fails the job. Progress detail: "Found N speakers" (or "One speaker").
- captions.py: per-word karaoke highlight color from `SPEAKER_COLORS[speaker
  % 4]` (ASS &HAABBGGRR — convert carefully); unassigned words keep violet.
  One ASS style per speaker or inline override tags — whichever is cleaner.
- models.py stage comment gains `diarize`.

### 4. Browser — `lib/studio/speakerfeats.ts` + wiring
- speakerfeats.ts: `segmentEmbeddings(pcm: Float32Array, segments) ->
  number[][]` — same recipe, hand-rolled radix-2 FFT (512-point, real input),
  runs on the main thread in chunks with macrotask yields (a 20-min video is
  ~60k frames ≈ well under a second of math; still yield every ~200 frames).
  MUST run before the PCM buffer is transferred to the whisper worker.
- useStudioPipeline.ts: compute embeddings BEFORE transcription transfers the
  buffer (stash them), then after the transcript arrives: buildSpeechSegments
  → assignSpeakers → stamp word.speaker. Emit stage "diarize" ("Listening for
  speakers"). CAREFUL with ordering: segments come from transcript words,
  which arrive AFTER the buffer is gone — so extract per-FRAME MFCC features
  from the whole PCM up front (before transfer), and build segment embeddings
  from those frames afterwards. Design speakerfeats.ts accordingly:
  `computeMfccFrames(pcm) -> {frames: Float32Array, dims, hopSeconds}` before
  transfer, then `segmentEmbeddingsFromFrames(frames, dims, hopSeconds,
  segments)` after. Include frame RMS as dim 0 for the voiced-frame skip.
- render.ts: active-word highlight color = SPEAKER_COLORS[word.speaker % 4]
  (fallback violet). No other caption changes.
- StageChecklist: row "Identify speakers" between Transcribe and Score
  moments; StudioApp cloud checklist + cloud.ts stage type gain "diarize".
- ClipCard (browser + cloud mapping): when a clip's words span 2+ speakers,
  show a neutral Badge "N speakers".

### 5. Tests must run here
Browser-side: a node test (scripts/speakerfeats.test.mjs, npm script
test:speakerfeats) generating synthetic PCM in JS (sawtooth vs square via
simple loops — no ffmpeg needed) and asserting the same discrimination
properties as the Python side, plus empty/short/silent segment handling.

## Non-negotiables
- Diarization is best-effort: ANY failure degrades to unassigned words →
  violet captions, exactly today's output. A clip must never fail because of
  it.
- Single-voice output must be byte-identical to today (speaker 0 = violet).
- All existing suites stay green (116 Python, 28 croptrack, 20 diarize, both
  e2e suites, three builds).

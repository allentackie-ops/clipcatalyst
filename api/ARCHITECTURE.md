# ClipCatalyst Cloud API — architecture (MVP)

The 90-second pipeline from the blueprint, shaped for a single GPU box or a
small fleet. FastAPI + Celery + Redis, SQLite job store, local-disk or S3
storage. Everything lives under `api/` in the monorepo; the Python package is
`clipcatalyst_api`.

## The pipeline (worker side)

```
upload → probe → transcribe (faster-whisper, word timestamps)
       → plan clips (virality engine — Python port of lib/studio/highlights.ts)
       → render each clip with ffmpeg (9:16 crop/scale, ASS karaoke captions,
         watermark, x264 + aac) → store → job done
```

Target hardware story (documented in DEPLOY.md, never hard-coded):
distil/large-v3 on a T4/A10 transcribes a 60-min episode in ~1–2 min;
`base`/`small` on CPU works for dev. Model + device come from env.

## Job lifecycle

states: `awaiting_upload → queued → processing → done | failed`
processing sub-stages mirror the browser Studio: `probe, transcribe, analyze,
render` with 0..1 progress and a human `detail` string, persisted on the job
row so `GET /jobs/{id}` can stream honest progress.

## HTTP API (all JSON, CORS open to configured origins)

- `POST /v1/jobs` body `{filename, size_bytes, target_length, count, height}`
  → `201 {job_id, upload: {mode: "put", url}}`
  Local mode: url = `/v1/uploads/{job_id}`. S3 mode: presigned PUT url.
- `PUT /v1/uploads/{job_id}` raw body = the video file (local mode only,
  streams to disk, 2 GB cap). → `{ok: true}`
- `POST /v1/jobs/{job_id}/start` → `202 {job_id, status: "queued"}`
  (validates the upload exists, enqueues the Celery task)
- `GET /v1/jobs/{job_id}` →
  `{job_id, status, stage, progress, detail, error, clips: [ClipOut]}`
  `ClipOut = {id, index, score, title, hooks, reason, tip, start, end,
  duration, url, width, height}`
- `GET /v1/files/{job_id}/{name}` — serves rendered clips in local-storage
  mode (S3 mode returns presigned GET urls in `clips[].url` instead).
- `GET /v1/healthz` → `{ok, version, queue: "redis"|"eager", storage,
  transcriber}`

## Modules & contracts

- `settings.py` (DONE — frozen): env-driven config, see file.
- `pipeline/types.py` (DONE — frozen): Word/Transcript/AudioFeatures/ClipPlan
  dataclasses; keep field names aligned with the TS types in
  `lib/studio/types.ts`.
- `pipeline/probe.py`: `probe_media(path) -> MediaInfo{duration, width,
  height, has_audio}` via ffprobe (`settings.ffprobe_bin`).
- `pipeline/transcribe.py`: `get_transcriber(settings) -> Transcriber`;
  `Transcriber.transcribe(path, on_progress) -> Transcript`.
  Implementations: `FasterWhisperTranscriber` (imported lazily so the API
  container never needs torch/ctranslate2 installed) and `FakeTranscriber`
  (`CC_TRANSCRIBER=fake`): reads a transcript JSON from
  `CC_FAKE_TRANSCRIPT_PATH` — powers tests and the sandbox.
  Word timestamps required; merge faster-whisper segments into flat words.
  Also computes `AudioFeatures` (RMS/silences) — extract PCM via ffmpeg
  (`-f f32le -ac 1 -ar 16000 -`) and reuse the same math as the browser
  (port from `lib/studio/audio.ts` computeAudioFeatures).
- `pipeline/highlights.py`: faithful Python port of
  `lib/studio/highlights.ts` — same tunables, same lexicons, same scoring,
  same snapping and title/hooks/reason/tip generation, deterministic.
  `plan_clips(transcript, features, options) -> list[ClipPlan]`.
- `pipeline/captions.py`: `build_ass(plan, style) -> str` — ASS subtitles
  with per-word karaoke highlight (brand violet #A78BFA active word — note
  ASS colors are &HBBGGRR&), grouped ≤4 words / ≤18 chars per event exactly
  like the browser renderer, positioned lower-third for 1080×1920, Inter →
  fallback fonts. Include a small semi-transparent watermark line
  ("⚡ ClipCatalyst") bottom-right when `style.watermark`.
- `pipeline/render.py`: `render_clip(src, plan, out_path, opts, on_progress)`
  — build + run the ffmpeg command: `-ss/-to` input trim, filtergraph
  `crop` to 9:16 center → `scale=1080:1920` (or opts height), `subtitles=`
  the generated .ass (escape the path), x264 `-preset veryfast -crf 21`,
  aac 128k, `+faststart`. Parse `-progress pipe:1` for on_progress. Raise
  RenderError with the tail of stderr on failure.
- `db.py`: SQLite (WAL) at `settings.db_path`; plain sqlite3, tiny DAO:
  `create_job, get_job, update_job(**fields), set_clips(job_id, clips)`.
  Thread/process safe via short-lived connections.
- `storage.py`: `LocalStorage(root)` and `S3Storage(bucket, prefix)`
  behind one interface: `upload_target(job_id) -> UploadTarget`,
  `source_path(job_id) -> Path` (downloads from S3 to tmp when needed),
  `put_clip(job_id, path, name) -> stored key`, `clip_url(job_id, name) ->
  str` (local: `/v1/files/...`; S3: presigned GET). boto3 imported lazily.
- `queue_app.py`: Celery app named `clipcatalyst`, broker/backend from
  `settings.redis_url`; `task_always_eager` when `CC_QUEUE=eager` (tests +
  single-box dev without Redis).
- `worker.py`: the `process_job(job_id)` Celery task — orchestrates the
  pipeline, updates job progress via db, catches everything into
  `failed` + friendly error. Renders clips sequentially, isolates per-clip
  failures like the browser hook does (partial results + failed_count).
- `main.py`: FastAPI app wiring routes to db/storage/queue; CORS from
  `settings.cors_origins`; startup creates dirs/tables.

## Testing (must run in this sandbox — no network beyond PyPI)

- Unit: highlights port (golden expectations mirroring TS behavior),
  captions ASS output, probe/render on a tiny ffmpeg-generated test video
  (`testsrc2` + `sine`, the local static ffmpeg has libx264+aac+libass).
- Integration: FastAPI TestClient + `CC_QUEUE=eager` +
  `CC_TRANSCRIBER=fake` — full create→upload→start→poll→done flow
  producing REAL rendered mp4s, asserting clip urls download and probe as
  portrait H.264 with audio.

## Env vars (see settings.py for defaults)

CC_DATA_DIR, CC_DB_PATH, CC_STORAGE (local|s3), CC_S3_BUCKET/PREFIX/REGION,
CC_REDIS_URL, CC_QUEUE (redis|eager), CC_TRANSCRIBER (faster-whisper|fake),
CC_WHISPER_MODEL, CC_WHISPER_DEVICE, CC_WHISPER_COMPUTE, CC_FFMPEG_BIN,
CC_FFPROBE_BIN, CC_CORS_ORIGINS, CC_MAX_UPLOAD_BYTES, CC_PUBLIC_BASE_URL,
CC_FAKE_TRANSCRIPT_PATH.

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
- `PUT /v1/me/brand` (session) — the brand kit, as multipart
  (`logo` file + `caption_color` + `show_logo`) or JSON (`logo` as a base64
  `data:` URL). Replaces the WHOLE kit. Logo ≤ 2 MB with the content type
  SNIFFED from the bytes, colour validated with the same rule as the TS,
  stored at `data_dir/brand/{user_id}.{ext}` — the extension comes from the
  sniffed type, never from the upload's filename. `403` when the effective
  plan carries no `brand_kit`. → `{logo_url, caption_color, show_logo}`
- `DELETE /v1/me/brand` — clears the kit and its file. Not plan-gated: a
  downgrade must never strand somebody's logo on our disk.
- `GET /v1/me/brand/logo` — the stored logo (session-only, `nosniff` +
  `default-src 'none'` because an uploaded SVG is markup).
- `POST /v1/auth/google` body `{id_token}` — the ID token Google Identity
  Services hands the browser. Verified for real against Google's JWKS
  (cached, refetched on an unknown `kid`): signature, `iss`, `aud ==
  CC_GOOGLE_CLIENT_ID`, `exp`/`nbf` with ≤ 60 s skew, and `email_verified`
  REQUIRED. Finds the account by `google_sub`, else LINKS one whose email
  matches, else creates a password-less one (`password_hash = ''`, which
  `/v1/auth/login` refuses outright). Mints the same session `/v1/auth/login`
  does, under the same rate limiter. `503` when CC_GOOGLE_CLIENT_ID is unset.
  → `{token, user}`
- `GET /v1/clips?limit=&before=` (session) — the clip library, newest first,
  ≤ 50 per page. `before` is the opaque cursor the previous page returned
  (`created_at` + the row's id, because one render writes several clips in the
  same millisecond). Each item carries `available` (the FILE is still here)
  and a `url` only when available; the metadata is there either way.
- `GET /v1/clips/{id}` (session, owner only, 404 otherwise) — one clip with its
  transcript. `DELETE /v1/clips/{id}` removes the row AND the file, and is the
  only thing that deletes a row.
- `GET /v1/clips/{id}/file` — the saved video (local mode serves it; S3 mode
  redirects to a presigned GET). `404` once retention has taken the file, with
  a body that says so rather than pretending the clip never existed.
- `POST /v1/clips/upload` (session) multipart `file` + `metadata` (JSON) —
  saves a clip the BROWSER rendered, only ever on an explicit click. The
  container is SNIFFED from the bytes (MP4/WebM), the size is counted as it
  streams (`CC_MAX_CLIP_BYTES`, 413 over), `engine` is forced to `browser`,
  and `expires_at` comes from the current plan. It costs NO monthly quota —
  nothing rendered on our hardware — so it is bounded instead by
  `CC_LIBRARY_MAX_BYTES` of stored files per account (402 over).
- `GET /v1/me` also returns `brand` (the kit, logo as a URL),
  `entitlements.brand_kit`, `entitlements.retention_days` (how long saved
  clips are kept; `null` = forever), and `auth_methods` (`["password"]`,
  `["google"]`, or both — the account page never offers "change password"
  without one).
- `GET /v1/healthz` → `{ok, version, queue: "redis"|"eager", storage,
  transcriber}`

## The clip library (LIBRARY.md Part 2)

Its own `clips` table, deliberately not the `jobs` row: jobs carry pipeline
state and are reaped at `CC_JOB_TTL_HOURS`, the library outlives them. Two
lifetimes share the row — the METADATA (title, score, hooks, transcript) is
permanent and dies only when the owner deletes the clip, while the FILE lives
until `expires_at = created_at + the owner plan's retention_days` (free 7,
starter 30, pro 90, enterprise forever). `worker.reap_expired_clips` deletes
the file and blanks `file_path`, keeping the row, which then lists with
`available: false`. A plan change EXTENDS every non-expired clip and never
shortens one (`billing.sync_clip_retention` → `db.extend_clip_expiry`).

Saved clips live under `settings.library_dir`, NOT under `clips_dir`. Two paths
`rmtree(clips_dir / job_id)` — the 48 h job reaper and `worker._discard_output`
— and a library file inside that tree would be destroyed by a sweep that knows
nothing about libraries. Separate roots make that structural; on top of it,
`_discard_output` refuses to delete any file a live library row still
references (`db.clip_file_referenced`), because "the job row is gone" is also
what a reaped job looks like. The rows themselves are written only after the
`processing → done` transition is WON, so a run that lost its claim — whose
clips were refunded — cannot file them in anybody's library.

## Modules & contracts

- `settings.py` (DONE — frozen): env-driven config, see file.
- `pipeline/types.py` (DONE — frozen): Word/Transcript/AudioFeatures/ClipPlan
  dataclasses; keep field names aligned with the TS types in
  `lib/studio/types.ts`.
- `brandkit.py`: the cloud half of the shared brand-kit core — a port of the
  pure section of `lib/studio/brandkit.ts` (`logo_box` geometry,
  `active_word_color`, `normalize_hex`, the upload limits) plus the
  server-only parts (`sniff_image_type`, id-derived storage paths).
  `api/tests/test_brandkit.py` cross-checks `logo_box` against the TypeScript
  through a node subprocess, like croptrack and diarize.
- `pipeline/probe.py`: `probe_media(path) -> MediaInfo{duration, width,
  height, has_audio}` via ffprobe (`settings.ffprobe_bin`); plus
  `probe_image_size(path) -> (w, h) | None`, which never raises — the logo
  overlay degrades to no overlay rather than failing a render.
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
  With a brand logo (`opts.logo_path`, only on unwatermarked renders) the
  logo becomes a second `-i` and the same chain moves into `-filter_complex`:
  `[1:v]scale=W:H[logo];[0:v]…[base];[base][logo]overlay=X:Y[v]` with
  `-map [v] -map 0:a?`, W/H/X/Y from `brandkit.logo_box`. Unbranded renders
  build the identical argv they always did.
- `db.py`: SQLite (WAL) at `settings.db_path`; plain sqlite3, tiny DAO:
  `create_job, get_job, update_job(**fields), set_clips(job_id, clips)`, plus
  the library half — `create_clip, get_clip, list_clips (cursor paginated),
  library_bytes, list_expired_clips, clear_clip_file, delete_clip,
  extend_clip_expiry`. Thread/process safe via short-lived connections.
- `storage.py`: `LocalStorage(root)` and `S3Storage(bucket, prefix)`
  behind one interface: `upload_target(job_id) -> UploadTarget`,
  `source_path(job_id) -> Path` (downloads from S3 to tmp when needed),
  `put_clip(job_id, path, name) -> stored key`, `clip_url(job_id, name) ->
  str` (local: `/v1/files/...`; S3: presigned GET). The library has its own
  four calls and its own root: `put_library_clip(user_id, name, path) ->
  file_path`, `library_clip_file`, `library_clip_url`, `delete_library_clip`.
  boto3 imported lazily.
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
CC_FAKE_TRANSCRIPT_PATH, CC_MAX_CLIP_BYTES, CC_LIBRARY_MAX_BYTES.
Clip RETENTION is deliberately not an env var — it is a plan entitlement.

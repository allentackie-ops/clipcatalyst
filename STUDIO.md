# ClipCatalyst Studio — working pipeline spec (browser-only MVP)

Studio is the real product at MVP scale: the visitor picks a video file, and
everything runs **in their browser** — no server, no upload. Pipeline:

1. Decode the audio track → mono 16 kHz PCM (`lib/studio/audio.ts`)
2. Transcribe with Whisper via transformers.js in a Web Worker
   (`lib/studio/transcribe.worker.ts` — DONE, do not modify)
3. Find + score the best moments (`lib/studio/highlights.ts`)
4. Render 9:16 clips with animated captions to MP4/WebM (`lib/studio/render.ts`)

The orchestration hook `components/studio/useStudioPipeline.ts` is DONE — read
it and `lib/studio/types.ts` first; they are the binding contract. Types in
types.ts must not be changed.

Design system: read DESIGN.md (tokens, shared kit `@/components/ui`). Studio
must look like a sibling of the `/demo` app — same ink/brand/spark palette,
mono for numbers, ScoreRing for scores.

Honesty rules: never imply upload/cloud processing. The privacy angle is a
feature — "your video never leaves your device." Label Studio "beta".
No new npm dependencies. TypeScript strict. No `any` unless unavoidable.

## Module specs

### lib/studio/audio.ts
- `decodeToMono16k(file, onProgress?)`: read the File into an ArrayBuffer
  (report progress via FileReader or Response streaming if easy; otherwise
  call onProgress(-1) once), decode with `AudioContext.decodeAudioData`, then
  resample/mix to mono 16 kHz with `OfflineAudioContext`. Return
  `{ pcm: Float32Array, duration }`. Close/free contexts when done. Wrap
  decode failures in a clear Error ("Couldn't read this file's audio…").
- `computeAudioFeatures(pcm, sampleRate)`: hop = 0.05 s. RMS per hop,
  normalized so the 95th percentile maps to 1 (clamp 0..1). Silence spans:
  normalized RMS < 0.08 sustained ≥ 0.35 s, merged within 0.1 s gaps.

### lib/studio/highlights.ts — the Virality Engine v0 (pure, deterministic)
`planClips(transcript, features, { targetLength, count })`:
- Build sentences: split words on terminal punctuation (. ? !) or inter-word
  gaps > 0.8 s; cap 25 words per sentence.
- Candidate windows start at each sentence: extend whole sentences while
  duration < targetLength; accept windows within [0.6×, 1.35×] targetLength.
- Score each window from features (weights suggested, tune freely but keep
  deterministic):
  - Hook strength of the FIRST sentence (0..1): lexicon hits — question mark;
    second person (you/your); numbers or $ amounts; superlatives/absolutes
    (best/worst/never/always/nobody/everyone); intrigue words (secret,
    mistake, truth, wrong, actually, honestly, surprised, insane, crazy);
    contrast openers (but, instead, here's the thing).
  - Info density: unique content words (≥4 chars, minus stopwords) / second.
  - Energy: mean normalized RMS across the window, plus a bonus if any hop
    > 0.85 (laughter/emphasis spike).
  - Pause penalty: fraction of the window inside silence spans.
  - Completeness bonus: window's last sentence ends with terminal punctuation.
  - Cold-open penalty: window starting < 8 s into the video (intros/greetings).
- Final score mapped to a 0–100 integer that spreads results (aim: winners in
  the 70s–90s, weak ones in the 40s–60s; never 100).
- Selection: greedy by score, windows must not overlap and starts must be
  ≥ 15 s apart; return `count` plans sorted by score desc.
- Snapping: pull start back to the nearest silence edge or sentence start
  (−0.15 s lead-in); push end to silence edge or sentence end (+0.25 s tail).
  Clamp to [0, duration].
- Per plan: `title` — strongest sentence, strip filler openers (so, and, um,
  you know, like), ≤ 48 chars cut at a word boundary, sentence case, no
  trailing punctuation. `hooks` — top 5 sentences in-window by hook score,
  trimmed ≤ 80 chars, strongest first. `reason` — one concrete line naming
  the top two scoring components ("Opens on a question and rides an energy
  spike"). `tip` — one actionable line derived from the weakest component
  ("Trim the pause at 0:12 — dead air is retention poison"). `words` —
  transcript words inside the window, times re-based to clip start.
  `id` — deterministic, e.g. `clip-${index}-${Math.round(start * 1000)}`.

### lib/studio/render.ts — canvas renderer
`renderClip(source, plan, { height, watermark }, onProgress)`:
- Create a hidden `<video>` (src = source.url, playsInline, crossOrigin not
  needed for blob URLs). Await metadata.
- Canvas: h = options.height, w = nearest even of h×9/16. Cover-crop the
  video frame centered (scale to fill, crop overflow).
- Audio capture WITHOUT audible playback: `new AudioContext()` →
  `createMediaElementSource(video)` → `createMediaStreamDestination()`;
  connect source→destination only (NOT ctx.destination).
- Stream: `canvas.captureStream(30)` video track + destination audio track →
  MediaRecorder. Mime preference order:
  `video/mp4;codecs="avc1.42E01E,mp4a.40.2"`, `video/mp4`,
  `video/webm;codecs=vp9,opus`, `video/webm`. videoBitsPerSecond:
  6_000_000 for height ≥ 1280 else 3_500_000. Throw a clear Error if
  MediaRecorder is entirely unavailable.
- Flow: seek to plan.start (await `seeked`), start recorder, `video.play()`,
  rAF loop: draw frame, draw captions for t = video.currentTime − plan.start,
  onProgress(clamp((t)/(plan.end−plan.start))), stop when
  currentTime ≥ plan.end: pause, recorder.stop(), collect chunks, cleanup
  (close AudioContext, remove video, stop tracks).
- Captions from plan.words (already re-based): group into lines of ≤ 4 words
  and ≤ 18 chars; render the active group centered at ~72% height: rounded
  dark strip (rgba(0,0,0,0.55), radius 12, padding), text
  `700 ${h*0.042}px Inter, sans-serif`, white; the CURRENT word in #a78bfa.
  Wrap to canvas width × 0.86 max. No captions during silent stretches.
- Watermark when options.watermark: bottom-right, `600 ${h*0.021}px Inter`,
  rgba(255,255,255,0.55): "⚡ ClipCatalyst".
- Thin progress bar: 4 px, bottom, brand violet #8b5cf6, width = t/duration.
- Return `{ blob, mimeType, extension }` (extension "mp4" iff mime includes
  mp4, else "webm").

### components/studio/StudioApp.tsx (+ any files under components/studio/,
except useStudioPipeline.ts which is DONE)
Client component using `useStudioPipeline`. Full-page studio experience, dark
studio look, min-h-dvh, its own slim top bar (Logo → "/", "Studio" + neutral
Badge "Beta", right side: link "Live demo" → /demo). States:
- **idle**: centered dropzone card (drag-over ring, click to pick;
  accept="video/mp4,video/quicktime,video/webm,video/x-m4v,video/*").
  Under it: settings — clip length chips (15s / 30s / 60s, default 30),
  clips count chips (1 / 2 / 3, default 2), quality select mapping to
  height 960 ("540×960 · fast"), 1280 ("720×1280 · balanced", default),
  1920 ("1080×1920 · max"). Privacy line with a lock inline-SVG: "Runs 100%
  in your browser — your video never leaves your device." Small print:
  best with talking content ≤ 15 minutes; first run downloads a ~40 MB AI
  model, cached afterwards.
  On file choose: probe duration via a temp video element; reject > 20 min
  with a friendly message (suggest trimming); show file chip (name, duration,
  size) + primary Button "Find my clips" that calls run(file, settings);
  watermark: true always (free tier).
- **running**: stage checklist (Read audio → Load AI model → Transcribe →
  Score moments → Render clips) — done stages get a signal check, current
  stage shows an animated bar (indeterminate when progress < 0, else %) and
  the `detail` line in font-mono; render stage shows "clip {i}/{n}". Include
  a subtle note while the model stage runs: "~40 MB on first visit — cached
  after." A "Cancel" ghost button → location.reload().
- **done**: "Your clips are ready" header + responsive grid of clip cards:
  9:16 `<video controls playsInline src={clip.url}>` (rounded-2xl,
  max-h ~480), ScoreRing(score), title, mono duration + format Badge
  (MP4/WEBM), reason line, tip line (ember tint), hooks: top 3 as list with
  A/B/C mono markers ("Recommended" signal Badge on the first), primary
  Button "Download" as an <a download={`clipcatalyst-${i + 1}.${ext}`}
  href={url}>, secondary "Clip another video" → reset(). Footnote: exports
  include the beta watermark; MP4 vs WebM depends on the browser.
- **error**: Card with the message, ember tone, "Try another video" → reset().
Also handle: browsers without MediaRecorder/AudioContext — show a friendly
"Studio needs a modern desktop browser (Chrome, Edge, or Firefox)" state
instead of crashing (feature-detect in an effect).

### Site integration (existing files)
- Navbar links: add { href: "/studio", label: "Studio" } before "Live demo".
- Hero: secondary CTA becomes href="/studio", label "Clip a video free →"
  (keep primary CTA unchanged).
- Demo banner (components/demo/DemoApp.tsx): extend the demo-mode banner
  copy with: "This is the guided tour — the real engine lives in Studio."
  where "Studio" is a Link to /studio (styled underline, brand-300).
- FAQ: add ONE entry: "What can the free Studio beta do right now?" — honest
  answer: runs Whisper AI + the virality engine in your browser, cuts 9:16
  captioned clips from talking videos (≤15 min works best), video never
  leaves the device; the 90-second cloud pipeline for hour-long podcasts is
  what the waitlist unlocks.
- FinalCTA: under the reassurance line add a subtle link line: "Can't wait?
  Try the Studio beta now →" → /studio.
- README.md: add a short "Studio (working beta)" section describing the
  in-browser pipeline and its limits.

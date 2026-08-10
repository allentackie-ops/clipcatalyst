# ClipCatalyst

Marketing site + interactive product demo for **ClipCatalyst** — studio-quality
AI clips in 90 seconds.

## Stack

- [Next.js 15](https://nextjs.org) (App Router) + React 19 + TypeScript (strict)
- [Tailwind CSS v4](https://tailwindcss.com) — design tokens live in
  `app/globals.css` (`@theme` block)
- One runtime dependency beyond React/Next: `@huggingface/transformers`
  (in-browser Whisper for Studio); the marketing pages are still asset-free —
  all visuals are CSS gradients and inline SVG

## Run it

```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # production build
npm start        # serve the production build
```

## Studio (working beta)

`/studio` is a real, working clipping pipeline that runs 100% in the browser —
no server, no upload, your video never leaves the device:

1. Decodes the audio track to mono 16 kHz PCM (Web Audio)
2. Transcribes it with Whisper (`@huggingface/transformers`) in a Web Worker —
   the ~40 MB model downloads on first run and is cached afterwards
3. Scores every moment with a deterministic virality engine (hooks, info
   density, energy, pauses)
4. Renders 9:16 captioned clips on a canvas via MediaRecorder (MP4 or WebM,
   depending on the browser)

Limits: talking content ≤ 15 minutes works best (hard cap 20), exports carry a
beta watermark, and it needs a modern desktop browser (Chrome, Edge, or
Firefox). The 90-second cloud pipeline for long-form footage is the product
the waitlist is for.

## Structure

```
app/
  layout.tsx          fonts, metadata, skip link
  page.tsx            landing page (composes the sections below)
  demo/page.tsx       interactive product demo (/demo)
  studio/page.tsx     working in-browser clipping pipeline (/studio)
  globals.css         Tailwind v4 theme tokens + global styles
components/
  ui.tsx              shared kit: Button, Badge, Card, ScoreRing, Logo, …
  Navbar.tsx          fixed navbar with mobile menu
  Footer.tsx
  sections/           one file per landing section (Hero, Pricing, …)
  demo/               the /demo app shell, clip grid, detail panel, chat mock
  studio/             the /studio app: dropzone, stage checklist, clip cards
lib/
  studio/             pipeline modules: audio, highlights, render, worker
DESIGN.md             design system + content spec the site was built against
STUDIO.md             working pipeline spec the Studio beta was built against
```

## Notes

- The waitlist form is UI-only (no backend); wire it to your email provider.
- The `/demo` page is a fully client-side mock — no uploads, no network calls.
- Pricing, features, and comparison content follow the product blueprint in
  `DESIGN.md`.

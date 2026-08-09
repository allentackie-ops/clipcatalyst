# ClipCatalyst — Design & Build Spec

ClipCatalyst is an AI video clipping tool: it turns long-form video (podcasts,
streams, VODs) into viral-ready vertical clips. Three product pillars, in
priority order: **speed** (studio-quality clips in under 90 seconds while
competitors take 10–20 minutes), **quality** (4K, no re-editing needed, XML
export to Premiere/Resolve), and **conversation** (chat-based editing — type
"make clip 2 more energetic" instead of clicking menus).

Audience: podcasters, streamers, and content teams. Voice: confident, punchy,
concrete. Numbers over adjectives ("90 seconds", "0–100", "4K") — never
corporate fluff. Short sentences. No exclamation marks except sparingly in UI
mock content.

## Hard rules (all builders)

1. **Write ONLY the file(s) assigned to you.** Never touch `app/page.tsx`,
   `app/layout.tsx`, `app/globals.css`, `components/ui.tsx`, or another
   builder's files.
2. TypeScript strict mode. Default export the section component. No props on
   section components (self-contained).
3. Server components by default. Add `"use client"` ONLY if you use state,
   effects, or event handlers.
4. **No external assets.** No image files, no remote URLs, no `next/image`
   with external src, no new npm dependencies. All visuals are CSS gradients,
   borders, and inline SVG.
5. No real company logos as graphics. Competitor NAMES in text (OpusClip,
   Klap, VEED, Descript) are fine in comparison copy.
6. No fabricated testimonials or fake press quotes. Product-UI mock content
   (fake clip titles, fake chat messages, fake filenames like
   `founder-podcast-ep42.mp4`) is encouraged — it reads as product, not as
   endorsement.
7. Accessibility: one `<h2>` per landing section (the section title), `<h3>`
   below it. Meaningful `aria-label`s on icon-only controls. Never rely on
   color alone.
8. Mobile-first responsive. Grids collapse to one column on mobile. Nothing
   overflows the viewport horizontally.
9. Use Tailwind utility classes only (v4). Custom one-off values via
   arbitrary values (`w-[4.5rem]`) are fine. Do not edit config files.

## Layout conventions

- Every landing section:
  `<section id="<kebab-id>" className="relative py-24 md:py-32">` with a
  `<Container>` inside. Use the exact `id` assigned to your section.
- Alternate background treatment is allowed (e.g. `bg-ink-900/40` or a
  radial glow) but keep the page cohesive — ink-950 base, subtle violet
  glows, never loud backgrounds.
- Vertical clip mockups are 9:16 aspect (`aspect-[9/16]`), rounded-2xl,
  gradient placeholder "video" (e.g.
  `bg-gradient-to-br from-ink-700 via-ink-800 to-brand-700/30`), with
  caption bars / score rings overlaid.
- Decorative glow: absolutely-positioned blurred radial div, e.g.
  `<div aria-hidden className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/20 blur-[120px]" />`
  Use sparingly (max one per section).

## Design tokens (Tailwind v4 theme — already defined)

Colors: `ink-950/900/850/800/700/600` (page + surfaces, darkest→lightest),
`brand-300..700` (violet, primary), `spark-400/500` (fuchsia accent),
`ember-400/500` (amber accent), `signal-400/500` (green success/score),
`line` and `line-strong` (borders: `border-line`, `border-line-strong`).

Fonts: `font-display` (Space Grotesk — headlines), `font-sans` (Inter — body,
default), `font-mono` (JetBrains Mono — numbers, scores, labels, code).

Animations: `animate-marquee` (needs a duplicated inline track, translates
-50%), `animate-pulse-soft`, `animate-rise`.

Signature gradient (use for key emphasis only):
`bg-gradient-to-r from-brand-400 via-spark-400 to-ember-400` (or the
`<GradientText>` helper).

## Shared UI kit — `@/components/ui` (import from here; exact API)

```tsx
Container({ children, className? })            // max-w-6xl px wrapper
Eyebrow({ children })                          // mono uppercase kicker
SectionHeading({ eyebrow?, title, lede?, align? = "center" | "left" })
GradientText({ children })                     // brand gradient span
Button({ children, href?, onClick?, variant? = "primary"|"secondary"|"ghost",
         size? = "md"|"lg", className?, type? })   // renders Link when href
Badge({ children, tone? = "brand"|"spark"|"ember"|"signal"|"neutral", className? })
Card({ children, className? })                 // rounded-2xl bordered surface
ScoreRing({ score, size? = 48, className? })   // 0–100 circular indicator
Logo({ className? })                           // bolt glyph + wordmark
```

`Button` with `onClick` requires a `"use client"` component. `ScoreRing`
colors itself: ≥80 green, ≥60 amber, else zinc.

## Section content source (from the product blueprint)

### Hero (`id="hero"`)
Headline territory: studio-quality clips in 90 seconds / stop editing, start
posting. Sub: paste a link, AI finds the viral moments, reframes to 9:16,
captions, scores. CTAs: "Start free — 3 clips/month" (#waitlist) and "Try the
live demo" (/demo). Stats to feature: **<90s** median processing, **4K** max
export, **0–100** virality score, **10GB** max upload, one-click publish to
TikTok / Shorts / Reels.

### Problem (`id="problem"`)
Creators spend ~80% of their time on manual editing — scrubbing hours of
footage for 60-second moments. Existing tools are "good enough": inaccurate
frame detection, 10–20 min processing, limited exports, clips that need
re-editing. ClipCatalyst's answer: find, cut, caption, score, publish —
automatically.

### Virality Engine (`id="virality-engine"`)
Not a score — an engine. Four capabilities: **Platform-specific
optimization** (tunes each clip for TikTok vs Reels vs Shorts trends),
**Hook generation** (writes 5 scroll-stopping hooks per clip, recommends the
strongest), **Timing optimization** (finds the exact millisecond to cut in
and out for retention), **Audio analysis** (detects laughter spikes,
dramatic pauses, emotional peaks). Every clip gets a 0–100 score plus
actionable improvement tips.

### Speed (`id="speed"`)
The 90-Second Guarantee. Competitors: 10–20 minutes. How: optimized GPU
pipelines, parallel processing, intelligent caching. Comparison bar visual:
ClipCatalyst ~90s, OpusClip ~12 min, Klap ~18 min.

### Chat editing (`id="chat-editing"`)
Type what you want instead of hunting menus: "Make clip 2 more energetic",
"Remove the awkward pause in clip 4", "Add a zoom on the host's face when he
laughs". AI applies the edit and shows the result. Reduces the edit loop
from hours to minutes.

### Features (`id="features"`)
**Core:** Smart highlighting (golden-nugget detection) · Auto-captioning
(10+ animated templates, keyword highlighting, auto-emoji, speaker colors) ·
Vertical reframing (9:16 with face tracking) · Brand kit (logo, fonts,
colors auto-applied) · Built-in editor · One-click publish · Virality score ·
Bulk processing (whole podcasts/VODs) · Multi-speaker detection.
**Pro:** AI hook generator · Auto B-roll (relevant stock footage) ·
Chat-based editing · Team workspace (approvals, brand consistency) · XML
export (Premiere, Resolve, Final Cut) · A/B testing (3 versions per clip) ·
Analytics dashboard · API access.

### Pipeline (`id="pipeline"`)
"Under the hood" — seven layers: L1 Topic detection (WhisperX + speaker
diarization) → L2 Creative scoring (multimodal LLM) → L3 Visual processing
(9:16 reframe, face tracking — OpenCV + MediaPipe) → L4 Audio (cleanup,
music, levels — FFmpeg + AI noise reduction) → L5 Post-production (captions,
transitions, B-roll) → L6 Distribution (one-click publish, platform APIs) →
L7 Optimization (A/B tests, reinforcement learning — every clip makes the
engine smarter).

### How it works (`id="how-it-works"`)
1. **Upload** — paste a YouTube link, upload a file (MP4/MOV/AVI/WebM up to
   10GB), or connect Drive/Dropbox. 2. **Configure** (optional) — platforms,
   clip length (15/30/60s), brand kit, vibe (funny/serious/educational/
   dramatic). 3. **AI processes** — live progress, preview clips as they
   finish. 4. **Review & edit** — virality scores, built-in editor, chat
   refinements. 5. **Publish** — download up to 4K, one-click post, schedule,
   or share a review link.

### Pricing (`id="pricing"`)
Free $0: 3 clips/mo, watermark, 720p. Starter $19/mo: 30 clips, no
watermark, 1080p, brand kit. **Pro $49/mo (highlight, "Most popular"):**
100 clips, 4K, chat editing, AI hooks, B-roll. Enterprise from $99/mo:
unlimited clips, API, team workspace, custom branding. Add-on: 50 extra
credits for $20 (1 credit = 1 clip). Annual toggle optional (2 months free
if you build one). CTA per tier → #waitlist (Enterprise → "Talk to us").

### Comparison (`id="compare"`)
Rows vs OpusClip (Pro-gated features, slower processing), Klap (frame
detection issues, slow editor, limited exports), VEED (generalist, not
purpose-built), Descript (powerful but steep learning curve). ClipCatalyst
column: ~90s processing, chat-based editing, 4K + XML export, purpose-built
for podcasters & streamers, all features accessible. Keep it factual-toned,
check/dash matrix plus one-line "where it falls short" per competitor.

### FAQ (`id="faq"`)
6–8 questions: processing speed (how is 90s possible), accuracy, supported
formats/length, watermarks, XML export workflow, publishing platforms,
credits vs plans, data ownership (your footage stays yours; clips train the
virality model only in aggregate, opt-out available).

### Final CTA + waitlist (`id="waitlist"`)
Big closing moment: "Your next viral clip is already in your footage."
Email input + "Join the waitlist" button (UI only — no backend; on submit
show an inline "You're on the list" success state, no network call).
Reassurance line: free tier, no credit card, 90-second first clip.

## Demo app (`/demo`) — separate spec

Immersive product mock (client components, all state local, no network).
Layout: left sidebar (Logo, nav: Projects / Clips / Analytics / Brand Kit /
Settings; credits meter at bottom), top bar (project name
"founder-podcast-ep42.mp4 · 1:24:06", "Processing complete" badge, Export
all button), main area: grid of 6 clip cards.

Each clip card: 9:16 gradient thumbnail (vary the gradients), duration badge,
title, ScoreRing, platform badges, hover ring. Clicking a card opens a detail
panel: bigger 9:16 preview with animated caption bar mock + hook options
(5 hooks, one marked "Recommended"), and a chat panel ("Catalyst" assistant)
with a scripted conversation — user asks e.g. "Punch up the hook and tighten
the first 2 seconds", assistant replies with what it changed, score updates
(e.g. 74 → 91). Include a typing indicator and 2–3 quick-action chips
("Remove filler words", "Add zoom on laugh", "More energetic pacing").
Also include one card in "processing" state with an animated progress bar
and "~90s" ETA. A dismissible banner may note: "Demo mode — sample project,
no upload required."

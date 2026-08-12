# Pre-launch checklist

The site describes the **finished** ClipCatalyst. That is correct while we are
building it — the marketing is the spec. This file is the gap between that spec
and the code, so nothing on the site goes in front of a stranger while it is
still untrue.

An audit compared all 197 user-facing claims against the shipped code. Nothing
below is a request to change the site's ambition; it is the build list the site
implies, plus a short list of claims that should change wording even after
everything is built.

---

## 1. Ship before the site is public

These are advertised and not yet built. Each needs code, or the claim moves to
a "coming soon" section rather than a plan's feature list.

| Claim | Where | State |
|---|---|---|
| Brand kit | Pricing (Starter), Features, FAQ | No brand assets exist — no logo/font/colour anywhere in the schema or the renderers |
| Auto B-roll | Pricing (Pro), Features | Neither renderer composites a second video source |
| Team workspace | Pricing (Enterprise), Features | `users` is flat and single-owner; jobs carry one `user_id` |
| API access | Pricing (Enterprise), Features | No customer API keys — only a session bearer and the founder token |
| Custom branding | Pricing (Enterprise) | The watermark is a string literal in both renderers |
| One-click publish to TikTok / Shorts / Reels | Hero mock, Features, Pipeline L7, HowItWorks | No platform integration; the only outbound path is the OS share sheet |
| Credits add-on (50 for $20, "never expire") | Pricing | No credit system; the allowance is a per-calendar-month counter, which does expire |
| Affiliate programme, 20% lifetime | Footer | No referral codes, attribution, or payout logic |
| XML / timeline export | Features, FAQ | Nothing emits an NLE timeline |
| A/B testing + analytics dashboard | Features | No experiment or analytics storage |
| Caption templates, keyword highlighting, emoji | Features, Pipeline L5 | Captions are word-level with per-speaker colour; no templates |
| AI noise reduction, music beds | Pipeline L4 | ffmpeg copies audio; no filtering or mixing |
| Bulk processing / "feed it whole podcasts" | Features | Hard caps: 3 clips per job, 20 min in-browser |
| YouTube-link ingest, Drive / Dropbox | HowItWorks | File input only |
| About / Blog / Contact pages | Footer | `href="#"` — Contact is the only way to reach you and it goes nowhere |

## 2. Numbers to make true (or restate)

| Claim | Reality in code | Options |
|---|---|---|
| "10GB max upload" | 1.4 GB in-browser, 2 GB cloud (`CC_MAX_UPLOAD_BYTES`) | Raise the cloud cap, or state the real number |
| "4K max export" | Cloud-only, and a 9:16 crop of a 1080p source is 608 px wide, so 2160-wide output is a 3.6× upscale | Keep 4K (founder's call) and note the source requirement, or gate the option on source size |
| "0–100 virality score" | Clamped to 35–96 in both engines | Widen the clamp, or call it a 0–100 scale |
| "<90s median time" | Never measured. Browser records in real time, so render ≥ clip length; `DEPLOY.md` estimates ~2–3 min on a T4 | Measure on the GPU box, then publish the real number |
| Competitor times (OpusClip ~12 min, Klap ~18 min) and the four `vs.` comparison columns | No source anywhere in the repo | Source them from public pricing/docs with a date, or drop the comparison. **This is the item most likely to draw a legal complaint.** |
| "80% of a creator's time" (Problem) | No source | Source or cut |
| Pipeline mock shows a 1:24:06 source | Exceeds the 20-minute cap the same site states | Change the mock or raise the cap |

## 3. Wording that should change even once everything is built

These describe the engineering incorrectly, so shipping more features will not
make them true.

- **"WhisperX"** (Pipeline L1) — the code uses `faster-whisper`. Say so, or switch libraries.
- **"Multimodal LLM · retention priors"**, **"a multimodal model rates each candidate"** (Pipeline L2, Virality Engine) — the scorer is a deterministic six-component weighting with no model and no LLM. *Determinism is the better story: the same video always yields the same clips.*
- **"MediaPipe face tracking"** (Pipeline L3) — the cloud uses OpenCV Haar cascades, the browser uses face-api.js.
- **"keeps every face locked dead-center"** — tracking is best-effort and degrades to a centred crop when confidence drops.
- **Chat demo replies** — the demo shows "crossfaded the audio", "re-flowed the caption line", "boosted pacing 12%", "re-timed captions to land on the beat". The editor cuts pauses, trims, zooms, toggles captions and swaps hooks. Either build those, or show replies `applyCommand` actually produces.
- **Score deltas after an edit** ("74 → 88") — nothing re-scores a clip after editing.
- **"Every clip renders simultaneously"** (Speed) — renders are sequential, one clip at a time.
- **"Intelligent caching"** (Speed) — no cache exists in either engine.
- **FAQ ownership answer** — claims an aggregate-training opt-out and server-side project deletion; neither exists.

## 4. A domain

The site currently lives at `allentackie-ops.github.io/clipcatalyst`. Buying a
domain is not cosmetic — four separate things are waiting on it:

- **Email deliverability.** Sign-in codes send from `onboarding@resend.dev`
  until a domain is verified with Resend. That is a shared address, so a share
  of codes will land in spam — and a sign-in code in a spam folder is a lost
  customer, not an inconvenience. This is the one that breaks the product.
- **The API.** The RunPod box answers on `<pod-id>-8000.proxy.runpod.net`,
  which changes if the pod is ever resized or recreated. `api.<domain>` means
  the box can move without rebuilding the site.
- **Google sign-in.** The OAuth client's authorized origin must match the
  site's origin exactly, so configuring it against github.io means redoing it
  later.
- **Charging money.** $19–$99/month against a `github.io` URL reads as a side
  project at exactly the moment someone is deciding whether to trust it.

When it exists, the wiring is: a `CNAME` file plus DNS for Pages;
`metadataBase` in `app/layout.tsx` (hardcoded to github.io today, and wrong
link previews are silent); `CC_CORS_ORIGINS` on the API; Google's authorized
origins; and Resend's DNS records. Roughly fifteen minutes of config.

`.com` may be gone; `.app` and `.video` read fine for this and cost less than
`.io`/`.ai`. Cloudflare sells at cost and its DNS is what you would want
anyway.

## 5. Sequencing

1. **RunPod box** — the cloud engine has never run on a GPU. Everything in
   the cloud column of the site is untested against real hardware, and until
   it exists the account page correctly says accounts aren't live: sign-in,
   plans, billing and the library all live on that server.
2. **Domain** (§4) — before Google sign-in is configured and before sign-in
   codes are sent to anyone real, so neither has to be redone.
3. **Stripe keys** — billing is built and hardened; it needs three price IDs
   and a webhook secret to charge.
4. Then the items in §1 that you actually want in v1 — the rest move to a
   roadmap section.
5. Then measure the real numbers in §2 and publish those.
6. Then §3, which is a copy pass.

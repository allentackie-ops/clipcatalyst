# De-AI the design — build spec

Four tells make a site read as machine-generated. Remove all four across every
page and component, without breaking the product.

## 1. Gradients → one solid accent

The rainbow violet→fuchsia→amber gradients, the glowing gradient buttons and
the big blurred colour blobs are the loudest tell. Founder's call: **restrained
— a single solid violet accent, solid buttons, no glows.**

- **`GradientText`** (`from-brand-400 via-spark-400 to-ember-400 bg-clip-text`)
  → solid `text-brand-300`. Keep the component name/API so callers are
  untouched; change only what it renders.
- **`Button` primary** (`bg-gradient-to-r from-brand-600 to-spark-500` + the
  `shadow-[0_0_24px…]` glow + `hover:brightness-110`) → a **solid** `bg-brand-600`
  with `hover:bg-brand-500`, no glow shadow. Secondary/ghost keep their shapes.
- **Every `bg-gradient-to-*` on a filled surface** (CTA cards, the pricing
  highlight, chat avatars, score chips, the app icon lockups on the marketing
  page) → a solid brand tint. A gradient is allowed ONLY where it is genuinely
  structural and not decorative: the 2 px `RouteProgress` bar and the burned-in
  render progress bar may keep a subtle two-stop gradient. Nothing else.
- **The big blurred radial glows** — every `blur-[100px]`/`blur-[120px]`/
  `blur-[130px]` decorative blob behind heroes and sections — **delete them.**
  They are the single most AI-SaaS element on the site. Backgrounds become the
  flat ink ground.
- The **wordmark** stays two-tone (solid "Clip" white + solid "Catalyst"
  violet) — that is brand identity in solid colours, not a gradient, and it is
  fine.
- The Splice **mark** and the **loader** keep their gradient fill — the mark is
  the logo and the loader is a deliberate, contained brand moment, not a
  page-wide wash. Leave `BrandLoader`, `app/icon.svg`, `apple-icon.png`,
  `opengraph-image.png` alone.

The result should read like a considered product with one confident accent
colour, not a template with the "AI gradient" preset on.

## 2. Em-dashes → natural punctuation

The em-dash (`—` / `&mdash;`) is the classic AI-writing tell. Remove **every
one from user-facing copy** and rewrite the sentence so it reads naturally:
a period, a comma, a colon, parentheses, or a restructure — whichever fits.
Do NOT leave a hanging " - " in its place; that is its own tell. Read each
sentence and make it sound like a person wrote it.

**Keep legitimate hyphens.** This is the one place the literal instruction
must be softened, and the reason is that removing them would look broken, not
polished: hyphens inside compound words and identifiers are correct English
and every real product uses them — `on-device`, `sign-in`, `one-click`,
`9:16`, `real-time`, `word-level`, `720×1280`, `pre-ticked`. Removing those
would read as a bug. The tell is the **em-dash used as a dramatic pause**, not
the hyphen in a compound.

**En-dash ranges** (`0–100`, `7–90`): convert to a plain form — `0 to 100`,
or a hyphen where it reads clean (`720x1280` already uses ×). No `–` survives
in copy.

Scope: every `.tsx` under `app/` and `components/`, plus any user-facing string
in `lib/`. Code comments and commit history are not user-facing and are out of
scope — but a `—` inside a JSX text node, a `title`/`aria-label`/`placeholder`,
a metadata `description`, or a data array that renders to screen IS in scope.

## 3. Mobile must not bleed

Nothing may overflow the viewport horizontally on a phone. Test at **320, 375
and 390 px** wide. The page body must never scroll sideways, and no element,
badge, table, code block, button row or heading may sit under the edge or push
past it.

- Audit every route (`/`, `/studio`, `/demo`, `/account`, `/privacy`,
  `/terms`) and every section at all three widths.
- Wide content (comparison tables, the pipeline, any horizontal strip) gets a
  contained `overflow-x-auto` on its OWN wrapper so the wrapper scrolls, not
  the page.
- Long unbroken strings (tokens, filenames, URLs) get `break-words` /
  `truncate` so they cannot force width.
- The fix is real containment, not `overflow-x: hidden` on the body masking a
  child that still bleeds — find the child and constrain it.

## 4. Remove decorative badges

Founder's call: **decorative pills only; keep functional status.**

REMOVE (marketing decoration, the "EARLY ACCESS" tell):
- The hero eyebrow pill (`Free — no signup…` with the pulsing dot).
- Every section **`Eyebrow`** chip above a heading (`Studio beta`,
  `The secret sauce`, `Two engines`, `Plain-language editing`, etc.) — replace
  with nothing, or fold the word into the heading if it carried meaning. The
  headings stand on their own.
- `Most popular` / `Recommended` marketing badges on pricing/features.
- The `Beta` chip in the Studio top bar and the "Studio beta" eyebrow.

KEEP (functional status that carries real information):
- Plan state on the account page (`Active`, `Current plan`), usage, the "N
  speakers" clip badge, upload/render progress, the cloud-vs-device labels,
  the honest "awaiting review" connection state, format/size chips on a clip
  card. These tell the user something true and specific; they are not
  decoration.

When a whole `Eyebrow`/`Badge` usage is removed, remove the now-unused import
and tidy the spacing the chip left behind so the heading is not floating in a
gap.

## Verify

- Grep proves it: **zero** `—`/`&mdash;`/`–`/`&ndash;` in user-facing copy;
  **zero** `bg-gradient` on filled surfaces outside the two allowed progress
  bars; **zero** decorative `blur-[…]` glow blobs; the decorative eyebrows and
  marketing badges gone.
- Mobile: automated horizontal-overflow check at 320/375/390 on every route
  returns 0 px, captured with screenshots.
- Every existing suite stays green; both builds pass; `npx tsc --noEmit` clean.
- Screenshots of the home, studio, pricing, account and demo at desktop AND
  phone width, before/after, so the change is visible.

## Non-negotiables

- The product keeps working — this is paint and copy, not behaviour. No test
  changes beyond snapshot text that legitimately moved.
- Legitimate hyphens in compounds and identifiers survive.
- The logo, favicon, loader and OG image are untouched.
- Functional, information-carrying status stays; only decoration goes.

# ClipCatalyst

Marketing site + interactive product demo for **ClipCatalyst** — studio-quality
AI clips in 90 seconds.

## Stack

- [Next.js 15](https://nextjs.org) (App Router) + React 19 + TypeScript (strict)
- [Tailwind CSS v4](https://tailwindcss.com) — design tokens live in
  `app/globals.css` (`@theme` block)
- Zero runtime dependencies beyond React/Next; all visuals are CSS gradients
  and inline SVG (no external assets)

## Run it

```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # production build
npm start        # serve the production build
```

## Structure

```
app/
  layout.tsx          fonts, metadata, skip link
  page.tsx            landing page (composes the sections below)
  demo/page.tsx       interactive product demo (/demo)
  globals.css         Tailwind v4 theme tokens + global styles
components/
  ui.tsx              shared kit: Button, Badge, Card, ScoreRing, Logo, …
  Navbar.tsx          fixed navbar with mobile menu
  Footer.tsx
  sections/           one file per landing section (Hero, Pricing, …)
  demo/               the /demo app shell, clip grid, detail panel, chat mock
DESIGN.md             design system + content spec the site was built against
```

## Notes

- The waitlist form is UI-only (no backend); wire it to your email provider.
- The `/demo` page is a fully client-side mock — no uploads, no network calls.
- Pricing, features, and comparison content follow the product blueprint in
  `DESIGN.md`.

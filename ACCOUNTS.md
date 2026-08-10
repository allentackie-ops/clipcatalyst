# Accounts + billing — build spec

The pricing page promises Free ($0, 3 clips/mo, 720p, watermark), Starter
($19, 30 clips/mo, 1080p, no watermark), Pro ($49, 100 clips/mo, 4K) and
Enterprise ($99+, unlimited). This spec makes those real: email+password
accounts, Stripe subscriptions, and server-enforced entitlements — all in
the existing FastAPI service (`api/`), consumed by the static frontend.

## Scope decisions (binding)

- **The API service owns accounts and billing.** The frontend is a static
  export (GitHub Pages) with no server; every account interaction is a
  client-side call to the API at `NEXT_PUBLIC_CLOUD_API`. When that env is
  unset (today's public build), all account UI degrades to an honest
  "activates when the ClipCatalyst server is live" state and the pricing
  CTAs stay exactly as they are now (waitlist).
- **Stripe via the official `stripe` python package** behind a small
  gateway. `CC_BILLING = stripe | fake | off` (default off). `fake` is the
  test/dev gateway (FakeTranscriber pattern). What CANNOT be verified in
  this sandbox: live Stripe API round-trips (egress blocked). What IS
  verified for real: webhook **signature verification** (the stripe lib
  signs and verifies offline), the full entitlement pipeline, and the
  browser flow against the fake gateway.
- **Email is optional infrastructure.** `CC_MAILER = none | console |
  resend` (default none). With `none`, signup works unverified and password
  reset returns an honest 400 ("reset isn't available yet"). The flows are
  built; Resend activates them when the user has a key. No fake "check your
  inbox" messages when nothing was sent.
- **No plan changes ever come from client input.** Plans change ONLY via
  verified Stripe webhooks (or the founder flipping the DB). The checkout
  endpoint takes a plan *name* and maps it to a server-configured price id.

## Data model (db.py — CREATE TABLE IF NOT EXISTS + guarded ALTERs)

```
users:   id TEXT PK (hex32), email TEXT UNIQUE (lowercased), password_hash TEXT,
         created_at TEXT, stripe_customer_id TEXT DEFAULT '',
         plan TEXT DEFAULT 'free', plan_status TEXT DEFAULT '',
         current_period_end TEXT DEFAULT ''
sessions: token_hash TEXT PK (sha256 hex of the raw token), user_id TEXT,
         created_at TEXT, expires_at TEXT
stripe_events: event_id TEXT PK, processed_at TEXT       -- webhook idempotency
usage:   user_id TEXT, month TEXT 'YYYY-MM', clips_used INTEGER,
         PRIMARY KEY (user_id, month)
jobs:    + user_id TEXT DEFAULT '' (ALTER TABLE guarded by PRAGMA table_info)
```

## Auth (`api/clipcatalyst_api/auth.py`)

- Password hashing: **stdlib `hashlib.scrypt`** (no new dependency),
  n=2^14 r=8 p=1, 32-byte `secrets` salt, stored as
  `scrypt$16384$8$1$<salt_b64>$<hash_b64>`; verify with
  `hmac.compare_digest`. Min password length 8. Emails lowercased/stripped,
  validated with a simple regex.
- Sessions: raw token `cc_sess_` + `secrets.token_urlsafe(32)`, returned
  once; only the sha256 hex is stored. TTL `CC_SESSION_TTL_DAYS` (30).
  Bearer header. Expired/unknown → 401.
- Endpoints (`/v1/auth/*`):
  - `POST /v1/auth/register {email, password}` → `{token, user}` (auto
    sign-in). Duplicate email → 409 (accepted enumeration tradeoff — a
    product signup form, not a bank; noted in code).
  - `POST /v1/auth/login {email, password}` → `{token, user}`; any failure
    → 401 with one generic message (no user-exists oracle in the ERROR path).
  - `POST /v1/auth/logout` (session) → revokes that session.
  - `GET /v1/me` (session) → `{email, plan, plan_status, quota: {limit,
    used, month}, entitlements: {max_height, watermark_required,
    clips_per_month}}` — the single source the frontend trusts.
- Rate limiting: in-process fixed-window per (client ip, route) — 10/min on
  register+login, 429 beyond. Honest limitation (per-process, resets on
  restart) documented where it's defined.
- **Job ownership:** session-created jobs store `user_id`; `GET /v1/jobs/
  {id}`, uploads, start, and `/v1/files/*` for a user-owned job require
  that user's session (founder `CC_API_TOKEN` still passes everything —
  it's the owner's box). Anonymous founder-token jobs behave exactly as
  today. A session must never read or drive another user's job (IDOR
  tests required).
- Passwords and raw tokens must never be logged.

## Billing (`api/clipcatalyst_api/billing.py`)

- `PLANS` (single source of truth for entitlements, mirrored nowhere else):

```
free:       clips_per_month 3,    max_height 1280, watermark_required True
starter:    clips_per_month 30,   max_height 1920, watermark_required False
pro:        clips_per_month 100,  max_height 3840, watermark_required False
enterprise: clips_per_month None, max_height 3840, watermark_required False
```

- Effective plan = `plan` while `plan_status` in {active, trialing,
  past_due}; anything else (canceled, unpaid, "") → free.
- Gateway protocol: `create_checkout(user, plan) -> url`,
  `create_portal(user) -> url`; both create/reuse `stripe_customer_id`.
  - StripeGateway: Checkout Session `mode=subscription`, the plan's price
    id, `client_reference_id=user.id`, subscription metadata `user_id`,
    success `={CC_FRONTEND_ORIGIN}/account?checkout=success`, cancel
    `…?checkout=cancelled`. Portal via `billing_portal.Session.create`.
  - FakeGateway: deterministic urls (`https://billing.invalid/checkout/
    {plan}/{user_id}`), records calls for tests.
- Endpoints:
  - `POST /v1/billing/checkout {plan}` (session) → `{url}`; 503 honest
    message when billing is off; 400 for unknown/free plan.
  - `POST /v1/billing/portal` (session, requires a customer id) → `{url}`.
  - `POST /v1/billing/webhook` — NO session/token auth; **`Stripe-
    Signature` verified against `CC_STRIPE_WEBHOOK_SECRET` on the RAW
    body** via `stripe.Webhook.construct_event`; missing/bad signature →
    400, billing off → 503. Idempotent via `stripe_events` (replay → 200
    no-op). Handled events: `checkout.session.completed` (resolve
    subscription → price → plan; store customer id, status, period end),
    `customer.subscription.updated` (plan/status/period follow Stripe),
    `customer.subscription.deleted` (→ free), `invoice.payment_failed`
    (→ past_due). Unknown events → 200 ignored. Price↔plan map from
    `CC_STRIPE_PRICE_STARTER/PRO/ENTERPRISE`.
- New settings (+ `.env.example` + DEPLOY.md): `CC_BILLING`,
  `CC_STRIPE_SECRET_KEY`, `CC_STRIPE_WEBHOOK_SECRET`,
  `CC_STRIPE_PRICE_STARTER/PRO/ENTERPRISE`, `CC_FRONTEND_ORIGIN`,
  `CC_MAILER`, `CC_RESEND_API_KEY`, `CC_SESSION_TTL_DAYS`.
  `requirements.txt` gains `stripe` (pin the installed major).

## Enforcement (the part that makes the promises true)

- `POST /v1/jobs` with a session: requested `height` is clamped to the
  effective plan's `max_height`; watermark is forced on for plans with
  `watermark_required` (RenderOptions already carries `watermark` — cloud
  renders currently always watermark; paid plans now turn it off).
  Cloud `height` gains 3840 (4K) as a valid value end-to-end (models,
  render.py width math handles it unchanged; browser stays ≤1920 — 4K is
  cloud-only, and the pricing copy already frames 4K as an export).
- `POST /v1/jobs/{id}/start` with a session: monthly quota check —
  `used + job.count > clips_per_month` → **402** with an honest message
  naming the plan, the limit, and the reset month. Founder-token and
  dev-open jobs: unchanged, no quota.
- Worker: on job completion, increment `usage(user_id, month)` by the
  number of clips actually rendered (month = completion time UTC).
- `GET /v1/me` reports `quota.used` from the same table the enforcement
  reads — the account page can never disagree with the server.

## Frontend

- `lib/account.ts`: typed client (register/login/logout/me/checkout/
  portal), token in `localStorage["cc_session"]` (XSS tradeoff noted in a
  comment; no cookies — the API is cross-origin from github.io).
- `components/account/AccountProvider.tsx`: context + `useAccount()` →
  `{user, loading, refresh, signOut}`; fetches `/v1/me` on mount when a
  token exists; 401 clears the token.
- `app/account/page.tsx` (client component, static-export safe):
  - API unset → honest card: accounts activate when the ClipCatalyst
    server is live; link back to Studio/waitlist.
  - Signed out → Sign in / Create account (one card, tabbed), inline
    errors, loading states.
  - Signed in → plan card (name, status, renewal), usage meter
    (`used / limit` this month), upgrade buttons per paid plan →
    `checkout()` redirect (billing off → the server's honest 503 message
    inline), "Manage billing" → portal (only when a customer id exists),
    sign out.
  - `?checkout=success` → "Confirming your upgrade…" polling `/v1/me`
    (2 s interval, ~20 s budget) until the plan reflects the webhook;
    timeout → honest "payment received; this can take a minute" note.
    `?checkout=cancelled` → dismissible notice.
- `components/sections/Pricing.tsx`: when the API is configured, Free CTA
  → `/account`, Starter/Pro → `/account?plan=starter|pro` (the account
  page auto-opens checkout for that plan once signed in), Enterprise →
  keep "Talk to us" (waitlist). When unset: exactly today's waitlist CTAs.
- Navbar: "Account" link (the page handles every state).
- Studio: `useAccount()` — an effective paid plan renders device clips
  WITHOUT the watermark (soft client-side enforcement; the device pipeline
  runs on the user's own hardware and the honest boundary is documented in
  STUDIO.md); free/anon → exactly today. Cloud engine calls attach the
  session bearer when present (`cloud.ts` gains an auth header hook) so
  the server enforces quotas/heights/watermark.
- All new UI matches the existing design language and a11y patterns.

## Tests (api/tests/: test_auth.py, test_billing.py, test_entitlements.py)

- Auth: register/login/logout/me round-trip; duplicate 409; wrong password
  401 (same body as unknown email); malformed/expired/revoked sessions
  401; scrypt hash format + verify; sessions stored hashed (assert the
  raw token is NOT in the DB file); rate limit 429; IDOR — user B cannot
  GET/start/upload/read files of user A's job (404, matching the
  path-traversal convention), founder token still can.
- Billing: fake gateway urls + customer-id reuse; webhook with a REAL
  stripe-lib-signed payload accepted; tampered body / wrong secret /
  missing header → 400; replayed event id → 200 no-op with no double
  apply; each handled event mutates the user correctly (incl. price→plan
  mapping and subscription.deleted → free); unknown event 200.
- Entitlements: free session job — height 1920 request clamps to 1280 and
  watermark forced; starter clamps 3840→1920; pro gets 3840; quota — free
  user with 3 used gets 402 on the next start (message names the plan and
  limit); usage increments by rendered count on completion (eager queue);
  month key rollover respected; canceled plan behaves as free; founder
  token unaffected everywhere.
- Browser e2e (scratchpad, static build + uvicorn with CC_BILLING=fake +
  eager queue): register on /account → shows Free 0/3 → harness POSTs a
  stripe-signed `checkout.session.completed` webhook → refreshed account
  shows Starter 0/30 + Manage billing appears; pricing page CTAs point at
  /account in this build; sign out returns the signed-out card.
- Every existing suite stays green (172 python + 142 edits + 119 intent +
  11 interactive + 28 croptrack + 20 diarize + 20 speakerfeats, tsc, both
  builds, studio e2e).

## Security non-negotiables

- Webhook: signature verification is mandatory in stripe mode — there is
  NO code path that applies an unverified event.
- Sessions and passwords: hashed at rest, constant-time compares, never
  logged, never in URLs.
- Ownership: every job route checks user_id when the job has one; 404 (not
  403) for other users' jobs.
- Plan/entitlement values live server-side only; nothing the client sends
  can raise its own plan, height cap, or quota.
- Login failure body is identical for bad-email and bad-password (409 on
  register is the accepted, documented exception).
- CORS stays the existing explicit `cors_origins` allowlist; the webhook
  route is server-to-server and does not rely on CORS for safety.

## User activation steps (documented in api/STRIPE-SETUP.md)

A copy-paste walkthrough for the founder: create the Stripe account, the
three products/prices, the webhook endpoint (`/v1/billing/webhook`, the
four event types), then set the CC_* envs on the API box and
`NEXT_PUBLIC_CLOUD_API` at frontend build time. Includes the test-mode
card number flow and how to verify with `stripe listen` locally.

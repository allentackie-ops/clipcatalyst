# Stripe setup — from zero to paid subscriptions

A copy-paste walkthrough for turning ClipCatalyst's paid plans on. Everything
server-side is already built; this document is only about creating the Stripe
objects and wiring six environment variables. Budget ~30 minutes, most of it
clicking around the Stripe dashboard.

How it works once wired: the account page calls `POST /v1/billing/checkout`,
the API mints a Stripe Checkout URL, the user pays on Stripe's page, and
Stripe calls back to `POST /v1/billing/webhook`. **Only that webhook —
signature-verified against your signing secret — ever changes a plan.** The
API then enforces the plan server-side: clip quota (402 when spent), max
render height, and the watermark on free-tier renders.

## 1. Create the Stripe account

[dashboard.stripe.com/register](https://dashboard.stripe.com/register). You
can build and test everything in **Test mode** (toggle in the top right)
before activating live payments — do that first; every step below works
identically in both modes, they just produce separate keys/prices/secrets.

## 2. Create the three products

Dashboard → **Product catalog** → **Add product**, three times:

| Product name          | Price     | Billing period |
| --------------------- | --------- | -------------- |
| ClipCatalyst Starter  | $19.00    | Monthly        |
| ClipCatalyst Pro      | $49.00    | Monthly        |
| ClipCatalyst Enterprise | $99.00  | Monthly        |

Each product gets one recurring price. Open each product and copy its **price
id** (`price_…` — the price's id, not the product's `prod_…` id). These three
ids are how the webhook maps a paid subscription back to a plan name; a price
the server doesn't recognize never grants a plan.

The entitlements themselves (3/30/100/unlimited clips, 720p/1080p/4K,
watermark) live in `clipcatalyst_api/plans.py` — Stripe only decides *which*
plan a user pays for.

## 3. Create the webhook endpoint

Dashboard → **Developers** → **Webhooks** → **Add endpoint**:

- Endpoint URL: `https://your-box.example.com/v1/billing/webhook`
  (your public API origin — the same host the frontend calls; it must be
  HTTPS, see DEPLOY.md §3/§7 for putting TLS in front).
- Events to send — select exactly these four:
  - `checkout.session.completed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_failed`

After creating it, reveal and copy the endpoint's **Signing secret**
(`whsec_…`). The API rejects (400) any webhook whose `Stripe-Signature`
doesn't verify against it, so the endpoint being public is fine.

## 4. Set the environment on the API box

In the `.env` next to `docker-compose.yml` (see `.env.example` for the
documented template):

```
CC_BILLING=stripe
CC_STRIPE_SECRET_KEY=sk_test_...        # Developers → API keys (sk_live_… when live)
CC_STRIPE_WEBHOOK_SECRET=whsec_...      # step 3
CC_STRIPE_PRICE_STARTER=price_...      # step 2
CC_STRIPE_PRICE_PRO=price_...
CC_STRIPE_PRICE_ENTERPRISE=price_...
CC_FRONTEND_ORIGIN=https://allentackie-ops.github.io/clipcatalyst
```

`CC_FRONTEND_ORIGIN` is where Stripe sends the browser back after checkout
(`…/account?checkout=success|cancelled`) — no trailing slash. Restart the
stack (`docker compose up -d`) so the API picks the values up.

The frontend needs nothing new for billing — just the usual build-time API
URL (DEPLOY.md §3): `NEXT_PUBLIC_CLOUD_API=https://your-box.example.com`.

## 5. Verify with a test-mode purchase

1. Open the site, create an account, and hit **Upgrade** on Starter — you
   should land on a Stripe Checkout page.
2. Pay with the test card `4242 4242 4242 4242`, any future expiry, any CVC,
   any postal code. (Declines: `4000 0000 0000 0002`.)
3. You're sent back to `/account?checkout=success`; within a few seconds the
   webhook lands and the page shows **Starter · 0/30 clips** and a **Manage
   billing** button (the Stripe customer portal).
4. Dashboard → Webhooks → your endpoint should show the deliveries as
   succeeded (2xx). A 400 there means the signing secret doesn't match.
5. In the portal, cancel the subscription — after `customer.subscription.
   deleted` arrives the account is free-tier again (3 clips, 720p,
   watermark).

## 6. Local verification with `stripe listen`

To exercise real signed webhooks against a dev API (no public URL needed):

```bash
stripe login
stripe listen --forward-to localhost:8000/v1/billing/webhook
```

`stripe listen` prints its own `whsec_…` — put THAT in
`CC_STRIPE_WEBHOOK_SECRET` while it runs (it differs from the dashboard
endpoint's secret). Then either click through a test checkout, or fire a
single event by hand:

```bash
stripe trigger checkout.session.completed
```

(Triggered fixture events carry no real user id, so the API logs
"matched no user" and ignores them — that still proves signature
verification and delivery end to end. For a full plan change, drive a real
test checkout from the site with the account you registered.)

## 7. Going live

Flip the dashboard out of Test mode, then repeat steps 2–4 once in Live mode
— live keys, live prices, and a live webhook endpoint are separate objects —
and swap the four `sk_/whsec_/price_` values in `.env`. Test-mode
subscriptions never bill anyone; live ones do.

Two honest caveats:

- **No prorated downgrades UI**: plan switches happen through the Stripe
  portal ("Manage billing"); Stripe's default proration applies. Good enough
  to launch.
- **The founder override**: `CC_API_TOKEN` requests bypass quotas and
  entitlements by design (it's your box). Plans can also be changed manually
  by editing the `users` table — the webhook will overwrite manual edits the
  next time that user's subscription changes.

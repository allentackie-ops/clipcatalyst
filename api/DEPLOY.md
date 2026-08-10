# Deploying ClipCatalyst Cloud

One image, three containers: `api` (FastAPI), `worker` (Celery, same image,
different command), `redis`. All state — uploads, rendered clips, SQLite job
store, Whisper model cache — lives in the `ccdata` volume mounted at `/data`.

## 1. Local dev (CPU)

```bash
cd api
docker compose up --build
```

That's the whole setup. API on `http://localhost:8000`, health at
`GET /v1/healthz`. The compose defaults are CPU-safe: `CC_WHISPER_MODEL=base`,
device auto-detected. Expect **minutes, not seconds** — `base` on an 8-core
laptop transcribes a 60-minute episode in ~10–15 minutes. That's fine: local
dev is for exercising the flow, not the 90-second story. For pipeline tests
without any model at all, set `CC_TRANSCRIBER=fake` and
`CC_FAKE_TRANSCRIPT_PATH` to a transcript JSON.

First real run downloads the Whisper model into `/data/hf-cache` (persists in
the volume; `distil-large-v3` is ~1.5 GB).

## 2. Production: a GPU box

Any CUDA VM works — RunPod, Lambda, EC2 `g4dn`/`g5`, your own box. Driver
installed, then:

```bash
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Uncomment the GPU `deploy:` stanza on the `worker` service in
`docker-compose.yml`, and set (a `.env` file next to the compose file works):

```
CC_WHISPER_MODEL=distil-large-v3
CC_WHISPER_DEVICE=cuda
CC_WHISPER_COMPUTE=float16
```

`--concurrency=1` on the worker is deliberate: one GPU, one model instance,
one job at a time. Scale by adding worker containers on more boxes pointed at
the same Redis, not by raising concurrency on one GPU.

### What to expect: 60-min 1080p episode → transcribe + 3 rendered clips

| Hardware | Transcribe | 3 renders (sequential) | Wall total |
|---|---|---|---|
| T4, distil-large-v3, float16 | ~60–90 s | ~45–75 s | **~2–3 min** |
| A10, distil-large-v3, float16 | ~40–60 s | ~30–50 s | **~1.5–2 min** |
| 8 vCPU, base, int8 | ~10–15 min | ~1.5–3 min | ~12–18 min |

Render time is per-clip, independent of episode length (ffmpeg input-seeks
before decoding), so the episode only taxes transcription.

**Hitting ~90 seconds** takes GPU transcription (~60–80 s) **plus parallel
renders**. The three renders are independent single-clip ffmpeg jobs (~15–25 s
each) but the MVP worker runs them sequentially — parallelizing them (fan out
per-clip Celery tasks, or a small process pool inside `process_job`) is the
first optimization, worth ~30–50 s of wall time. Everything else is already in
budget.

## 3. Point the frontend at it

The frontend is a static Next.js export on GitHub Pages, so the API URL is
baked in **at build time**:

```bash
NEXT_PUBLIC_CLOUD_API=https://your-box.example.com npm run build
```

Two things must line up:

- `CC_CORS_ORIGINS` must include `https://allentackie-ops.github.io` — the
  compose default already does. If you fork the frontend, add your origin.
- GitHub Pages is HTTPS, so browsers will block calls to plain
  `http://your-box:8000` as mixed content. Put TLS in front:
  `caddy reverse-proxy --from your-box.example.com --to localhost:8000` is a
  one-liner, or use a Cloudflare Tunnel. Then set
  `CC_PUBLIC_BASE_URL=https://your-box.example.com` so clip URLs come back
  absolute.

## 4. S3 mode

Local-disk storage means 2 GB uploads flow through your box and clips are
served by FastAPI. S3 mode moves both to presigned URLs — browser PUTs the
video straight to the bucket, clip links are presigned GETs:

```
CC_STORAGE=s3
CC_S3_BUCKET=your-bucket
CC_S3_PREFIX=clipcatalyst
CC_S3_REGION=us-east-1
AWS_ACCESS_KEY_ID=...        # standard boto3 credential chain;
AWS_SECRET_ACCESS_KEY=...    # instance roles work too
```

Set these on **both** `api` and `worker` (they share the `x-app-env` block, so
a `.env` file covers both). The bucket needs a CORS rule allowing `PUT` and
`GET` from your frontend origin.

## 5. Cost reality

- **T4**: ~$0.20–0.35/hr (RunPod, Vast). Comfortable for this workload;
  ~$150–250/mo if you keep it warm 24/7.
- **A10/A10G**: ~$0.35–0.60/hr. Buys you the sub-2-minute experience.
- **CPU-only VPS**: $20–40/mo. Works end to end; jobs take 10–20 minutes.

A warm 24/7 GPU is wasteful until you have steady traffic. **Scale-to-zero is
the next step**: Modal or RunPod Serverless spin a GPU up per job and bill by
the second. The port is small by design — the `clipcatalyst_api.pipeline`
package is deployment-agnostic (no FastAPI or Celery imports), so a Modal
function is just `probe → transcribe → plan_clips → render_clip` wrapped in
their decorator, with the Celery queue swapped for their invocation API. Keep
this compose stack for the API and job store; only the worker moves.

## 6. Accounts + billing (Stripe)

Email+password accounts are always on. Paid plans need Stripe, which is **off
by default** (`CC_BILLING=off`): checkout and portal answer an honest 503 and
every account is free-tier. To turn subscriptions on, follow the step-by-step
founder walkthrough in **[STRIPE-SETUP.md](STRIPE-SETUP.md)** — create the
three products, the webhook endpoint, then set:

```
CC_BILLING=stripe
CC_STRIPE_SECRET_KEY=sk_live_...
CC_STRIPE_WEBHOOK_SECRET=whsec_...
CC_STRIPE_PRICE_STARTER=price_...
CC_STRIPE_PRICE_PRO=price_...
CC_STRIPE_PRICE_ENTERPRISE=price_...
CC_FRONTEND_ORIGIN=https://allentackie-ops.github.io/clipcatalyst
```

Plans change **only** through webhook events whose `Stripe-Signature` verifies
against `CC_STRIPE_WEBHOOK_SECRET` — nothing a client sends can raise its own
plan, height cap, or quota. Entitlements are enforced server-side: session job
heights clamp to the plan's `max_height`, free-tier renders keep the
watermark, and `POST /v1/jobs/{id}/start` answers 402 once the plan's monthly
clip quota is spent. `CC_BILLING=fake` is the offline dev gateway (tests, and
driving the account UI without a Stripe account) — webhooks are
signature-verified in fake mode too.

## 7. Security — do this before you expose the box publicly

The default build is **open**: with `CC_API_TOKEN` unset, anyone who can reach
`:8000` can create jobs, upload 2 GB files, and download clips. That default
exists so `docker compose up` just works on localhost. **It is dev-only.** The
moment the API is reachable from the internet, treat the steps below as
mandatory, not optional.

### 7.1 Set an API token

Generate a strong token and put it in the `.env` next to the compose file:

```bash
openssl rand -hex 32
```

```
CC_API_TOKEN=<paste-the-generated-token>
```

When set, the mutating routes — `POST /v1/jobs`, `PUT /v1/uploads/{id}`,
`POST /v1/jobs/{id}/start`, and `GET /v1/files/...` — require
`Authorization: Bearer <token>` (compared in constant time). `GET /v1/jobs/{id}`
and `GET /v1/healthz` stay open so status polling and health checks keep
working. Setting it also switches off the interactive docs (`/docs`, `/redoc`,
`/openapi.json`), which exist to help you explore a dev box, not to enumerate
a public one. Copy `.env.example → .env` for the full, documented variable list.

**Selling plans requires this token.** With `CC_BILLING` set to anything but
`off`, the API refuses to start while `CC_API_TOKEN` is empty, and says so:

```
RuntimeError: CC_BILLING='stripe' but CC_API_TOKEN is empty. With no founder
token the job routes stay open to anonymous callers, ...
```

That is not pedantry. An empty token leaves the job routes open to callers
with no session, and a caller with no session has no account: no plan, so no
height clamp (4K for anyone) and no monthly quota (renders nobody is billed
for). Every entitlement the pricing page promises becomes optional, and the
paying customers are the only ones subject to it. Set the token, or run
`CC_BILLING=off` — the dev default, where every account is free-tier anyway.

### 7.2 Put TLS + rate limiting in front

Never expose plain `http://your-box:8000`. GitHub Pages is HTTPS, so browsers
block mixed-content calls anyway (see §3). Terminate TLS and rate-limit at a
reverse proxy:

- **Caddy** — automatic HTTPS plus a simple limiter:

  ```
  your-box.example.com {
      @api path /v1/*
      rate_limit @api {
          zone api { key {remote_host}; events 60; window 1m }
      }
      reverse_proxy localhost:8000 {
          # Overwrite, don't append: the API reads the LEFTMOST entry, and a
          # header the client wrote must never survive into it.
          header_up X-Forwarded-For {remote_host}
      }
  }
  ```

- **Cloudflare** — a Tunnel (no open inbound port) with a WAF rate-limit rule
  on `/v1/jobs` and `/v1/uploads` is the lowest-effort option.

Then set `CC_PUBLIC_BASE_URL=https://your-box.example.com` so clip URLs come
back absolute. Rate limiting matters because uploads and renders are expensive —
one script hammering `POST /v1/jobs` can fill your disk or GPU queue.

#### Tell the API who the proxy is

Once a proxy is in front, every request arrives from the *proxy's* address.
Nothing downstream can tell two callers apart unless you say which peer is
allowed to speak for them:

```
CC_TRUSTED_PROXIES=172.18.0.2        # ips or CIDR blocks, comma-separated
```

With that set, the API's own auth rate limiter (10/min on register, login and
checkout) keys on the leftmost `X-Forwarded-For` entry — but **only** when the
connection came from one of those addresses. Everything else keys on the peer.
The default is empty, meaning trust nobody: that is deliberate, because a
forwarded header is client-written, and believing one unconditionally does not
just weaken the limiter, it removes it (an attacker rotates the header per
attempt and is never counted twice).

Leaving it unset behind a proxy has the opposite failure: every client shares
one bucket, so a single attacker at a trickle can 429 login and signup for
*everyone*. Set it, and configure the proxy to overwrite `X-Forwarded-For`
(the Caddy `header_up` above) — a proxy that appends leaves the leftmost entry
under the client's control.

For the same reason uvicorn is started **without** `--proxy-headers` in the
image. If you want `request.client.host` itself rewritten (access logs, other
middleware), override the command with both flags and name the proxy — never
`*`:

```yaml
command: >
  uvicorn clipcatalyst_api.main:app --host 0.0.0.0 --port 8000
  --proxy-headers --forwarded-allow-ips 172.18.0.2
```

### 7.3 Retention

`CC_JOB_TTL_HOURS` (default 48) bounds how long uploaded sources, rendered
clips, and job rows live before the hourly reaper deletes them. It is a storage
and privacy control as much as a cleanup one: user videos should not sit on the
box forever. Lower it if you handle sensitive footage; raise it if users need
longer to fetch their clips.

### 7.4 How the frontend sends the token — and the honest caveat

The static frontend bakes both the API URL and the token in at build time:

```bash
NEXT_PUBLIC_CLOUD_API=https://your-box.example.com \
NEXT_PUBLIC_CLOUD_API_TOKEN=<the-same-token> \
npm run build
```

The client sends it as `Authorization: Bearer <token>` on every write call.

**Be clear-eyed about what this token is not.** Anything baked into a browser
build is visible to anyone who opens devtools — a `NEXT_PUBLIC_*` value ships in
the JavaScript bundle. So a shared token in a public static site is not a real
per-user secret; it only keeps out casual/random traffic and pairs with the
proxy rate limit. For a single-operator setup that is fine. Before you open
Studio's cloud mode to untrusted users, pick one of:

- **Keep cloud mode private** — gate it behind the founder's own use, or behind
  a login on the frontend so the bundle (and token) never reaches the public.
- **Move to per-user tokens** — issue a short-lived token per authenticated user
  from a tiny auth endpoint, instead of one shared build-time token. The Bearer
  check here already accepts that model; only token minting needs adding.

Until then: shared token + TLS + rate limit is a reasonable founder-use
posture, but do not market it as multi-tenant-secure.

# Publishing to socials — build spec

One touch from a finished clip to a post. YouTube first, because it is the
least gated and reuses the Google OAuth already in the codebase; TikTok and
Instagram are adapters behind the same interface, added when their reviews
land.

## What is actually blocking this (not code)

- **YouTube**: `youtube.upload` is a Google *sensitive* scope. Before
  verification an app is capped at 100 users and **every upload is forced to
  `private`**. After verification, public posting works. So the honest v1 is:
  it really uploads, to your own channel, as a private video you publish from
  YouTube — and it says exactly that in the UI.
- **TikTok**: Direct Post needs an audit; un-audited apps can only create
  private drafts.
- **Instagram**: needs a Business/Creator account, a linked Facebook Page, and
  App Review.
- **All three** require a privacy policy URL, terms, and a verified domain for
  the redirect URI. Hence Part 1.

Nothing in the UI may promise a platform that is not connected and working.

## Part 1 — `/privacy` and `/terms`

Plain English, and **true of the code**. Anything here that misdescribes the
product is a defect, not a wording choice. The facts, from the codebase:

- The browser engine processes video entirely on the device; nothing is
  uploaded, and there is no telemetry on it.
- The cloud engine uploads the source. **The source file is deleted as soon
  as the job finishes** (`worker._remove_source`), win or lose.
- Rendered clips live in the library for the plan's retention window (7 / 30 /
  90 days / unlimited); after that the file is deleted and only the metadata —
  title, score, hooks, transcript — is kept, so a library still lists what was
  made. Users can delete either at any time.
- Job rows are reaped after 48 hours.
- Accounts store: email, a scrypt password hash (or none, for code/Google
  sign-in), a Google subject id if linked, session tokens **stored hashed**,
  and a brand kit (logo file, caption colour).
- Payments: Stripe. We store a Stripe customer id and plan state, never card
  details.
- Email: Resend delivers sign-in codes. Sign-in codes are stored hashed.
- **No user video or transcript is used to train anything.** There is no
  training pipeline in this product; say so plainly rather than with the usual
  hedge.
- Analytics: none installed today. If that changes the page changes with it.
- Publishing: when a channel is connected we store an access token and refresh
  token, encrypted, plus the channel name. Disconnecting revokes them.

Terms cover: what the service does, acceptable use (no content you do not have
rights to), that the user owns their footage and their clips, that the free
tier is watermarked, cancellation and refunds, the 90-second figure being a
target rather than a guarantee, and liability limits. Written to be read, not
to be impressive.

Both pages: same layout language as the rest of the site, a "last updated"
date, linked from the footer, and reachable at stable URLs (they go into three
platform applications).

## Part 2 — connections (`api/clipcatalyst_api/connections.py`)

```
connections: id TEXT PK, user_id TEXT, platform TEXT,        -- 'youtube' …
             account_name TEXT,                              -- shown in the UI
             account_id TEXT,
             access_token_enc TEXT, refresh_token_enc TEXT,
             expires_at TEXT, scopes TEXT,
             created_at TEXT, updated_at TEXT
unique index on (user_id, platform, account_id)
```

- **Tokens are encrypted at rest** with Fernet (`cryptography`, already a
  dependency via google-auth) keyed by `CC_TOKEN_KEY`. No key configured →
  connecting is refused with an honest 503 rather than storing plaintext.
  The key is never logged; a decrypt failure disconnects the account and asks
  the user to reconnect rather than crashing a publish.
- OAuth: authorization-code flow with **PKCE** and a signed, single-use
  `state` bound to the session (CSRF). `access_type=offline` and
  `prompt=consent` so a refresh token actually arrives.
- Endpoints: `GET /v1/connections` (list, never returns tokens),
  `POST /v1/connections/{platform}/start` → `{authorize_url}`,
  `GET /v1/connections/{platform}/callback` (the redirect target; exchanges
  the code, stores the connection, redirects back to `/account`),
  `DELETE /v1/connections/{id}` (revokes with the provider, then deletes).
- Refresh: a helper that returns a live access token, refreshing when it is
  within 5 minutes of expiry and persisting the new one. Providers that
  rotate refresh tokens must have the new value stored.

## Part 3 — the YouTube adapter + publish jobs

`api/clipcatalyst_api/publish/youtube.py`, behind a `PublishTarget` protocol
so TikTok and Instagram slot in later without touching the queue.

- Upload is YouTube's **resumable** protocol: `POST
  /upload/youtube/v3/videos?uploadType=resumable&part=snippet,status` with the
  metadata, then PUT the bytes to the returned session URL, honouring 308
  resume responses and retrying a chunk up to 3 times on 5xx.
- Metadata from the clip: title (the clip's title, truncated to YouTube's 100
  characters), description (the user's caption plus the clip's top hook if
  they leave it blank), `categoryId` 22, and **`privacyStatus` from the
  connection's capability** — `private` while the app is unverified, the
  user's choice once it is not. The UI states which applies.
- A `publish_jobs` table mirrors the render job pattern: queued → uploading →
  done/failed, with progress and a user-facing error. Runs on the same Celery
  queue. The clip file comes from the library, so publishing an expired clip
  is refused with a clear message rather than a 500.
- Quota: YouTube's upload quota is small (a few uploads/day on a default
  project). A 403 quota error must surface as "YouTube's daily upload limit
  for this app was reached. Try tomorrow.", not a generic failure.

## Part 4 — UI

- `/account` gains **Connections**: one row per platform showing connected
  account name and a Connect/Disconnect button. Unconnected platforms that
  cannot yet work say so plainly ("TikTok posting is awaiting review") rather
  than offering a dead button.
- Library and Studio results gain **Post to YouTube** on a clip, opening a
  small sheet: title, description, and a note on the current privacy
  behaviour. Progress while uploading, then a link to the video.
- The existing native share sheet **stays** — it is the only path that works
  on a phone with no connections, and for TikTok and Instagram it remains the
  real answer until their reviews land.

## Tests

- `test_connections.py`: state is single-use and session-bound (a replayed or
  foreign state is refused); PKCE verifier is required; tokens are never
  returned by any endpoint and never appear in the DB in plaintext (grep the
  file); disconnect revokes then deletes; a decrypt failure degrades to
  "reconnect" instead of raising; no `CC_TOKEN_KEY` → 503 and nothing stored.
- `test_publish_youtube.py`: against a stubbed HTTP layer (the mailer/googleid
  pattern) — a full resumable upload; a 308 resume; a chunk retried on 5xx; a
  403 quota mapped to the friendly message; an expired-token refresh that
  persists the new token; publishing an expired library clip refused; another
  user's clip 404.

## Non-negotiables

- No token is ever stored or logged in plaintext, or returned by an endpoint.
- The UI never offers a platform that cannot currently complete a post.
- Privacy and terms describe the code as it is; if the code changes, they do.
- The native share sheet is not removed.

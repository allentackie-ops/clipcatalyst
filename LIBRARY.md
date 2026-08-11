# Sign-in and the clip library — build spec

Two decisions, one spec, because they share a schema: how people get into an
account, and what the account remembers.

Founder's calls, already made:
- **Google first**, alongside the email/password login that already exists.
- **Retention by plan**: free 7 days, starter 30, pro 90, enterprise unlimited.
- **Metadata is permanent** — a clip's title, score, hooks and transcript
  outlive its video file.
- **Browser clips are uploadable, but only on purpose** — off by default.

## Part 1 — Sign in with Google

### The separation that matters

Identity and publishing are different systems and must not be conflated.
Google is an **identity provider** — it says who someone is. TikTok, Instagram
and YouTube will be **connections** on an existing account, added later, purely
for posting. Signing in *with* TikTok would mean a creator whose TikTok gets
banned loses their ClipCatalyst account and library with it. We are not doing
that.

### Flow

Frontend uses Google Identity Services to obtain an **ID token** (a JWT) and
POSTs it to `/v1/auth/google`. The server:

1. Verifies the signature against Google's JWKS (cached, refreshed on unknown
   `kid`), and checks `iss` ∈ {accounts.google.com, https://accounts.google.com},
   `aud == CC_GOOGLE_CLIENT_ID`, and `exp`/`nbf` with ≤ 60 s clock skew.
2. **Requires `email_verified == true`.** An unverified email is refused — this
   is the whole security boundary, because a matched email links accounts.
3. Finds the user by `google_sub`, else by email:
   - `google_sub` match → sign in.
   - email match, no `google_sub` → **link**: set `google_sub`, sign in. A
     verified Google email proving ownership of an address is the same
     assurance a password reset would give.
   - no match → create the user with `password_hash = ''` (a password-less
     account; the password login path must reject an empty hash rather than
     comparing against it).
4. Mints a session exactly like `/v1/auth/login`, same TTL, same cookie-less
   bearer, and rate-limits by the same limiter (a forged-token flood is cheap
   to send and expensive to verify).

`users` gains `google_sub TEXT DEFAULT ''` with a UNIQUE index where non-empty
(guarded ALTER). Settings gain `CC_GOOGLE_CLIENT_ID` (empty = the endpoint 503s
honestly, and the button does not render).

New dependency: `google-auth` for token verification. Hand-rolling JWKS +
RS256 is the kind of code that is quietly wrong for a year, and this is an auth
boundary. Note it in the report.

`GET /v1/me` gains `auth_methods: ["password"] | ["google"] | both`, so the
account page can say how someone signs in and never offer "change password" on
a password-less account.

### Tests (`api/tests/test_google_auth.py`)
Sign the ID tokens in the test with a locally generated RSA key and point the
verifier at a stub JWKS — verification must be exercised for real, not mocked
away. Cases: valid token creates a user; second call signs the same user in;
email match links to an existing password account; `email_verified: false` →
401 and no account touched; wrong `aud`, wrong `iss`, expired, `nbf` in the
future, bad signature, and a token signed by an unknown `kid` → 401 each, with
the user table unchanged; unset `CC_GOOGLE_CLIENT_ID` → 503; the rate limiter
applies. Also: a password-less account cannot be logged into via
`/v1/auth/login` with an empty password.

## Part 2 — The clip library

### Schema

A new `clips` table, deliberately **not** the `jobs` row — jobs are reaped and
carry pipeline state; the library outlives them.

```
clips: id TEXT PK, user_id TEXT, job_id TEXT DEFAULT '', clip_index INTEGER,
       title TEXT, score INTEGER, hooks TEXT(json), reason TEXT, tip TEXT,
       start REAL, end REAL, duration REAL, width INTEGER, height INTEGER,
       speaker_count INTEGER DEFAULT 0, words TEXT(json) DEFAULT '',
       engine TEXT,                      -- 'cloud' | 'browser'
       file_path TEXT DEFAULT '',        -- '' once the file has been reaped
       bytes INTEGER DEFAULT 0,
       created_at TEXT, expires_at TEXT DEFAULT ''   -- '' = never
index on (user_id, created_at DESC)
```

`Plan` gains `retention_days: int | None` — free 7, starter 30, pro 90,
enterprise None. Do NOT derive it from any existing field.

### Retention

- `expires_at = created_at + retention_days` of the owner's plan **at render
  time**; empty string for unlimited.
- **A plan change extends but never shortens.** On an upgrade, recompute every
  non-expired clip's `expires_at` and keep the later value. On a downgrade,
  leave existing clips alone — taking away something already made is a support
  ticket, and the storage is already spent. New clips get the new plan's window.
- The reaper deletes the **file** at `expires_at` and blanks `file_path`,
  leaving the row. Metadata is permanent, as decided. A row is only deleted
  when the user deletes it or the account is deleted.
- The existing 48 h `jobs` reaper is unchanged and independent: it clears
  pipeline state, not the library. `worker.py` must write the library row
  **before** the job becomes reapable, and must not delete a clip file that a
  library row still points at — check the current `_discard_output` and reaper
  paths for exactly this, since they currently `rmtree` the whole clips dir.

### Endpoints

- `GET /v1/clips?limit=&before=` (session) → newest first, cursor on
  `created_at`, ≤ 50 per page. Each item carries `available: bool`
  (`file_path != ''`) and a `url` only when available.
- `GET /v1/clips/{id}` (session, owner only, 404 otherwise — same convention
  as jobs).
- `DELETE /v1/clips/{id}` → removes the row and the file.
- `POST /v1/clips/upload` (session) — multipart: the rendered file plus a JSON
  metadata part. For saving a **browser** clip on purpose. Size cap
  `CC_MAX_CLIP_BYTES` (default 200 MB), content type sniffed, `engine` forced
  to `'browser'`, `expires_at` from the current plan. **Does not consume
  monthly quota** — nothing was rendered on our hardware — but is refused with
  402 when the account is over a separate `CC_LIBRARY_MAX_BYTES` per-user
  storage ceiling (default 5 GB) so a free account cannot be used as a disk.
- All of these enforce ownership; `/v1/files/{job_id}/{name}` keeps working for
  in-flight jobs and gains a library-aware path for clips whose job is gone.

### Tests (`api/tests/test_library.py`)
Cloud render writes a library row with the right `expires_at` per plan;
listing is owner-scoped and paginated in the right order; another user gets 404
on read and delete; the reaper blanks the file and keeps the row, and the row
still lists with `available: false`; upgrade extends `expires_at`, downgrade
does not shorten it; upload rejects oversize, wrong type, and an over-ceiling
account; upload does not move the monthly quota counter; deleting removes both
row and file; the jobs reaper does not destroy a file the library still
references.

## Part 3 — Frontend

- `lib/account.ts`: `signInWithGoogle(idToken)`, `listClips`, `deleteClip`,
  `uploadClip`.
- Google button: load Google Identity Services from their CDN **only on the
  account page**, and render nothing when `CC_GOOGLE_CLIENT_ID` is unset. It is
  a third-party script — it must not land on every page.
- `/account` gains a **Library** section: a grid of clip cards (poster, title,
  score, duration, date), an "Expired" state for rows whose file is gone that
  still shows title, score and hooks, a delete control with confirmation, and
  a plan-aware line saying how long clips are kept.
- Studio results: a **"Save to library"** button per clip, visible only when
  signed in, **never automatic** — the site promises the video stays on the
  device, so an upload is always an explicit act. Show its progress, and on
  success mark the card saved. Failure is inline and never loses the local file.

## Non-negotiables

- An unverified Google email never links to or creates an account.
- Browser clips upload only on an explicit click, never on a timer, never on
  completion, never behind a pre-ticked box.
- Metadata is permanent; files expire. The UI must make the difference obvious
  rather than showing a broken player.
- Source videos are still never stored beyond their job.
- Every existing suite stays green.

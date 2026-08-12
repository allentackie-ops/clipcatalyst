# Email code sign-in — build spec

Sign in by typing an email, receiving a 6-digit code, and typing it back. No
password, no OAuth console, no app review, no consent screen. Works on every
device and the same way on all of them.

**A code, not a magic link.** Corporate mail scanners and link-preview bots
fetch URLs in incoming mail, which silently consumes a one-time link before
the human ever clicks it — the classic "the link says it's already been used"
support ticket. A typed code cannot be consumed by a scanner, and it works
when the mail arrives on a phone but the browser is on a laptop.

This does not replace anything. Email/password stays; Google stays behind its
client-id gate. This is the third door and, once a mailer is configured, the
one to lead with.

## Flow

1. `POST /v1/auth/email/start {email}` → always **200 `{sent: true}`** when a
   mailer is configured, whether or not the address has an account. Creating
   the account happens at verify time, so there is nothing to enumerate.
2. `POST /v1/auth/email/verify {email, code}` → 200 with a session, exactly the
   shape `/v1/auth/login` returns.

Verifying an address is the same assurance a password reset gives, so:
- unknown email → **create** a password-less account (as Google does)
- known email, password account → **sign in** to it
- known email, Google account → sign in to it; nothing about `google_sub` changes

## Storage

```
login_codes: email TEXT PRIMARY KEY,   -- lowercased; one live code per address
             code_hash TEXT,           -- sha256 of the 6 digits, NEVER the code
             expires_at TEXT,
             attempts INTEGER DEFAULT 0,
             created_at TEXT
```

A new `start` for an address **replaces** its row, so a second request
invalidates the first code. Rows are deleted on success and swept by the same
hourly beat that reaps jobs.

## The rules that make it safe

- **Code**: 6 digits from `secrets.randbelow(1_000_000)`, zero-padded. Never
  logged, never returned by any endpoint, never stored in plaintext.
- **Compare**: sha256 + `hmac.compare_digest`, same discipline as sessions.
- **TTL**: `CC_EMAIL_CODE_TTL_MINUTES`, default **10**.
- **Attempts**: **5** per code, then the row is deleted and the code is dead —
  the user asks for a new one. Without this, 6 digits is a million guesses at
  whatever rate the limiter allows.
- **Rate limits, two independent ones**, because they stop different attacks:
  - per **address**: `CC_EMAIL_CODE_PER_HOUR`, default **5** starts/hour, so
    nobody can be mail-bombed by someone typing their address repeatedly;
  - per **client**, through the existing `enforce_rate_limit` on both routes,
    so one machine cannot farm codes or grind verifications.
- **Uniform responses**: `start` returns the same body and timing whether or
  not the address exists. `verify` returns ONE generic 401 for wrong, expired,
  exhausted and never-requested alike.
- **Mailer off** (`CC_MAILER=none`): `start` returns **503** with an honest
  message. It must never pretend to have sent mail.

## Mailer (`api/clipcatalyst_api/mailer.py`)

`CC_MAILER = none | console | resend` (already in settings).

- `console` — writes the message to the log, including the code. Dev only, and
  the module must say so; it is the one place a code appears in plaintext.
- `resend` — `POST https://api.resend.com/emails` with `CC_RESEND_API_KEY` and
  `CC_MAIL_FROM` (default `ClipCatalyst <onboarding@resend.dev>`, which Resend
  allows before a domain is verified). 10 s timeout. A send failure raises, and
  `start` returns 503 rather than a false success — a user staring at an inbox
  that will never fill is worse than an error.
- The email is plain text and short: the code, that it lasts 10 minutes, and
  that nobody at ClipCatalyst will ask for it. Subject carries the code too,
  so a phone notification is enough to read it without opening the mail.

## Frontend

`/account`'s auth card leads with **"Email me a code"**: one email field →
`Send code` → the field is replaced by a 6-digit input (`inputMode="numeric"`,
`autoComplete="one-time-code"` so iOS and Android offer it from the
notification) → `Verify`. A "use a different address" link goes back, and
`Resend code` appears after 30 s with a countdown.

Password and Google remain, below a divider. When no mailer is configured the
code option is not rendered at all — same discipline as the Google button.

`lib/account.ts`: `startEmailCode(email)`, `verifyEmailCode(email, code)`.

## Tests (`api/tests/test_email_auth.py`)

Round trip creates a password-less account and signs in; a second round trip
signs into the same account with no duplicate row; an existing password
account is signed into without touching its hash; wrong code → 401 and
`attempts` increments; the 6th attempt is refused and the row is gone;
an expired code → 401; a used code cannot be reused; a second `start`
invalidates the first code; the per-address hourly cap → 429; the per-client
limiter applies to both routes; `CC_MAILER=none` → 503 on start and no row
written; the stored value is not the code (assert the plaintext appears
nowhere in the DB file); `start` responses for an existing and a non-existent
address are byte-identical; the console mailer is the only path that renders a
code, and the resend mailer is exercised against a stubbed HTTP layer
including a failure → 503.

## Non-negotiables

- No endpoint ever reveals whether an address has an account.
- A code is never stored, logged, or returned in plaintext outside the console
  mailer.
- A failed send is an error, never a silent success.
- Existing email/password and Google sign-in are untouched; all current suites
  stay green.

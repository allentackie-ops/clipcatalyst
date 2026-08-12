"""Account email: the one place ClipCatalyst sends mail (EMAILAUTH.md).

Three backends behind one call, chosen by ``CC_MAILER``:

  * ``none`` — nothing can be sent. Callers must check ``is_configured``
    FIRST and answer an honest 503; this module will not invent a delivery.
  * ``console`` — writes the whole message, code included, to the API log.
    **Development only.** It is the one place in the entire system where a
    sign-in code exists in plaintext outside the minute it is typed, so a box
    where anybody but the developer can read the log must not run it.
  * ``resend`` — POSTs the message to Resend's HTTP API.

The rule the module exists to keep, and the reason sending is a raise rather
than a bool: **a failed send is an error, never a silent success.** A user
staring at an inbox that will never fill has no way to tell that from a slow
mail server; they wait, then they leave. An error tells them to try again.
So every failure path here — an unreachable API, a refused key, a backend
name nobody implements — ends in ``MailError``, and ``/v1/auth/email/start``
turns that into a 503 AFTER deleting the code it had staged.

Nothing here decides who may sign in. It renders a message and hands it to a
transport; ``main.py`` owns the account rules.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Mapping

from .settings import Settings

logger = logging.getLogger(__name__)

#: Resend's transactional send endpoint.
RESEND_URL = "https://api.resend.com/emails"

#: Ten seconds, as the spec sets. Long enough for a normal API call, short
#: enough that a hung provider does not pin a request thread — /v1/auth/email/
#: start is a `def` route, so it runs in the anyio threadpool and a send that
#: never returns would hold a worker for as long as the kernel would wait.
_TIMEOUT_S = 10

#: Nothing useful lives past this in a provider's reply; the cap is here so a
#: misbehaving endpoint cannot stream a response into our memory.
_MAX_RESPONSE_BYTES = 64 * 1024

#: What a redacted code looks like in a log line (see _redact).
_REDACTED = "[code redacted]"


class MailError(RuntimeError):
    """The message was not delivered, whatever the reason.

    Every cause collapses to one answer at the HTTP edge — 503, "we could not
    send it" — so the message is for the server log and never for the client.
    """


def is_configured(settings: Settings) -> bool:
    """Can this box send account email at all?

    ``CC_MAILER=none`` (the default, and any unset value) is False and means
    the email-code door does not exist on this deployment: the endpoint 503s
    and the frontend renders no code option, exactly as an unset
    ``CC_GOOGLE_CLIENT_ID`` removes the Google button.

    A name we do not recognise reads as True on purpose. An operator who typed
    ``CC_MAILER=resnd`` asked for email, and the honest answer to that is a
    loud failure on the first send (see ``send_login_code``) naming the value —
    not a silent slide back into "email is off", which looks exactly like a
    box that was never meant to have it.
    """
    return settings.mailer.strip().lower() not in ("", "none")


def _redact(text: str, code: str) -> str:
    """`text` with the code taken out of it.

    Applied to everything a provider says back before it reaches a log. No
    reply we know of echoes the request body, but "no provider does that
    today" is not a property this code can enforce, and a code in a log file
    outlives the ten minutes it was supposed to exist for.
    """
    return text.replace(code, _REDACTED) if code else text


def _minutes(count: int) -> str:
    return "1 minute" if count == 1 else f"{count} minutes"


def login_code_message(code: str, ttl_minutes: int) -> tuple[str, str]:
    """The (subject, body) of a sign-in code email. Plain text, and short.

    The code is in the SUBJECT as well as the body, which is the whole point:
    a phone notification shows the subject line, so the code can be read
    without unlocking anything or opening the mail. The body says how long it
    lasts — from the configured TTL, so the sentence cannot drift from what
    the server enforces — and that nobody here will ever ask for it, because
    the code is exactly what a support-desk impersonator would ask for.
    """
    subject = f"{code} is your ClipCatalyst sign-in code"
    body = (
        "Your ClipCatalyst sign-in code is:\n"
        f"\n    {code}\n\n"
        f"It works once and expires in {_minutes(ttl_minutes)}.\n\n"
        "Nobody at ClipCatalyst will ever ask you for this code. If you did "
        "not try to sign in, you can ignore this email. Nothing has changed "
        "and no account was created.\n"
    )
    return subject, body


def post_json(
    url: str, payload: Mapping[str, object], headers: Mapping[str, str]
) -> tuple[int, str]:
    """POST JSON and return (status, body-as-text). The ONE network call here.

    A plain module-level function for the same reason ``googleid.fetch_jwks``
    is one: the tests replace it with a stub, so the whole resend path — the
    headers, the payload shape, the success and failure branches — is
    exercised for real instead of being mocked away wholesale.

    An HTTP error status is a RETURN, not a raise: 4xx/5xx is the provider
    answering, and the caller decides what a non-2xx means. Transport failures
    (DNS, connect, timeout, TLS) do raise, and the caller turns them into
    MailError.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return int(response.status), response.read(_MAX_RESPONSE_BYTES).decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as error:
        # Still the provider talking — read its explanation and hand it back.
        return int(error.code), error.read(_MAX_RESPONSE_BYTES).decode(
            "utf-8", "replace"
        )


def _send_console(settings: Settings, to: str, subject: str, body: str) -> None:
    """DEV ONLY: write the message — code and all — to the API log.

    This is the one code path in ClipCatalyst that renders a sign-in code in
    plaintext, and it exists so a developer with no mail provider can complete
    the flow. It is never reached on a box whose CC_MAILER says anything else.
    """
    logger.info(
        "CC_MAILER=console, so this mail is printed instead of sent.\n"
        "  From: %s\n  To: %s\n  Subject: %s\n\n%s",
        settings.mail_from,
        to,
        subject,
        body,
    )


def _send_resend(
    settings: Settings, to: str, subject: str, body: str, code: str
) -> None:
    """Deliver via Resend; raise MailError on anything short of accepted."""
    api_key = settings.resend_api_key.strip()
    if not api_key:
        raise MailError("CC_MAILER=resend but CC_RESEND_API_KEY is empty")
    try:
        status, reply = post_json(
            RESEND_URL,
            {
                "from": settings.mail_from,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            {"Authorization": f"Bearer {api_key}"},
        )
    except Exception as error:  # noqa: BLE001 - every transport failure is "not sent"
        # A refused connection, a DNS failure, a TLS error and a timeout are
        # one event to the caller: the mail did not go. Narrowing this would
        # turn some of them into a 500 on an unauthenticated route, which is a
        # worse answer than the honest 503.
        raise MailError(
            f"resend request failed: {type(error).__name__}: {error}"
        ) from error
    if status < 200 or status >= 300:
        # Redacted before it is quoted anywhere: see _redact.
        raise MailError(
            f"resend refused the message (HTTP {status}): {_redact(reply, code)[:500]}"
        )


def send_login_code(settings: Settings, to: str, code: str) -> None:
    """Send one sign-in code to one address, or raise MailError.

    `code` is passed in rather than generated here so there is exactly one
    generator (``auth.new_login_code``) and one place that knows a code at
    all. It is never logged by this function — only the console backend, which
    IS the log, ever renders it.
    """
    subject, body = login_code_message(code, settings.email_code_ttl_minutes)
    backend = settings.mailer.strip().lower()
    if backend == "console":
        _send_console(settings, to, subject, body)
        return
    if backend == "resend":
        _send_resend(settings, to, subject, body, code)
        return
    # Reached only for a backend nobody implements — `none` never gets here,
    # because callers check is_configured before staging a code at all.
    raise MailError(f"CC_MAILER={settings.mailer!r} is not a mailer this build can use")

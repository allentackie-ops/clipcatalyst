"""The YouTube adapter: a clip on somebody's channel (PUBLISH.md Part 3).

Implements `PublishTarget` against YouTube Data API v3's **resumable** upload,
which is three ideas and one loop:

  1. ``POST /upload/youtube/v3/videos?uploadType=resumable`` with the metadata
     and no bytes. YouTube answers 200 and a ``Location`` — a session URL that
     is itself a bearer capability for that channel, which is why it never
     touches the database or a log line and lives only inside one task run.
  2. ``PUT`` the bytes to that URL in chunks, each one carrying a
     ``Content-Range`` that says exactly which slice of the file it is.
  3. A **308** means "still going, and here is what I actually have" — which is
     not always what we sent. Honouring the ``Range`` it carries, rather than
     assuming our own arithmetic, is what makes the upload resumable at all: a
     connection that dropped halfway through a chunk is a normal event, and the
     server's count is the only count that is true.

A 5xx is the other normal event, and the answer is to send the same chunk
again: ``Content-Range`` names the slice, so a retry is idempotent — bytes the
server already has are not appended twice. Up to `CHUNK_RETRIES` of those, then
we stop, because a provider that has failed four times is having an outage and
hammering it is not a strategy.

**All provider HTTP goes through ``http_request``** — the seam ``mailer.
post_json`` and ``connections.http_request`` already establish — so the whole
protocol above (the init, the 308s, the retries, the quota refusal) is driven
against a stub in the tests, for real, with no network.

Two product facts shape the rest:

  * Every upload from an app whose ``youtube.upload`` scope Google has not yet
    reviewed is FORCED to `private` — by Google, not by us. `capability` says
    so, and the UI reads it, so nothing anywhere offers a switch that would be
    silently overridden.
  * The daily upload quota on a default project is a handful of videos. So a
    403 that means "quota" is not a generic failure; it is a specific sentence
    with a specific answer (`QUOTA_MESSAGE`), because the difference between
    those two is whether the user tries again tomorrow or gives up on the
    feature.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping

from .. import connections
from ..settings import Settings
from . import Capability, ProgressFn, PublishError, PublishRequest, PublishResult

logger = logging.getLogger(__name__)

#: Where a resumable upload is opened. `part` names the halves of the video
#: resource we are sending: the snippet (title, description, category) and the
#: status (privacy).
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
UPLOAD_PARAMS = "uploadType=resumable&part=snippet,status"

#: Where a finished upload can be watched. Built rather than taken from the
#: reply because the API returns an id, not a link.
WATCH_URL = "https://www.youtube.com/watch?v="

#: People & Blogs — PUBLISH.md's choice, and the right one for a talking clip.
CATEGORY_ID = "22"

#: YouTube's own limits. Exceeding either is a 400 with an unhelpful body, so
#: the metadata is cut to fit before it is ever sent.
MAX_TITLE = 100
MAX_DESCRIPTION = 5000

#: Bytes per PUT. A multiple of 256 KiB, which YouTube requires of every chunk
#: but the last. 8 MiB sends a typical 30-second vertical clip in one or two
#: requests while keeping a retry cheap.
CHUNK_BYTES = 8 * 1024 * 1024

#: How many times ONE chunk may be sent again after a 5xx or a dropped
#: connection — three retries, so four attempts in all (PUBLISH.md).
CHUNK_RETRIES = 3

#: Waits between those attempts. Short, because a publish is something a person
#: is watching happen.
BACKOFF_S = (0.5, 1.0, 2.0)

#: A 308 that does not move the offset forward means the server is holding
#: exactly what it held last time. Twice is a hiccup; this many in a row is a
#: loop, and looping forever inside a Celery task is worse than failing.
MAX_STALLED_RESUMES = 3

_TIMEOUT_S = 60  # a chunk PUT is bytes on the wire, not a JSON round trip
_MAX_RESPONSE_BYTES = 64 * 1024

#: Values of ``CC_YOUTUBE_VERIFIED`` that mean the review has landed. Opt-IN
#: (rather than an off-list like CC_DIARIZATION's) on purpose: a typo must fall
#: back to the cautious answer. Reading "verifed" as verified would have the
#: product offering public posting it cannot actually perform.
_ON_VALUES = {"1", "on", "true", "yes", "verified"}

#: Google's names for "you have run out". `uploadLimitExceeded` is the one that
#: actually bites on a default project, and it can arrive as a 400 as well as a
#: 403, which is why the reason is what is matched rather than the status.
_QUOTA_REASONS = {
    "quotaexceeded",
    "uploadlimitexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
}

# --- the sentences a creator reads ----------------------------------------- #

#: PUBLISH.md names this one exactly: a quota refusal must say what happened
#: and when to come back, never "upload failed".
QUOTA_MESSAGE = (
    "YouTube's daily upload limit for this app was reached. Try tomorrow."
)
_UNREACHABLE = (
    "We couldn't reach YouTube to finish this upload. Please try again in a "
    "few minutes."
)
_REFUSED = (
    "YouTube wouldn't accept this upload. Check that your channel is in good "
    "standing and able to upload, then try again."
)
_REJECTED_DETAILS = (
    "YouTube rejected this post's details. Try a shorter title or description."
)
_SESSION_LOST = (
    "The upload to YouTube was interrupted and its session has expired. "
    "Please post the clip again."
)
_EMPTY_CLIP = (
    "This clip's video file is empty, so there's nothing to upload. Re-render "
    "the clip and try again."
)
_NO_KEY = (
    "This server can't read your channel's saved tokens right now "
    "(CC_TOKEN_KEY is unset or invalid), so nothing was uploaded."
)

#: Shown wherever a post to YouTube is offered, and true of the code: an
#: unverified sensitive scope means Google itself forces `private`.
UNVERIFIED_NOTE = (
    "Until Google finishes reviewing this app, uploads land on your channel as "
    "private videos. You publish them from YouTube."
)
VERIFIED_NOTE = (
    "Posts go straight to your channel with the visibility you choose here."
)

#: The last-resort title. A video needs one, and an untitled upload is worse
#: than a generic one.
_FALLBACK_TITLE = "ClipCatalyst clip"


def http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], str]:
    """Make one call to YouTube; return (status, response headers, body text).

    The ONE place this module touches the network. Response HEADERS are part of
    the return value because the resumable protocol lives in them — the session
    URL arrives as ``Location`` and every resume point as ``Range`` — so a seam
    that handed back only a body could not express the protocol it is standing
    in for, and the tests would be exercising a different one.

    An HTTP error status is a RETURN, not a raise, exactly as in
    ``connections.http_request``: 308 is the protocol working, 403 may be a
    quota, 5xx is retryable, and telling those apart is the caller's job.
    Transport failures (DNS, connect, timeout, a dropped socket mid-chunk) do
    raise, and `_send` turns them into the same retry a 5xx gets.

    Redirects are NOT followed. 308 is the resumable protocol's own status, and
    a redirect handler would either chase it or choke on the missing
    ``Location``; either way the upload would silently re-send the video.
    """
    request = urllib.request.Request(url, data=body, method=method.upper())
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with _opener().open(request, timeout=_TIMEOUT_S) as response:
            return (
                int(response.status),
                _headers(response.headers),
                response.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as error:
        return (
            int(error.code),
            _headers(error.headers),
            error.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace"),
        )


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, turning it back into a status we can read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001,ANN201,D102
        return None


_OPENER: urllib.request.OpenerDirector | None = None


def _opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_NoRedirects)
    return _OPENER


def _headers(raw) -> dict[str, str]:  # noqa: ANN001 - a header mapping
    """Response headers, lower-cased, so lookups don't depend on the casing.

    HTTP header names are case-insensitive and the whole protocol below is
    header lookups, so a ``Location`` that came back spelled differently would
    read as an upload session that was never opened.
    """
    return {str(name).lower(): str(value) for name, value in dict(raw).items()}


#: Patched by the tests so a retry costs no wall-clock time. A module-level
#: name rather than a parameter for the same reason `http_request` is one.
pause = time.sleep


def _payload(body: str) -> dict:
    """A reply as a dict; {} when it is not a JSON object."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_fields(payload: Mapping[str, object]) -> tuple[str, set[str]]:
    """(message, reasons) out of a Google API error body.

    NAMED FIELDS ONLY — never the whole body, and never anything echoed back
    from the request. `connections._reason` takes the same care for the same
    reason: the difference between logging a failure and logging a reply is one
    edit by somebody who does not know that a success carries a token.
    """
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return "", set()
    message = str(error.get("message") or "")
    reasons = {str(error.get("status") or "").strip().lower()}
    raw = error.get("errors")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                reasons.add(str(item.get("reason") or "").strip().lower())
    return message, {reason for reason in reasons if reason}


def _is_quota(status: int, message: str, reasons: set[str]) -> bool:
    """Is this refusal "you have used up your uploads for today"?

    Matched on the REASON rather than the status: `uploadLimitExceeded` arrives
    as a 400 as often as a 403, and a 403 on its own is just as likely to mean
    a channel that is not allowed to upload — two refusals with two different
    answers, so guessing from the status alone would put the wrong one in front
    of half the users who hit either.
    """
    if reasons & _QUOTA_REASONS:
        return True
    if status == 403 and "resource_exhausted" in reasons:
        return True
    # Google's YouTube-specific quota errors are consistent about the reason,
    # but the message is the only signal on a few of the older shapes.
    return status in (400, 403) and "quota" in message.lower()


def _refusal(status: int, body: str) -> PublishError:
    """The sentence for a refusal YouTube is not going to change its mind on."""
    payload = _payload(body)
    message, reasons = _error_fields(payload)
    if _is_quota(status, message, reasons):
        logger.warning("youtube refused an upload for quota: %s", message or status)
        return PublishError(QUOTA_MESSAGE)
    logger.warning(
        "youtube refused an upload: HTTP %s %s %s",
        status,
        sorted(reasons) or "",
        message,
    )
    if status == 401:
        # The grant died between the token refresh and this call. The user has
        # to authorize again, and that message already exists.
        return PublishError(connections.RECONNECT_REQUIRED)
    if status in (404, 410):
        return PublishError(_SESSION_LOST)
    if status == 400:
        return PublishError(_REJECTED_DETAILS)
    return PublishError(_REFUSED)


def _send(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None = None,
    what: str,
) -> tuple[int, dict[str, str], str]:
    """One request, retried on 5xx and on a dropped connection.

    Up to CHUNK_RETRIES retries (four attempts in all). Safe to retry because
    every request this module makes says which slice of the file it is: the
    init carries no bytes, and a chunk carries ``Content-Range``, so YouTube
    ignores what it already has rather than appending it twice.

    Exhausting the retries raises `_UNREACHABLE`, which is the honest thing to
    tell somebody: nothing is wrong with their clip.
    """
    last = ""
    for attempt in range(CHUNK_RETRIES + 1):
        try:
            status, raw_headers, text = http_request(
                method, url, headers=headers, body=body
            )
        except Exception as error:  # noqa: BLE001 - every transport failure is "no answer"
            # A reset socket mid-chunk, a DNS failure, a timeout: one event to
            # us, and the same answer as a 503 — send it again.
            last = f"{type(error).__name__}: {error}"
        else:
            if status < 500:
                # Normalised again on the way out, because the seam is a
                # function and a caller's answer is not this module's to assume.
                return status, _headers(raw_headers), text
            last = f"HTTP {status}"
        if attempt < CHUNK_RETRIES:
            logger.info(
                "youtube %s failed (%s); retrying (%d/%d)",
                what,
                last,
                attempt + 1,
                CHUNK_RETRIES,
            )
            pause(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
    logger.warning("youtube %s failed %d times: %s", what, CHUNK_RETRIES + 1, last)
    raise PublishError(_UNREACHABLE)


def _clean(text: str) -> str:
    """Metadata text YouTube will accept, with the characters it refuses gone.

    ``<`` and ``>`` are an outright 400 (`invalidTitle` / `invalidDescription`)
    on YouTube's side, and a clip's title comes out of a TRANSCRIPT — text the
    product did not write and cannot vouch for. Refusing the post over a
    character we can simply drop would be blaming the user for their own words.
    """
    return text.replace("<", "").replace(">", "")


def _one_line(text: str) -> str:
    """A title's worth of text: no newlines, no runs of whitespace."""
    return " ".join(text.split())


def _top_hook(clip: Mapping[str, object]) -> str:
    """The clip's best hook, or '' — what a blank description falls back to."""
    hooks = clip.get("hooks")
    if isinstance(hooks, (list, tuple)):
        for hook in hooks:
            text = _one_line(str(hook or ""))
            if text:
                return text
    return ""


def build_title(request: PublishRequest) -> str:
    """The video's title: what was typed, else the clip's, cut to 100.

    The cut is at YouTube's limit rather than at a friendlier one, and it is a
    plain truncation: this is the title a creator can edit in Studio in five
    seconds, and quietly renaming their clip to something shorter would be a
    worse surprise than a long one ending early.
    """
    typed = _one_line(_clean(request.title))
    fallback = _one_line(_clean(str(request.clip.get("title") or "")))
    title = typed or fallback or _FALLBACK_TITLE
    return title[:MAX_TITLE].rstrip() or _FALLBACK_TITLE


def build_description(request: PublishRequest) -> str:
    """The description: the user's caption, or the clip's top hook if blank.

    PUBLISH.md's rule, and the reason for it is that an empty description is a
    wasted field on a platform that reads it — the hook is the one line the
    analysis already believes is the best thing about this clip.
    """
    typed = _clean(request.description).strip()
    return (typed or _clean(_top_hook(request.clip)))[:MAX_DESCRIPTION]


def _verified(settings: Settings) -> bool:
    return str(getattr(settings, "youtube_verified", "off")).strip().lower() in _ON_VALUES


class YouTubeTarget:
    """`PublishTarget` for YouTube. Stateless — one instance, shared."""

    platform = "youtube"

    def capability(self, settings: Settings) -> Capability:
        """What a post can do here: one option, or three.

        While the review is outstanding this returns a list of exactly ONE
        privacy value, so a UI built by rendering the list cannot offer a
        choice that does not exist — which is the whole difference between
        "the UI says uploads are private" and "the UI cannot say otherwise".
        """
        if _verified(settings):
            return Capability(
                privacy_choices=("public", "unlisted", "private"),
                forced="",
                note=VERIFIED_NOTE,
            )
        return Capability(
            privacy_choices=("private",), forced="private", note=UNVERIFIED_NOTE
        )

    def metadata(self, request: PublishRequest) -> dict:
        """The video resource we open the upload with.

        `selfDeclaredMadeForKids` is deliberately absent. It is a legal
        declaration about somebody else's content, made to a regulator, and
        this product has no way to know the answer — so YouTube asks the
        creator for it in Studio instead of us guessing on their behalf.
        """
        return {
            "snippet": {
                "title": build_title(request),
                "description": build_description(request),
                "categoryId": CATEGORY_ID,
            },
            "status": {"privacyStatus": request.privacy},
        }

    def publish(
        self,
        settings: Settings,
        connection: Mapping[str, object],
        request: PublishRequest,
        on_progress: ProgressFn,
    ) -> PublishResult:
        """Upload one clip to the connected channel. Raises PublishError."""
        if request.size_bytes <= 0:
            raise PublishError(_EMPTY_CLIP)
        access = self._access_token(settings, connection)
        session_url = self._open_session(access, request)
        video_id = self._upload(session_url, access, request, on_progress)
        logger.info(
            "published clip %s to youtube as %s (%s)",
            request.clip.get("id"),
            video_id,
            request.privacy,
        )
        return PublishResult(video_id=video_id, url=f"{WATCH_URL}{video_id}")

    # -- the three steps ---------------------------------------------------- #

    def _access_token(
        self, settings: Settings, connection: Mapping[str, object]
    ) -> str:
        """A live token, with every way of not getting one turned into prose.

        ``connections.access_token`` refreshes and persists on its own; what it
        raises are three genuinely different events, and each has its own
        sentence. NeedsReconnect already carries one — it is written for
        exactly this moment — so it is passed through rather than reworded.
        """
        try:
            return connections.access_token(settings, connection)
        except connections.NeedsReconnect as error:
            raise PublishError(str(error)) from error
        except connections.TokenKeyUnavailable as error:
            raise PublishError(_NO_KEY) from error
        except connections.ProviderUnavailable as error:
            logger.warning("youtube token refresh unavailable: %s", error)
            raise PublishError(_UNREACHABLE) from error

    def _open_session(self, access: str, request: PublishRequest) -> str:
        """Start the resumable upload; return the session URL it hands back."""
        body = json.dumps(self.metadata(request)).encode("utf-8")
        status, headers, text = _send(
            "POST",
            f"{UPLOAD_URL}?{UPLOAD_PARAMS}",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json; charset=UTF-8",
                # What the session is FOR. YouTube uses both to reject an
                # oversized or wrong-typed upload now rather than after we have
                # spent minutes sending it.
                "X-Upload-Content-Length": str(request.size_bytes),
                # Not hard-coded to MP4: a clip saved from the browser engine is
                # frequently WebM, which YouTube accepts and which it refuses
                # when it is announced as something else.
                "X-Upload-Content-Type": request.media_type or "video/mp4",
            },
            body=body,
            what="upload session",
        )
        if status < 200 or status >= 300:
            raise _refusal(status, text)
        session_url = headers.get("location", "")
        if not session_url:
            # 2xx with nowhere to send the bytes. Nothing to retry and nothing
            # to resume — and the reply is NOT logged: this is the one response
            # in the flow whose body would carry the session URL.
            logger.warning("youtube opened an upload session with no Location header")
            raise PublishError(_UNREACHABLE)
        return session_url

    def _upload(
        self,
        session_url: str,
        access: str,
        request: PublishRequest,
        on_progress: ProgressFn,
    ) -> str:
        """PUT the file to the session, honouring 308s. Returns the video id."""
        total = request.size_bytes
        offset = 0
        stalled = 0
        with Path(request.file).open("rb") as handle:
            while offset < total:
                handle.seek(offset)
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    # The file is SHORTER than the length the session was opened
                    # for, so the upload can never complete however long we keep
                    # sending. Somebody truncated it, or the row's size is a lie.
                    logger.warning(
                        "youtube upload ran off the end of the clip at %d/%d bytes",
                        offset,
                        total,
                    )
                    raise PublishError(_EMPTY_CLIP)
                last_byte = offset + len(chunk) - 1
                status, headers, text = _send(
                    "PUT",
                    session_url,
                    headers={
                        # The session URL is authorization enough on its own, but
                        # every official client sends the token with each chunk
                        # too, and a session that expects it 401s without.
                        "Authorization": f"Bearer {access}",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{last_byte}/{total}",
                    },
                    body=chunk,
                    what="chunk",
                )
                if status in (200, 201):
                    on_progress(1.0, "Finishing up")
                    return _video_id(text)
                if status != 308:
                    raise _refusal(status, text)

                # 308: still going. The server's own count wins over ours — a
                # chunk it only partly received is exactly what this status is
                # for, and trusting our arithmetic instead would leave a hole
                # in the middle of somebody's video.
                received = _resume_offset(headers)
                if received <= offset:
                    stalled += 1
                    if stalled >= MAX_STALLED_RESUMES:
                        logger.warning(
                            "youtube kept resuming at %d of %d bytes; giving up",
                            received,
                            total,
                        )
                        raise PublishError(_UNREACHABLE)
                else:
                    stalled = 0
                offset = received
                on_progress(
                    min(0.99, offset / total) if total else 0.0, "Uploading to YouTube"
                )
        # Every byte is accounted for and YouTube never answered with the video.
        # Not a refusal and not something to retry into — the upload is in a
        # state only YouTube can resolve, so the honest answer is "try again".
        logger.warning("youtube holds all %d bytes but never finished the upload", total)
        raise PublishError(_UNREACHABLE)


def _resume_offset(headers: Mapping[str, str]) -> int:
    """Where the next chunk starts, from a 308's ``Range: bytes=0-<last>``.

    No Range header means YouTube has nothing yet — its documented way of
    saying "start over" — so the honest answer is 0, not "carry on where we
    thought we were", which would upload the tail of a file whose head is
    missing and leave a video that cannot be played.
    """
    raw = str(headers.get("range") or "").strip()
    if not raw:
        return 0
    _, _, span = raw.partition("=")
    _, _, last = span.partition("-")
    try:
        return int(last) + 1
    except (TypeError, ValueError):
        return 0


def _video_id(body: str) -> str:
    """The id out of a finished upload's video resource."""
    video_id = str(_payload(body).get("id") or "")
    if not video_id:
        # The bytes are on YouTube but we cannot say where. Surfacing it as a
        # failure would be worse than useless — the user would post again and
        # get a duplicate — so this is deliberately loud in the log and
        # unrecoverable here.
        logger.error("youtube accepted an upload but named no video id")
        raise PublishError(
            "YouTube accepted the upload but didn't say where it landed. "
            "Check your channel before posting this clip again."
        )
    return video_id


#: The one instance. Stateless, so there is nothing to construct per publish.
TARGET = YouTubeTarget()

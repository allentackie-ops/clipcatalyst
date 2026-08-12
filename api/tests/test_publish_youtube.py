"""Posting a clip to YouTube (PUBLISH.md Part 3), against a stubbed provider.

Nothing here is mocked away except the network, and the network is replaced at
the two seams the product actually has: ``connections.http_request`` (the OAuth
half — a token refresh in the middle of a publish is an ordinary event) and
``publish.youtube.http_request`` (the upload half). Everything else runs for
real — a real library clip written by a real upload route, real Fernet
ciphertext, real SQLite rows, the real Celery task in eager mode, and the real
resumable state machine driven byte by byte against a fake YouTube that counts
what it received.

That counting is the point of the file. A resumable upload is not "POST the
file"; it is a negotiation in which the SERVER decides how much of what you
sent it actually kept, and the three ways that goes wrong are the three ways a
creator ends up with a truncated video on their channel:

  * §3 a 308 that reports fewer bytes than we sent — the resume must follow the
    server's count, not our arithmetic;
  * §4 a 5xx mid-chunk — the same chunk goes again, and lands ONCE, because
    ``Content-Range`` says which slice it is;
  * §5 a 403 that means "no uploads left today" — a specific sentence with a
    specific answer, never a generic failure on a feature whose whole quota is
    a handful of videos a day.

The rest is refusals that must be honest (§6-§7: an expired clip, somebody
else's clip, no channel connected) and the promise that nothing secret leaks
into the row a browser polls (§8) — the resumable session URL is a bearer
capability for somebody's channel, and it is treated like one.

Mirrors the env/import dance of ``test_connections.py``: all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules are purged for a clean
re-import. The whole os.environ is snapshotted and restored around each client.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest
from cryptography.fernet import Fernet

# Distinctive on purpose: §8 greps the database file and the API's own
# responses for these exact strings.
ACCESS_TOKEN = "ya29.PUBLISH-ACCESS-TOKEN-NEVER-IN-PLAINTEXT"
REFRESH_TOKEN = "1//PUBLISH-REFRESH-TOKEN-NEVER-IN-PLAINTEXT"
REFRESHED_ACCESS_TOKEN = "ya29.PUBLISH-SECOND-ACCESS-TOKEN-ALSO-SECRET"

#: The resumable session URL. A bearer capability for the channel — anybody
#: holding it can put bytes on it — so it is as much a secret as the tokens.
SESSION_URL = "https://upload.youtube.test/resumable/SESSION-URL-IS-A-CAPABILITY"
VIDEO_ID = "dQw4w9WgXcQ"

CHANNEL_ID = "UC_publish_channel_0001"
CHANNEL_NAME = "Creator Studio"

CLIENT_ID = "1234567890-publish.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-client-secret"
API_ORIGIN = "https://api.clipcatalyst.test"
FRONTEND_ORIGIN = "https://app.clipcatalyst.test"

PASSWORD = "correct-horse-battery"

# The magic that makes an uploaded byte string a clip (main._sniff_clip_type).
# A clip saved from the browser engine is whatever the device could record, so
# both containers really do end up in a library.
MP4_MAGIC = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00"
WEBM_MAGIC = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1fB\x82\x84webm"

# The modules that snapshot settings at import time, and only those.
#
# `clipcatalyst_api.publish` and its adapters are deliberately NOT here. They
# read settings live, so there is nothing to re-import for — and re-importing
# them would be actively wrong: purging a submodule from sys.modules does not
# unset the attribute the parent package holds, so `from . import publish`
# inside worker.py would keep the OLD module while `from clipcatalyst_api.
# publish import youtube` below re-imported a new one. The adapter would then
# raise a PublishError the worker's `except` clause had never heard of, and
# every honest failure message in this file would quietly become the worker's
# generic one.
_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
    "clipcatalyst_api.auth",
    "clipcatalyst_api.connections",
)


def _purge() -> None:
    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    for name in _SNAPSHOT_MODULES:
        sys.modules.pop(name, None)


def _set_env(**values: str | None) -> None:
    """Flip CC_* vars at runtime; every route reads get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


def _clip_bytes(size: int = 4112, magic: bytes = MP4_MAGIC) -> bytes:
    """A byte string that sniffs as a clip and is exactly `size` long."""
    assert size >= len(magic)
    filler = bytes(range(256)) * (size // 256 + 1)
    return (magic + filler)[:size]


# --------------------------------------------------------------------------- #
# The two stubs. One provider each for the two halves of a publish.
# --------------------------------------------------------------------------- #


class FakeGoogleOAuth:
    """``connections.http_request``: enough Google to connect and to refresh."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.access_token = ACCESS_TOKEN
        self.refresh_token = REFRESH_TOKEN
        self.expires_in: object = 3600
        self.token_status = 200

    @property
    def refreshes(self) -> list[dict]:
        return [
            call
            for call in self.calls
            if call["kind"] == "token" and call["form"].get("refresh_token")
        ]

    def __call__(self, method, url, *, headers=None, form=None):  # noqa: ANN001,ANN204
        kind = (
            "token"
            if url.endswith("/token")
            else "revoke"
            if url.endswith("/revoke")
            else "identity"
            if "youtube/v3/channels" in url
            else "unknown"
        )
        self.calls.append(
            {"kind": kind, "url": url, "form": dict(form or {}), "headers": dict(headers or {})}
        )
        if kind == "identity":
            return 200, json.dumps(
                {"items": [{"id": CHANNEL_ID, "snippet": {"title": CHANNEL_NAME}}]}
            )
        if kind == "revoke":
            return 200, "{}"
        if kind != "token":
            return 404, json.dumps({"error": "not_found"})
        if self.token_status != 200:
            return self.token_status, json.dumps({"error": "invalid_grant"})
        body: dict = {"access_token": self.access_token, "token_type": "Bearer"}
        if self.refresh_token:
            body["refresh_token"] = self.refresh_token
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        body["scope"] = "https://www.googleapis.com/auth/youtube.upload"
        return 200, json.dumps(body)


class FakeYouTubeUpload:
    """``publish.youtube.http_request``: a resumable endpoint that keeps count.

    It behaves like the real one in the way that matters — it decides how much
    of each chunk it kept and reports that back in a 308's ``Range`` — so the
    adapter's resume arithmetic is tested against a server that disagrees with
    it rather than against one that always agrees.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        #: Everything the "server" has actually stored.
        self.received = bytearray()
        self.session_url = SESSION_URL
        self.include_location = True
        self.init_status = 200
        self.init_body = "{}"
        self.video_id = VIDEO_ID
        #: Answers to give the next chunk PUTs, one per attempt, consumed in
        #: order: an int status, a (status, body) pair, or "drop" for a socket
        #: that dies mid-chunk.
        self.chunk_faults: list = []
        #: Bytes of the NEXT chunk to actually keep; None = all of it. Set to a
        #: number to make the server accept a partial chunk exactly once.
        self.accept_bytes: int | None = None
        #: What the upload was opened with.
        self.metadata: dict | None = None
        self.init_headers: dict = {}
        self.chunk_ranges: list[str] = []
        self.authorizations: list[str] = []

    @property
    def chunks(self) -> list[dict]:
        return [call for call in self.calls if call["kind"] == "chunk"]

    @property
    def inits(self) -> list[dict]:
        return [call for call in self.calls if call["kind"] == "init"]

    def __call__(self, method, url, *, headers=None, body=None):  # noqa: ANN001,ANN204
        headers = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        payload = body or b""
        kind = "init" if url.startswith("https://www.googleapis.com/upload") else (
            "chunk" if url == self.session_url else "unknown"
        )
        self.calls.append(
            {"kind": kind, "method": method, "url": url, "headers": headers,
             "size": len(payload)}
        )
        if kind == "init":
            return self._init(headers, payload)
        if kind == "chunk":
            return self._chunk(headers, payload)
        return 404, {}, json.dumps({"error": {"message": "no such endpoint"}})

    def _init(self, headers: dict, body: bytes) -> tuple[int, dict, str]:
        self.metadata = json.loads(body.decode("utf-8")) if body else None
        self.init_headers = headers
        self.authorizations.append(headers.get("authorization", ""))
        if self.init_status != 200:
            return self.init_status, {}, self.init_body
        location = {"Location": self.session_url} if self.include_location else {}
        return 200, location, "{}"

    def _chunk(self, headers: dict, body: bytes) -> tuple[int, dict, str]:
        content_range = str(headers.get("content-range") or "")
        self.chunk_ranges.append(content_range)
        self.authorizations.append(headers.get("authorization", ""))
        if self.chunk_faults:
            fault = self.chunk_faults.pop(0)
            if fault == "drop":
                raise OSError("stub youtube: the connection died mid-chunk")
            if isinstance(fault, tuple):
                return fault[0], {}, fault[1]
            return fault, {}, json.dumps({"error": {"message": "server error"}})

        span, _, total_text = content_range.partition("/")
        start = int(span.split()[1].split("-")[0])
        total = int(total_text)
        # The real endpoint refuses a chunk that does not continue what it has.
        assert start == len(self.received), (
            f"chunk started at {start} but the server holds {len(self.received)}"
        )
        keep = len(body) if self.accept_bytes is None else min(self.accept_bytes, len(body))
        self.accept_bytes = None
        self.received.extend(body[:keep])
        if len(self.received) >= total:
            return 200, {}, json.dumps({"id": self.video_id, "kind": "youtube#video"})
        return 308, {"Range": f"bytes=0-{len(self.received) - 1}"}, ""


def _rows(db_module, table: str) -> list[dict]:  # noqa: ANN001 - the db module
    with contextlib.closing(db_module._connect()) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


@pytest.fixture()
def sandbox(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """A TestClient with publishing configured and both providers stubbed."""
    saved_env = dict(os.environ)
    data_dir = tmp_path_factory.mktemp("publishdata")
    token_key = Fernet.generate_key().decode("ascii")

    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_PUBLIC_BASE_URL": API_ORIGIN,
            "CC_FRONTEND_ORIGIN": FRONTEND_ORIGIN,
            "CC_BILLING": "off",
            "CC_TOKEN_KEY": token_key,
            "CC_YOUTUBE_CLIENT_ID": CLIENT_ID,
            "CC_YOUTUBE_CLIENT_SECRET": CLIENT_SECRET,
        }
    )
    for name in ("CC_API_TOKEN", "CC_YOUTUBE_VERIFIED"):
        os.environ.pop(name, None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth, connections, db, worker
    from clipcatalyst_api.main import app
    from clipcatalyst_api.publish import youtube
    from clipcatalyst_api.settings import get_settings

    oauth = FakeGoogleOAuth()
    upload = FakeYouTubeUpload()
    monkeypatch.setattr(connections, "http_request", oauth)
    monkeypatch.setattr(youtube, "http_request", upload)
    # A retry must cost the test nothing but a loop iteration.
    monkeypatch.setattr(youtube, "pause", lambda seconds: None)
    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                data_dir=data_dir,
                oauth=oauth,
                upload=upload,
                token_key=token_key,
                connections=connections,
                youtube=youtube,
                worker=worker,
                db=db,
                settings=lambda: get_settings(),
            )
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


# --------------------------------------------------------------------------- #
# Driving it.
# --------------------------------------------------------------------------- #


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _account(client, email: str = "creator@example.com") -> str:
    resp = client.post("/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _connect(sandbox: SimpleNamespace, token: str) -> dict:
    """Run the real OAuth flow so a real connection row exists."""
    start = sandbox.client.post(
        "/v1/connections/youtube/start", headers=_bearer(token)
    )
    assert start.status_code == 200, start.text
    import urllib.parse

    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(start.json()["authorize_url"]).query
    )
    callback = sandbox.client.get(
        "/v1/connections/youtube/callback",
        params={"state": query["state"][0], "code": "4/auth-code-0001"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    return _rows(sandbox.db, "connections")[0]


def _save_clip(
    sandbox: SimpleNamespace,
    token: str,
    *,
    size: int = 4112,
    magic: bytes = MP4_MAGIC,
    **metadata,
) -> dict:
    """Put one clip in the library, with a real file behind it."""
    files: dict = {"file": ("clip.mp4", _clip_bytes(size, magic), "video/mp4")}
    if metadata:
        files["metadata"] = (None, json.dumps(metadata), "application/json")
    resp = sandbox.client.post(
        "/v1/clips/upload", files=files, headers=_bearer(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ready(sandbox: SimpleNamespace, email: str = "creator@example.com", **clip):
    """An account with a connected channel and one saved clip. (token, clip)."""
    token = _account(sandbox.client, email)
    _connect(sandbox, token)
    return token, _save_clip(sandbox, token, **clip)


def _post(sandbox: SimpleNamespace, token: str, clip_id: str, **body):  # noqa: ANN202
    return sandbox.client.post(
        f"/v1/clips/{clip_id}/publish", json=body, headers=_bearer(token)
    )


def _publish(sandbox: SimpleNamespace, token: str, clip_id: str, **body) -> dict:
    resp = _post(sandbox, token, clip_id, **body)
    assert resp.status_code == 202, resp.text
    return resp.json()


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="milliseconds")


def _platform(sandbox: SimpleNamespace, token: str, platform: str = "youtube") -> dict:
    body = sandbox.client.get("/v1/connections", headers=_bearer(token)).json()
    return next(entry for entry in body["platforms"] if entry["platform"] == platform)


# --------------------------------------------------------------------------- #
# 1. The full resumable upload.
# --------------------------------------------------------------------------- #


def test_a_clip_is_uploaded_resumably_and_the_row_says_where_it_landed(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox, title="How to grow an audience")
    body = _publish(sandbox, token, clip["id"])

    assert body["status"] == "done", body
    assert body["error"] is None
    assert body["progress"] == 1.0
    assert body["video_id"] == VIDEO_ID
    assert body["video_url"] == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert body["platform"] == "youtube"
    assert body["clip_id"] == clip["id"]

    # The upload really happened, and the bytes that arrived are the clip's.
    assert len(sandbox.upload.inits) == 1
    assert bytes(sandbox.upload.received) == _clip_bytes()
    # Opened as a resumable session, declaring what was coming.
    init = sandbox.upload.inits[0]
    assert "uploadType=resumable" in init["url"]
    assert "part=snippet,status" in init["url"]
    assert sandbox.upload.init_headers["x-upload-content-length"] == str(len(_clip_bytes()))
    assert sandbox.upload.init_headers["x-upload-content-type"] == "video/mp4"
    # Every request of the upload carries the channel's token — the session URL
    # alone is authorization enough for some, and not for all.
    assert sandbox.upload.authorizations == [f"Bearer {ACCESS_TOKEN}"] * 2
    # And the bytes went to the session URL the init handed back, not to the
    # upload endpoint again.
    assert [call["url"] for call in sandbox.upload.chunks] == [SESSION_URL]
    assert sandbox.upload.chunk_ranges == [f"bytes 0-{len(_clip_bytes()) - 1}/{len(_clip_bytes())}"]


def test_the_polling_route_reports_the_same_publish(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox)
    started = _publish(sandbox, token, clip["id"])

    resp = sandbox.client.get(f"/v1/publishes/{started['id']}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["video_id"] == VIDEO_ID
    assert resp.headers["cache-control"] == "no-store"


def test_a_file_larger_than_one_chunk_is_sent_in_order(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several chunks, each naming its own slice — and the file arrives whole."""
    monkeypatch.setattr(sandbox.youtube, "CHUNK_BYTES", 1024)
    token, clip = _ready(sandbox, size=4112)
    assert _publish(sandbox, token, clip["id"])["status"] == "done"

    assert sandbox.upload.chunk_ranges == [
        "bytes 0-1023/4112",
        "bytes 1024-2047/4112",
        "bytes 2048-3071/4112",
        "bytes 3072-4095/4112",
        "bytes 4096-4111/4112",
    ]
    assert bytes(sandbox.upload.received) == _clip_bytes(4112)


# --------------------------------------------------------------------------- #
# 2. The metadata: what actually lands on the channel.
# --------------------------------------------------------------------------- #


def test_the_metadata_is_the_clips_own_title_and_the_users_caption(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox, title="A clip title nobody typed over")
    _publish(sandbox, token, clip["id"], description="Watch this one.")

    snippet = sandbox.upload.metadata["snippet"]
    assert snippet["title"] == "A clip title nobody typed over"
    assert snippet["description"] == "Watch this one."
    assert snippet["categoryId"] == "22"


def test_a_typed_title_wins_over_the_clips_own(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox, title="The clip's title")
    _publish(sandbox, token, clip["id"], title="What I actually want to call it")
    assert (
        sandbox.upload.metadata["snippet"]["title"] == "What I actually want to call it"
    )


def test_a_title_is_truncated_to_youtubes_hundred_characters(
    sandbox: SimpleNamespace,
) -> None:
    """Longer than YouTube accepts is a 400 from them, so it is cut here."""
    token, clip = _ready(sandbox, title="x" * 250)
    _publish(sandbox, token, clip["id"])
    assert sandbox.upload.metadata["snippet"]["title"] == "x" * 100


def test_a_blank_description_falls_back_to_the_clips_top_hook(
    sandbox: SimpleNamespace,
) -> None:
    """PUBLISH.md's rule — the hook is the best line the analysis found."""
    token, clip = _ready(
        sandbox,
        title="Growing an audience",
        hooks=["Nobody tells you this about the algorithm", "A second hook"],
    )
    _publish(sandbox, token, clip["id"], description="")
    assert sandbox.upload.metadata["snippet"]["description"] == (
        "Nobody tells you this about the algorithm"
    )


def test_a_webm_clip_is_announced_as_webm(sandbox: SimpleNamespace) -> None:
    """A browser clip is whatever the device could record — say so, or be refused."""
    token, clip = _ready(sandbox, magic=WEBM_MAGIC)
    assert _publish(sandbox, token, clip["id"])["status"] == "done"
    assert sandbox.upload.init_headers["x-upload-content-type"] == "video/webm"
    assert bytes(sandbox.upload.received) == _clip_bytes(magic=WEBM_MAGIC)


def test_a_clip_with_no_title_still_gets_one(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox)
    _publish(sandbox, token, clip["id"])
    assert sandbox.upload.metadata["snippet"]["title"] == "ClipCatalyst clip"


def test_angle_brackets_from_a_transcript_do_not_fail_the_post(
    sandbox: SimpleNamespace,
) -> None:
    """A title comes out of somebody's speech; YouTube 400s on < and >."""
    token, clip = _ready(sandbox, title="Why <this> works")
    assert _publish(sandbox, token, clip["id"])["status"] == "done"
    assert sandbox.upload.metadata["snippet"]["title"] == "Why this works"


def test_uploads_are_private_while_the_app_is_unverified(
    sandbox: SimpleNamespace,
) -> None:
    """Google forces it, so asking for `public` cannot be honoured — or hidden."""
    token, clip = _ready(sandbox)
    body = _publish(sandbox, token, clip["id"], privacy="public")

    assert sandbox.upload.metadata["status"]["privacyStatus"] == "private"
    assert body["privacy"] == "private"
    # And the UI is told, rather than left to find out.
    assert "private" in body["note"]
    entry = _platform(sandbox, token)
    assert entry["publishable"] is True
    assert entry["privacy_choices"] == ["private"]
    assert entry["forced_privacy"] == "private"


def test_once_the_app_is_verified_the_users_choice_stands(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox)
    _set_env(CC_YOUTUBE_VERIFIED="on")

    body = _publish(sandbox, token, clip["id"], privacy="public")
    assert sandbox.upload.metadata["status"]["privacyStatus"] == "public"
    assert body["privacy"] == "public"

    entry = _platform(sandbox, token)
    assert entry["privacy_choices"] == ["public", "unlisted", "private"]
    assert entry["forced_privacy"] == ""


def test_a_typo_in_the_verified_flag_keeps_the_cautious_answer(
    sandbox: SimpleNamespace,
) -> None:
    """Reading "verifed" as verified would offer public posting that 403s."""
    token, clip = _ready(sandbox)
    _set_env(CC_YOUTUBE_VERIFIED="verifed")
    assert _publish(sandbox, token, clip["id"], privacy="public")["privacy"] == "private"


def test_a_row_queued_for_public_is_still_forced_private_at_upload_time(
    sandbox: SimpleNamespace,
) -> None:
    """The capability is re-read by the WORKER, not trusted off the row.

    A publish queued while a box was mid-deploy — or a row written by an older
    build — must not be able to carry a `public` past the rule Google enforces.
    """
    token, clip = _ready(sandbox)
    publish_id = "1a" * 16
    sandbox.db.create_publish_job(
        publish_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=clip["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="public",  # never resolved through a capability
    )

    sandbox.worker.publish_clip(publish_id)

    assert sandbox.db.get_publish_job(publish_id)["status"] == "done"
    assert sandbox.upload.metadata["status"]["privacyStatus"] == "private"


# --------------------------------------------------------------------------- #
# 2b. The protocol itself — the promise that TikTok is a module, not a branch.
# --------------------------------------------------------------------------- #


def test_the_adapter_is_reached_only_through_the_protocol(
    sandbox: SimpleNamespace,
) -> None:
    """Everything the queue needs, and a platform name it looks up rather than
    hard-codes."""
    from clipcatalyst_api import publish

    target = publish.target_for("YouTube  ")  # case and spacing are not an API
    assert target is not None
    assert target.platform == "youtube"
    for name in ("capability", "metadata", "publish"):
        assert callable(getattr(target, name))
    # A platform the product knows but cannot post to has no adapter at all —
    # which is what makes `publishable` an honest answer rather than a guess.
    assert publish.target_for("tiktok") is None
    assert publish.capability_for(sandbox.settings(), "tiktok") is None


def test_metadata_is_answerable_without_touching_the_network(
    sandbox: SimpleNamespace,
) -> None:
    """`metadata` is pure, so the rules with edge cases can be read directly."""
    from pathlib import Path as _Path

    from clipcatalyst_api import publish

    target = publish.target_for("youtube")
    request = publish.PublishRequest(
        clip={"title": "  A   title\nwith   whitespace  ", "hooks": ["", "  ", "Hook"]},
        file=_Path("/dev/null"),
        size_bytes=1,
        title="",
        description="",
        privacy="unlisted",
    )
    document = target.metadata(request)

    assert document["snippet"]["title"] == "A title with whitespace"
    # The first hook with anything in it — blank ones are not a description.
    assert document["snippet"]["description"] == "Hook"
    assert document["snippet"]["categoryId"] == "22"
    assert document["status"]["privacyStatus"] == "unlisted"
    assert sandbox.upload.calls == []


# --------------------------------------------------------------------------- #
# 3. The 308 resume: the server's count wins.
# --------------------------------------------------------------------------- #


def test_a_partly_accepted_chunk_resumes_from_where_youtube_says(
    sandbox: SimpleNamespace,
) -> None:
    """The heart of "resumable": trust the Range, not our own arithmetic.

    The stub keeps 1000 of the 4112 bytes it was sent and says so. Resuming
    from 4112 (what we sent) would leave a 3112-byte hole in somebody's video
    and the upload would never complete.
    """
    token, clip = _ready(sandbox)
    sandbox.upload.accept_bytes = 1000

    assert _publish(sandbox, token, clip["id"])["status"] == "done"
    assert sandbox.upload.chunk_ranges == ["bytes 0-4111/4112", "bytes 1000-4111/4112"]
    # The file that arrived is the file that was sent — no hole, no overlap.
    assert bytes(sandbox.upload.received) == _clip_bytes()


def test_a_308_with_no_range_starts_the_upload_over(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YouTube's way of saying it has nothing: resume from zero, not from us."""
    token, clip = _ready(sandbox)
    # A 308 that keeps nothing and reports nothing, once.
    sandbox.upload.chunk_faults = [(308, "")]

    assert _publish(sandbox, token, clip["id"])["status"] == "done"
    assert sandbox.upload.chunk_ranges == ["bytes 0-4111/4112", "bytes 0-4111/4112"]
    assert bytes(sandbox.upload.received) == _clip_bytes()


def test_an_endless_resume_at_the_same_offset_gives_up(
    sandbox: SimpleNamespace,
) -> None:
    """A server that never advances is a loop, and a loop in a task is worse."""
    token, clip = _ready(sandbox)
    sandbox.upload.chunk_faults = [(308, "")] * 10

    body = _publish(sandbox, token, clip["id"])
    assert body["status"] == "failed"
    assert "try again" in body["error"]
    assert len(sandbox.upload.chunks) < 10


# --------------------------------------------------------------------------- #
# 4. A chunk retried on 5xx.
# --------------------------------------------------------------------------- #


def test_a_chunk_is_retried_on_a_5xx_and_lands_once(sandbox: SimpleNamespace) -> None:
    """Three retries are allowed; Content-Range makes each one idempotent."""
    token, clip = _ready(sandbox)
    sandbox.upload.chunk_faults = [500, 503, 500]

    body = _publish(sandbox, token, clip["id"])
    assert body["status"] == "done", body
    # Four attempts at the SAME slice, and the file arrives exactly once.
    assert len(sandbox.upload.chunks) == 4
    assert set(sandbox.upload.chunk_ranges) == {"bytes 0-4111/4112"}
    assert bytes(sandbox.upload.received) == _clip_bytes()


def test_a_dropped_connection_is_retried_like_a_5xx(sandbox: SimpleNamespace) -> None:
    """A socket that dies mid-chunk is the same event as a 503 to the user."""
    token, clip = _ready(sandbox)
    sandbox.upload.chunk_faults = ["drop", "drop"]

    assert _publish(sandbox, token, clip["id"])["status"] == "done"
    assert bytes(sandbox.upload.received) == _clip_bytes()


def test_a_fourth_5xx_stops_the_upload_with_an_honest_message(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox)
    sandbox.upload.chunk_faults = [500, 500, 500, 500, 500]

    body = _publish(sandbox, token, clip["id"])
    assert body["status"] == "failed"
    assert body["error"] == (
        "We couldn't reach YouTube to finish this upload — please try again in "
        "a few minutes."
    )
    # Stopped rather than hammered: four attempts, not five.
    assert len(sandbox.upload.chunks) == 4


def test_the_upload_session_is_opened_again_after_a_5xx(
    sandbox: SimpleNamespace,
) -> None:
    """The init gets the same retry policy — it carries no bytes to duplicate."""
    token, clip = _ready(sandbox)
    sandbox.upload.init_status = 503
    original_init = sandbox.upload._init

    calls = {"n": 0}

    def flaky_init(headers, body):  # noqa: ANN001,ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            return 503, {}, "{}"
        sandbox.upload.init_status = 200
        return original_init(headers, body)

    sandbox.upload._init = flaky_init
    assert _publish(sandbox, token, clip["id"])["status"] == "done"
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# 5. The 403 quota: PUBLISH.md's exact sentence.
# --------------------------------------------------------------------------- #


_QUOTA_BODY = json.dumps(
    {
        "error": {
            "code": 403,
            "message": "The user has exceeded the number of videos they may upload.",
            "errors": [{"reason": "uploadLimitExceeded", "domain": "youtube.video"}],
        }
    }
)
_QUOTA_MESSAGE = "YouTube's daily upload limit for this app was reached — try tomorrow."


def test_a_403_quota_becomes_the_friendly_message(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox)
    sandbox.upload.init_status = 403
    sandbox.upload.init_body = _QUOTA_BODY

    body = _publish(sandbox, token, clip["id"])
    assert body["status"] == "failed"
    assert body["error"] == _QUOTA_MESSAGE
    # Nothing was sent: the refusal came before a byte of video moved.
    assert sandbox.upload.chunks == []
    # And it is not retried — tomorrow is the answer, not four attempts.
    assert len(sandbox.upload.inits) == 1


def test_a_quota_refusal_on_a_chunk_reads_the_same(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox)
    sandbox.upload.chunk_faults = [(403, _QUOTA_BODY)]

    assert _publish(sandbox, token, clip["id"])["error"] == _QUOTA_MESSAGE


def test_a_403_that_is_not_about_quota_says_something_else(
    sandbox: SimpleNamespace,
) -> None:
    """Two refusals with two different answers must not read the same."""
    token, clip = _ready(sandbox)
    sandbox.upload.init_status = 403
    sandbox.upload.init_body = json.dumps(
        {
            "error": {
                "code": 403,
                "message": "The channel is not enabled for uploads.",
                "errors": [{"reason": "forbidden"}],
            }
        }
    )

    error = _publish(sandbox, token, clip["id"])["error"]
    assert error != _QUOTA_MESSAGE
    assert "good standing" in error


def test_a_401_mid_upload_asks_for_a_reconnect(sandbox: SimpleNamespace) -> None:
    """The grant died between the refresh and the upload — an ordinary event."""
    token, clip = _ready(sandbox)
    sandbox.upload.init_status = 401
    sandbox.upload.init_body = json.dumps(
        {"error": {"code": 401, "message": "Invalid Credentials"}}
    )

    assert "reconnect" in _publish(sandbox, token, clip["id"])["error"].lower()


# --------------------------------------------------------------------------- #
# 6. Tokens: refreshed mid-publish, and persisted.
# --------------------------------------------------------------------------- #


def _connection(sandbox: SimpleNamespace) -> dict:
    return _rows(sandbox.db, "connections")[0]


def _decrypted(sandbox: SimpleNamespace, field: str) -> str:
    row = _connection(sandbox)
    return Fernet(sandbox.token_key.encode()).decrypt(row[field].encode()).decode()


def test_an_expiring_token_is_refreshed_persisted_and_used_for_the_upload(
    sandbox: SimpleNamespace,
) -> None:
    """A publish is minutes long; a token with four left would die mid-file."""
    token, clip = _ready(sandbox)
    sandbox.db.update_connection(
        _connection(sandbox)["id"], expires_at=_iso(timedelta(minutes=4))
    )
    sandbox.oauth.access_token = REFRESHED_ACCESS_TOKEN
    sandbox.oauth.refresh_token = ""  # Google sends none on a refresh

    assert _publish(sandbox, token, clip["id"])["status"] == "done"

    # Refreshed once, with the token we held…
    assert len(sandbox.oauth.refreshes) == 1
    assert sandbox.oauth.refreshes[0]["form"]["refresh_token"] == REFRESH_TOKEN
    # …the NEW one was used for every request of the upload…
    assert set(sandbox.upload.authorizations) == {f"Bearer {REFRESHED_ACCESS_TOKEN}"}
    # …and it was persisted, encrypted, so the next publish does not refresh.
    assert _decrypted(sandbox, "access_token_enc") == REFRESHED_ACCESS_TOKEN
    assert _decrypted(sandbox, "refresh_token_enc") == REFRESH_TOKEN
    assert REFRESHED_ACCESS_TOKEN.encode() not in _database_bytes(sandbox)

    sandbox.upload.received = bytearray()
    second = _save_clip(sandbox, token)
    assert _publish(sandbox, token, second["id"])["status"] == "done"
    assert len(sandbox.oauth.refreshes) == 1


def test_a_revoked_grant_fails_the_publish_and_asks_for_a_reconnect(
    sandbox: SimpleNamespace,
) -> None:
    """They removed us from their Google account. Nothing here should crash."""
    token, clip = _ready(sandbox)
    sandbox.db.update_connection(
        _connection(sandbox)["id"], expires_at=_iso(timedelta(seconds=-1))
    )
    sandbox.oauth.token_status = 400

    body = _publish(sandbox, token, clip["id"])
    assert body["status"] == "failed"
    assert "reconnect" in body["error"].lower()
    # The dead connection is gone, so the account page already agrees with the
    # message the user is reading.
    assert _rows(sandbox.db, "connections") == []
    assert sandbox.upload.calls == []


def test_unreadable_stored_tokens_fail_the_publish_rather_than_crashing_it(
    sandbox: SimpleNamespace,
) -> None:
    """A rotated key must degrade to "reconnect", never to a 500 mid-post."""
    token, clip = _ready(sandbox)
    with contextlib.closing(sandbox.db._connect()) as conn:
        conn.execute(
            "UPDATE connections SET access_token_enc = 'x', refresh_token_enc = 'x'"
        )

    body = _publish(sandbox, token, clip["id"])
    assert body["status"] == "failed"
    assert "reconnect" in body["error"].lower()
    assert sandbox.upload.calls == []


# --------------------------------------------------------------------------- #
# 7. Refusals that must be honest.
# --------------------------------------------------------------------------- #


def test_publishing_an_expired_clip_is_refused_with_a_clear_message(
    sandbox: SimpleNamespace,
) -> None:
    """The row is still there; only the video expired. Say exactly that."""
    token, clip = _ready(sandbox)
    assert sandbox.db.clear_clip_file(clip["id"]) is True  # what retention does

    resp = _post(sandbox, token, clip["id"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "expired" in detail
    assert "transcript are still in your library" in detail
    # Refused BEFORE anything was queued or sent.
    assert _rows(sandbox.db, "publish_jobs") == []
    assert sandbox.upload.calls == []


def test_a_clip_that_expires_after_queueing_fails_the_job_not_the_process(
    sandbox: SimpleNamespace,
) -> None:
    """The retention sweep can land between the request and the upload."""
    token, clip = _ready(sandbox)
    publish_id = "aa" * 16
    connection = _connection(sandbox)
    sandbox.db.create_publish_job(
        publish_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=clip["id"],
        connection_id=connection["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )
    sandbox.db.clear_clip_file(clip["id"])

    sandbox.worker.publish_clip(publish_id)

    row = sandbox.db.get_publish_job(publish_id)
    assert row["status"] == "failed"
    assert "expired" in row["error"]
    assert sandbox.upload.calls == []


def test_another_users_clip_is_a_404(sandbox: SimpleNamespace) -> None:
    """Indistinguishable from a clip that never existed — as everywhere else."""
    alice_token, clip = _ready(sandbox, email="alice@example.com")
    bob = _account(sandbox.client, "bob@example.com")
    _connect(sandbox, bob)  # Bob has a channel of his own; it changes nothing

    resp = _post(sandbox, bob, clip["id"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Unknown clip."
    assert _rows(sandbox.db, "publish_jobs") == []
    assert sandbox.upload.calls == []


def test_another_users_publish_is_a_404_to_poll(sandbox: SimpleNamespace) -> None:
    alice_token, clip = _ready(sandbox, email="alice@example.com")
    started = _publish(sandbox, alice_token, clip["id"])
    bob = _account(sandbox.client, "bob@example.com")

    resp = sandbox.client.get(f"/v1/publishes/{started['id']}", headers=_bearer(bob))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown post."


def test_posting_with_no_channel_connected_is_refused(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    clip = _save_clip(sandbox, token)

    resp = _post(sandbox, token, clip["id"])
    assert resp.status_code == 409, resp.text
    assert "account page" in resp.json()["detail"]
    assert sandbox.upload.calls == []


def test_a_platform_that_cannot_be_posted_to_is_refused_not_queued(
    sandbox: SimpleNamespace,
) -> None:
    """TikTok is known and unavailable — that is a 503 with a reason, not a 202."""
    token, clip = _ready(sandbox)
    resp = _post(sandbox, token, clip["id"], platform="tiktok")
    assert resp.status_code == 503, resp.text
    assert "share button" in resp.json()["detail"]

    entry = _platform(sandbox, token, "tiktok")
    assert entry["publishable"] is False
    assert entry["privacy_choices"] == []


def test_an_unknown_platform_is_a_404(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox)
    assert _post(sandbox, token, clip["id"], platform="myspace").status_code == 404


def test_publishing_needs_a_session(sandbox: SimpleNamespace) -> None:
    token, clip = _ready(sandbox)
    assert sandbox.client.post(f"/v1/clips/{clip['id']}/publish", json={}).status_code == 401
    assert sandbox.client.get("/v1/publishes/anything").status_code == 401
    assert _rows(sandbox.db, "publish_jobs") == []


def test_with_no_token_key_a_publish_is_refused(sandbox: SimpleNamespace) -> None:
    """No key means the channel's tokens cannot be read to upload with."""
    token, clip = _ready(sandbox)
    _set_env(CC_TOKEN_KEY=None)

    resp = _post(sandbox, token, clip["id"])
    assert resp.status_code == 503
    assert "CC_TOKEN_KEY" in resp.json()["detail"]
    assert _rows(sandbox.db, "publish_jobs") == []


def test_a_second_post_of_the_same_clip_returns_the_one_already_running(
    sandbox: SimpleNamespace,
) -> None:
    """A double-tapped button must not spend two of a day's few uploads."""
    token, clip = _ready(sandbox)
    user_id = _rows(sandbox.db, "users")[0]["id"]
    live = sandbox.db.create_publish_job(
        "bb" * 16,
        user_id=user_id,
        clip_id=clip["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )

    resp = _post(sandbox, token, clip["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == live["id"]
    assert len(_rows(sandbox.db, "publish_jobs")) == 1
    assert sandbox.upload.calls == []


def test_a_finished_clip_can_be_posted_again(sandbox: SimpleNamespace) -> None:
    """The guard is about what is IN FLIGHT, not about posting twice ever."""
    token, clip = _ready(sandbox)
    first = _publish(sandbox, token, clip["id"])
    sandbox.upload.received = bytearray()
    second = _publish(sandbox, token, clip["id"])

    assert second["id"] != first["id"]
    assert second["status"] == "done"
    assert len(_rows(sandbox.db, "publish_jobs")) == 2


# --------------------------------------------------------------------------- #
# 8. Nothing secret reaches the row, the response, or a log line.
# --------------------------------------------------------------------------- #


def _database_bytes(sandbox: SimpleNamespace) -> bytes:
    """Every byte SQLite holds for us — the WAL included (see test_connections)."""
    return b"".join(
        path.read_bytes()
        for path in sorted(sandbox.data_dir.glob("jobs.sqlite3*"))
        if path.is_file()
    )


def test_no_token_or_session_url_is_stored_or_returned(
    sandbox: SimpleNamespace,
) -> None:
    """The resumable session URL is a bearer capability for the channel."""
    token, clip = _ready(sandbox)
    body = _publish(sandbox, token, clip["id"])
    polled = sandbox.client.get(
        f"/v1/publishes/{body['id']}", headers=_bearer(token)
    ).text

    raw = _database_bytes(sandbox)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, SESSION_URL):
        assert secret.encode("utf-8") not in raw, f"{secret!r} is on disk in plaintext"
        assert secret not in json.dumps(body)
        assert secret not in polled


def test_the_publish_row_has_no_field_a_secret_could_go_in(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox)
    body = _publish(sandbox, token, clip["id"])
    assert set(body) == {
        "id",
        "clip_id",
        "platform",
        "status",
        "progress",
        "detail",
        "error",
        "title",
        "privacy",
        "video_id",
        "video_url",
        "note",
        "created_at",
        "updated_at",
    }


def test_nothing_secret_reaches_a_log_line(
    sandbox: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """Driven through the failure branches, which is where logging happens."""
    caplog.set_level(0)
    token, clip = _ready(sandbox)
    _publish(sandbox, token, clip["id"])

    # A quota refusal, a retried chunk and an exhausted retry: every branch
    # that writes a log line about a provider reply.
    second = _save_clip(sandbox, token)
    sandbox.upload.init_status = 403
    sandbox.upload.init_body = _QUOTA_BODY
    _publish(sandbox, token, second["id"])
    sandbox.upload.init_status = 200
    third = _save_clip(sandbox, token)
    sandbox.upload.chunk_faults = [500] * 5
    _publish(sandbox, token, third["id"])

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, SESSION_URL, sandbox.token_key):
        assert secret and secret not in logged


# --------------------------------------------------------------------------- #
# 9. The queue's own housekeeping: nothing polls forever, nothing accumulates.
# --------------------------------------------------------------------------- #


def _age_publish(sandbox: SimpleNamespace, publish_id: str, **columns: str) -> None:
    with contextlib.closing(sandbox.db._connect()) as conn:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(
            f"UPDATE publish_jobs SET {assignments} WHERE id = ?",
            (*columns.values(), publish_id),
        )


def test_an_upload_whose_worker_died_is_failed_rather_than_left_spinning(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox)
    publish_id = "cc" * 16
    sandbox.db.create_publish_job(
        publish_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=clip["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )
    _age_publish(sandbox, publish_id, updated_at="2000-01-01T00:00:00.000+00:00")

    assert sandbox.worker.reconcile_stalled_publishes() == 1
    row = sandbox.db.get_publish_job(publish_id)
    assert row["status"] == "failed"
    assert "try posting this clip again" in row["error"]
    # A live row is not touched by a second sweep.
    assert sandbox.worker.reconcile_stalled_publishes() == 0


def test_a_box_with_no_beat_settles_a_stranded_upload_on_its_next_boot(
    sandbox: SimpleNamespace,
) -> None:
    """The other half of the same promise, and the half that needs no beat.

    `celery beat` is optional on a single box (queue_app.beat_schedule says so
    out loud), and on a box without one the hourly sweep above never runs — so
    an upload whose worker was killed would read `uploading` with nobody
    working on it for as long as the row survived, and the sheet polling it
    would spin for exactly that long. The API's startup hook reconciles these
    for the same reason it reconciles renders.
    """
    from fastapi.testclient import TestClient

    from clipcatalyst_api.main import app

    token, clip = _ready(sandbox)
    publish_id = "ba" * 16
    sandbox.db.create_publish_job(
        publish_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=clip["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )
    _age_publish(sandbox, publish_id, updated_at="2000-01-01T00:00:00.000+00:00")

    # A restart, and nothing else: no sweep is called by hand here.
    with TestClient(app):
        pass

    row = sandbox.db.get_publish_job(publish_id)
    assert row["status"] == "failed"
    assert "try posting this clip again" in row["error"]
    # And the poll a stuck sheet is making now has a terminal answer for it.
    body = sandbox.client.get(
        f"/v1/publishes/{publish_id}", headers=_bearer(token)
    ).json()
    assert body["status"] == "failed"
    assert body["error"] == row["error"]


def test_a_running_upload_is_never_swept_out_from_under_itself(
    sandbox: SimpleNamespace,
) -> None:
    """The sweep's precondition is "and nothing has touched this since"."""
    token, clip = _ready(sandbox)
    publish_id = "dd" * 16
    sandbox.db.create_publish_job(
        publish_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=clip["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )
    assert sandbox.worker.reconcile_stalled_publishes() == 0
    assert sandbox.db.get_publish_job(publish_id)["status"] == "queued"


def test_finished_publish_rows_are_reaped_and_live_ones_are_not(
    sandbox: SimpleNamespace,
) -> None:
    token, clip = _ready(sandbox)
    done = _publish(sandbox, token, clip["id"])["id"]
    live_id = "ee" * 16
    second = _save_clip(sandbox, token)
    sandbox.db.create_publish_job(
        live_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=second["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )
    for publish_id in (done, live_id):
        _age_publish(sandbox, publish_id, created_at="2000-01-01T00:00:00.000+00:00")

    assert sandbox.worker.reap_expired_publishes() == 1
    assert sandbox.db.get_publish_job(done) is None
    assert sandbox.db.get_publish_job(live_id) is not None
    # The clip and the connection are untouched by any of it.
    assert len(_rows(sandbox.db, "clips")) == 2
    assert len(_rows(sandbox.db, "connections")) == 1


def test_a_status_write_from_a_run_that_lost_its_row_is_refused(
    sandbox: SimpleNamespace,
) -> None:
    """The compare-and-swap, directly: two runs cannot both write a terminal."""
    token, clip = _ready(sandbox)
    publish_id = "ff" * 16
    sandbox.db.create_publish_job(
        publish_id,
        user_id=_rows(sandbox.db, "users")[0]["id"],
        clip_id=clip["id"],
        connection_id=_connection(sandbox)["id"],
        platform="youtube",
        title="",
        description="",
        privacy="private",
    )
    assert sandbox.db.transition_publish_status(
        publish_id, expect="queued", to="failed", error="first"
    )
    assert not sandbox.db.transition_publish_status(
        publish_id, expect="queued", to="uploading"
    )
    assert not sandbox.db.transition_publish_status(
        publish_id, expect="uploading", to="done", video_id=VIDEO_ID
    )
    assert sandbox.db.get_publish_job(publish_id)["error"] == "first"


def test_a_publish_row_cannot_have_its_status_written_blind(
    sandbox: SimpleNamespace,
) -> None:
    with pytest.raises(ValueError):
        sandbox.db.update_publish_job("whatever", status="done")
    with pytest.raises(ValueError):
        sandbox.db.update_publish_job("whatever", clip_id="somebody-elses")


# --------------------------------------------------------------------------- #
# 10. The seam itself, unstubbed.
#
# Every test above replaces `http_request`, which means every test above takes
# it on faith that the real one can express a 308 at all — and it very nearly
# cannot. urllib raises on a 308 rather than returning it, follows redirects
# unless told not to, and would resend the whole video for its trouble. So this
# one drives the REAL function against a loopback server that speaks the
# protocol: no stub, no network, and the assumption the other 48 rest on is
# checked rather than assumed.
# --------------------------------------------------------------------------- #


def test_the_real_http_seam_returns_a_308_with_its_range_intact() -> None:
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from clipcatalyst_api.publish import youtube

    held = {"bytes": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: ANN002 - quiet in the test log
            pass

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Location", f"http://127.0.0.1:{port}/session")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_PUT(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            total = int(str(self.headers.get("Content-Range")).split("/")[1])
            # Keep half of the first chunk, so the client has to resume.
            held["bytes"] += len(body) // 2 if held["bytes"] == 0 else len(body)
            if held["bytes"] >= total:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(_json.dumps({"id": "REALVIDEO"}).encode())
                return
            self.send_response(308)
            self.send_header("Range", f"bytes=0-{held['bytes'] - 1}")
            self.end_headers()

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as error:  # a sandbox with no loopback to bind
        pytest.skip(f"cannot bind a loopback server here: {error}")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, headers, _ = youtube.http_request(
            "POST",
            f"http://127.0.0.1:{port}/upload",
            headers={"Content-Type": "application/json"},
            body=b"{}",
        )
        # The session URL arrives in a header, lower-cased on the way out.
        assert status == 200
        session_url = headers["location"]
        assert session_url.endswith("/session")

        data = bytes(range(256)) * 8
        status, headers, _ = youtube.http_request(
            "PUT",
            session_url,
            headers={
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
            },
            body=data,
        )
        # A 308 is a RETURN, not a raise, and it still carries its Range.
        assert status == 308
        assert youtube._resume_offset(headers) == len(data) // 2

        offset = youtube._resume_offset(headers)
        status, _, text = youtube.http_request(
            "PUT",
            session_url,
            headers={
                "Content-Length": str(len(data) - offset),
                "Content-Range": f"bytes {offset}-{len(data) - 1}/{len(data)}",
            },
            body=data[offset:],
        )
        assert status == 200
        assert _json.loads(text)["id"] == "REALVIDEO"
        assert held["bytes"] == len(data)
    finally:
        server.shutdown()
        server.server_close()

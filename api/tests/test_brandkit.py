"""Brand kit, cloud side (BRANDKIT.md — Module 3).

What this suite is for, section by section:

1. **Entitlement.** A brand kit is sold from Starter up, so the server refuses
   to store one for a plan that does not carry it — read from the EFFECTIVE
   plan, so a cancelled subscription loses the ability the moment it lapses,
   not at next login.
2. **Clearing.** DELETE removes the row and the file, and is deliberately NOT
   gated: a downgrade must never strand a creator's logo on our disk.
3. **Uploads.** The bytes are trusted for nothing: the type is sniffed rather
   than read off the header, the size is measured after decoding, and the
   colour is re-validated with the same rule as the TypeScript. Both request
   shapes — multipart and JSON — go through the same checks.
4. **Path safety.** The stored filename is built from the account id and a
   whitelisted extension, so a traversal attempt in the upload's filename
   cannot escape the brand directory. The upload's filename is never an input.
5. **Captions.** The brand colour replaces the default violet for unassigned
   words, and diarization still wins whenever a clip has two or more speakers:
   that colour carries information the brand colour would destroy. The
   unbranded output stays byte-identical to what shipped before.
6. **Renders.** A real ffmpeg render with a real generated PNG logo, asserting
   the filtergraph carries exactly one overlay, the file exists, and the logo
   pixels really are in the corner ``logo_box`` asked for. A corrupt logo, an
   SVG this build cannot decode, and a plan that requires OUR mark all render
   fine — without an overlay. A brand kit must never fail a render.
7. **Render-time resolution.** The kit comes from the owner's LIVE plan, so a
   started job carries what the account is entitled to *now* — proven on a
   real job, through the worker, not just on the helper.
8. **Parity.** ``logo_box`` and ``active_word_color`` are compiled-and-run
   against ``logoBox`` and ``activeWordColor`` in ``lib/studio/brandkit.ts``
   through a node subprocess (the croptrack/diarize pattern). They must agree
   to the pixel and to the colour, including JavaScript's ties-to-+infinity
   rounding.

Mirrors the env/import dance of ``test_entitlements.py`` — all CC_* vars are
set BEFORE any app module is imported, ``get_settings`` is lru_cached so its
cache is cleared, and the settings-snapshotting modules are purged for a clean
re-import. The renderer tests use the lighter ``settings`` fixture from
``test_render.py``, since they call the pipeline directly.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import cv2
import pytest

from clipcatalyst_api import brandkit
from clipcatalyst_api.brandkit import (
    DEFAULT_CAPTION_COLOR,
    LOGO_HEIGHT_RATIO,
    LOGO_MARGIN_RATIO,
    LOGO_MAX_WIDTH_RATIO,
    MAX_LOGO_BYTES,
    logo_box,
    normalize_hex,
    sniff_image_type,
)
from clipcatalyst_api.pipeline.captions import ACTIVE_COLOR, INACTIVE_COLOR, build_ass
from clipcatalyst_api.pipeline.probe import probe_media
from clipcatalyst_api.pipeline.render import build_logo_graph, render_clip
from clipcatalyst_api.pipeline.types import ClipPlan, RenderOptions, Word
from clipcatalyst_api.settings import get_settings

PASSWORD = "correct-horse-battery"
MAX_UPLOAD_BYTES = 20_000_000

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_SOURCE = REPO_ROOT / "lib" / "studio" / "brandkit.ts"

# #ff2d95 in ASS's blue-green-red channel order, converted by hand.
PINK = "#ff2d95"
PINK_ASS = r"\1c&H952DFF&"
# The diarization palette, likewise (see test_captions_speakers.py).
VIOLET_0 = r"\1c&HFA8BA7&"
AMBER_1 = r"\1c&H24BFFB&"

_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
)


def _purge() -> None:
    get_settings.cache_clear()
    for name in _SNAPSHOT_MODULES:
        sys.modules.pop(name, None)


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _make_png(path: Path, width: int, height: int, color: str = "magenta") -> Path:
    """A real PNG, encoded by ffmpeg — not a hand-written header."""
    subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}",
            "-frames:v", "1", str(path),
        ],
        check=True,
        timeout=120,
    )
    assert path.stat().st_size > 0
    return path


def _data_url(data: bytes, declared: str = "image/png") -> str:
    return f"data:{declared};base64,{base64.b64encode(data).decode('ascii')}"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir; billing off, no founder token."""
    saved_env = dict(os.environ)
    data_dir = tmp_path_factory.mktemp("branddata")

    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_TRANSCRIBER": "fake",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_PUBLIC_BASE_URL": "",
            "CC_MAX_UPLOAD_BYTES": str(MAX_UPLOAD_BYTES),
            "CC_BILLING": "off",  # plans are set directly here
        }
    )
    os.environ.pop("CC_API_TOKEN", None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth
    from clipcatalyst_api.main import app

    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, data_dir=data_dir)
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The renderer's settings, pointed at this test's own data dir."""
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 6 s 640x360 test pattern with a sine audio track (x264 + aac)."""
    path = tmp_path_factory.mktemp("brandsource") / "source.mp4"
    subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        timeout=300,
    )
    return path


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _account(client, email: str, *, plan: str = "free", status: str = "") -> tuple[str, str]:
    """Register an account and put it on a plan (as a verified webhook would)."""
    from clipcatalyst_api import db

    resp = client.post("/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    token, user_id = body["token"], body["user"]["id"]
    if plan != "free" or status:
        db.update_user(user_id, plan=plan, plan_status=status)
    return token, user_id


def _put_kit(client, token: str, **fields):  # noqa: ANN202 - Response
    """PUT a kit as JSON (the shape the panel syncs)."""
    return client.put("/v1/me/brand", json=fields, headers=_bearer(token))


def _put_multipart(client, token: str, *, filename: str, data: bytes, **fields):  # noqa: ANN202
    return client.put(
        "/v1/me/brand",
        files={"logo": (filename, data, "image/png")},
        data=fields,
        headers=_bearer(token),
    )


def _me(client, token: str) -> dict:
    resp = client.get("/v1/me", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def make_plan(words: list[Word], start: float = 1.0, end: float = 4.0) -> ClipPlan:
    return ClipPlan(
        id="brand-1", start=start, end=end, score=80, title="Brand test",
        hooks=[], reason="", tip="", words=words,
    )


# --------------------------------------------------------------------------- #
# 1. Entitlement: the kit is a Starter promise, read from the EFFECTIVE plan.
# --------------------------------------------------------------------------- #


def test_free_plan_is_refused_with_an_honest_message(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    client = sandbox.client
    token, user_id = _account(client, "free@example.com")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()

    resp = _put_kit(client, token, logo=_data_url(logo), caption_color=PINK)
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    # Honest means it names the feature, the plan that carries it, and what
    # happens meanwhile — never a bare "forbidden".
    assert "Starter" in detail
    assert "ClipCatalyst mark" in detail

    # Nothing was stored: not the row, not a file.
    from clipcatalyst_api import db

    user = db.get_user_by_id(user_id)
    assert user["brand_logo_path"] == ""
    assert user["brand_caption_color"] == ""
    assert not list((sandbox.data_dir / "brand").glob("*")) or not any(
        p.is_file() for p in (sandbox.data_dir / "brand").iterdir()
    )
    assert _me(client, token)["entitlements"]["brand_kit"] is False


@pytest.mark.parametrize("plan", ["starter", "pro", "enterprise"])
def test_paid_plans_store_the_kit(
    sandbox: SimpleNamespace, tmp_path: Path, plan: str
) -> None:
    client = sandbox.client
    token, user_id = _account(client, f"{plan}@example.com", plan=plan, status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()

    resp = _put_kit(client, token, logo=_data_url(logo), caption_color=PINK)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "logo_url": "/v1/me/brand/logo",
        "caption_color": PINK,
        "show_logo": True,
    }

    from clipcatalyst_api import db

    user = db.get_user_by_id(user_id)
    assert user["brand_logo_path"] == f"brand/{user_id}.png"
    assert user["brand_caption_color"] == PINK
    assert user["brand_show_logo"] == 1
    stored = sandbox.data_dir / "brand" / f"{user_id}.png"
    assert stored.read_bytes() == logo  # the bytes, untouched

    me = _me(client, token)
    assert me["entitlements"]["brand_kit"] is True
    assert me["brand"] == {
        "logo_url": "/v1/me/brand/logo",
        "caption_color": PINK,
        "show_logo": True,
    }

    # The logo comes back as a URL, and that URL serves the bytes.
    logo_resp = client.get("/v1/me/brand/logo", headers=_bearer(token))
    assert logo_resp.status_code == 200
    assert logo_resp.content == logo
    assert logo_resp.headers["content-type"].startswith("image/png")
    assert logo_resp.headers["x-content-type-options"] == "nosniff"


def test_a_lapsed_subscription_loses_the_entitlement(sandbox: SimpleNamespace) -> None:
    """The stored kit survives a downgrade; the ability to change it does not."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "lapsed@example.com", plan="pro", status="active")
    assert _put_kit(client, token, caption_color=PINK).status_code == 200

    db.update_user(user_id, plan_status="canceled")
    resp = _put_kit(client, token, caption_color="#00ff00")
    assert resp.status_code == 403, resp.text

    me = _me(client, token)
    assert me["entitlements"]["brand_kit"] is False
    assert me["brand"]["caption_color"] == PINK  # kept, just not usable
    assert db.get_user_by_id(user_id)["brand_caption_color"] == PINK


def test_the_logo_route_is_private_to_its_owner(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    client = sandbox.client
    token, _ = _account(client, "owner@example.com", plan="starter", status="active")
    other, _ = _account(client, "stranger@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(logo)).status_code == 200

    # Another account's session sees only its OWN (absent) logo, never this one.
    assert client.get("/v1/me/brand/logo", headers=_bearer(other)).status_code == 404
    assert client.get("/v1/me/brand/logo").status_code == 401


# --------------------------------------------------------------------------- #
# 2. DELETE clears the kit — and is not gated, so a downgrade cannot strand a
#    creator's logo on our disk with no way to remove it.
# --------------------------------------------------------------------------- #


def test_delete_removes_the_row_and_the_file(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "clear@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(logo), caption_color=PINK).status_code == 200
    stored = sandbox.data_dir / "brand" / f"{user_id}.png"
    assert stored.is_file()

    resp = client.delete("/v1/me/brand", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"logo_url": None, "caption_color": None, "show_logo": True}
    assert not stored.exists()

    user = db.get_user_by_id(user_id)
    assert (user["brand_logo_path"], user["brand_caption_color"]) == ("", "")
    assert _me(client, token)["brand"]["logo_url"] is None
    assert client.get("/v1/me/brand/logo", headers=_bearer(token)).status_code == 404


def test_a_downgraded_account_can_still_delete_its_kit(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "downgrade@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(logo)).status_code == 200

    db.update_user(user_id, plan="free", plan_status="")
    assert client.delete("/v1/me/brand", headers=_bearer(token)).status_code == 200
    assert not (sandbox.data_dir / "brand" / f"{user_id}.png").exists()


def test_replacing_a_logo_leaves_exactly_one_file(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    """A PNG replaced by a JPEG must not leave the PNG behind."""
    client = sandbox.client
    token, user_id = _account(client, "replace@example.com", plan="pro", status="active")
    png = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(png)).status_code == 200

    jpeg_path = tmp_path / "logo.jpg"
    subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=120x120",
            "-frames:v", "1", str(jpeg_path),
        ],
        check=True,
        timeout=120,
    )
    assert _put_kit(client, token, logo=_data_url(jpeg_path.read_bytes())).status_code == 200

    files = sorted(p.name for p in (sandbox.data_dir / "brand").iterdir() if p.is_file())
    assert files == [f"{user_id}.jpg"]


# --------------------------------------------------------------------------- #
# 3. Uploads: sniffed types, measured sizes, validated colours.
# --------------------------------------------------------------------------- #


def test_oversize_logo_is_rejected_with_the_real_numbers(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, user_id = _account(client, "big@example.com", plan="pro", status="active")
    # A genuine PNG header padded past the ceiling: what the SERVER measures is
    # the bytes it received, not a declared length.
    oversize = b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_LOGO_BYTES

    resp = _put_kit(client, token, logo=_data_url(oversize))
    assert resp.status_code == 413, resp.text
    detail = resp.json()["detail"]
    assert "2 MB" in detail  # the limit, spelled as the panel spells it
    from clipcatalyst_api import db

    assert db.get_user_by_id(user_id)["brand_logo_path"] == ""


def test_a_body_far_over_the_ceiling_is_refused_before_it_is_buffered(
    sandbox: SimpleNamespace,
) -> None:
    client = sandbox.client
    token, _ = _account(client, "huge@example.com", plan="pro", status="active")
    resp = client.put(
        "/v1/me/brand",
        content=b"\0" * 5_000_000,
        headers={**_bearer(token), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413, resp.text


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"this is a text file, not an image at all", "plain text"),
        (b"GIF89a" + b"\0" * 32, "a GIF, which we do not accept"),
        (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "a PDF wearing an image content type"),
        (b"MZ\x90\x00\x03\x00\x00\x00", "a DOS/PE executable"),
    ],
)
def test_wrong_type_uploads_are_rejected_however_they_are_declared(
    sandbox: SimpleNamespace, payload: bytes, why: str
) -> None:
    """The declared type is never consulted — only the bytes are."""
    client = sandbox.client
    token, user_id = _account(client, "wrong@example.com", plan="pro", status="active")

    # Declared image/png in the data URL...
    resp = _put_kit(client, token, logo=_data_url(payload, declared="image/png"))
    assert resp.status_code == 400, f"{why}: {resp.text}"
    assert "PNG, JPEG, WebP or SVG" in resp.json()["detail"]

    # ...and declared image/png in a multipart part.
    resp = _put_multipart(client, token, filename="logo.png", data=payload)
    assert resp.status_code == 400, f"{why}: {resp.text}"

    from clipcatalyst_api import db

    assert db.get_user_by_id(user_id)["brand_logo_path"] == ""


def test_a_png_declared_as_text_is_still_stored_as_a_png(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    """Sniffing cuts both ways: an honest file with a wrong header is fine."""
    client = sandbox.client
    token, user_id = _account(client, "sniff@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()

    resp = client.put(
        "/v1/me/brand",
        files={"logo": ("whatever.bin", logo, "application/octet-stream")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    from clipcatalyst_api import db

    assert db.get_user_by_id(user_id)["brand_logo_path"] == f"brand/{user_id}.png"


def test_an_svg_is_accepted_and_served_with_its_own_type(
    sandbox: SimpleNamespace,
) -> None:
    client = sandbox.client
    token, user_id = _account(client, "svg@example.com", plan="pro", status="active")
    svg = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="80" height="20"/>'

    assert _put_kit(client, token, logo=_data_url(svg, declared="image/svg+xml")).status_code == 200
    from clipcatalyst_api import db

    assert db.get_user_by_id(user_id)["brand_logo_path"] == f"brand/{user_id}.svg"
    resp = client.get("/v1/me/brand/logo", headers=_bearer(token))
    assert resp.headers["content-type"].startswith("image/svg+xml")
    # Markup served from our own origin is inert: no sniffing, no subresources.
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


@pytest.mark.parametrize("bad", ["red", "#12345", "rgb(1,2,3)", "a78bfa", "#gggggg", "#a78bfa88"])
def test_a_colour_that_is_not_hex_is_refused(sandbox: SimpleNamespace, bad: str) -> None:
    client = sandbox.client
    token, user_id = _account(client, "color@example.com", plan="pro", status="active")
    resp = _put_kit(client, token, caption_color=bad)
    assert resp.status_code == 400, resp.text
    assert "#rrggbb" in resp.json()["detail"]
    from clipcatalyst_api import db

    assert db.get_user_by_id(user_id)["brand_caption_color"] == ""


def test_a_short_hex_colour_is_expanded_like_the_typescript(
    sandbox: SimpleNamespace,
) -> None:
    client = sandbox.client
    token, _ = _account(client, "short@example.com", plan="pro", status="active")
    resp = _put_kit(client, token, caption_color="  #F0A  ")
    assert resp.status_code == 200, resp.text
    assert resp.json()["caption_color"] == "#ff00aa"


def test_show_logo_off_is_stored_and_reported(sandbox: SimpleNamespace, tmp_path: Path) -> None:
    client = sandbox.client
    token, user_id = _account(client, "hidden@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()

    resp = _put_multipart(
        client, token, filename="logo.png", data=logo, show_logo="false", caption_color=PINK
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["show_logo"] is False
    from clipcatalyst_api import db

    assert db.get_user_by_id(user_id)["brand_show_logo"] == 0
    assert _me(client, token)["brand"]["show_logo"] is False


def test_an_unreadable_logo_payload_says_so(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, _ = _account(client, "garbled@example.com", plan="pro", status="active")
    resp = _put_kit(client, token, logo="not-a-data-url")
    assert resp.status_code == 400, resp.text
    assert "data:" in resp.json()["detail"]


def test_an_unsupported_body_type_is_named(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, _ = _account(client, "xml@example.com", plan="pro", status="active")
    resp = client.put(
        "/v1/me/brand",
        content=b"<kit/>",
        headers={**_bearer(token), "Content-Type": "application/xml"},
    )
    assert resp.status_code == 415, resp.text
    assert "multipart/form-data" in resp.json()["detail"]


def test_the_brand_routes_need_a_session(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    assert client.put("/v1/me/brand", json={"caption_color": PINK}).status_code == 401
    assert client.delete("/v1/me/brand").status_code == 401


# --------------------------------------------------------------------------- #
# 4. Path safety: the filename comes from the account id, never the upload.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "filename",
    [
        "../../../../etc/passwd.png",
        "..%2F..%2Fescape.png",
        "/etc/cron.d/pwned.png",
        "....//....//evil.png",
        "logo.png\x00.txt",
    ],
)
def test_a_traversal_filename_cannot_escape_the_brand_dir(
    sandbox: SimpleNamespace, tmp_path: Path, filename: str
) -> None:
    client = sandbox.client
    token, user_id = _account(client, "traverse@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()

    resp = _put_multipart(client, token, filename=filename, data=logo)
    assert resp.status_code == 200, resp.text

    from clipcatalyst_api import db

    # The row names the id-derived path, and the ONLY file written is that one.
    assert db.get_user_by_id(user_id)["brand_logo_path"] == f"brand/{user_id}.png"
    brand_dir = sandbox.data_dir / "brand"
    written = [p for p in brand_dir.rglob("*") if p.is_file()]
    assert [p.name for p in written] == [f"{user_id}.png"]
    # And nothing landed anywhere else under the data dir.
    strays = [
        p
        for p in sandbox.data_dir.rglob("*.png")
        if p.is_file() and p.parent != brand_dir
    ]
    assert strays == []


def test_logo_storage_name_only_builds_safe_names() -> None:
    assert brandkit.logo_storage_name("0123abcd", "image/png") == "brand/0123abcd.png"
    assert brandkit.logo_storage_name("0123abcd", "image/jpeg") == "brand/0123abcd.jpg"
    # An id that could not make a safe leaf, and a type with no extension.
    for hostile in ("../etc/passwd", "a/b", "", "..", "with space", "x" * 65):
        assert brandkit.logo_storage_name(hostile, "image/png") is None
    assert brandkit.logo_storage_name("0123abcd", "image/gif") is None


def test_logo_abs_path_refuses_anything_outside_the_brand_dir(settings) -> None:
    assert brandkit.logo_abs_path(settings, "") is None
    assert brandkit.logo_abs_path(settings, "../../etc/passwd") is None
    assert brandkit.logo_abs_path(settings, "brand/../uploads/x.src") is None
    assert brandkit.logo_abs_path(settings, "brand/abc.png") == (
        settings.brand_dir / "abc.png"
    )


# --------------------------------------------------------------------------- #
# 5. Captions: the brand colour, and diarization winning over it.
# --------------------------------------------------------------------------- #


def one_speaker_words() -> list[Word]:
    return [Word("Hello", 0.2, 0.6), Word(" world", 0.7, 1.2)]


def two_speaker_words() -> list[Word]:
    return [
        Word("Hello", 0.2, 0.6, speaker=0),
        Word(" wonderful", 0.7, 1.2, speaker=0),
        Word(" totally", 2.0, 2.5, speaker=1),
        Word(" agreed", 2.6, 3.1, speaker=1),
    ]


def test_the_brand_colour_lands_on_a_single_speaker_clip() -> None:
    ass = build_ass(make_plan(one_speaker_words()), height=960, watermark=False, caption_color=PINK)
    assert PINK_ASS in ass
    assert ACTIVE_COLOR not in ass  # the violet is gone, not merely joined
    assert INACTIVE_COLOR in ass  # inactive words are still white


def test_the_brand_colour_does_not_override_two_speakers() -> None:
    """Diarization wins: that colour says WHO is talking."""
    ass = build_ass(make_plan(two_speaker_words()), height=960, watermark=False, caption_color=PINK)
    assert PINK_ASS not in ass
    assert VIOLET_0 in ass
    assert AMBER_1 in ass


def test_unassigned_words_in_a_two_speaker_clip_keep_their_palette_colour() -> None:
    """A word with no speaker in a diarized clip is the seam between the rules.

    ``activeWordColor`` sends it down the brand branch — speaker is undefined,
    so the speakerCount test cannot apply — and both engines must agree.
    """
    words = [
        Word("Hello", 0.2, 0.6, speaker=0),
        Word(" there", 0.7, 1.0),  # unassigned
        Word(" totally", 2.0, 2.5, speaker=1),
    ]
    ass = build_ass(make_plan(words), height=960, watermark=False, caption_color=PINK)
    assert PINK_ASS in ass  # the unassigned word took the brand colour
    assert VIOLET_0 in ass and AMBER_1 in ass  # the diarized ones did not


def test_a_broken_stored_colour_degrades_to_the_default_violet() -> None:
    ass = build_ass(
        make_plan(one_speaker_words()), height=960, watermark=False, caption_color="octarine"
    )
    assert ACTIVE_COLOR in ass


def test_an_unbranded_clip_is_unchanged() -> None:
    """The whole free tier depends on this: no kit, no difference."""
    plan = make_plan(one_speaker_words())
    assert build_ass(plan, 960, True, None) == build_ass(plan, 960, True)
    assert build_ass(plan, 960, True, "") == build_ass(plan, 960, True)


def _watermark_events(ass: str) -> list[str]:
    return [
        line
        for line in ass.splitlines()
        if line.startswith("Dialogue:") and ",Watermark," in line
    ]


def test_our_watermark_line_is_written_only_for_our_mark() -> None:
    plan = make_plan(one_speaker_words())
    marked = build_ass(plan, 960, True, PINK)
    assert _watermark_events(marked) == [
        "Dialogue: 1,0:00:00.00,0:00:03.00,Watermark,,0,0,0,ClipCatalyst"
    ]
    # A branded (unwatermarked) render carries the logo instead — never both.
    assert _watermark_events(build_ass(plan, 960, False, PINK)) == []


# --------------------------------------------------------------------------- #
# 6. Renders: a real logo, really overlaid; every failure path still renders.
# --------------------------------------------------------------------------- #


def _spy_on_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ffmpeg argv render_clip runs, and run it for real."""
    from clipcatalyst_api.pipeline import render as render_module

    calls: list[list[str]] = []
    real_popen = subprocess.Popen

    def spy(cmd, **kwargs):  # noqa: ANN202 - subprocess.Popen
        calls.append(list(cmd))
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(render_module.subprocess, "Popen", spy)
    return calls


def _filtergraph(cmd: list[str]) -> str:
    assert "-filter_complex" in cmd, cmd
    return cmd[cmd.index("-filter_complex") + 1]


def test_a_real_logo_renders_one_overlay_into_a_real_file(
    source_video: Path, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logo = _make_png(tmp_path / "logo.png", 200, 50)
    out = tmp_path / "branded.mp4"
    calls = _spy_on_ffmpeg(monkeypatch)

    rendered = render_clip(
        source_video,
        make_plan(one_speaker_words()),
        out,
        RenderOptions(height=480, watermark=False, logo_path=str(logo), caption_color=PINK),
        settings,
    )

    graph = _filtergraph(calls[-1])
    assert graph.count("overlay") == 1, graph
    # The shape BRANDKIT.md specifies: scaled logo, the clip chain, then the
    # overlay AFTER the scale so the margin is in output pixels.
    box = logo_box(200, 50, rendered.width, rendered.height)
    assert graph == build_logo_graph(
        graph.split("[0:v]")[1].split("[base]")[0], box
    )
    assert graph.startswith(f"[1:v]scale={box.width}:{box.height}[logo];")
    assert graph.endswith(f"[base][logo]overlay={box.x}:{box.y}[v]")
    assert graph.index("scale=270:480") < graph.index("overlay=")
    assert calls[-1].count("-i") == 2 and str(logo) in calls[-1]

    assert out.is_file() and out.stat().st_size > 10_000
    info = probe_media(out, settings)
    assert (info.width, info.height) == (270, 480)
    assert info.has_audio is True  # -map 0:a? kept the sound

    # And the pixels: the logo really is in the corner logo_box asked for.
    capture = cv2.VideoCapture(str(out))
    ok, frame = capture.read()
    capture.release()
    assert ok, "could not decode the branded clip"
    patch = frame[box.y : box.y + box.height, box.x : box.x + box.width].reshape(-1, 3)
    blue, green, red = patch.mean(axis=0)
    assert blue > 200 and red > 200 and green < 60, f"corner is not magenta: {patch.mean(axis=0)}"


def test_an_odd_pixel_offset_is_still_a_valid_overlay(
    source_video: Path, settings, tmp_path: Path
) -> None:
    """A 1:4 mark lands on an odd x, which subsampled chroma is fussy about.

    ``logo_box`` rounds to whole pixels, not to even ones — the browser draws
    into the same box and has no such constraint, and forcing even numbers
    here would put the two engines a pixel apart. So the odd case is rendered
    for real and read back.
    """
    logo = _make_png(tmp_path / "tall.png", 50, 200)
    out = tmp_path / "tall.mp4"
    render_clip(
        source_video,
        make_plan(one_speaker_words()),
        out,
        RenderOptions(height=480, watermark=False, logo_path=str(logo)),
        settings,
    )
    box = logo_box(50, 200, 270, 480)
    assert box.x % 2 == 1, "this test is pointless unless the offset is odd"
    capture = cv2.VideoCapture(str(out))
    ok, frame = capture.read()
    capture.release()
    assert ok
    blue, green, red = (
        frame[box.y : box.y + box.height, box.x : box.x + box.width]
        .reshape(-1, 3)
        .mean(axis=0)
    )
    assert blue > 200 and red > 200 and green < 60


def test_an_unbranded_render_builds_the_same_command_as_before(
    source_video: Path, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free tier output is unchanged, byte for byte — including the argv."""
    calls = _spy_on_ffmpeg(monkeypatch)
    render_clip(
        source_video,
        make_plan(one_speaker_words()),
        tmp_path / "plain.mp4",
        RenderOptions(height=480, watermark=True),
        settings,
    )
    cmd = calls[-1]
    assert "-filter_complex" not in cmd
    assert "-map" not in cmd
    assert cmd.count("-i") == 1
    assert "overlay" not in cmd[cmd.index("-vf") + 1]


def test_our_watermark_beats_their_logo(
    source_video: Path, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence rule 1: a watermarked plan never draws somebody's logo.

    The renderer enforces it too, not just the worker — the two gates are
    independent, and this is the one that decides what ffmpeg is told.
    """
    logo = _make_png(tmp_path / "logo.png", 200, 50)
    calls = _spy_on_ffmpeg(monkeypatch)
    render_clip(
        source_video,
        make_plan(one_speaker_words()),
        tmp_path / "marked.mp4",
        RenderOptions(height=480, watermark=True, logo_path=str(logo)),
        settings,
    )
    assert "-filter_complex" not in calls[-1]
    assert str(logo) not in calls[-1]


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("corrupt.png", b"\x89PNG\r\n\x1a\n" + b"not really a png at all" * 8),
        ("empty.png", b""),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="20"/>'),
    ],
)
def test_an_unreadable_logo_still_renders_the_clip(
    source_video: Path,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    payload: bytes,
) -> None:
    """Corrupt, empty, or undecodable by this build — the clip still ships.

    (An SVG is here because ffmpeg builds without librsvg cannot decode one;
    on a build that can, the overlay is drawn and the render is just as fine,
    so the assertion is about the CLIP, not about the corner.)
    """
    logo = tmp_path / name
    logo.write_bytes(payload)
    out = tmp_path / f"{name}.mp4"

    rendered = render_clip(
        source_video,
        make_plan(one_speaker_words()),
        out,
        RenderOptions(height=480, watermark=False, logo_path=str(logo)),
        settings,
    )
    assert out.is_file() and out.stat().st_size > 10_000
    assert (rendered.width, rendered.height) == (270, 480)
    assert not list(settings.tmp_dir.glob("*.ass"))


def test_a_missing_logo_file_is_not_a_render_failure(
    source_video: Path, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_on_ffmpeg(monkeypatch)
    out = tmp_path / "gone.mp4"
    render_clip(
        source_video,
        make_plan(one_speaker_words()),
        out,
        RenderOptions(height=480, watermark=False, logo_path=str(tmp_path / "nope.png")),
        settings,
    )
    assert out.is_file()
    assert "-filter_complex" not in calls[-1]


def test_a_logo_path_full_of_filtergraph_metacharacters_renders(
    source_video: Path, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The logo path is argv, not filtergraph syntax — the punctuation that
    would split a ``subtitles=`` value cannot split this graph, because the
    path never appears in it (see build_logo_graph)."""
    awkward = tmp_path / "we,ird:dir'name[1]"
    awkward.mkdir()
    logo = _make_png(awkward / "logo.png", 200, 50)
    calls = _spy_on_ffmpeg(monkeypatch)
    out = tmp_path / "awkward.mp4"

    render_clip(
        source_video,
        make_plan(one_speaker_words()),
        out,
        RenderOptions(height=480, watermark=False, logo_path=str(logo)),
        settings,
    )
    graph = _filtergraph(calls[-1])
    assert str(logo) not in graph
    assert graph.count("overlay") == 1
    assert out.is_file() and out.stat().st_size > 10_000


def test_the_logo_box_scales_with_the_export_height() -> None:
    """One mark, three export sizes: same proportions, same corner."""
    for out_h in (960, 1280, 1920):
        out_w = round(out_h * 9 / 16 / 2) * 2
        box = logo_box(200, 50, out_w, out_h)  # a 4:1 wordmark
        assert box.height == pytest.approx(out_h * LOGO_HEIGHT_RATIO, abs=1)
        assert box.width == pytest.approx(out_h * LOGO_HEIGHT_RATIO * 4, abs=1)
        margin = round(out_h * LOGO_MARGIN_RATIO)
        assert (out_w - box.x - box.width, out_h - box.y - box.height) == (margin, margin)


# --------------------------------------------------------------------------- #
# 7. The kit is resolved from the owner's LIVE plan, at render time.
# --------------------------------------------------------------------------- #


def test_the_kit_follows_the_owner_plan_at_render_time(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    from clipcatalyst_api import db, worker
    from clipcatalyst_api.settings import get_settings as live_settings

    client = sandbox.client
    token, user_id = _account(client, "render@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(logo), caption_color=PINK).status_code == 200

    settings = live_settings()
    job = {"user_id": user_id}
    logo_path, color = worker._brand_kit_for(job, settings)
    assert logo_path == str(sandbox.data_dir / "brand" / f"{user_id}.png")
    assert color == PINK

    # A downgrade takes it away on the NEXT RENDER, not at next login.
    db.update_user(user_id, plan_status="canceled")
    assert worker._brand_kit_for(job, settings) == (None, None)

    # An upgrade hands it straight back — same row, same files.
    db.update_user(user_id, plan="starter", plan_status="active")
    assert worker._brand_kit_for(job, settings)[1] == PINK

    # showLogo off = a clean corner, and the colour still applies.
    assert client.put(
        "/v1/me/brand",
        json={"logo": _data_url(logo), "caption_color": PINK, "show_logo": False},
        headers=_bearer(token),
    ).status_code == 200
    assert worker._brand_kit_for(job, settings) == (None, PINK)

    # A row pointing at a file that is gone renders a clean corner, not an error.
    assert client.put(
        "/v1/me/brand",
        json={"logo": _data_url(logo), "caption_color": PINK, "show_logo": True},
        headers=_bearer(token),
    ).status_code == 200
    (sandbox.data_dir / "brand" / f"{user_id}.png").unlink()
    assert worker._brand_kit_for(job, settings) == (None, PINK)


def _fast_pipeline(monkeypatch: pytest.MonkeyPatch, worker) -> list[RenderOptions]:
    """Stub every stage but the one under test; return the captured options.

    The pipeline itself is exercised to death elsewhere; what this suite needs
    from a real job is only the wiring — that what ``_brand_kit_for`` resolves
    is what ``render_clip`` is actually handed.
    """
    from clipcatalyst_api.pipeline.types import (
        AudioFeatures,
        MediaInfo,
        RenderedClip,
        Transcript,
    )

    words = [Word(text=" word", start=float(i), end=float(i) + 0.4) for i in range(10)]
    plans = [make_plan(words, start=0.0, end=5.0)]
    captured: list[RenderOptions] = []

    def fake_render(src, plan, out_path, opts, settings, on_progress=None, track=None):
        captured.append(opts)
        Path(out_path).write_bytes(b"rendered mp4 bytes")
        return RenderedClip(plan=plan, path=str(out_path), width=1080, height=1920)

    monkeypatch.setattr(worker, "probe_media", lambda src, s: MediaInfo(30.0, 640, 360, True))
    monkeypatch.setattr(
        worker, "extract_audio_features", lambda src, s: AudioFeatures([1.0], 0.1, [])
    )
    monkeypatch.setattr(
        worker,
        "get_transcriber",
        lambda s: SimpleNamespace(
            transcribe=lambda src, on_progress=None: Transcript(words=words, text="word")
        ),
    )
    monkeypatch.setattr(worker, "diarization_enabled", lambda s: False)
    monkeypatch.setattr(worker, "plan_clips", lambda t, f, o: list(plans))
    monkeypatch.setattr(worker, "detect_faces", lambda *a, **k: [])
    monkeypatch.setattr(worker, "render_clip", fake_render)
    return captured


def _run_job(client, token: str) -> None:
    """create → upload → start, in eager mode: the whole task runs inline."""
    created = client.post(
        "/v1/jobs",
        json={
            "filename": "source.mp4",
            "size_bytes": 1234,
            "target_length": 15,
            "count": 1,
            "height": 1920,
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    ack = client.put(
        f"/v1/uploads/{job_id}", content=b"stand-in source bytes", headers=_bearer(token)
    )
    assert ack.status_code == 200, ack.text
    started = client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token))
    assert started.status_code == 202, started.text


def test_a_real_job_hands_the_kit_to_the_renderer(
    sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, end to end: a started job renders with the owner's kit."""
    from clipcatalyst_api import db, worker

    client = sandbox.client
    token, user_id = _account(client, "wired@example.com", plan="starter", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(logo), caption_color=PINK).status_code == 200

    captured = _fast_pipeline(monkeypatch, worker)
    _run_job(client, token)

    assert len(captured) == 1
    opts = captured[0]
    assert opts.logo_path == str(sandbox.data_dir / "brand" / f"{user_id}.png")
    assert opts.caption_color == PINK
    assert opts.watermark is False
    assert opts.height == 1920  # starter's ceiling, unrelated but proof of the row

    # Downgrade, then run another job: the very next render loses the kit and
    # gets our mark back. Nothing about the stored kit changed.
    db.update_user(user_id, plan="free", plan_status="")
    _run_job(client, token)
    assert len(captured) == 2
    assert (captured[1].logo_path, captured[1].caption_color) == (None, None)
    assert captured[1].watermark is True
    assert db.get_user_by_id(user_id)["brand_caption_color"] == PINK


def test_anonymous_and_unknown_owners_have_no_kit(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import worker
    from clipcatalyst_api.settings import get_settings as live_settings

    settings = live_settings()
    assert worker._brand_kit_for({"user_id": ""}, settings) == (None, None)
    assert worker._brand_kit_for({"user_id": "deleted-account"}, settings) == (None, None)


def test_free_plans_never_render_a_stored_kit(
    sandbox: SimpleNamespace, tmp_path: Path
) -> None:
    """A free account's kit is stored (by a past subscription) and previewed —
    but never rendered. That gap IS the upsell, so it gets its own test."""
    from clipcatalyst_api import db, worker
    from clipcatalyst_api.settings import get_settings as live_settings

    client = sandbox.client
    token, user_id = _account(client, "upsell@example.com", plan="pro", status="active")
    logo = _make_png(tmp_path / "logo.png", 200, 50).read_bytes()
    assert _put_kit(client, token, logo=_data_url(logo), caption_color=PINK).status_code == 200

    db.update_user(user_id, plan="free", plan_status="")
    settings = live_settings()
    assert worker._brand_kit_for({"user_id": user_id}, settings) == (None, None)
    assert worker._watermark_for({"user_id": user_id}) is True
    # The stored kit is still there — this is a gate, not a deletion.
    assert db.get_user_by_id(user_id)["brand_caption_color"] == PINK


# --------------------------------------------------------------------------- #
# 8. Cross-implementation check: logo_box vs logoBox in the TypeScript.
# --------------------------------------------------------------------------- #

_DRIVER = """
import { logoBox, activeWordColor } from "./brandkit.mjs";
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
process.stdout.write(JSON.stringify({
  boxes: input.cases.map((c) => logoBox(c.natural, c.out)),
  // JSON has no undefined: a null speaker is an UNASSIGNED word, which is
  // exactly the argument shape the browser passes for one.
  colors: input.colors.map((c) =>
    activeWordColor(c.speaker ?? undefined, c.speakerCount, {
      logo: null,
      captionColor: c.captionColor,
      showLogo: true,
    })
  ),
}));
"""


@pytest.fixture(scope="session")
def js_module(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the reference TypeScript to an importable ES module, or skip."""
    tmp_path = tmp_path_factory.mktemp("brandkit-ts")
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available; cannot cross-check against brandkit.ts")

    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    if not tsc.exists() or not TS_SOURCE.exists():
        prebuilt = os.environ.get("BRANDKIT_JS") or str(
            REPO_ROOT / ".croptrack-build" / "brandkit.js"
        )
        if not Path(prebuilt).exists():
            pytest.skip("no TypeScript compiler and no prebuilt brandkit.js to compare")
        target = tmp_path / "brandkit.mjs"
        target.write_text(Path(prebuilt).read_text(encoding="utf8"), encoding="utf8")
        (tmp_path / "driver.mjs").write_text(_DRIVER, encoding="utf8")
        return target

    proc = subprocess.run(
        [
            str(tsc), str(TS_SOURCE),
            "--outDir", str(tmp_path),
            "--module", "es2020",
            "--target", "es2020",
            "--skipLibCheck",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    emitted = tmp_path / "brandkit.js"
    if not emitted.exists():
        pytest.skip(f"tsc did not emit brandkit.js: {proc.stdout or proc.stderr}")
    target = tmp_path / "brandkit.mjs"
    target.write_text(emitted.read_text(encoding="utf8"), encoding="utf8")
    (tmp_path / "driver.mjs").write_text(_DRIVER, encoding="utf8")
    return target


def _run_ts(module: Path, cases: list[dict], colors: list[dict] | None = None) -> dict:
    """Run the TypeScript over the same inputs; returns {boxes, colors}."""
    src = module.parent / "input.json"
    src.write_text(json.dumps({"cases": cases, "colors": colors or []}), encoding="utf8")
    proc = subprocess.run(
        [shutil.which("node") or "node", str(module.parent / "driver.mjs"), str(src)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(module.parent),
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


# Wide/tall/square logos at the three export sizes, a landscape frame (the
# margin comes off the HEIGHT on both axes), the sizes where JavaScript's
# ties-to-+infinity rounding bites (0.045 x 500 = 22.5, x 1500 = 67.5,
# 0.02 x 1225 = 24.5), degenerate natural sizes, and an absurd frame.
_NATURALS = [
    {"width": 800, "height": 200},  # 4:1 wordmark — exactly on the clamp
    {"width": 1600, "height": 200},  # 8:1 — the clamp bites
    {"width": 4000, "height": 100},  # 40:1 — hard against it
    {"width": 300, "height": 900},  # 1:3 tall
    {"width": 512, "height": 512},  # square
    {"width": 0, "height": 0},  # unknown natural size
    {"width": 100, "height": 0},  # half-unknown
    {"width": -50, "height": 25},  # nonsense
]
_OUTS = [
    {"width": 540, "height": 960},
    {"width": 720, "height": 1280},
    {"width": 1080, "height": 1920},
    {"width": 2160, "height": 3840},
    {"width": 1920, "height": 1080},  # landscape
    {"width": 281, "height": 500},  # 0.045 x 500 = 22.5 exactly
    {"width": 844, "height": 1500},  # 0.045 x 1500 = 67.5 exactly
    {"width": 689, "height": 1225},  # 0.02 x 1225 = 24.5 exactly
    {"width": 40, "height": 640},  # absurdly narrow: the origin clamp
]


def test_logo_box_matches_the_typescript(js_module: Path) -> None:
    cases = [{"natural": n, "out": o} for n in _NATURALS for o in _OUTS]
    theirs = _run_ts(js_module, cases)["boxes"]
    assert len(theirs) == len(cases)

    clamped = 0
    for case, ts in zip(cases, theirs):
        natural, out = case["natural"], case["out"]
        mine = logo_box(natural["width"], natural["height"], out["width"], out["height"])
        assert (mine.x, mine.y, mine.width, mine.height) == (
            ts["x"],
            ts["y"],
            ts["width"],
            ts["height"],
        ), f"natural={natural} out={out}: python={mine} ts={ts}"
        if mine.width >= round(out["width"] * LOGO_MAX_WIDTH_RATIO):
            clamped += 1

    # A parity test over cases that never exercise the clamp would agree about
    # nothing interesting.
    assert clamped >= len(_OUTS), "the width clamp was never exercised"


def test_the_colour_rule_matches_the_typescript(js_module: Path) -> None:
    """The whole matrix of ``activeWordColor``, run in both engines.

    This is the rule diarization wins under, and the one place a divergence
    would be invisible in review: an off-by-one in the palette index or a
    speakerCount comparison flipped from >= 2 to > 2 still produces a
    perfectly plausible colour on each side — just not the same one.
    """
    colors = [
        {"speaker": speaker, "speakerCount": count, "captionColor": kit}
        for speaker in (None, 0, 1, 3, 4, 5, -1)
        for count in (0, 1, 2, 3, 5)
        for kit in (None, PINK, "#ABC", "octarine", "")
    ]
    theirs = _run_ts(js_module, [], colors)["colors"]
    assert len(theirs) == len(colors)

    for case, ts in zip(colors, theirs):
        mine = brandkit.active_word_color(
            case["speaker"], case["speakerCount"], case["captionColor"]
        )
        assert mine == ts, f"{case}: python={mine} ts={ts}"
    # And the matrix really does cover both branches.
    assert DEFAULT_CAPTION_COLOR in theirs and PINK in theirs


def test_the_tie_rounding_is_javascripts_not_pythons() -> None:
    """``round(22.5)`` is 22 in Python and 23 in JavaScript. 480-tall exports
    land exactly there, so banker's rounding would put the whole mark one
    pixel out on one engine and not the other."""
    assert logo_box(512, 512, 281, 500).height == 23
    assert logo_box(512, 512, 844, 1500).height == 68
    assert logo_box(512, 512, 689, 1225).x == 689 - 25 - logo_box(512, 512, 689, 1225).width


def test_the_tunables_are_the_typescripts() -> None:
    """Every one of these is pinned in scripts/brandkit.test.mjs too; if the
    two files ever disagree the corner moves on one engine only."""
    source = TS_SOURCE.read_text(encoding="utf8")
    for name, value in (
        ("MAX_LOGO_BYTES", "2_000_000"),
        ("LOGO_HEIGHT_RATIO", "0.045"),
        ("LOGO_MAX_WIDTH_RATIO", "0.32"),
        ("LOGO_MARGIN_RATIO", "0.02"),
        ("DEFAULT_CAPTION_COLOR", '"#a78bfa"'),
    ):
        assert f"export const {name} = {value};" in source, name
    assert (MAX_LOGO_BYTES, LOGO_HEIGHT_RATIO, LOGO_MAX_WIDTH_RATIO, LOGO_MARGIN_RATIO) == (
        2_000_000,
        0.045,
        0.32,
        0.02,
    )
    assert DEFAULT_CAPTION_COLOR == "#a78bfa"


def test_normalize_hex_and_sniffing_agree_with_the_shared_rules() -> None:
    assert normalize_hex("#ABC") == "#aabbcc"
    assert normalize_hex("  #a78bfa ") == "#a78bfa"
    for bad in ("a78bfa", "#12345", "#a78bfa88", "rgb(1,2,3)", "", None):
        assert normalize_hex(bad) is None
    assert sniff_image_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image_type(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"\xef\xbb\xbf  <svg/>") == "image/svg+xml"
    assert sniff_image_type(b"GIF89a") is None
    assert sniff_image_type(b"") is None
    # Not markup, just a mention of svg — the root element is what counts.
    assert sniff_image_type(b"my favourite format is <svg>") is None

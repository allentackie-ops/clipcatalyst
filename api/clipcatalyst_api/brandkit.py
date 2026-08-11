"""Brand kit: the cloud half of the shared core (BRANDKIT.md, Module 3).

A direct port of the pure section of ``lib/studio/brandkit.ts`` — the overlay
geometry, the colour rule both engines obey, and the upload limits — plus the
two things only a server can do: sniff the bytes it was actually handed
instead of trusting a header, and derive a storage path from the account id
that cannot leave the brand directory whatever the upload called itself.

Two rules carry over from the TypeScript verbatim, and everything else follows
from them:

  1. A brand kit must NEVER be the reason a render fails. Every function here
     is total — garbage in gets a truthful fallback out, never a raise.
     ``normalize_hex`` answers None, ``logo_box`` answers a usable box, an
     unreadable logo answers "no overlay" back in render.py.
  2. Diarization wins over the brand colour. With two or more speakers the
     palette keeps colouring the active word, because that colour says WHO is
     talking; the brand colour replaces the default violet for unassigned
     words and single-speaker clips only. ``active_word_color`` is the ONE
     place that decides it, exactly as ``activeWordColor`` is over there.

The plan gate lives with the callers (main.py for storage, worker.py for
render time), not here: this module knows nothing about plans, and
``watermark_required`` precedence — our mark beats their logo — is the
renderer's to apply.

``logo_box`` is cross-checked against the TypeScript by a node subprocess in
``api/tests/test_brandkit.py``, like croptrack and diarize, so a drift in
either direction fails a test rather than shipping two different corners.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .pipeline.diarize import SPEAKER_COLORS
from .settings import Settings

# --- Tunables. The same numbers as lib/studio/brandkit.ts, pinned there by
#     scripts/brandkit.test.mjs and here by api/tests/test_brandkit.py. -------

#: Upload ceiling, applied to the bytes RECEIVED (never to a declared length).
MAX_LOGO_BYTES = 2_000_000
#: Accepted upload types. SVG is allowed: the browser rasterizes it via <img>,
#: ffmpeg via its own decoder — and when a build has no SVG decoder the render
#: simply carries no logo instead of failing (see render._logo_box_for).
LOGO_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/svg+xml",
)
#: The violet the caption highlight has always used (== SPEAKER_COLORS[0]).
DEFAULT_CAPTION_COLOR = "#a78bfa"
#: Logo height as a fraction of output height.
LOGO_HEIGHT_RATIO = 0.045
#: Width ceiling as a fraction of output width, for wordmark-shaped logos.
LOGO_MAX_WIDTH_RATIO = 0.32
#: Corner margin, as a fraction of output HEIGHT on both axes.
LOGO_MARGIN_RATIO = 0.02

#: The ONLY filename extensions a stored logo can have: one per accepted type,
#: chosen by the SNIFFED type. Nothing derived from the upload's own filename
#: ever reaches the filesystem (see logo_storage_name).
LOGO_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}

#: The inverse map, for serving a stored logo back with an honest type. Built
#: from LOGO_EXTENSIONS so the two can never drift apart.
LOGO_MEDIA_TYPES: dict[str, str] = {
    extension: content_type for content_type, extension in LOGO_EXTENSIONS.items()
}

#: The name of ``settings.brand_dir`` inside the data dir. A logo is stored on
#: the user row as "<BRAND_DIR_NAME>/<user id>.<ext>" — relative, so the row
#: stays portable across boxes whose data dirs differ.
BRAND_DIR_NAME = "brand"

#: An account id is a uuid4 hex, but founder-edited rows exist; the filename
#: is built only from ids that are already safe, rather than by sanitizing a
#: hostile one into looking safe.
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_UTF8_BOM = b"\xef\xbb\xbf"
#: How far into a file we look for an ``<svg`` root. An XML declaration plus a
#: DOCTYPE is ~150 bytes; a kilobyte is generous without being a scan.
_SVG_SNIFF_BYTES = 1024

WRONG_TYPE_MESSAGE = (
    "That file isn't an image we can use — pick a PNG, JPEG, WebP or SVG."
)
EMPTY_MESSAGE = "That file is empty — pick a PNG, JPEG, WebP or SVG."


@dataclass(frozen=True)
class LogoBox:
    """Pixel box the logo is drawn into, top-left origin, whole pixels."""

    x: int
    y: int
    width: int
    height: int


def _js_round(x: float) -> int:
    """``Math.round`` semantics: nearest integer, ties toward +infinity.

    Python's built-in ``round`` is banker's rounding, so it answers 22 where
    JavaScript answers 23 for 22.5 — and a 480-tall export puts the logo
    height on exactly that half. Same helper, same reason, as diarize's
    ``_js_round``; the parity test would catch its absence at 500 and 1500.
    """
    if not math.isfinite(x):
        return 0
    floor = math.floor(x)
    return floor + 1 if x - floor >= 0.5 else floor


def _finite_or(value: float | None, fallback: float) -> float:
    """``finiteOr`` from the TypeScript: NaN/inf/None read as the fallback."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else fallback
    return fallback


# --- Colour ----------------------------------------------------------------


def normalize_hex(value: str | None) -> str | None:
    """``"#abc"`` → ``"#aabbcc"``, ``"#AABBCC"`` → ``"#aabbcc"``; else None.

    The same rule as ``normalizeHex`` in the TypeScript, so a colour that
    survives the panel survives the upload: surrounding whitespace is forgiven
    (a human types into that field), nothing else is — no missing hash, no
    named colours, no ``rgb()``, no 4/8-digit alpha forms.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if len(raw) not in (4, 7) or not raw.startswith("#"):
        return None
    body = raw[1:]
    if any(ch not in "0123456789abcdef" for ch in body):
        return None
    if len(body) == 3:
        return f"#{body[0] * 2}{body[1] * 2}{body[2] * 2}"
    return f"#{body}"


def active_word_color(
    speaker: int | None, speaker_count: int, caption_color: str | None
) -> str:
    """The colour one active caption word takes — mirrors ``activeWordColor``.

    Diarization wins: with 2+ speakers an assigned word keeps its speaker
    colour. Everything else — unassigned words, single-speaker clips — takes
    the brand colour when there is one, else the default violet, which IS
    ``SPEAKER_COLORS[0]``, so an unbranded clip is coloured exactly as it was
    before brand kits existed. A stored colour that no longer parses degrades
    to the default rather than emitting a broken value into an ASS file.
    """
    if speaker is not None and speaker_count >= 2:
        return SPEAKER_COLORS[int(speaker) % len(SPEAKER_COLORS)]
    return normalize_hex(caption_color) or DEFAULT_CAPTION_COLOR


# --- Geometry --------------------------------------------------------------


def logo_box(
    natural_width: float, natural_height: float, out_width: float, out_height: float
) -> LogoBox:
    """The box the logo is drawn into, bottom-right, aspect preserved.

    Height is ``LOGO_HEIGHT_RATIO`` of the output height, width follows the
    natural aspect, and a wordmark too wide for ``LOGO_MAX_WIDTH_RATIO`` of the
    output width is clamped with the height RE-DERIVED so it never stretches.
    The margin is ``LOGO_MARGIN_RATIO`` of the output height on both axes.

    Line for line the same as ``logoBox`` in lib/studio/brandkit.ts, including
    the rounding: whole pixels out, because a half-pixel overlay origin makes
    ffmpeg resample the logo. Total by construction — a zero, negative or
    non-finite natural size reads as a square rather than a division by zero,
    and the box is never smaller than 1x1.
    """
    out_w = max(1, _js_round(_finite_or(out_width, 0)))
    out_h = max(1, _js_round(_finite_or(out_height, 0)))
    nat_w = _finite_or(natural_width, 0)
    nat_h = _finite_or(natural_height, 0)
    # Unknown natural size (an SVG with no intrinsic dimensions, a logo that
    # failed to decode) reads as square rather than as a divide by zero.
    aspect = nat_w / nat_h if nat_w > 0 and nat_h > 0 else 1.0

    height = out_h * LOGO_HEIGHT_RATIO
    width = height * aspect
    max_width = out_w * LOGO_MAX_WIDTH_RATIO
    if width > max_width:
        width = max_width
        height = width / aspect
    w = max(1, _js_round(width))
    h = max(1, _js_round(height))
    margin = _js_round(out_h * LOGO_MARGIN_RATIO)
    # The origin only needs clamping for absurd frames (taller than they are
    # wide by more than ~16:1), where the height-derived margin outgrows the
    # width. An off-frame overlay origin would be an ffmpeg error rather than
    # a missing logo, so it is guarded.
    return LogoBox(
        x=max(0, out_w - margin - w),
        y=max(0, out_h - margin - h),
        width=w,
        height=h,
    )


# --- Uploads ---------------------------------------------------------------


def format_mb(size: int) -> str:
    """Megabytes for a message, rounded UP to the nearest 10 KB.

    ``formatMb``'s rule, so a file one byte over the limit never reads as
    "That logo is 2 MB — the limit is 2 MB", and the server's refusal is
    worded identically to the one the panel would have shown — including the
    number's spelling: JavaScript prints a whole 2 as "2", not "2.0".
    """
    value = math.ceil(size / 10_000) / 100
    return str(int(value)) if float(value).is_integer() else str(value)


def sniff_image_type(data: bytes) -> str | None:
    """The image type these BYTES actually are, or None if not one we accept.

    The upload's own ``Content-Type`` is not consulted anywhere: it is client
    input, and the whole point of a whitelisted extension is undone if the
    client picks which one applies. Magic numbers for the raster formats; a
    real ``<svg`` root element for SVG.
    """
    if not data:
        return None
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _looks_like_svg(data):
        return "image/svg+xml"
    return None


def _looks_like_svg(data: bytes) -> bool:
    """An XML document whose markup opens an ``<svg`` root, near the top."""
    head = data[:_SVG_SNIFF_BYTES]
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM) :]
    head = head.lstrip()
    return head.startswith(b"<") and b"<svg" in head.lower()


def validate_logo_bytes(data: bytes) -> tuple[str | None, str | None]:
    """``(sniffed content type, error message)`` — exactly one is not None.

    The messages are the ones ``validateLogoFile`` shows, verbatim, because
    they are shown to the creator: they say what is wrong and what the limit
    is, never "invalid file".
    """
    if not data:
        return None, EMPTY_MESSAGE
    if len(data) > MAX_LOGO_BYTES:
        return None, (
            f"That logo is {format_mb(len(data))} MB — the limit is "
            f"{format_mb(MAX_LOGO_BYTES)} MB."
        )
    content_type = sniff_image_type(data)
    if content_type is None:
        return None, WRONG_TYPE_MESSAGE
    return content_type, None


# --- Storage paths ---------------------------------------------------------


def logo_storage_name(user_id: str, content_type: str) -> str | None:
    """``"brand/<user id>.<ext>"``, or None when the id has no safe form.

    Path safety is structural rather than a filter: the leaf is the account id
    (already a uuid4 hex) plus an extension chosen from LOGO_EXTENSIONS by the
    sniffed type. A ``../../etc/passwd`` filename on the upload cannot reach
    this function's output because the upload's filename is never an input.
    """
    extension = LOGO_EXTENSIONS.get(content_type)
    if extension is None or not _SAFE_USER_ID_RE.match(user_id or ""):
        return None
    return f"{BRAND_DIR_NAME}/{user_id}.{extension}"


def logo_abs_path(settings: Settings, stored: str) -> Path | None:
    """A stored logo name resolved to a real path inside the brand dir.

    None when the row is empty, or when it resolves anywhere else — the row is
    written by ``logo_storage_name`` alone, so this only bites on a hand-edited
    database, which is exactly the case worth refusing to read a file for.
    """
    if not stored:
        return None
    root = settings.brand_dir
    resolved = (settings.data_dir / stored).resolve()
    try:
        inside = resolved.is_relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if inside else None

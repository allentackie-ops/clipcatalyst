"""ASS caption builder with per-word karaoke highlight.

Mirrors the browser canvas renderer (lib/studio/render.ts): words are grouped
into short strips (<= 4 words and <= 18 chars), drawn bold on a subtle
semi-transparent box around 72% of frame height, with exactly one active word
highlighted in brand violet #A78BFA at any moment. The highlight is driven by
`\\t` override-tag transforms inside a single Dialogue event per group, so the
active color follows each word's own start time (ASS colors are &HAABBGGRR,
hence #A78BFA -> &H00FA8BA7&).

Diarized words (word.speaker set) highlight in that speaker's SPEAKER_COLORS
entry instead; words without a speaker keep the violet above, byte for byte —
unless the clip's owner has a brand colour, which replaces that violet in
clips with fewer than two speakers (brandkit.active_word_color decides, and
the browser renderer calls the same rule through the TypeScript twin).

Fontname is "Inter"; libass/fontconfig substitutes the default sans-serif
automatically when Inter is not installed.
"""

from __future__ import annotations

from dataclasses import replace

from ..brandkit import active_word_color
from .diarize import SPEAKER_COLORS
from .types import ClipPlan, Word

CAPTION_MAX_WORDS = 4
CAPTION_MAX_CHARS = 18

ACTIVE_COLOR = r"\1c&HFA8BA7&"  # #A78BFA in &HBBGGRR
INACTIVE_COLOR = r"\1c&HFFFFFF&"


def _hex_to_ass_color(hex_color: str) -> str:
    """`#rrggbb` -> an ASS `\\1c&HBBGGRR&` override (ASS is blue-green-red)."""
    r, g, b = (hex_color[i : i + 2].upper() for i in (1, 3, 5))
    return rf"\1c&H{b}{g}{r}&"


#: The diarization palette in ASS channel order; index = speaker % len(palette).
#: What a word actually gets is decided by ``_active_color`` (a brand colour can
#: replace the violet below when the clip has fewer than two speakers), but the
#: mapping from a palette entry to an override tag is exactly this.
SPEAKER_ACTIVE_COLORS = tuple(_hex_to_ass_color(c) for c in SPEAKER_COLORS)


def _timestamp(seconds: float) -> str:
    """ASS H:MM:SS.CC timestamp (centisecond precision, never negative)."""
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _escape_text(text: str) -> str:
    """Escape braces (override-tag delimiters) and fold newlines to \\N."""
    return (
        text.replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
        .replace("\r", r"\N")
    )


def group_words(words: list[Word]) -> list[list[Word]]:
    """Group caption words into strips of <= 4 words and <= 18 chars.

    Straight port of buildCaptionGroups in lib/studio/render.ts (whitespace
    trimmed, empty words dropped, char budget counts single joining spaces).
    """
    cleaned = [
        replace(w, text=t)  # keeps start/end AND the diarized speaker index
        for w in words
        if (t := w.text.strip())
    ]
    groups: list[list[Word]] = []
    current: list[Word] = []
    chars = 0
    for w in cleaned:
        candidate = len(w.text) if not current else chars + 1 + len(w.text)
        if current and (len(current) >= CAPTION_MAX_WORDS or candidate > CAPTION_MAX_CHARS):
            groups.append(current)
            current = []
            chars = 0
        chars = len(w.text) if not current else chars + 1 + len(w.text)
        current.append(w)
    if current:
        groups.append(current)
    return groups


def speaker_count(words: list[Word]) -> int:
    """Distinct diarized speakers in a clip's words (unassigned ones aside).

    The same count the browser derives from the clip's words, and the same one
    ``worker._clip_out`` reports — it is what decides whether diarization is
    colouring this clip at all, and therefore whether a brand colour applies.
    """
    return len({w.speaker for w in words if getattr(w, "speaker", None) is not None})


def _active_color(word: Word, speakers: int, caption_color: str | None) -> str:
    """The highlight color for one word, in ASS's blue-green-red channels.

    The decision itself is ``brandkit.active_word_color`` — the one place both
    engines agree: a diarized word in a 2+ speaker clip keeps its palette
    entry, everything else takes the brand colour or the legacy violet. That
    violet round-trips to exactly ``ACTIVE_COLOR``, so an unbranded clip is
    still byte-identical to the pre-brand-kit output.
    """
    speaker = getattr(word, "speaker", None)
    return _hex_to_ass_color(active_word_color(speaker, speakers, caption_color))


def _group_text(
    group: list[Word],
    group_start: float,
    group_end: float,
    speakers: int,
    caption_color: str | None,
) -> str:
    """One group's event text: each word carries its highlight transforms."""
    parts: list[str] = []
    for i, w in enumerate(group):
        active = _active_color(w, speakers, caption_color)
        on_ms = max(0, round((w.start - group_start) * 1000))
        tags = active if on_ms == 0 else (
            INACTIVE_COLOR + rf"\t({on_ms},{on_ms},{active})"
        )
        if i + 1 < len(group):  # revert to white when the next word takes over
            off_ms = max(on_ms, round((group[i + 1].start - group_start) * 1000))
            tags += rf"\t({off_ms},{off_ms},{INACTIVE_COLOR})"
        parts.append("{" + tags + "}" + _escape_text(w.text))
    return " ".join(parts)


def build_ass(
    plan: ClipPlan, height: int, watermark: bool, caption_color: str | None = None
) -> str:
    """Build a complete ASS document for one clip (words already re-based).

    ``watermark`` is our mark, and it is the ONLY thing that writes the
    Watermark dialogue line — a clip carrying the owner's own logo (drawn by
    render.py, over the video rather than into the subtitles) must not also
    carry ours. ``caption_color`` is the owner's brand colour, applied per the
    rule in ``_active_color``; None keeps today's violet.
    """
    width = round(height * 9 / 16 / 2) * 2  # nearest even 9:16 width
    font_size = max(1, round(height * 0.042))
    wm_size = max(1, round(height * 0.021))
    pad = max(1, round(font_size * 0.42))  # box padding, like the canvas padY
    margin_v = round(height * 0.235)  # strip lands around 72% of frame height
    wm_margin = max(1, round(height * 0.02))
    clip_len = max(0.1, plan.end - plan.start)

    lines = [
        "[Script Info]",
        "; ClipCatalyst karaoke captions",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # BorderStyle=4: libass background box in BackColour (subtle, 55% black);
        # inactive words white, bold, Inter with automatic sans fallback.
        f"Style: Caption,Inter,{font_size},&H00FFFFFF,&H00FA8BA7,"
        f"&H73000000,&H73000000,-1,0,0,0,100,100,0,0,4,{pad},0,"
        f"2,{pad},{pad},{margin_v},1",
        # Watermark: small, ~55% alpha white, bottom-right.
        f"Style: Watermark,Inter,{wm_size},&H73FFFFFF,&H73FFFFFF,"
        f"&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,"
        f"3,{wm_margin},{wm_margin},{wm_margin},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text",
    ]

    speakers = speaker_count(plan.words)
    for group in group_words(plan.words):
        start = group[0].start
        end = max(group[-1].end, start + 0.01)
        lines.append(
            f"Dialogue: 0,{_timestamp(start)},{_timestamp(end)},Caption,,0,0,0,"
            f"{_group_text(group, start, end, speakers, caption_color)}"
        )

    if watermark:
        # Always-on brand mark (no emoji: libass font coverage is unreliable).
        lines.append(
            f"Dialogue: 1,{_timestamp(0)},{_timestamp(clip_len)},Watermark,,"
            f"0,0,0,ClipCatalyst"
        )

    return "\n".join(lines) + "\n"

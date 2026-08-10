"""ASS caption builder with per-word karaoke highlight.

Mirrors the browser canvas renderer (lib/studio/render.ts): words are grouped
into short strips (<= 4 words and <= 18 chars), drawn bold on a subtle
semi-transparent box around 72% of frame height, with exactly one active word
highlighted in brand violet #A78BFA at any moment. The highlight is driven by
`\\t` override-tag transforms inside a single Dialogue event per group, so the
active color follows each word's own start time (ASS colors are &HAABBGGRR,
hence #A78BFA -> &H00FAB8A7&).

Fontname is "Inter"; libass/fontconfig substitutes the default sans-serif
automatically when Inter is not installed.
"""

from __future__ import annotations

from .types import ClipPlan, Word

CAPTION_MAX_WORDS = 4
CAPTION_MAX_CHARS = 18

ACTIVE_COLOR = r"\1c&HFAB8A7&"  # #A78BFA in &HBBGGRR
INACTIVE_COLOR = r"\1c&HFFFFFF&"


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
        Word(text=t, start=w.start, end=w.end)
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


def _group_text(group: list[Word], group_start: float, group_end: float) -> str:
    """One group's event text: each word carries its highlight transforms."""
    parts: list[str] = []
    for i, w in enumerate(group):
        on_ms = max(0, round((w.start - group_start) * 1000))
        tags = ACTIVE_COLOR if on_ms == 0 else (
            INACTIVE_COLOR + rf"\t({on_ms},{on_ms},{ACTIVE_COLOR})"
        )
        if i + 1 < len(group):  # revert to white when the next word takes over
            off_ms = max(on_ms, round((group[i + 1].start - group_start) * 1000))
            tags += rf"\t({off_ms},{off_ms},{INACTIVE_COLOR})"
        parts.append("{" + tags + "}" + _escape_text(w.text))
    return " ".join(parts)


def build_ass(plan: ClipPlan, height: int, watermark: bool) -> str:
    """Build a complete ASS document for one clip (words already re-based)."""
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
        f"Style: Caption,Inter,{font_size},&H00FFFFFF,&H00FAB8A7,"
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

    for group in group_words(plan.words):
        start = group[0].start
        end = max(group[-1].end, start + 0.01)
        lines.append(
            f"Dialogue: 0,{_timestamp(start)},{_timestamp(end)},Caption,,0,0,0,"
            f"{_group_text(group, start, end)}"
        )

    if watermark:
        # Always-on brand mark (no emoji: libass font coverage is unreliable).
        lines.append(
            f"Dialogue: 1,{_timestamp(0)},{_timestamp(clip_len)},Watermark,,"
            f"0,0,0,ClipCatalyst"
        )

    return "\n".join(lines) + "\n"

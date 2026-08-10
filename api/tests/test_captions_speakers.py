"""Speaker-colored caption tests (SPEAKERS.md §3, cloud renderer).

Two contracts guarded here:

1. Words carrying a diarized ``speaker`` index highlight in that speaker's
   ``SPEAKER_COLORS`` entry, converted to ASS's blue-green-red channel order
   (``#rrggbb`` -> ``&HBBGGRR``).
2. A plan with NO speakers renders byte-identically to the pre-diarization
   builder. ``GOLDEN_NO_SPEAKERS`` below was captured by running the previous
   ``build_ass`` (before ``Word.speaker`` existed) on the same fixture.
"""

from __future__ import annotations

import re

from clipcatalyst_api.pipeline.captions import (
    ACTIVE_COLOR,
    INACTIVE_COLOR,
    SPEAKER_ACTIVE_COLORS,
    build_ass,
)
from clipcatalyst_api.pipeline.diarize import SPEAKER_COLORS
from clipcatalyst_api.pipeline.types import ClipPlan, Word

# The palette converted by hand: #rrggbb -> \1c&HBBGGRR& (ASS is blue-green-red).
VIOLET_0 = r"\1c&HFA8BA7&"  # #a78bfa
AMBER_1 = r"\1c&H24BFFB&"  # #fbbf24
SKY_2 = r"\1c&HF8BD38&"  # #38bdf8
ROSE_3 = r"\1c&H8571FB&"  # #fb7185

TAG_BLOCK = re.compile(r"\{[^}]*\}")


def make_plan(words: list[Word], end: float = 10.0) -> ClipPlan:
    return ClipPlan(
        id="clip-1",
        start=2.0,
        end=2.0 + end,
        score=80,
        title="Test clip",
        hooks=[],
        reason="",
        tip="",
        words=words,
    )


def caption_dialogues(ass: str) -> list[str]:
    return [
        line
        for line in ass.splitlines()
        if line.startswith("Dialogue:") and ",Caption," in line
    ]


def event_for(ass: str, word: str) -> str:
    matches = [line for line in caption_dialogues(ass) if word in line]
    assert len(matches) == 1, f"expected exactly one event containing {word!r}"
    return matches[0]


class TestChannelOrder:
    def test_palette_converts_rgb_to_ass_bgr(self) -> None:
        # #a78bfa: R=A7 G=8B B=FA -> &H FA 8B A7 (blue first!), etc.
        assert SPEAKER_ACTIVE_COLORS == (VIOLET_0, AMBER_1, SKY_2, ROSE_3)

    def test_palette_covers_every_speaker_color(self) -> None:
        assert len(SPEAKER_ACTIVE_COLORS) == len(SPEAKER_COLORS)


class TestSpeakerColors:
    def two_speaker_plan(self) -> ClipPlan:
        # Char budget (18) splits the groups exactly at the turn boundary:
        # [Hello wonderful] then [totally agreed].
        return make_plan(
            [
                Word("Hello", 0.2, 0.6, speaker=0),
                Word(" wonderful", 0.7, 1.2, speaker=0),
                Word(" totally", 2.0, 2.5, speaker=1),
                Word(" agreed", 2.6, 3.1, speaker=1),
            ]
        )

    def test_each_speakers_words_highlight_in_their_color(self) -> None:
        ass = build_ass(self.two_speaker_plan(), height=960, watermark=False)
        speaker0_event = event_for(ass, "wonderful")
        speaker1_event = event_for(ass, "totally")
        assert VIOLET_0 in speaker0_event
        assert AMBER_1 not in speaker0_event
        assert AMBER_1 in speaker1_event
        assert VIOLET_0 not in speaker1_event
        # The legacy unassigned violet appears nowhere: every word has a speaker.
        # Speaker 0's violet IS the legacy active color — the spec's promise
        # ("speaker 0 keeps the exact violet"). They were only distinct while
        # the legacy constant carried a transposed-byte bug.
        assert VIOLET_0 == ACTIVE_COLOR
        # Inactive words still revert to white.
        assert INACTIVE_COLOR in speaker0_event
        assert INACTIVE_COLOR in speaker1_event

    def test_mixed_group_colors_each_word_individually(self) -> None:
        # One group (4 words, 14 chars) spanning speakers 0, None, and 1.
        ass = build_ass(
            make_plan(
                [
                    Word("one", 0.0, 0.3, speaker=0),
                    Word(" two", 0.4, 0.7),  # unassigned -> legacy violet
                    Word(" three", 0.8, 1.1, speaker=1),
                    Word(" four", 1.2, 1.5, speaker=3),
                ]
            ),
            height=960,
            watermark=False,
        )
        line = event_for(ass, "three")
        blocks = TAG_BLOCK.findall(line)
        assert len(blocks) == 4
        assert VIOLET_0 in blocks[0]
        assert ACTIVE_COLOR in blocks[1]
        assert AMBER_1 in blocks[2]
        assert ROSE_3 in blocks[3]

    def test_speaker_index_wraps_around_the_palette(self) -> None:
        ass = build_ass(
            make_plan(
                [
                    Word("wrapfour", 0.0, 0.4, speaker=4),  # 4 % 4 -> violet
                    Word(" wrapfive", 1.5, 1.9, speaker=5),  # 5 % 4 -> amber
                ]
            ),
            height=960,
            watermark=False,
        )
        assert VIOLET_0 in event_for(ass, "wrapfour")
        assert AMBER_1 in event_for(ass, "wrapfive")


# --- the byte-identical regression ------------------------------------------

# Captured from the pre-diarization build_ass on no_speaker_words()
# (height=960, watermark=True), then DELIBERATELY updated once: the original
# capture carried a transposed-byte violet (&HFAB8A7& — periwinkle, not brand
# violet). The golden now holds the corrected &HFA8BA7& so it guards both the
# no-speaker structure AND the right color. Do not regenerate casually.
GOLDEN_NO_SPEAKERS = r"""[Script Info]
; ClipCatalyst karaoke captions
ScriptType: v4.00+
PlayResX: 540
PlayResY: 960
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Inter,40,&H00FFFFFF,&H00FA8BA7,&H73000000,&H73000000,-1,0,0,0,100,100,0,0,4,17,0,2,17,17,226,1
Style: Watermark,Inter,20,&H73FFFFFF,&H73FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,3,19,19,19,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text
Dialogue: 0,0:00:00.20,0:00:01.70,Caption,,0,0,0,{\1c&HFA8BA7&\t(500,500,\1c&HFFFFFF&)}Hello {\1c&HFFFFFF&\t(500,500,\1c&HFA8BA7&)\t(1100,1100,\1c&HFFFFFF&)}world {\1c&HFFFFFF&\t(1100,1100,\1c&HFA8BA7&)}this
Dialogue: 0,0:00:01.80,0:00:03.00,Caption,,0,0,0,{\1c&HFA8BA7&\t(300,300,\1c&HFFFFFF&)}is {\1c&HFFFFFF&\t(300,300,\1c&HFA8BA7&)\t(500,500,\1c&HFFFFFF&)}a {\1c&HFFFFFF&\t(500,500,\1c&HFA8BA7&)}karaoke
Dialogue: 0,0:00:03.10,0:00:05.60,Caption,,0,0,0,{\1c&HFA8BA7&\t(800,800,\1c&HFFFFFF&)}caption {\1c&HFFFFFF&\t(800,800,\1c&HFA8BA7&)\t(1900,1900,\1c&HFFFFFF&)}test {\1c&HFFFFFF&\t(1900,1900,\1c&HFA8BA7&)}strip
Dialogue: 1,0:00:00.00,0:00:10.00,Watermark,,0,0,0,ClipCatalyst
"""


def no_speaker_words() -> list[Word]:
    return [
        Word("Hello", 0.2, 0.6),
        Word(" world", 0.7, 1.2),
        Word(" this", 1.3, 1.7),
        Word(" is", 1.8, 2.0),
        Word(" a", 2.1, 2.2),
        Word(" karaoke", 2.3, 3.0),
        Word(" caption", 3.1, 3.8),
        Word(" test", 3.9, 4.5),
        Word(" strip", 5.0, 5.6),
    ]


class TestNoSpeakerRegression:
    def test_unassigned_words_render_byte_identically_to_before(self) -> None:
        ass = build_ass(make_plan(no_speaker_words()), height=960, watermark=True)
        assert ass == GOLDEN_NO_SPEAKERS

    def test_unassigned_words_use_only_the_legacy_colors(self) -> None:
        ass = build_ass(make_plan(no_speaker_words()), height=960, watermark=False)
        colors = set(re.findall(r"\\1c&H[0-9A-F]+&", ass))
        assert colors == {ACTIVE_COLOR, INACTIVE_COLOR}

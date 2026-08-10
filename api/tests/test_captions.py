"""Unit tests for the ASS karaoke caption builder."""

from __future__ import annotations

import re

from clipcatalyst_api.pipeline.captions import build_ass, group_words
from clipcatalyst_api.pipeline.types import ClipPlan, Word

ACTIVE = r"\1c&HFA8BA7&"
INACTIVE = r"\1c&HFFFFFF&"
TIMESTAMP = r"\d+:\d{2}:\d{2}\.\d{2}"
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


def sample_words() -> list[Word]:
    # Leading spaces mirror what faster-whisper emits.
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


def caption_dialogues(ass: str) -> list[str]:
    return [
        line
        for line in ass.splitlines()
        if line.startswith("Dialogue:") and ",Caption," in line
    ]


def dialogue_text(line: str) -> str:
    # 9 fields (Layer..Text) = 8 commas before Text; Text may contain commas.
    return line.split(",", 8)[8]


def visible_words(line: str) -> list[str]:
    return TAG_BLOCK.sub("", dialogue_text(line)).split()


class TestStructure:
    def test_sections_and_play_res(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=960, watermark=True)
        assert ass.index("[Script Info]") < ass.index("[V4+ Styles]") < ass.index("[Events]")
        assert "ScriptType: v4.00+" in ass
        assert "PlayResX: 540" in ass  # even 9:16 width for 960
        assert "PlayResY: 960" in ass
        assert "Style: Caption,Inter," in ass
        assert "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text" in ass

    def test_dialogue_timestamps_are_well_formed(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=960, watermark=True)
        lines = caption_dialogues(ass)
        assert lines, "expected at least one caption event"
        for line in lines:
            assert re.match(rf"Dialogue: 0,{TIMESTAMP},{TIMESTAMP},Caption,", line)

    def test_first_event_starts_at_first_word(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=960, watermark=False)
        assert caption_dialogues(ass)[0].startswith("Dialogue: 0,0:00:00.20,")


class TestKaraoke:
    def test_override_tags_highlight_each_word(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=1920, watermark=False)
        lines = caption_dialogues(ass)
        for line in lines:
            text = dialogue_text(line)
            # Per-word transform tags flip the active word to brand violet.
            assert ACTIVE in text
            assert "\\t(" in text
            # Every word carries its own override block.
            assert text.count("{") == len(visible_words(line))

    def test_only_transitions_between_white_and_violet(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=960, watermark=False)
        colors = set(re.findall(r"\\1c&H[0-9A-F]+&", ass))
        assert colors == {ACTIVE, INACTIVE}


class TestGrouping:
    def test_group_limits_respected_in_output(self) -> None:
        words = [Word(f" w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(30)]
        words += [Word(" supercalifragilistic", 15.0, 15.5), Word(" end", 15.6, 15.9)]
        ass = build_ass(make_plan(words, end=20.0), height=960, watermark=False)
        for line in caption_dialogues(ass):
            group = visible_words(line)
            assert 1 <= len(group) <= 4
            if len(group) > 1:  # a single oversized word still gets its own strip
                assert len(" ".join(group)) <= 18

    def test_group_words_matches_browser_algorithm(self) -> None:
        words = [
            Word("Hello", 0.0, 0.1),
            Word(" world", 0.2, 0.3),
            Word(" this", 0.4, 0.5),
            Word(" is", 0.6, 0.7),  # would make 19 chars -> new group
            Word(" a", 0.8, 0.9),
            Word(" b", 1.0, 1.1),
            Word(" c", 1.2, 1.3),
            Word(" d", 1.4, 1.5),  # 4-word cap reached
            Word(" e", 1.6, 1.7),
        ]
        groups = group_words(words)
        assert [[w.text for w in g] for g in groups] == [
            ["Hello", "world", "this"],
            ["is", "a", "b", "c"],
            ["d", "e"],
        ]

    def test_empty_and_whitespace_words_are_dropped(self) -> None:
        groups = group_words([Word("  ", 0.0, 0.1), Word(" hi", 0.2, 0.3)])
        assert [[w.text for w in g] for g in groups] == [["hi"]]


class TestWatermark:
    def test_watermark_on(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=960, watermark=True)
        wm = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,") and ",Watermark," in ln]
        assert len(wm) == 1
        assert wm[0].endswith("ClipCatalyst")
        assert "⚡" not in ass  # no emoji: libass font coverage is unreliable
        # Always-on: spans the whole clip (plan is 10 s long).
        assert ",0:00:00.00,0:00:10.00,Watermark," in wm[0]

    def test_watermark_off(self) -> None:
        ass = build_ass(make_plan(sample_words()), height=960, watermark=False)
        assert not [ln for ln in ass.splitlines() if ln.startswith("Dialogue:") and ",Watermark," in ln]
        assert "ClipCatalyst\n" not in ass


class TestEscaping:
    def test_braces_and_newlines_escaped(self) -> None:
        words = [Word("{hi}", 0.0, 0.4), Word(" two\nlines", 0.5, 0.9)]
        ass = build_ass(make_plan(words), height=960, watermark=False)
        text = dialogue_text(caption_dialogues(ass)[0])
        assert r"\{hi\}" in text
        assert "\n" not in text
        # The grouping trims whitespace, so the newline word arrives intact.
        assert "two" in text and "lines" in text

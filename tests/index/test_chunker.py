# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `chunk_note` and `detect_lang` (#25)."""

from __future__ import annotations

from memory_manager.index.chunker import Chunk, chunk_note, detect_lang

_TITLE = "Example note"


def _prefix(heading_path: str) -> str:
    if heading_path:
        return f"{_TITLE}\n{heading_path}\n\n"
    return f"{_TITLE}\n\n"


def _content_of(chunk: Chunk) -> str:
    prefix = _prefix(chunk.heading_path)
    assert chunk.text.startswith(prefix)
    return chunk.text[len(prefix) :]


class TestSectionsAndHeadingPaths:
    def test_body_without_headings_is_one_chunk(self) -> None:
        chunks = chunk_note(_TITLE, "Just some plain text.\nSecond line.")

        assert len(chunks) == 1
        assert chunks[0].heading_path == ""
        assert chunks[0].ord == 0
        assert _content_of(chunks[0]) == "Just some plain text.\nSecond line."

    def test_nested_headings_build_heading_path(self) -> None:
        body = (
            "# Setup\n"
            "Intro text.\n"
            "## Database\n"
            "Connect to the database.\n"
            "## Cache\n"
            "Configure the cache.\n"
            "# Deployment\n"
            "Ship it.\n"
        )

        chunks = chunk_note(_TITLE, body)

        paths = [chunk.heading_path for chunk in chunks]
        assert paths == ["Setup", "Setup > Database", "Setup > Cache", "Deployment"]
        assert _content_of(chunks[1]) == "Connect to the database."

    def test_heading_with_no_content_contributes_no_chunk_but_keeps_path(self) -> None:
        body = "# Parent\n## Empty\n## Child\nActual content.\n"

        chunks = chunk_note(_TITLE, body)

        assert len(chunks) == 1
        assert chunks[0].heading_path == "Parent > Child"

    def test_leading_text_before_first_heading_is_chunk_zero(self) -> None:
        body = "Leading paragraph.\n# First heading\nBody.\n"

        chunks = chunk_note(_TITLE, body)

        assert chunks[0].heading_path == ""
        assert _content_of(chunks[0]) == "Leading paragraph."
        assert chunks[1].heading_path == "First heading"


class TestHeadingDetection:
    def test_heading_like_line_inside_fenced_block_is_not_a_heading(self) -> None:
        body = "# Real heading\n```\n# not a heading\n```\nAfter code.\n"

        chunks = chunk_note(_TITLE, body)

        assert len(chunks) == 1
        assert chunks[0].heading_path == "Real heading"
        assert "# not a heading" in _content_of(chunks[0])

    def test_hash_without_space_is_not_a_heading(self) -> None:
        body = "#hashtag is just text.\n"

        chunks = chunk_note(_TITLE, body)

        assert len(chunks) == 1
        assert chunks[0].heading_path == ""
        assert _content_of(chunks[0]) == "#hashtag is just text."


class TestLongSectionSplitting:
    def test_long_section_splits_on_paragraph_boundaries(self) -> None:
        paragraphs = [f"Paragraph {i} " + "word " * 30 for i in range(20)]
        body = "# Big\n" + "\n\n".join(paragraphs) + "\n"
        max_chars = 200

        chunks = chunk_note(_TITLE, body, max_chars=max_chars)

        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.heading_path == "Big"
            assert len(_content_of(chunk)) <= max_chars
        # No paragraph text was lost or reordered.
        rejoined = "\n\n".join(_content_of(c) for c in chunks)
        for paragraph in paragraphs:
            assert paragraph in rejoined

    def test_very_long_single_paragraph_is_hard_split(self) -> None:
        long_word_run = "word " * 400  # one paragraph, far over the limit
        body = f"# Big\n{long_word_run}\n"
        max_chars = 100

        chunks = chunk_note(_TITLE, body, max_chars=max_chars)

        assert len(chunks) > 1
        for chunk in chunks:
            assert len(_content_of(chunk)) <= max_chars
        rejoined = " ".join(_content_of(c).strip() for c in chunks)
        assert rejoined.replace("  ", " ") == long_word_run.strip().replace("  ", " ")

    def test_fenced_block_alone_within_limit_is_not_split(self) -> None:
        code = "```\n" + "\n".join(f"line {i}" for i in range(5)) + "\n```"
        before = "word " * 5
        after = "word " * 5
        body = f"# Big\n{before}\n\n{code}\n\n{after}\n"
        max_chars = len(code) + 5

        chunks = chunk_note(_TITLE, body, max_chars=max_chars)

        assert len(chunks) > 1
        assert any(_content_of(c) == code for c in chunks)
        for chunk in chunks:
            assert len(_content_of(chunk)) <= max_chars


class TestOrdsAndDeterminism:
    def test_ords_are_contiguous(self) -> None:
        body = "# A\ntext\n# B\ntext\n# C\ntext\n"

        chunks = chunk_note(_TITLE, body)

        assert [c.ord for c in chunks] == list(range(len(chunks)))

    def test_same_input_produces_same_chunks(self) -> None:
        body = "# A\nSome text.\n## B\nMore text.\n"

        first = chunk_note(_TITLE, body)
        second = chunk_note(_TITLE, body)

        assert first == second


class TestDetectLang:
    def test_german_text_is_detected(self) -> None:
        text = "Das ist nicht für mich, das ist für die Datenbank und das Backup."

        assert detect_lang(text) == "de"

    def test_english_text_is_detected(self) -> None:
        text = "This is the plan for the database and the cache, not for the frontend."

        assert detect_lang(text) == "en"

    def test_short_or_ambiguous_text_is_none(self) -> None:
        assert detect_lang("ok") is None
        assert detect_lang("database cache frontend") is None

    def test_umlaut_contributes_to_german_signal(self) -> None:
        text = "Für Größe und Übergröße ist das wichtig."

        assert detect_lang(text) == "de"

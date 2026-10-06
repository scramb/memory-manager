# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for note parsing and canonical serialization (ADR-0005)."""

import random
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from memory_manager.vault.note import Note, NoteFormatError, parse, serialize, version
from memory_manager.vault.ulid import new_ulid

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "notes"
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.md"))

_MINIMAL_NOTE = (
    b"---\n"
    b"id: 01KDVDNA007NVN9S5TAZG3TMQ9\n"
    b"title: Minimal note\n"
    b"description: A minimal fixture with only required fields.\n"
    b"type: fact\n"
    b"created: 2026-01-01T00:00:00Z\n"
    b"updated: 2026-01-01T00:00:00Z\n"
    b"---\n"
    b"Body.\n"
)


def _corrupt(original: bytes, old: bytes, new: bytes) -> bytes:
    assert old in original
    return original.replace(old, new)


class TestFixtureRoundTrip:
    """Canonical fixtures must survive parse -> serialize unchanged."""

    @pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
    def test_round_trip_is_byte_identical(self, path: Path) -> None:
        raw = path.read_bytes()
        note = parse(raw)
        assert serialize(note) == raw

    def test_at_least_four_fixtures_exist(self) -> None:
        assert len(FIXTURE_FILES) >= 4


class TestNonCanonicalInput:
    """A human can write valid YAML that is not in canonical form."""

    def test_different_key_order_block_lists_and_quotes_round_trip(self) -> None:
        raw = (
            b"---\n"
            b"updated: 2026-04-05T10:00:00Z\n"
            b"tags:\n"
            b"  - gamma\n"
            b"  - delta\n"
            b"title: 'Non canonical: note'\n"
            b"id: 01KDVDNA00Y31WQWDE0GZ8A1SF\n"
            b'description: "A human-written note with non-canonical YAML."\n'
            b"created: 2026-04-01T09:00:00Z\n"
            b"type: fact\n"
            b"---\n"
            b"Body text here.\n"
        )

        note = parse(raw)
        canonical = serialize(note)
        assert parse(canonical) == note
        # The non-canonical input is not byte-identical to the canonical form.
        assert canonical != raw
        assert note.tags == ("gamma", "delta")
        assert note.title == "Non canonical: note"


class TestRandomNotes:
    """Generated notes with awkward strings must round-trip through bytes."""

    _TRICKY_WORDS = (
        "-leading-dash",
        "key: value",
        "trailing #hash",
        "it's",
        'a "quoted" word',
        "yes",
        "null",
        "1.0",
        "ümlaut äöüß",
        "plain-word",
        "with, comma",
        "with [brackets]",
        "with {braces}",
        "",
    )
    _TYPES = ("user", "feedback", "project", "reference", "fact")

    def _random_text(self, rng: random.Random) -> str:
        words = [rng.choice(self._TRICKY_WORDS) for _ in range(rng.randint(1, 4))]
        text = " ".join(w for w in words if w)
        return text or "fallback text"

    def _random_tuple(self, rng: random.Random) -> tuple[str, ...]:
        count = rng.randint(0, 3)
        return tuple(self._random_text(rng) for _ in range(count))

    def _random_note(self, rng: random.Random) -> Note:
        created = datetime(
            2020 + rng.randint(0, 5),
            rng.randint(1, 12),
            rng.randint(1, 28),
            rng.randint(0, 23),
            rng.randint(0, 59),
            rng.randint(0, 59),
            tzinfo=UTC,
        )
        has_valid_from = rng.random() < 0.5
        valid_from = (
            date(2021 + rng.randint(0, 3), rng.randint(1, 12), rng.randint(1, 28))
            if has_valid_from
            else None
        )
        valid_to = (
            date(2025 + rng.randint(0, 3), rng.randint(1, 12), rng.randint(1, 28))
            if has_valid_from and rng.random() < 0.7
            else None
        )
        return Note(
            id=new_ulid(created),
            title=self._random_text(rng),
            description=self._random_text(rng),
            type=rng.choice(self._TYPES),
            created=created,
            updated=created,
            body=self._random_text(rng) + "\n",
            tags=self._random_tuple(rng),
            aliases=self._random_tuple(rng),
            valid_from=valid_from,
            valid_to=valid_to,
            supersedes=self._random_tuple(rng),
            source=self._random_text(rng) if rng.random() < 0.5 else None,
        )

    def test_two_hundred_random_notes_round_trip(self) -> None:
        rng = random.Random(20261006)  # noqa: S311 - deterministic test fixtures, not crypto
        for _ in range(200):
            note = self._random_note(rng)
            assert parse(serialize(note)) == note


class TestStructuralErrors:
    """The parser must reject structurally broken input with a clear error."""

    def test_missing_opening_delimiter(self) -> None:
        with pytest.raises(NoteFormatError, match="start with"):
            parse(b"id: x\ntitle: y\n")

    def test_missing_closing_delimiter(self) -> None:
        with pytest.raises(NoteFormatError, match="closing"):
            parse(b"---\nid: x\n")

    def test_frontmatter_must_be_a_mapping(self) -> None:
        raw = b"---\n- one\n- two\n---\nBody.\n"
        with pytest.raises(NoteFormatError, match="mapping"):
            parse(raw)

    def test_unknown_key_is_rejected(self) -> None:
        raw = _corrupt(_MINIMAL_NOTE, b"type: fact\n", b"type: fact\nbogus: 1\n")
        with pytest.raises(NoteFormatError, match="unknown") as excinfo:
            parse(raw)
        assert excinfo.value.field == "bogus"

    def test_wrong_type_is_rejected(self) -> None:
        raw = _corrupt(_MINIMAL_NOTE, b"title: Minimal note\n", b"title: 123\n")
        with pytest.raises(NoteFormatError, match="title") as excinfo:
            parse(raw)
        assert excinfo.value.field == "title"

    def test_missing_required_field_is_rejected(self) -> None:
        raw = _corrupt(_MINIMAL_NOTE, b"type: fact\n", b"")
        with pytest.raises(NoteFormatError, match="type") as excinfo:
            parse(raw)
        assert excinfo.value.field == "type"

    def test_naive_timestamp_is_rejected(self) -> None:
        raw = _corrupt(
            _MINIMAL_NOTE,
            b"created: 2026-01-01T00:00:00Z\n",
            b"created: 2026-01-01T00:00:00\n",
        )
        with pytest.raises(NoteFormatError, match="timezone-aware"):
            parse(raw)

    def test_crlf_is_rejected(self) -> None:
        raw = _MINIMAL_NOTE.replace(b"\n", b"\r\n")
        with pytest.raises(NoteFormatError, match="CR"):
            parse(raw)

    def test_bom_is_rejected(self) -> None:
        raw = b"\xef\xbb\xbf" + _MINIMAL_NOTE
        with pytest.raises(NoteFormatError, match="BOM"):
            parse(raw)

    def test_yaml_syntax_error_reports_line_number(self) -> None:
        raw = b"---\nid: x\ntitle: [unclosed\ndescription: y\n---\nBody.\n"
        with pytest.raises(NoteFormatError, match="not valid YAML") as excinfo:
            parse(raw)
        assert excinfo.value.line == 4

    def test_non_utf8_bytes_are_rejected(self) -> None:
        with pytest.raises(NoteFormatError, match="UTF-8"):
            parse(b"---\n" + b"\xff\xfe" + b"\n---\nBody.\n")


class TestVersion:
    def test_is_lowercase_hex_sha256(self) -> None:
        digest = version(_MINIMAL_NOTE)
        assert len(digest) == 64
        assert digest == digest.lower()
        int(digest, 16)  # raises if not hex

    def test_differs_for_different_content(self) -> None:
        other = _corrupt(_MINIMAL_NOTE, b"Body.\n", b"Other body.\n")
        assert version(_MINIMAL_NOTE) != version(other)


class TestUlid:
    def test_new_ulid_has_valid_shape(self) -> None:
        from memory_manager.vault.ulid import is_ulid

        ulid = new_ulid()
        assert len(ulid) == 26
        assert is_ulid(ulid)

    def test_new_ulid_is_deterministic_in_timestamp_part(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        a = new_ulid(moment)
        b = new_ulid(moment)
        assert a[:10] == b[:10]  # same millisecond -> same timestamp prefix
        assert a != b  # random part differs

    def test_is_ulid_rejects_bad_shapes(self) -> None:
        from memory_manager.vault.ulid import is_ulid

        assert not is_ulid("too-short")
        assert not is_ulid("01KDVDNA007NVN9S5TAZG3TM")  # 24 chars
        assert not is_ulid("01KDVDNA007NVN9S5TAZG3TMQ9I")  # 27 chars
        assert not is_ulid("8" + "0" * 25)  # first char out of [0-7]
        assert not is_ulid("0ILO1DNA007NVN9S5TAZG3TMQ9")  # excluded letters

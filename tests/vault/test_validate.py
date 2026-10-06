# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for semantic note validation (ADR-0005)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid
from memory_manager.vault.validate import (
    MAX_FILE_BYTES,
    NoteInvalid,
    validate,
    validate_bytes,
)

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _note(**overrides: object) -> Note:
    defaults: dict[str, object] = {
        "id": new_ulid(_CREATED),
        "title": "A valid note",
        "description": "A valid description.",
        "type": "fact",
        "created": _CREATED,
        "updated": _CREATED,
        "body": "Body.\n",
        "tags": (),
        "aliases": (),
        "valid_from": None,
        "valid_to": None,
        "supersedes": (),
        "source": None,
    }
    defaults.update(overrides)
    return Note(**defaults)  # type: ignore[arg-type]


def _field_issues(exc: NoteInvalid, field: str | None) -> list[str]:
    return [issue.message for issue in exc.issues if issue.field == field]


class TestTitle:
    def test_title_at_max_length_is_accepted(self) -> None:
        validate(_note(title="x" * 120))

    def test_title_over_max_length_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(title="x" * 121))
        messages = _field_issues(excinfo.value, "title")
        assert any("121 chars, max 120" in m for m in messages)

    def test_title_with_newline_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(title="Line one\nLine two"))
        messages = _field_issues(excinfo.value, "title")
        assert any("newline" in m for m in messages)

    def test_empty_title_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(title=""))
        assert _field_issues(excinfo.value, "title")


class TestDescription:
    def test_description_at_max_length_is_accepted(self) -> None:
        validate(_note(description="x" * 150))

    def test_description_over_max_length_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(description="x" * 151))
        messages = _field_issues(excinfo.value, "description")
        assert any("151 chars, max 150" in m for m in messages)

    def test_description_with_newline_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(description="Line one\nLine two"))
        assert _field_issues(excinfo.value, "description")

    def test_empty_description_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(description=""))
        assert _field_issues(excinfo.value, "description")


class TestType:
    @pytest.mark.parametrize("note_type", ["user", "feedback", "project", "reference", "fact"])
    def test_each_enum_value_is_accepted(self, note_type: str) -> None:
        validate(_note(type=note_type))

    def test_unknown_type_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(type="note"))
        assert _field_issues(excinfo.value, "type")

    def test_type_matching_expected_type_is_accepted(self) -> None:
        validate(_note(type="fact"), expected_type="fact")

    def test_type_mismatching_expected_type_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(type="fact"), expected_type="reference")
        messages = _field_issues(excinfo.value, "type")
        assert any("does not match the directory type" in m for m in messages)

    def test_expected_type_none_skips_the_check(self) -> None:
        validate(_note(type="fact"), expected_type=None)


class TestTags:
    def test_twenty_tags_are_accepted(self) -> None:
        validate(_note(tags=tuple(f"tag-{i}" for i in range(20))))

    def test_twenty_one_tags_are_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(tags=tuple(f"tag-{i}" for i in range(21))))
        messages = _field_issues(excinfo.value, "tags")
        assert any("21 entries, max 20" in m for m in messages)

    def test_tag_with_invalid_charset_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(tags=("Not-Lowercase",)))
        messages = _field_issues(excinfo.value, "tags")
        assert any("Not-Lowercase" in m for m in messages)

    def test_tag_starting_with_hyphen_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(tags=("-leading",)))
        assert _field_issues(excinfo.value, "tags")

    def test_duplicate_tags_are_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(tags=("alpha", "beta", "alpha")))
        messages = _field_issues(excinfo.value, "tags")
        assert any("more than once" in m for m in messages)

    def test_tag_at_max_length_is_accepted(self) -> None:
        validate(_note(tags=("a" + "b" * 39,)))

    def test_tag_over_max_length_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(tags=("a" + "b" * 40,)))
        assert _field_issues(excinfo.value, "tags")


class TestAliases:
    def test_twenty_aliases_are_accepted(self) -> None:
        validate(_note(aliases=tuple(f"alias {i}" for i in range(20))))

    def test_twenty_one_aliases_are_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(aliases=tuple(f"alias {i}" for i in range(21))))
        messages = _field_issues(excinfo.value, "aliases")
        assert any("21 entries, max 20" in m for m in messages)

    def test_alias_at_max_length_is_accepted(self) -> None:
        validate(_note(aliases=("x" * 80,)))

    def test_alias_over_max_length_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(aliases=("x" * 81,)))
        messages = _field_issues(excinfo.value, "aliases")
        assert any("81 chars" in m for m in messages)

    def test_empty_alias_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(aliases=("",)))
        assert _field_issues(excinfo.value, "aliases")

    def test_alias_with_newline_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(aliases=("line one\nline two",)))
        assert _field_issues(excinfo.value, "aliases")


class TestTimestamps:
    def test_updated_equal_to_created_is_accepted(self) -> None:
        validate(_note(created=_CREATED, updated=_CREATED))

    def test_updated_after_created_is_accepted(self) -> None:
        validate(_note(created=_CREATED, updated=datetime(2026, 1, 2, tzinfo=UTC)))

    def test_updated_before_created_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(created=datetime(2026, 1, 2, tzinfo=UTC), updated=_CREATED))
        messages = _field_issues(excinfo.value, "updated")
        assert any(">= created" in m for m in messages)


class TestValidRange:
    def test_valid_to_equal_to_valid_from_is_accepted(self) -> None:
        from datetime import date

        validate(_note(valid_from=date(2026, 1, 1), valid_to=date(2026, 1, 1)))

    def test_valid_to_before_valid_from_is_rejected(self) -> None:
        from datetime import date

        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(valid_from=date(2026, 1, 2), valid_to=date(2026, 1, 1)))
        messages = _field_issues(excinfo.value, "valid_to")
        assert any(">= valid_from" in m for m in messages)

    def test_only_valid_from_set_is_accepted(self) -> None:
        from datetime import date

        validate(_note(valid_from=date(2026, 1, 1), valid_to=None))

    def test_only_valid_to_set_is_accepted(self) -> None:
        from datetime import date

        validate(_note(valid_from=None, valid_to=date(2026, 1, 1)))


class TestSupersedes:
    def test_valid_ulid_is_accepted(self) -> None:
        validate(_note(supersedes=(new_ulid(_CREATED),)))

    def test_non_ulid_entry_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(supersedes=("not-a-ulid",)))
        messages = _field_issues(excinfo.value, "supersedes")
        assert any("not-a-ulid" in m for m in messages)


class TestSource:
    def test_source_at_max_length_is_accepted(self) -> None:
        validate(_note(source="x" * 200))

    def test_source_over_max_length_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(source="x" * 201))
        messages = _field_issues(excinfo.value, "source")
        assert any("201 chars" in m for m in messages)

    def test_source_none_is_accepted(self) -> None:
        validate(_note(source=None))

    def test_empty_source_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(source=""))
        assert _field_issues(excinfo.value, "source")


class TestId:
    def test_non_ulid_id_is_rejected(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(id="not-a-ulid"))
        messages = _field_issues(excinfo.value, "id")
        assert any("not-a-ulid" in m for m in messages)


class TestSizeCap:
    def test_note_at_max_size_is_accepted(self) -> None:
        note = _note()
        current_size = len(serialize(note))
        padding = "x" * (MAX_FILE_BYTES - current_size - 1)
        note = replace(note, body=note.body + padding)
        assert len(serialize(note)) == MAX_FILE_BYTES
        validate(note)

    def test_note_over_max_size_is_rejected(self) -> None:
        note = _note()
        current_size = len(serialize(note))
        padding = "x" * (MAX_FILE_BYTES - current_size)
        note = replace(note, body=note.body + padding)
        assert len(serialize(note)) == MAX_FILE_BYTES + 1
        with pytest.raises(NoteInvalid) as excinfo:
            validate(note)
        messages = _field_issues(excinfo.value, None)
        assert any(
            f"{MAX_FILE_BYTES + 1} bytes, max {MAX_FILE_BYTES}" in m and "consolidate" in m
            for m in messages
        )

    def test_raw_size_overrides_computed_size(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(), raw_size=MAX_FILE_BYTES + 1)
        messages = _field_issues(excinfo.value, None)
        assert any(f"{MAX_FILE_BYTES + 1} bytes" in m for m in messages)


class TestAggregation:
    def test_multiple_violations_are_all_reported(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(title="x" * 121, description="y" * 151, type="note"))
        fields = {issue.field for issue in excinfo.value.issues}
        assert {"title", "description", "type"} <= fields
        assert len(excinfo.value.issues) >= 3

    def test_exception_message_joins_all_issues(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(title="x" * 121, description="y" * 151))
        message = str(excinfo.value)
        assert "title" in message
        assert "description" in message


_ACTION_VERBS = ("shorten", "remove", "use", "split", "set")


class TestMessagesNameAnAction:
    """Every rule's message must name the value, the limit and a fix."""

    def _invalid_notes(self) -> list[tuple[str, Note]]:
        from datetime import date

        return [
            ("title too long", _note(title="x" * 121)),
            ("title empty", _note(title="")),
            ("title newline", _note(title="a\nb")),
            ("description too long", _note(description="y" * 151)),
            ("description empty", _note(description="")),
            ("description newline", _note(description="a\nb")),
            ("type unknown", _note(type="note")),
            ("tags too many", _note(tags=tuple(f"tag-{i}" for i in range(21)))),
            ("tag bad charset", _note(tags=("Not-Lowercase",))),
            ("tag duplicate", _note(tags=("alpha", "alpha"))),
            ("aliases too many", _note(aliases=tuple(f"alias {i}" for i in range(21)))),
            ("alias newline", _note(aliases=("a\nb",))),
            ("alias empty", _note(aliases=("",))),
            ("alias too long", _note(aliases=("x" * 81,))),
            (
                "updated before created",
                _note(created=datetime(2026, 1, 2, tzinfo=UTC), updated=_CREATED),
            ),
            (
                "valid_to before valid_from",
                _note(valid_from=date(2026, 1, 2), valid_to=date(2026, 1, 1)),
            ),
            ("supersedes non-ulid", _note(supersedes=("not-a-ulid",))),
            ("source newline", _note(source="a\nb")),
            ("source empty", _note(source="")),
            ("source too long", _note(source="x" * 201)),
            ("id non-ulid", _note(id="not-a-ulid")),
        ]

    def test_every_rule_message_names_an_action(self) -> None:
        for label, note in self._invalid_notes():
            with pytest.raises(NoteInvalid) as excinfo:
                validate(note)
            assert excinfo.value.issues, f"{label}: expected at least one issue"
            for issue in excinfo.value.issues:
                assert any(verb in issue.message for verb in _ACTION_VERBS), (
                    f"{label}: message for field {issue.field!r} has no action "
                    f"verb ({_ACTION_VERBS}): {issue.message!r}"
                )

    def test_type_mismatch_message_names_an_action(self) -> None:
        with pytest.raises(NoteInvalid) as excinfo:
            validate(_note(type="fact"), expected_type="reference")
        for issue in excinfo.value.issues:
            assert any(verb in issue.message for verb in _ACTION_VERBS)

    def test_size_cap_message_names_an_action(self) -> None:
        note = _note()
        current_size = len(serialize(note))
        padding = "x" * (MAX_FILE_BYTES - current_size)
        note = replace(note, body=note.body + padding)
        with pytest.raises(NoteInvalid) as excinfo:
            validate(note)
        for issue in excinfo.value.issues:
            assert any(verb in issue.message for verb in _ACTION_VERBS)


class TestValidateBytes:
    def test_valid_bytes_return_a_note(self) -> None:
        note = _note()
        parsed = validate_bytes(serialize(note))
        assert parsed == note

    def test_invalid_bytes_raise_note_invalid(self) -> None:
        note = _note(title="x" * 121)
        with pytest.raises(NoteInvalid):
            validate_bytes(serialize(note))

    def test_expected_type_is_passed_through(self) -> None:
        note = _note(type="fact")
        with pytest.raises(NoteInvalid) as excinfo:
            validate_bytes(serialize(note), expected_type="reference")
        assert _field_issues(excinfo.value, "type")

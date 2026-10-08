# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the operator blocklist (`vault/blocklist.py`, #244)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_manager.vault.blocklist import BlocklistConfigError, BlocklistFound, check, load_rules

_EXAMPLE_FILE = Path(__file__).resolve().parents[2] / "examples" / "blocklist.example.toml"


def _write(tmp_path: Path, content: str, *, name: str = "blocklist.toml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadRules:
    def test_no_path_is_an_empty_ruleset(self) -> None:
        rule_set = load_rules(None)
        assert rule_set.categories == ()

    def test_missing_file_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(BlocklistConfigError):
            load_rules(tmp_path / "does-not-exist.toml")

    def test_invalid_toml_refuses(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "this is not [valid toml")
        with pytest.raises(BlocklistConfigError):
            load_rules(path)

    def test_category_without_name_refuses(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[[category]]\nkeywords = ["foo"]\n')
        with pytest.raises(BlocklistConfigError):
            load_rules(path)

    def test_category_without_patterns_or_keywords_refuses(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[[category]]\nname = "empty"\n')
        with pytest.raises(BlocklistConfigError):
            load_rules(path)

    def test_invalid_regex_refuses(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[[category]]\nname = "bad"\npatterns = ["(unterminated"]\n')
        with pytest.raises(BlocklistConfigError):
            load_rules(path)

    def test_valid_file_parses_into_categories(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            '[[category]]\nname = "a"\nkeywords = ["foo"]\n\n'
            '[[category]]\nname = "b"\npatterns = ["bar+"]\n',
        )
        rule_set = load_rules(path)
        assert [category.name for category in rule_set.categories] == ["a", "b"]

    def test_shipped_example_file_loads_without_error(self) -> None:
        rule_set = load_rules(_EXAMPLE_FILE)
        assert len(rule_set.categories) > 0


class TestCheck:
    def test_no_blocklist_file_configured_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BLOCKLIST_FILE", raising=False)
        check("this would match if a blocklist were configured: forbidden-phrase")

    def test_keyword_hit_raises_with_the_category_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[[category]]\nname = "confidential"\nkeywords = ["topsecret"]\n')
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        with pytest.raises(BlocklistFound) as excinfo:
            check("the plan is topsecret for now")
        assert excinfo.value.category == "confidential"

    def test_pattern_hit_raises_with_the_category_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path, '[[category]]\nname = "numbers"\npatterns = ["\\\\d{3}-\\\\d{4}"]\n'
        )
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        with pytest.raises(BlocklistFound) as excinfo:
            check("call 555-1234 about it")
        assert excinfo.value.category == "numbers"

    def test_clean_text_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[[category]]\nname = "confidential"\nkeywords = ["topsecret"]\n')
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        check("just a normal note about groceries and plans")

    def test_keyword_match_is_case_insensitive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[[category]]\nname = "confidential"\nkeywords = ["topsecret"]\n')
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        with pytest.raises(BlocklistFound):
            check("marked TOPSECRET by the sender")

    def test_keyword_does_not_match_inside_a_longer_word(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[[category]]\nname = "confidential"\nkeywords = ["secret"]\n')
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        check("the secretary is out today")

    def test_german_umlaut_keyword_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path, '[[category]]\nname = "confidential-de"\nkeywords = ["Geschäftsgeheimnis"]\n'
        )
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        with pytest.raises(BlocklistFound) as excinfo:
            check("das ist ein Geschäftsgeheimnis, bitte nicht teilen")
        assert excinfo.value.category == "confidential-de"

    def test_german_sharp_s_keyword_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[[category]]\nname = "insult-de"\nkeywords = ["Großmaul"]\n')
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))

        with pytest.raises(BlocklistFound) as excinfo:
            check("so ein Großmaul")
        assert excinfo.value.category == "insult-de"

    def test_message_never_echoes_the_matched_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[[category]]\nname = "confidential"\nkeywords = ["topsecret"]\n')
        monkeypatch.setenv("BLOCKLIST_FILE", str(path))
        text = "the plan is topsecret for now"

        with pytest.raises(BlocklistFound) as excinfo:
            check(text)
        assert text not in str(excinfo.value)
        assert "topsecret" not in str(excinfo.value)

    def test_found_exception_carries_no_text_attribute(self) -> None:
        exc = BlocklistFound("some-category")
        assert not hasattr(exc, "text")
        assert not hasattr(exc, "match")

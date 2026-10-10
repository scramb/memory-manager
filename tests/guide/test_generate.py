# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the `docs/memory-guide.md` generator (#127).

`tests/guide/golden/{INSTRUCTIONS,GUIDE,TOOL_DATA_SENTENCE}` are byte-exact copies of the
three constants as they were hand-written in `mcp/instructions.py` before this generator
existed - the contract the generator must keep: parsing the committed `docs/memory-guide.md`
and rendering from it must reproduce them exactly, not just something close enough.
`tests/guide/golden/CORE_RULES` is the same kind of byte-exact copy for the two-sentence core
(owner decision 2026-10-10, #132); `TOOL_DATA_SENTENCE`'s own golden stays exactly what it was
before `CORE_RULES` existed - just its first sentence, not the second one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_manager.cli import main
from memory_manager.guide import GuideFormatError, is_current, parse_guide
from memory_manager.guide.targets import TARGETS, render_target
from memory_manager.mcp import instructions_generated
from memory_manager.mcp.instructions import (
    CORE_RULES,
    GUIDE,
    INSTRUCTIONS,
    SHORT,
    TOOL_DATA_SENTENCE,
)

_ROOT = Path(__file__).resolve().parents[2]
_GUIDE_PATH = _ROOT / "docs" / "memory-guide.md"
_GENERATED_PATH = _ROOT / "src" / "memory_manager" / "mcp" / "instructions_generated.py"
_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _golden(name: str) -> str:
    return (_GOLDEN_DIR / name).read_text(encoding="utf-8")


def test_parsed_sections_of_the_committed_guide_equal_the_golden_values() -> None:
    sections = parse_guide(_GUIDE_PATH.read_text(encoding="utf-8"))
    assert sections["core"] == _golden("CORE_RULES")
    assert sections["instructions"] == _golden("INSTRUCTIONS")
    assert sections["long"] == _golden("GUIDE")


def test_generated_module_constants_equal_the_golden_values() -> None:
    assert _golden("TOOL_DATA_SENTENCE") == instructions_generated.TOOL_DATA_SENTENCE
    assert _golden("CORE_RULES") == instructions_generated.CORE_RULES
    assert _golden("INSTRUCTIONS") == instructions_generated.INSTRUCTIONS
    assert _golden("GUIDE") == instructions_generated.GUIDE


def test_reexported_constants_equal_the_golden_values() -> None:
    assert _golden("TOOL_DATA_SENTENCE") == TOOL_DATA_SENTENCE
    assert _golden("CORE_RULES") == CORE_RULES
    assert _golden("INSTRUCTIONS") == INSTRUCTIONS
    assert _golden("GUIDE") == GUIDE


def test_tool_data_sentence_is_the_core_rules_first_line() -> None:
    assert CORE_RULES.split("\n", 1)[0] == TOOL_DATA_SENTENCE
    assert CORE_RULES != TOOL_DATA_SENTENCE


def test_instructions_stays_within_the_claude_code_truncation_limit() -> None:
    assert len(INSTRUCTIONS) <= 2048


def test_checked_in_generated_file_is_current() -> None:
    assert is_current(_GUIDE_PATH, _GENERATED_PATH), (
        f"{_GENERATED_PATH} is stale; run `memory-manager instructions generate`"
    )


def test_short_form_is_at_most_600_chars_and_contains_core_verbatim() -> None:
    sections = parse_guide(_GUIDE_PATH.read_text(encoding="utf-8"))
    assert len(sections["short"]) <= 600
    assert sections["core"] in sections["short"]


def test_generated_short_constant_matches_the_committed_guide() -> None:
    sections = parse_guide(_GUIDE_PATH.read_text(encoding="utf-8"))
    assert sections["short"] == instructions_generated.SHORT
    assert sections["short"] == SHORT


def test_checked_in_client_files_are_current_and_carry_the_generated_header() -> None:
    sections = parse_guide(_GUIDE_PATH.read_text(encoding="utf-8"))
    for target in TARGETS.values():
        path = _ROOT / target.output
        text = path.read_text(encoding="utf-8")
        assert text == render_target(target, sections)
        assert text.splitlines()[0].startswith("<!-- generated from docs/memory-guide.md")


class TestParseGuideErrors:
    def test_missing_section_raises(self) -> None:
        text = "<!-- core -->\nx\n<!-- /core -->\n"
        with pytest.raises(GuideFormatError, match="missing required section"):
            parse_guide(text)

    def test_duplicate_section_raises(self) -> None:
        text = (
            "<!-- core -->\nx\n<!-- /core -->\n"
            "<!-- core -->\nx\n<!-- /core -->\n"
            "<!-- instructions -->\nx\n<!-- /instructions -->\n"
            "<!-- long -->\nx\n<!-- /long -->\n"
        )
        with pytest.raises(GuideFormatError, match="duplicate section"):
            parse_guide(text)

    def test_unclosed_section_raises(self) -> None:
        text = "<!-- core -->\nx\n"
        with pytest.raises(GuideFormatError, match="is never closed"):
            parse_guide(text)

    def test_mismatched_closing_marker_raises(self) -> None:
        text = "<!-- core -->\nx\n<!-- /instructions -->\n"
        with pytest.raises(GuideFormatError):
            parse_guide(text)

    def test_core_not_verbatim_in_instructions_raises(self) -> None:
        text = (
            "<!-- core -->\nthe core sentence\n<!-- /core -->\n"
            "<!-- instructions -->\nsomething else entirely\n<!-- /instructions -->\n"
            "<!-- long -->\nthe core sentence\n<!-- /long -->\n"
            "<!-- short -->\nthe core sentence\n<!-- /short -->\n"
        )
        with pytest.raises(GuideFormatError, match="verbatim"):
            parse_guide(text)

    def test_core_not_verbatim_in_short_raises(self) -> None:
        text = (
            "<!-- core -->\nthe core sentence\n<!-- /core -->\n"
            "<!-- instructions -->\nthe core sentence\n<!-- /instructions -->\n"
            "<!-- long -->\nthe core sentence\n<!-- /long -->\n"
            "<!-- short -->\nsomething else entirely\n<!-- /short -->\n"
        )
        with pytest.raises(GuideFormatError, match="verbatim"):
            parse_guide(text)


def _write_minimal_guide(path: Path) -> None:
    path.write_text(
        "<!-- core -->\ncore\n<!-- /core -->\n"
        "<!-- instructions -->\ncore\n<!-- /instructions -->\n"
        "<!-- long -->\ncore\n<!-- /long -->\n"
        "<!-- short -->\ncore\n<!-- /short -->\n",
        encoding="utf-8",
    )


class TestInstructionsGenerateCli:
    def test_check_exits_1_when_stale(self, tmp_path: Path) -> None:
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)

        exit_code = main(
            ["instructions", "generate", "--check", "--guide", str(guide), "--out", str(out)]
        )

        assert exit_code == 1
        assert not out.exists()

    def test_generate_then_check_exits_0(self, tmp_path: Path) -> None:
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)

        exit_code = main(["instructions", "generate", "--guide", str(guide), "--out", str(out)])
        assert exit_code == 0
        assert out.exists()

        exit_code = main(
            ["instructions", "generate", "--check", "--guide", str(guide), "--out", str(out)]
        )
        assert exit_code == 0

    def test_missing_guide_exits_2(self, tmp_path: Path) -> None:
        guide = tmp_path / "missing.md"
        out = tmp_path / "out.py"

        exit_code = main(["instructions", "generate", "--guide", str(guide), "--out", str(out)])

        assert exit_code == 2
        assert not out.exists()

    def test_broken_guide_exits_2(self, tmp_path: Path) -> None:
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        guide.write_text("<!-- core -->\nx\n", encoding="utf-8")

        exit_code = main(["instructions", "generate", "--guide", str(guide), "--out", str(out)])

        assert exit_code == 2
        assert not out.exists()

    def test_unknown_client_exits_2(self, tmp_path: Path) -> None:
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)

        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "instructions",
                    "generate",
                    "--client",
                    "no-such-client",
                    "--guide",
                    str(guide),
                    "--out",
                    str(out),
                ]
            )
        assert excinfo.value.code == 2


class TestInstructionsGenerateAllClients:
    """`--all`/`--client` write client files relative to the current directory, same as
    `tests/test_export.py` does for `export`'s own relative defaults."""

    def test_all_writes_the_module_and_every_client_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)

        exit_code = main(
            ["instructions", "generate", "--all", "--guide", str(guide), "--out", str(out)]
        )

        assert exit_code == 0
        assert out.exists()
        for target in TARGETS.values():
            assert (tmp_path / target.output).exists()

    def test_all_then_all_check_exits_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)
        assert (
            main(["instructions", "generate", "--all", "--guide", str(guide), "--out", str(out)])
            == 0
        )

        exit_code = main(
            [
                "instructions",
                "generate",
                "--all",
                "--check",
                "--guide",
                str(guide),
                "--out",
                str(out),
            ]
        )
        assert exit_code == 0

    def test_two_all_runs_are_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)
        args = ["instructions", "generate", "--all", "--guide", str(guide), "--out", str(out)]

        assert main(args) == 0
        first = {name: (tmp_path / t.output).read_bytes() for name, t in TARGETS.items()}
        first_module = out.read_bytes()

        assert main(args) == 0
        second = {name: (tmp_path / t.output).read_bytes() for name, t in TARGETS.items()}
        second_module = out.read_bytes()

        assert first == second
        assert first_module == second_module

    def test_hand_edited_client_file_fails_check_and_is_left_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)
        assert (
            main(["instructions", "generate", "--all", "--guide", str(guide), "--out", str(out)])
            == 0
        )

        edited = tmp_path / TARGETS["claude-code"].output
        hand_edited_text = edited.read_text(encoding="utf-8") + "hand edit\n"
        edited.write_text(hand_edited_text, encoding="utf-8")

        exit_code = main(
            [
                "instructions",
                "generate",
                "--all",
                "--check",
                "--guide",
                str(guide),
                "--out",
                str(out),
            ]
        )

        assert exit_code == 1
        assert edited.read_text(encoding="utf-8") == hand_edited_text

    def test_client_check_detects_a_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        guide = tmp_path / "guide.md"
        out = tmp_path / "out.py"
        _write_minimal_guide(guide)

        exit_code = main(
            [
                "instructions",
                "generate",
                "--client",
                "generic",
                "--check",
                "--guide",
                str(guide),
                "--out",
                str(out),
            ]
        )

        assert exit_code == 1
        assert not (tmp_path / TARGETS["generic"].output).exists()

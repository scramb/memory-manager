# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `tests/reference_docs.py` itself (#200, ADR-0017): the generator/checker
behind `docs/reference/{tools,cli,config,note-format}.md`, not the pages' content -
that is `scripts/check-docs.sh`/`make lint`'s job once the pages exist.

No `conftest.py`: every fixture below is small enough to live next to its one test.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from reference_docs import (
    REPO_ROOT,
    _note_format_expectations,
    check_config_drift,
    check_note_format_drift,
    error_kinds,
    main,
    render_cli_md,
    render_tools_md,
)


def _copy_reference_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the real `docs/reference/` under `tmp_path` - `main`'s
    `--check` always compares against the committed pages otherwise, which a test
    must never write to."""
    src = REPO_ROOT / "docs" / "reference"
    dst = tmp_path / "docs" / "reference"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def _real_src_root() -> Path:
    return REPO_ROOT / "src" / "memory_manager"


def test_check_passes_on_a_fresh_copy(tmp_path: Path) -> None:
    _copy_reference_dir(tmp_path)
    assert main(["--check"], root=tmp_path, src_root=_real_src_root()) == 0


def test_check_fails_on_a_stale_tools_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference_dir = _copy_reference_dir(tmp_path)
    stale = reference_dir / "tools.md"
    stale.write_text(stale.read_text(encoding="utf-8") + "\nstale addition\n", encoding="utf-8")

    assert main(["--check"], root=tmp_path, src_root=_real_src_root()) == 1
    assert "docs/reference/tools.md" in capsys.readouterr().err


def test_check_fails_on_a_stale_cli_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference_dir = _copy_reference_dir(tmp_path)
    stale = reference_dir / "cli.md"
    stale.write_text(stale.read_text(encoding="utf-8") + "\nstale addition\n", encoding="utf-8")

    assert main(["--check"], root=tmp_path, src_root=_real_src_root()) == 1
    assert "docs/reference/cli.md" in capsys.readouterr().err


def test_check_writes_nothing(tmp_path: Path) -> None:
    reference_dir = _copy_reference_dir(tmp_path)
    before = {page.name: page.read_text(encoding="utf-8") for page in reference_dir.iterdir()}

    main(["--check"], root=tmp_path, src_root=_real_src_root())

    after = {page.name: page.read_text(encoding="utf-8") for page in reference_dir.iterdir()}
    assert before == after


async def test_render_tools_md_is_deterministic() -> None:
    first = await render_tools_md()
    second = await render_tools_md()
    assert first == second


def test_render_cli_md_is_deterministic() -> None:
    assert render_cli_md() == render_cli_md()


def test_render_cli_md_is_independent_of_vault_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_DIR", raising=False)
    without_vault_dir = render_cli_md()

    monkeypatch.setenv("VAULT_DIR", "/some/other/vault")
    with_vault_dir = render_cli_md()

    assert without_vault_dir == with_vault_dir


def test_check_config_drift_reports_a_missing_var_by_name() -> None:
    found = {"FOO_BAR": ["src/memory_manager/example.py"]}
    problems = check_config_drift("nothing documented here", found)
    assert any("FOO_BAR" in problem for problem in problems)


def test_check_config_drift_reports_an_extra_documented_var_by_name() -> None:
    config_text = "| `BAZ_QUX` | unset | not actually read anywhere |"
    problems = check_config_drift(config_text, {})
    assert any("BAZ_QUX" in problem for problem in problems)


def test_check_config_drift_is_clean_when_names_match() -> None:
    found = {"FOO_BAR": ["src/memory_manager/example.py"]}
    config_text = "| `FOO_BAR` | unset | an example |"
    assert check_config_drift(config_text, found) == []


def test_check_note_format_drift_is_clean_against_a_complete_page() -> None:
    complete_text = " ".join(expected for _, expected in _note_format_expectations())
    assert check_note_format_drift(complete_text) == []


def test_check_note_format_drift_reports_a_changed_limit() -> None:
    complete_text = " ".join(expected for _, expected in _note_format_expectations())
    stale_text = complete_text.replace("16384", "16000")

    problems = check_note_format_drift(stale_text)

    assert any("MAX_FILE_BYTES" in problem for problem in problems)


def test_error_kinds_match_docs_compatibility_md() -> None:
    compatibility_text = (REPO_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    line = next(
        line
        for line in compatibility_text.splitlines()
        if line.startswith("| Structured error kinds")
    )
    documented = set(re.findall(r"`([A-Z][A-Za-z]+)`", line))

    assert documented == set(error_kinds())

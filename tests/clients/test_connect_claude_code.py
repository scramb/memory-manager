# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager connect claude-code`/`connect claude-ai` (#137).

Every test runs with `HOME` pointed at a throwaway `tmp_path` directory and
`CLAUDE_CONFIG_DIR` unset, so nothing here ever touches a real user's Claude Code config -
`_isolated_paths` below is autouse for exactly that reason.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from memory_manager.cli import main
from memory_manager.clients.files import ConcurrentModificationError, write_config

_URL = "https://memory.example.com/mcp"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNIPPET = _REPO_ROOT / "integrations" / "claude-code" / "CLAUDE.snippet.md"


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / "home"
    home.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("MEMORY_MANAGER_TOKEN", raising=False)
    monkeypatch.chdir(project_dir)
    return SimpleNamespace(home=home, project_dir=project_dir)


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.mm-backup-*"))


class TestFreshConfig:
    def test_creates_config_with_0600_and_no_trailing_newline(
        self, _isolated_paths: SimpleNamespace
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 0
        assert config_path.exists()
        raw = config_path.read_text(encoding="utf-8")
        assert not raw.endswith("\n")
        data = json.loads(raw)
        assert data == {"mcpServers": {"memory-manager": {"type": "http", "url": _URL}}}
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert _backups(_isolated_paths.home) == []

    def test_dry_run_writes_nothing(
        self, _isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"

        exit_code = main(["connect", "claude-code", "--url", _URL, "--dry-run"])

        assert exit_code == 0
        assert not config_path.exists()
        assert "--dry-run" in capsys.readouterr().out


class TestExistingConfig:
    def _write_initial(self, path: Path, data: dict[str, Any], *, mode: int = 0o644) -> str:
        text = json.dumps(data, indent=2, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return text

    def test_keeps_key_order_and_unknown_keys_of_other_entries(
        self, _isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"
        original = {
            "theme": "dark",
            "mcpServers": {
                "other": {"type": "stdio", "command": "foo", "timeout": 30},
            },
        }
        original_text = self._write_initial(config_path, original)

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 0
        new_text = config_path.read_text(encoding="utf-8")
        assert new_text.index('"theme"') < new_text.index('"mcpServers"')
        data = json.loads(new_text)
        assert data["mcpServers"]["other"] == original["mcpServers"]["other"]  # type: ignore[index]
        assert data["mcpServers"]["memory-manager"] == {"type": "http", "url": _URL}

        diff = capsys.readouterr().out
        removed_lines = [line for line in diff.splitlines() if line.startswith("-")]
        assert not any('"theme"' in line or '"other"' in line for line in removed_lines)

        backups = _backups(_isolated_paths.home)
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original_text
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o644
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o644

    def test_identical_entry_is_a_noop(
        self, _isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"
        original = {"mcpServers": {"memory-manager": {"type": "http", "url": _URL}}}
        original_text = self._write_initial(config_path, original)
        mtime_before = config_path.stat().st_mtime_ns

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 0
        assert "already up to date" in capsys.readouterr().out
        assert config_path.read_text(encoding="utf-8") == original_text
        assert config_path.stat().st_mtime_ns == mtime_before
        assert _backups(_isolated_paths.home) == []

    def test_invalid_json_exits_2_and_leaves_the_file_untouched(
        self, _isolated_paths: SimpleNamespace
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"
        original_bytes = b"{not valid json"
        config_path.write_bytes(original_bytes)

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 2
        assert config_path.read_bytes() == original_bytes
        assert _backups(_isolated_paths.home) == []

    def test_duplicate_keys_are_rejected(self, _isolated_paths: SimpleNamespace) -> None:
        config_path = _isolated_paths.home / ".claude.json"
        original_bytes = b'{\n  "mcpServers": {},\n  "mcpServers": {}\n}'
        config_path.write_bytes(original_bytes)

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 2
        assert config_path.read_bytes() == original_bytes
        assert _backups(_isolated_paths.home) == []

    def test_no_trailing_newline_round_trip_is_preserved(
        self, _isolated_paths: SimpleNamespace
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"
        original = {"mcpServers": {"other": {"type": "stdio", "command": "foo"}}}
        original_text = json.dumps(original, indent=2, ensure_ascii=False)
        config_path.write_text(original_text, encoding="utf-8")
        assert not original_text.endswith("\n")

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 0
        new_text = config_path.read_text(encoding="utf-8")
        assert not new_text.endswith("\n")
        assert '"type": "stdio",\n      "command": "foo"' in new_text


class TestScopeAndTransportGuards:
    def test_project_scope_rejects_inline_token(
        self, _isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _isolated_paths.project_dir / ".mcp.json"
        monkeypatch.setenv("MEMORY_MANAGER_TOKEN", "unused")

        exit_code = main(
            [
                "connect",
                "claude-code",
                "--url",
                _URL,
                "--scope",
                "project",
                "--inline-token",
            ]
        )

        assert exit_code == 2
        assert not config_path.exists()

    def test_project_scope_rejects_stdio_transport(self, _isolated_paths: SimpleNamespace) -> None:
        config_path = _isolated_paths.project_dir / ".mcp.json"

        exit_code = main(["connect", "claude-code", "--scope", "project", "--transport", "stdio"])

        assert exit_code == 2
        assert not config_path.exists()


class TestTokenHandling:
    def test_token_env_without_inline_token_writes_a_placeholder(
        self, _isolated_paths: SimpleNamespace
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"

        exit_code = main(["connect", "claude-code", "--url", _URL, "--token-env"])

        assert exit_code == 0
        data = json.loads(config_path.read_text(encoding="utf-8"))
        headers = data["mcpServers"]["memory-manager"]["headers"]
        assert headers == {"Authorization": "Bearer ${MEMORY_MANAGER_TOKEN}"}

    def test_inline_token_writes_the_value_but_never_shows_it_on_stdout(
        self,
        _isolated_paths: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"
        secret = "supersecretvalue"  # noqa: S105 - a test token value, never a real credential
        monkeypatch.setenv("MEMORY_MANAGER_TOKEN", secret)

        exit_code = main(["connect", "claude-code", "--url", _URL, "--inline-token"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

        data = json.loads(config_path.read_text(encoding="utf-8"))
        headers = data["mcpServers"]["memory-manager"]["headers"]
        assert headers == {"Authorization": f"Bearer {secret}"}

    def test_inline_token_without_value_set_is_rejected(
        self, _isolated_paths: SimpleNamespace
    ) -> None:
        config_path = _isolated_paths.home / ".claude.json"

        exit_code = main(["connect", "claude-code", "--url", _URL, "--inline-token"])

        assert exit_code == 2
        assert not config_path.exists()


class TestClaudeConfigDir:
    def test_claude_config_dir_is_honoured(
        self, _isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom_dir = tmp_path / "custom-config"
        custom_dir.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))

        exit_code = main(["connect", "claude-code", "--url", _URL])

        assert exit_code == 0
        assert (custom_dir / ".claude.json").exists()
        assert not (_isolated_paths.home / ".claude.json").exists()


class TestConcurrentModification:
    def test_write_config_aborts_on_a_stale_base(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude.json"
        path.write_text('{"mcpServers": {}}', encoding="utf-8")

        with pytest.raises(ConcurrentModificationError):
            write_config(
                path, '{"mcpServers": {"x": {}}}', base_text='{"mcpServers": {"stale": 1}}'
            )

        assert path.read_text(encoding="utf-8") == '{"mcpServers": {}}'
        assert _backups(tmp_path) == []


class TestWithInstructions:
    def test_writes_the_generated_snippet_and_is_a_noop_on_a_second_run(
        self, _isolated_paths: SimpleNamespace
    ) -> None:
        rules_path = _isolated_paths.project_dir / ".claude" / "rules" / "memory-manager.md"

        exit_code = main(["connect", "claude-code", "--url", _URL, "--with-instructions"])
        assert exit_code == 0
        assert rules_path.exists()
        if _SNIPPET.exists():
            assert rules_path.read_text(encoding="utf-8") == _SNIPPET.read_text(encoding="utf-8")
        mtime_after_first_run = rules_path.stat().st_mtime_ns

        exit_code = main(["connect", "claude-code", "--url", _URL, "--with-instructions"])
        assert exit_code == 0
        assert rules_path.stat().st_mtime_ns == mtime_after_first_run


class TestConnectClaudeAi:
    def test_prints_the_setup_steps_with_the_url(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["connect", "claude-ai", "--url", _URL])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert _URL in out
        assert "publicly reachable" in out or "reachable" in out

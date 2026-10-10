# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the Open WebUI built-in memory importer (#152, F-02)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from conftest import human_commit

from memory_manager import cli
from memory_manager.cli import main
from memory_manager.config import VaultConfig
from memory_manager.importers.core import dedupe_against_vault, run_import
from memory_manager.importers.openwebui import (
    OpenWebUIFormatError,
    collect,
    collect_from_memories,
    fetch_memories,
)
from memory_manager.queue import WriteQueue
from memory_manager.storage.base import StorageBackend
from memory_manager.storage.git import GitBackend
from memory_manager.vault.git import Git
from memory_manager.vault.note import parse as parse_note
from memory_manager.vault.repo import Repo

# Fake secret, assembled at runtime so no secret-shaped literal sits in the
# source (same convention as tests/importers/test_exports.py:31-33).
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_FAKE_API_KEY = "sk-" + "doesnotmatter0123456789"

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "exports"
_FIXTURE_PATH = _FIXTURES / "openwebui_memories.json"
_TODAY = date(2026, 10, 10)


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _log(remote: Path) -> list[str]:
    result = Git(cwd=remote).run("log", "--format=%H", check=False)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.decode("utf-8").strip("\n").split("\n") if line]


def _remote_file(remote: Path, rel: str) -> bytes | None:
    result = Git(cwd=remote).run("show", f"main:{rel}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


@pytest.fixture
async def backend(vault_config: VaultConfig) -> AsyncIterator[StorageBackend]:
    """A `GitBackend` over a fresh clone, following `tests/importers/test_exports.py`."""
    repo = Repo(vault_config)
    queue = WriteQueue(repo)
    await queue.start()
    try:
        yield GitBackend(queue, repo, vault_config.dir)
    finally:
        await queue.stop()


@pytest.fixture
def repo(vault_config: VaultConfig) -> Repo:
    return Repo(vault_config)


class TestCollectFromFile:
    def test_mapping_from_the_fixture_file(self) -> None:
        items, rejected = collect(_FIXTURE_PATH, namespace="personal", today=_TODAY)

        assert rejected == []
        assert len(items) == 5
        assert all(item.source.startswith("import:openwebui:") for item in items)
        assert all(
            f"Imported from openwebui on {_TODAY.isoformat()}." in item.body for item in items
        )

        user_item = next(item for item in items if item.source.endswith("e5f1"))
        assert user_item.type == "user"
        assert user_item.tags == ()
        assert user_item.created == datetime.fromtimestamp(1760000000, tz=UTC)

        context_no_path = next(item for item in items if item.source.endswith("e5f2"))
        assert context_no_path.type == "fact"
        assert context_no_path.tags == ()

        context_with_path = next(item for item in items if item.source.endswith("e5f3"))
        assert context_with_path.type == "fact"
        assert context_with_path.tags == ("projects-rivermouth",)

        punctuation_twin = next(item for item in items if item.source.endswith("e5f4"))
        assert punctuation_twin.type == "user"
        assert "tea over coffee" in punctuation_twin.body.lower()

        long_path_item = next(item for item in items if item.source.endswith("e5f5"))
        assert len(long_path_item.tags) == 1
        assert len(long_path_item.tags[0]) <= 40
        assert long_path_item.tags[0]  # non-empty

    def test_non_list_top_level_raises_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "export.json"
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        with pytest.raises(OpenWebUIFormatError, match="array"):
            collect(path, namespace="personal", today=_TODAY)

    def test_invalid_json_raises_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "export.json"
        path.write_text("not json at all", encoding="utf-8")

        with pytest.raises(OpenWebUIFormatError):
            collect(path, namespace="personal", today=_TODAY)

    def test_file_over_the_byte_limit_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("memory_manager.importers.openwebui._MAX_FILE_BYTES", 10)
        path = tmp_path / "export.json"
        path.write_text(json.dumps([{"id": "x", "content": "this is longer than 10 bytes"}]))

        with pytest.raises(OpenWebUIFormatError, match="byte import limit"):
            collect(path, namespace="personal", today=_TODAY)


class TestCollectFromMemories:
    def test_non_list_raises_clear_error(self) -> None:
        with pytest.raises(OpenWebUIFormatError, match="array"):
            collect_from_memories({"not": "a list"}, namespace="personal", today=_TODAY)

    def test_non_object_and_empty_content_fall_back_to_index_identifiers(self) -> None:
        data = [
            42,
            {"content": ""},
            {"id": "withid", "content": "   "},
        ]

        items, rejected = collect_from_memories(data, namespace="personal", today=_TODAY)

        assert items == []
        assert [source for source, _ in rejected] == [
            "import:openwebui:[0]",
            "import:openwebui:[1]",
            "import:openwebui:withid",
        ]

    def test_missing_type_field_defaults_to_fact(self) -> None:
        data = [{"id": "no-type", "content": "The user likes tea."}]

        items, rejected = collect_from_memories(data, namespace="personal", today=_TODAY)

        assert rejected == []
        assert items[0].type == "fact"


class TestFetchMemories:
    async def test_sends_bearer_and_trailing_slash_path(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=json.loads(_read_fixture("openwebui_memories.json")))

        data = await fetch_memories(
            "http://localhost:3000", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
        )

        assert isinstance(data, list)
        assert len(captured) == 1
        assert captured[0].url.path == "/api/v1/memories/"
        assert captured[0].headers["authorization"] == f"Bearer {_FAKE_API_KEY}"

    async def test_trailing_slash_on_the_base_url_is_not_doubled(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        await fetch_memories(
            "http://localhost:3000/", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
        )

        assert captured[0].url.path == "/api/v1/memories/"

    async def test_401_raises_clear_error_without_leaking_the_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "Invalid token"})

        with pytest.raises(OpenWebUIFormatError) as excinfo:
            await fetch_memories(
                "https://example.invalid", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
            )

        message = str(excinfo.value)
        assert "invalid" in message.lower()
        assert _FAKE_API_KEY not in message

    async def test_403_api_keys_disabled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, json={"detail": "Use of API key is not enabled in the environment."}
            )

        with pytest.raises(OpenWebUIFormatError, match="disabled"):
            await fetch_memories(
                "https://example.invalid", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
            )

    async def test_404_memories_disabled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Not found"})

        with pytest.raises(OpenWebUIFormatError, match="ENABLE_MEMORIES"):
            await fetch_memories(
                "https://example.invalid", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
            )

    async def test_non_list_json_body_is_accepted_here_rejected_by_collect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "a list"})

        data = await fetch_memories(
            "https://example.invalid", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
        )

        with pytest.raises(OpenWebUIFormatError, match="array"):
            collect_from_memories(data, namespace="personal", today=_TODAY)

    async def test_redirect_is_rejected_not_followed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(307, headers={"location": "https://evil.invalid/steal"})

        with pytest.raises(OpenWebUIFormatError, match="redirect"):
            await fetch_memories(
                "https://example.invalid", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
            )

    async def test_html_body_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>login</html>", headers={})

        with pytest.raises(OpenWebUIFormatError):
            await fetch_memories(
                "https://example.invalid", _FAKE_API_KEY, transport=httpx.MockTransport(handler)
            )


class TestOpenWebUiUrlSafety:
    def test_https_is_accepted_for_any_host(self) -> None:
        assert cli._check_openwebui_url("https://example.invalid") is None

    def test_http_is_accepted_for_loopback_hosts(self) -> None:
        assert cli._check_openwebui_url("http://127.0.0.1:3000") is None
        assert cli._check_openwebui_url("http://localhost:3000") is None

    def test_http_is_rejected_for_a_non_loopback_host(self) -> None:
        error = cli._check_openwebui_url("http://example.invalid")
        assert error is not None
        assert "http" in error.lower()

    def test_unknown_scheme_is_rejected(self) -> None:
        assert cli._check_openwebui_url("ftp://example.invalid") is not None


class TestOpenWebUiCliViaMain:
    def test_missing_env_var_exits_2_naming_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)

        exit_code = main(
            ["import", "openwebui", "--url", "https://example.invalid", "--namespace", "personal"]
        )

        assert exit_code == 2
        assert "OPENWEBUI_API_KEY" in capsys.readouterr().err

    def test_custom_api_key_env_name_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("MY_OWUI_KEY", raising=False)

        exit_code = main(
            [
                "import",
                "openwebui",
                "--url",
                "https://example.invalid",
                "--namespace",
                "personal",
                "--api-key-env",
                "MY_OWUI_KEY",
            ]
        )

        assert exit_code == 2
        assert "MY_OWUI_KEY" in capsys.readouterr().err

    def test_non_loopback_http_url_exits_2_without_touching_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENWEBUI_API_KEY", "irrelevant")

        exit_code = main(
            ["import", "openwebui", "--url", "http://example.invalid", "--namespace", "personal"]
        )

        assert exit_code == 2

    def test_api_key_value_never_appears_in_stdout_or_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
    ) -> None:
        monkeypatch.setenv("OPENWEBUI_API_KEY", _FAKE_API_KEY)
        monkeypatch.setenv("VAULT_REMOTE", str(bare_remote))
        monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))

        seen_keys: list[str] = []

        async def fake_fetch(
            url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
        ) -> object:
            seen_keys.append(api_key)
            return json.loads(_read_fixture("openwebui_memories.json"))

        monkeypatch.setattr(cli, "fetch_openwebui_memories", fake_fetch)

        exit_code = main(
            ["import", "openwebui", "--url", "https://example.invalid", "--namespace", "personal"]
        )

        assert exit_code == 0
        assert seen_keys == [_FAKE_API_KEY]
        captured = capsys.readouterr()
        assert _FAKE_API_KEY not in captured.out
        assert _FAKE_API_KEY not in captured.err


class TestRunImportIntegration:
    async def test_dry_run_writes_nothing(
        self,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        before = _log(bare_remote)
        items, rejected = collect(_FIXTURE_PATH, namespace="personal", today=_TODAY)
        kept, duplicates = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=False)

        assert rejected == []
        assert len(duplicates) == 1  # the punctuation/case twin collapses in-run
        assert len(report.created) == 4
        assert _log(bare_remote) == before

    async def test_apply_writes_and_rerunning_creates_nothing_new(
        self,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        items, _ = collect(_FIXTURE_PATH, namespace="personal", today=_TODAY)
        kept, duplicates = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=True)

        assert len(duplicates) == 1
        assert len(report.created) == 4

        await asyncio.to_thread(repo.sync)
        second_items, _ = collect(_FIXTURE_PATH, namespace="personal", today=_TODAY)
        second_kept, second_duplicates = await dedupe_against_vault(second_items, backend)

        assert second_kept == []
        assert len(second_duplicates) == 5

        second_report = await run_import(second_kept, backend, apply=True)
        assert second_report.created == []

    async def test_fake_secret_item_is_rejected_others_are_imported(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        data = [
            {
                "id": "0198b1a1-caf3-7bee-8dec-a1b2c3d4e5f1",
                "type": "user",
                "path": None,
                "content": f"The user's AWS access key is {_FAKE_AWS_ACCESS_KEY_ID}, do not "
                "lose it.",
                "created_at": 1760000000,
            },
            {
                "id": "0198b1a2-caf3-7bee-8dec-a1b2c3d4e5f2",
                "type": "user",
                "path": None,
                "content": "The user prefers dark mode in every app.",
                "created_at": 1760000100,
            },
        ]
        path = tmp_path / "memories.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        items, rejected = collect(path, namespace="personal", today=_TODAY)
        assert rejected == []

        kept, duplicates = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=True)

        assert duplicates == []
        assert len(report.created) == 1
        assert len(report.rejected) == 1
        source_ref, reason = report.rejected[0]
        assert source_ref.startswith("import:openwebui:")
        assert "secret" in reason.lower() or "aws" in reason.lower()

    async def test_dedupe_against_vault_skips_a_memory_already_imported_by_chatgpt(
        self,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        existing_note = (
            b"---\n"
            b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
            b"title: Prefers tea\n"
            b"description: The user prefers tea over coffee.\n"
            b"type: user\n"
            b"created: 2026-01-01T00:00:00Z\n"
            b"updated: 2026-01-01T00:00:00Z\n"
            b"source: import:chatgpt:line:0\n"
            b"---\n"
            b"The user prefers tea over coffee.\n\n"
            b"Imported from chatgpt on 2026-01-01.\n"
        )
        human_commit(bare_remote, "personal/user/already-there.md", existing_note)

        items, _ = collect(_FIXTURE_PATH, namespace="personal", today=_TODAY)
        await asyncio.to_thread(repo.sync)
        kept, duplicates = await dedupe_against_vault(items, backend)

        # Both the "tea" entry and its punctuation/case twin normalize to the
        # same text as the pre-existing chatgpt note.
        assert len(duplicates) == 2
        assert len(kept) == 3
        assert all("tea over coffee" not in item.body.lower() for item in kept)


class TestEndToEndCommittedNote:
    async def test_imported_note_parses_and_has_expected_source(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        data = [
            {
                "id": "0198b2a1-caf3-7bee-8dec-a1b2c3d4e5f1",
                "type": "context",
                "path": None,
                "content": "The user prefers dark mode in every app.",
                "created_at": 1760000000,
            }
        ]
        path = tmp_path / "memories.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        items, _ = collect(path, namespace="personal", today=_TODAY)
        kept, _ = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=True)

        assert len(report.created) == 1
        note_path = report.created[0]
        note_bytes = _remote_file(bare_remote, note_path)
        assert note_bytes is not None
        note = parse_note(note_bytes)
        assert note.source == "import:openwebui:0198b2a1-caf3-7bee-8dec-a1b2c3d4e5f1"
        assert note.type == "fact"
        assert "Imported from openwebui on 2026-10-10." in note.body

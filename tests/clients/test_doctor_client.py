# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager doctor --client <name>` (#138).

Every test runs with `HOME` pointed at a throwaway `tmp_path` directory, the same
`_isolated_paths` autouse fixture `test_connect_claude_code.py` uses, so nothing here ever
touches a real user's Claude Code config. Postgres-backed cases skip without
`MM_TEST_DATABASE_URL` (`test_database_url`, `tests/conftest.py`) - never silently skipped in
CI, same contract as every other Postgres test in this repository.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from http_fixtures import run_http_server

from memory_manager.auth.scopes import READ_SCOPE, WRITE_SCOPE
from memory_manager.auth.tokens import ALL_NAMESPACES, MEMORY_ROLES, create_token
from memory_manager.cli import main
from memory_manager.vault.git import Git

_PUBLIC_URL = "https://mm-doctor-test.example.invalid"
#: Owner principal for the postgres-backend namespace-matrix test below (ADR-0008
#: addendum, #115) - never needs a matching `users` row (`mm_readable_ns`/
#: `mm_writable_ns` `LEFT JOIN users`, both treat a missing row as still active).
_OWNER_OID = "oid-doctor-test"


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / "home"
    home.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("MEMORY_MANAGER_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.chdir(project_dir)
    return SimpleNamespace(home=home, project_dir=project_dir)


def _git_env(tmp_path: Path, bare_remote: Path) -> dict[str, str]:
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


async def _create_token(
    database_url: str,
    *,
    scopes: list[str],
    namespaces: list[str] | None = None,
    owner_oid: str | None = None,
    roles: tuple[str, ...] = (),
) -> str:
    pool = await asyncpg.create_pool(database_url)
    try:
        plaintext, _info = await create_token(
            pool,
            "doctor-test",
            scopes=scopes,
            namespaces=namespaces or [ALL_NAMESPACES],
            owner_oid=owner_oid,
            roles=roles,
        )
    finally:
        await pool.close()
    return plaintext


async def _create_app_role(admin_database_url: str) -> str:
    """A disposable, non-owner, non-superuser role (ADR-0008 addendum, #116) - only needed
    because `STORAGE_BACKEND=postgres` refuses to start without `DATABASE_APP_ROLE` naming
    one, same pattern as `tests/conformance/test_http.py`'s own `_create_app_role`."""
    role = f"mm_doctor_test_app_{secrets.token_hex(8)}"
    conn = await asyncpg.connect(admin_database_url)
    try:
        await conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await conn.close()
    return role


async def _drop_app_role(admin_database_url: str, database_url: str, role: str) -> None:
    owned_conn: asyncpg.Connection | None
    try:
        owned_conn = await asyncpg.connect(database_url)
    except asyncpg.PostgresError:
        owned_conn = None
    if owned_conn is not None:
        try:
            await owned_conn.execute(f'drop owned by "{role}"')
        finally:
            await owned_conn.close()
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'drop role if exists "{role}"')
    finally:
        await admin_conn.close()


def _archived_paths(bare_remote: Path) -> list[str]:
    result = Git(cwd=bare_remote).run("ls-tree", "-r", "HEAD", "--name-only")
    return result.stdout.decode("utf-8").splitlines()


def _run_doctor_json(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, dict[str, object]]:
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(" ".join(map(str, a))))
    exit_code = main(["doctor", "--client", "claude-code", "--json", *args])
    report = json.loads(captured[0])
    return exit_code, report


def _steps_by_name(report: dict[str, object]) -> dict[str, dict[str, object]]:
    steps = report["steps"]
    assert isinstance(steps, list)
    return {step["name"]: step for step in steps}


class TestEndToEndOverHttp:
    async def test_connect_then_doctor_passes_every_step_against_a_real_server(
        self, tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _git_env(tmp_path, bare_remote)
        async with run_http_server(env) as server:
            exit_code = main(["connect", "claude-code", "--url", server.mcp_url])
            assert exit_code == 0

            doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)

        assert doctor_exit == 0
        assert report["ok"] is True
        steps = _steps_by_name(report)
        for name in (
            "config found",
            "URL reachable",
            "auth",
            "profile resolved",
            "memory_index",
            "memory_write",
            "memory_read",
            "memory_edit",
            "memory_archive",
        ):
            assert steps[name]["status"] == "pass", (name, steps[name])

        archived = _archived_paths(bare_remote)
        assert any(path.startswith("_archive/mm-doctor/") for path in archived), archived


class TestWithDatabase:
    async def test_token_env_against_a_database_backed_server_passes(
        self,
        tmp_path: Path,
        bare_remote: Path,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = {
            **_git_env(tmp_path, bare_remote),
            "DATABASE_URL": test_database_url,
            "PUBLIC_URL": _PUBLIC_URL,
        }
        async with run_http_server(env) as server:
            token = await _create_token(test_database_url, scopes=[READ_SCOPE, WRITE_SCOPE])
            monkeypatch.setenv("MEMORY_MANAGER_TOKEN", token)

            exit_code = main(["connect", "claude-code", "--url", server.mcp_url, "--token-env"])
            assert exit_code == 0

            doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)

        assert doctor_exit == 0
        assert report["ok"] is True
        assert _steps_by_name(report)["auth"]["detail"] == "the configured token was accepted"

    async def test_wrong_token_fails_auth_and_names_the_variable(
        self,
        tmp_path: Path,
        bare_remote: Path,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = {
            **_git_env(tmp_path, bare_remote),
            "DATABASE_URL": test_database_url,
            "PUBLIC_URL": _PUBLIC_URL,
        }
        async with run_http_server(env) as server:
            exit_code = main(["connect", "claude-code", "--url", server.mcp_url, "--token-env"])
            assert exit_code == 0
            monkeypatch.setenv("MEMORY_MANAGER_TOKEN", "this-was-never-issued")

            doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)

        assert doctor_exit == 1
        steps = _steps_by_name(report)
        assert steps["auth"]["status"] == "fail"
        assert "401" in str(steps["auth"]["detail"])
        assert "MEMORY_MANAGER_TOKEN" in str(steps["auth"]["hint"])
        assert "this-was-never-issued" not in json.dumps(report)

    async def test_read_only_token_fails_memory_write_with_a_scope_hint(
        self,
        tmp_path: Path,
        bare_remote: Path,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = {
            **_git_env(tmp_path, bare_remote),
            "DATABASE_URL": test_database_url,
            "PUBLIC_URL": _PUBLIC_URL,
        }
        async with run_http_server(env) as server:
            token = await _create_token(test_database_url, scopes=[READ_SCOPE])
            monkeypatch.setenv("MEMORY_MANAGER_TOKEN", token)
            exit_code = main(["connect", "claude-code", "--url", server.mcp_url, "--token-env"])
            assert exit_code == 0

            doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)

        assert doctor_exit == 1
        steps = _steps_by_name(report)
        assert steps["memory_index"]["status"] == "pass"
        assert steps["memory_write"]["status"] == "fail"
        assert WRITE_SCOPE in str(steps["memory_write"]["detail"])
        assert WRITE_SCOPE in str(steps["memory_write"]["hint"])
        for name in ("memory_read", "memory_edit", "memory_archive"):
            assert steps[name]["status"] == "skip"

    async def test_no_token_against_a_database_backed_server_without_an_as_fails_with_a_hint(
        self,
        tmp_path: Path,
        bare_remote: Path,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = {
            **_git_env(tmp_path, bare_remote),
            "DATABASE_URL": test_database_url,
            "PUBLIC_URL": _PUBLIC_URL,
        }
        async with run_http_server(env) as server:
            exit_code = main(["connect", "claude-code", "--url", server.mcp_url])
            assert exit_code == 0

            doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)

        assert doctor_exit == 1
        steps = _steps_by_name(report)
        assert steps["auth"]["status"] == "fail"
        assert "--token-env" in str(steps["auth"]["hint"])

    async def test_unregistered_namespace_under_the_postgres_backend_names_the_backend_rule(
        self,
        tmp_path: Path,
        bare_remote: Path,
        test_database_url: str,
        admin_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `STORAGE_BACKEND=postgres` refuses to start without `DATABASE_APP_ROLE`
        # (ADR-0008 addendum, #116) - a disposable role is enough, the same
        # `tests/conformance/test_http.py` pattern. With `app_role` set, `mcp/server.py`'s
        # `_require_writable` replaces `mcp/authz.py`'s plain token-namespace check with
        # the ADR-0008 namespace-registry matrix (`src/memory_manager/mcp/server.py`'s own
        # module docstring) - `mm-doctor` is never registered there, so even an
        # unrestricted token (`ALL_NAMESPACES`) with a real owner principal still cannot
        # write to it.
        app_role = await _create_app_role(admin_database_url)
        env = {
            "STORAGE_BACKEND": "postgres",
            "DATABASE_URL": test_database_url,
            "PUBLIC_URL": _PUBLIC_URL,
            "DATABASE_APP_ROLE": app_role,
        }
        try:
            async with run_http_server(env) as server:
                token = await _create_token(
                    test_database_url,
                    scopes=[READ_SCOPE, WRITE_SCOPE],
                    owner_oid=_OWNER_OID,
                    roles=(MEMORY_ROLES[0],),
                )
                monkeypatch.setenv("MEMORY_MANAGER_TOKEN", token)
                exit_code = main(["connect", "claude-code", "--url", server.mcp_url, "--token-env"])
                assert exit_code == 0

                # `storage_backend_from_env` (`_write_hint`'s own call) requires `DATABASE_URL`
                # whenever `STORAGE_BACKEND=postgres`, same as the subprocess's own env above -
                # this is `doctor`'s own process env, read independently of the subprocess's.
                monkeypatch.setenv("STORAGE_BACKEND", "postgres")
                monkeypatch.setenv("DATABASE_URL", test_database_url)
                doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)
        finally:
            await _drop_app_role(admin_database_url, test_database_url, app_role)

        assert doctor_exit == 1
        steps = _steps_by_name(report)
        assert steps["memory_write"]["status"] == "fail"
        assert "must exist and be writable" in str(steps["memory_write"]["hint"])


class TestReadOnlyFlag:
    async def test_read_only_skips_the_write_round_trip(
        self, tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _git_env(tmp_path, bare_remote)
        async with run_http_server(env) as server:
            exit_code = main(["connect", "claude-code", "--url", server.mcp_url])
            assert exit_code == 0

            doctor_exit, report = await asyncio.to_thread(
                _run_doctor_json, monkeypatch, "--read-only"
            )

        assert doctor_exit == 0
        steps = _steps_by_name(report)
        assert steps["memory_index"]["status"] == "pass"
        for name in ("memory_write", "memory_read", "memory_edit", "memory_archive"):
            assert steps[name]["status"] == "skip"


class TestJsonRendering:
    async def test_json_report_is_parseable_and_carries_no_token(
        self, tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _git_env(tmp_path, bare_remote)
        async with run_http_server(env) as server:
            exit_code = main(["connect", "claude-code", "--url", server.mcp_url])
            assert exit_code == 0

            doctor_exit, report = await asyncio.to_thread(_run_doctor_json, monkeypatch)

        assert doctor_exit == 0
        assert report["client"] == "claude-code"
        assert isinstance(report["steps"], list)


class TestNoConfig:
    def test_no_config_fails_the_first_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        doctor_exit, report = _run_doctor_json(monkeypatch)

        assert doctor_exit == 1
        steps = _steps_by_name(report)
        assert steps["config found"]["status"] == "fail"
        assert len(steps) == 1


class TestStdioEntry:
    def test_stdio_entry_against_a_git_vault_passes(
        self, tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _git_env(tmp_path, bare_remote)
        monkeypatch.setenv("VAULT_REMOTE", env["VAULT_REMOTE"])
        monkeypatch.setenv("VAULT_DIR", env["VAULT_DIR"])
        monkeypatch.delenv("DATABASE_URL", raising=False)

        exit_code = main(["connect", "claude-code", "--transport", "stdio"])
        assert exit_code == 0

        doctor_exit, report = _run_doctor_json(monkeypatch)

        assert doctor_exit == 0
        steps = _steps_by_name(report)
        assert steps["URL reachable"]["status"] == "skip"
        assert steps["auth"]["status"] == "skip"
        assert steps["memory_write"]["status"] == "pass"


class TestVaultDoctorRegression:
    def test_doctor_without_client_is_unchanged(self, tmp_path: Path) -> None:
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()

        exit_code = main(["doctor", "--vault", str(vault_dir)])

        assert exit_code == 0

# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for tests against a real Postgres, shared across test packages.

These tests need a reachable Postgres 16 with pgvector, named by
`MM_TEST_DATABASE_URL` (see `make db-up`). Without that variable they skip
locally - but fail outright when `CI` is set, so the database tests can
never silently skip in CI (few dependencies: no testcontainers here).

Each test gets its own fresh `mm_test_<random>` database, created from the
admin connection named by `MM_TEST_DATABASE_URL` and dropped again
afterwards, so tests never see each other's state.

Lives at the `tests/` root (not under `tests/db/`) so every test package -
`tests/db`, `tests/index`, ... - gets these fixtures without the
same-named-`conftest.py`-in-sibling-directories trick `tests/vault` needs
for its bare `from conftest import ...` imports (see the `mypy_path`
comment in `pyproject.toml`).

Git fixtures live in `tests/git_fixtures.py` and are re-exported here, so
every test package can use them.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from git_fixtures import bare_remote, human_commit, human_delete, human_rename, vault_config

__all__ = [
    "admin_database_url",
    "bare_remote",
    "conn",
    "human_commit",
    "human_delete",
    "human_rename",
    "test_database_url",
    "valkey_url",
    "vault_config",
]


@pytest.fixture(scope="session")
def admin_database_url() -> str:
    """The admin connection URL from `MM_TEST_DATABASE_URL`.

    Skips the test locally if the variable is unset; fails it instead when
    `CI` is set.
    """
    url = os.environ.get("MM_TEST_DATABASE_URL")
    if url:
        return url
    reason = "MM_TEST_DATABASE_URL is not set"
    if os.environ.get("CI"):
        pytest.fail(f"{reason} (required in CI)")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def valkey_url() -> str:
    """The connection URL from `MM_TEST_VALKEY_URL` (#104), same shape as
    `admin_database_url` above.

    Skips the test locally if the variable is unset; fails it instead when
    `CI` is set. Unlike Postgres, every test shares the one Valkey instance
    this names - `tests/auth/test_shared_state.py`'s own `shared_state`
    fixture gives each test its own key prefix instead of its own database.
    """
    url = os.environ.get("MM_TEST_VALKEY_URL")
    if url:
        return url
    reason = "MM_TEST_VALKEY_URL is not set"
    if os.environ.get("CI"):
        pytest.fail(f"{reason} (required in CI)")
    pytest.skip(reason)


@pytest_asyncio.fixture
async def test_database_url(admin_database_url: str) -> AsyncIterator[str]:
    """A freshly created, empty database; dropped again after the test."""
    db_name = f"mm_test_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create database "{db_name}"')
    finally:
        await admin_conn.close()

    base, _, _ = admin_database_url.rpartition("/")
    test_url = f"{base}/{db_name}"
    try:
        yield test_url
    finally:
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = $1 and pid <> pg_backend_pid()",
                db_name,
            )
            await admin_conn.execute(f'drop database if exists "{db_name}"')
        finally:
            await admin_conn.close()


@pytest_asyncio.fixture
async def conn(test_database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """An `asyncpg` connection to a fresh, empty test database."""
    connection = await asyncpg.connect(test_database_url)
    try:
        yield connection
    finally:
        await connection.close()

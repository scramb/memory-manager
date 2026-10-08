# SPDX-License-Identifier: AGPL-3.0-only
"""`pool`, for this package's own `PostgresSharedState`-backed `shared_state` cases.

Duplicated from `tests/auth/conftest.py` rather than imported from there -
`pyproject.toml`'s `mypy_path` comment is why a bare `from conftest import
...` across sibling test packages is ambiguous; a fresh fixture in this
package's own `conftest.py` is the established way around it (`tests/mcp/
conftest.py`'s module docstring gives the same reasoning).

`bare_remote`/`human_commit`/`human_delete`/`human_rename`/`human_session`/
`seed_notes`/`vault_config`/`wrap_push_with_side_effect` are re-exported from
`tests/git_fixtures.py` for the same reason `tests/auth/conftest.py` and
`tests/mcp/conftest.py` both do (see either module's own docstring): at
runtime, every `conftest.py` under `tests/` (there is no `__init__.py`) is
importable under the bare name `conftest`, and whichever one Python's import
system resolves first for a given run is the one a plain `from conftest
import human_commit` (`tests/test_queue.py`, `tests/test_queue_conflict.py`,
`tests/vault/test_sync.py`, `tests/vault/test_repo.py`, `tests/importers/
test_markdown.py`, `tests/importers/test_exports.py`) actually gets - so
every `conftest.py` that could win that race must carry the same superset,
the full set `git_fixtures.py` exports, not just the names this package's
own tests happen to use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest_asyncio
from git_fixtures import (
    bare_remote,
    human_commit,
    human_delete,
    human_rename,
    human_session,
    seed_notes,
    vault_config,
    wrap_push_with_side_effect,
)

from memory_manager.db.migrate import migrate

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "human_session",
    "pool",
    "seed_notes",
    "vault_config",
    "wrap_push_with_side_effect",
]


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()
    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()

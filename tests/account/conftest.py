# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for `tests/account/test_sessions.py`.

`pool` is the same migrated-pool fixture `tests/auth/conftest.py` defines, repeated
here rather than imported - pytest's bare `from conftest import ...` imports resolve
to *some* `conftest.py` module named `conftest`, not necessarily this directory's own
one, so every package under `tests/` that needs it defines its own. The
`bare_remote`/`human_commit`/`human_delete`/`human_rename`/`vault_config` re-exports
are for the same reason (see `tests/auth/conftest.py`'s own docstring): this package
does not use them today, but re-exporting keeps any such import elsewhere in the
suite working regardless of which `conftest.py` collection order picks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest_asyncio
from git_fixtures import bare_remote, human_commit, human_delete, human_rename, vault_config

from memory_manager.db.migrate import migrate

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "pool",
    "vault_config",
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

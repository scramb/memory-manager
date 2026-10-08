# SPDX-License-Identifier: AGPL-3.0-only
"""`GitBackend` against the backend-agnostic contract suite (#94, ADR-0007 §1)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from storage.contract import (
    AuditEntry,
    ChangesSinceContract,
    ListContract,
    PromoteContract,
    ReadWriteEditContract,
    SupersedeArchiveContract,
    audit_recorder,
)

from memory_manager.config import VaultConfig
from memory_manager.queue import WriteQueue
from memory_manager.storage.base import StorageBackend
from memory_manager.storage.git import GitBackend
from memory_manager.vault.repo import Repo


@pytest.fixture
def audit_log() -> list[AuditEntry]:
    """Every `AuditHook` call the `backend` fixture below's queue fires, in order."""
    return []


@pytest.fixture
async def backend(
    vault_config: VaultConfig, audit_log: list[AuditEntry]
) -> AsyncIterator[StorageBackend]:
    """A `GitBackend` over a fresh clone of a throwaway bare remote."""
    repo = Repo(vault_config)
    queue = WriteQueue(repo)
    queue.add_audit_hook(audit_recorder(audit_log))
    await queue.start()
    try:
        yield GitBackend(queue, repo, vault_config.dir)
    finally:
        await queue.stop()


class TestReadWriteEdit(ReadWriteEditContract):
    pass


class TestSupersedeArchive(SupersedeArchiveContract):
    pass


class TestPromote(PromoteContract):
    pass


class TestList(ListContract):
    pass


class TestChangesSince(ChangesSinceContract):
    pass

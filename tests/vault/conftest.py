# SPDX-License-Identifier: AGPL-3.0-only
"""Vault-specific test fixtures.

The git fixtures (`bare_remote`, `vault_config`, `human_commit`,
`human_delete`, `human_rename`) moved to `tests/git_fixtures.py` so both
`tests/vault` and `tests/test_queue.py` can use them; re-exported here so
existing `from conftest import ...` imports in this directory's tests keep
working unchanged.
"""

from __future__ import annotations

from git_fixtures import bare_remote, human_commit, human_delete, human_rename, vault_config

__all__ = ["bare_remote", "human_commit", "human_delete", "human_rename", "vault_config"]

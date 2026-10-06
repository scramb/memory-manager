# SPDX-License-Identifier: AGPL-3.0-only
"""Shared fixtures for vault git/repo tests.

`bare_remote` gives each test its own local bare repository to clone and
push against, so tests never touch the network. `human_commit` simulates a
change made outside the vault server (e.g. a human editing the vault
directly), useful for push-rejection and future pull/sync tests (#12, #15,
#16).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memory_manager.config import VaultConfig
from memory_manager.vault.git import Git

__all__ = ["bare_remote", "human_commit", "vault_config"]


def _run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    # Reuses the production wrapper (fixed env, isolated git config) instead
    # of duplicating its isolation setup here.
    Git(cwd=cwd, env_extra=env or {}).run(*args)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """A fresh, empty bare git repository to clone/push against."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(["init", "--bare", "-b", "main"], cwd=remote)
    return remote


@pytest.fixture
def vault_config(tmp_path: Path, bare_remote: Path) -> VaultConfig:
    """A `VaultConfig` pointing at `bare_remote`, cloning into `tmp_path/vault`."""
    return VaultConfig(remote=str(bare_remote), dir=tmp_path / "vault", branch="main")


def human_commit(remote: Path, rel: str, content: bytes) -> None:
    """Commit `content` at `rel` as `human` and push it directly to `remote`.

    Bypasses `Repo` entirely (a throwaway clone in a temp dir), simulating a
    change made outside the vault server - e.g. to advance the remote ahead
    of a `Repo`'s local clone for push-rejection tests.
    """
    author_env = {
        "GIT_AUTHOR_NAME": "human",
        "GIT_AUTHOR_EMAIL": "human@memory-manager.invalid",
        "GIT_COMMITTER_NAME": "human",
        "GIT_COMMITTER_EMAIL": "human@memory-manager.invalid",
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "clone"
        _run(["clone", "--origin", "origin", str(remote), str(clone_dir)], cwd=tmp_path)

        target = clone_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        _run(["add", "--", rel], cwd=clone_dir)
        _run(["commit", "-m", f"human: write {rel}"], cwd=clone_dir, env=author_env)
        _run(["push", "origin", "HEAD:main"], cwd=clone_dir)

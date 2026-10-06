# SPDX-License-Identifier: AGPL-3.0-only
"""Shared git fixtures for all tests under `tests/` (vault, queue, ...).

`bare_remote` gives each test its own local bare git repository to clone and
push against, so tests never touch the network. `human_commit`/`human_delete`/
`human_rename` simulate a change made outside the vault server (e.g. a human
editing the vault directly), useful for push-rejection, sync and write-queue
conflict tests (#12, #15, #16).

`wrap_push_with_side_effect` goes one step further: it patches a `Repo`
instance's `push()` so an arbitrary side effect (typically `human_commit` or
`human_delete`) runs on the remote *inside* the write queue's own push call,
right before the push - the only way to reliably race a human change
against a specific `sync()`/`push()` pair from outside.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from memory_manager.config import VaultConfig
from memory_manager.vault.git import Git
from memory_manager.vault.repo import Repo

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "vault_config",
    "wrap_push_with_side_effect",
]

_HUMAN_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "human",
    "GIT_AUTHOR_EMAIL": "human@memory-manager.invalid",
    "GIT_COMMITTER_NAME": "human",
    "GIT_COMMITTER_EMAIL": "human@memory-manager.invalid",
}


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
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "clone"
        _run(["clone", "--origin", "origin", str(remote), str(clone_dir)], cwd=tmp_path)

        target = clone_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        _run(["add", "--", rel], cwd=clone_dir)
        _run(["commit", "-m", f"human: write {rel}"], cwd=clone_dir, env=_HUMAN_AUTHOR_ENV)
        _run(["push", "origin", "HEAD:main"], cwd=clone_dir)


def human_delete(remote: Path, rel: str) -> None:
    """Delete `rel` and push the removal directly to `remote`, as `human`.

    Used to test sync's handling of a deletion, the same way `human_commit`
    is used for additions/modifications.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "clone"
        _run(["clone", "--origin", "origin", str(remote), str(clone_dir)], cwd=tmp_path)

        _run(["rm", "--", rel], cwd=clone_dir)
        _run(["commit", "-m", f"human: delete {rel}"], cwd=clone_dir, env=_HUMAN_AUTHOR_ENV)
        _run(["push", "origin", "HEAD:main"], cwd=clone_dir)


def wrap_push_with_side_effect(
    repo: Repo,
    before_push: Callable[[int], None],
    *,
    times: int | None = 1,
) -> None:
    """Patch `repo.push` so `before_push(n)` runs right before the nth push call.

    Lets a test run an arbitrary remote mutation (`human_commit`,
    `human_delete`, ...) exactly inside the write queue's own push call -
    the only place a human change can race a specific `sync()`/`push()`
    pair. `times` caps how many push calls get the side effect first
    (`None` means every call, used to simulate "the remote keeps moving
    ahead of every rebase").
    """
    original_push = repo.push
    calls = 0

    def wrapped_push() -> None:
        nonlocal calls
        if times is None or calls < times:
            before_push(calls)
        calls += 1
        original_push()

    repo.push = wrapped_push  # type: ignore[method-assign]


def human_rename(remote: Path, src_rel: str, dst_rel: str) -> None:
    """Rename `src_rel` to `dst_rel` and push it directly to `remote`, as `human`.

    Used to test sync's rename handling (reported as delete(old) + add(new)).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "clone"
        _run(["clone", "--origin", "origin", str(remote), str(clone_dir)], cwd=tmp_path)

        dst_path = clone_dir / dst_rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        _run(["mv", "--", src_rel, dst_rel], cwd=clone_dir)
        _run(
            ["commit", "-m", f"human: rename {src_rel} to {dst_rel}"],
            cwd=clone_dir,
            env=_HUMAN_AUTHOR_ENV,
        )
        _run(["push", "origin", "HEAD:main"], cwd=clone_dir)

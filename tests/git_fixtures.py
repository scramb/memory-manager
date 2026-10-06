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

`seed_notes`/`human_session` are for the concurrency stress test (#16):
`seed_notes` pre-populates a remote with a batch of notes in one commit,
`human_session` simulates a human making several commits directly against
the remote over time, interleaved with whatever else is writing to it.
"""

from __future__ import annotations

import random
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from memory_manager.config import VaultConfig
from memory_manager.vault.git import Git
from memory_manager.vault.note import NoteFormatError, parse, serialize
from memory_manager.vault.repo import Repo

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "human_session",
    "seed_notes",
    "vault_config",
    "wrap_push_with_side_effect",
]

_MAX_HUMAN_PUSH_RETRIES = 6

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


def seed_notes(remote: Path, notes: Mapping[str, bytes]) -> None:
    """Commit every `path -> content` in `notes` to `remote` in a single push.

    Used to pre-populate a fresh remote with a batch of notes before a
    `WriteQueue` and its clients start working against it (#16).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "seed-clone"
        _run(["clone", "--origin", "origin", str(remote), str(clone_dir)], cwd=tmp_path)

        for rel, content in notes.items():
            target = clone_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            _run(["add", "--", rel], cwd=clone_dir)

        _run(["commit", "-m", "seed: initial notes"], cwd=clone_dir, env=_HUMAN_AUTHOR_ENV)
        _run(["push", "origin", "HEAD:main"], cwd=clone_dir)


def human_session(remote: Path, rng: random.Random, commits: int) -> list[str]:
    """Simulate a human making `commits` separate edits directly against `remote`.

    Clones once into its own throwaway directory, then repeatedly: picks a
    random existing note file, edits its body, commits as `human`, and
    pushes - retrying through `pull --rebase` when the push is rejected
    (#16). A small random sleep before each attempt interleaves the human's
    commits with whatever else is writing to `remote` at the same time.

    Returns the sha of each commit that actually made it onto `remote`, in
    the order it landed there. Runs synchronously (plain subprocess calls);
    call it through `asyncio.to_thread` from async test code.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "human-clone"
        _run(["clone", "--origin", "origin", str(remote), str(clone_dir)], cwd=tmp_path)

        shas: list[str] = []
        attempts = 0
        max_attempts = commits * 10
        while len(shas) < commits and attempts < max_attempts:
            attempts += 1
            time.sleep(rng.uniform(0, 0.01))
            sha = _human_commit_once(clone_dir, rng, attempts)
            if sha is not None:
                shas.append(sha)

        if len(shas) < commits:
            raise AssertionError(
                f"human session only landed {len(shas)}/{commits} commits after {attempts} attempts"
            )
        return shas


def _human_commit_once(clone_dir: Path, rng: random.Random, index: int) -> str | None:
    """Make and push one human commit against the clone at `clone_dir`.

    Returns `None` (no commit landed, the caller should just try again)
    when there is nothing editable left after a fresh pull, the picked
    note cannot be parsed, or a rejected push's rebase conflicts - in every
    case the clone is left clean and caught up with the remote.
    """
    git = Git(cwd=clone_dir)
    git.run("fetch", "origin", "main")
    git.run("reset", "--hard", "origin/main")

    candidates = _list_note_files(clone_dir)
    if not candidates:
        return None
    rel = rng.choice(candidates)
    if not _edit_note(clone_dir / rel, index):
        return None

    _run(["add", "--", rel], cwd=clone_dir)
    _run(
        ["commit", "-m", f"human: edit {rel} #{index}"],
        cwd=clone_dir,
        env=_HUMAN_AUTHOR_ENV,
    )

    for _ in range(_MAX_HUMAN_PUSH_RETRIES):
        push = git.run("push", "origin", "HEAD:main", check=False)
        if push.returncode == 0:
            return git.run("rev-parse", "HEAD").stdout.decode("utf-8").strip()
        pull = git.run("pull", "--rebase", "origin", "main", check=False)
        if pull.returncode != 0:
            git.run("rebase", "--abort", check=False)
            git.run("fetch", "origin", "main")
            git.run("reset", "--hard", "origin/main")
            return None
    return None


def _list_note_files(clone_dir: Path) -> list[str]:
    """Every live note file in `clone_dir`, as vault-relative paths.

    Excludes `_archive/` (archived notes) and `*.conflict.md` (server-owned
    conflict files, never a human's to edit casually in this harness).
    """
    files: list[str] = []
    for path in clone_dir.rglob("*.md"):
        rel = path.relative_to(clone_dir).as_posix()
        if rel.startswith("_archive/") or rel.endswith(".conflict.md"):
            continue
        files.append(rel)
    return files


def _edit_note(path: Path, index: int) -> bool:
    """Rewrite `path`'s body in place, keeping the rest of the note intact.

    Returns `False` (nothing written) if the file is not a parseable note.
    """
    try:
        note = parse(path.read_bytes())
    except NoteFormatError:
        return False
    edited = replace(note, body=f"Edited by human, attempt {index}.\n")
    path.write_bytes(serialize(edited))
    return True


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

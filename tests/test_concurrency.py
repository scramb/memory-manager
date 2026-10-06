# SPDX-License-Identifier: AGPL-3.0-only
"""Concurrency stress test: parallel client writes plus human pushes (#16).

Acceptance test for M1: two clients (`claude-ai`, `claude-code`) hammer a
`WriteQueue` with writes, edits and archives while a human commits and
pushes directly against the same remote - the same shape of contention the
write queue (`queue.py`, #14/#15) and `Repo.sync`/`push` (`vault/repo.py`,
#12) were built to survive. Every outcome is recorded; afterwards a battery
of invariants checks that no write was silently lost, overwritten or
double-reported (`CLAUDE.md` "Never overwrite silently").

Parametrized over 20 fixed seeds so a failure is reproducible; the op
sequence, picks and sleep durations are all driven by `random.Random(seed)` -
only id generation uses the process RNG, since its exact value never affects
which outcome is correct, only its uniqueness.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_fixtures import human_session, seed_notes

from memory_manager.config import VaultConfig
from memory_manager.queue import (
    EditMismatch,
    InvalidNote,
    NotFound,
    Op,
    VersionConflict,
    WriteConflict,
    WriteFailed,
    WriteQueue,
    WriteRequest,
)
from memory_manager.vault.git import Git
from memory_manager.vault.note import Note, parse, serialize, version
from memory_manager.vault.repo import Repo
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_CLIENTS = ("claude-ai", "claude-code")
_K_SEED_NOTES = 6
_N_CLIENT_OPS = 15
_M_HUMAN_COMMITS = 8
_REMOTE_KEEPS_MOVING = "remote keeps moving"
_OPS: tuple[Op, ...] = ("write", "edit", "archive")


def _note_bytes(**overrides: object) -> bytes:
    defaults: dict[str, object] = {
        "id": new_ulid(),
        "title": "A note",
        "description": "A description.",
        "type": "fact",
        "created": _CREATED,
        "updated": _CREATED,
        "body": "Body.\n",
    }
    defaults.update(overrides)
    return serialize(Note(**defaults))  # type: ignore[arg-type]


def _seed_note_contents() -> dict[str, bytes]:
    return {
        f"personal/fact/seed-{i}.md": _note_bytes(
            id=new_ulid(), title=f"Seed note {i}", body=f"Seed body {i}.\n"
        )
        for i in range(_K_SEED_NOTES)
    }


@dataclass
class _Outcome:
    """What happened to one submitted `WriteRequest`."""

    client: str
    op: str
    path: str
    kind: str
    reason: str | None = None
    commit: str | None = None
    version: str | None = None
    content: bytes | None = None
    conflict_content: bytes | None = None


async def _run_client(
    client: str,
    queue: WriteQueue,
    repo: Repo,
    rng: random.Random,
    known_paths: list[str],
    outcomes: list[_Outcome],
) -> None:
    write_counter = 0
    for i in range(_N_CLIENT_OPS):
        await asyncio.sleep(rng.uniform(0, 0.01))
        op: Op = rng.choice(_OPS) if known_paths else "write"

        expected_content: bytes | None = None
        path: str

        if op == "write":
            write_counter += 1
            path = f"agent/fact/{client}-{write_counter}.md"
            content = _note_bytes(id=new_ulid(), body=f"Created by {client}, op {i}.\n")
            request = WriteRequest(
                op="write", path=path, client=client, if_version="new", content=content
            )
            expected_content = content
        else:
            path = rng.choice(known_paths)
            current = await asyncio.to_thread(repo.read_file, path)
            if current is None:
                request = WriteRequest(
                    op=op,
                    path=path,
                    client=client,
                    if_version="0" * 64,
                    old_str="x" if op == "edit" else None,
                    new_str="y" if op == "edit" else None,
                )
            elif op == "edit":
                note = parse(current)
                new_body = f"Edited by {client}, op {i}.\n"
                request = WriteRequest(
                    op="edit",
                    path=path,
                    client=client,
                    if_version=version(current),
                    old_str=note.body,
                    new_str=new_body,
                )
                expected_content = serialize(replace(note, body=new_body))
            else:  # archive
                request = WriteRequest(
                    op="archive", path=path, client=client, if_version=version(current)
                )

        try:
            result = await queue.submit(request)
        except (VersionConflict, EditMismatch, NotFound, InvalidNote) as exc:
            outcomes.append(
                _Outcome(client, request.op, request.path, type(exc).__name__, reason=str(exc))
            )
        except WriteConflict as exc:
            outcomes.append(
                _Outcome(
                    client,
                    request.op,
                    request.path,
                    "WriteConflict",
                    reason=str(exc),
                    conflict_content=expected_content,
                )
            )
        except WriteFailed as exc:
            if _REMOTE_KEEPS_MOVING not in str(exc):
                raise
            outcomes.append(
                _Outcome(client, request.op, request.path, "WriteFailed", reason=str(exc))
            )
        else:
            outcomes.append(
                _Outcome(
                    client,
                    request.op,
                    result.path,
                    "success",
                    commit=result.commit,
                    version=result.version,
                    content=expected_content,
                )
            )
            if request.op == "write":
                known_paths.append(path)


def _is_ancestor(git: Git, candidate: str, tip: str) -> bool:
    result = git.run("merge-base", "--is-ancestor", candidate, tip, check=False)
    return result.returncode == 0


def _show(git: Git, commit: str, path: str) -> bytes | None:
    result = git.run("show", f"{commit}:{path}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def _assert_invariants(
    vault_cfg: VaultConfig, remote: Path, outcomes: list[_Outcome], human_shas: list[str]
) -> None:
    git_remote = Git(cwd=remote)
    remote_head = git_remote.run("rev-parse", "main").stdout.decode("utf-8").strip()

    success_contents: set[bytes] = set()
    for outcome in outcomes:
        if outcome.kind != "success":
            continue
        assert outcome.commit is not None
        assert _is_ancestor(git_remote, outcome.commit, remote_head), (
            f"{outcome.client} {outcome.op} {outcome.path}: commit {outcome.commit} "
            "is not reachable from origin/main"
        )
        committed = _show(git_remote, outcome.commit, outcome.path)
        assert committed is not None, (
            f"{outcome.client} {outcome.op} {outcome.path}: no content for "
            f"{outcome.path} at commit {outcome.commit}"
        )
        # (a)+self-consistency: the reported version really is this content's hash.
        assert version(committed) == outcome.version, (
            f"{outcome.client} {outcome.op} {outcome.path}: reported version does not "
            "match the content actually committed"
        )
        # (b): for write/edit, the committed content is exactly what was submitted.
        if outcome.content is not None:
            assert committed == outcome.content, (
                f"{outcome.client} {outcome.op} {outcome.path}: committed content differs "
                "from what the client submitted"
            )
        success_contents.add(committed)

    for outcome in outcomes:
        # (d) every rejection carries a reason.
        if outcome.kind != "success":
            assert outcome.reason, f"{outcome.client} {outcome.op} {outcome.path} has no reason"
        # (f) content rejected via a conflict file was never also reported as a success.
        if outcome.kind == "WriteConflict" and outcome.conflict_content is not None:
            assert outcome.conflict_content not in success_contents, (
                f"{outcome.client} {outcome.op} {outcome.path}: content rejected as a "
                "conflict was also committed as a success"
            )

    # (c) every human commit is still part of history - nothing was lost or force-pushed over.
    for sha in human_shas:
        assert _is_ancestor(git_remote, sha, remote_head), (
            f"human commit {sha} is not reachable from origin/main - history was rewritten"
        )

    # (e) the queue's clone is clean and has no commit the remote does not have.
    git_local = Git(cwd=vault_cfg.dir)
    git_local.run("fetch", "origin", "main")
    status = git_local.run("status", "--porcelain").stdout
    assert status == b"", f"queue clone is not clean: {status!r}"
    local_head = git_local.run("rev-parse", "HEAD").stdout.decode("utf-8").strip()
    ancestor = git_local.run("merge-base", "--is-ancestor", local_head, "origin/main", check=False)
    assert ancestor.returncode == 0, "queue clone has a commit the remote does not have"


@pytest.mark.parametrize("seed", range(20))
async def test_concurrent_client_writes_and_human_pushes_resolve_without_silent_loss(
    seed: int, vault_config: VaultConfig, bare_remote: Path
) -> None:
    seed_notes(bare_remote, _seed_note_contents())

    repo = Repo(vault_config)
    queue = WriteQueue(repo)
    await queue.start()

    known_paths: list[str] = [f"personal/fact/seed-{i}.md" for i in range(_K_SEED_NOTES)]
    outcomes: list[_Outcome] = []

    root_rng = random.Random(seed)  # noqa: S311 - deterministic test fixture, not crypto
    client_rngs = {client: random.Random(root_rng.random()) for client in _CLIENTS}  # noqa: S311
    human_rng = random.Random(root_rng.random())  # noqa: S311

    try:
        client_tasks = [
            asyncio.create_task(
                _run_client(client, queue, repo, client_rngs[client], known_paths, outcomes)
            )
            for client in _CLIENTS
        ]
        human_task = asyncio.create_task(
            asyncio.to_thread(human_session, bare_remote, human_rng, _M_HUMAN_COMMITS)
        )

        await asyncio.gather(*client_tasks)
        human_shas = await human_task
    finally:
        await queue.stop()

    await asyncio.to_thread(repo.sync)

    _assert_invariants(vault_config, bare_remote, outcomes, human_shas)

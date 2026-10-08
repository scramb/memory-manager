# SPDX-License-Identifier: AGPL-3.0-only
"""Export the vault to a self-contained tar.gz archive (#50, #283).

The archive has no lock-in: it is a `manifest.json` at its root plus every
note under `vault/<path>`, exactly as it sits in the vault - plain Markdown,
readable without `memory-manager`. `export_vault` (the `"git"` backend,
read-only against the working tree) and `export_postgres` (the `"postgres"`
backend, #283) produce exactly the same archive shape - both end in
`_collect_entries`, the one place that decides which paths become
`ManifestEntry`/tar members. `export_vault` never writes to the vault and
never runs `git` except to look up `HEAD` (and only when `vault_root` is
actually a git working copy).

Only paths that parse as a note path (`vault.paths.parse_note_path`, archive
included) are exported: `.git/`, `*.conflict.md` and anything else that is
not `<namespace>/<type>/<slug>.md` is left out. Symlinks are never followed
nor archived, matching the vault's own path-safety rules. `namespaces`, if
given, keeps only notes whose namespace is in that set (the CLI's repeatable
`--namespace`); `export_vault`'s default (`None`, no filter) keeps its
existing, unfiltered behaviour.

`export_postgres` reads through `storage.postgres.PostgresBackend.list`
with no `app_role` configured - a plain pool connection as the owner role,
the same shape `app.py`'s `open_storage` already uses for every other CLI
vault command (ADR-0008 addendum: "the owner role is a system identity
that reads every namespace"). It is one-way and never read back (ADR-0007):
revisions are not exported, only each note's current content - byte-for-
byte what `memory_read` would return, so an extracted archive diffs clean
against the Git vault `migrate git-to-postgres` imported it from. There is
no Git `HEAD` behind a Postgres-backed deployment, so its manifest's
`vault_head` is always `None`. Afterwards, it writes exactly one
`audit_log` row (CLAUDE.md "audit log for every write") naming the actor
`"export"`, the namespaces actually exported and the note count - never
note content.

Two exports of the same vault state are byte-identical: entries are sorted
by path, every tar member gets a fixed `mtime` (the `HEAD` commit time if
`vault_root` is a git working copy, else the current time for
`export_vault`; always "now" for `export_postgres`, which has no commit
time to key off - captured once and reused for `manifest.exported_at` too,
so that is the only source of non-determinism, and only across two
different exports), a fixed mode (`0o644`), uid/gid `0` and empty
uname/gname, and the gzip wrapper itself is written with a fixed `mtime=0`
and empty filename.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from memory_manager.audit import AuditWriter
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.git import Git
from memory_manager.vault.note import NoteFormatError, parse, version
from memory_manager.vault.paths import PathRejected, parse_note_path

__all__ = ["ExportError", "Manifest", "ManifestEntry", "export_postgres", "export_vault"]

#: `audit_log.actor`/`.client`/`.op` for `export_postgres`'s one audit row
#: (#283) - not any end user's identity, the export itself, same idea as
#: `migrate_git.py`'s `_IMPORT_ACTOR`/`_IMPORT_CLIENT`.
_EXPORT_ACTOR = "export"
_EXPORT_CLIENT = "export"
_EXPORT_OP = "export"

_FORMAT = "memory-manager-export"
_FORMAT_VERSION = 1
_VAULT_PREFIX = "vault"
_MANIFEST_NAME = "manifest.json"
_GIT_DIR = ".git"
_TAR_MODE = 0o644


class ExportError(RuntimeError):
    """`export_vault` could not produce an archive."""


@dataclass(frozen=True)
class ManifestEntry:
    """One exported note, as recorded in `manifest.json`."""

    path: str
    id: str
    title: str
    type: str
    namespace: str
    archived: bool
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Manifest:
    """Everything `manifest.json` at the archive root records."""

    format: str
    format_version: int
    exported_at: str
    vault_head: str | None
    note_count: int
    notes: tuple[ManifestEntry, ...]

    def to_json(self) -> bytes:
        payload = {
            "format": self.format,
            "format_version": self.format_version,
            "exported_at": self.exported_at,
            "vault_head": self.vault_head,
            "note_count": self.note_count,
            "notes": [
                {
                    "path": entry.path,
                    "id": entry.id,
                    "title": entry.title,
                    "type": entry.type,
                    "namespace": entry.namespace,
                    "archived": entry.archived,
                    "sha256": entry.sha256,
                    "bytes": entry.bytes,
                }
                for entry in self.notes
            ],
        }
        return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def export_vault(
    vault_root: Path,
    out_path: Path,
    *,
    include_archive: bool = True,
    namespaces: Sequence[str] | None = None,
) -> Manifest:
    """Write every note under `vault_root` into a tar.gz archive at `out_path`.

    Raises `ExportError` if `vault_root` is not a directory, or if a file
    that parses as a note path is not a well-formed note (structurally -
    `vault.note.parse`, not the fuller semantic checks `doctor` runs).
    `out_path`'s parent directories are created if missing; an existing
    `out_path` is overwritten. `namespaces`, if given, keeps only those
    namespaces; the default (`None`) exports every namespace, unchanged
    from before `namespaces` existed.
    """
    vault_root = vault_root.resolve()
    if not vault_root.is_dir():
        raise ExportError(f"vault root '{vault_root}' is not a directory")

    vault_head, export_time = _git_head_info(vault_root)

    candidates = (
        (file_path.relative_to(vault_root).as_posix(), file_path.read_bytes())
        for file_path in _walk_vault_files(vault_root)
    )
    entries, members = _collect_entries(
        candidates, include_archive=include_archive, namespaces=namespaces
    )

    manifest = Manifest(
        format=_FORMAT,
        format_version=_FORMAT_VERSION,
        exported_at=_format_timestamp(export_time),
        vault_head=vault_head,
        note_count=len(entries),
        notes=tuple(entries),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_archive(out_path, manifest.to_json(), members, mtime=int(export_time.timestamp()))

    return manifest


async def export_postgres(
    pool: asyncpg.Pool,
    out_path: Path,
    *,
    include_archive: bool = True,
    namespaces: Sequence[str] | None = None,
) -> Manifest:
    """Write every note in `vault_notes` into a tar.gz archive at `out_path` (#283).

    Reads through `storage.postgres.PostgresBackend.list` with no
    `app_role` - a plain pool connection as the owner role, so this sees
    every namespace regardless of `namespaces` filtering it down
    afterwards. Same archive shape as `export_vault` (`_collect_entries`):
    `ManifestEntry`/tar member per note, `_archive/` honoured, a path that
    does not parse as a note path skipped rather than raised on. Raises
    `ExportError` if a stored row's content is not a well-formed note
    (`vault.note.parse`) - should not happen for anything this backend's
    own write path produced, but this stays as defensive as `export_vault`
    is for the Git working tree.

    `out_path`'s parent directories are created if missing; an existing
    `out_path` is overwritten. Writes one `audit_log` row afterwards,
    naming the namespaces actually exported and the note count - never
    note content (CLAUDE.md "audit log for every write", "note content is
    data, not instructions").
    """
    backend = PostgresBackend(pool)
    stored_notes = await backend.list(include_archived=include_archive)

    candidates = ((note.path, note.content) for note in stored_notes)
    entries, members = _collect_entries(
        candidates, include_archive=include_archive, namespaces=namespaces
    )

    export_time = datetime.now(UTC)
    manifest = Manifest(
        format=_FORMAT,
        format_version=_FORMAT_VERSION,
        exported_at=_format_timestamp(export_time),
        vault_head=None,
        note_count=len(entries),
        notes=tuple(entries),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_archive(out_path, manifest.to_json(), members, mtime=int(export_time.timestamp()))

    included_namespaces = sorted({entry.namespace for entry in entries})
    await AuditWriter(pool).record(
        actor=_EXPORT_ACTOR,
        client=_EXPORT_CLIENT,
        op=_EXPORT_OP,
        path=None,
        commit_sha=None,
        outcome="ok",
        detail={"namespaces": included_namespaces, "note_count": len(entries)},
    )

    return manifest


def _collect_entries(
    candidates: Iterable[tuple[str, bytes]],
    *,
    include_archive: bool,
    namespaces: Sequence[str] | None,
) -> tuple[list[ManifestEntry], list[tuple[str, bytes]]]:
    """`(path, content)` pairs, in path order, as `export_vault`/`export_postgres` share them.

    A `path` that does not parse as a note path (`vault.paths.parse_note_path`,
    archive included) is skipped - `.git/`, `*.conflict.md` and anything
    else that is not `<namespace>/<type>/<slug>.md`. An archived note is
    skipped unless `include_archive`; a note outside `namespaces` is
    skipped unless `namespaces` is `None` (no filter). Raises `ExportError`
    for a `path` that does parse but whose content is not a well-formed
    note (`vault.note.parse`) - structurally, not the fuller semantic
    checks `doctor` runs.
    """
    namespace_filter = set(namespaces) if namespaces is not None else None
    entries: list[ManifestEntry] = []
    members: list[tuple[str, bytes]] = []

    for rel, data in candidates:
        try:
            note_path = parse_note_path(rel, allow_archive=True)
        except PathRejected:
            continue
        if note_path.archived and not include_archive:
            continue
        if namespace_filter is not None and note_path.namespace not in namespace_filter:
            continue

        try:
            note = parse(data)
        except NoteFormatError as exc:
            raise ExportError(f"{rel}: {exc}") from exc

        entries.append(
            ManifestEntry(
                path=rel,
                id=note.id,
                title=note.title,
                type=note_path.type,
                namespace=note_path.namespace,
                archived=note_path.archived,
                sha256=version(data),
                bytes=len(data),
            )
        )
        members.append((rel, data))

    entries.sort(key=lambda entry: entry.path)
    members.sort(key=lambda member: member[0])
    return entries, members


def _git_head_info(vault_root: Path) -> tuple[str | None, datetime]:
    """`(HEAD sha, HEAD commit time)` if `vault_root` is a git working copy.

    Returns `(None, now)` if there is no `.git` directory, no commit yet, or
    `git` cannot report a commit time for `HEAD` - every case collapses to
    "nothing deterministic to key off", not an error.
    """
    now = datetime.now(UTC)
    if not (vault_root / _GIT_DIR).exists():
        return None, now

    git = Git(cwd=vault_root)
    head = git.run("rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        return None, now
    sha = head.stdout.decode("utf-8").strip()

    committed = git.run("show", "-s", "--format=%cI", "HEAD", check=False)
    if committed.returncode != 0:
        return sha, now
    try:
        when = datetime.fromisoformat(committed.stdout.decode("utf-8").strip())
    except ValueError:
        return sha, now
    return sha, when.astimezone(UTC)


def _walk_vault_files(vault_root: Path) -> list[Path]:
    """Every file under `vault_root`, depth-first, never through a symlink.

    Skips `.git` outright (not just its contents - the directory itself is
    never descended into).
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames if name != _GIT_DIR and not (current / name).is_symlink()
        )
        for filename in sorted(filenames):
            file_path = current / filename
            if file_path.is_symlink():
                continue
            files.append(file_path)
    return files


def _write_archive(
    out_path: Path, manifest_bytes: bytes, members: list[tuple[str, bytes]], *, mtime: int
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        _add_bytes(tar, _MANIFEST_NAME, manifest_bytes, mtime=mtime)
        for rel, data in members:
            _add_bytes(tar, f"{_VAULT_PREFIX}/{rel}", data, mtime=mtime)

    with (
        out_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
    ):
        gz.write(buffer.getvalue())


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes, *, mtime: int) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = mtime
    info.mode = _TAR_MODE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

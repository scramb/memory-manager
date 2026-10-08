# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager migrate git-to-postgres` (#246 dry run, #247 the real import, ADR-0007 §6).

`dry_run` only ever reads: the vault's working tree and its `git log`
history. It never opens a database connection and never writes anything.
`import_vault` (#247) is the write path: it re-runs `dry_run` itself first
and refuses to write anything if that reports a problem, then imports each
mapped namespace - current notes and their Git history as revisions - into
Postgres in one transaction per namespace, connected as the owner role
(ADR-0008 addendum #100, "system identity"): both `vault_notes_owner_access`
and `vault_revisions_owner_access` (`migrations/0005_rls.sql`) grant that
role unconditional read/write, so this module needs no `app_role`/principal
of its own and can set an explicit `author_oid` per revision, something no
MCP tool's write path is ever allowed to do.

`parse_map_entries` turns the repeatable `--map <git-ns>=<kind>:<key>[:<alias>]`
flags (owner decision 2026-10-08, no map-file format) into `MapEntry` objects,
with precise errors for anything malformed. `kind` is one of ADR-0008 A2's
registry kinds (`agent` is additive later, ADR-0013, and not accepted here).
`user:<oid>` takes no alias - a personal namespace is always shown as `me`,
never under a chosen alias (`mcp/namespaces.py`'s own `ME_ALIAS`). `org` maps
only to the one fixed `org:org` target, also without an alias. `group`/
`project` aliases default to the Git namespace name and otherwise follow the
same charset `vault/paths.py` enforces for a namespace segment, plus the
reserved words and prefixes ADR-0008 and ADR-0005 carve out: a leading `_`
(ADR-0005 "Names starting with `_` are reserved"), the `me`/`org`
pseudo-aliases, and the `u-` prefix `mm_ensure_personal_ns()` uses for every
personal namespace (`mcp/namespaces.py`'s `_INTERNAL_ALIAS_PREFIX`).

`dry_run` discovers every top-level namespace of the vault - including one
that exists only under `_archive/` - and requires every one of them to have a
`--map` entry (and every `--map` entry to name a namespace that actually
exists). For each discovered namespace it walks every current note with the
same checks a write would make (`vault.paths.parse_note_path`,
`vault.validate.validate_bytes`, `vault.secrets.check`) to count live and
archived notes and to collect anything that would block the import, and it
walks the whole Git history (`vault.git.Git`, `git log --reverse -M
--name-status`) to count revisions per note, keyed by the note's frontmatter
`id` rather than its path - the one key that survives a rename or an
archive move across commits. `import_vault` walks that same history a second
time, this time keeping every revision's content, commit author, author time
and subject (`_revisions_by_id`, shared with `dry_run`), and writes them
oldest first, followed by one current `vault_notes` row per note whose
`version` is `vault.note.version` of the HEAD bytes - byte-identical to what
is on disk, per ADR-0007 §6. A namespace whose stored alias already has rows
in `vault_notes` is refused, reported, and left untouched; every other
namespace's own transaction rolls back whole on any failure, so a crash
during one note's import never leaves that namespace half-imported.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import asyncpg
import asyncpg.pool

from memory_manager.audit import AuditWriter
from memory_manager.vault.git import Git
from memory_manager.vault.note import NoteFormatError
from memory_manager.vault.note import parse as parse_note_structural
from memory_manager.vault.note import version as note_version
from memory_manager.vault.paths import NotePath, PathRejected, iter_md_files, parse_note_path
from memory_manager.vault.secrets import SecretFound
from memory_manager.vault.secrets import check as check_secrets
from memory_manager.vault.validate import NoteInvalid, validate_bytes

__all__ = [
    "DryRunReport",
    "ImportReport",
    "MapEntry",
    "MapError",
    "MigrationError",
    "NamespaceImportResult",
    "NamespaceReport",
    "discover_namespaces",
    "dry_run",
    "import_vault",
    "parse_map_entries",
]

_NAMESPACE_KINDS = ("user", "group", "project", "org")
_ORG_KEY = "org"
_ORG_ALIAS = "org"

# Same charset as a namespace path segment (`vault/paths.py`'s `_NAMESPACE_RE`) -
# an alias becomes a path segment once a note is written under it.
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_RESERVED_ALIASES = frozenset({"me", "org"})
_INTERNAL_ALIAS_PREFIX = "u-"

_ARCHIVE_SEGMENT = "_archive"
_CONFLICT_SUFFIX = ".conflict.md"
_GIT_DIR = ".git"

#: `vault_revisions.client` for every revision `import_vault` writes (#247) -
#: distinguishes an imported revision from one a real MCP client wrote. Also
#: `audit_log.client` for this module's own audit rows (same identity).
_IMPORT_CLIENT = "migrate-git"

#: `audit_log.actor` for every row `import_vault` writes - CLAUDE.md "audit
#: log for every write" and ADR-0008 addendum's "system identity" (#100):
#: this is not any end user's `oid`, it is the migration itself.
_IMPORT_ACTOR = "migrate"

#: `audit_log.op` for every row `import_vault` writes.
_IMPORT_OP = "migrate_import"


class MapError(ValueError):
    """A `--map` entry is malformed, or two entries contradict each other."""


class MigrationError(RuntimeError):
    """`dry_run` could not read `vault_root` as a Git vault."""


@dataclass(frozen=True)
class MapEntry:
    """One resolved `--map` entry: a Git namespace and its Postgres target.

    `alias` is `None` only for `kind == "user"` - every other kind always
    carries a concrete, validated alias (defaulted to `git_namespace` for
    `group`/`project`, fixed to `"org"` for `org`).
    """

    git_namespace: str
    kind: str
    key: str
    alias: str | None


def _validate_alias(alias: str, *, raw: str) -> None:
    if alias.startswith("_"):
        raise MapError(
            f"--map {raw!r}: alias {alias!r} starts with '_', which is reserved "
            "(ADR-0005) - choose a different alias"
        )
    if alias.startswith(_INTERNAL_ALIAS_PREFIX):
        raise MapError(
            f"--map {raw!r}: alias {alias!r} starts with {_INTERNAL_ALIAS_PREFIX!r}, "
            "which is reserved for personal namespaces - choose a different alias"
        )
    if alias in _RESERVED_ALIASES:
        raise MapError(f"--map {raw!r}: alias {alias!r} is reserved - choose a different alias")
    if not _ALIAS_RE.match(alias):
        raise MapError(
            f"--map {raw!r}: alias {alias!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$ - "
            "use lower-case letters, digits and hyphens only, max 40 chars"
        )


def parse_map_entry(raw: str) -> MapEntry:
    """Parse one `--map <git-ns>=<kind>:<key>[:<alias>]` flag value.

    Raises `MapError` with a message naming exactly what is wrong.
    """
    if "=" not in raw:
        raise MapError(f"--map {raw!r} is missing '=' - expected <git-ns>=<kind>:<key>[:<alias>]")
    git_namespace, _, target = raw.partition("=")
    if not git_namespace:
        raise MapError(f"--map {raw!r} has an empty namespace before '='")
    if not target:
        raise MapError(f"--map {raw!r} has an empty target after '='")

    parts = target.split(":")
    if len(parts) < 2:
        raise MapError(
            f"--map {raw!r}: target {target!r} is missing ':' - expected <kind>:<key>[:<alias>]"
        )
    if len(parts) > 3:
        raise MapError(f"--map {raw!r}: target {target!r} has too many ':' separators")

    kind, key = parts[0], parts[1]
    alias = parts[2] if len(parts) == 3 else None

    if kind not in _NAMESPACE_KINDS:
        allowed = ", ".join(_NAMESPACE_KINDS)
        raise MapError(f"--map {raw!r}: kind {kind!r} is not one of ({allowed})")
    if not key:
        raise MapError(f"--map {raw!r}: key is empty")
    if alias == "":
        raise MapError(f"--map {raw!r}: alias is empty - omit the trailing ':' instead")

    if kind == "user":
        if alias is not None:
            raise MapError(
                f"--map {raw!r}: kind 'user' takes no alias - a personal namespace "
                "is always shown as 'me'"
            )
        return MapEntry(git_namespace=git_namespace, kind=kind, key=key, alias=None)

    if kind == "org":
        if key != _ORG_KEY:
            raise MapError(
                f"--map {raw!r}: kind 'org' takes key {_ORG_KEY!r} - "
                "there is exactly one org namespace"
            )
        if alias is not None:
            raise MapError(f"--map {raw!r}: kind 'org' takes no alias - it is always 'org'")
        return MapEntry(git_namespace=git_namespace, kind=kind, key=key, alias=_ORG_ALIAS)

    # group / project
    resolved_alias = alias if alias is not None else git_namespace
    _validate_alias(resolved_alias, raw=raw)
    return MapEntry(git_namespace=git_namespace, kind=kind, key=key, alias=resolved_alias)


def parse_map_entries(raw_entries: Sequence[str]) -> dict[str, MapEntry]:
    """Parse every `--map` flag, keyed by Git namespace.

    Raises `MapError` for a malformed entry, a namespace mapped twice, two
    namespaces mapped to the same `kind:key` under different aliases, or two
    different targets sharing one alias (the registry's `alias` column is
    globally unique, not just per kind).
    """
    entries: dict[str, MapEntry] = {}
    for raw in raw_entries:
        entry = parse_map_entry(raw)
        if entry.git_namespace in entries:
            raise MapError(f"--map has more than one entry for namespace {entry.git_namespace!r}")
        entries[entry.git_namespace] = entry

    _check_target_consistency(entries)
    _check_alias_collisions(entries)
    return entries


def _check_target_consistency(entries: Mapping[str, MapEntry]) -> None:
    by_target: dict[tuple[str, str], set[str | None]] = {}
    for entry in entries.values():
        by_target.setdefault((entry.kind, entry.key), set()).add(entry.alias)
    for (kind, key), aliases in by_target.items():
        if len(aliases) > 1:
            raise MapError(
                f"--map maps {kind}:{key} to more than one alias "
                f"({sorted(a for a in aliases if a is not None)!r}) - give every namespace "
                "mapped to it the same alias (or none)"
            )


def _check_alias_collisions(entries: Mapping[str, MapEntry]) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for entry in entries.values():
        if entry.alias is None:
            continue
        target = (entry.kind, entry.key)
        existing = seen.get(entry.alias)
        if existing is not None and existing != target:
            raise MapError(
                f"--map alias {entry.alias!r} is used for both {existing[0]}:{existing[1]} "
                f"and {entry.kind}:{entry.key} - aliases must be unique"
            )
        seen[entry.alias] = target


@dataclass(frozen=True)
class NamespaceReport:
    """What `dry_run` found for one top-level Git namespace."""

    git_namespace: str
    target: MapEntry | None
    live_notes: int
    archived_notes: int
    revisions: int
    problems: tuple[str, ...]


@dataclass(frozen=True)
class DryRunReport:
    """Everything `dry_run` found, one `NamespaceReport` per discovered namespace."""

    namespaces: tuple[NamespaceReport, ...]
    unknown_mappings: tuple[str, ...]

    @property
    def unmapped(self) -> tuple[str, ...]:
        """Discovered namespaces with no `--map` entry, sorted."""
        return tuple(ns.git_namespace for ns in self.namespaces if ns.target is None)

    @property
    def ok(self) -> bool:
        """Whether the vault could be imported as mapped, without any open problem."""
        if self.unmapped or self.unknown_mappings:
            return False
        return not any(ns.problems for ns in self.namespaces)


def discover_namespaces(vault_root: Path) -> set[str]:
    """Every top-level namespace of the vault, live or archive-only.

    A namespace is a top-level directory of `vault_root`, or a top-level
    directory under `_archive/` for one that currently only has archived
    notes. Hidden directories (`.git` among them) and symlinks are skipped -
    neither is ever a namespace.
    """
    namespaces: set[str] = set()
    for entry in vault_root.iterdir():
        if entry.is_symlink() or not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name == _ARCHIVE_SEGMENT:
            for sub in entry.iterdir():
                if sub.is_symlink() or not sub.is_dir() or sub.name.startswith("."):
                    continue
                namespaces.add(sub.name)
            continue
        namespaces.add(entry.name)
    return namespaces


def _top_level_namespace(rel: str) -> str:
    segments = rel.split("/")
    if segments[0] == _ARCHIVE_SEGMENT and len(segments) > 1:
        return segments[1]
    return segments[0]


def _effective_path(change_line: str) -> str | None:
    """The path a `--name-status` change line sets content at, `None` for a delete.

    A rename/copy line (`R100\\told\\tnew`, `C100\\told\\tnew`) reports both the
    old and the new path; only the new path still carries content at this
    commit. A plain delete (`D\\tpath`) carries none - there is nothing left
    to read a revision's `id` from, and the note's removal is not itself
    counted as a revision.
    """
    parts = change_line.split("\t")
    status = parts[0]
    if status.startswith("D"):
        return None
    if status.startswith(("R", "C")):
        return parts[2] if len(parts) > 2 else None
    return parts[1] if len(parts) > 1 else None


@dataclass(frozen=True)
class _HistoricalRevision:
    """One commit that changed the note identified by `note_id`, oldest first.

    `path` is the path this revision's content lived at, in the original Git
    namespace (not yet rewritten to the import's target alias - `import_vault`
    does that per namespace, since the same history is shared across every
    namespace `dry_run`/`import_vault` process in one call). `content` is the
    raw bytes read at that commit, never re-validated against today's rules
    (`_read_note_at`'s own docstring).
    """

    sha: str
    author: str
    created_at: datetime
    message: str
    path: str
    content: bytes


def _read_note_at(git: Git, sha: str, path: str) -> tuple[str, bytes] | None:
    """`(id, content)` of `path` as it read at commit `sha`, `None` if unreadable.

    Only a structural parse (`vault.note.parse`) - a historical revision is
    never re-validated against today's semantic rules, only used to key it
    to the note it belongs to.
    """
    result = git.run("show", f"{sha}:{path}", check=False)
    if result.returncode != 0:
        return None
    content = result.stdout
    try:
        note_id = parse_note_structural(content).id
    except NoteFormatError:
        return None
    return note_id, content


def _revisions_by_id(vault_root: Path) -> dict[str, list[_HistoricalRevision]]:
    """Every note id's revisions, oldest first, across the whole history of `vault_root`.

    One `git log --reverse -M --name-status` walk, oldest commit first, with
    the commit's author name, author time and subject carried on the same
    header line (`%x01`-separated, right after the `%x00`-prefixed sha
    `dry_run`'s own commit-block split already relies on). `-M` turns an
    archive move or a plain rename into a single `R`-status line instead of a
    `D`+`A` pair, but the `id`-keyed grouping below is correct either way: a
    `D` carries no content and is never counted, so a rename git does not
    think similar enough to flag only ever contributes the one `A` side.
    """
    git = Git(cwd=vault_root)
    result = git.run(
        "log",
        "--reverse",
        "-M",
        "--name-status",
        "--format=%x00%H%x01%an%x01%aI%x01%s",
        check=False,
    )
    if result.returncode != 0:
        return {}

    revisions: dict[str, list[_HistoricalRevision]] = {}
    text = result.stdout.decode("utf-8", errors="replace")
    for commit_block in text.split("\x00"):
        if not commit_block:
            continue
        header, _, body = commit_block.partition("\n")
        fields = header.split("\x01")
        if len(fields) != 4:
            continue
        sha, author, author_time, subject = fields
        sha = sha.strip()
        if not sha:
            continue
        try:
            created_at = datetime.fromisoformat(author_time)
        except ValueError:
            continue
        for change_line in body.splitlines():
            if not change_line:
                continue
            path = _effective_path(change_line)
            if path is None:
                continue
            try:
                parse_note_path(path, allow_archive=True)
            except PathRejected:
                continue
            found = _read_note_at(git, sha, path)
            if found is None:
                continue
            note_id, content = found
            revisions.setdefault(note_id, []).append(
                _HistoricalRevision(
                    sha=sha,
                    author=author,
                    created_at=created_at,
                    message=subject,
                    path=path,
                    content=content,
                )
            )
    return revisions


def _require_git_vault(vault_root: Path) -> Path:
    """`vault_root`, resolved, if it is a directory with a `.git` - shared by
    `dry_run`/`import_vault`.

    Raises `MigrationError` otherwise.
    """
    vault_root = vault_root.resolve()
    if not vault_root.is_dir():
        raise MigrationError(f"vault root '{vault_root}' is not a directory")
    if not (vault_root / _GIT_DIR).exists():
        raise MigrationError(
            f"vault root '{vault_root}' is not a git working copy - no '.git' found"
        )
    return vault_root


def dry_run(vault_root: Path, map_entries: Mapping[str, MapEntry]) -> DryRunReport:
    """Report the mapping and every note `vault_root` would import, without writing anything.

    Raises `MigrationError` if `vault_root` is not a directory or not a Git
    working copy. Everything else - an unmapped namespace, a stale `--map`
    entry, an invalid note, a secret, an unresolved conflict file - is
    collected into the returned report (`DryRunReport.ok`), not raised:
    the caller sees every problem at once, the same way `doctor` does.
    """
    vault_root = _require_git_vault(vault_root)

    namespaces = discover_namespaces(vault_root)
    unknown_mappings = tuple(sorted(git_ns for git_ns in map_entries if git_ns not in namespaces))

    live_counts: dict[str, int] = {}
    archived_counts: dict[str, int] = {}
    id_namespace: dict[str, str] = {}
    problems: dict[str, list[str]] = {namespace: [] for namespace in namespaces}

    for file_path in iter_md_files(vault_root):
        rel = file_path.relative_to(vault_root).as_posix()

        if rel.endswith(_CONFLICT_SUFFIX):
            problems.setdefault(_top_level_namespace(rel), []).append(
                f"{rel}: unresolved conflict file"
            )
            continue

        try:
            note_path = parse_note_path(rel, allow_archive=True)
        except PathRejected as exc:
            problems.setdefault(_top_level_namespace(rel), []).append(
                f"{rel}: invalid path - {exc}"
            )
            continue

        namespace = note_path.namespace
        data = file_path.read_bytes()

        try:
            note = validate_bytes(data, expected_type=note_path.type)
        except NoteFormatError as exc:
            problems.setdefault(namespace, []).append(f"{rel}: {exc}")
            continue
        except NoteInvalid as exc:
            for issue in exc.issues:
                problems.setdefault(namespace, []).append(f"{rel}: {issue.message}")
            continue

        try:
            check_secrets(data.decode("utf-8"))
        except SecretFound as exc:
            problems.setdefault(namespace, []).append(f"{rel}: {exc}")
            continue

        if note_path.archived:
            archived_counts[namespace] = archived_counts.get(namespace, 0) + 1
        else:
            live_counts[namespace] = live_counts.get(namespace, 0) + 1
        id_namespace[note.id] = namespace

    revisions_by_namespace: dict[str, int] = {}
    for note_id, note_revisions in _revisions_by_id(vault_root).items():
        owning_namespace = id_namespace.get(note_id)
        if owning_namespace is None:
            continue
        revisions_by_namespace[owning_namespace] = revisions_by_namespace.get(
            owning_namespace, 0
        ) + len(note_revisions)

    reports = tuple(
        NamespaceReport(
            git_namespace=namespace,
            target=map_entries.get(namespace),
            live_notes=live_counts.get(namespace, 0),
            archived_notes=archived_counts.get(namespace, 0),
            revisions=revisions_by_namespace.get(namespace, 0),
            problems=tuple(problems.get(namespace, [])),
        )
        for namespace in sorted(namespaces)
    )

    return DryRunReport(namespaces=reports, unknown_mappings=unknown_mappings)


@dataclass(frozen=True)
class _CurrentNote:
    """One note `import_vault` found in `vault_root`'s current working tree."""

    note_id: str
    git_namespace: str
    note_path: NotePath
    content: bytes


def _catalog_current_notes(vault_root: Path) -> dict[str, _CurrentNote]:
    """Every current note in `vault_root`, keyed by id.

    Assumes the caller already confirmed `dry_run(vault_root, ...).ok` -
    `import_vault` always does, right before calling this - so a file that
    fails to parse here is silently skipped rather than reported again; it
    cannot happen once `dry_run` is clean.
    """
    catalog: dict[str, _CurrentNote] = {}
    for file_path in iter_md_files(vault_root):
        rel = file_path.relative_to(vault_root).as_posix()
        if rel.endswith(_CONFLICT_SUFFIX):
            continue
        try:
            note_path = parse_note_path(rel, allow_archive=True)
        except PathRejected:
            continue
        data = file_path.read_bytes()
        try:
            note = parse_note_structural(data)
        except NoteFormatError:
            continue
        catalog[note.id] = _CurrentNote(
            note_id=note.id, git_namespace=note_path.namespace, note_path=note_path, content=data
        )
    return catalog


@dataclass(frozen=True)
class NamespaceImportResult:
    """What `import_vault` did for one mapped Git namespace."""

    git_namespace: str
    target: MapEntry
    stored_alias: str
    imported_notes: int
    imported_revisions: int
    refused: bool
    reason: str | None


@dataclass(frozen=True)
class ImportReport:
    """Everything `import_vault` did, one `NamespaceImportResult` per mapped namespace."""

    namespaces: tuple[NamespaceImportResult, ...]

    @property
    def ok(self) -> bool:
        """Whether every mapped namespace actually got imported, none refused."""
        return all(not ns.refused for ns in self.namespaces)


_SELECT_NAMESPACE_HAS_NOTES = "select exists(select 1 from vault_notes where namespace = $1)"

_INSERT_NAMESPACE = """
insert into namespaces (kind, external_key, alias)
values ($1, $2, $3)
on conflict (kind, external_key) do update
    set alias = coalesce(namespaces.alias, excluded.alias)
returning alias
"""

_INSERT_CURRENT_NOTE = """
insert into vault_notes (id, namespace, path, content, version, current_revision)
values ($1, $2, $3, $4, $5, $6)
"""

_INSERT_IMPORTED_REVISION = """
insert into vault_revisions
    (note_id, revision, path, content, version, author, client, message, created_at, author_oid)
values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""


async def _resolve_or_create_namespace(
    conn: asyncpg.pool.PoolConnectionProxy, target: MapEntry
) -> str:
    """The stored alias `target` resolves to, creating the registry row if needed.

    `kind == "user"` reuses `mm_ensure_personal_ns()`
    (`migrations/0009_namespace_resolution.sql`) rather than duplicating its
    id-derived `u-<id>` alias scheme: `set_config('app.oid', ..., true)`
    scopes the identity to this call's own transaction only (`is_local`), so
    it never leaks onto anything else sharing `conn`. Every other kind
    already carries a concrete alias (`MapEntry`'s own invariant, enforced
    by `parse_map_entry`); the upsert is idempotent, so a namespace a
    previous run - or WP-19's own provisioning - already created is reused
    unchanged rather than fought over.
    """
    if target.kind == "user":
        await conn.execute("select set_config('app.oid', $1, true)", target.key)
        alias = await conn.fetchval("select mm_ensure_personal_ns()")
        if (
            alias is None
        ):  # pragma: no cover - target.key is never empty, parse_map_entry rejects that
            raise MigrationError(f"could not resolve a personal namespace for oid {target.key!r}")
        return str(alias)

    if target.alias is None:  # pragma: no cover - MapEntry's own invariant for group/project/org
        raise MigrationError(f"map entry for {target.git_namespace!r} carries no alias")
    row = await conn.fetchrow(_INSERT_NAMESPACE, target.kind, target.key, target.alias)
    if row is None:  # pragma: no cover - the upsert's own RETURNING always produces one row
        raise MigrationError(f"could not resolve a namespace for {target.kind}:{target.key}")
    return str(row["alias"])


@dataclass(frozen=True)
class _ImportedNoteAudit:
    """One imported note's audit record, built while its transaction is still open,
    but only ever written to `audit_log` once that transaction has actually committed
    (`_import_namespace`'s own docstring).
    """

    path: str
    version: str
    revisions_written: int
    commit_sha: str | None


async def _import_namespace(
    conn: asyncpg.pool.PoolConnectionProxy,
    git_namespace: str,
    target: MapEntry,
    notes: Sequence[_CurrentNote],
    history: Mapping[str, Sequence[_HistoricalRevision]],
    audit: AuditWriter,
) -> NamespaceImportResult:
    """Import every one of `notes` and its history into Postgres, in one transaction.

    Refused (without writing a single row) if the stored namespace already
    has any note at all - the "merge into a non-empty namespace" case this
    task deliberately leaves out (#247's "Not included"). Any other failure
    - a bug or an `asyncpg.PostgresError` - is never caught here: it
    propagates out of `conn.transaction()`, which rolls back everything this
    namespace wrote so far, leaving it exactly as empty as before this call.

    Only the current (HEAD) bytes of each note were scanned for secrets, in
    the `dry_run` preflight `import_vault` already ran; a historical
    revision's content is written unchanged from Git, never re-scanned -
    Git's own history already holds it unchanged, so importing it creates no
    new exposure beyond what the vault's own history already is, and
    rewriting history to redact one is explicitly out of scope (ADR-0007 §6
    "Git history as revisions").

    `audit_log` gets one row per note actually committed here (CLAUDE.md
    "audit log for every write"; ADR-0008 addendum: system-identity writes
    are audited too, same as every other write) - or exactly one row for
    the namespace if it was refused - written only *after* `conn`'s own
    transaction has committed or rolled back, the same ordering
    `app.py`'s own write-queue/`PostgresBackend` audit hooks use: a
    rolled-back write (the injected-failure case) is therefore never
    audited as having happened. `detail` carries a version, a revision
    count and a commit sha only, never a note's content or body text -
    `AuditWriter`'s own docstring, "the one place a note's content could
    leak into the audit log".
    """
    imported: list[_ImportedNoteAudit] = []
    refused_reason: str | None = None

    async with conn.transaction():
        stored_alias = await _resolve_or_create_namespace(conn, target)
        already_populated = await conn.fetchval(_SELECT_NAMESPACE_HAS_NOTES, stored_alias)
        if already_populated:
            refused_reason = f"namespace {stored_alias!r} already holds notes"
        else:
            # ADR-0008 addendum "curate is author-based": only a 'user'
            # namespace's revisions carry an `author_oid` at all, so curate
            # stays the owner's; every shared namespace's imported revisions
            # are NULL, i.e. foreign.
            author_oid = target.key if target.kind == "user" else None
            for note in sorted(notes, key=lambda n: n.note_path.relative):
                note_revisions = history.get(note.note_id, ())
                current_revision = len(note_revisions) or 1
                stored_path = replace(note.note_path, namespace=stored_alias).relative
                note_content_version = note_version(note.content)
                await conn.execute(
                    _INSERT_CURRENT_NOTE,
                    note.note_id,
                    stored_alias,
                    stored_path,
                    note.content,
                    note_content_version,
                    current_revision,
                )
                last_sha: str | None = None
                revisions_written = 0
                for revision_number, revision in enumerate(note_revisions, start=1):
                    revision_path = replace(
                        parse_note_path(revision.path, allow_archive=True), namespace=stored_alias
                    ).relative
                    await conn.execute(
                        _INSERT_IMPORTED_REVISION,
                        note.note_id,
                        revision_number,
                        revision_path,
                        revision.content,
                        note_version(revision.content),
                        revision.author,
                        _IMPORT_CLIENT,
                        revision.message,
                        revision.created_at,
                        author_oid,
                    )
                    last_sha = revision.sha
                    revisions_written += 1
                imported.append(
                    _ImportedNoteAudit(
                        path=stored_path,
                        version=note_content_version,
                        revisions_written=revisions_written,
                        commit_sha=last_sha,
                    )
                )

    if refused_reason is not None:
        await audit.record(
            actor=_IMPORT_ACTOR,
            client=_IMPORT_CLIENT,
            op=_IMPORT_OP,
            path=None,
            commit_sha=None,
            outcome="rejected",
            detail={"namespace": stored_alias, "reason": refused_reason},
        )
        return NamespaceImportResult(
            git_namespace=git_namespace,
            target=target,
            stored_alias=stored_alias,
            imported_notes=0,
            imported_revisions=0,
            refused=True,
            reason=refused_reason,
        )

    for note_audit in imported:
        await audit.record(
            actor=_IMPORT_ACTOR,
            client=_IMPORT_CLIENT,
            op=_IMPORT_OP,
            path=note_audit.path,
            commit_sha=note_audit.commit_sha,
            outcome="ok",
            detail={"version": note_audit.version, "revisions": note_audit.revisions_written},
        )

    return NamespaceImportResult(
        git_namespace=git_namespace,
        target=target,
        stored_alias=stored_alias,
        imported_notes=len(imported),
        imported_revisions=sum(note_audit.revisions_written for note_audit in imported),
        refused=False,
        reason=None,
    )


async def import_vault(
    pool: asyncpg.Pool, vault_root: Path, map_entries: Mapping[str, MapEntry]
) -> ImportReport:
    """Import `vault_root` into Postgres per `map_entries` (#247, ADR-0007 §6).

    Re-runs `dry_run` itself first and raises `MigrationError` if it is not
    `.ok` - a caller must never import a vault this module itself would
    refuse to even report cleanly, and `report.ok` already guarantees every
    `map_entries` key names a namespace that actually exists and every
    discovered namespace has an entry (`DryRunReport.ok`'s own check of
    `unmapped`/`unknown_mappings`), so the loop below never has to re-check
    either. Every mapped namespace gets its own connection and its own
    transaction (`_import_namespace`) - one namespace failing or being
    refused never stops another from importing.
    """
    report = dry_run(vault_root, map_entries)
    if not report.ok:
        raise MigrationError("dry run found problems - run --dry-run first and fix them")

    vault_root = _require_git_vault(vault_root)
    history = _revisions_by_id(vault_root)
    current = _catalog_current_notes(vault_root)

    notes_by_namespace: dict[str, list[_CurrentNote]] = {}
    for note in current.values():
        notes_by_namespace.setdefault(note.git_namespace, []).append(note)

    audit = AuditWriter(pool)
    results: list[NamespaceImportResult] = []
    for git_namespace in sorted(map_entries):
        target = map_entries[git_namespace]
        notes = notes_by_namespace.get(git_namespace, [])
        async with pool.acquire() as conn:
            result = await _import_namespace(conn, git_namespace, target, notes, history, audit)
        results.append(result)

    return ImportReport(namespaces=tuple(results))

# SPDX-License-Identifier: AGPL-3.0-only
"""Dry run for `memory-manager migrate git-to-postgres` (#246, ADR-0007 §6).

This module only ever reads: the vault's working tree and its `git log`
history. It never opens a database connection and never writes anything -
writing the imported notes and revisions into Postgres is #247 (the issue's
"Not included").

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
archive move across commits.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from memory_manager.vault.git import Git
from memory_manager.vault.note import NoteFormatError
from memory_manager.vault.note import parse as parse_note_structural
from memory_manager.vault.paths import PathRejected, iter_md_files, parse_note_path
from memory_manager.vault.secrets import SecretFound
from memory_manager.vault.secrets import check as check_secrets
from memory_manager.vault.validate import NoteInvalid, validate_bytes

__all__ = [
    "DryRunReport",
    "MapEntry",
    "MapError",
    "MigrationError",
    "NamespaceReport",
    "discover_namespaces",
    "dry_run",
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


def _read_id_at(git: Git, sha: str, path: str) -> str | None:
    """The frontmatter `id` of `path` as it read at commit `sha`, `None` if unreadable.

    Only a structural parse (`vault.note.parse`) - a historical revision is
    never re-validated against today's semantic rules, only used to key it
    to the note it belongs to.
    """
    result = git.run("show", f"{sha}:{path}", check=False)
    if result.returncode != 0:
        return None
    try:
        return parse_note_structural(result.stdout).id
    except NoteFormatError:
        return None


def _revisions_by_id(vault_root: Path) -> dict[str, int]:
    """Every note id's revision count across the whole history of `vault_root`.

    One `git log --reverse -M --name-status` walk, oldest commit first. `-M`
    turns an archive move or a plain rename into a single `R`-status line
    instead of a `D`+`A` pair, but the `id`-keyed counting below is correct
    either way: a `D` carries no content and is never counted, so a rename
    git does not think similar enough to flag only ever contributes the one
    `A` side.
    """
    git = Git(cwd=vault_root)
    result = git.run("log", "--reverse", "-M", "--name-status", "--format=%x00%H", check=False)
    if result.returncode != 0:
        return {}

    counts: dict[str, int] = {}
    text = result.stdout.decode("utf-8", errors="replace")
    for commit_block in text.split("\x00"):
        if not commit_block:
            continue
        sha, _, body = commit_block.partition("\n")
        sha = sha.strip()
        if not sha:
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
            note_id = _read_id_at(git, sha, path)
            if note_id is None:
                continue
            counts[note_id] = counts.get(note_id, 0) + 1
    return counts


def dry_run(vault_root: Path, map_entries: Mapping[str, MapEntry]) -> DryRunReport:
    """Report the mapping and every note `vault_root` would import, without writing anything.

    Raises `MigrationError` if `vault_root` is not a directory or not a Git
    working copy. Everything else - an unmapped namespace, a stale `--map`
    entry, an invalid note, a secret, an unresolved conflict file - is
    collected into the returned report (`DryRunReport.ok`), not raised:
    the caller sees every problem at once, the same way `doctor` does.
    """
    vault_root = vault_root.resolve()
    if not vault_root.is_dir():
        raise MigrationError(f"vault root '{vault_root}' is not a directory")
    if not (vault_root / _GIT_DIR).exists():
        raise MigrationError(
            f"vault root '{vault_root}' is not a git working copy - no '.git' found"
        )

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
    for note_id, count in _revisions_by_id(vault_root).items():
        owning_namespace = id_namespace.get(note_id)
        if owning_namespace is None:
            continue
        revisions_by_namespace[owning_namespace] = (
            revisions_by_namespace.get(owning_namespace, 0) + count
        )

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

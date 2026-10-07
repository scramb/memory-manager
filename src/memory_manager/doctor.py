# SPDX-License-Identifier: AGPL-3.0-only
"""Vault health check: walk every note on disk and report what is wrong (#31).

`run_doctor` is the read-only counterpart to the write path's validation: it
walks `vault_root` and applies the same rules `vault.paths`, `vault.note`,
`vault.validate` and `vault.secrets` apply on write, plus a few checks that
only make sense across the whole vault at once (duplicate ids, dangling
`supersedes`, dangling `[[links]]`). Nothing here writes to the vault.

Errors are things that must be fixed before the vault can be trusted: a
broken path, invalid frontmatter, a duplicate id, a secret. Warnings are
things worth looking at but that do not corrupt the index: a non-canonical
file a human edited, a `supersedes` pointing at an unknown id, a dangling
link, or a `*.conflict.md` file waiting for a human to resolve it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from memory_manager.vault.links import VaultEntry, extract_links, resolve_links
from memory_manager.vault.note import Note, NoteFormatError, serialize
from memory_manager.vault.paths import PathRejected, iter_md_files, parse_note_path
from memory_manager.vault.secrets import scan as scan_secrets
from memory_manager.vault.validate import NoteInvalid, validate_bytes

__all__ = ["DoctorReport", "run_doctor"]

_CONFLICT_SUFFIX = ".conflict.md"


@dataclass
class DoctorReport:
    """Everything `run_doctor` found, in the order it found it."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class _LiveNote:
    rel: str
    note: Note
    namespace: str
    slug: str


def run_doctor(vault_root: Path) -> DoctorReport:
    """Walk `vault_root` and check every `.md` file against ADR-0005.

    Skips `.git` and never follows a symlink (`vault.paths.iter_md_files`) - a
    `*.md` symlink has no business in the vault and must never have its
    target's content read, scanned or reported back as if it were a note.
    A `*.conflict.md` file is reported as a warning (a conflict is waiting
    for a human to resolve) and otherwise left alone - it is never valid
    note content on its own and is not expected to parse.
    """
    report = DoctorReport()
    live_notes: list[_LiveNote] = []

    for file in iter_md_files(vault_root):
        rel = "/".join(file.relative_to(vault_root).parts)
        data = file.read_bytes()

        text = _decode(data)
        if text is not None:
            for finding in scan_secrets(text):
                report.errors.append(
                    f"{rel}: line {finding.line} looks like {finding.description} "
                    f"(rule {finding.rule_id})"
                )

        if file.name.endswith(_CONFLICT_SUFFIX):
            report.warnings.append(f"{rel}: conflict awaits resolution")
            continue

        try:
            note_path = parse_note_path(rel, allow_archive=True)
        except PathRejected as exc:
            report.errors.append(f"{rel}: invalid path - {exc}")
            continue

        try:
            note = validate_bytes(data, expected_type=note_path.type)
        except NoteFormatError as exc:
            report.errors.append(f"{rel}: {exc}")
            continue
        except NoteInvalid as exc:
            for issue in exc.issues:
                report.errors.append(f"{rel}: {issue.message}")
            continue

        if serialize(note) != data:
            report.warnings.append(f"{rel}: not canonical, a server write will reformat it")

        live_notes.append(
            _LiveNote(rel=rel, note=note, namespace=note_path.namespace, slug=note_path.slug)
        )

    _check_duplicate_ids(live_notes, report)
    _check_supersedes(live_notes, report)
    _check_dangling_links(live_notes, report)

    return report


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _check_duplicate_ids(live_notes: list[_LiveNote], report: DoctorReport) -> None:
    by_id: dict[str, list[str]] = {}
    for live in live_notes:
        by_id.setdefault(live.note.id, []).append(live.rel)
    for note_id, rels in sorted(by_id.items()):
        if len(rels) > 1:
            report.errors.append(f"id {note_id} is used by more than one file: {', '.join(rels)}")


def _check_supersedes(live_notes: list[_LiveNote], report: DoctorReport) -> None:
    known_ids = {live.note.id for live in live_notes}
    for live in live_notes:
        for superseded_id in live.note.supersedes:
            if superseded_id not in known_ids:
                report.warnings.append(f"{live.rel}: supersedes unknown id {superseded_id}")


def _check_dangling_links(live_notes: list[_LiveNote], report: DoctorReport) -> None:
    entries = [
        VaultEntry(
            path=live.rel,
            namespace=live.namespace,
            slug=live.slug,
            aliases=live.note.aliases,
        )
        for live in live_notes
    ]
    all_namespaces = {live.namespace for live in live_notes}

    for live in live_notes:
        refs = extract_links(live.note.body)
        if not refs:
            continue
        resolved = resolve_links(
            refs,
            source_namespace=live.namespace,
            entries=entries,
            readable_namespaces=all_namespaces,
        )
        for link in resolved:
            if link.dangling:
                report.warnings.append(
                    f"{live.rel}: dangling link '[[{link.ref.target}]]' at line {link.ref.line}"
                )

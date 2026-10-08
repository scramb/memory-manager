# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the synthetic load-test vault generator (#107)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from loadtest.generate import generate
from memory_manager.vault.note import NoteFormatError
from memory_manager.vault.paths import parse_note_path
from memory_manager.vault.secrets import scan
from memory_manager.vault.ulid import is_ulid
from memory_manager.vault.validate import NoteInvalid, validate_bytes

_REPO_ROOT = Path(__file__).resolve().parents[2]

_NOTES = 300
_USERS = 10
_GROUPS = 3
_SEED = 1


def _generate(out: Path, *, seed: int = _SEED) -> None:
    generate(notes=_NOTES, users=_USERS, groups=_GROUPS, seed=seed, out=out)


def _hash_tree(root: Path) -> str:
    """SHA-256 over every file under `root`, sorted by its relative path."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_namespaces(out: Path) -> dict[str, dict[str, object]]:
    payload = json.loads((out / "namespaces.json").read_text(encoding="utf-8"))
    namespaces: dict[str, dict[str, object]] = payload["namespaces"]
    return namespaces


def _load_queries(out: Path) -> list[dict[str, object]]:
    lines = (out / "queries.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_every_note_is_valid_and_has_a_unique_ulid(tmp_path: Path) -> None:
    out = tmp_path / "vault-out"
    _generate(out)

    vault_root = out / "vault"
    md_files = sorted(vault_root.rglob("*.md"))
    assert md_files, "generator produced no notes"

    ids: list[str] = []
    for path in md_files:
        rel = path.relative_to(vault_root)
        note_path = parse_note_path(str(rel))
        data = path.read_bytes()
        try:
            note = validate_bytes(data, expected_type=note_path.type)
        except (NoteFormatError, NoteInvalid) as exc:
            raise AssertionError(f"{rel} failed validation: {exc}") from exc
        assert is_ulid(note.id), f"{rel}: id {note.id!r} is not a valid ULID"
        ids.append(note.id)

        findings = scan(data.decode("utf-8"))
        assert not findings, f"{rel} triggered the secret scan: {findings}"

    assert len(ids) == len(set(ids)), "note ids are not unique"


def test_same_seed_is_byte_identical_across_a_subprocess_with_a_different_hash_seed(
    tmp_path: Path,
) -> None:
    in_process_out = tmp_path / "in-process"
    _generate(in_process_out)

    subprocess_out = tmp_path / "subprocess"
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "4242"
    subprocess.run(  # noqa: S603 - fixed executable, argument list, no shell
        [
            sys.executable,
            "-m",
            "loadtest.generate",
            "--notes",
            str(_NOTES),
            "--users",
            str(_USERS),
            "--groups",
            str(_GROUPS),
            "--seed",
            str(_SEED),
            "--out",
            str(subprocess_out),
        ],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert _hash_tree(in_process_out) == _hash_tree(subprocess_out)


def test_different_seed_changes_the_output(tmp_path: Path) -> None:
    out_a = tmp_path / "seed-a"
    out_b = tmp_path / "seed-b"
    _generate(out_a, seed=_SEED)
    _generate(out_b, seed=_SEED + 1)

    assert _hash_tree(out_a) != _hash_tree(out_b)


def test_queries_point_at_their_marker_and_nowhere_else(tmp_path: Path) -> None:
    out = tmp_path / "vault-out"
    _generate(out)

    vault_root = out / "vault"
    bodies = {
        str(path.relative_to(vault_root)): path.read_text(encoding="utf-8")
        for path in vault_root.rglob("*.md")
    }
    ids_by_path = {rel: validate_bytes(text.encode("utf-8")).id for rel, text in bodies.items()}
    path_by_id = {note_id: rel for rel, note_id in ids_by_path.items()}

    queries = _load_queries(out)
    assert queries, "generator produced no queries"

    for query in queries:
        expected_ids = query["expected"]
        assert isinstance(expected_ids, list) and expected_ids
        for note_id in expected_ids:
            assert note_id in path_by_id, f"{query['id']}: expected id {note_id} does not exist"

        query_text = query["query"]
        assert isinstance(query_text, str)
        marker = query_text.rsplit(" ", 1)[-1].rstrip("?")
        matching = [rel for rel, text in bodies.items() if marker in text]
        expected_paths = {path_by_id[note_id] for note_id in expected_ids}
        assert set(matching) == expected_paths, (
            f"{query['id']}: marker {marker!r} found in {matching}, expected {expected_paths}"
        )


def test_namespaces_file_matches_the_generated_vault_and_is_well_formed(tmp_path: Path) -> None:
    out = tmp_path / "vault-out"
    _generate(out)

    namespaces = _load_namespaces(out)
    kinds = {entry["kind"] for entry in namespaces.values()}
    assert kinds == {"personal", "group", "org"}

    users = {alias for alias, entry in namespaces.items() if entry["kind"] == "personal"}
    for alias, entry in namespaces.items():
        if entry["kind"] == "group":
            members = entry["members"]
            assert isinstance(members, list)
            assert set(members) <= users, f"{alias}: members not a subset of users"

    vault_root = out / "vault"
    namespace_dirs = {p.name for p in vault_root.iterdir() if p.is_dir()}
    assert namespace_dirs <= namespaces.keys()


def test_no_single_word_dominates_the_generated_titles(tmp_path: Path) -> None:
    """#108 round 1: the old generator put the literal word "note" in every
    title, which let full-text search rank every chunk for a query
    containing "note" - this is the regression test for that specific
    failure, not a general style check."""
    out = tmp_path / "vault-out"
    _generate(out)

    vault_root = out / "vault"
    titles = [validate_bytes(path.read_bytes()).title.lower() for path in vault_root.rglob("*.md")]
    assert titles, "generator produced no notes"

    word_counts = Counter(word for title in titles for word in title.split())
    most_common_word, occurrences = word_counts.most_common(1)[0]
    share = occurrences / len(titles)
    assert share <= 0.5, (
        f"{most_common_word!r} appears in {occurrences}/{len(titles)} titles ({share:.0%}) - "
        "a word this common would let full-text search rank every note for it"
    )


def test_namespace_sizes_are_clearly_unequal(tmp_path: Path) -> None:
    out = tmp_path / "vault-out"
    _generate(out)

    vault_root = out / "vault"
    counts = Counter(path.relative_to(vault_root).parts[0] for path in vault_root.rglob("*.md"))
    assert len(counts) > 1

    largest, smallest = max(counts.values()), min(counts.values())
    assert largest >= 3 * smallest, f"namespace sizes too even: {dict(counts)}"

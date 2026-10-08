# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the synthetic load-test vault generator (#107, #266)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

from loadtest.generate import generate
from loadtest.vectors import near_vector, synthetic_vector
from memory_manager.index.chunker import chunk_note
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


_MARKER_LINE_PREFIX = "Unique load-test marker: "


def _extract_marker(body_text: str) -> str | None:
    """The marker word `_build_body` planted at the very front of `body_text`,
    or `None` if there is none (a note with no `queries.jsonl` entry)."""
    idx = body_text.find(_MARKER_LINE_PREFIX)
    if idx == -1:
        return None
    rest = body_text[idx + len(_MARKER_LINE_PREFIX) :]
    return rest.split(".", 1)[0]


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

    vector_only_count = 0
    for query in queries:
        expected_ids = query["expected"]
        assert isinstance(expected_ids, list) and expected_ids
        for note_id in expected_ids:
            assert note_id in path_by_id, f"{query['id']}: expected id {note_id} does not exist"

        expected_paths = {path_by_id[note_id] for note_id in expected_ids}
        marker = _extract_marker(bodies[next(iter(expected_paths))])
        assert marker is not None, f"{query['id']}: expected note has no marker"
        matching = [rel for rel, text in bodies.items() if marker in text]
        assert set(matching) == expected_paths, (
            f"{query['id']}: marker {marker!r} found in {matching}, expected {expected_paths}"
        )

        # `vector_key` is "{note_id}:0" - the marker always lands in the
        # first-produced chunk (see `_Query`'s docstring in generate.py).
        assert query["vector_key"] == f"{expected_ids[0]}:0"

        query_text = query["query"]
        assert isinstance(query_text, str)
        if marker not in query_text:
            vector_only_count += 1

    assert 0 < vector_only_count < len(queries), (
        "expected a share of queries to be vector-only (no lexical marker in the "
        f"query text) and a share to carry the lexical marker, got {vector_only_count} "
        f"vector-only out of {len(queries)}"
    )


def test_namespaces_file_matches_the_generated_vault_and_is_well_formed(tmp_path: Path) -> None:
    out = tmp_path / "vault-out"
    _generate(out)

    namespaces = _load_namespaces(out)
    kinds = {entry["kind"] for entry in namespaces.values()}
    assert kinds == {"personal", "group", "project", "org"}

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


def test_every_namespace_kind_gets_notes_with_the_default_kinds_mix(tmp_path: Path) -> None:
    """#266: `--kinds`' default mix must emit `project` namespaces too, so
    every ADR-0016 `chunks` partition (`user`/`group`/`project`/`org`)
    receives rows once this vault is loaded."""
    out = tmp_path / "vault-out"
    _generate(out)

    namespaces = _load_namespaces(out)
    vault_root = out / "vault"
    namespace_dirs = {p.name for p in vault_root.iterdir() if p.is_dir()}
    kinds_with_notes = {namespaces[alias]["kind"] for alias in namespace_dirs}
    assert kinds_with_notes == {"personal", "group", "project", "org"}


def test_average_chunks_per_note_is_within_tolerance_of_the_target(tmp_path: Path) -> None:
    """#266: `--target-chunks-per-note` (default 5) must size bodies so that
    the real `index.chunker.chunk_note` yields, on average, that many
    chunks per note, within +-10%, over a 2,000-note sample."""
    out = tmp_path / "vault-out"
    target = 5.0
    generate(notes=2000, users=20, groups=5, seed=1, out=out, target_chunks_per_note=target)

    vault_root = out / "vault"
    md_files = list(vault_root.rglob("*.md"))
    assert len(md_files) == 2000

    chunk_counts = []
    for path in md_files:
        note = validate_bytes(path.read_bytes())
        chunks = chunk_note(
            note.title,
            note.body,
            description=note.description,
            aliases=note.aliases,
            tags=note.tags,
        )
        chunk_counts.append(len(chunks))

    average = sum(chunk_counts) / len(chunk_counts)
    deviation = abs(average - target) / target
    assert deviation <= 0.10, (
        f"average chunks/note {average:.3f} deviates {deviation:.1%} from target {target} "
        "(tolerance +-10%)"
    )


def test_synthetic_vector_is_deterministic_and_unit_length() -> None:
    dimension = 1024
    key = "01ARZ3NDEKTSV4RRFFQ69G5FAV:0"

    first = synthetic_vector(key, dimension)
    second = synthetic_vector(key, dimension)
    assert first == second, "same key/dimension must yield the same vector"
    assert len(first) == dimension

    norm = math.sqrt(sum(v * v for v in first))
    assert math.isclose(norm, 1.0, rel_tol=1e-9), f"vector is not unit length: {norm}"

    other = synthetic_vector("a-different-key:0", dimension)
    assert other != first, "different keys must yield different vectors"


def test_near_vector_of_a_marker_query_ranks_its_target_first(tmp_path: Path) -> None:
    """#266: `near_vector` of a marker query's `vector_key` must rank its own
    target chunk's `synthetic_vector` first by cosine, against 1,000 random
    keys' vectors."""
    dimension = 1024
    out = tmp_path / "vault-out"
    _generate(out)
    queries = _load_queries(out)
    assert queries, "generator produced no queries"

    target_key = str(queries[0]["vector_key"])
    query_vector = near_vector(target_key, dimension)
    target_vector = synthetic_vector(target_key, dimension)

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    target_cosine = cosine(query_vector, target_vector)

    rng = random.Random(4242)  # noqa: S311 - reproducible test data, not a secret
    random_cosines = [
        cosine(query_vector, synthetic_vector(f"random-key-{rng.getrandbits(64):016x}", dimension))
        for _ in range(1000)
    ]

    assert target_cosine > max(random_cosines), (
        f"target cosine {target_cosine:.4f} did not rank first against 1,000 random keys "
        f"(best random: {max(random_cosines):.4f})"
    )

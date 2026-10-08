# SPDX-License-Identifier: AGPL-3.0-only
"""Write a deterministic, synthetic vault for load testing (#107).

Usage::

    python -m loadtest.generate --notes 100000 --users 2000 --groups 100 \\
        --seed 1 --out ./loadtest-vault

The same `--seed` and the same other arguments always produce byte-identical
output (every note file, `namespaces.json` and `queries.jsonl`). A different
seed changes the random draws (namespace assignment, body filler words, the
local ULID's random bits, the `updated` offset) but not the overall shape:
same note count, same namespaces, same marker-bearing notes.

Everything here is invented: made-up words, made-up usernames, no real
personal data. Note ids are generated locally (not through
`memory_manager.vault.ulid.new_ulid`, which reaches into `secrets` and would
make two runs differ) but checked against `is_ulid` so they have the exact
same shape a real note id would.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import is_ulid
from memory_manager.vault.validate import MAX_FILE_BYTES, NOTE_TYPES

__all__ = ["main"]

# Mirrors memory_manager.vault.ulid's bit layout (48-bit ms timestamp + 80
# bits of randomness, Crockford base32), but is not allowed to import that
# module's private encoding table (Blast-Radius: read-only vault/*.py) - so
# it is duplicated here, deliberately small enough to keep in sync by eye.
_ULID_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_TIMESTAMP_BITS = 48
_ULID_RANDOM_BITS = 80
_ULID_CHAR_COUNT = 26
_ULID_CHAR_BITS = 5

# A fixed epoch keeps `created` reproducible across machines and clocks;
# `created` advances one whole second per note (ADR-0005 second precision).
_BASE_CREATED = datetime(2025, 1, 1, tzinfo=UTC)
_MAX_UPDATED_OFFSET_SECONDS = 86_400  # updated is created + up to one day

_MIN_BODY_BYTES = 512  # 0.5 KB, the generator's own body size floor

# Every Nth note (by generation index) gets a unique marker term planted in
# its body, and a matching entry in queries.jsonl - a load test query with a
# single, known-correct answer.
_QUERY_MARKER_INTERVAL = 25

_ORG_ALIAS = "org"

# Title/description word counts (#108 round 1): drawn from `_VOCAB` by
# `rng.sample` (no repeats within one title/description), never a fixed
# English word repeated on every note. The earlier generator put the
# literal word "note" in every title ("Synthetic note {index}") and every
# description ("Synthetic description for note {index} in ..."); full-text
# search then ranked every chunk for a query containing "note" - the search
# had to intersect a near-total-table posting list with the one note
# actually matching the marker. #117 now bounds that case via planner
# statistics, but the generator should not manufacture it in the first
# place.
_TITLE_WORD_COUNT = 3
_DESCRIPTION_WORD_COUNT = 4

# Invented words only (CLAUDE.md: no real personal data) - plain lower-case
# ASCII, nothing that resembles a hex/base32/ULID token, so the body filler
# can never trip the secret scan or be mistaken for a real identifier.
_VOCAB = (
    "lumen", "vantrix", "obelisk", "cobalt", "ember", "nautik", "pallid",
    "quorra", "silvan", "tundra", "wraith", "yonder", "zephyr", "amaranth",
    "borealis", "crescent", "driftwood", "echofen", "fennel", "glimmer",
    "harbor", "ivorywood", "jasperine", "kindle", "mossveil", "nimbus",
    "opaline", "petrichor", "quietude", "rivermist", "stonewick",
    "thistledown", "umbercross", "velvetine", "whisperlane", "xenolith",
    "yarrowfield", "zinnberry", "copperleaf", "duskwood", "foxglove",
)  # fmt: skip


@dataclass(frozen=True)
class _Namespace:
    """One synthetic namespace: its alias, kind and member user aliases."""

    alias: str
    kind: str  # "personal" | "group" | "org"
    members: tuple[str, ...]


@dataclass(frozen=True)
class _Query:
    """One `queries.jsonl` entry: a marker query with a known answer."""

    id: str
    query: str
    expected: tuple[str, ...]
    namespaces: tuple[str, ...]


def _local_ulid(created: datetime, rng: random.Random) -> str:
    """A deterministic ULID: `created`'s millisecond timestamp plus `rng` bits.

    Uses the given `rng` instance (not `secrets`, which `vault.ulid.new_ulid`
    uses) so the same seed always yields the same id. Checked against
    `is_ulid` because that is the one thing that must stay true: anything
    consuming this vault later expects a real ULID shape.
    """
    timestamp_ms = int(created.timestamp() * 1000)
    if timestamp_ms < 0 or timestamp_ms >= 1 << _ULID_TIMESTAMP_BITS:
        raise ValueError(f"timestamp out of ULID range: {timestamp_ms}")
    random_bits = rng.getrandbits(_ULID_RANDOM_BITS)
    value = (timestamp_ms << _ULID_RANDOM_BITS) | random_bits
    chars = []
    for i in range(_ULID_CHAR_COUNT):
        shift = _ULID_CHAR_BITS * (_ULID_CHAR_COUNT - 1 - i)
        chars.append(_ULID_ENCODING[(value >> shift) & 0x1F])
    ulid = "".join(chars)
    if not is_ulid(ulid):
        raise RuntimeError(f"generated id {ulid!r} is not a valid ULID - fix _local_ulid")
    return ulid


def _build_namespaces(users: int, groups: int, rng: random.Random) -> list[_Namespace]:
    """One personal namespace per user, `groups` groups, and one `org`.

    Group membership is a random sample of the users, so group sizes vary.
    `org`'s members are every user - the one namespace everyone is in.
    """
    user_aliases = tuple(f"user-{u:05d}" for u in range(1, users + 1))
    namespaces = [
        _Namespace(alias=alias, kind="personal", members=(alias,)) for alias in user_aliases
    ]
    for g in range(1, groups + 1):
        alias = f"group-{g:03d}"
        if user_aliases:
            size = rng.randint(1, len(user_aliases))
            members = tuple(sorted(rng.sample(user_aliases, size)))
        else:
            members = ()
        namespaces.append(_Namespace(alias=alias, kind="group", members=members))
    namespaces.append(_Namespace(alias=_ORG_ALIAS, kind="org", members=user_aliases))
    return namespaces


def _namespace_weights(count: int) -> list[float]:
    """Zipf-by-rank weights for `count` namespaces: rank 1 is `1/1`, rank 2 `1/2`, ..."""
    return [1.0 / rank for rank in range(1, count + 1)]


def _pick_namespace(
    rng: random.Random,
    namespaces: Sequence[_Namespace],
    cumulative_weights: Sequence[float],
    total_weight: float,
) -> _Namespace:
    """Pick one namespace per note via cumulative weights (no per-note list)."""
    draw = rng.random() * total_weight
    index = _bisect_left(cumulative_weights, draw)
    if index >= len(namespaces):
        index = len(namespaces) - 1
    return namespaces[index]


def _bisect_left(values: Sequence[float], target: float) -> int:
    low, high = 0, len(values)
    while low < high:
        mid = (low + high) // 2
        if values[mid] < target:
            low = mid + 1
        else:
            high = mid
    return low


def _build_title(rng: random.Random) -> str:
    """A title made of `_TITLE_WORD_COUNT` distinct `_VOCAB` words.

    Every title draws from the same 41-word pool, so no single word - unlike
    the old fixed "note" - dominates titles the way a fixed English word
    would (see `test_no_single_word_dominates_the_generated_titles`).
    """
    words = rng.sample(_VOCAB, _TITLE_WORD_COUNT)
    return " ".join(word.capitalize() for word in words)


def _build_description(rng: random.Random, namespace_alias: str) -> str:
    """A description made of `_DESCRIPTION_WORD_COUNT` distinct `_VOCAB` words
    plus the owning namespace's alias - never a fixed English phrase."""
    words = rng.sample(_VOCAB, _DESCRIPTION_WORD_COUNT)
    return f"{' '.join(words)} - {namespace_alias}"


def _build_body(rng: random.Random, target_bytes: int, marker: str | None) -> str:
    """`target_bytes` bytes (including the trailing `\\n`) of invented words.

    `marker`, if given, is a whole word placed at the very front, so the
    final truncation to `target_bytes` can never cut it in half.
    """
    parts: list[str] = []
    if marker is not None:
        parts.append(f"Unique load-test marker: {marker}.")
    length = sum(len(part) + 1 for part in parts)
    while length < target_bytes:
        word = rng.choice(_VOCAB)
        parts.append(word)
        length += len(word) + 1
    text = " ".join(parts)
    body = text[: max(target_bytes - 1, 1)]
    return body + "\n"


def _note_header_bytes(note_without_body: Note) -> int:
    """Byte length of `note_without_body`'s frontmatter, body excluded."""
    # `serialize` always appends a trailing "\n" to an empty body, so the
    # frontmatter itself is one byte shorter than the serialized length.
    return len(serialize(note_without_body)) - 1


def _build_note(
    index: int,
    *,
    namespaces: Sequence[_Namespace],
    cumulative_weights: Sequence[float],
    total_weight: float,
    slug_width: int,
    rng: random.Random,
) -> tuple[_Namespace, str, bytes, _Query | None]:
    """Build note `index`: its namespace, vault-relative slug, bytes, and
    the `queries.jsonl` entry it contributes, if any."""
    created = _BASE_CREATED + timedelta(seconds=index)
    updated = created + timedelta(seconds=rng.randint(0, _MAX_UPDATED_OFFSET_SECONDS))
    note_id = _local_ulid(created, rng)
    namespace = _pick_namespace(rng, namespaces, cumulative_weights, total_weight)
    note_type = NOTE_TYPES[index % len(NOTE_TYPES)]
    slug = f"note-{index:0{slug_width}d}"

    title = _build_title(rng)
    description = _build_description(rng, namespace.alias)

    marker = None
    if index % _QUERY_MARKER_INTERVAL == 0:
        marker = f"loadtestmarker{index:0{slug_width}d}"

    note_without_body = Note(
        id=note_id,
        title=title,
        description=description,
        type=note_type,
        created=created,
        updated=updated,
        body="",
    )
    header_bytes = _note_header_bytes(note_without_body)
    max_body_bytes = MAX_FILE_BYTES - header_bytes
    # Force the very first note to the file size cap, so the generated
    # vault always exercises the largest note the server will accept.
    target_bytes = max_body_bytes if index == 0 else rng.randint(_MIN_BODY_BYTES, max_body_bytes)

    note = replace(note_without_body, body=_build_body(rng, target_bytes, marker))
    data = serialize(note)
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError(f"note {index} is {len(data)} bytes, max {MAX_FILE_BYTES}")

    query = None
    if marker is not None:
        query = _Query(
            id=f"q{index:0{slug_width}d}",
            query=f"What is tagged with the unique marker {marker}?",
            expected=(note_id,),
            namespaces=(namespace.alias,),
        )
    return namespace, slug, data, query


def _write_namespaces_file(path: Path, namespaces: Sequence[_Namespace]) -> None:
    payload = {
        "namespaces": {
            ns.alias: {"kind": ns.kind, "members": list(ns.members)} for ns in namespaces
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_queries_file(path: Path, queries: Sequence[_Query]) -> None:
    lines = [
        json.dumps(
            {
                "id": query.id,
                "query": query.query,
                "expected": list(query.expected),
                "namespaces": list(query.namespaces),
            },
            sort_keys=True,
        )
        for query in queries
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def generate(*, notes: int, users: int, groups: int, seed: int, out: Path) -> None:
    """Write `notes` synthetic notes and the `namespaces.json`/`queries.jsonl`
    sidecars under `out`, deterministically for a given `seed`."""
    if notes < 1:
        raise ValueError(f"--notes must be >= 1, got {notes}")
    if users < 0:
        raise ValueError(f"--users must be >= 0, got {users}")
    if groups < 0:
        raise ValueError(f"--groups must be >= 0, got {groups}")

    # One RNG instance drives every random draw (namespace assignment, body
    # filler, the local ULID's random bits, the updated-time offset, group
    # membership) - this is synthetic test data, not a security boundary.
    rng = random.Random(seed)  # noqa: S311 - reproducible fixture data, not a secret

    namespaces = _build_namespaces(users, groups, rng)
    weights = _namespace_weights(len(namespaces))
    cumulative_weights = list(itertools.accumulate(weights))
    total_weight = cumulative_weights[-1]

    slug_width = max(6, len(str(notes - 1)))

    out.mkdir(parents=True, exist_ok=True)
    vault_dir = out / "vault"
    queries: list[_Query] = []

    for index in range(notes):
        namespace, slug, data, query = _build_note(
            index,
            namespaces=namespaces,
            cumulative_weights=cumulative_weights,
            total_weight=total_weight,
            slug_width=slug_width,
            rng=rng,
        )
        note_type = NOTE_TYPES[index % len(NOTE_TYPES)]
        note_path = vault_dir / namespace.alias / note_type / f"{slug}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_bytes(data)
        if query is not None:
            queries.append(query)

    _write_namespaces_file(out / "namespaces.json", namespaces)
    _write_queries_file(out / "queries.jsonl", queries)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.generate",
        description="write a deterministic, synthetic vault for load testing (#107)",
    )
    parser.add_argument("--notes", type=int, default=1000, help="number of notes to generate")
    parser.add_argument("--users", type=int, default=20, help="number of personal namespaces")
    parser.add_argument("--groups", type=int, default=5, help="number of group namespaces")
    parser.add_argument("--seed", type=int, default=1, help="seed; same seed = same output")
    parser.add_argument("--out", type=Path, default=Path("loadtest-vault"), help="output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and write the synthetic vault."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    generate(notes=args.notes, users=args.users, groups=args.groups, seed=args.seed, out=args.out)
    print(f"wrote {args.notes} notes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

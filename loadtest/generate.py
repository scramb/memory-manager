# SPDX-License-Identifier: AGPL-3.0-only
"""Write a deterministic, synthetic vault for load testing (#107, #266).

Usage::

    python -m loadtest.generate --notes 1000000 --users 2000 --groups 500 \\
        --projects 50 --target-chunks-per-note 5 --seed 1 --out ./loadtest-vault

The same `--seed` and the same other arguments always produce byte-identical
output (every note file, `namespaces.json` and `queries.jsonl`). A different
seed changes the random draws (namespace assignment, body filler words, the
local ULID's random bits, the `updated` offset) but not the overall shape:
same note count, same namespaces, same marker-bearing notes.

`--target-chunks-per-note` (#266, F-01's 1M notes / ~5M chunks acceptance)
sizes each note's body so that `index.chunker.chunk_note` yields, on
average, that many chunks per note - see `_body_byte_range`. `--kinds`/
`--projects` extend the namespace mix with ADR-0008's `project` kind (on top
of the existing personal/group/org), so every ADR-0016 `chunks` partition
(`user`/`group`/`project`/`org`) receives rows once this vault is loaded.
Every marker-bearing note's `queries.jsonl` entry also carries a
`vector_key` - the target chunk's `"{note_id}:{ord}"` identity, matching
the `unique (note_id, ord, namespace_kind)` constraint
`0012_vector_layout.sql` puts on `chunks` - for `loadtest.vectors`'s
`synthetic_vector`/`near_vector` (shared with the loader, #267, and the
embedding stub, #268) to turn into an actual vector later. A share of these
queries deliberately drop the lexical marker from their `query` text (see
`_build_vector_only_query_text`), so a hybrid run's vector-search branch has
queries it cannot win through full-text alone.

Everything here is invented: made-up words, made-up usernames, no real
personal data. Note ids are generated locally (not through
`memory_manager.vault.ulid.new_ulid`, which reaches into `secrets` and would
make two runs differ) but checked against `is_ulid` so they have the exact
same shape a real note id would.
"""

from __future__ import annotations

import argparse
import inspect
import itertools
import json
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memory_manager.index.chunker import chunk_note
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import is_ulid
from memory_manager.vault.validate import MAX_FILE_BYTES, NOTE_TYPES

__all__ = ["main"]

# Read from `chunk_note` itself (not duplicated as a literal) so this module
# can never silently drift from the real chunk size `index/indexer.py` uses -
# unlike `_ULID_ENCODING` below, which *is* a deliberate, by-eye-kept-in-sync
# duplicate, because `vault.ulid`'s table is private and Blast-Radius-excluded
# from this task; `chunk_note`'s default argument is neither.
_CHUNKER_DEFAULT_MAX_CHARS: int = inspect.signature(chunk_note).parameters["max_chars"].default

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

# Body length is drawn uniformly from a window centred on
# `target_chunks_per_note * _CHUNKER_DEFAULT_MAX_CHARS`, +/- this fraction -
# see `_body_byte_range`. Symmetric, so the sample mean converges on the
# target chunk count (test: within +/-10% over a 2,000-note sample).
_CHUNK_SIZE_SPREAD = 0.5

# Every Nth note (by generation index) gets a unique marker term planted in
# its body, and a matching entry in queries.jsonl - a load test query with a
# single, known-correct answer.
_QUERY_MARKER_INTERVAL = 25

# Of those marker notes, every Nth one's query drops the lexical marker from
# its `query` text (see `_build_vector_only_query_text`) - a 1-in-5 (20%)
# share of marker queries that a full-text search can never answer, only a
# vector search against `vector_key` can (#266).
_VECTOR_ONLY_QUERY_SHARE_DENOMINATOR = 5

_ORG_ALIAS = "org"
_ALL_NAMESPACE_KINDS = ("personal", "group", "project", "org")

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
    kind: str  # "personal" | "group" | "project" | "org"
    members: tuple[str, ...]


@dataclass(frozen=True)
class _Query:
    """One `queries.jsonl` entry: a marker query with a known answer.

    `vector_key` is the target chunk's `"{note_id}:{ord}"` identity (always
    chunk `0`: the marker is the first word of the body, which has no
    heading, so it always lands in `chunk_note`'s first-produced chunk,
    whatever the body's length) - `loadtest.vectors.synthetic_vector`/
    `near_vector`'s input, for the loader (#267) and the embedding stub
    (#268) to turn into an actual vector.
    """

    id: str
    query: str
    expected: tuple[str, ...]
    namespaces: tuple[str, ...]
    vector_key: str


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


def _parse_kinds(raw: str) -> tuple[str, ...]:
    """Parse `--kinds`' comma-separated list, e.g. `"personal,group,org"`."""
    kinds = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not kinds:
        raise ValueError("--kinds must name at least one namespace kind")
    unknown = sorted(set(kinds) - set(_ALL_NAMESPACE_KINDS))
    if unknown:
        raise ValueError(
            f"--kinds has unknown kind(s) {unknown}, expected a subset of {_ALL_NAMESPACE_KINDS}"
        )
    return kinds


def _sample_members(user_aliases: Sequence[str], rng: random.Random) -> tuple[str, ...]:
    """A random, varying-size subset of `user_aliases` - group/project membership."""
    if not user_aliases:
        return ()
    size = rng.randint(1, len(user_aliases))
    return tuple(sorted(rng.sample(user_aliases, size)))


def _build_namespaces(
    users: int, groups: int, projects: int, kinds: Sequence[str], rng: random.Random
) -> list[_Namespace]:
    """One personal namespace per user, `groups` groups, `projects` projects
    (ADR-0008's `project` kind) and one `org` - each only if its kind is in
    `kinds` (#266's `--kinds`, default every kind, so every ADR-0016
    `chunks` partition receives rows once this vault is loaded).

    Group/project membership is a random sample of the users, so sizes vary.
    `org`'s members are every user - the one namespace everyone is in.
    """
    user_aliases = tuple(f"user-{u:05d}" for u in range(1, users + 1))
    namespaces: list[_Namespace] = []
    if "personal" in kinds:
        namespaces.extend(
            _Namespace(alias=alias, kind="personal", members=(alias,)) for alias in user_aliases
        )
    if "group" in kinds:
        for g in range(1, groups + 1):
            alias = f"group-{g:03d}"
            namespaces.append(
                _Namespace(alias=alias, kind="group", members=_sample_members(user_aliases, rng))
            )
    if "project" in kinds:
        for p in range(1, projects + 1):
            alias = f"proj-{p:03d}"
            namespaces.append(
                _Namespace(alias=alias, kind="project", members=_sample_members(user_aliases, rng))
            )
    if "org" in kinds:
        namespaces.append(_Namespace(alias=_ORG_ALIAS, kind="org", members=user_aliases))
    if not namespaces:
        raise ValueError(
            "no namespaces to assign notes to - check --users/--groups/--projects/--kinds"
        )
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


def _build_vector_only_query_text(rng: random.Random) -> str:
    """A marker-free question: `_DESCRIPTION_WORD_COUNT` distinct `_VOCAB`
    words, never the unique marker - so only a vector search against the
    query's `vector_key` can resolve it, not full-text (#266)."""
    words = rng.sample(_VOCAB, _DESCRIPTION_WORD_COUNT)
    return f"Which note best matches the topic of {' '.join(words)}?"


def _body_byte_range(target_chunks_per_note: float, max_body_bytes: int) -> tuple[int, int]:
    """The `[low, high]` body-byte window `_build_note` draws from.

    Centred on `target_chunks_per_note * _CHUNKER_DEFAULT_MAX_CHARS` chars,
    +/- `_CHUNK_SIZE_SPREAD` - since the real body is plain, unbroken text
    (no headings, no blank lines), `index.chunker.chunk_note` hard-splits it
    into roughly `len(body) / _CHUNKER_DEFAULT_MAX_CHARS` chunks, so a
    uniform draw centred there converges, on average, on
    `target_chunks_per_note` (test: within +/-10% over a 2,000-note sample).
    Clamped into `[_MIN_BODY_BYTES, max_body_bytes]` - the generator's own
    floor and the 16 KiB file cap minus this note's frontmatter.

    Centred on `target_chunks_per_note - 0.5`, not `target_chunks_per_note`
    itself: `index.chunker._hard_split`'s last piece per note is a
    remainder, uniformly distributed between empty and a full `max_chars`
    (whatever is left once every earlier, near-`max_chars` piece is cut
    off) - so `ceil(body_chars / max_chars)` runs, on average, half a chunk
    *above* `body_chars / max_chars` (confirmed empirically: an uncorrected
    `target_chunks_per_note * max_chars` mean measured 5.49 chunks/note for
    a target of 5, a +9.7% bias - just inside this task's own +-10%
    tolerance, but not by a margin this generator should rely on).
    """
    mean_chars = max(0.1, target_chunks_per_note - 0.5) * _CHUNKER_DEFAULT_MAX_CHARS
    low = round(mean_chars * (1 - _CHUNK_SIZE_SPREAD))
    high = round(mean_chars * (1 + _CHUNK_SIZE_SPREAD))
    if high > max_body_bytes:
        # Shift the whole window down, keeping its width (and so the same
        # average chunk count the width implies) rather than clamping only
        # `high` - clamping `high` alone would skew the mean down without
        # also narrowing `low`, biasing the average below the target for
        # any `target_chunks_per_note` large enough to brush the 16 KiB cap.
        shift = high - max_body_bytes
        low -= shift
        high = max_body_bytes
    low = max(_MIN_BODY_BYTES, low)
    high = max(low, high)
    return low, high


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
    target_chunks_per_note: float,
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
    is_vector_only = False
    if index % _QUERY_MARKER_INTERVAL == 0:
        marker = f"loadtestmarker{index:0{slug_width}d}"
        marker_ordinal = index // _QUERY_MARKER_INTERVAL
        is_vector_only = marker_ordinal % _VECTOR_ONLY_QUERY_SHARE_DENOMINATOR == 0

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
    low, high = _body_byte_range(target_chunks_per_note, max_body_bytes)
    # Force the very first note to the file size cap, so the generated
    # vault always exercises the largest note the server will accept.
    target_bytes = max_body_bytes if index == 0 else rng.randint(low, high)

    note = replace(note_without_body, body=_build_body(rng, target_bytes, marker))
    data = serialize(note)
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError(f"note {index} is {len(data)} bytes, max {MAX_FILE_BYTES}")

    query = None
    if marker is not None:
        # Always chunk 0: the marker is the body's very first word and the
        # body has no heading, so it always lands in `chunk_note`'s
        # first-produced chunk, whatever the body's length (see `_Query`).
        vector_key = f"{note_id}:0"
        query_text = (
            _build_vector_only_query_text(rng)
            if is_vector_only
            else f"What is tagged with the unique marker {marker}?"
        )
        query = _Query(
            id=f"q{index:0{slug_width}d}",
            query=query_text,
            expected=(note_id,),
            namespaces=(namespace.alias,),
            vector_key=vector_key,
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
                "vector_key": query.vector_key,
            },
            sort_keys=True,
        )
        for query in queries
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def generate(
    *,
    notes: int,
    users: int,
    groups: int,
    seed: int,
    out: Path,
    projects: int = 10,
    kinds: Sequence[str] = _ALL_NAMESPACE_KINDS,
    target_chunks_per_note: float = 5.0,
) -> None:
    """Write `notes` synthetic notes and the `namespaces.json`/`queries.jsonl`
    sidecars under `out`, deterministically for a given `seed`."""
    if notes < 1:
        raise ValueError(f"--notes must be >= 1, got {notes}")
    if users < 0:
        raise ValueError(f"--users must be >= 0, got {users}")
    if groups < 0:
        raise ValueError(f"--groups must be >= 0, got {groups}")
    if projects < 0:
        raise ValueError(f"--projects must be >= 0, got {projects}")
    if target_chunks_per_note <= 0:
        raise ValueError(f"--target-chunks-per-note must be > 0, got {target_chunks_per_note}")

    # One RNG instance drives every random draw (namespace assignment, body
    # filler, the local ULID's random bits, the updated-time offset, group
    # membership) - this is synthetic test data, not a security boundary.
    rng = random.Random(seed)  # noqa: S311 - reproducible fixture data, not a secret

    namespaces = _build_namespaces(users, groups, projects, kinds, rng)
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
            target_chunks_per_note=target_chunks_per_note,
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
        description="write a deterministic, synthetic vault for load testing (#107, #266)",
    )
    parser.add_argument("--notes", type=int, default=1000, help="number of notes to generate")
    parser.add_argument("--users", type=int, default=20, help="number of personal namespaces")
    parser.add_argument("--groups", type=int, default=5, help="number of group namespaces")
    parser.add_argument(
        "--projects", type=int, default=10, help="number of project namespaces (ADR-0008)"
    )
    parser.add_argument(
        "--kinds",
        default=",".join(_ALL_NAMESPACE_KINDS),
        help=f"comma-separated namespace kinds to generate, subset of {_ALL_NAMESPACE_KINDS}",
    )
    parser.add_argument(
        "--target-chunks-per-note",
        type=float,
        default=5.0,
        help="average chunks index.chunker should yield per note (#266)",
    )
    parser.add_argument("--seed", type=int, default=1, help="seed; same seed = same output")
    parser.add_argument("--out", type=Path, default=Path("loadtest-vault"), help="output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and write the synthetic vault."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    generate(
        notes=args.notes,
        users=args.users,
        groups=args.groups,
        projects=args.projects,
        kinds=_parse_kinds(args.kinds),
        target_chunks_per_note=args.target_chunks_per_note,
        seed=args.seed,
        out=args.out,
    )
    print(f"wrote {args.notes} notes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

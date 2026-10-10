# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic synthetic chunk vectors, shared by the generator (#266), the
loader (#267) and the embedding stub (#268).

`synthetic_vector` turns any string key - a chunk's `"{note_id}:{ord}"`
identity, say - into a reproducible unit vector: the same key always yields
the same vector, in every process, without any of those three tools ever
talking to each other or to a real embedding model. `near_vector` builds a
vector correlated with one target key's vector but not identical to it - the
query side of "a marker query embeds to a vector close to its target chunk"
(#266's acceptance), the way a real embedding model relates a question to its
answer passage, not a byte-for-byte copy of it.

Unlike `loadtest.vector_index.iter_rows`'s centroid-plus-Gaussian-noise
clusters (ADR-0016 spike, #125), this module never mixes several keys'
signal into one vector - each key's vector depends only on its own hash, and
`near_vector` perturbs that one vector, not a centroid shared with other
keys. That sidesteps the spike's own finding
(`docs/research/vector-index.md` §0): at 1024 dimensions, a unit vector's
own per-component magnitude is `~1/sqrt(1024) ≈ 0.03`, far below the
spike's `noise=0.15`, so a *shared* centroid's signal washes out after
re-normalising and every row ends up indistinguishable from uniform random.
`near_vector`'s signal is the target vector itself, not a shared centroid
diluted across many points, so a much smaller `noise` already leaves a
comfortable margin - see its docstring.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence

__all__ = ["near_vector", "synthetic_vector"]

# At 1024 dimensions this leaves cosine(target, near_vector(target)) ≈ 0.36
# (see `near_vector`'s docstring for the derivation) - a random key's cosine
# to an unrelated key is 0 +/- ~1/sqrt(1024) ≈ 0.03, so the margin is roughly
# 10 standard deviations: the target ranks first against any number of
# random keys a load test could plausibly draw, not just on average.
_DEFAULT_NOISE = 0.08


def _seed_from_key(key: str) -> int:
    """A stable integer seed for `key`, independent of `PYTHONHASHSEED`.

    `random.Random(key)` would work too (the stdlib hashes a `str` seed
    internally), but through `hash()`'s own salt, randomised per process
    unless `PYTHONHASHSEED` is fixed - the opposite of "same key -> same
    vector in every process" this module exists for. SHA-256 has no such
    salt.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


def _normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def synthetic_vector(key: str, dimension: int) -> list[float]:
    """A deterministic, unit-length vector for `key`, at `dimension` components.

    The same `key`/`dimension` pair always yields the same vector, including
    across separate `python` invocations (the generator, the loader and the
    embedding stub each run in their own process) - `key` only ever needs to
    exist as a string (a chunk's `"{note_id}:{ord}"` identity, a note id, a
    query id, ...), never as an actual row, for its vector to be computed.
    """
    if dimension < 1:
        raise ValueError(f"dimension must be >= 1, got {dimension}")
    rng = random.Random(_seed_from_key(key))  # noqa: S311 - reproducible synthetic data, not a secret
    values = [rng.gauss(0.0, 1.0) for _ in range(dimension)]
    return _normalize(values)


def near_vector(key: str, dimension: int, noise: float = _DEFAULT_NOISE) -> list[float]:
    """A vector correlated with `synthetic_vector(key, dimension)`, not equal to it.

    Perturbs `key`'s own vector with Gaussian noise of standard deviation
    `noise` per component (seeded from `key` with a fixed suffix, so the
    perturbation itself is deterministic but independent of the draw
    `synthetic_vector` used for `key`'s own vector - reusing that draw would
    make the "noise" a scalar multiple of the vector itself, i.e. the exact
    same direction, cosine 1.0, not a realistic near-miss), then
    re-normalises.

    For a unit vector `v` in `dimension` dimensions perturbed by Gaussian
    noise `g` of per-component standard deviation `noise`,
    `cosine(v, normalize(v + noise*g)) ≈ 1 / sqrt(1 + noise**2 * dimension)`
    for large `dimension` (the noise vector's squared length concentrates
    around `noise**2 * dimension`, dominating the `O(noise)` cross term).
    The default `noise` keeps `noise**2 * dimension` small at 1024
    dimensions (≈ 6.55, cosine ≈ 0.36) - the opposite of
    `loadtest.vector_index.iter_rows`' `noise=0.15` against a *shared*
    centroid, which `docs/research/vector-index.md` §0 found washes out
    entirely at this many dimensions. A random, unrelated key's cosine to
    `key`'s own vector is `0 +/- ~1/sqrt(dimension)` (≈ 0.03 at 1024
    dimensions) - comfortably below 0.36, so `near_vector(key, ...)` ranks
    `key`'s own `synthetic_vector` above any number of random keys a load
    test could plausibly draw (see `tests/loadtest/test_generate.py`).
    """
    if dimension < 1:
        raise ValueError(f"dimension must be >= 1, got {dimension}")
    target = synthetic_vector(key, dimension)
    rng = random.Random(  # noqa: S311 - reproducible synthetic data, not a secret
        _seed_from_key(f"{key}\x00near_vector")
    )
    perturbed = [v + rng.gauss(0.0, noise) for v in target]
    return _normalize(perturbed)

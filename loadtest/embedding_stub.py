# SPDX-License-Identifier: AGPL-3.0-only
"""An OpenAI-compatible embedding stand-in for load tests (#268).

Hybrid search embeds the query text at request time
(`memory_manager.index.embeddings.OpenAICompatibleProvider`). Pointing a
load-test run's `EMBEDDING_URL` at this stub instead of a real embedding
model keeps that request/response round trip (and so its latency
contribution) in the run, but with vectors that line up with
`loadtest.generate`'s planted chunks (#266) and `loadtest.load`'s stored
embeddings (#267) - not a real model's, which would neither be
reproducible nor fast enough at this scale. `loadtest.vectors` is the
single source of truth both sides already share; this module adds no
vector math of its own, only the HTTP shape around it.

Serves `POST /v1/embeddings`, mirroring exactly what
`OpenAICompatibleProvider._embed_batch` sends and expects back:

- request: `{"model": ..., "input": [...]}`, optionally `"dimensions":
  ...` (sent whenever `EmbeddingConfig.dimensions` is set) - `--dimension`
  is only the fallback used when a request omits it.
- response: `{"data": [{"index": ..., "embedding": [...]}, ...]}`, one
  item per `input` entry, `index` matching its position in the request
  (the provider re-sorts by `index` before use, so request order does not
  need preserving, but this stub returns it in order anyway).

Per `input` text: `loadtest.vectors.near_vector(vector_key, dimension)` if
the text is a marker query from `--queries`' `queries.jsonl` (`vector_key`
is that query's planted chunk's `"{note_id}:{ord}"` identity, exactly as
`loadtest.load` stored it), else `loadtest.vectors.synthetic_vector(text,
dimension)` for any other text (chunk bodies at embed time, or a query
this run's generator did not plant a marker for). Built on Starlette/
uvicorn (already a dependency of the real server, ADR-0001) rather than a
second, stdlib-only HTTP stack, so the one request-handling path this
project already tests and runs in production is also the one a load test
points `EMBEDDING_URL` at.

Usage::

    python -m loadtest.embedding_stub --queries ./loadtest-vault/queries.jsonl \\
        --port 8090 --dimension 1024
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from loadtest.vectors import near_vector, synthetic_vector

__all__ = ["create_app", "load_query_vector_keys", "main"]

#: Matches `loadtest.load._CHUNK_VECTOR_DIMENSION` - the dimension a load
#: test actually stores, unless a request (or `--dimension`) says otherwise.
_DEFAULT_DIMENSION = 1024


def load_query_vector_keys(queries_path: Path) -> dict[str, str]:
    """Map each `queries.jsonl` entry's `query` text to its `vector_key`.

    `queries.jsonl` is `loadtest.generate`'s output (#266): one JSON object
    per line, each with a `query` string and a `vector_key` string (the
    planted chunk's `"{note_id}:{ord}"` identity). Query texts are unique
    by construction (`loadtest.generate._build_note` draws each from a
    41-word vocabulary plus a note-specific marker), so a plain `dict`
    never silently drops a collision in practice.
    """
    lines = queries_path.read_text(encoding="utf-8").splitlines()
    mapping: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        entry = json.loads(line)
        mapping[str(entry["query"])] = str(entry["vector_key"])
    return mapping


def _embed_one(text: str, query_vector_keys: Mapping[str, str], dimension: int) -> list[float]:
    """`near_vector` of the text's marker query target, else `synthetic_vector`."""
    vector_key = query_vector_keys.get(text)
    if vector_key is not None:
        return near_vector(vector_key, dimension)
    return synthetic_vector(text, dimension)


def create_app(
    query_vector_keys: Mapping[str, str],
    *,
    dimension: int = _DEFAULT_DIMENSION,
    latency_ms: float = 0.0,
) -> Starlette:
    """Build the stub app: `POST /v1/embeddings` plus `GET /healthz`.

    `latency_ms`, if set, delays each `/v1/embeddings` response by that
    many milliseconds (once per request, not per `input` entry) - a crude
    stand-in for a real provider's own latency, so a load test can still
    see request concurrency and timeout behaviour without waiting on an
    actual model.
    """

    async def embeddings(request: Request) -> JSONResponse:
        if latency_ms > 0:
            await asyncio.sleep(latency_ms / 1000)

        body: Any = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        texts = body.get("input")
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            return JSONResponse({"error": "'input' must be a list of strings"}, status_code=400)

        requested_dimension = body.get("dimensions", dimension)
        if not isinstance(requested_dimension, int) or requested_dimension < 1:
            return JSONResponse(
                {"error": "'dimensions' must be a positive integer"}, status_code=400
            )

        data = [
            {"index": index, "embedding": _embed_one(text, query_vector_keys, requested_dimension)}
            for index, text in enumerate(texts)
        ]
        return JSONResponse({"data": data, "model": body.get("model")})

    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/v1/embeddings", embeddings, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.embedding_stub",
        description="serve a deterministic, OpenAI-compatible /v1/embeddings endpoint (#268)",
    )
    parser.add_argument(
        "--queries", type=Path, required=True, help="a loadtest.generate queries.jsonl file"
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind")
    parser.add_argument("--port", type=int, required=True, help="port to bind")
    parser.add_argument(
        "--dimension",
        type=int,
        default=_DEFAULT_DIMENSION,
        help="embedding dimension used when a request omits 'dimensions'",
    )
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=0.0,
        help="artificial per-request delay, modelling a real provider's own latency",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and serve the stub until interrupted."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    query_vector_keys = load_query_vector_keys(args.queries)
    app = create_app(query_vector_keys, dimension=args.dimension, latency_ms=args.latency_ms)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

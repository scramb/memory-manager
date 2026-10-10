# SPDX-License-Identifier: AGPL-3.0-only
"""A scripted, OpenAI-compatible chat model for the Open WebUI E2E stack (#141, WP-42).

Open WebUI's native MCP tool calling needs a chat model that actually *calls*
`memory_*` tools, deterministically, so a test can assert on what came back
from memory-manager rather than on whatever a real LLM felt like doing this
run (ADR-0011's "note content is data, not instructions" guardrail applies
here too: this stub never looks at message *content* to decide what to do -
only the caller-supplied script and the turn's position in the conversation
drive its replies). `deploy/openwebui/docker-compose.yml` points Open
WebUI's `OPENAI_API_BASE_URLS` at this stub instead of a real provider.

Deliberately self-contained (see the package docstring): this module is
mounted read-only into the `memory-manager` image and started by file path
(`python3 /stubs/scripted_model.py ...`), where `tests.` and `loadtest.`
are not importable. Only the standard library plus Starlette/uvicorn are
used - both already transitive runtime dependencies of `mcp` (ADR-0001), the
same dependency the production server and `loadtest/embedding_stub.py`
already rely on.

Endpoints:

- `GET /v1/models`: one fixed model (`--model-id`, default
  `scripted-model`), the shape Open WebUI's model list expects.
- `POST /v1/chat/completions`: replies for one "turn" of the script, chosen
  *statelessly* from the request itself - the number of `"role": "assistant"`
  entries already in `messages` (turn 0 before any assistant reply exists,
  turn 1 once Open WebUI has appended the first one, and so on). No session
  state is kept server-side, so concurrent chats and process restarts never
  disturb which turn a given request gets - the request alone carries
  everything needed to answer it the same way every time.
- `GET /healthz`: liveness, matching `loadtest/embedding_stub.py`'s shape.

Script format (`--script`/`SCRIPTED_MODEL_SCRIPT`, a JSON file): a list of
turn objects, each either

    {"tool_calls": [{"name": "memory_search", "arguments": {"query": "..."}}]}

or

    {"content": "a final assistant reply"}

A turn index past the end of the script, no `--script` at all, or (#314) a
`--script` path whose file does not exist *at request time*, falls back to
one fixed text reply (`_DEFAULT_TURN`) - the "without script" case
the docs/TASKS.md Definition of Done for #141 only needs the shape of, not
any particular scripted conversation (that is #144/#150's job).

With `--script`, the file is re-read on **every** `/v1/chat/completions`
request (#314) rather than once at startup: a live run
(`deploy/openwebui/docker-compose.yml`) mounts `tests/e2e/stubs` read-only
and starts this stub before a test's own script exists on disk, so the
file can appear - or change - after the process is already serving
requests. Turn selection still only looks at the request's own message
list (see `_turn_index` below), so re-reading does not reintroduce session
state: two requests carrying the same `messages` get the same answer
regardless of when either one reads the file, as long as the file itself
has not changed between them.

Both `"stream": false` and `"stream": true` are served. Streaming follows
the OpenAI `chat.completion.chunk` shape: a tool-call turn emits one delta
per tool call (`delta.tool_calls[0]` with `index`, `id`, `type` and
`function.name`/`function.arguments`), a text turn emits the content in one
delta, both end with an empty-delta chunk carrying `finish_reason`
(`"tool_calls"` or `"stop"`) and then the literal `data: [DONE]` line Open
WebUI's SSE client watches for.

Usage::

    python3 scripted_model.py --port 8091 --script ./script.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

__all__ = ["create_app", "load_script", "main"]

#: Used whenever a turn index has no scripted entry (no `--script` at all,
#: or a conversation longer than the script) - a plain text reply, never a
#: tool call, so a missing/short script degrades to "the model just talks"
#: rather than silently stalling a tool-calling test.
_DEFAULT_TURN: dict[str, Any] = {"content": "This is a scripted reply."}

_DEFAULT_MODEL_ID = "scripted-model"


def load_script(path: Path) -> list[dict[str, Any]]:
    """Reads a `--script` JSON file: a list of turn objects (see module docstring)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"script {path} must be a JSON array of turn objects")
    return [dict(turn) for turn in data]


def _turn_index(messages: Sequence[Mapping[str, Any]]) -> int:
    """How many assistant turns this conversation already has - stateless by
    construction (see module docstring): the request body is the only input."""
    return sum(1 for message in messages if message.get("role") == "assistant")


def _turn_for(script: Sequence[Mapping[str, Any]], index: int) -> Mapping[str, Any]:
    if 0 <= index < len(script):
        return script[index]
    return _DEFAULT_TURN


def _current_script(
    static_script: Sequence[Mapping[str, Any]], script_path: Path | None
) -> Sequence[Mapping[str, Any]]:
    """The script for *this* request: re-read from `script_path` every time
    (module docstring, #314) when one was given, falling back to no script
    (-> `_DEFAULT_TURN`, same as no `--script` at all) while the file does
    not exist yet. `static_script` is only used when no path was given at
    all, keeping the in-process tests (`create_app(script)`) unaffected."""
    if script_path is None:
        return static_script
    try:
        return load_script(script_path)
    except FileNotFoundError:
        return ()


def _tool_call_message(turn_index: int, tool_calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{turn_index}_{call_index}",
                "type": "function",
                "function": {
                    "name": str(call["name"]),
                    "arguments": json.dumps(call.get("arguments", {})),
                },
            }
            for call_index, call in enumerate(tool_calls)
        ],
    }


def _chunk(
    completion_id: str,
    created: int,
    model_id: str,
    delta: Mapping[str, Any],
    finish_reason: str | None,
) -> str:
    body = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_id,
        "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(body)}\n\n"


async def _stream_tool_call_turn(
    completion_id: str,
    created: int,
    model_id: str,
    turn_index: int,
    tool_calls: Sequence[Mapping[str, Any]],
) -> AsyncIterator[str]:
    yield _chunk(completion_id, created, model_id, {"role": "assistant"}, None)
    for call_index, call in enumerate(tool_calls):
        delta = {
            "tool_calls": [
                {
                    "index": call_index,
                    "id": f"call_{turn_index}_{call_index}",
                    "type": "function",
                    "function": {
                        "name": str(call["name"]),
                        "arguments": json.dumps(call.get("arguments", {})),
                    },
                }
            ]
        }
        yield _chunk(completion_id, created, model_id, delta, None)
    yield _chunk(completion_id, created, model_id, {}, "tool_calls")
    yield "data: [DONE]\n\n"


async def _stream_text_turn(
    completion_id: str, created: int, model_id: str, content: str
) -> AsyncIterator[str]:
    yield _chunk(completion_id, created, model_id, {"role": "assistant"}, None)
    yield _chunk(completion_id, created, model_id, {"content": content}, None)
    yield _chunk(completion_id, created, model_id, {}, "stop")
    yield "data: [DONE]\n\n"


def create_app(
    script: Sequence[Mapping[str, Any]] = (),
    *,
    model_id: str = _DEFAULT_MODEL_ID,
    script_path: Path | None = None,
) -> Starlette:
    """Builds the stub: `GET /v1/models`, `POST /v1/chat/completions`, `GET /healthz`.

    `script` is a fixed, in-process script (what the unit tests below use).
    `script_path`, when given, takes over instead and is re-read on every
    `/v1/chat/completions` request (#314, see the module docstring) -
    `main()` always passes this for the `--script`/`SCRIPTED_MODEL_SCRIPT`
    CLI path; `script` stays the fixed-list seam for tests."""

    async def models(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_id, "object": "model", "created": 0, "owned_by": "scripted-model"}
                ],
            }
        )

    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        body: Any = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        messages = body.get("messages")
        if not isinstance(messages, list):
            return JSONResponse({"error": "'messages' must be a list"}, status_code=400)
        stream = bool(body.get("stream", False))

        turn_index = _turn_index(messages)
        turn = _turn_for(_current_script(script, script_path), turn_index)
        tool_calls = turn.get("tool_calls")

        completion_id = f"chatcmpl-{turn_index}"
        created = int(time.time())

        if stream:
            if tool_calls:
                generator = _stream_tool_call_turn(
                    completion_id, created, model_id, turn_index, tool_calls
                )
            else:
                generator = _stream_text_turn(
                    completion_id, created, model_id, str(turn.get("content", ""))
                )
            return StreamingResponse(generator, media_type="text/event-stream")

        if tool_calls:
            message = _tool_call_message(turn_index, tool_calls)
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": str(turn.get("content", ""))}
            finish_reason = "stop"

        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )


def _default_script_path() -> Path | None:
    value = os.environ.get("SCRIPTED_MODEL_SCRIPT")
    return Path(value) if value else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripted_model",
        description="serve a deterministic, OpenAI-compatible chat model for Open WebUI E2E (#141)",
    )
    # Defaults to loopback, like `loadtest/embedding_stub.py` - the container
    # deployment (`deploy/openwebui/docker-compose.yml`) passes `--host
    # 0.0.0.0` explicitly, the same way `tests/mock_idp/Containerfile` does.
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind")
    parser.add_argument("--port", type=int, required=True, help="port to bind")
    parser.add_argument(
        "--model-id",
        default=_DEFAULT_MODEL_ID,
        help="the model id served under /v1/models and /v1/chat/completions",
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=_default_script_path(),
        help="a JSON turn-object file (module docstring); SCRIPTED_MODEL_SCRIPT as fallback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and serve the stub until interrupted."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    app = create_app(model_id=args.model_id, script_path=args.script)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

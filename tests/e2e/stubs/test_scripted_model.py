# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the scripted, OpenAI-compatible chat model stub (#141, WP-42).

Same in-process ASGI seam as `tests/loadtest/test_embedding_stub.py`:
`httpx.ASGITransport` talks to the Starlette app directly, no socket - the
module itself stays importable as `e2e.stubs.scripted_model` here (pytest's
own collection, `tests/` itself on `sys.path` - same convention as
`tests/mock_idp_fixtures.py`'s `from mock_idp.app import create_app`), even
though the container deployment in `deploy/openwebui/docker-compose.yml`
runs it by file path instead (see the module docstring for why).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e.stubs.scripted_model import create_app, load_script

_MODEL_ID = "scripted-model"


def _make_client(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://stub")


class TestModels:
    async def test_lists_the_one_fixed_model(self) -> None:
        app = create_app()
        async with _make_client(app) as client:
            response = await client.get("/v1/models")

        assert response.status_code == 200
        body = response.json()
        assert body["data"] == [
            {"id": _MODEL_ID, "object": "model", "created": 0, "owned_by": "scripted-model"}
        ]

    async def test_model_id_is_configurable(self) -> None:
        app = create_app(model_id="other-model")
        async with _make_client(app) as client:
            response = await client.get("/v1/models")

        assert response.json()["data"][0]["id"] == "other-model"


class TestHealthz:
    async def test_returns_200(self) -> None:
        app = create_app()
        async with _make_client(app) as client:
            response = await client.get("/healthz")

        assert response.status_code == 200


class TestLoadScript:
    def test_reads_a_list_of_turn_objects(self, tmp_path: Path) -> None:
        script = [{"content": "hi"}, {"tool_calls": [{"name": "memory_search", "arguments": {}}]}]
        path = tmp_path / "script.json"
        path.write_text(json.dumps(script), encoding="utf-8")

        assert load_script(path) == script

    def test_rejects_a_non_list_document(self, tmp_path: Path) -> None:
        path = tmp_path / "script.json"
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        with pytest.raises(ValueError, match="must be a JSON array"):
            load_script(path)


def _no_assistant_turns() -> list[dict[str, str]]:
    return [{"role": "user", "content": "hello"}]


def _one_assistant_turn() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "and then?"},
    ]


class TestChatCompletionsNonStreaming:
    async def test_without_a_script_gives_the_fixed_default_reply(self) -> None:
        app = create_app()
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )

        body = response.json()
        message = body["choices"][0]["message"]
        assert message == {"role": "assistant", "content": "This is a scripted reply."}
        assert body["choices"][0]["finish_reason"] == "stop"

    async def test_selects_the_turn_by_assistant_message_count(self) -> None:
        script = [{"content": "turn zero"}, {"content": "turn one"}]
        app = create_app(script)
        async with _make_client(app) as client:
            first = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )
            second = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _one_assistant_turn()}
            )

        assert first.json()["choices"][0]["message"]["content"] == "turn zero"
        assert second.json()["choices"][0]["message"]["content"] == "turn one"

    async def test_turn_selection_is_stateless_and_deterministic(self) -> None:
        script = [{"content": "turn zero"}, {"content": "turn one"}]
        app = create_app(script)
        async with _make_client(app) as client:
            first = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _one_assistant_turn()}
            )
            second = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _one_assistant_turn()}
            )

        assert first.json() == second.json()

    async def test_a_tool_call_turn_returns_tool_calls_and_no_content(self) -> None:
        script = [{"tool_calls": [{"name": "memory_search", "arguments": {"query": "x"}}]}]
        app = create_app(script)
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )

        body = response.json()
        choice = body["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] is None
        tool_call = choice["message"]["tool_calls"][0]
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "memory_search"
        assert json.loads(tool_call["function"]["arguments"]) == {"query": "x"}

    async def test_a_turn_past_the_end_of_the_script_falls_back_to_the_default(self) -> None:
        app = create_app([{"content": "only turn"}])
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _one_assistant_turn()}
            )

        assert response.json()["choices"][0]["message"]["content"] == "This is a scripted reply."


class TestChatCompletionsScriptPath:
    """`--script`/`script_path` (#314): re-read on every request, not once at
    startup - a live run may mount the script file after the stub is
    already serving, or change it mid-run."""

    async def test_reads_the_script_file_fresh_on_every_request(self, tmp_path: Path) -> None:
        path = tmp_path / "script.json"
        path.write_text(json.dumps([{"content": "first version"}]), encoding="utf-8")
        app = create_app(script_path=path)

        async with _make_client(app) as client:
            first = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )
            assert first.json()["choices"][0]["message"]["content"] == "first version"

            path.write_text(json.dumps([{"content": "second version"}]), encoding="utf-8")
            second = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )
            assert second.json()["choices"][0]["message"]["content"] == "second version"

    async def test_falls_back_to_the_default_turn_while_the_file_does_not_exist_yet(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "not-written-yet.json"
        app = create_app(script_path=path)

        async with _make_client(app) as client:
            before = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )
            assert before.json()["choices"][0]["message"]["content"] == "This is a scripted reply."

            path.write_text(json.dumps([{"content": "now it exists"}]), encoding="utf-8")
            after = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )
            assert after.json()["choices"][0]["message"]["content"] == "now it exists"

    async def test_script_path_takes_over_from_the_fixed_script_argument(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "script.json"
        path.write_text(json.dumps([{"content": "from the file"}]), encoding="utf-8")
        app = create_app([{"content": "from the fixed list"}], script_path=path)

        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions", json={"model": _MODEL_ID, "messages": _no_assistant_turns()}
            )

        assert response.json()["choices"][0]["message"]["content"] == "from the file"


def _parse_sse_events(raw: str) -> list[Any]:
    """`[DONE]` becomes `None`, every other `data: ...` line is JSON-decoded."""
    events: list[Any] = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ")
        events.append(None if payload == "[DONE]" else json.loads(payload))
    return events


class TestChatCompletionsStreaming:
    async def test_a_text_turn_streams_role_then_content_then_stop_then_done(self) -> None:
        app = create_app([{"content": "streamed hello"}])
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": _MODEL_ID, "messages": _no_assistant_turns(), "stream": True},
            )

        assert response.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse_events(response.text)

        assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert events[1]["choices"][0]["delta"] == {"content": "streamed hello"}
        assert events[2]["choices"][0]["delta"] == {}
        assert events[2]["choices"][0]["finish_reason"] == "stop"
        assert events[3] is None  # the "[DONE]" sentinel

    async def test_a_tool_call_turn_streams_a_tool_calls_delta_then_finish_reason(self) -> None:
        script = [{"tool_calls": [{"name": "memory_search", "arguments": {"query": "x"}}]}]
        app = create_app(script)
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": _MODEL_ID, "messages": _no_assistant_turns(), "stream": True},
            )

        events = _parse_sse_events(response.text)

        tool_call_events = [
            event
            for event in events
            if event is not None and event["choices"][0]["delta"].get("tool_calls")
        ]
        assert len(tool_call_events) == 1
        delta_call = tool_call_events[0]["choices"][0]["delta"]["tool_calls"][0]
        assert delta_call == {
            "index": 0,
            "id": "call_0_0",
            "type": "function",
            "function": {"name": "memory_search", "arguments": json.dumps({"query": "x"})},
        }

        final_event = events[-2]
        assert final_event is not None
        assert final_event["choices"][0]["finish_reason"] == "tool_calls"
        assert events[-1] is None  # the "[DONE]" sentinel

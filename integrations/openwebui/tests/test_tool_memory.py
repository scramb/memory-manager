# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the Open WebUI `Tools` class (#149, ADR-0011, ADR-0009).

Every test name below starts with `test_tool_` on purpose (`Fertig wenn`): this lets the
Open WebUI suite be run on its own as `pytest integrations/openwebui/tests -k tool`,
distinct from `test_filter_memory.py`'s `test_inlet_*`/`test_outlet_*` tests.

Deliberately no `conftest.py` next to this file, for the same reason
`test_filter_memory.py` has none: `tool_memory.py` has nothing other tests need to
share, and the self-containment test below relies on it having no sibling module of its
own to depend on in the first place.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import logging
import sys
import types
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from integrations.openwebui import tool_memory
from integrations.openwebui.tool_memory import Tools
from memory_manager.mcp.instructions_generated import TOOL_DATA_SENTENCE

_SERVER_URL = "https://memory.example.org/mcp"
_TOKEN = "mm_personal-test-token"  # noqa: S105 - a fake value shaped like one, not a credential

_SOURCE_PATH = Path(__file__).resolve().parents[1] / "tool_memory.py"
_FILTER_SOURCE_PATH = Path(__file__).resolve().parents[1] / "filter_memory.py"

# Helpers copied unchanged from `filter_memory.py` into `tool_memory.py` (module
# docstring, step 3 of the task contract) - drifting from their source is a bug, not a
# deliberate adaptation, unlike `_call_tool` which is allowed to differ.
_UNCHANGED_HELPER_NAMES = ("_sse_events", "_extract_message", "_ToolCallResult", "_result_text")


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Mirrors `test_filter_memory.py`'s own `_install_transport`: the tool builds its
    own `httpx.AsyncClient`, so the transport is injected by patching the module
    attribute it calls through."""
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )
    return requests


def _dispatcher(
    responses: dict[str, list[httpx.Response]],
) -> Callable[[httpx.Request], httpx.Response]:
    """A fake MCP server: `tools/call`s are answered in the order queued per tool name
    (`responses[name]`). Mirrors `test_filter_memory.py`'s own `_dispatcher`."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        name = payload["params"]["name"]
        queue = responses.get(name)
        if not queue:
            raise AssertionError(f"unexpected tools/call to {name!r}: {payload['params']}")
        return queue.pop(0)

    return handler


def _call_names(requests: list[httpx.Request]) -> list[str]:
    return [json.loads(request.content)["params"]["name"] for request in requests]


def _call_arguments(requests: list[httpx.Request], index: int = 0) -> dict[str, object]:
    payload = json.loads(requests[index].content)
    arguments = payload["params"]["arguments"]
    assert isinstance(arguments, dict)
    return arguments


def _rpc_ok(structured: object) -> httpx.Response:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": False,
            "structuredContent": structured,
            "content": [{"type": "text", "text": json.dumps(structured)}],
        },
    }
    return httpx.Response(200, json=message)


def _rpc_error(text: str, structured: dict[str, object] | None = None) -> httpx.Response:
    result: dict[str, object] = {"isError": True, "content": [{"type": "text", "text": text}]}
    if structured is not None:
        result["structuredContent"] = structured
    message = {"jsonrpc": "2.0", "id": 1, "result": result}
    return httpx.Response(200, json=message)


def _search_result(hits: list[dict[str, object]]) -> httpx.Response:
    return _rpc_ok({"results": hits, "mode": "git"})


def _read_result(items: list[dict[str, object]]) -> httpx.Response:
    return _rpc_ok({"result": items})


def _write_result(path: str, *, version: str = "v1") -> httpx.Response:
    return _rpc_ok(
        {"path": path, "id": "01J00000000000000000000000", "version": version, "commit": "c1"}
    )


def _version_conflict() -> httpx.Response:
    return _rpc_error(
        "'me/fact/tea.md' has moved to version v9, if_version is stale",
        structured={
            "error": "VersionConflict",
            "message": "'me/fact/tea.md' has moved to version v9, if_version is stale",
            "path": "me/fact/tea.md",
            "current_version": "v9",
            "current_content": "---\ntitle: Tea\n---\nI like green tea.\n",
        },
    )


def _user(token: str = _TOKEN) -> dict[str, Any]:
    return {"id": "user-1", "valves": Tools.UserValves(token=token)}


def _new_tool(**valve_overrides: Any) -> Tools:
    the_tool = Tools()
    the_tool.valves = Tools.Valves(server_url=_SERVER_URL, **valve_overrides)
    return the_tool


# --- memory_search / memory_read / memory_write: arguments and successful results ------


async def test_tool_memory_search_sends_query_namespaces_and_limit_and_parses_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = {
        "id": "01J00000000000000000000001",
        "path": "personal/fact/color.md",
        "title": "Color",
        "description": "Favorite color",
        "type": "fact",
        "tags": [],
        "snippet": "blue",
        "score": 0.9,
        "namespace_kind": None,
    }
    requests = _install_transport(monkeypatch, lambda request: _search_result([hit]))
    the_tool = _new_tool()

    result = await the_tool.memory_search(
        "favorite color", namespaces=["personal"], limit=3, __user__=_user()
    )

    assert _call_names(requests) == ["memory_search"]
    assert _call_arguments(requests) == {
        "query": "favorite color",
        "namespaces": ["personal"],
        "limit": 3,
    }
    assert json.loads(result) == {"results": [hit], "mode": "git"}


async def test_tool_memory_search_omits_namespaces_and_limit_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_transport(monkeypatch, lambda request: _search_result([]))
    the_tool = _new_tool()

    await the_tool.memory_search("favorite color", __user__=_user())

    arguments = _call_arguments(requests)
    assert "namespaces" not in arguments
    assert "limit" not in arguments


async def test_tool_memory_read_sends_items_verbatim_and_unwraps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: list[dict[str, object]] = [
        {
            "path": "personal/fact/color.md",
            "id": "01J00000000000000000000001",
            "version": "v1",
            "content": "---\ntitle: Color\n---\nBlue.\n",
        }
    ]
    requests = _install_transport(monkeypatch, lambda request: _read_result(items))
    the_tool = _new_tool()

    result = await the_tool.memory_read(["personal/fact/color.md"], __user__=_user())

    assert _call_arguments(requests) == {"items": ["personal/fact/color.md"]}
    assert json.loads(result) == items


async def test_tool_memory_write_sends_if_version_verbatim_and_parses_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_transport(
        monkeypatch, lambda request: _write_result("me/fact/tea.md", version="v2")
    )
    the_tool = _new_tool()

    result = await the_tool.memory_write(
        "me/fact/tea.md", "---\ntitle: Tea\n---\nI like tea.\n", "v1", __user__=_user()
    )

    assert _call_arguments(requests)["if_version"] == "v1"
    assert json.loads(result)["version"] == "v2"


def test_tool_memory_write_if_version_has_no_default() -> None:
    parameter = inspect.signature(Tools.memory_write).parameters["if_version"]
    assert parameter.default is inspect.Parameter.empty


async def test_tool_memory_write_version_conflict_returns_current_state_with_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_transport(monkeypatch, _dispatcher({"memory_write": [_version_conflict()]}))
    the_tool = _new_tool()

    result = await the_tool.memory_write(
        "me/fact/tea.md", "---\ntitle: Tea\n---\nI like tea.\n", "v1", __user__=_user()
    )

    assert _call_names(requests) == ["memory_write"]
    parsed = json.loads(result)
    assert parsed["error"] == "VersionConflict"
    assert parsed["current_version"] == "v9"
    assert parsed["current_content"] == "---\ntitle: Tea\n---\nI like green tea.\n"


async def test_tool_memory_search_iserror_without_structured_content_returns_server_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(
        monkeypatch,
        lambda request: _rpc_error("Error executing tool memory_search: token lacks memory:read"),
    )
    the_tool = _new_tool()

    result = await the_tool.memory_search("favorite color", __user__=_user())

    assert result == "Error executing tool memory_search: token lacks memory:read"


# --- missing configuration: never a request --------------------------------------------


async def test_tool_memory_without_a_token_sends_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_transport(monkeypatch, _dispatcher({}))
    the_tool = _new_tool()

    result = await the_tool.memory_search("favorite color", __user__=_user(token=""))

    assert requests == []
    assert result == tool_memory._NO_TOKEN_MESSAGE


async def test_tool_memory_without_user_valves_sends_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_transport(monkeypatch, _dispatcher({}))
    the_tool = _new_tool()

    result = await the_tool.memory_search("favorite color", __user__=None)

    assert requests == []
    assert result == tool_memory._NO_TOKEN_MESSAGE


async def test_tool_memory_without_a_server_url_sends_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_transport(monkeypatch, _dispatcher({}))
    the_tool = Tools()

    result = await the_tool.memory_search("favorite color", __user__=_user())

    assert requests == []
    assert result == tool_memory._NO_SERVER_MESSAGE


# --- transport failures: a message, never an exception ---------------------------------


async def test_tool_memory_connect_error_is_a_message_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, handler)
    the_tool = _new_tool()

    result = await the_tool.memory_search("favorite color", __user__=_user())

    assert result == tool_memory._TRANSPORT_ERROR_MESSAGE


async def test_tool_memory_401_is_a_message_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(401, text="unauthorized"))
    the_tool = _new_tool()

    result = await the_tool.memory_search("favorite color", __user__=_user())

    assert result == tool_memory._UNAUTHORIZED_MESSAGE


async def test_tool_memory_403_is_a_message_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(403, text="forbidden"))
    the_tool = _new_tool()

    result = await the_tool.memory_read(["me/fact/tea.md"], __user__=_user())

    assert result == tool_memory._UNAUTHORIZED_MESSAGE


async def test_tool_memory_broken_sse_is_a_message_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = "event: message\ndata: {not valid json\n\n"
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=broken, headers={"content-type": "text/event-stream"}
        ),
    )
    the_tool = _new_tool()

    result = await the_tool.memory_write(
        "me/fact/tea.md", "---\ntitle: Tea\n---\nI like tea.\n", "new", __user__=_user()
    )

    assert result == tool_memory._TRANSPORT_ERROR_MESSAGE


# --- the token never leaks ---------------------------------------------------------------


async def test_tool_memory_never_logs_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_transport(monkeypatch, lambda request: _search_result([]))
    the_tool = _new_tool()

    with caplog.at_level(logging.DEBUG):
        await the_tool.memory_search("favorite color", __user__=_user())

    for record in caplog.records:
        assert _TOKEN not in record.getMessage()


async def test_tool_memory_never_returns_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(401, text=_TOKEN))
    the_tool = _new_tool()

    result = await the_tool.memory_search("favorite color", __user__=_user())

    assert _TOKEN not in result


# --- docstrings: the tool-data sentence, verbatim, before the first :param -------------


def test_tool_memory_tool_data_sentence_matches_the_generated_constant() -> None:
    assert tool_memory._TOOL_DATA_SENTENCE == TOOL_DATA_SENTENCE


@pytest.mark.parametrize("name", ["memory_search", "memory_read", "memory_write"])
def test_tool_memory_docstring_contains_the_tool_data_sentence_before_the_first_param(
    name: str,
) -> None:
    doc = getattr(Tools, name).__doc__
    assert doc is not None
    before_param = doc.split(":param", 1)[0]
    assert TOOL_DATA_SENTENCE in before_param


# --- self-containment, the exec() load path, and the three-tool surface ----------------


async def test_tool_memory_module_is_self_contained_with_only_allowed_imports() -> None:
    """Mirrors how Open WebUI actually loads this file - `exec()` into a fresh module,
    no package, no siblings (`load_tool_module_by_id`,
    `backend/open_webui/utils/plugin.py:206-250` at v0.11.4) - and checks, via the AST
    rather than by trying every environment, that it imports nothing beyond the
    standard library, httpx and pydantic. Also checks the two things that load path
    relies on beyond the import: the public surface is exactly the three tools
    (`get_functions_from_tool`), and every method's type hints resolve
    (`get_type_hints`, used by `convert_function_to_pydantic_model`)."""
    source = _SOURCE_PATH.read_text(encoding="utf-8")

    allowed = set(sys.stdlib_module_names) | {"httpx", "pydantic"}
    tree = ast.parse(source, filename=str(_SOURCE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed, f"disallowed import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import is possible: there is no package"
            assert node.module is not None
            root = node.module.split(".")[0]
            assert root in allowed, f"disallowed import: {node.module}"

    module_name = "tool_memory_self_containment_check"
    module = types.ModuleType(module_name)
    # Open WebUI registers the module under `sys.modules` *before* `exec()`
    # (`utils/plugin.py`) - pydantic's forward-ref resolution for `Valves`/`UserValves`
    # (this file uses `from __future__ import annotations`) looks the module back up
    # there by name, so this step is not optional scaffolding but part of what makes
    # the real loader work.
    sys.modules[module_name] = module
    try:
        exec(source, module.__dict__)  # noqa: S102 - mirrors Open WebUI's own loader, see above

        assert hasattr(module, "Tools")
        loaded_tool = module.Tools()
        assert isinstance(loaded_tool, module.Tools)

        public_callables = {
            name
            for name in dir(loaded_tool)
            if callable(getattr(loaded_tool, name))
            and not name.startswith("_")
            and not inspect.isclass(getattr(loaded_tool, name))
        }
        assert public_callables == {"memory_search", "memory_read", "memory_write"}

        for name in public_callables:
            typing.get_type_hints(getattr(module.Tools, name))
    finally:
        del sys.modules[module_name]


# --- drift: the helpers copied unchanged from filter_memory.py stay unchanged ----------


def _top_level_node(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"no top-level definition named {name!r}")


def _without_docstring(node: ast.AST) -> ast.AST:
    """`node` with its own leading docstring statement dropped, so two definitions that
    differ only in an explanatory comment still compare equal - the drift check this
    enables is about the *logic* staying byte-for-byte identical, not the prose
    around it."""
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    node.body = body
    return node


@pytest.mark.parametrize("name", _UNCHANGED_HELPER_NAMES)
def test_tool_memory_copied_helper_matches_filter_memory_unchanged(name: str) -> None:
    tool_tree = ast.parse(_SOURCE_PATH.read_text(encoding="utf-8"), filename=str(_SOURCE_PATH))
    filter_tree = ast.parse(
        _FILTER_SOURCE_PATH.read_text(encoding="utf-8"), filename=str(_FILTER_SOURCE_PATH)
    )
    tool_node = _without_docstring(_top_level_node(tool_tree, name))
    filter_node = _without_docstring(_top_level_node(filter_tree, name))
    assert ast.dump(tool_node) == ast.dump(filter_node), (
        f"{name} drifted from filter_memory.py's own copy"
    )

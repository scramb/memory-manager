# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `compat/lint.py`'s schema linter and the `compat lint` CLI (#133, ADR-0010).

Each rule gets its own negative test against a synthetic `Tool`/`Profile` pair, injected
straight into `cli._run_compat_lint_command` or `compat.lint.check_tools` - never the real
server, which stays reserved for the one test below that every tool and every registered
profile today are expected to pass cleanly.
"""

from __future__ import annotations

from typing import Any

import mcp_types
import pytest

from memory_manager import cli
from memory_manager.compat.lint import check_tools
from memory_manager.compat.profiles import Profile

# A complete, valid `ToolAnnotations` - every helper below gets one by default (the shape
# `mcp/server.py`'s own `_READ_TOOL_ANNOTATIONS`/`_WRITE_TOOL_ANNOTATIONS` have), so a test
# that wants rule (f)'s violation passes `annotations=None` explicitly rather than every
# other test having to opt into a complete one.
_FULL_ANNOTATIONS = mcp_types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _tool(
    *,
    name: str = "ok_tool",
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    annotations: mcp_types.ToolAnnotations | None = _FULL_ANNOTATIONS,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        input_schema=input_schema
        if input_schema is not None
        else {"type": "object", "properties": {}},
        annotations=annotations,
    )


def _profile(
    *,
    name: str = "synthetic",
    max_instructions_chars: int | None = None,
    max_tool_description_chars: int | None = None,
    result_budget_chars: int | None = None,
    result_budget_tokens: int | None = None,
    max_tool_name_chars: int | None = None,
    tool_name_prefix_chars: int | None = None,
    disallowed_schema_keywords: frozenset[str] = frozenset(),
    max_tools: int | None = None,
) -> Profile:
    return Profile(
        name=name,
        delivery_mode="full",
        max_instructions_chars=max_instructions_chars,
        max_tool_description_chars=max_tool_description_chars,
        result_budget_chars=result_budget_chars,
        result_budget_tokens=result_budget_tokens,
        max_tool_name_chars=max_tool_name_chars,
        tool_name_prefix_chars=tool_name_prefix_chars,
        disallowed_schema_keywords=disallowed_schema_keywords,
        max_tools=max_tools,
    )


def test_the_real_server_passes_every_registered_profile() -> None:
    assert cli.main(["compat", "lint"]) == 0


def test_bare_compat_without_a_subcommand_is_a_usage_error() -> None:
    assert cli.main(["compat"]) == 2


def test_a_clean_tool_violates_nothing() -> None:
    assert check_tools([_tool()], [_profile()]) == []


def test_name_charset_violation_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    tool = _tool(name="bad name!")
    profile = _profile()

    exit_code = cli._run_compat_lint_command(tools=[tool], profiles=[profile])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "synthetic" in out
    assert "bad name!" in out
    assert "name_charset" in out


def test_name_length_counts_the_documented_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    tool = _tool(name="x" * 10)
    profile = _profile(max_tool_name_chars=12, tool_name_prefix_chars=5)

    exit_code = cli._run_compat_lint_command(tools=[tool], profiles=[profile])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "synthetic" in out
    assert tool.name in out
    assert "name_length" in out


def test_name_length_without_a_documented_prefix_checks_the_bare_name_only() -> None:
    tool = _tool(name="x" * 10)
    # 10 chars, no documented prefix: fits a 10-char cap (prefix counted as 0, not
    # "unlimited" and not some unknown positive value).
    assert check_tools([tool], [_profile(max_tool_name_chars=10)]) == []
    # Still flags a name that is too long on its own, with no prefix in play at all.
    violations = check_tools([tool], [_profile(max_tool_name_chars=9)])
    assert [v.rule for v in violations] == ["name_length"]


def test_description_length_violation_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    tool = _tool(description="d" * 50)
    profile = _profile(max_tool_description_chars=10)

    exit_code = cli._run_compat_lint_command(tools=[tool], profiles=[profile])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "synthetic" in out
    assert tool.name in out
    assert "description_length" in out


def test_tool_count_violation_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    tools = [_tool(name=f"tool_{i}") for i in range(3)]
    profile = _profile(max_tools=2)

    exit_code = cli._run_compat_lint_command(tools=tools, profiles=[profile])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "synthetic" in out
    assert "tool_count" in out


def test_missing_annotations_violation_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    tool = _tool(annotations=None)
    profile = _profile()

    exit_code = cli._run_compat_lint_command(tools=[tool], profiles=[profile])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "synthetic" in out
    assert tool.name in out
    assert "annotations_present" in out


def test_disallowed_schema_keyword_at_a_keyword_position_is_a_violation() -> None:
    tool = _tool(input_schema={"type": "object", "properties": {}, "$ref": "#/foo"})
    profile = _profile(disallowed_schema_keywords=frozenset({"$ref"}))

    violations = check_tools([tool], [profile])

    assert [(v.rule, v.value) for v in violations] == [("schema_keyword", "$ref")]


def test_disallowed_keyword_nested_under_allof_is_still_found() -> None:
    tool = _tool(
        input_schema={
            "type": "object",
            "properties": {"value": {"allOf": [{"$ref": "#/defs/x"}]}},
        }
    )
    profile = _profile(disallowed_schema_keywords=frozenset({"$ref"}))

    violations = check_tools([tool], [profile])

    assert [(v.rule, v.value) for v in violations] == [("schema_keyword", "$ref")]


def test_a_property_named_like_a_forbidden_keyword_is_not_a_violation() -> None:
    tool = _tool(
        input_schema={
            "type": "object",
            "properties": {"$ref": {"type": "string"}},
        }
    )
    profile = _profile(disallowed_schema_keywords=frozenset({"$ref"}))

    assert check_tools([tool], [profile]) == []


def test_a_definition_named_like_a_forbidden_keyword_is_not_a_violation() -> None:
    tool = _tool(
        input_schema={
            "type": "object",
            "properties": {},
            "$defs": {"additionalProperties": {"type": "string"}},
        }
    )
    profile = _profile(disallowed_schema_keywords=frozenset({"additionalProperties"}))

    assert check_tools([tool], [profile]) == []

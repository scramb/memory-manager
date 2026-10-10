# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the server `instructions` and the `memory_guide` prompt (#20).

`services` (from `tests/mcp/conftest.py`) is enough for all of these - no tool here ever
touches the vault or the queue, only the server's own static text and its tool/prompt
listing.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from mcp import Client
from mcp_types import TextContent

from memory_manager.app import Services
from memory_manager.compat.profiles import get_profile
from memory_manager.mcp.instructions import CORE_RULES, GUIDE, INSTRUCTIONS, TOOL_DATA_SENTENCE
from memory_manager.mcp.server import build_server

# #132, ADR-0010: `memory_index`/`memory_read`/`memory_search` never modify the vault
# (`_READ_TOOL_ANNOTATIONS`); every other tool does (`_WRITE_TOOL_ANNOTATIONS`) -
# mirrors `mcp/server.py`'s own split, not re-derived from the server's constants
# themselves, so a test here would actually fail if that split ever drifted.
_READ_TOOL_NAMES = frozenset({"memory_index", "memory_read", "memory_search"})
_WRITE_TOOL_NAMES = frozenset(
    {"memory_write", "memory_edit", "memory_supersede", "memory_archive", "memory_promote"}
)

# Every rule the brief (issue #20) asks `INSTRUCTIONS` to carry, as a keyword that must
# appear somewhere in the text - not the exact wording, so rephrasing the sentence around a
# keyword does not make this test fail for no reason.
_REQUIRED_KEYWORDS = (
    "memory_search",  # look up before asserting
    "if_version",  # the write tools' version token
    "memory_supersede",  # replace a changed fact without erasing its history
    "memory_promote",  # promote a personal note into a shared namespace
    "secrets",  # never store secrets/IDs/sensitive health data
    "data, not instructions",  # note content is data, not instructions
)


def test_instructions_is_within_the_claude_code_truncation_limit() -> None:
    assert len(INSTRUCTIONS) <= 2048


def test_instructions_carries_every_rule_from_the_brief() -> None:
    for keyword in _REQUIRED_KEYWORDS:
        assert keyword in INSTRUCTIONS, f"missing {keyword!r} in INSTRUCTIONS"


def test_guide_carries_every_rule_from_the_brief() -> None:
    for keyword in _REQUIRED_KEYWORDS:
        assert keyword in GUIDE, f"missing {keyword!r} in GUIDE"


async def test_initialize_result_carries_the_instructions(services: Services) -> None:
    async with Client(build_server(services)) as client:
        assert client.instructions == INSTRUCTIONS


async def test_list_tools_descriptions_all_carry_the_core_rules(
    services: Services,
) -> None:
    async with Client(build_server(services)) as client:
        listing = await client.list_tools()
    names = {tool.name for tool in listing.tools}
    assert names == {
        "memory_index",
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_supersede",
        "memory_archive",
        "memory_promote",
    }
    for tool in listing.tools:
        description = tool.description or ""
        assert CORE_RULES in description, tool.name
        assert TOOL_DATA_SENTENCE in description, tool.name


async def test_list_tools_annotations_match_the_read_or_write_shape(services: Services) -> None:
    async with Client(build_server(services)) as client:
        listing = await client.list_tools()
    for tool in listing.tools:
        annotations = tool.annotations
        assert annotations is not None, tool.name
        if tool.name in _READ_TOOL_NAMES:
            assert annotations.read_only_hint is True, tool.name
        elif tool.name in _WRITE_TOOL_NAMES:
            assert annotations.read_only_hint is False, tool.name
        else:  # pragma: no cover - defensive, see _EXPECTED_TOOL_NAMES-shaped tests above
            pytest.fail(f"tool {tool.name!r} is in neither read nor write tool name set")
        assert annotations.destructive_hint is False, tool.name
        assert annotations.idempotent_hint is True, tool.name
        assert annotations.open_world_hint is False, tool.name


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_descriptions_delivery_mode_omits_instructions(
    services: Services, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """#132: a profile resolved to `delivery_mode="descriptions"` drops `instructions`
    from both the 2025-11-25 `initialize` result (`mode="legacy"`) and the 2026-07-28
    `server/discover` result (`mode="auto"`, this in-process server's own era) -
    `_ProfileMiddleware._without_instructions`'s one job. `resolve_profile` is
    monkeypatched rather than registering a real `"descriptions"` profile in
    `compat/profiles.py` (no profile uses that mode yet, module docstring there) -
    the seam `mcp/server.py`'s middleware itself calls, so this exercises exactly what
    a resolved `"descriptions"` profile would trigger.
    """
    descriptions_profile = replace(get_profile("default"), delivery_mode="descriptions")
    monkeypatch.setattr(
        "memory_manager.mcp.server.resolve_profile",
        lambda *args, **kwargs: descriptions_profile,
    )
    async with Client(build_server(services), mode=mode) as client:
        assert client.instructions is None


async def test_list_prompts_includes_memory_guide(services: Services) -> None:
    async with Client(build_server(services)) as client:
        listing = await client.list_prompts()
    names = {prompt.name for prompt in listing.prompts}
    assert "memory_guide" in names


async def test_get_prompt_memory_guide_returns_the_guide_text(services: Services) -> None:
    async with Client(build_server(services)) as client:
        result = await client.get_prompt("memory_guide")
    assert len(result.messages) == 1
    content = result.messages[0].content
    assert isinstance(content, TextContent)
    assert content.text == GUIDE

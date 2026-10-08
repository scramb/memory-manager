# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the server `instructions` and the `memory_guide` prompt (#20).

`services` (from `tests/mcp/conftest.py`) is enough for all of these - no tool here ever
touches the vault or the queue, only the server's own static text and its tool/prompt
listing.
"""

from __future__ import annotations

from mcp import Client
from mcp_types import TextContent

from memory_manager.app import Services
from memory_manager.mcp.instructions import GUIDE, INSTRUCTIONS, TOOL_DATA_SENTENCE
from memory_manager.mcp.server import build_server

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


async def test_list_tools_descriptions_all_carry_the_data_not_instructions_sentence(
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
        assert TOOL_DATA_SENTENCE in (tool.description or ""), tool.name


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

# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the `memory_search` MCP tool (#30).

Every test drives the tool through an in-memory `mcp.Client`, like
`tests/mcp/test_read_tools.py`. The "with database" tests use
`services_with_db` (`tests/mcp/conftest.py`) and write their own fixture
notes through `memory_write` rather than relying on `SEEDED_NOTES`'
content, so each test's expected ranking/filtering is obvious from the note
it wrote. The "without database" tests use the plain `services` fixture
(no `DATABASE_URL`), exercising `search_fallback.scan_search`.

The "namespace authorization" tests at the bottom monkeypatch
`memory_manager.mcp.server.readable_namespaces` (the name `server.py`'s
tools actually call, bound at import time from `mcp/authz.py`) to pretend
M4 is already wired up with a real, narrowing identity, and check that
`memory_search`/`memory_index`/`memory_read` all fail closed on it - the
deny-all-becomes-allow-all regression this guards against is #30's finding
that an empty `namespaces` filter list must never collapse into "no
filter".
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from mcp import Client
from mcp_types import TextContent

from memory_manager.app import Services
from memory_manager.mcp.server import build_server
from memory_manager.vault.validate import NOTE_TYPES

_ARCHIVED_FACT_PATH = "_archive/personal/fact/retired-fact.md"
_FAVORITE_COLOR_PATH = "personal/fact/favorite-color.md"
_DEPLOY_NOTES_PATH = "work/reference/deploy-notes.md"


def _note_content(
    *,
    title: str,
    body: str,
    type: str = "fact",
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    valid_to: str | None = None,
) -> str:
    """A complete note file for `memory_write`, frontmatter built from the given fields."""
    lines = [
        f"title: {title}",
        f"description: {title} description.",
        f"type: {type}",
    ]
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    if aliases:
        lines.append("aliases: [" + ", ".join(aliases) + "]")
    if valid_to:
        lines.append(f"valid_to: {valid_to}")
    frontmatter = "\n".join(lines)
    return f"---\n{frontmatter}\n---\n{body}\n"


async def _write(client: Client, path: str, **fields: object) -> None:
    content = _note_content(**fields)  # type: ignore[arg-type]
    result = await client.call_tool(
        "memory_write", {"path": path, "content": content, "if_version": "new"}
    )
    assert result.is_error is False, result.content


async def _search(client: Client, query: str, **kwargs: object) -> dict[str, Any]:
    result = await client.call_tool("memory_search", {"query": query, **kwargs})
    assert result.is_error is False, result.content
    return cast(dict[str, Any], result.structured_content)


# -- with a database (search.hybrid_search) -----------------------------------


async def test_memory_search_with_database_ranks_by_relevance(services_with_db: Services) -> None:
    async with Client(build_server(services_with_db)) as client:
        await _write(
            client,
            "personal/fact/giraffe-habits.md",
            title="Giraffe Habits",
            body=(
                "Giraffes eat leaves high in acacia trees. Giraffes are the tallest "
                "land animal, and giraffes use their long necks to reach food."
            ),
        )
        await _write(
            client,
            "personal/fact/giraffe-decoy.md",
            title="Neighborhood Newsletter",
            body=(
                "This newsletter covers gardening, weather, recipes, local sports, "
                "traffic updates and a brief note about a giraffe at the zoo."
            ),
        )

        payload = await _search(client, "giraffe")

    assert payload["mode"] == "fulltext"
    paths = [item["path"] for item in payload["results"]]
    assert paths[0] == "personal/fact/giraffe-habits.md"
    assert "personal/fact/giraffe-decoy.md" in paths

    top = payload["results"][0]
    assert top["title"] == "Giraffe Habits"
    assert set(top) == {"id", "path", "title", "description", "type", "tags", "snippet", "score"}


async def test_memory_search_with_database_filters_by_type_tags_and_namespace(
    services_with_db: Services,
) -> None:
    async with Client(build_server(services_with_db)) as client:
        await _write(
            client,
            "personal/fact/giraffe-habits.md",
            title="Giraffe Habits",
            type="fact",
            tags=["animal"],
            body="Giraffes eat leaves high in acacia trees in the savanna.",
        )
        await _write(
            client,
            "work/reference/giraffe-reference.md",
            title="Giraffe Reference",
            type="reference",
            tags=["ref"],
            body="Reference material about the giraffe, for work use.",
        )

        by_type = await _search(client, "giraffe", types=["fact"])
        assert {item["path"] for item in by_type["results"]} == {"personal/fact/giraffe-habits.md"}

        by_tag = await _search(client, "giraffe", tags=["animal"])
        assert {item["path"] for item in by_tag["results"]} == {"personal/fact/giraffe-habits.md"}

        by_namespace = await _search(client, "giraffe", namespaces=["work"])
        assert {item["path"] for item in by_namespace["results"]} == {
            "work/reference/giraffe-reference.md"
        }


async def test_memory_search_with_database_limit_is_clamped(services_with_db: Services) -> None:
    async with Client(build_server(services_with_db)) as client:
        for i in range(3):
            await _write(
                client,
                f"personal/fact/lighthouse-{i}.md",
                title=f"Lighthouse {i}",
                body="A lighthouse guides ships safely along the lighthouse coastline.",
            )

        clamped_low = await _search(client, "lighthouse", limit=0)
        assert len(clamped_low["results"]) == 1

        clamped_high = await _search(client, "lighthouse", limit=999)
        assert len(clamped_high["results"]) <= 25
        assert len(clamped_high["results"]) == 3


# -- without a database (search_fallback.scan_search) --------------------------


async def test_memory_search_without_database_mode_is_scan(services: Services) -> None:
    async with Client(build_server(services)) as client:
        payload = await _search(client, "favorite")

    assert payload["mode"] == "scan"
    assert any(item["path"] == "personal/fact/favorite-color.md" for item in payload["results"])


async def test_memory_search_without_database_finds_by_title_and_alias(
    services: Services,
) -> None:
    async with Client(build_server(services)) as client:
        await _write(
            client,
            "personal/fact/quokka-routine.md",
            title="Quokka Routine",
            aliases=["zephyrus-notes"],
            body="A short note about a small marsupial's daily routine.",
        )

        by_title = await _search(client, "quokka")
        assert {item["path"] for item in by_title["results"]} == {"personal/fact/quokka-routine.md"}

        by_alias = await _search(client, "zephyrus")
        assert {item["path"] for item in by_alias["results"]} == {"personal/fact/quokka-routine.md"}


async def test_memory_search_without_database_excludes_archived_unless_requested(
    services: Services,
) -> None:
    async with Client(build_server(services)) as client:
        default = await _search(client, "retired")
        assert default["results"] == []

        with_archived = await _search(client, "retired", include_archived=True)
        assert {item["path"] for item in with_archived["results"]} == {_ARCHIVED_FACT_PATH}


async def test_memory_search_without_database_valid_at_today_excludes_expired(
    services: Services,
) -> None:
    async with Client(build_server(services)) as client:
        await _write(
            client,
            "personal/fact/lumen-protocol.md",
            title="Lumen Protocol",
            valid_to="2000-01-01",
            body="Details of the lumen protocol, no longer in effect.",
        )

        without_filter = await _search(client, "lumen")
        assert {item["path"] for item in without_filter["results"]} == {
            "personal/fact/lumen-protocol.md"
        }

        with_today = await _search(client, "lumen", valid_at="today")
        assert with_today["results"] == []


# -- the `postgres` backend, no indexer yet (search_fallback.scan_notes, #98) ---


async def test_memory_search_with_postgres_backend_mode_is_scan_and_finds_the_note(
    services_with_postgres_backend: Services,
) -> None:
    async with Client(build_server(services_with_postgres_backend)) as client:
        await _write(
            client,
            "personal/fact/aardvark-routine.md",
            title="Aardvark Routine",
            body="A short note about an aardvark's nightly foraging routine.",
        )

        payload = await _search(client, "aardvark")

    assert payload["mode"] == "scan"
    assert {item["path"] for item in payload["results"]} == {"personal/fact/aardvark-routine.md"}


# -- validation, shared by both modes -------------------------------------------


async def test_memory_search_rejects_unknown_type(services: Services) -> None:
    async with Client(build_server(services)) as client:
        result = await client.call_tool("memory_search", {"query": "x", "types": ["bogus"]})
    assert result.is_error is True
    content_block = result.content[0]
    assert isinstance(content_block, TextContent)
    assert "bogus" in content_block.text
    for note_type in NOTE_TYPES:
        assert note_type in content_block.text


# -- namespace authorization fails closed (#30) ---------------------------------


def _deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend `readable_namespaces` found the caller may read nothing at all."""
    monkeypatch.setattr("memory_manager.mcp.server.readable_namespaces", lambda ctx: set())


def _allow_only(monkeypatch: pytest.MonkeyPatch, namespace: str) -> None:
    """Pretend `readable_namespaces` found the caller may read only `namespace`."""
    monkeypatch.setattr("memory_manager.mcp.server.readable_namespaces", lambda ctx: {namespace})


async def test_memory_search_deny_all_returns_empty_without_querying_scan(
    monkeypatch: pytest.MonkeyPatch, services: Services
) -> None:
    _deny_all(monkeypatch)
    async with Client(build_server(services)) as client:
        payload = await _search(client, "favorite")
    assert payload == {"results": [], "mode": "scan"}


async def test_memory_search_deny_all_returns_empty_without_querying_with_database(
    monkeypatch: pytest.MonkeyPatch, services_with_db: Services
) -> None:
    _deny_all(monkeypatch)
    async with Client(build_server(services_with_db)) as client:
        payload = await _search(client, "favorite")
    assert payload == {"results": [], "mode": "fulltext"}


async def test_memory_search_readable_subset_ignores_an_unreadable_request(
    monkeypatch: pytest.MonkeyPatch, services: Services
) -> None:
    _allow_only(monkeypatch, "work")
    async with Client(build_server(services)) as client:
        # The caller asks for "personal", which is not in what is readable - the
        # intersection is empty, so the result must be empty, not "work" notes.
        denied = await _search(client, "notes", namespaces=["personal"])
        assert denied["results"] == []

        # No explicit `namespaces` filter at all - readable narrows it to "work" alone.
        allowed = await _search(client, "notes")
        assert {item["path"] for item in allowed["results"]} == {_DEPLOY_NOTES_PATH}


async def test_memory_index_deny_all_lists_nothing(
    monkeypatch: pytest.MonkeyPatch, services: Services
) -> None:
    _deny_all(monkeypatch)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("memory_index", {})
    assert result.is_error is False
    assert result.structured_content["result"] == []


async def test_memory_read_deny_all_reports_not_found(
    monkeypatch: pytest.MonkeyPatch, services: Services
) -> None:
    _deny_all(monkeypatch)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("memory_read", {"items": [_FAVORITE_COLOR_PATH]})
    assert result.is_error is False
    items = result.structured_content["result"]
    assert items[0]["error"]["error"] == "NotFound"

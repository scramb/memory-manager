# SPDX-License-Identifier: AGPL-3.0-only
"""Checks the real server's tools against every registered profile's limits (#133, ADR-0010).

ADR-0010 option B keeps one tool contract and lets profiles (`profiles.py`) only change
*delivery*; this module is the "schema linter in CI" that decision promises to enforce
that the contract stays inside every profile's documented limits. `check_tools` is a pure
function over a list of `mcp_types.Tool` and `profiles.Profile` - it reads data, it never
builds or owns a server itself. `list_server_tools` is the one function that does: it
builds the real server with an inert `Services` (never calling anything that touches the
filesystem, a git remote or a database) and lists its tools in-process the same way
`tests/mcp/test_instructions.py` does, following `next_cursor` across pages.

This is `compat/__init__.py`'s one documented exception to "profiles are data, not code
that reaches into `mcp`/`app`" - see that module's docstring for why the reverse
(`mcp/server.py` importing `lint.py`) would be the actual cycle, which does not happen.

Rules checked, one profile x one tool at a time unless noted:
(a) **name charset** - every tool name must match SEP-986's `^[A-Za-z0-9._-]{1,128}$`
    (docs/research/mcp-auth-and-connectors.md §1 [S13], retrieved 2026-10-10). This is a
    protocol-level SHOULD, not a per-client limit, but it is checked once per registered
    profile anyway - same report shape as every other rule, and it means a report that
    filters by profile never silently drops it.
(b) **name length** - `len(tool.name)` plus the profile's documented `tool_name_prefix_chars`
    (0 when `None` - "not documented" never means "no prefix is ever added", but there is
    nothing to add either) against `max_tool_name_chars`. Skipped when the latter is `None`.
(c) **description length** - `len(tool.description or "")` against `max_tool_description_chars`.
    Skipped when `None`.
(d) **disallowed schema keywords** - `profile.disallowed_schema_keywords`, walked
    recursively through `tool.input_schema` only (never `output_schema` - #133 excludes
    it). A JSON Schema keyword only means a dict key at a *keyword position*: the schema
    object's own keys, or one of `allOf`/`anyOf`/`oneOf`/`prefixItems` (a list of nested
    schemas), `items`/`not`/`if`/`then`/`else`/`additionalProperties`/`contains`/
    `propertyNames` (a single nested schema), or the *values* of `properties`/
    `patternProperties`/`$defs`/`definitions` (each a nested schema) - never the *keys* of
    those last three, which are property names, regex patterns and definition names, not
    keywords. A parameter literally named `type` is therefore never a violation; a
    genuine `type` keyword still is, wherever it appears.
(e) **tool count** - `len(tools)` against `max_tools`, once per profile. Skipped when `None`.
(f) **annotations present** (#132, ADR-0010) - `tool.annotations` must be set, with all
    four of `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` not `None`.
    Profile-independent like rule (a): every tool's contract carries the same
    `ToolAnnotations`, regardless of which profile is checked, so this is reported once
    per profile too, the same report shape as every other rule here.

Not checked here (#133's own "not included"): the concrete *values* `mcp/server.py` sets
for `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` (only that they are
present, rule (f) above - `tests/mcp/test_instructions.py` asserts the exact values
instead), Codex's concrete charset/length/prefix values (#163, #166, #169), `output_schema`,
and linting a tool *call result* rather than its declared contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp_types
from mcp import Client

from memory_manager.app import Services
from memory_manager.compat.profiles import Profile
from memory_manager.config import VaultConfig
from memory_manager.mcp.server import build_server
from memory_manager.queue import WriteQueue
from memory_manager.storage.git import GitBackend
from memory_manager.vault.repo import Repo

__all__ = ["Violation", "check_tools", "list_server_tools"]

# SEP-986 (docs/research/mcp-auth-and-connectors.md §1 [S13], retrieved 2026-10-10):
# "Tool names SHOULD be between 1 and 128 characters ... Allowed characters: uppercase
# and lowercase ASCII letters, digits, underscore, dash, and dot." Identical to `mcp`
# 2.3.0's own `mcp.shared.tool_name_validation.TOOL_NAME_REGEX` - duplicated rather than
# imported, since that module only logs a warning (never raises) and is private SDK
# plumbing we want to compare our tools against, not depend on by name.
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# A schema's own keys whose *value* is one nested schema to recurse into.
_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {"items", "not", "if", "then", "else", "additionalProperties", "contains", "propertyNames"}
)
# A schema's own keys whose *value* is a list of nested schemas to recurse into.
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# A schema's own keys whose *value* maps names (property names, regex patterns, or
# definition names - never keywords themselves) to nested schemas to recurse into.
_SCHEMA_NAME_MAP_KEYWORDS = frozenset({"properties", "patternProperties", "$defs", "definitions"})

# Never resolved (no I/O is ever performed against them - `list_server_tools` only
# constructs `Repo`/`WriteQueue`/`GitBackend` to satisfy `Services`'s shape, then lists
# tools in-process; nothing here calls `ensure_clone`, `start`, or any other method that
# would touch a filesystem path or a git remote).
_INERT_REMOTE = "https://compat-lint.invalid/unused.git"
_INERT_VAULT_DIR = Path("/nonexistent/compat-lint-vault")


@dataclass(frozen=True)
class Violation:
    """One tool that breaks one profile's documented limit, or SEP-986 (rule `a`)."""

    profile: str
    tool: str
    rule: str
    value: str


def _inert_services() -> Services:
    """A `Services` that never performs I/O, built only so `build_server` has one to act on.

    `Repo.__init__`/`WriteQueue.__init__`/`GitBackend.__init__` only store the arguments
    they are given (`vault/repo.py`, `queue.py`, `storage/git.py`) - none of the three
    clones, opens, or reads anything. `list_server_tools` only lists tools, which never
    calls a method on `repo`/`queue`/`storage` that would.
    """
    repo = Repo(VaultConfig(remote=_INERT_REMOTE, dir=_INERT_VAULT_DIR))
    queue = WriteQueue(repo)
    storage = GitBackend(queue, repo, _INERT_VAULT_DIR)
    return Services(
        repo=repo,
        queue=queue,
        vault_root=_INERT_VAULT_DIR,
        pool=None,
        indexer=None,
        provider=None,
        storage=storage,
    )


async def list_server_tools() -> list[mcp_types.Tool]:
    """Every tool the real server registers, listed in-process (pattern: `test_instructions.py`).

    Builds the server with `_inert_services()` and lists its tools through an in-process
    `mcp.Client`, following `next_cursor` across pages until the listing is complete - the
    real server returns everything on one page today, but a linter that silently checked
    only the first page would stay quietly wrong the day that stops being true.
    """
    services = _inert_services()
    tools: list[mcp_types.Tool] = []
    async with Client(build_server(services)) as client:
        cursor: str | None = None
        while True:
            listing = await client.list_tools(cursor=cursor)
            tools.extend(listing.tools)
            if listing.next_cursor is None:
                return tools
            cursor = listing.next_cursor


def check_tools(tools: Sequence[mcp_types.Tool], profiles: Sequence[Profile]) -> list[Violation]:
    """Every way `tools` breaks one of `profiles`'s documented limits, or SEP-986."""
    violations: list[Violation] = []
    for profile in profiles:
        violations.extend(_check_tool_count(tools, profile))
        for tool in tools:
            violations.extend(_check_name_charset(tool, profile))
            violations.extend(_check_name_length(tool, profile))
            violations.extend(_check_description_length(tool, profile))
            violations.extend(_check_schema_keywords(tool, profile))
            violations.extend(_check_annotations_present(tool, profile))
    return violations


def _check_name_charset(tool: mcp_types.Tool, profile: Profile) -> list[Violation]:
    if _TOOL_NAME_PATTERN.match(tool.name):
        return []
    return [Violation(profile.name, tool.name, "name_charset", tool.name)]


def _check_annotations_present(tool: mcp_types.Tool, profile: Profile) -> list[Violation]:
    annotations = tool.annotations
    if annotations is not None and (
        annotations.read_only_hint is not None
        and annotations.destructive_hint is not None
        and annotations.idempotent_hint is not None
        and annotations.open_world_hint is not None
    ):
        return []
    return [Violation(profile.name, tool.name, "annotations_present", "missing")]


def _check_name_length(tool: mcp_types.Tool, profile: Profile) -> list[Violation]:
    if profile.max_tool_name_chars is None:
        return []
    # `tool_name_prefix_chars` not documented (`None`) means "check the bare name alone,
    # with a 0-length prefix" - not "no prefix is ever added" (profiles.py's own
    # docstring for the field).
    prefix = profile.tool_name_prefix_chars or 0
    length = len(tool.name) + prefix
    if length <= profile.max_tool_name_chars:
        return []
    value = (
        f"{length}/{profile.max_tool_name_chars} chars (name {len(tool.name)} + prefix {prefix})"
    )
    return [Violation(profile.name, tool.name, "name_length", value)]


def _check_description_length(tool: mcp_types.Tool, profile: Profile) -> list[Violation]:
    if profile.max_tool_description_chars is None:
        return []
    length = len(tool.description or "")
    if length <= profile.max_tool_description_chars:
        return []
    value = f"{length}/{profile.max_tool_description_chars} chars"
    return [Violation(profile.name, tool.name, "description_length", value)]


def _check_schema_keywords(tool: mcp_types.Tool, profile: Profile) -> list[Violation]:
    if not profile.disallowed_schema_keywords:
        return []
    found: set[str] = set()
    _walk_schema(tool.input_schema, profile.disallowed_schema_keywords, found)
    return [
        Violation(profile.name, tool.name, "schema_keyword", keyword) for keyword in sorted(found)
    ]


def _check_tool_count(tools: Sequence[mcp_types.Tool], profile: Profile) -> list[Violation]:
    if profile.max_tools is None or len(tools) <= profile.max_tools:
        return []
    value = f"{len(tools)}/{profile.max_tools} tools"
    return [Violation(profile.name, "(server)", "tool_count", value)]


def _walk_schema(node: Any, disallowed: frozenset[str], found: set[str]) -> None:
    """Add every `disallowed` keyword actually used as a JSON Schema keyword in `node`.

    `node` is a schema object; every one of its own keys is a keyword position and is
    checked against `disallowed` outright. Keys that carry nested schemas are then
    recursed into according to how JSON Schema 2020-12 shapes them - see the module
    docstring's rule (d) for the three shapes. A property/pattern/definition *name* is
    never itself checked: only the three `_SCHEMA_NAME_MAP_KEYWORDS` keys that introduce
    such a map are keyword positions; their map's own keys are not.
    """
    if not isinstance(node, Mapping):
        return
    for key, value in node.items():
        if key in disallowed:
            found.add(key)
        if key in _SCHEMA_NAME_MAP_KEYWORDS and isinstance(value, Mapping):
            for nested in value.values():
                _walk_schema(nested, disallowed, found)
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            for nested in value:
                _walk_schema(nested, disallowed, found)
        elif key in _SCHEMA_SINGLE_KEYWORDS and isinstance(value, Mapping):
            _walk_schema(value, disallowed, found)

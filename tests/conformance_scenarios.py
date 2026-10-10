# SPDX-License-Identifier: AGPL-3.0-only
"""The conformance scenario list, its transcript format, and golden comparison (#136,
ADR-0010).

`SCENARIO_NAMES` is the fixed, ordered list every combination
(`conformance_fixtures.Combo`) runs once: `surface` (the tool listing itself), then the
read-only tools (`index`, `search`, `read`), then one scenario per write tool (`write`
exercises create-then-replace; `edit`, `supersede`, `archive` each create their own note
first), then the three error cases (`error-stale-version`, `error-missing-scope`,
`error-foreign-namespace`). Every scenario uses its own path under
`conformance_fixtures` (never the seed note, never another scenario's path), so none of
them can ever race another - `run_scenario` (the one entry point
`tests/conformance/profiles/test_profile_conformance.py` calls) runs them in whatever
order the caller likes; this module's own `SCENARIO_NAMES` is what fixes the order the
contract promises.

A scenario is a plain async function over a `ScenarioContext`: `ctx.call(tool,
arguments, identity=...)` performs one tool call, records it (normalised) onto
`ctx.steps`, and returns the *raw* `CallToolResult` so the scenario itself can read a
real `version`/`id` back out to feed into its next call - golden comparison only ever
sees the normalised copy, replaying real values into the next request would make every
run diverge from the last. `ctx.call` (and `ctx.list_tools`) raise `ScenarioSkipped`
outright when the combination has no such identity at all (stdio: no bearer tokens);
`run_scenario` catches that and reports "skipped", never "failed" - `error-missing-
scope`/`error-foreign-namespace` rely on this to skip themselves cleanly on stdio.

ADR-0010's own rule - profiles change delivery only, never tool behaviour - is what
makes one golden file per `scenario x backend` (`tests/conformance/profiles/golden/
<backend>/<scenario>.json`) correct regardless of which profile or transport produced
it: `compare_or_update` is the one function that reads/writes/compares them.
`check_delivery` is the profile-dependent check this module deliberately keeps
*separate* from the scenario transcripts: it has nothing to do with tool behaviour, only
with what `initialize`/`server/discover` returns as `instructions`.

Replay rule for whoever reuses `ScenarioContext`/`SCENARIOS` for the later client/E2E
tests the module docstring above promises: run every scenario in `SCENARIO_NAMES`'s own
order from a freshly seeded combination, or run only the read-only prefix
(`surface`/`index`/`search`/`read`) on its own - never a write scenario on a vault that
already carries its own earlier run's notes, and never out of order: `write`/`edit`/
`supersede`/`archive`/the three error scenarios each assume the fresh seed's exact state.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from conformance_fixtures import (
    ARCHIVE_PATH,
    EDIT_PATH,
    FOREIGN_PATH_GIT,
    FOREIGN_PATH_POSTGRES,
    MISSING_SCOPE_PATH,
    SEED_PATH,
    STALE_PATH,
    SUPERSEDE_NEW_PATH,
    SUPERSEDE_OLD_PATH,
    WRITE_PATH,
    ConformanceSession,
)
from mcp_types import CallToolResult, ListToolsResult, TextContent

from memory_manager.compat.profiles import Profile
from memory_manager.mcp.instructions import INSTRUCTIONS, SHORT

__all__ = [
    "SCENARIO_NAMES",
    "ScenarioContext",
    "ScenarioSkipped",
    "Transcript",
    "check_delivery",
    "compare_or_update",
    "run_scenario",
]

SCENARIO_NAMES: tuple[str, ...] = (
    "surface",
    "index",
    "search",
    "read",
    "write",
    "edit",
    "supersede",
    "archive",
    "error-stale-version",
    "error-missing-scope",
    "error-foreign-namespace",
)

_GOLDEN_ROOT = Path(__file__).parent / "conformance" / "profiles" / "golden"


class ScenarioSkipped(Exception):
    """A scenario cannot run against this combination (a needed identity is missing).

    Caught by `run_scenario`, never by a scenario itself - raised, not returned, so a
    scenario body can call `ctx.call(...)` with an identity it merely hopes exists and
    let the skip propagate, rather than every scenario re-deriving the same
    `if not ctx.has_identity(...)` guard.
    """


# --- Normalisation: the one thing every transcript's values go through ------------

#: `vault.note.version`'s sha256 hexdigest (64 lowercase hex chars) - both the
#: `"version"`/`"current_version"`/`"commit"` *keys* (below, by name) and, separately,
#: the same digest embedded in free text (`VersionConflict`'s own message) go through
#: this.
_HEX64_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])")

#: `vault.ulid._ULID_RE`'s own pattern, unanchored and boundary-guarded so it never
#: matches a 26-character window inside a longer alphanumeric run (a ULID embedded in
#: a note's `content` body, or in a `"postgres"`-backend `commit` value shaped
#: `<ulid>@<revision>` - the latter is already consumed whole by the `"commit"` key
#: rule below before this ever runs over it).
_ULID_RE = re.compile(r"(?<![0-9A-Za-z])[0-7][0-9A-HJKMNP-TV-Z]{25}(?![0-9A-Za-z])")

#: A full ISO-8601 timestamp - `vault.note._format_timestamp`'s `...Z` form and
#: `datetime.isoformat()`'s `...+00:00` form alike.
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")

#: A bare `YYYY-MM-DD` date left over once every full timestamp above is already gone -
#: only masked when it actually equals *today* (a fixed seed date, e.g. the seeded
#: note's own `2025-06-01`, must stay literal and visible in the golden).
_BARE_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")

#: Dict keys whose value is swapped wholesale (regardless of its shape) rather than
#: scanned for a pattern - `"version"`/`"current_version"` and `"commit"` share one
#: numbering (both are the same sha256 digest on this server, `vault.note.version`);
#: `"commit"` gets its own numbering, since a `"postgres"`-backend commit
#: (`<ulid>@<revision>`) is a different shape entirely.
_VERSION_KEYS = ("version", "current_version")


class _Normalizer:
    """Per-transcript state: every placeholder category is numbered by first
    appearance *within one transcript*, never shared across scenarios or combinations
    - two different values that happen to collide across two transcripts must not
    appear to be "the same" just because both were assigned `1`.
    """

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, int]] = {"version": {}, "commit": {}, "ulid": {}}
        self._today = date.today().isoformat()

    def _assign(self, category: str, raw: str) -> str:
        table = self._tables[category]
        if raw not in table:
            table[raw] = len(table) + 1
        return f"<{category}:{table[raw]}>"

    def normalize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._normalize_entry(key, item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.normalize(item) for item in value]
        if isinstance(value, str):
            return self._normalize_string(value)
        return value

    def _normalize_entry(self, key: str, value: Any) -> Any:
        if key in _VERSION_KEYS and isinstance(value, str):
            return self._assign("version", value)
        if key == "commit" and isinstance(value, str):
            return self._assign("commit", value)
        if key == "score" and value is not None:
            return "<score>"
        return self.normalize(value)

    def _normalize_string(self, text: str) -> str:
        text = _HEX64_RE.sub(lambda m: self._assign("version", m.group(0)), text)
        text = _ULID_RE.sub(lambda m: self._assign("ulid", m.group(0)), text)
        text = _DATETIME_RE.sub("<datetime>", text)
        text = _BARE_DATE_RE.sub(
            lambda m: "<today>" if m.group(0) == self._today else m.group(0), text
        )
        return text


def _joined_text(result: CallToolResult) -> str:
    """Every `TextContent` block in `result.content`, joined - the transcript's own
    `"text"` field for a `ToolError` result, which carries no `structured_content`.
    """
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _build_step(
    normalizer: _Normalizer,
    identity: str,
    tool: str,
    arguments: Mapping[str, Any],
    result: CallToolResult,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "identity": identity,
        "tool": tool,
        "arguments": normalizer.normalize(dict(arguments)),
        "is_error": bool(result.is_error),
    }
    if result.structured_content is not None:
        step["structured"] = normalizer.normalize(result.structured_content)
    else:
        step["text"] = normalizer.normalize(_joined_text(result))
    return step


def _build_surface_step(
    normalizer: _Normalizer, identity: str, listing: ListToolsResult
) -> dict[str, Any]:
    tools = []
    for tool in sorted(listing.tools, key=lambda item: item.name):
        annotations = (
            tool.annotations.model_dump(mode="json", exclude_none=True)
            if tool.annotations is not None
            else None
        )
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "annotations": annotations,
            }
        )
    return {
        "identity": identity,
        "tool": "tools/list",
        "arguments": {},
        "is_error": False,
        "structured": normalizer.normalize(tools),
    }


# --- Scenario context: what a scenario function runs against ----------------------


@dataclass
class ScenarioContext:
    """What one scenario run calls through - one `_Normalizer`, one `steps` list, both
    fresh for every `run_scenario` call (never shared across scenarios).
    """

    session: ConformanceSession
    steps: list[dict[str, Any]] = field(default_factory=list)
    _normalizer: _Normalizer = field(default_factory=_Normalizer)

    @property
    def backend(self) -> str:
        return self.session.combo.backend

    def has_identity(self, name: str) -> bool:
        return self.session.has_identity(name)

    async def call(
        self, tool: str, arguments: Mapping[str, Any], *, identity: str = "primary"
    ) -> CallToolResult:
        if not self.session.has_identity(identity):
            raise ScenarioSkipped(
                f"{self.session.combo.id}: no {identity!r} identity (no bearer tokens here)"
            )
        result = await self.session.call_tool(identity, tool, dict(arguments))
        self.steps.append(_build_step(self._normalizer, identity, tool, arguments, result))
        return result

    async def list_tools(self, *, identity: str = "primary") -> ListToolsResult:
        if not self.session.has_identity(identity):
            raise ScenarioSkipped(
                f"{self.session.combo.id}: no {identity!r} identity (no bearer tokens here)"
            )
        listing = await self.session.list_tools(identity)
        self.steps.append(_build_surface_step(self._normalizer, identity, listing))
        return listing


def _note_text(*, title: str, description: str, body: str) -> str:
    return f"---\ntitle: {title}\ndescription: {description}\ntype: fact\n---\n{body}"


def _require_structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, "expected structured content, got none"
    return dict(result.structured_content)


# --- The scenarios themselves, in SCENARIO_NAMES's order ---------------------------


async def _scenario_surface(ctx: ScenarioContext) -> None:
    await ctx.list_tools()


async def _scenario_index(ctx: ScenarioContext) -> None:
    await ctx.call("memory_index", {})


async def _scenario_search(ctx: ScenarioContext) -> None:
    await ctx.call("memory_search", {"query": "conformance"})


async def _scenario_read(ctx: ScenarioContext) -> None:
    await ctx.call("memory_read", {"items": [SEED_PATH]})


async def _scenario_write(ctx: ScenarioContext) -> None:
    created = await ctx.call(
        "memory_write",
        {
            "path": WRITE_PATH,
            "content": _note_text(
                title="Conformance write",
                description="Created for the write scenario.",
                body="First.\n",
            ),
            "if_version": "new",
        },
    )
    version = _require_structured(created)["version"]
    await ctx.call(
        "memory_write",
        {
            "path": WRITE_PATH,
            "content": _note_text(
                title="Conformance write",
                description="Created for the write scenario.",
                body="Replaced.\n",
            ),
            "if_version": version,
        },
    )


async def _scenario_edit(ctx: ScenarioContext) -> None:
    created = await ctx.call(
        "memory_write",
        {
            "path": EDIT_PATH,
            "content": _note_text(
                title="Conformance edit",
                description="Created for the edit scenario.",
                body="Original.\n",
            ),
            "if_version": "new",
        },
    )
    version = _require_structured(created)["version"]
    await ctx.call(
        "memory_edit",
        {
            "path": EDIT_PATH,
            "old_str": "Original.\n",
            "new_str": "Edited.\n",
            "if_version": version,
        },
    )


async def _scenario_supersede(ctx: ScenarioContext) -> None:
    created = await ctx.call(
        "memory_write",
        {
            "path": SUPERSEDE_OLD_PATH,
            "content": _note_text(
                title="Conformance supersede (old)",
                description="Created for the supersede scenario.",
                body="Outdated.\n",
            ),
            "if_version": "new",
        },
    )
    old_version = _require_structured(created)["version"]
    await ctx.call(
        "memory_supersede",
        {
            "old": SUPERSEDE_OLD_PATH,
            "new_path": SUPERSEDE_NEW_PATH,
            "new_content": _note_text(
                title="Conformance supersede (new)",
                description="Replaces the old note.",
                body="Current.\n",
            ),
            "if_version": old_version,
        },
    )


async def _scenario_archive(ctx: ScenarioContext) -> None:
    created = await ctx.call(
        "memory_write",
        {
            "path": ARCHIVE_PATH,
            "content": _note_text(
                title="Conformance archive",
                description="Created for the archive scenario.",
                body="To be archived.\n",
            ),
            "if_version": "new",
        },
    )
    version = _require_structured(created)["version"]
    # `storage.rules.prepare_archive` stamps `updated` to the archive-time `now()`
    # (truncated to whole seconds, `queue.py`'s own `_do_archive`) and touches nothing
    # else in the note's content - sleeping past a second boundary first guarantees the
    # archived note's version always differs from the freshly created one, deterministic
    # across runs; without it, a create+archive landing in the same wall-clock second
    # would leave the content, and therefore the version, byte-identical, flipping the
    # golden's "same value, same <version:N>" vs. "new value, new <version:N>" shape
    # depending on timing alone (caught by running this suite twice in a row).
    await asyncio.sleep(1.1)
    await ctx.call("memory_archive", {"path": ARCHIVE_PATH, "if_version": version})


async def _scenario_error_stale_version(ctx: ScenarioContext) -> None:
    await ctx.call(
        "memory_write",
        {
            "path": STALE_PATH,
            "content": _note_text(
                title="Conformance stale version",
                description="Created for the error-stale-version scenario.",
                body="First.\n",
            ),
            "if_version": "new",
        },
    )
    # The note now exists, so `if_version="new"` against it again is always stale.
    conflict = await ctx.call(
        "memory_write",
        {
            "path": STALE_PATH,
            "content": _note_text(
                title="Conformance stale version",
                description="Created for the error-stale-version scenario.",
                body="Second, never actually written.\n",
            ),
            "if_version": "new",
        },
    )
    conflict_payload = _require_structured(conflict)
    assert conflict_payload["error"] == "VersionConflict", conflict_payload
    current_version = conflict_payload["current_version"]

    fresh_read = await ctx.call("memory_read", {"items": [STALE_PATH]})
    read_items = _require_structured(fresh_read)["result"]
    assert read_items[0]["version"] == current_version, (
        f"VersionConflict.current_version {current_version!r} does not match a fresh "
        f"memory_read ({read_items[0].get('version')!r})"
    )


async def _scenario_error_missing_scope(ctx: ScenarioContext) -> None:
    await ctx.call(
        "memory_write",
        {
            "path": MISSING_SCOPE_PATH,
            "content": _note_text(
                title="Conformance missing scope",
                description="Should never land - the identity lacks memory:write.",
                body="Nope.\n",
            ),
            "if_version": "new",
        },
        identity="read_only",
    )


async def _scenario_error_foreign_namespace(ctx: ScenarioContext) -> None:
    if ctx.backend == "postgres":
        await ctx.call("memory_read", {"items": [FOREIGN_PATH_POSTGRES]}, identity="foreign")
        await ctx.call(
            "memory_write",
            {
                "path": FOREIGN_PATH_POSTGRES,
                "content": _note_text(
                    title="Conformance foreign namespace",
                    description="Should never land - a different principal's namespace.",
                    body="Nope.\n",
                ),
                "if_version": "new",
            },
            identity="foreign",
        )
        return

    await ctx.call(
        "memory_write",
        {
            "path": FOREIGN_PATH_GIT,
            "content": _note_text(
                title="Conformance foreign namespace",
                description="Should never land - the token is restricted to another namespace.",
                body="Nope.\n",
            ),
            "if_version": "new",
        },
        identity="restricted",
    )


_SCENARIOS: dict[str, Callable[[ScenarioContext], Awaitable[None]]] = {
    "surface": _scenario_surface,
    "index": _scenario_index,
    "search": _scenario_search,
    "read": _scenario_read,
    "write": _scenario_write,
    "edit": _scenario_edit,
    "supersede": _scenario_supersede,
    "archive": _scenario_archive,
    "error-stale-version": _scenario_error_stale_version,
    "error-missing-scope": _scenario_error_missing_scope,
    "error-foreign-namespace": _scenario_error_foreign_namespace,
}
assert set(_SCENARIOS) == set(SCENARIO_NAMES)


# --- Transcript, golden load/compare/write -----------------------------------------


@dataclass(frozen=True)
class Transcript:
    scenario: str
    backend: str
    steps: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "format": 1,
            "scenario": self.scenario,
            "backend": self.backend,
            "steps": self.steps,
        }


def _golden_path(scenario: str, backend: str) -> Path:
    return _GOLDEN_ROOT / backend / f"{scenario}.json"


def _render(transcript: Transcript) -> str:
    return json.dumps(transcript.to_json(), sort_keys=True, indent=2) + "\n"


def compare_or_update(transcript: Transcript) -> str | None:
    """Compare `transcript` against its golden file (`scenario x backend`, ADR-0010:
    independent of profile and transport).

    `None` on a match. A missing golden fails with a command hint, unless
    `MM_CONFORMANCE_UPDATE=1` is set (refused outright when `CI` is set) - in which
    case it is written and this still returns `None`; this only ever *creates* a
    missing golden, it never overwrites an existing one; a later combination in the
    same run that disagrees with what an earlier one just wrote is exactly the
    profile/transport-dependence ADR-0010 rules out, so it is reported as a mismatch
    like any other, not silently resolved by writing again.
    """
    path = _golden_path(transcript.scenario, transcript.backend)
    rendered = _render(transcript)

    if not path.exists():
        if os.environ.get("MM_CONFORMANCE_UPDATE") == "1":
            if os.environ.get("CI"):
                raise RuntimeError("MM_CONFORMANCE_UPDATE=1 is refused when CI is set")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            return None
        raise AssertionError(
            f"no golden at {path} - run with MM_CONFORMANCE_UPDATE=1 to create it "
            f"(uv run pytest tests/conformance/profiles)"
        )

    existing = path.read_text(encoding="utf-8")
    if existing == rendered:
        return None
    diff = "".join(
        difflib.unified_diff(
            existing.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(path),
            tofile="actual",
        )
    )
    return f"golden mismatch at {path}:\n{diff}"


async def run_scenario(name: str, session: ConformanceSession) -> str | None:
    """Run the scenario named `name` against `session`.

    `None` if the scenario was skipped (a needed identity is missing on this
    combination) or matched its golden; otherwise the mismatch message for the caller
    to collect - `test_profile_conformance.py` runs every scenario for one combination
    and reports every mismatch together, rather than stopping at the first.
    """
    ctx = ScenarioContext(session)
    try:
        await _SCENARIOS[name](ctx)
    except ScenarioSkipped:
        return None
    transcript = Transcript(scenario=name, backend=session.combo.backend, steps=ctx.steps)
    try:
        return compare_or_update(transcript)
    except AssertionError as exc:
        return str(exc)


# --- Delivery check (ADR-0010): instructions, independent of the scenario goldens --


def check_delivery(instructions: str | None, profile: Profile) -> str | None:
    """`None` if `instructions` (an `initialize`/`server/discover` result's own field)
    matches what `profile.delivery_mode` promises; otherwise a mismatch message.
    """
    if profile.delivery_mode == "full" and instructions != INSTRUCTIONS:
        return f"{profile.name}: expected the full INSTRUCTIONS text, got {instructions!r}"
    if profile.delivery_mode == "descriptions" and instructions:
        return f"{profile.name}: expected no instructions, got {instructions!r}"
    if profile.delivery_mode == "short" and instructions != SHORT:
        return f"{profile.name}: expected the SHORT instructions, got {instructions!r}"
    return None

# SPDX-License-Identifier: AGPL-3.0-only
"""Builds a normalised snapshot of the real MCP tool contract and compares two such
snapshots for additive vs. breaking change (#201, ADR-0010, ADR-0017,
`docs/compatibility.md` "Change rules").

`build_snapshot` lists the real server's tools the same way `compat/lint.py`'s own
`list_server_tools` does (in-process, no filesystem/git/database I/O - this module
reuses that function rather than duplicating it) and turns each one into
`{"input": ..., "output": ..., "annotations": ...}`: `input`/`output` are the tool's
`inputSchema`/`outputSchema` after `normalize_schema` below, `annotations` are
`ToolAnnotations.model_dump(by_alias=True)` - wire names (`readOnlyHint`, ...), the
names `docs/compatibility.md` talks about, not Python's `read_only_hint`.

`normalize_schema` does exactly two things, and nothing else:

1. Inlines every local `$ref: "#/$defs/X"` and drops `$defs` - a result type reused by
   two tools, or a list tool's `{"result": [...]}` wrapper (the SDK's own doing, not
   this module's), must not look like a schema *difference* just because pydantic chose
   to factor it out this time and not last time. A `$ref` outside `$defs`, or one that
   cycles back into a definition already being resolved, fails loudly - silently giving
   up on either would make a snapshot that silently stopped comparing something, not a
   missing feature we can live without.
2. Strips `title`/`description` wherever they are JSON Schema *metadata keywords* - a
   schema object's own `title`/`description` key, never a `properties` *name*. This
   matters concretely here: `MemoryIndexEntry`/`MemorySearchResult` both have result
   FIELDS named `title` and `description` (`mcp/server.py`) - those are dict keys under
   `properties`, never touched. Only the metadata key sitting *inside* a property's own
   nested schema (`{"title": "Title", "type": "string"}`, pydantic's default for a
   field literally named `title`) is removed, leaving `{"type": "string"}` - the
   property itself stays. `default` is deliberately kept: unlike `title`/`description`
   it is a contract fact (`docs/compatibility.md`: "changing a default's behaviour" is
   breaking).

Both walks share `compat/lint.py`'s own three keyword-position sets
(`_SCHEMA_SINGLE_KEYWORDS`/`_SCHEMA_LIST_KEYWORDS`/`_SCHEMA_NAME_MAP_KEYWORDS`,
duplicated rather than imported - that module walks `input_schema` only and never
produces a normalised copy, this one walks both schemas and inlines `$ref`, two things
it deliberately does not do) - the same reasoning as there applies verbatim: a
parameter literally named `type` (`memory_index` has one) is a `properties` *name*,
never a keyword position, so it is data here exactly as it is to the linter.

`compare_snapshots(old, new)` is the comparator: every tool/parameter/field/annotation
difference between two snapshots built this way, classified `additive` or `breaking`
per `docs/compatibility.md`'s "Change rules" table. `record_snapshot` is the one
function that ever writes `tests/contract/snapshot.json` - only when
`MM_CONTRACT_UPDATE` is set, refusing outright when `CI` is set, and refusing a
breaking change under plain `MM_CONTRACT_UPDATE=1` unless the deliberate pre-major act
`MM_CONTRACT_UPDATE=breaking` is used instead (pattern:
`tests/conformance_scenarios.py:540-586`'s `compare_or_update`, adapted: that function
only ever *creates* a missing golden and never overwrites one that disagrees: this one
is explicitly allowed to overwrite an existing snapshot, because recording an additive
change on purpose is the whole point of this module).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory_manager.compat.lint import list_server_tools

__all__ = [
    "SNAPSHOT_PATH",
    "Finding",
    "build_snapshot",
    "compare_snapshots",
    "load_snapshot",
    "normalize_schema",
    "record_snapshot",
    "render_snapshot",
]

SNAPSHOT_PATH = Path(__file__).parent / "contract" / "snapshot.json"

_FORMAT = 1

# A schema's own keys whose *value* is one nested schema to recurse into.
_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {"items", "not", "if", "then", "else", "additionalProperties", "contains", "propertyNames"}
)
# A schema's own keys whose *value* is a list of nested schemas to recurse into.
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# A schema's own keys whose *value* maps names (property names, regex patterns, or
# definition names - never keywords themselves) to nested schemas to recurse into.
_SCHEMA_NAME_MAP_KEYWORDS = frozenset({"properties", "patternProperties", "$defs", "definitions"})

# JSON Schema metadata keywords stripped during normalisation - annotation only, never a
# contract fact. Removed only at a keyword position (see the module docstring).
_METADATA_KEYWORDS = frozenset({"title", "description"})

_NULL_SCHEMA: dict[str, Any] = {"type": "null"}

# -- Finding --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One way `new` differs from `old`, classified against `docs/compatibility.md`'s
    "Change rules" table."""

    kind: str  # "additive" | "breaking"
    tool: str
    path: str
    rule: str
    detail: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.kind} {self.tool} {self.path}: {self.rule}{suffix}"


# -- Normalisation ----------------------------------------------------------------------


def _inline_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve every local `$ref: "#/$defs/X"` in `schema`, then drop `$defs`.

    A non-local `$ref` (anything but `#/$defs/<name>`) or a cyclic one (a definition
    that refers back to itself, directly or through another definition already being
    resolved) raises `ValueError` - this module has no use for either, and a comparator
    that quietly gave up on one could not tell "no contract change" from "no comparison
    happened".
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any, resolving: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [resolve(item, resolving) for item in node]
        if not isinstance(node, Mapping):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise ValueError(f"non-local $ref: {ref!r}")
            name = ref.removeprefix("#/$defs/")
            if name not in defs:
                raise ValueError(f"$ref to an unknown definition: {ref!r}")
            if name in resolving:
                raise ValueError(f"cyclic $ref: {ref!r}")
            target = resolve(defs[name], resolving | {name})
            sibling = {key: value for key, value in node.items() if key != "$ref"}
            merged = dict(target)
            merged.update(resolve(sibling, resolving))
            return merged
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "definitions"):
                continue
            if key in _SCHEMA_NAME_MAP_KEYWORDS and isinstance(value, Mapping):
                result[key] = {k: resolve(v, resolving) for k, v in value.items()}
            elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
                result[key] = [resolve(v, resolving) for v in value]
            elif key in _SCHEMA_SINGLE_KEYWORDS and isinstance(value, Mapping):
                result[key] = resolve(value, resolving)
            else:
                result[key] = value
        return result

    return resolve(schema, frozenset())  # type: ignore[no-any-return]


def _strip_metadata(node: Any) -> Any:
    """Remove `title`/`description` wherever they sit at a JSON Schema keyword
    position - never where they are a `properties`/`$defs` *name* (call after
    `_inline_refs`, so no `$ref`/`$defs` are left to walk around)."""
    if isinstance(node, list):
        return [_strip_metadata(item) for item in node]
    if not isinstance(node, Mapping):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _METADATA_KEYWORDS:
            continue
        if key in _SCHEMA_NAME_MAP_KEYWORDS and isinstance(value, Mapping):
            result[key] = {k: _strip_metadata(v) for k, v in value.items()}
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            result[key] = [_strip_metadata(v) for v in value]
        elif key in _SCHEMA_SINGLE_KEYWORDS and isinstance(value, Mapping):
            result[key] = _strip_metadata(value)
        else:
            result[key] = value
    return result


def normalize_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """`schema` with every local `$ref` inlined, `$defs` dropped, and `title`/
    `description` stripped at keyword positions only (see the module docstring)."""
    return _strip_metadata(_inline_refs(schema))  # type: ignore[no-any-return]


# -- Snapshot build ---------------------------------------------------------------------


async def build_snapshot() -> dict[str, Any]:
    """The real server's current tool contract: `{"format": 1, "tools": {name: ...}}`,
    one entry per tool, sorted by name (also guaranteed by `render_snapshot`'s
    `sort_keys=True` - sorting here too keeps anyone iterating this dict directly, such
    as a debugger, seeing the same order)."""
    tools = await list_server_tools()
    entries: dict[str, Any] = {}
    for tool in sorted(tools, key=lambda t: t.name):
        dumped = tool.model_dump(by_alias=True, exclude_none=True)
        entry: dict[str, Any] = {"input": normalize_schema(dumped["inputSchema"])}
        if "outputSchema" in dumped:
            entry["output"] = normalize_schema(dumped["outputSchema"])
        entry["annotations"] = dumped.get("annotations", {})
        entries[tool.name] = entry
    return {"format": _FORMAT, "tools": entries}


def render_snapshot(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, indent=2) + "\n"


def load_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def record_snapshot(
    committed: Mapping[str, Any] | None, current: Mapping[str, Any], path: Path
) -> None:
    """Write `current` to `path` when `MM_CONTRACT_UPDATE` is set - a no-op otherwise.

    `=1` is the ordinary path: refused when comparing `current` against `committed`
    (`None` - no snapshot recorded yet - counts as an empty one) turns up a breaking
    finding; `=breaking` is the deliberate pre-major act that records one on purpose.
    Either value raises `RuntimeError` outright when `CI` is set - a snapshot update is
    never meant to happen unattended.
    """
    mode = os.environ.get("MM_CONTRACT_UPDATE")
    if mode is None:
        return
    if os.environ.get("CI"):
        raise RuntimeError(f"MM_CONTRACT_UPDATE={mode} is refused when CI is set")
    if mode not in ("1", "breaking"):
        raise RuntimeError(f"unknown MM_CONTRACT_UPDATE={mode!r}, expected '1' or 'breaking'")
    if mode == "1":
        baseline = committed if committed is not None else {"format": _FORMAT, "tools": {}}
        breaking = [f for f in compare_snapshots(baseline, current) if f.kind == "breaking"]
        if breaking:
            details = "\n".join(str(f) for f in breaking)
            raise RuntimeError(
                "MM_CONTRACT_UPDATE=1 refuses to record a breaking change to the tool "
                "contract - use MM_CONTRACT_UPDATE=breaking if this is the deliberate "
                f"pre-major act:\n{details}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_snapshot(current), encoding="utf-8")


# -- Comparator ---------------------------------------------------------------------


def compare_snapshots(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[Finding]:
    """Every way `new` differs from `old`, classified `additive`/`breaking` against
    `docs/compatibility.md`'s "Change rules" table."""
    findings: list[Finding] = []
    old_tools: Mapping[str, Any] = old.get("tools", {})
    new_tools: Mapping[str, Any] = new.get("tools", {})
    for name in sorted(set(old_tools) | set(new_tools)):
        if name not in new_tools:
            findings.append(Finding("breaking", name, "(tool)", "tool_removed"))
            continue
        if name not in old_tools:
            findings.append(Finding("additive", name, "(tool)", "tool_added"))
            continue
        old_tool = old_tools[name]
        new_tool = new_tools[name]
        _compare_node(
            name, old_tool.get("input", {}), new_tool.get("input", {}), "input", "input", findings
        )
        _compare_output_schema(name, old_tool.get("output"), new_tool.get("output"), findings)
        _compare_annotations(
            name, old_tool.get("annotations", {}), new_tool.get("annotations", {}), findings
        )
    findings.sort(key=lambda f: (f.tool, f.path, f.rule, f.kind))
    return findings


def _compare_output_schema(
    tool: str,
    old_schema: Mapping[str, Any] | None,
    new_schema: Mapping[str, Any] | None,
    findings: list[Finding],
) -> None:
    if old_schema is None and new_schema is None:
        return
    if old_schema is None:
        findings.append(Finding("additive", tool, "output", "output_schema_added"))
        return
    if new_schema is None:
        findings.append(Finding("breaking", tool, "output", "output_schema_removed"))
        return
    _compare_node(tool, old_schema, new_schema, "output", "output", findings)


# Keys `_compare_node` itself resolves (recursed into or compared directly) - anything
# else left over after that, and still different, is an unrecognised schema keyword
# change: fail-closed, `docs/compatibility.md`'s catch-all ("any other schema keyword
# added/removed/changed -> breaking").
_HANDLED_KEYS = frozenset({"type", "properties", "required", "items", "anyOf", "enum", "default"})
# Ignored outright, same as `normalize_schema`'s own `_METADATA_KEYWORDS`/`$defs`
# handling - "title/description/$defs names ignored" is a comparator guarantee too, not
# just a normalisation step: a caller that feeds this module an un-normalised schema
# must not get a false "breaking" on metadata that was never a contract fact.
_IGNORED_KEYS = frozenset({"title", "description", "$defs", "definitions"})


def _compare_node(
    tool: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    path: str,
    direction: str,
    findings: list[Finding],
) -> None:
    """Compare one schema node. `direction` is `"input"` (a tool parameter - the
    caller's burden grows when something becomes required) or `"output"` (a result
    field - the server's guarantee grows when something becomes required); every rule
    below that depends on direction follows `docs/compatibility.md`'s table, which
    frames each one from exactly that angle.
    """
    if old == new:
        return
    if "properties" in old or "properties" in new:
        _compare_properties(tool, old, new, path, direction, findings)
    if "items" in old or "items" in new:
        _compare_items(tool, old, new, path, direction, findings)
    _compare_type_or_anyof(tool, old, new, path, direction, findings)
    _compare_enum(tool, old, new, path, findings)
    _compare_leftover_keywords(tool, old, new, path, findings)


def _compare_properties(
    tool: str,
    old_node: Mapping[str, Any],
    new_node: Mapping[str, Any],
    path: str,
    direction: str,
    findings: list[Finding],
) -> None:
    old_props: Mapping[str, Any] = old_node.get("properties") or {}
    new_props: Mapping[str, Any] = new_node.get("properties") or {}
    old_required: set[str] = set(old_node.get("required") or [])
    new_required: set[str] = set(new_node.get("required") or [])
    for name in sorted(set(old_props) | set(new_props)):
        child_path = f"{path}.{name}"
        if name not in new_props:
            findings.append(Finding("breaking", tool, child_path, "removed"))
            continue
        if name not in old_props:
            is_required = name in new_required
            if direction == "input":
                kind = "breaking" if is_required else "additive"
            else:
                kind = "additive"
            findings.append(Finding(kind, tool, child_path, "added"))
            continue
        was_required = name in old_required
        is_required = name in new_required
        if was_required != is_required:
            if direction == "input":
                kind = "breaking" if is_required else "additive"
            else:
                kind = "breaking" if not is_required else "additive"
            findings.append(Finding(kind, tool, child_path, "required_changed"))
        _compare_default(tool, old_props[name], new_props[name], child_path, is_required, findings)
        _compare_node(tool, old_props[name], new_props[name], child_path, direction, findings)


def _compare_default(
    tool: str,
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    path: str,
    is_required_new: bool,
    findings: list[Finding],
) -> None:
    has_old = "default" in old_schema
    has_new = "default" in new_schema
    if not has_old and not has_new:
        return
    if has_old and has_new:
        if old_schema["default"] != new_schema["default"]:
            findings.append(Finding("breaking", tool, path, "default_changed"))
        return
    if has_old and not has_new:
        findings.append(Finding("breaking", tool, path, "default_removed"))
        return
    kind = "additive" if not is_required_new else "breaking"
    findings.append(Finding(kind, tool, path, "default_added"))


def _compare_items(
    tool: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    path: str,
    direction: str,
    findings: list[Finding],
) -> None:
    old_items = old.get("items")
    new_items = new.get("items")
    if old_items is None and new_items is None:
        return
    if old_items is None or new_items is None:
        findings.append(Finding("breaking", tool, path, "type_changed", "items added/removed"))
        return
    _compare_node(tool, old_items, new_items, f"{path}[]", direction, findings)


def _unordered_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    if len(left) != len(right):
        return False
    remaining = list(right)
    for item in left:
        if item in remaining:
            remaining.remove(item)
        else:
            return False
    return True


def _alternatives(node: Mapping[str, Any]) -> list[Any] | None:
    """`node`'s type alternatives, whichever of the two JSON Schema shapes expresses
    them: an explicit `anyOf` list, or a single `type` keyword synthesised into a
    one-branch list - so a plain `{"type": "string"}` becoming `{"anyOf": [{"type":
    "string"}, {"type": "null"}]}` compares as "a null alternative was added", not as
    "anyOf was introduced" (`None` either way means neither keyword is present at all).
    """
    if "anyOf" in node:
        return list(node["anyOf"])
    if "type" in node:
        return [{"type": node["type"]}]
    return None


def _compare_type_or_anyof(
    tool: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    path: str,
    direction: str,
    findings: list[Finding],
) -> None:
    old_alts = _alternatives(old)
    new_alts = _alternatives(new)
    if old_alts is None and new_alts is None:
        return
    if old_alts is None or new_alts is None:
        findings.append(
            Finding("breaking", tool, path, "type_changed", "type/anyOf introduced or removed")
        )
        return
    old_null = _NULL_SCHEMA in old_alts
    new_null = _NULL_SCHEMA in new_alts
    old_rest = [branch for branch in old_alts if branch != _NULL_SCHEMA]
    new_rest = [branch for branch in new_alts if branch != _NULL_SCHEMA]
    if not _unordered_equal(old_rest, new_rest):
        findings.append(
            Finding("breaking", tool, path, "type_changed", "anyOf/type branch changed")
        )
        return
    if old_null != new_null:
        if direction == "input":
            kind = "additive" if new_null and not old_null else "breaking"
        else:
            # `docs/compatibility.md`: "any output type/anyOf change (incl. adding
            # null) -> breaking" - unlike input, widening what a *result* may be is
            # still breaking for a caller that already trusted the old type.
            kind = "breaking"
        findings.append(Finding(kind, tool, path, "null_alternative_changed"))


def _compare_enum(
    tool: str, old: Mapping[str, Any], new: Mapping[str, Any], path: str, findings: list[Finding]
) -> None:
    old_enum = old.get("enum")
    new_enum = new.get("enum")
    if old_enum == new_enum:
        return
    if old_enum is None:
        findings.append(Finding("breaking", tool, path, "enum_introduced"))
        return
    if new_enum is None:
        findings.append(Finding("additive", tool, path, "enum_dropped"))
        return
    old_set, new_set = set(old_enum), set(new_enum)
    removed = old_set - new_set
    added = new_set - old_set
    if removed:
        detail = ", ".join(sorted(str(value) for value in removed))
        findings.append(Finding("breaking", tool, path, "enum_values_removed", detail))
    elif added:
        detail = ", ".join(sorted(str(value) for value in added))
        findings.append(Finding("additive", tool, path, "enum_values_added", detail))


def _compare_leftover_keywords(
    tool: str, old: Mapping[str, Any], new: Mapping[str, Any], path: str, findings: list[Finding]
) -> None:
    skip = _HANDLED_KEYS | _IGNORED_KEYS
    old_other = {key: value for key, value in old.items() if key not in skip}
    new_other = {key: value for key, value in new.items() if key not in skip}
    if old_other != new_other:
        changed = sorted(set(old_other) | set(new_other))
        detail = ", ".join(changed)
        findings.append(Finding("breaking", tool, path, "schema_keyword_changed", detail))


# `ToolAnnotations`'s own spec defaults (mcp_types.ToolAnnotations docstrings) - a
# missing hint means exactly this, never "unknown"/"no opinion".
_ANNOTATION_DEFAULTS: dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

# Each hint's own (from, to) pair that `docs/compatibility.md` calls breaking ("making
# an annotation more permissive"); the reverse direction is additive, no change is
# neither.
_ANNOTATION_BREAKING_DIRECTION: dict[str, tuple[bool, bool]] = {
    "readOnlyHint": (True, False),
    "destructiveHint": (False, True),
    "idempotentHint": (True, False),
    "openWorldHint": (False, True),
}


def _compare_annotations(
    tool: str,
    old_annotations: Mapping[str, Any],
    new_annotations: Mapping[str, Any],
    findings: list[Finding],
) -> None:
    for hint, default in _ANNOTATION_DEFAULTS.items():
        old_value = old_annotations.get(hint, default)
        new_value = new_annotations.get(hint, default)
        if old_value == new_value:
            continue
        breaking_from, breaking_to = _ANNOTATION_BREAKING_DIRECTION[hint]
        kind = "breaking" if (old_value, new_value) == (breaking_from, breaking_to) else "additive"
        findings.append(Finding(kind, tool, f"annotations.{hint}", "annotation_changed"))

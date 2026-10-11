# SPDX-License-Identifier: AGPL-3.0-only
"""Mutation tests for `contract_snapshot.compare_snapshots` against every row of
`docs/compatibility.md`'s "Change rules" table (#201, ADR-0010, ADR-0017).

Each test builds two synthetic snapshots (`_snapshot`/`_tool` below - same style as
`tests/compat/test_lint.py`'s `_tool`/`_profile` factories) that differ in exactly one
way, and asserts `compare_snapshots(old, new)` reports exactly the finding the table
promises for that change - never the real server, which is `tests/contract/
test_snapshot.py`'s job. The one exception is `test_removing_memory_search_limit_is_a_
breaking_finding`: the mandatory "param removed" check runs against a mutated copy of
the actual recorded `tests/contract/snapshot.json`, because that specific regression
(`src/memory_manager/mcp/server.py` dropping `memory_search`'s `limit` parameter) is
the one `Fertig wenn` names explicitly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from contract_snapshot import (
    SNAPSHOT_PATH,
    Finding,
    compare_snapshots,
    load_snapshot,
    record_snapshot,
)

# The read-tool shape `mcp/server.py`'s `_READ_TOOL_ANNOTATIONS` has - every synthetic
# tool below gets this by default so a test about one hint does not also have to spell
# out the other three.
_FULL_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _tool(
    *,
    input_properties: dict[str, Any] | None = None,
    input_required: list[str] | None = None,
    output: dict[str, Any] | None = None,
    annotations: dict[str, bool] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "input": {"type": "object", "properties": input_properties or {}},
        "annotations": _FULL_ANNOTATIONS if annotations is None else annotations,
    }
    if input_required:
        entry["input"]["required"] = input_required
    if output is not None:
        entry["output"] = output
    return entry


def _snapshot(tools: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"format": 1, "tools": tools}


def _only(findings: list[Finding], *, kind: str | None = None) -> list[Finding]:
    if kind is None:
        return findings
    return [finding for finding in findings if finding.kind == kind]


# -- Tool-level -------------------------------------------------------------------------


def test_a_new_tool_is_additive() -> None:
    old = _snapshot({})
    new = _snapshot({"memory_new": _tool()})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.tool, f.rule) for f in findings] == [
        ("additive", "memory_new", "tool_added")
    ]


def test_a_removed_tool_is_breaking() -> None:
    old = _snapshot({"memory_old": _tool()})
    new = _snapshot({})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.tool, f.rule) for f in findings] == [
        ("breaking", "memory_old", "tool_removed")
    ]


# -- Input parameters ---------------------------------------------------------------------


def test_a_new_optional_input_param_is_additive() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    new = _snapshot(
        {
            "t": _tool(
                input_properties={"a": {"type": "string"}, "b": {"type": "string"}},
                input_required=["a"],
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [("additive", "input.b", "added")]


def test_a_new_required_input_param_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    new = _snapshot(
        {
            "t": _tool(
                input_properties={"a": {"type": "string"}, "b": {"type": "string"}},
                input_required=["a", "b"],
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [("breaking", "input.b", "added")]


def test_a_removed_or_renamed_input_param_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    new = _snapshot(
        {"t": _tool(input_properties={"renamed": {"type": "string"}}, input_required=["renamed"])}
    )
    findings = compare_snapshots(old, new)
    breaking = _only(findings, kind="breaking")
    assert ("input.a", "removed") in [(f.path, f.rule) for f in breaking]


def test_input_optional_to_required_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "required_changed")
    ]


def test_input_required_to_optional_is_additive() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "input.a", "required_changed")
    ]


def test_input_null_alternative_added_is_additive() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    new = _snapshot(
        {"t": _tool(input_properties={"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}})}
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "input.a", "null_alternative_changed")
    ]


def test_input_null_alternative_removed_is_breaking() -> None:
    old = _snapshot(
        {"t": _tool(input_properties={"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}})}
    )
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "null_alternative_changed")
    ]


def test_input_any_other_type_change_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "integer"}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [("breaking", "input.a", "type_changed")]


def test_input_enum_values_added_is_additive() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "enum": ["x"]}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "enum": ["x", "y"]}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "input.a", "enum_values_added")
    ]


def test_input_enum_dropped_entirely_is_additive() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "enum": ["x", "y"]}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [("additive", "input.a", "enum_dropped")]


def test_input_enum_values_removed_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "enum": ["x", "y"]}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "enum": ["x"]}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "enum_values_removed")
    ]


def test_input_enum_introduced_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "enum": ["x"]}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "enum_introduced")
    ]


def test_input_default_changed_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "integer", "default": 1}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "integer", "default": 2}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "default_changed")
    ]


def test_input_default_removed_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "integer", "default": 1}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "integer"}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "default_removed")
    ]


def test_input_default_added_while_optional_is_additive() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "integer"}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "integer", "default": 1}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "input.a", "default_added")
    ]


# -- Output fields ------------------------------------------------------------------------


def _object_output(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def test_a_new_output_field_is_additive_regardless_of_required() -> None:
    old = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    new = _snapshot(
        {
            "t": _tool(
                output=_object_output(
                    {"a": {"type": "string"}, "b": {"type": "string"}}, ["a", "b"]
                )
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [("additive", "output.b", "added")]


def test_a_removed_or_renamed_output_field_is_breaking() -> None:
    old = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    new = _snapshot(
        {"t": _tool(output=_object_output({"renamed": {"type": "string"}}, ["renamed"]))}
    )
    findings = compare_snapshots(old, new)
    breaking = _only(findings, kind="breaking")
    assert ("output.a", "removed") in [(f.path, f.rule) for f in breaking]


def test_output_required_to_not_required_is_breaking() -> None:
    old = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    new = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, []))})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "output.a", "required_changed")
    ]


def test_output_not_required_to_required_is_additive() -> None:
    old = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, []))})
    new = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "output.a", "required_changed")
    ]


def test_output_type_change_including_adding_null_is_breaking() -> None:
    old = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    new = _snapshot(
        {
            "t": _tool(
                output=_object_output(
                    {"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, ["a"]
                )
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "output.a", "null_alternative_changed")
    ]


def test_output_enum_values_added_is_additive() -> None:
    old = _snapshot(
        {"t": _tool(output=_object_output({"a": {"type": "string", "enum": ["x"]}}, ["a"]))}
    )
    new = _snapshot(
        {"t": _tool(output=_object_output({"a": {"type": "string", "enum": ["x", "y"]}}, ["a"]))}
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "output.a", "enum_values_added")
    ]


def test_output_enum_values_removed_is_breaking() -> None:
    old = _snapshot(
        {"t": _tool(output=_object_output({"a": {"type": "string", "enum": ["x", "y"]}}, ["a"]))}
    )
    new = _snapshot(
        {"t": _tool(output=_object_output({"a": {"type": "string", "enum": ["x"]}}, ["a"]))}
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "output.a", "enum_values_removed")
    ]


def test_items_and_nested_objects_recurse_in_the_same_direction() -> None:
    old = _snapshot(
        {
            "t": _tool(
                output=_object_output(
                    {
                        "results": {
                            "type": "array",
                            "items": _object_output({"id": {"type": "string"}}, ["id"]),
                        }
                    },
                    ["results"],
                )
            )
        }
    )
    new = _snapshot(
        {
            "t": _tool(
                output=_object_output(
                    {
                        "results": {
                            "type": "array",
                            "items": _object_output({}, []),
                        }
                    },
                    ["results"],
                )
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "output.results[].id", "removed")
    ]


def test_output_schema_newly_added_is_additive() -> None:
    old = _snapshot({"t": _tool()})
    new = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "output", "output_schema_added")
    ]


def test_output_schema_removed_is_breaking() -> None:
    old = _snapshot({"t": _tool(output=_object_output({"a": {"type": "string"}}, ["a"]))})
    new = _snapshot({"t": _tool()})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "output", "output_schema_removed")
    ]


# -- Annotations --------------------------------------------------------------------------


def test_read_only_hint_true_to_false_is_breaking() -> None:
    old = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "readOnlyHint": True})})
    new = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "readOnlyHint": False})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "annotations.readOnlyHint", "annotation_changed")
    ]


def test_read_only_hint_false_to_true_is_additive() -> None:
    old = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "readOnlyHint": False})})
    new = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "readOnlyHint": True})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("additive", "annotations.readOnlyHint", "annotation_changed")
    ]


def test_destructive_hint_false_to_true_is_breaking() -> None:
    old = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "destructiveHint": False})})
    new = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "destructiveHint": True})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "annotations.destructiveHint", "annotation_changed")
    ]


def test_idempotent_hint_true_to_false_is_breaking() -> None:
    old = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "idempotentHint": True})})
    new = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "idempotentHint": False})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "annotations.idempotentHint", "annotation_changed")
    ]


def test_open_world_hint_false_to_true_is_breaking() -> None:
    old = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "openWorldHint": False})})
    new = _snapshot({"t": _tool(annotations={**_FULL_ANNOTATIONS, "openWorldHint": True})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "annotations.openWorldHint", "annotation_changed")
    ]


def test_missing_annotation_falls_back_to_the_spec_default_before_comparing() -> None:
    # Old carries no annotations at all; the spec default for `destructiveHint` is
    # `true`. New explicitly sets it to `true` too - no finding, because the comparator
    # must treat "missing" as that default, not as "unknown"/always-different.
    old = _snapshot({"t": _tool(annotations={})})
    new = _snapshot({"t": _tool(annotations={"destructiveHint": True})})
    assert compare_snapshots(old, new) == []
    # The opposite value against the same missing-old baseline is still the documented
    # breaking direction (false -> true for destructiveHint).
    other = _snapshot({"t": _tool(annotations={"destructiveHint": False})})
    findings = compare_snapshots(old, other)
    assert [(f.kind, f.rule) for f in findings] == [("additive", "annotation_changed")]


# -- Fail-closed, and the noise this module must ignore ----------------------------------


def test_any_other_schema_keyword_change_is_breaking() -> None:
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}})})
    new = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "pattern": "^x"}})})
    findings = compare_snapshots(old, new)
    assert [(f.kind, f.path, f.rule) for f in findings] == [
        ("breaking", "input.a", "schema_keyword_changed")
    ]


def test_title_and_description_differences_produce_zero_findings() -> None:
    # Simulates what a *pre-normalisation* schema would carry (this module's input is
    # always already-normalised, but the comparator itself must not special-case
    # "title"/"description" back in - it simply never sees them unless something feeds
    # it a schema that was not normalised, which this test does on purpose to prove
    # the comparator itself does not re-introduce a dependency on them).
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string", "title": "Old Title"}})})
    new = _snapshot(
        {
            "t": _tool(
                input_properties={
                    "a": {"type": "string", "title": "New Title", "description": "new"}
                }
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert findings == []


def test_additive_counter_probes_produce_zero_breaking_findings() -> None:
    old = _snapshot(
        {"t": _tool(input_properties={"a": {"type": "string"}, "b": {"type": "string"}})}
    )
    new = _snapshot(
        {
            "t": _tool(
                input_properties={
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "c": {"type": "string"},
                },
            )
        }
    )
    findings = compare_snapshots(old, new)
    assert _only(findings, kind="breaking") == []
    assert [(f.kind, f.path, f.rule) for f in findings] == [("additive", "input.c", "added")]


# -- The mandatory regression: memory_search losing `limit` -----------------------------


def test_removing_memory_search_limit_is_a_breaking_finding() -> None:
    committed = load_snapshot(SNAPSHOT_PATH)
    assert committed is not None, f"no recorded snapshot at {SNAPSHOT_PATH} to mutate"
    mutated = copy.deepcopy(committed)
    del mutated["tools"]["memory_search"]["input"]["properties"]["limit"]

    findings = compare_snapshots(committed, mutated)
    breaking = _only(findings, kind="breaking")

    assert ("memory_search", "input.limit", "removed") in [
        (f.tool, f.path, f.rule) for f in breaking
    ]


# -- record_snapshot / CI refusal --------------------------------------------------------


def test_record_snapshot_is_a_no_op_without_mm_contract_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MM_CONTRACT_UPDATE", raising=False)
    path = tmp_path / "snapshot.json"
    record_snapshot(None, _snapshot({}), path)
    assert not path.exists()


def test_record_snapshot_refuses_when_ci_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MM_CONTRACT_UPDATE", "1")
    monkeypatch.setenv("CI", "1")
    with pytest.raises(RuntimeError, match="CI"):
        record_snapshot(_snapshot({}), _snapshot({}), tmp_path / "snapshot.json")


def test_record_snapshot_refuses_a_breaking_change_under_plain_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MM_CONTRACT_UPDATE", "1")
    monkeypatch.delenv("CI", raising=False)
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    new = _snapshot({"t": _tool()})  # "a" removed: breaking
    with pytest.raises(RuntimeError, match="MM_CONTRACT_UPDATE=breaking"):
        record_snapshot(old, new, tmp_path / "snapshot.json")


def test_record_snapshot_breaking_mode_writes_despite_a_breaking_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MM_CONTRACT_UPDATE", "breaking")
    monkeypatch.delenv("CI", raising=False)
    old = _snapshot({"t": _tool(input_properties={"a": {"type": "string"}}, input_required=["a"])})
    new = _snapshot({"t": _tool()})
    path = tmp_path / "snapshot.json"

    record_snapshot(old, new, path)

    assert json.loads(path.read_text(encoding="utf-8")) == new

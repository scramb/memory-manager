# SPDX-License-Identifier: AGPL-3.0-only
"""Text-level JSON merge for client configs shaped like `{"mcpServers": {...}}` - Claude
Code's `~/.claude.json` and `.mcp.json` (#137, docs/research/clients/claude-code.md).

Deliberately a surgical merge rather than a plain `json.loads`/`json.dumps` round trip of the
whole file: an existing entry keeps every key this module does not manage (`timeout`,
`oauth`, ...), and a file this module has not touched yet keeps its own indent width and
trailing-newline state untouched. `json.dumps`/`json.loads` only back the shape check and the
one entry being merged, not a full reformat - a mismatch between the original bytes and what a
plain reserialisation would produce (non-standard spacing the parser does not preserve) is
only ever a printed warning, never a silent rewrite of parts the caller did not ask to change.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

__all__ = ["ClientConfigError", "merge_server_entry"]

_INDENT_RE = re.compile(r"\n([ \t]+)\S")
_DEFAULT_INDENT = "  "


class ClientConfigError(ValueError):
    """`current_text` is not a client config this module can safely merge into."""


def merge_server_entry(
    current_text: str | None,
    *,
    name: str,
    entry: dict[str, Any],
    managed_keys: tuple[str, ...],
) -> str:
    """Merge `entry` into `mcpServers[name]` of `current_text`, returning the new full text.

    `current_text is None` renders a fresh `{"mcpServers": {...}}` document. An existing entry
    under `name` keeps every key outside `managed_keys` untouched; a `managed_keys` key absent
    from `entry` is removed from the existing one (for example a stale `headers` left over
    from a transport that no longer sets one). The result is `current_text` unchanged, byte
    for byte, when nothing would actually change.

    Raises `ClientConfigError` if `current_text` does not parse as a JSON object, has a
    duplicate key anywhere, or has a non-object `mcpServers`.
    """
    data: dict[str, Any] = {} if current_text is None else _parse(current_text)

    servers_obj = data.get("mcpServers")
    if servers_obj is not None and not isinstance(servers_obj, dict):
        raise ClientConfigError("'mcpServers' must be an object")
    servers: dict[str, Any] = servers_obj if isinstance(servers_obj, dict) else {}
    existing = servers.get(name)

    if isinstance(existing, dict) and existing.get("type") == entry.get("type"):
        merged_entry = dict(existing)
        for key in managed_keys:
            if key in entry:
                merged_entry[key] = entry[key]
            else:
                merged_entry.pop(key, None)
    else:
        merged_entry = dict(entry)

    if current_text is not None and existing == merged_entry:
        return current_text

    servers[name] = merged_entry
    data["mcpServers"] = servers
    return _render(data, current_text)


def _parse(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ClientConfigError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ClientConfigError("the top-level JSON value must be an object")
    return data


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ClientConfigError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _detect_indent(text: str) -> str:
    match = _INDENT_RE.search(text)
    return match.group(1) if match else _DEFAULT_INDENT


def _render(data: dict[str, Any], current_text: str | None) -> str:
    if current_text is None:
        return json.dumps(data, indent=_DEFAULT_INDENT, ensure_ascii=False)

    indent = _detect_indent(current_text)
    _warn_if_not_round_trip(current_text, indent)
    body = json.dumps(data, indent=indent, ensure_ascii=False)
    return body + "\n" if current_text.endswith("\n") else body


def _warn_if_not_round_trip(current_text: str, indent: str) -> None:
    try:
        original = json.loads(current_text)
    except json.JSONDecodeError:
        return
    expected = json.dumps(original, indent=indent, ensure_ascii=False)
    if current_text.endswith("\n"):
        expected += "\n"
    if expected != current_text:
        print(
            "memory-manager: note: this config's existing formatting does not round-trip "
            "byte-exact; parts of it outside the memory-manager entry will be normalised on "
            "write.",
            file=sys.stderr,
        )

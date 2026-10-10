# SPDX-License-Identifier: AGPL-3.0-only
"""Claude Code's `ClientAdapter` (#137, #138): user scope merges into `~/.claude.json`
(`$CLAUDE_CONFIG_DIR/.claude.json` when set), project scope into `<project>/.mcp.json`, local
scope into that same `~/.claude.json`'s `projects[<abs project_dir>].mcpServers` instead - all
three the same `{"mcpServers": {...}}` shape (docs/research/clients/claude-code.md). The
optional `.claude/rules/memory-manager.md` instructions file carries the same generated
snippet as `integrations/claude-code/CLAUDE.snippet.md` (`guide/targets.py`), rendered from
the `INSTRUCTIONS` constant rather than read from that file, so it is correct from an
installed wheel too, without a repository checkout.

`read_entry` (#138) expands `${VAR}`/`${VAR:-default}` inside `url`, `headers`, `command`,
`args` and `env` values exactly like Claude Code itself does (docs/research/clients/
claude-code.md: "an unset VAR is left as the literal `${VAR}` string plus a warning, it does
not blank out") - an entry with no recognizable `type` is read as `None`, the same "silently
skipped" shape `merge`'s own docstring already notes for a broken stdio entry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from memory_manager.clients.base import MANAGED_KEYS, SERVER_NAME, ClientEntry, ServerEntry
from memory_manager.clients.jsonconfig import merge_server_entry, read_server_entry
from memory_manager.guide.targets import TARGETS, render_target
from memory_manager.mcp.instructions import INSTRUCTIONS

__all__ = ["ClaudeCodeAdapter", "instructions_content"]

#: Local > project > user (docs/research/clients/claude-code.md) - the order `read_entry`'s
#: caller (`doctor --client`) tries scopes in, highest precedence first.
_SCOPES: tuple[str, ...] = ("local", "project", "user")

# `${VAR}` or `${VAR:-default}` - group 1 is the variable name, group 2 (if present at all)
# is the default's text, which may itself be empty (`${VAR:-}`).
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# A whole header value that is exactly one `${VAR}`/`${VAR:-default}` placeholder and
# nothing else - what `_token_env_of` looks for in a raw (pre-expansion) `Authorization`
# value to name the env var a 401 hint should point at.
_WHOLE_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}$")


class ClaudeCodeAdapter:
    """Merges the memory-manager server into Claude Code's own config file."""

    name = "claude-code"
    client_info_name: str | None = "claude-code"
    scopes: tuple[str, ...] = _SCOPES

    def locate_config(
        self, *, scope: str, home: Path, project_dir: Path, env: Mapping[str, str]
    ) -> Path:
        if scope == "project":
            return project_dir / ".mcp.json"
        config_dir = env.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            return Path(config_dir) / ".claude.json"
        return home / ".claude.json"

    def merge(self, current_text: str | None, entry: ServerEntry) -> str:
        return merge_server_entry(
            current_text, name=SERVER_NAME, entry=entry.as_json(), managed_keys=MANAGED_KEYS
        )

    def instructions_file(self, project_dir: Path) -> Path:
        return project_dir / ".claude" / "rules" / "memory-manager.md"

    def web_steps(self, url: str) -> None:
        return None

    def read_entry(
        self, text: str, *, scope: str, project_dir: Path, env: Mapping[str, str]
    ) -> ClientEntry | None:
        raw = read_server_entry(text, name=SERVER_NAME, scope=scope, project_dir=project_dir)
        if raw is None:
            return None
        entry_type = raw.get("type")
        if entry_type not in ("http", "stdio"):
            return None

        unresolved: list[str] = []
        token_env = _token_env_of(raw.get("headers"))

        if entry_type == "http":
            url = _expand(raw.get("url"), env, unresolved)
            headers = _expand_mapping(raw.get("headers"), env, unresolved)
            return ClientEntry(
                transport="http",
                scope=scope,
                url=url,
                headers=headers,
                unresolved_vars=tuple(unresolved),
                token_env=token_env,
            )

        command = _expand(raw.get("command"), env, unresolved)
        args_raw = raw.get("args")
        args = (
            tuple(_substitute(item, env, unresolved) for item in args_raw if isinstance(item, str))
            if isinstance(args_raw, list)
            else ()
        )
        entry_env = _expand_mapping(raw.get("env"), env, unresolved)
        return ClientEntry(
            transport="stdio",
            scope=scope,
            command=command,
            args=args,
            env=entry_env,
            unresolved_vars=tuple(unresolved),
        )


def _substitute(value: str, env: Mapping[str, str], unresolved: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        if var_name in env:
            return env[var_name]
        if default is not None:
            return default
        if var_name not in unresolved:
            unresolved.append(var_name)
        return match.group(0)

    return _VAR_RE.sub(repl, value)


def _expand(value: Any, env: Mapping[str, str], unresolved: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    return _substitute(value, env, unresolved)


def _expand_mapping(value: Any, env: Mapping[str, str], unresolved: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: expanded
        for key, raw_value in value.items()
        if isinstance(key, str) and (expanded := _expand(raw_value, env, unresolved)) is not None
    }


def _token_env_of(headers: Any) -> str | None:
    if not isinstance(headers, dict):
        return None
    authorization = headers.get("Authorization")
    if not isinstance(authorization, str):
        return None
    match = re.search(r"\bBearer\s+(\S+)$", authorization)
    if match is None:
        return None
    whole = _WHOLE_VAR_RE.match(match.group(1))
    return whole.group(1) if whole is not None else None


def instructions_content() -> str:
    """The rendered `.claude/rules/memory-manager.md` body, byte-identical to
    `integrations/claude-code/CLAUDE.snippet.md`."""
    return render_target(TARGETS["claude-code"], {"instructions": INSTRUCTIONS})

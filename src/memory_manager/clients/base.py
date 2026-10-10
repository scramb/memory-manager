# SPDX-License-Identifier: AGPL-3.0-only
"""Shared types for `connect <client>` (#137): one `ClientAdapter` per supported client, and
the `ServerEntry` it turns into that client's own config shape.

`ClientAdapter.merge` works at text level on purpose - `current_text | None` in, the whole new
file content out - rather than returning a parsed structure: a later TOML/JSONC client needs
the same kind of surgical splice, and a round trip through a generic data structure would not
keep comments, unknown per-entry keys, or key order the way a text-level merge can.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

__all__ = [
    "DEFAULT_TOKEN_ENV",
    "MANAGED_KEYS",
    "SERVER_NAME",
    "ClientAdapter",
    "ServerEntry",
]

# The name every adapter merges its entry under.
SERVER_NAME = "memory-manager"

# What `--token-env`/`--inline-token` default to when given without naming a variable - never
# a credential-shaped name like `ANTHROPIC_API_KEY` (read as empty towards a remote MCP server,
# docs/research/clients/claude-code.md).
DEFAULT_TOKEN_ENV = "MEMORY_MANAGER_TOKEN"  # noqa: S105 - an env var name, not a credential

# Every key an adapter's `merge` may add, change or remove on the entry it owns. Anything else
# already on an existing entry (`timeout`, `oauth`, ...) is left untouched.
MANAGED_KEYS: tuple[str, ...] = ("type", "url", "headers", "command", "args", "env")


@dataclass(frozen=True)
class ServerEntry:
    """The memory-manager MCP server entry an adapter merges into a client's config.

    `inline_token_value`, when set, is the current value read from `token_env` at connect
    time, inserted literally instead of the usual `${token_env}` placeholder - never passed on
    the command line itself (CLAUDE.md: secrets never as CLI argument values).
    """

    name: str
    transport: Literal["http", "stdio"]
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    token_env: str | None = None
    inline_token_value: str | None = None

    def as_json(self) -> dict[str, Any]:
        """This entry's JSON shape, as Claude Code's config documents it."""
        if self.transport == "http":
            entry: dict[str, Any] = {"type": "http", "url": self.url}
            if self.token_env is not None:
                token = (
                    self.inline_token_value
                    if self.inline_token_value is not None
                    else f"${{{self.token_env}}}"
                )
                entry["headers"] = {"Authorization": f"Bearer {token}"}
            return entry
        return {"type": "stdio", "command": self.command, "args": list(self.args)}


class ClientAdapter(Protocol):
    """One supported client's config format and setup flow."""

    name: str

    def locate_config(
        self, *, scope: str, home: Path, project_dir: Path, env: Mapping[str, str]
    ) -> Path | None:
        """Where this client's config for `scope` lives, or `None` for a client with none
        (claude.ai, which only has a remote connector dialog)."""

    def merge(self, current_text: str | None, entry: ServerEntry) -> str:
        """The new full text of the config after merging `entry` in (`current_text is None`
        for a config file that does not exist yet)."""

    def instructions_file(self, project_dir: Path) -> Path | None:
        """Where this client's own usage-rules file lives, or `None` if it has none."""

    def web_steps(self, url: str) -> list[str] | None:
        """Manual setup steps for a client with no local config to merge into, or `None` for
        a client this adapter merges a config file for instead."""

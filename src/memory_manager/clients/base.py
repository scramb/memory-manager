# SPDX-License-Identifier: AGPL-3.0-only
"""Shared types for `connect <client>` (#137) and `doctor --client` (#138): one
`ClientAdapter` per supported client, the `ServerEntry` it turns into that client's own
config shape, and the `ClientEntry` it reads one back as.

`ClientAdapter.merge` works at text level on purpose - `current_text | None` in, the whole new
file content out - rather than returning a parsed structure: a later TOML/JSONC client needs
the same kind of surgical splice, and a round trip through a generic data structure would not
keep comments, unknown per-entry keys, or key order the way a text-level merge can.

`ClientAdapter.read_entry` is the other direction `doctor --client` needs: given the raw text
`merge` would have read, and the environment a running client would expand its own `${VAR}`
syntax against, return the memory-manager entry fully resolved to what that client would
actually send on the wire - URL, headers, command, args, env. Expansion rules differ per
client (docs/research/clients/<name>.md), so this lives on the adapter rather than in one
shared parser: a client without `${VAR}` substitution at all (a future one) simply does not
call any expansion helper, instead of a shared function growing a per-client branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

__all__ = [
    "DEFAULT_TOKEN_ENV",
    "MANAGED_KEYS",
    "SERVER_NAME",
    "ClientAdapter",
    "ClientEntry",
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


@dataclass(frozen=True)
class ClientEntry:
    """The memory-manager server entry `ClientAdapter.read_entry` found, fully resolved to
    what this client's own expansion rules would actually send on the wire.

    `url`/`headers` for `transport="http"`, `command`/`args`/`env` for `transport="stdio"` -
    the other side's fields stay at their defaults, same split as `ServerEntry.as_json`.
    `unresolved_vars` names every `${VAR}` this adapter's own rules could not resolve against
    the `env` it was given (left as the literal `${VAR}` text in the field that contained it,
    per docs/research/clients/claude-code.md) - never silently dropped, so `doctor --client`
    can report it instead of just failing the step it breaks further down the line.

    `token_env` is the env var name an `Authorization` header's value names, when that value
    is (or was, before expansion) exactly one `${VAR}` placeholder - `None` for `--inline-token`
    (a literal value, no variable to name) or no `Authorization` header at all. Only ever
    informational (a hint names it on a 401), never re-read to recompute anything.
    """

    transport: Literal["http", "stdio"]
    scope: str
    url: str | None = None
    headers: Mapping[str, str] = MappingProxyType({})
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = MappingProxyType({})
    unresolved_vars: tuple[str, ...] = ()
    token_env: str | None = None


class ClientAdapter(Protocol):
    """One supported client's config format and setup flow."""

    name: str

    #: The `clientInfo.name` this client's own MCP `Client` sends (#131, ADR-0010,
    #: `compat.select`'s `_CLIENT_INFO_PROFILE`) - `None` when that name is not sourced
    #: (claude.ai) or the client has no MCP `Client` of its own to read one from.
    client_info_name: str | None

    #: This client's config scopes, in the order `doctor --client` looks them up - highest
    #: precedence first, same order the client itself resolves a name collision with
    #: (docs/research/clients/<name>.md). Empty for a client with no local config at all.
    scopes: tuple[str, ...]

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

    def read_entry(
        self, text: str, *, scope: str, project_dir: Path, env: Mapping[str, str]
    ) -> ClientEntry | None:
        """The memory-manager entry in `text` (this scope's config file content), with every
        `${VAR}` this client expands resolved against `env` - `None` if `text` carries no
        such entry, or one with no recognizable `type` (`merge`'s own "silently skipped"
        case, docs/research/clients/claude-code.md). Never reads a file itself - `text` is
        whatever the caller already read from `locate_config`'s path, I/O-free by design so
        a test can exercise this against a literal string."""

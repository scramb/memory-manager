# SPDX-License-Identifier: AGPL-3.0-only
"""claude.ai's `ClientAdapter` (#137, #138): a custom connector configured in claude.ai's own
web UI, not a local file this adapter could merge into - `locate_config`/`instructions_file`
are both `None`, and `connect claude-ai` instead prints the manual setup steps from
`docs/clients/claude-ai.md` "Setup/Global", filled in with the server's own URL.

`read_entry` always returns `None` and `scopes` is empty for the same reason: there is no
local file `doctor --client claude-ai` could ever read a connector back out of, so its
"config found" step fails for every scope by construction, pointing at the manual checklist
in `docs/clients/claude-ai.md` instead - never at a `connect claude-ai` run, since that command
never writes a file either.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from memory_manager.clients.base import ClientEntry, ServerEntry

__all__ = ["ClaudeAiAdapter"]


class ClaudeAiAdapter:
    """Prints the claude.ai custom-connector setup steps; there is no config file to merge."""

    name = "claude-ai"
    client_info_name: str | None = None
    scopes: tuple[str, ...] = ()

    def locate_config(
        self, *, scope: str, home: Path, project_dir: Path, env: Mapping[str, str]
    ) -> None:
        return None

    def merge(self, current_text: str | None, entry: ServerEntry) -> str:
        raise NotImplementedError("claude.ai has no local config file to merge into")

    def instructions_file(self, project_dir: Path) -> None:
        return None

    def read_entry(
        self, text: str, *, scope: str, project_dir: Path, env: Mapping[str, str]
    ) -> ClientEntry | None:
        return None

    def web_steps(self, url: str) -> list[str]:
        return [
            "1. Open Settings -> Connectors -> Add custom connector.",
            f"2. URL: {url} - leave client ID and secret empty, so claude.ai registers "
            "itself through a Client ID Metadata Document.",
            "3. Select Connect. A memory-manager page names the client and its redirect "
            "host first; continue to the sign-in page and sign in.",
            "4. In a chat with the connector enabled, ask Claude to remember something; a "
            "commit authored by 'claude-ai' appears in the vault repository.",
            f"Note: {url} must be reachable from Anthropic's cloud, not just from this "
            "machine - use a tunnel if it is not publicly reachable yet.",
        ]

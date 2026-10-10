# SPDX-License-Identifier: AGPL-3.0-only
"""Claude Code's `ClientAdapter` (#137): user scope merges into `~/.claude.json`
(`$CLAUDE_CONFIG_DIR/.claude.json` when set), project scope into `<project>/.mcp.json` - both
the same `{"mcpServers": {...}}` shape (docs/research/clients/claude-code.md). The optional
`.claude/rules/memory-manager.md` instructions file carries the same generated snippet as
`integrations/claude-code/CLAUDE.snippet.md` (`guide/targets.py`), rendered from the
`INSTRUCTIONS` constant rather than read from that file, so it is correct from an installed
wheel too, without a repository checkout.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from memory_manager.clients.base import MANAGED_KEYS, SERVER_NAME, ServerEntry
from memory_manager.clients.jsonconfig import merge_server_entry
from memory_manager.guide.targets import TARGETS, render_target
from memory_manager.mcp.instructions import INSTRUCTIONS

__all__ = ["ClaudeCodeAdapter", "instructions_content"]


class ClaudeCodeAdapter:
    """Merges the memory-manager server into Claude Code's own config file."""

    name = "claude-code"

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


def instructions_content() -> str:
    """The rendered `.claude/rules/memory-manager.md` body, byte-identical to
    `integrations/claude-code/CLAUDE.snippet.md`."""
    return render_target(TARGETS["claude-code"], {"instructions": INSTRUCTIONS})

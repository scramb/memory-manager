# SPDX-License-Identifier: AGPL-3.0-only
"""The table of per-client instruction files `instructions generate` renders from
`docs/memory-guide.md` (#128): one `ClientTarget` per supported client, naming its output
path, the guide section that feeds its body, and the template that wraps that body.

stdlib only, and never imports from `memory_manager.mcp` - same rule as `guide/generate.py`,
for the same reason: this package generates files other packages depend on, not the reverse.

Each template carries a single `{{BODY}}` placeholder, filled by a plain `str.replace` rather
than `str.format`/`string.Template` - the body text (prose with code spans, parentheses, and
no reason to ever contain `{` or `$`) should never be able to interact with the substitution
mechanism itself.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TARGETS", "ClientTarget", "render_target"]

# First line of every generated client file, so a reader (or a future `--check`) can tell at a
# glance that hand-editing it is pointless - the next `instructions generate --all` overwrites it.
_HEADER = (
    "<!-- generated from docs/memory-guide.md by `memory-manager instructions generate` "
    "— do not edit -->"
)

_BODY_PLACEHOLDER = "{{BODY}}"

_CLAUDE_CODE_TEMPLATE = (
    _HEADER + "\n"
    "## Memory\n"
    "\n"
    "Durable facts, preferences and decisions about the user live in the memory-manager "
    "vault, reached through the `memory_*` MCP tools (`claude mcp list`, `/mcp`) — not "
    "in this file, and not lost between sessions.\n"
    "\n"
    "<!-- BEGIN memory-manager instructions -->\n"
    f"{_BODY_PLACEHOLDER}\n"
    "<!-- END memory-manager instructions -->\n"
)

_GENERIC_TEMPLATE = (
    _HEADER + "\n"
    "## Memory\n"
    "\n"
    "Durable facts, preferences and decisions about the user live in the memory-manager "
    "vault, reached through its `memory_*` MCP tools — not lost between sessions.\n"
    "\n"
    "<!-- BEGIN memory-manager instructions -->\n"
    f"{_BODY_PLACEHOLDER}\n"
    "<!-- END memory-manager instructions -->\n"
)


@dataclass(frozen=True)
class ClientTarget:
    """One client's generated instruction file.

    `output` is a path relative to the repository root. `section` is the `docs/memory-guide.md`
    section (a key of `parse_guide`'s result) that fills the template's `{{BODY}}` placeholder.
    """

    output: str
    section: str
    template: str


TARGETS: dict[str, ClientTarget] = {
    "claude-code": ClientTarget(
        output="integrations/claude-code/CLAUDE.snippet.md",
        section="instructions",
        template=_CLAUDE_CODE_TEMPLATE,
    ),
    "generic": ClientTarget(
        output="integrations/generic/AGENTS.md",
        section="instructions",
        template=_GENERIC_TEMPLATE,
    ),
}


def render_target(target: ClientTarget, sections: dict[str, str]) -> str:
    """Render `target`'s full output text from the parsed guide `sections`.

    Raises `KeyError` if `sections` lacks `target.section` (the generator's `_REQUIRED_SECTIONS`
    check already rules that out for a well-formed `docs/memory-guide.md`).
    """
    return target.template.replace(_BODY_PLACEHOLDER, sections[target.section])

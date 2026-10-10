# SPDX-License-Identifier: AGPL-3.0-only
"""Parses `docs/memory-guide.md` into named sections and renders the generated module text
for `src/memory_manager/mcp/instructions_generated.py` (#127).

A section is a pair of HTML comment markers, each on its own line: `<!-- name -->` opens it,
`<!-- /name -->` closes it. `docs/memory-guide.md` carries three today (`core`,
`instructions`, `long`); the format allows more later (`short`, #128) without a parser
change - only `_REQUIRED_SECTIONS` below is specific to what this module renders.

Trailing-newline rule: a closing marker always sits on its own line, so there is always at
least one newline between a section's last content line and its marker - that one is
structural, not part of the section's value, and gets stripped. A section whose value should
itself end in "\\n" (`long`/`GUIDE`) carries that through as a blank line before its closing
marker in the Markdown: stripping the structural newline then leaves exactly one.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["GuideFormatError", "build", "is_current", "parse_guide", "render_module"]

_MARKER = re.compile(r"\A<!--\s*(?P<slash>/?)(?P<name>[a-z][a-z0-9_-]*)\s*-->\Z")

# What `render_module` needs to exist; the format itself allows other section names too (#128).
_REQUIRED_SECTIONS = ("core", "instructions", "long")

_MODULE_HEADER = '''\
# SPDX-License-Identifier: AGPL-3.0-only
"""Generated from docs/memory-guide.md by `memory-manager instructions generate` (#127) -
do not edit by hand. Edit docs/memory-guide.md and regenerate instead.
"""

from __future__ import annotations

__all__ = ["GUIDE", "INSTRUCTIONS", "TOOL_DATA_SENTENCE"]

'''


class GuideFormatError(ValueError):
    """`docs/memory-guide.md` does not parse into well-formed named sections."""


def parse_guide(text: str) -> dict[str, str]:
    """Split `text` into its named sections, keyed by section name.

    Raises `GuideFormatError` for a missing, duplicate or unclosed section, or for a
    `core` whose value does not appear verbatim inside `instructions` and `long`.
    """
    lines = text.splitlines(keepends=True)
    sections: dict[str, str] = {}
    open_name: str | None = None
    open_start = 0
    for index, line in enumerate(lines):
        match = _MARKER.match(line.strip("\n"))
        if match is None:
            continue
        name = match["name"]
        if match["slash"]:
            if open_name != name:
                where = f"closes {open_name!r}" if open_name else "has no matching opener"
                raise GuideFormatError(f"<!-- /{name} --> {where}")
            sections[name] = _strip_one_trailing_newline("".join(lines[open_start:index]))
            open_name = None
        else:
            if open_name is not None:
                raise GuideFormatError(f"<!-- {name} --> opens inside <!-- {open_name} -->")
            if name in sections:
                raise GuideFormatError(f"duplicate section {name!r}")
            open_name = name
            open_start = index + 1
    if open_name is not None:
        raise GuideFormatError(f"<!-- {open_name} --> is never closed")

    missing = [name for name in _REQUIRED_SECTIONS if name not in sections]
    if missing:
        raise GuideFormatError(f"missing required section(s): {', '.join(missing)}")

    core = sections["core"]
    if core not in sections["instructions"]:
        raise GuideFormatError("'core' does not appear verbatim inside 'instructions'")
    if core not in sections["long"]:
        raise GuideFormatError("'core' does not appear verbatim inside 'long'")
    return sections


def _strip_one_trailing_newline(raw: str) -> str:
    return raw[:-1] if raw.endswith("\n") else raw


def render_module(sections: dict[str, str]) -> str:
    """Render the full source text of `instructions_generated.py` for these sections."""
    assignments = (
        ("TOOL_DATA_SENTENCE", sections["core"]),
        ("INSTRUCTIONS", sections["instructions"]),
        ("GUIDE", sections["long"]),
    )
    body = "\n\n".join(f"{name} = {value!r}" for name, value in assignments)
    return _MODULE_HEADER + body + "\n"


def build(guide_path: Path) -> str:
    """Read and parse `guide_path`, returning the generated module's full source text.

    Raises `FileNotFoundError` if `guide_path` does not exist, `GuideFormatError` if it does
    not parse.
    """
    text = guide_path.read_text(encoding="utf-8")
    return render_module(parse_guide(text))


def is_current(guide_path: Path, out_path: Path) -> bool:
    """Whether `out_path` already holds what `build(guide_path)` would write."""
    if not out_path.exists():
        return False
    return out_path.read_text(encoding="utf-8") == build(guide_path)

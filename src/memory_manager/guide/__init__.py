# SPDX-License-Identifier: AGPL-3.0-only
"""Generates `src/memory_manager/mcp/instructions_generated.py` and the per-client instruction
files from `docs/memory-guide.md` (#127, #128) - the single source for the server's
`instructions`, the `memory_guide` prompt, the short form, and every client's instruction file.

stdlib only, and never imports from `memory_manager.mcp`: this package is what generates
`instructions_generated.py`, so it cannot itself depend on the thing it produces.
"""

from __future__ import annotations

from memory_manager.guide.generate import GuideFormatError, build, is_current, parse_guide
from memory_manager.guide.targets import TARGETS, ClientTarget, render_target

__all__ = [
    "TARGETS",
    "ClientTarget",
    "GuideFormatError",
    "build",
    "is_current",
    "parse_guide",
    "render_target",
]

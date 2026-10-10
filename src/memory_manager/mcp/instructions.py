# SPDX-License-Identifier: AGPL-3.0-only
"""Usage rules shipped to the client: server `instructions` and the `memory_guide` prompt (#20,
#127).

The source of these rules is `docs/memory-guide.md`, not this module: `memory-manager
instructions generate` (`guide/generate.py`) parses its three marked sections and writes
`instructions_generated.py`, which this module re-exports unchanged. Edit the guide, run the
generator, commit both.

Two texts, two audiences. `INSTRUCTIONS` is what a client sees on every connection (the MCP
`initialize` result) and what Claude Code truncates past 2,048 characters (`docs/PLAN.md`
"Protocol targets") - dense and imperative, every rule in one or two sentences. `GUIDE` is
what a client pulls on demand through the `memory_guide` prompt (`server.py`) - the long form,
with the worked examples `INSTRUCTIONS` has no room for: a good note, a good `description`, the
supersede pattern, the conflict-merge loop, and what never to store.

Both are plain module constants and both are Markdown-friendly plain text - so a future Claude
Code skill / `CLAUDE.md` snippet (#22) can quote or embed them directly instead of re-deriving
the same rules.

`TOOL_DATA_SENTENCE` is the one rule every tool description in `server.py` repeats verbatim,
pulled out here so `INSTRUCTIONS`, `GUIDE` and all six tool descriptions never drift from each
other on its exact wording.
"""

from __future__ import annotations

from memory_manager.mcp.instructions_generated import GUIDE, INSTRUCTIONS, TOOL_DATA_SENTENCE

__all__ = ["GUIDE", "INSTRUCTIONS", "TOOL_DATA_SENTENCE"]

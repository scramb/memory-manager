# SPDX-License-Identifier: AGPL-3.0-only
"""Importers: turn external content into notes through the write queue (#48).

`importers.core` is the source-independent half (`ImportItem` -> note bytes
-> `WriteQueue`); each source gets its own module that only has to produce
`ImportItem`s - `importers.markdown` today, the Claude/ChatGPT export
parsers of `#49` later.
"""

from __future__ import annotations

from memory_manager.importers.core import (
    ImportItem,
    ImportItemRejected,
    ImportReport,
    dedupe_against_vault,
    open_queue,
    run_import,
)

__all__ = [
    "ImportItem",
    "ImportItemRejected",
    "ImportReport",
    "dedupe_against_vault",
    "open_queue",
    "run_import",
]

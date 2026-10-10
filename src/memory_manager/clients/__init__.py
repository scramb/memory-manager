# SPDX-License-Identifier: AGPL-3.0-only
"""`connect <client>` support (#137, F-02): one `ClientAdapter` per supported client, keyed by
name in `REGISTRY` - same shape as `index/embeddings.py`'s `EmbeddingProvider` registry.

`base.py` carries the shared `ClientAdapter` protocol and `ServerEntry`; `jsonconfig.py` does
the text-level JSON merge Claude Code's config needs; `files.py` turns a merge result into a
safe write (diff, backup, atomic, concurrency-checked); `connect.py` is the CLI-facing flow
`cli.py`'s `connect` branch calls into directly.
"""

from __future__ import annotations

from memory_manager.clients.base import ClientAdapter
from memory_manager.clients.claude_ai import ClaudeAiAdapter
from memory_manager.clients.claude_code import ClaudeCodeAdapter

__all__ = ["REGISTRY", "ClientAdapter"]

REGISTRY: dict[str, ClientAdapter] = {
    "claude-code": ClaudeCodeAdapter(),
    "claude-ai": ClaudeAiAdapter(),
}

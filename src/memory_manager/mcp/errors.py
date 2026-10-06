# SPDX-License-Identifier: AGPL-3.0-only
"""Map vault/queue exceptions to an actionable, client-facing error dict (#17).

`error_to_dict` is the one seam between the vault/queue layer's exceptions
and what an MCP tool reports back: every `queue.WriteError` already carries
its own `to_dict()` (write/edit/archive, #18/#19 - including
`VersionConflict`/`WriteConflict`'s current content and version, so a client
can retry without a second round trip); `PathRejected`, `NoteFormatError`
and `NoteInvalid` are the read-path errors this task actually raises, mapped
here the same shape so every tool error looks the same regardless of which
layer raised it.
"""

from __future__ import annotations

from memory_manager.queue import WriteError
from memory_manager.vault.note import NoteFormatError
from memory_manager.vault.paths import PathRejected
from memory_manager.vault.validate import NoteInvalid

__all__ = ["error_to_dict"]


def error_to_dict(exc: Exception) -> dict[str, object]:
    """Turn an exception from the vault/queue layer into a tool-error dict.

    Raises `TypeError` for anything not in the known set - a programming
    error (a new exception type was introduced without updating this
    mapping), not something a client should ever see.
    """
    if isinstance(exc, WriteError):
        return exc.to_dict()
    if isinstance(exc, PathRejected):
        return {"error": "PathRejected", "message": str(exc)}
    if isinstance(exc, NoteFormatError):
        return {"error": "NoteFormatError", "message": str(exc)}
    if isinstance(exc, NoteInvalid):
        return {
            "error": "NoteInvalid",
            "message": str(exc),
            "issues": [{"field": issue.field, "message": issue.message} for issue in exc.issues],
        }
    raise TypeError(f"no error mapping for {type(exc).__name__}")

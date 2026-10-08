# SPDX-License-Identifier: AGPL-3.0-only
"""Storage backends: the vault behind `StorageBackend` (ADR-0007 §1).

Re-exports `storage.base` (the protocol and its supporting types) and the
shared validation rules from `storage.rules`. Deliberately does **not**
import `storage.git`: the Git implementation depends on `WriteQueue`
(`memory_manager.queue`), which in turn imports `Op`/`WriteRequest`/
`WriteResult`/the `WriteError` hierarchy from `storage.base` - importing
`storage.git` here would make `import memory_manager.storage` a cycle back
through `queue.py`. Import `memory_manager.storage.git` directly for the
Git backend.
"""

from __future__ import annotations

from memory_manager.storage.base import (
    BlocklistRejected,
    EditMismatch,
    InvalidNote,
    NotFound,
    Op,
    SecretRejected,
    StorageBackend,
    StorageChanges,
    StoredNote,
    VersionConflict,
    WriteConflict,
    WriteError,
    WriteFailed,
    WriteRequest,
    WriteResult,
)
from memory_manager.storage.rules import (
    check_version,
    decode_for_conflict,
    new_content_for,
    parse_note_path_or_raise,
    prepare_archive,
    prepare_supersede_content,
    prepare_supersede_paths,
    prepare_write_or_edit,
)

__all__ = [
    "BlocklistRejected",
    "EditMismatch",
    "InvalidNote",
    "NotFound",
    "Op",
    "SecretRejected",
    "StorageBackend",
    "StorageChanges",
    "StoredNote",
    "VersionConflict",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteRequest",
    "WriteResult",
    "check_version",
    "decode_for_conflict",
    "new_content_for",
    "parse_note_path_or_raise",
    "prepare_archive",
    "prepare_supersede_content",
    "prepare_supersede_paths",
    "prepare_write_or_edit",
]

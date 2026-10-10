# SPDX-License-Identifier: AGPL-3.0-only
"""Writing a client config change safely (#137): a unified diff for the operator to read, a
timestamped backup beside the file, and an atomic write that aborts instead of clobbering a
concurrent change.

The write itself follows `vault/repo.py`'s `_atomic_write` pattern (write to a sibling temp
file, `os.replace` over the target) plus what that pattern does not need for a vault commit:
preserving the target's own file mode, resolving a symlink to its target first, and re-reading
the target right before the swap to catch a change made to it since it was first read - never
overwrite silently (CLAUDE.md).
"""

from __future__ import annotations

import contextlib
import difflib
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["ConcurrentModificationError", "render_diff", "write_config"]

_NEW_FILE_MODE = 0o600


class ConcurrentModificationError(RuntimeError):
    """`path` changed on disk since it was read; the caller should read and retry."""


def render_diff(old: str, new: str, *, path: Path, mask: str | None = None) -> str:
    """A unified diff from `old` to `new`, with any occurrence of `mask` (an inline secret
    value) replaced by `***` - a diff is printed to the terminal/log, a secret is not."""
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return diff.replace(mask, "***") if mask else diff


def write_config(path: Path, new_text: str, *, base_text: str | None) -> Path | None:
    """Write `new_text` to `path`, backing up any existing content first.

    `base_text` is the content the caller built `new_text` from (`None` for a file that did
    not exist yet when read). Raises `ConcurrentModificationError` without writing anything if
    `path`'s current content no longer matches `base_text` - something else changed the file
    since it was read, run the command again. Returns the backup file's path, or `None` when
    there was nothing to back up (a fresh file).
    """
    target = path.resolve()
    existed = target.exists()
    current_bytes = target.read_bytes() if existed else b""
    expected_bytes = base_text.encode("utf-8") if base_text is not None else b""

    if existed != (base_text is not None) or current_bytes != expected_bytes:
        raise ConcurrentModificationError(
            f"{path} changed on disk since it was read - run the command again"
        )

    mode = stat.S_IMODE(target.stat().st_mode) if existed else _NEW_FILE_MODE
    backup_path = _backup(target, current_bytes, mode) if existed else None
    _atomic_write(target, new_text.encode("utf-8"), mode=mode)
    return backup_path


def _backup(path: Path, content: bytes, mode: int) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.mm-backup-{timestamp}")
    _atomic_write(backup_path, content, mode=mode)
    return backup_path


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise

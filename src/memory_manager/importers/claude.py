# SPDX-License-Identifier: AGPL-3.0-only
"""Claude memory export importer (#49).

Accepts, detected by content rather than file name (`docs/research/
memory-exports.md`):

- an account-data export zip (manifest + nested `memories-*.zip`), a bare
  `memories-*.zip`, or any zip holding `memories.json` / `memories/
  <uuid>.json` directly - every other zip entry (conversations, projects,
  ...) is ignored;
- a single memories JSON file - the legacy top-level array, or the current
  per-account object - with any subset of `conversations_memory`,
  `project_memories`, `memory_files`;
- a plain text/Markdown list, the fallback for whatever the model printed.

`memory_files[]` entries become one note each (the file's own content,
never split). `conversations_memory` and each `project_memories` entry are
prose/Markdown blocks split into one item per bullet or paragraph via
`importers.textlist.parse_items`.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from memory_manager.importers.core import ImportItem, build_source, slugify
from memory_manager.importers.textlist import (
    build_body,
    collect_from_text,
    derive_title,
    parse_items,
)

__all__ = ["ClaudeFormatError", "collect"]

_SOURCE_PREFIX = "import:claude:"
_PROVIDER_LABEL = "claude"
_MEMORY_KEYS = ("conversations_memory", "project_memories", "memory_files")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")

# Zip-bomb guards (#49 fix round): a Claude export is a handful of small JSON
# files, never anything close to these limits in practice.
_MAX_ZIP_DEPTH = 2  # the outer export zip, plus at most one inner `memories-*.zip`
_MAX_ENTRY_BYTES = 20 * 1024 * 1024  # skip any single entry bigger than this
_MAX_TOTAL_BYTES = 100 * 1024 * 1024  # shared budget across every entry actually read
_MAX_COMPRESSION_RATIO = 100  # file_size:compress_size beyond this, once already large, is a bomb
_SUSPICIOUS_RATIO_FLOOR = 1024 * 1024  # ratio only matters once an entry claims >1 MB uncompressed
_INNER_ZIP_NAME_RE = re.compile(r"^memories(-\d+)?\.zip$", re.IGNORECASE)


class ClaudeFormatError(ValueError):
    """`path` is not a Claude export/memories shape this importer understands."""


class _ReadBudget:
    """Tracks decompressed bytes actually read across one zip walk.

    Shared by every `_read_entry_limited` call for one `collect()` so that
    many individually small-looking entries cannot add up past
    `_MAX_TOTAL_BYTES` (#49 fix round: a zip bomb spread across entries
    rather than concentrated in one).
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self, amount: int) -> None:
        self.used += amount
        if self.used > self.limit:
            raise ClaudeFormatError(
                f"zip export exceeds the {self.limit}-byte total read budget - "
                "refusing to keep decompressing (possible zip bomb)"
            )


def collect(
    path: Path, *, namespace: str, type_: str = "user", today: date | None = None
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    """Parse `path` into `(items, pre_rejected)` - see the module docstring for inputs.

    Raises `ClaudeFormatError` when `path` is recognizably JSON or a zip but
    none of its content matches a known Claude memories shape - a clear
    error naming what was expected, rather than silently importing nothing
    (CLAUDE.md "never trust input structure") - or when it looks like a zip
    bomb (oversized, an implausible compression ratio, nested too deep, or a
    declared size the decompressed stream itself contradicts).
    """
    when = today or date.today()

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise ClaudeFormatError(f"cannot stat '{path}': {exc}") from exc
    if file_size > _MAX_TOTAL_BYTES:
        raise ClaudeFormatError(
            f"'{path}' is {file_size} bytes, more than the {_MAX_TOTAL_BYTES}-byte import limit"
        )

    data = path.read_bytes()

    if zipfile.is_zipfile(path):
        memories = _memories_from_zip_bytes(data)
        if memories is None:
            raise ClaudeFormatError(
                f"'{path}' is a zip but no memories.json / memories/<uuid>.json with "
                f"one of {', '.join(_MEMORY_KEYS)} was found inside it (or its nested zips)"
            )
        return _collect_from_memories(memories, namespace=namespace, type_=type_, when=when)

    text = _decode_or_none(data)
    if text is None:
        raise ClaudeFormatError(f"'{path}' is neither a zip nor valid UTF-8 text")

    memories = _try_parse_memories_json(text)
    if memories is not None:
        return _collect_from_memories(memories, namespace=namespace, type_=type_, when=when)

    if text.strip().startswith(("{", "[")):
        raise ClaudeFormatError(
            f"'{path}' looks like JSON but has none of {', '.join(_MEMORY_KEYS)} - "
            "expected a Claude memories export"
        )

    items = collect_from_text(
        text,
        namespace=namespace,
        type_=type_,
        source_prefix=_SOURCE_PREFIX,
        provider_label=_PROVIDER_LABEL,
        today=when,
    )
    return items, []


def _decode_or_none(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _as_memories_object(candidate: object) -> dict[str, object] | None:
    if isinstance(candidate, list):
        candidate = candidate[0] if candidate else None
    if isinstance(candidate, dict) and any(key in candidate for key in _MEMORY_KEYS):
        return candidate
    return None


def _try_parse_memories_json(text: str) -> dict[str, object] | None:
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _as_memories_object(candidate)


def _memories_from_zip_bytes(data: bytes) -> dict[str, object] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _search_zip(archive, depth=1, budget=_ReadBudget(_MAX_TOTAL_BYTES))
    except zipfile.BadZipFile:
        return None


def _is_memories_json_entry(filename: str, base: str) -> bool:
    return base == "memories.json" or (filename.startswith("memories/") and base.endswith(".json"))


def _read_entry_limited(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _ReadBudget
) -> bytes | None:
    """Read `info`'s decompressed content, or `None` to skip it outright.

    Three independent zip-bomb guards (#49 fix round), each with its own
    reason to exist: a plain oversized entry is skipped quietly (not every
    big file is an attack); an implausible declared compression ratio is a
    clear `ClaudeFormatError`, because that is specifically what a bomb
    looks like before it is even decompressed; and the actual read is
    capped and streamed (`ZipFile.open().read(limit + 1)`) rather than
    trusting `file_size`, so a header that lies about its own size is
    still caught. Every entry actually read also counts against the
    shared `budget` across the whole zip walk.
    """
    if info.file_size > _MAX_ENTRY_BYTES:
        return None

    if (
        info.file_size > _SUSPICIOUS_RATIO_FLOOR
        and info.compress_size > 0
        and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
    ):
        raise ClaudeFormatError(
            f"'{info.filename}' claims {info.file_size} bytes from only "
            f"{info.compress_size} compressed - an implausible compression ratio, "
            "refusing to decompress it (possible zip bomb)"
        )

    limit = min(_MAX_ENTRY_BYTES, budget.limit - budget.used)
    if limit <= 0:
        raise ClaudeFormatError(
            f"zip export exceeds the {budget.limit}-byte total read budget - "
            "refusing to keep decompressing (possible zip bomb)"
        )

    with archive.open(info) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ClaudeFormatError(
            f"'{info.filename}' decompresses to more than its declared size - "
            "refusing to keep reading (possible zip bomb)"
        )
    budget.take(len(content))
    return content


def _search_zip(
    archive: zipfile.ZipFile, *, depth: int, budget: _ReadBudget
) -> dict[str, object] | None:
    for info in archive.infolist():
        if info.is_dir():
            continue
        base = info.filename.rsplit("/", 1)[-1]

        if _is_memories_json_entry(info.filename, base):
            content = _read_entry_limited(archive, info, budget)
            if content is None:
                continue
            try:
                candidate = json.loads(content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            memories = _as_memories_object(candidate)
            if memories is not None:
                return memories
        elif _INNER_ZIP_NAME_RE.match(base):
            next_depth = depth + 1
            if next_depth > _MAX_ZIP_DEPTH:
                raise ClaudeFormatError(
                    f"'{info.filename}' nests a zip deeper than the maximum depth of "
                    f"{_MAX_ZIP_DEPTH} - refusing to descend further (possible zip bomb)"
                )
            content = _read_entry_limited(archive, info, budget)
            if content is None:
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as nested:
                    found = _search_zip(nested, depth=next_depth, budget=budget)
            except zipfile.BadZipFile:
                continue
            if found is not None:
                return found
        # Everything else (conversations/projects zips, media, ...) is
        # ignored without ever being opened - only the expected memory
        # layout's own entries are read at all.
    return None


def _collect_from_memories(
    memories: dict[str, object], *, namespace: str, type_: str, when: date
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    items: list[ImportItem] = []
    rejected: list[tuple[str, str]] = []

    memory_files = memories.get("memory_files")
    if isinstance(memory_files, list):
        for index, entry in enumerate(memory_files):
            identifier = _memory_file_identifier(entry, index)
            source_ref = build_source(_SOURCE_PREFIX, f"memory_files:{identifier}")
            try:
                items.append(
                    _item_from_memory_file(
                        entry, namespace=namespace, type_=type_, when=when, source_ref=source_ref
                    )
                )
            except ClaudeFormatError as exc:
                rejected.append((source_ref, str(exc)))

    conversations_memory = memories.get("conversations_memory")
    if isinstance(conversations_memory, str) and conversations_memory.strip():
        for position, parsed in enumerate(parse_items(conversations_memory, join_lines=True)):
            items.append(
                ImportItem(
                    title=derive_title(parsed.text),
                    body=build_body(parsed.text, provider_label=_PROVIDER_LABEL, when=when),
                    description=None,
                    type="user",
                    tags=(),
                    aliases=(),
                    created=parsed.created,
                    source=build_source(_SOURCE_PREFIX, f"conversations_memory:{position}"),
                    slug_hint=None,
                    namespace=namespace,
                )
            )

    project_memories = memories.get("project_memories")
    if isinstance(project_memories, dict):
        for project_id, project_text in project_memories.items():
            if not isinstance(project_text, str) or not project_text.strip():
                continue
            project_tag = slugify(str(project_id))
            for position, parsed in enumerate(parse_items(project_text, join_lines=True)):
                items.append(
                    ImportItem(
                        title=derive_title(parsed.text),
                        body=build_body(parsed.text, provider_label=_PROVIDER_LABEL, when=when),
                        description=None,
                        type="project",
                        tags=(project_tag,) if project_tag else (),
                        aliases=(),
                        created=parsed.created,
                        source=build_source(
                            _SOURCE_PREFIX, f"project_memories:{project_id}:{position}"
                        ),
                        slug_hint=None,
                        namespace=namespace,
                    )
                )

    return items, rejected


def _memory_file_identifier(entry: object, index: int) -> str:
    if isinstance(entry, dict):
        raw_path = entry.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            return raw_path.strip()
    return f"[{index}]"


def _item_from_memory_file(
    entry: object, *, namespace: str, type_: str, when: date, source_ref: str
) -> ImportItem:
    if not isinstance(entry, dict):
        raise ClaudeFormatError(f"memory_files entry must be an object, got {type(entry).__name__}")
    content = entry.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ClaudeFormatError("memory_files entry has no non-empty 'content' string")

    raw_path = entry.get("path")
    path_text = raw_path if isinstance(raw_path, str) and raw_path.strip() else "memory"
    stem = PurePosixPath(path_text).stem or "memory"
    title = _first_heading(content) or _humanize(stem)
    created = _parse_timestamp(entry.get("updated_at"))

    return ImportItem(
        title=title,
        body=build_body(content, provider_label=_PROVIDER_LABEL, when=when),
        description=None,
        type=type_,
        tags=(),
        aliases=(),
        created=created,
        source=source_ref,
        slug_hint=stem,
        namespace=namespace,
    )


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            return match.group(1).strip()
    return None


def _humanize(slug_hint: str) -> str:
    words = [word for word in re.split(r"[-_]+", slug_hint) if word]
    return " ".join(word.capitalize() for word in words) or "Untitled"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

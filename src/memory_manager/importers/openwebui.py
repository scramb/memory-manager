# SPDX-License-Identifier: AGPL-3.0-only
"""Open WebUI built-in memory importer (#152, F-02, docs/research/clients/openwebui.md).

Open WebUI has no export button and no dedicated export endpoint - its one
read path is `GET /api/v1/memories/`, the same endpoint the built-in memory
tab itself calls, returning the caller's own memories as a plain JSON array
(no pagination). `fetch_memories` calls that endpoint directly; `collect`
parses a saved copy of the exact same response from a file, so both paths
share `collect_from_memories` as their one mapping step.

Each memory is `{"id", "user_id", "type", "path", "content", "meta",
"updated_at", "created_at"}`. `user_id`/`meta` are never imported (not ours
to carry into the vault); `type` ("user"|"context", default "context")
decides the note type - `"user"` stays `"user"`, everything else (including
any future value this importer does not know yet) becomes `"fact"`
(docs/memory-guide.md). A set `path` becomes one tag, folded through
`importers.core.slugify` to ADR-0005's tag shape and length.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from memory_manager.importers.core import ImportItem, build_source, slugify
from memory_manager.importers.textlist import build_body, derive_title

__all__ = ["OpenWebUIFormatError", "collect", "collect_from_memories", "fetch_memories"]

_SOURCE_PREFIX = "import:openwebui:"
_PROVIDER_LABEL = "openwebui"
_MAX_TAG_CHARS = 40  # ADR-0005 "Frontmatter fields": a tag is at most 40 chars.

# A memory export is a plain JSON array with no attachments - nowhere near
# this size in practice; guards against an accidentally huge/wrong file
# (pattern importers/claude.py's own `_MAX_TOTAL_BYTES`).
_MAX_FILE_BYTES = 100 * 1024 * 1024

_MEMORIES_PATH = "/api/v1/memories/"
_REQUEST_TIMEOUT_SECONDS = 10.0

_STATUS_EXPLANATIONS = {
    401: "the API key is invalid",
    403: "API keys are disabled in this Open WebUI deployment (ENABLE_API_KEYS) or this "
    "account's role lacks the api_keys/memories feature",
    404: "memories are disabled in this Open WebUI deployment (ENABLE_MEMORIES)",
}


class OpenWebUIFormatError(ValueError):
    """`path`/the fetched response is not a shape this importer understands."""


def collect(
    path: Path, *, namespace: str, today: date | None = None
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    """Parse `path` - a saved `GET /api/v1/memories/` response - into `(items, pre_rejected)`.

    Raises `OpenWebUIFormatError` if `path` is too large, not valid UTF-8/JSON,
    or its top-level shape is not a JSON array.
    """
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise OpenWebUIFormatError(f"cannot stat '{path}': {exc}") from exc
    if file_size > _MAX_FILE_BYTES:
        raise OpenWebUIFormatError(
            f"'{path}' is {file_size} bytes, more than the {_MAX_FILE_BYTES}-byte import limit"
        )

    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenWebUIFormatError(f"'{path}' is not valid UTF-8 text") from exc

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenWebUIFormatError(
            f"'{path}' is not valid JSON - expected a saved GET /api/v1/memories/ response"
        ) from exc

    return collect_from_memories(parsed, namespace=namespace, today=today)


def collect_from_memories(
    memories: object, *, namespace: str, today: date | None = None
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    """Map already-decoded JSON (the body of `GET /api/v1/memories/`) into
    `(items, pre_rejected)`. Shared by `collect` (a saved file) and `fetch_memories`
    (a live call) - both hand their already-parsed JSON to this one function.

    Raises `OpenWebUIFormatError` if `memories` is not a JSON array. An
    individual entry that is not an object, or has no non-empty `content`
    string, is reported in `pre_rejected` instead - one bad entry never
    drops the rest of the batch.
    """
    if not isinstance(memories, list):
        raise OpenWebUIFormatError(
            f"expected a JSON array of memories, got {type(memories).__name__}"
        )

    when = today or date.today()
    items: list[ImportItem] = []
    rejected: list[tuple[str, str]] = []

    for index, entry in enumerate(memories):
        identifier = _identifier(entry, index)
        source_ref = build_source(_SOURCE_PREFIX, identifier)

        if not isinstance(entry, dict):
            rejected.append(
                (source_ref, f"memory entry must be an object, got {type(entry).__name__}")
            )
            continue

        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            rejected.append((source_ref, "memory entry has no non-empty 'content' string"))
            continue

        items.append(
            _item_from_entry(entry, content, namespace=namespace, when=when, source_ref=source_ref)
        )

    return items, rejected


def _identifier(entry: object, index: int) -> str:
    if isinstance(entry, dict):
        raw_id = entry.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            return raw_id.strip()
    return f"[{index}]"


def _resolve_type(entry: dict[str, object]) -> str:
    # Open WebUI's own default on creation is "context" (models/memories.py);
    # anything other than the literal "user" - including a future type value
    # this importer does not know about yet - maps to "fact" (decision #152).
    return "user" if entry.get("type") == "user" else "fact"


def _resolve_tags(entry: dict[str, object]) -> tuple[str, ...]:
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return ()
    tag = slugify(raw_path, max_len=_MAX_TAG_CHARS)
    return (tag,) if tag else ()


def _parse_epoch_seconds(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _item_from_entry(
    entry: dict[str, object], content: str, *, namespace: str, when: date, source_ref: str
) -> ImportItem:
    return ImportItem(
        title=derive_title(content),
        body=build_body(content, provider_label=_PROVIDER_LABEL, when=when),
        description=None,
        type=_resolve_type(entry),
        tags=_resolve_tags(entry),
        aliases=(),
        created=_parse_epoch_seconds(entry.get("created_at")),
        source=source_ref,
        slug_hint=None,
        namespace=namespace,
    )


async def fetch_memories(
    url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> object:
    """`GET <url without a trailing slash>/api/v1/memories/` with a bearer token.

    Returns the parsed JSON body (not yet validated as a list - that is
    `collect_from_memories`'s job, the one place both input paths share it).
    Raises `OpenWebUIFormatError` for a connection failure, a redirect (never
    followed - the bearer token must never reach a different host), a non-2xx
    status (401/403/404 get a specific explanation, matching the verified
    Open WebUI error shapes) or a non-JSON body (for example an HTML login
    page). `transport` is a test seam (pattern `auth/cimd.py`'s
    `ClientMetadataFetcher`) - `None` opens a real connection.
    """
    fetch_url = f"{url.rstrip('/')}{_MEMORIES_PATH}"
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, timeout=_REQUEST_TIMEOUT_SECONDS
    ) as client:
        try:
            response = await client.get(fetch_url, headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as exc:
            raise OpenWebUIFormatError(f"could not reach {url}: {exc}") from exc

    if response.is_redirect:
        raise OpenWebUIFormatError(
            f"{url} answered with a redirect - refusing to follow it (the bearer token "
            "must never be sent to a different host)"
        )
    if response.status_code != 200:
        raise OpenWebUIFormatError(_error_message(url, response))

    try:
        return response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown content-type")
        raise OpenWebUIFormatError(
            f"{url} did not return JSON (content-type: {content_type}) - expected the "
            "GET /api/v1/memories/ response"
        ) from exc


def _error_message(url: str, response: httpx.Response) -> str:
    detail = _detail_from_body(response)
    explanation = _STATUS_EXPLANATIONS.get(response.status_code)
    if explanation is not None:
        suffix = f" ({detail})" if detail else ""
        return f"{url} returned {response.status_code}: {explanation}{suffix}"
    suffix = f": {detail}" if detail else ""
    return f"{url} returned {response.status_code}{suffix}"


def _detail_from_body(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return None

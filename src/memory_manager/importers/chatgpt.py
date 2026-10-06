# SPDX-License-Identifier: AGPL-3.0-only
"""ChatGPT memory importer (#49).

The main path (and ChatGPT's only genuinely documented one -
`docs/research/memory-exports.md` section 2) is a plain text/Markdown list:
one memory per line or bullet, with an optional leading `[date] -`, the
shape both vendors' own "ask the model to print your memories" prompt
produces. Parsing is `importers.textlist.parse_items`, shared with Claude's
prose-block splitting.

`--from-conversations` is opt-in and switches `collect()` into a different
mode entirely: `path` is then a ChatGPT `conversations.json` export, and
items come from assistant messages with `recipient == "bio"` - a lower-
confidence, historical trace of memory writes rather than the current
memory state (research caveat: a bio call does not prove the memory was
actually kept).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from memory_manager.importers.core import ImportItem, build_source
from memory_manager.importers.textlist import build_body, collect_from_text, derive_title

__all__ = ["ChatGPTFormatError", "collect"]

_SOURCE_PREFIX = "import:chatgpt:"
_PROVIDER_LABEL = "chatgpt"


class ChatGPTFormatError(ValueError):
    """`path` is not a shape this importer understands for the requested mode."""


def collect(
    path: Path,
    *,
    namespace: str,
    type_: str = "user",
    from_conversations: bool = False,
    today: date | None = None,
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    """Parse `path` into `(items, pre_rejected)`.

    `from_conversations=False` (the default): `path` is a plain text/
    Markdown memory list. `from_conversations=True`: `path` is a ChatGPT
    `conversations.json` export; items are `bio` tool calls found in it.
    """
    when = today or date.today()
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChatGPTFormatError(f"'{path}' is not valid UTF-8 text") from exc

    if not from_conversations:
        items = collect_from_text(
            text,
            namespace=namespace,
            type_=type_,
            source_prefix=_SOURCE_PREFIX,
            provider_label=_PROVIDER_LABEL,
            today=when,
        )
        return items, []

    try:
        conversations = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChatGPTFormatError(
            f"'{path}' is not valid JSON - expected a ChatGPT conversations.json export"
        ) from exc
    if not isinstance(conversations, list):
        raise ChatGPTFormatError(
            f"'{path}' must be a JSON array of conversations, got {type(conversations).__name__}"
        )
    return _bio_items_from_conversations(conversations, namespace=namespace, type_=type_, when=when)


def _bio_items_from_conversations(
    conversations: list[object], *, namespace: str, type_: str, when: date
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    items: list[ImportItem] = []
    for conv_index, conversation in enumerate(conversations):
        if not isinstance(conversation, dict):
            continue
        mapping = conversation.get("mapping")
        if not isinstance(mapping, dict):
            continue
        for node_id, node in mapping.items():
            text = _bio_text_from_node(node)
            if text is None:
                continue
            created = _parse_create_time(_message(node).get("create_time"))
            items.append(
                ImportItem(
                    title=derive_title(text),
                    body=build_body(text, provider_label=_PROVIDER_LABEL, when=when),
                    description=None,
                    type=type_,
                    tags=(),
                    aliases=(),
                    created=created,
                    source=build_source(_SOURCE_PREFIX, f"bio:{conv_index}:{node_id}"),
                    slug_hint=None,
                    namespace=namespace,
                )
            )
    return items, []


def _message(node: object) -> dict[str, object]:
    if not isinstance(node, dict):
        return {}
    message = node.get("message")
    return message if isinstance(message, dict) else {}


def _bio_text_from_node(node: object) -> str | None:
    message = _message(node)
    if message.get("recipient") != "bio":
        return None
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    text = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    return text or None


def _parse_create_time(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None

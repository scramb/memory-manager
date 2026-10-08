# SPDX-License-Identifier: AGPL-3.0-only
"""Rejects note content that matches an operator-defined blocklist category (#244).

Owner decision O26 (2026-10-08, `docs/PLAN.md`): an operator opts in by
pointing `BLOCKLIST_FILE` at a TOML file of categories, each a list of
regex patterns and/or keywords (German and English both fit in one file -
there is no separate language mechanism). A hit rejects the write and names
only the category, never the matched text (CLAUDE.md: note content is
data, not instructions - extended here to "never echoed back", the same
property `vault.secrets.SecretFound` already has). With no `BLOCKLIST_FILE`
set, `check()` is a no-op: empty by default, same as every `QUOTA_*` budget
in `config.py`.

This mirrors `vault/secrets.py` (`load_rules`/`check`, cached compilation,
a message that never carries the match), but the rule set is operator-owned
and path-configurable rather than a fixed file shipped with the code, so
`load_rules` is cached per path instead of parameterless. `load_rules` is
also called eagerly at process startup (`app.open_storage`/`open_services`)
so a typo'd regex in `BLOCKLIST_FILE` refuses startup instead of surfacing
as a confusing rejection on whatever note a user happens to write first.

Keyword matching is case-insensitive on a word boundary (`\\b`); Python's
`re` already treats letters outside ASCII - including the German umlauts
and `ß` - as word characters, so no extra handling is needed for them.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from memory_manager.config import blocklist_file_from_env

__all__ = ["BlocklistConfigError", "BlocklistFound", "check", "load_rules"]


class BlocklistConfigError(ValueError):
    """`BLOCKLIST_FILE` is set but could not be read, parsed or compiled."""


@dataclass(frozen=True)
class _Category:
    """One compiled `[[category]]` entry: a name and its compiled patterns."""

    name: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class _RuleSet:
    categories: tuple[_Category, ...]


class BlocklistFound(ValueError):
    """Raised by `check` when the text matches a configured category.

    Carries only the category name - never the matched text - so a caller
    can report and audit the rejection without the note's content ever
    leaving this module.
    """

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(
            f"matches the operator-defined blocklist category {category!r} - "
            "remove or rephrase the flagged content"
        )


def _compile_pattern(category_name: str, regex: str) -> re.Pattern[str]:
    try:
        return re.compile(regex, re.IGNORECASE)
    except re.error as exc:
        raise BlocklistConfigError(
            f"blocklist category {category_name!r}: invalid regex {regex!r}: {exc}"
        ) from exc


def _keyword_pattern(category_name: str, keyword: str) -> re.Pattern[str]:
    return _compile_pattern(category_name, rf"\b{re.escape(keyword)}\b")


def _compile_category(entry: dict[str, Any]) -> _Category:
    if "name" not in entry:
        raise BlocklistConfigError(f"blocklist category {entry!r} is missing 'name'")
    name = str(entry["name"])
    patterns = tuple(_compile_pattern(name, str(raw)) for raw in entry.get("patterns", [])) + tuple(
        _keyword_pattern(name, str(raw)) for raw in entry.get("keywords", [])
    )
    if not patterns:
        raise BlocklistConfigError(
            f"blocklist category {name!r} has neither 'patterns' nor 'keywords'"
        )
    return _Category(name=name, patterns=patterns)


@lru_cache(maxsize=8)
def load_rules(path: Path | None) -> _RuleSet:
    """Load and compile the blocklist at `path` (cached per path).

    `path=None` - no `BLOCKLIST_FILE` configured - loads an empty rule set,
    so `check()` is then always a no-op. Raises `BlocklistConfigError` if
    the file cannot be read, is not valid TOML, a category is missing
    `name`, has neither `patterns` nor `keywords`, or one of its regexes
    fails to compile.
    """
    if path is None:
        return _RuleSet(categories=())
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BlocklistConfigError(f"BLOCKLIST_FILE {path} could not be read: {exc}") from exc
    try:
        data: dict[str, Any] = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise BlocklistConfigError(f"BLOCKLIST_FILE {path} is not valid TOML: {exc}") from exc
    categories = tuple(_compile_category(entry) for entry in data.get("category", []))
    return _RuleSet(categories=categories)


def check(text: str) -> None:
    """Raise `BlocklistFound` if `text` matches a category of the configured blocklist.

    Reads `BLOCKLIST_FILE` from the process environment on every call (like
    `vault.secrets.check` reads its own fixed rule file), so there is no
    extra parameter for `storage.rules` to thread through every write path -
    `load_rules` below is what actually caches the compiled result, keyed
    by path. No `BLOCKLIST_FILE` set is always a no-op.
    """
    path = blocklist_file_from_env(dict(os.environ))
    rule_set = load_rules(path)
    for category in rule_set.categories:
        if any(pattern.search(text) for pattern in category.patterns):
            raise BlocklistFound(category.name)

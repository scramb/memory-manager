# SPDX-License-Identifier: AGPL-3.0-only
"""Link extraction and resolution for note bodies (ADR-0005 "Links", #10).

A note body may reference other notes with `[[slug]]` or `[[slug|label]]`.
`extract_links` finds those references in raw Markdown text, skipping
anything inside fenced code blocks or inline code spans. `resolve_links`
then matches each reference against a set of known vault entries, without
knowing anything about the filesystem (see "Nicht dabei" in the task: no
`paths.py` import, no path-safety checks here).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

__all__ = ["LinkRef", "ResolvedLink", "VaultEntry", "extract_links", "resolve_links"]

_LINK_RE = re.compile(r"\[\[(.*?)\]\]")
_FENCE_CHARS = ("`", "~")
_MIN_FENCE_LEN = 3


@dataclass(frozen=True)
class LinkRef:
    """A single `[[...]]` reference found in a note body."""

    target: str
    label: str | None
    line: int


@dataclass(frozen=True)
class VaultEntry:
    """The subset of a note's identity needed to resolve links to it."""

    path: str
    namespace: str
    slug: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedLink:
    """A `LinkRef` matched against the vault, or found to be dangling."""

    ref: LinkRef
    target_path: str | None

    @property
    def dangling(self) -> bool:
        return self.target_path is None


def extract_links(body: str) -> list[LinkRef]:
    """Find every `[[slug]]` / `[[slug|label]]` reference in `body`.

    Links inside fenced code blocks (``` ``` `` or `~~~`, a closing fence
    needs the same character and at least the opening length) and inline
    code spans (a backtick run closed by a run of the same length) are
    ignored. Targets and labels are trimmed; an empty target (`[[]]`, or
    `[[ ]]`) is skipped.

    Repeated targets are deduplicated: the first occurrence wins and keeps
    its position in the returned list, later ones are dropped. The target
    text is compared as written (case-sensitive) — two links that only
    differ in case are kept as separate `LinkRef`s, since case folding is a
    resolution concern (`resolve_links`), not an extraction one.
    """
    refs: list[LinkRef] = []
    seen_targets: set[str] = set()

    in_fence = False
    fence_char = ""
    fence_len = 0

    for line_no, line in enumerate(body.split("\n"), start=1):
        if in_fence:
            if _is_fence_close(line, fence_char, fence_len):
                in_fence = False
            continue

        opened = _fence_open(line)
        if opened is not None:
            fence_char, fence_len = opened
            in_fence = True
            continue

        masked = _mask_inline_code(line)
        for match in _LINK_RE.finditer(masked):
            target_raw, sep, label_raw = match.group(1).partition("|")
            target = target_raw.strip()
            if not target:
                continue
            label = label_raw.strip() if sep else None
            if label == "":
                label = None
            if target in seen_targets:
                continue
            seen_targets.add(target)
            refs.append(LinkRef(target=target, label=label, line=line_no))

    return refs


def _fence_open(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip()
    for char in _FENCE_CHARS:
        if stripped.startswith(char * _MIN_FENCE_LEN):
            run_len = len(stripped) - len(stripped.lstrip(char))
            return char, run_len
    return None


def _is_fence_close(line: str, fence_char: str, open_len: int) -> bool:
    stripped = line.strip()
    if len(stripped) < _MIN_FENCE_LEN or len(stripped) < open_len:
        return False
    return stripped == fence_char * len(stripped)


def _mask_inline_code(line: str) -> str:
    """Replace inline code spans with spaces so they cannot match `_LINK_RE`.

    A code span opens with a run of backticks and closes with the next run
    of *exactly* the same length (a longer or shorter run does not close
    it, matching CommonMark). A run with no matching close is left as
    literal text.
    """
    result: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] != "`":
            result.append(line[i])
            i += 1
            continue

        open_end = i
        while open_end < n and line[open_end] == "`":
            open_end += 1
        run_len = open_end - i

        close_start = -1
        close_end = -1
        k = open_end
        while k < n:
            if line[k] != "`":
                k += 1
                continue
            run_end = k
            while run_end < n and line[run_end] == "`":
                run_end += 1
            if run_end - k == run_len:
                close_start, close_end = k, run_end
                break
            k = run_end

        if close_start == -1:
            result.append(line[i:open_end])
            i = open_end
        else:
            result.append(" " * (close_end - i))
            i = close_end

    return "".join(result)


def resolve_links(
    refs: Sequence[LinkRef],
    *,
    source_namespace: str,
    entries: Sequence[VaultEntry],
    readable_namespaces: Collection[str],
) -> list[ResolvedLink]:
    """Match `refs` against `entries`, per the #10 resolution order.

    `source_namespace` is always searched; it does not need to appear in
    `readable_namespaces`. `readable_namespaces` lists the *other*
    namespaces the caller may read — anything else is treated as unreadable
    and stays dangling even if a matching entry exists for it in `entries`.

    A target is normalised (trimmed, lower-cased) before matching, so
    `[[Foo]]` and `[[foo]]` resolve the same way. A target containing a
    `/` (`ns/slug`) addresses that namespace explicitly: only that one
    namespace is searched (slug, then alias), and it must be the source
    namespace or a readable one, or the link is dangling.

    Without an explicit namespace, the order is: slug in the source
    namespace, then alias in the source namespace, then slug in the other
    readable namespaces (sorted, for a deterministic result), then alias in
    those namespaces, then dangling.

    If more than one entry in a namespace shares a slug or alias (two notes
    of different `type` with the same slug — allowed, since uniqueness is
    only enforced within `namespace/type`), the entry with the
    lexicographically smallest `path` is picked, deterministically.
    """
    by_namespace: dict[str, list[VaultEntry]] = defaultdict(list)
    for entry in entries:
        by_namespace[entry.namespace].append(entry)

    other_namespaces = sorted({ns for ns in readable_namespaces if ns != source_namespace})

    return [_resolve_one(ref, source_namespace, other_namespaces, by_namespace) for ref in refs]


def _resolve_one(
    ref: LinkRef,
    source_namespace: str,
    other_namespaces: list[str],
    by_namespace: dict[str, list[VaultEntry]],
) -> ResolvedLink:
    explicit_namespace, slug = _normalize_target(ref.target)
    if not slug:
        return ResolvedLink(ref=ref, target_path=None)

    if explicit_namespace is not None:
        if explicit_namespace != source_namespace and explicit_namespace not in other_namespaces:
            return ResolvedLink(ref=ref, target_path=None)
        entry = _match_slug(by_namespace, explicit_namespace, slug) or _match_alias(
            by_namespace, explicit_namespace, slug
        )
        return ResolvedLink(ref=ref, target_path=entry.path if entry else None)

    entry = _match_slug(by_namespace, source_namespace, slug)
    if entry is None:
        entry = _match_alias(by_namespace, source_namespace, slug)
    if entry is None:
        entry = _match_in_namespaces(by_namespace, other_namespaces, slug, _match_slug)
    if entry is None:
        entry = _match_in_namespaces(by_namespace, other_namespaces, slug, _match_alias)
    return ResolvedLink(ref=ref, target_path=entry.path if entry else None)


def _normalize_target(target: str) -> tuple[str | None, str]:
    normalized = target.strip().lower()
    namespace, sep, slug = normalized.partition("/")
    if sep:
        return namespace, slug
    return None, namespace


def _match_slug(
    by_namespace: dict[str, list[VaultEntry]], namespace: str, slug: str
) -> VaultEntry | None:
    candidates = [e for e in by_namespace.get(namespace, ()) if e.slug.lower() == slug]
    return min(candidates, key=lambda e: e.path) if candidates else None


def _match_alias(
    by_namespace: dict[str, list[VaultEntry]], namespace: str, slug: str
) -> VaultEntry | None:
    candidates = [
        e for e in by_namespace.get(namespace, ()) if any(a.lower() == slug for a in e.aliases)
    ]
    return min(candidates, key=lambda e: e.path) if candidates else None


_Matcher = Callable[[dict[str, list[VaultEntry]], str, str], VaultEntry | None]


def _match_in_namespaces(
    by_namespace: dict[str, list[VaultEntry]],
    namespaces: list[str],
    slug: str,
    matcher: _Matcher,
) -> VaultEntry | None:
    for namespace in namespaces:
        entry = matcher(by_namespace, namespace, slug)
        if entry is not None:
            return entry
    return None

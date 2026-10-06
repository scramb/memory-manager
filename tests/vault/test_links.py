# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for link extraction and resolution (ADR-0005 "Links", #10)."""

from memory_manager.vault.links import (
    LinkRef,
    ResolvedLink,
    VaultEntry,
    extract_links,
    resolve_links,
)


def _entry(path: str, namespace: str, slug: str, aliases: tuple[str, ...] = ()) -> VaultEntry:
    return VaultEntry(path=path, namespace=namespace, slug=slug, aliases=aliases)


class TestExtractLinks:
    def test_simple_link_without_label(self) -> None:
        refs = extract_links("See [[project-x]] for details.")
        assert refs == [LinkRef(target="project-x", label=None, line=1)]

    def test_link_with_label(self) -> None:
        refs = extract_links("See [[project-x|the project]] for details.")
        assert refs == [LinkRef(target="project-x", label="the project", line=1)]

    def test_label_is_trimmed(self) -> None:
        refs = extract_links("[[project-x|  the project  ]]")
        assert refs[0].label == "the project"

    def test_target_is_trimmed(self) -> None:
        refs = extract_links("[[  project-x  ]]")
        assert refs[0].target == "project-x"

    def test_empty_label_becomes_none(self) -> None:
        refs = extract_links("[[project-x|]]")
        assert refs == [LinkRef(target="project-x", label=None, line=1)]

    def test_empty_target_is_skipped(self) -> None:
        assert extract_links("[[]]") == []

    def test_blank_target_is_skipped(self) -> None:
        assert extract_links("[[   ]]") == []

    def test_line_numbers_are_one_based(self) -> None:
        body = "no link here\n[[first]]\nstill no link\n[[second]]\n"
        refs = extract_links(body)
        assert [(r.target, r.line) for r in refs] == [("first", 2), ("second", 4)]

    def test_multiple_links_on_one_line(self) -> None:
        refs = extract_links("[[a]] and [[b]]")
        assert [r.target for r in refs] == ["a", "b"]

    def test_duplicate_target_keeps_only_first_occurrence(self) -> None:
        body = "[[project-x]] again later: [[project-x]]"
        refs = extract_links(body)
        assert refs == [LinkRef(target="project-x", label=None, line=1)]

    def test_duplicate_across_lines_keeps_first_line(self) -> None:
        body = "[[project-x]]\nmore text\n[[project-x]]"
        refs = extract_links(body)
        assert refs == [LinkRef(target="project-x", label=None, line=1)]

    def test_different_case_targets_are_not_deduplicated(self) -> None:
        refs = extract_links("[[Project-X]] and [[project-x]]")
        assert [r.target for r in refs] == ["Project-X", "project-x"]

    def test_link_inside_fenced_code_block_is_ignored(self) -> None:
        body = "before\n```\n[[x]]\n```\nafter [[z]]"
        refs = extract_links(body)
        assert [r.target for r in refs] == ["z"]

    def test_link_inside_inline_code_span_is_ignored(self) -> None:
        refs = extract_links("see `[[y]]` but not this [[z]]")
        assert [r.target for r in refs] == ["z"]

    def test_fenced_block_and_inline_span_combined(self) -> None:
        body = "intro `[[y]]` middle\n```\n[[x]]\n```\nreal [[z]]"
        refs = extract_links(body)
        assert [r.target for r in refs] == ["z"]

    def test_tilde_fence_is_also_ignored(self) -> None:
        body = "~~~\n[[x]]\n~~~\n[[z]]"
        refs = extract_links(body)
        assert [r.target for r in refs] == ["z"]

    def test_closing_fence_shorter_than_opening_does_not_close(self) -> None:
        body = "````\n[[x]]\n```\nstill fenced [[w]]\n````\n[[z]]"
        refs = extract_links(body)
        assert [r.target for r in refs] == ["z"]

    def test_shorter_backtick_run_inside_span_does_not_close_it(self) -> None:
        # The opening run is length 2; the lone backtick before "mid" is not
        # a valid closer (wrong length), so "[[x]]" stays masked until the
        # matching "``" closes the span.
        refs = extract_links("``[[x]]`mid`` real [[z]]")
        assert [r.target for r in refs] == ["z"]

    def test_unmatched_single_backtick_leaves_link_as_plain_text(self) -> None:
        # No run of length 1 follows, so there is no valid code span at all;
        # the backtick is literal and "[[y]]" is a real link.
        refs = extract_links("`[[y]]`` real [[z]]")
        assert [r.target for r in refs] == ["y", "z"]

    def test_unterminated_inline_backtick_is_treated_as_literal(self) -> None:
        refs = extract_links("odd ` backtick then [[z]]")
        assert [r.target for r in refs] == ["z"]

    def test_no_links_returns_empty_list(self) -> None:
        assert extract_links("just plain text") == []


class TestResolveLinks:
    def test_slug_match_in_source_namespace(self) -> None:
        refs = [LinkRef(target="project-x", label=None, line=1)]
        entries = [_entry("work/project/project-x.md", "work", "project-x")]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved == [ResolvedLink(ref=refs[0], target_path="work/project/project-x.md")]
        assert not resolved[0].dangling

    def test_slug_match_is_case_insensitive(self) -> None:
        refs = [LinkRef(target="Project-X", label=None, line=1)]
        entries = [_entry("work/project/project-x.md", "work", "project-x")]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].target_path == "work/project/project-x.md"

    def test_alias_match_in_source_namespace(self) -> None:
        refs = [LinkRef(target="px", label=None, line=1)]
        entries = [_entry("work/project/project-x.md", "work", "project-x", aliases=("PX",))]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].target_path == "work/project/project-x.md"

    def test_slug_match_wins_over_alias_match_in_source_namespace(self) -> None:
        refs = [LinkRef(target="project-x", label=None, line=1)]
        entries = [
            _entry("work/project/project-x.md", "work", "project-x"),
            _entry("work/fact/other.md", "work", "other", aliases=("project-x",)),
        ]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].target_path == "work/project/project-x.md"

    def test_cross_namespace_slug_fallback(self) -> None:
        refs = [LinkRef(target="project-x", label=None, line=1)]
        entries = [_entry("other/project/project-x.md", "other", "project-x")]
        resolved = resolve_links(
            refs,
            source_namespace="work",
            entries=entries,
            readable_namespaces=("other",),
        )
        assert resolved[0].target_path == "other/project/project-x.md"

    def test_cross_namespace_alias_fallback(self) -> None:
        refs = [LinkRef(target="px", label=None, line=1)]
        entries = [_entry("other/project/project-x.md", "other", "project-x", aliases=("px",))]
        resolved = resolve_links(
            refs,
            source_namespace="work",
            entries=entries,
            readable_namespaces=("other",),
        )
        assert resolved[0].target_path == "other/project/project-x.md"

    def test_cross_namespace_slug_is_preferred_over_cross_namespace_alias(self) -> None:
        refs = [LinkRef(target="project-x", label=None, line=1)]
        entries = [
            _entry("alpha/fact/other.md", "alpha", "other", aliases=("project-x",)),
            _entry("beta/project/project-x.md", "beta", "project-x"),
        ]
        resolved = resolve_links(
            refs,
            source_namespace="work",
            entries=entries,
            readable_namespaces=("alpha", "beta"),
        )
        assert resolved[0].target_path == "beta/project/project-x.md"

    def test_cross_namespace_fallback_is_sorted_deterministically(self) -> None:
        refs = [LinkRef(target="project-x", label=None, line=1)]
        entries = [
            _entry("zeta/project/project-x.md", "zeta", "project-x"),
            _entry("alpha/project/project-x.md", "alpha", "project-x"),
        ]
        resolved = resolve_links(
            refs,
            source_namespace="work",
            entries=entries,
            readable_namespaces=("zeta", "alpha"),
        )
        assert resolved[0].target_path == "alpha/project/project-x.md"

    def test_unreadable_namespace_stays_dangling(self) -> None:
        refs = [LinkRef(target="secret/project-x", label=None, line=1)]
        entries = [_entry("secret/project/project-x.md", "secret", "project-x")]
        resolved = resolve_links(
            refs,
            source_namespace="work",
            entries=entries,
            readable_namespaces=("other",),
        )
        assert resolved[0].dangling
        assert resolved[0].target_path is None

    def test_explicit_namespace_target_resolves_directly(self) -> None:
        refs = [LinkRef(target="other/project-x", label=None, line=1)]
        entries = [
            _entry("work/project/project-x.md", "work", "project-x"),
            _entry("other/project/project-x.md", "other", "project-x"),
        ]
        resolved = resolve_links(
            refs,
            source_namespace="work",
            entries=entries,
            readable_namespaces=("other",),
        )
        assert resolved[0].target_path == "other/project/project-x.md"

    def test_explicit_namespace_target_in_source_namespace(self) -> None:
        refs = [LinkRef(target="work/project-x", label=None, line=1)]
        entries = [_entry("work/project/project-x.md", "work", "project-x")]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].target_path == "work/project/project-x.md"

    def test_no_matching_entry_is_dangling(self) -> None:
        refs = [LinkRef(target="missing", label=None, line=1)]
        resolved = resolve_links(refs, source_namespace="work", entries=(), readable_namespaces=())
        assert resolved[0].dangling

    def test_blank_slug_after_namespace_split_is_dangling(self) -> None:
        refs = [LinkRef(target="work/", label=None, line=1)]
        entries = [_entry("work/project/project-x.md", "work", "project-x")]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].dangling

    def test_duplicate_slug_across_types_picks_smallest_path(self) -> None:
        refs = [LinkRef(target="shared", label=None, line=1)]
        entries = [
            _entry("work/reference/shared.md", "work", "shared"),
            _entry("work/fact/shared.md", "work", "shared"),
        ]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].target_path == "work/fact/shared.md"

    def test_preserves_ref_order_and_identity(self) -> None:
        refs = [
            LinkRef(target="a", label=None, line=1),
            LinkRef(target="b", label=None, line=2),
        ]
        entries = [_entry("work/fact/a.md", "work", "a")]
        resolved = resolve_links(
            refs, source_namespace="work", entries=entries, readable_namespaces=()
        )
        assert resolved[0].ref is refs[0]
        assert resolved[1].ref is refs[1]
        assert resolved[0].target_path == "work/fact/a.md"
        assert resolved[1].dangling

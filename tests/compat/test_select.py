# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `compat/select.py`'s profile resolver (#131, ADR-0010).

Only the resolver's own contract is asserted here: resolution order, rejection of an
unknown override, and the unmapped-`clientInfo.name`-falls-back-to-`default` rule.
`mcp/server.py`'s middleware (which calls this module) and `http.py`'s ASGI middleware
(which sets the override) are exercised end to end by `tests/conformance/`, not here.
"""

from __future__ import annotations

import pytest

from memory_manager.compat.profiles import DEFAULT_PROFILE, UnknownProfile
from memory_manager.compat.select import (
    current_profile_override,
    current_resolved_profile,
    reset_profile_override,
    reset_resolved_profile,
    resolve_profile,
    set_profile_override,
    set_resolved_profile,
)


def test_no_override_and_no_client_info_resolves_to_default() -> None:
    profile = resolve_profile(None)
    assert profile.name == DEFAULT_PROFILE


def test_mapped_client_info_name_resolves_to_its_profile() -> None:
    profile = resolve_profile("claude-code")
    assert profile.name == "claude-code"


@pytest.mark.parametrize("name", ["Claude Code", "CLAUDE-CODE", "claude.ai", "", "mcp"])
def test_unmapped_client_info_name_falls_back_to_default(name: str) -> None:
    profile = resolve_profile(name)
    assert profile.name == DEFAULT_PROFILE


def test_override_wins_over_client_info() -> None:
    profile = resolve_profile("claude-code", override="default")
    assert profile.name == "default"


def test_override_alone_resolves_to_itself() -> None:
    profile = resolve_profile(None, override="claude-code")
    assert profile.name == "claude-code"


def test_unknown_override_is_rejected() -> None:
    with pytest.raises(UnknownProfile):
        resolve_profile(None, override="nope")


class TestProfileOverrideContextvar:
    def test_defaults_to_none(self) -> None:
        assert current_profile_override() is None

    def test_set_then_reset_round_trips(self) -> None:
        assert current_profile_override() is None
        token = set_profile_override("claude-code")
        try:
            assert current_profile_override() == "claude-code"
        finally:
            reset_profile_override(token)
        assert current_profile_override() is None

    @pytest.mark.parametrize("name", ["", "Claude-Code", "claude_code", "claude-opus"])
    def test_rejects_empty_or_wrong_case_or_unknown_names(self, name: str) -> None:
        with pytest.raises(UnknownProfile):
            set_profile_override(name)
        # A rejected `set` must not have touched the contextvar.
        assert current_profile_override() is None


class TestResolvedProfileContextvar:
    def test_defaults_to_default_profile_name(self) -> None:
        assert current_resolved_profile() == DEFAULT_PROFILE

    def test_set_then_reset_round_trips(self) -> None:
        assert current_resolved_profile() == DEFAULT_PROFILE
        token = set_resolved_profile("claude-code")
        try:
            assert current_resolved_profile() == "claude-code"
        finally:
            reset_resolved_profile(token)
        assert current_resolved_profile() == DEFAULT_PROFILE

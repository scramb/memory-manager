# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the compatibility profile registry (#130, ADR-0010).

No `MappingProxyType`/`frozenset` internals are asserted directly - only the contract a
future caller (#131's profile selection, #133's linter) relies on: every documented name
resolves to a `Profile` that carries its own name, an unknown name is rejected rather than
silently mapped, and the registry cannot be mutated from outside.
"""

from __future__ import annotations

from typing import get_args

import pytest

from memory_manager.compat.profiles import (
    DEFAULT_PROFILE,
    DeliveryMode,
    Profile,
    UnknownProfile,
    get_profile,
    profile_names,
)
from memory_manager.mcp.instructions import INSTRUCTIONS

_KNOWN_NAMES = ("default", "claude-ai", "claude-code")


@pytest.mark.parametrize("name", _KNOWN_NAMES)
def test_known_name_resolves_to_itself(name: str) -> None:
    profile = get_profile(name)
    assert profile.name == name


def test_registry_has_exactly_the_known_names() -> None:
    assert profile_names() == tuple(sorted(_KNOWN_NAMES))


def test_default_profile_name_is_in_the_registry() -> None:
    assert DEFAULT_PROFILE in profile_names()


@pytest.mark.parametrize("name", ["claude-opus", "Claude-AI", "CLAUDE-CODE", ""])
def test_unknown_name_is_rejected_with_valid_names_listed(name: str) -> None:
    with pytest.raises(UnknownProfile) as excinfo:
        get_profile(name)
    message = str(excinfo.value)
    assert repr(name) in message
    for valid_name in _KNOWN_NAMES:
        assert valid_name in message


@pytest.mark.parametrize("name", _KNOWN_NAMES)
def test_delivery_mode_is_one_of_the_documented_values(name: str) -> None:
    profile = get_profile(name)
    assert profile.delivery_mode in get_args(DeliveryMode)


@pytest.mark.parametrize("name", _KNOWN_NAMES)
def test_every_profile_has_at_least_one_set_limit(name: str) -> None:
    profile = get_profile(name)
    limits = (
        profile.max_instructions_chars,
        profile.max_tool_description_chars,
        profile.result_budget_chars,
        profile.result_budget_tokens,
        profile.max_tool_name_chars,
        profile.max_tools,
    )
    assert any(limit is not None for limit in limits)


@pytest.mark.parametrize("name", _KNOWN_NAMES)
def test_every_set_limit_is_a_positive_int(name: str) -> None:
    profile = get_profile(name)
    limits = (
        profile.max_instructions_chars,
        profile.max_tool_description_chars,
        profile.result_budget_chars,
        profile.result_budget_tokens,
        profile.max_tool_name_chars,
        profile.max_tools,
    )
    for limit in limits:
        if limit is not None:
            assert isinstance(limit, int) and limit > 0


@pytest.mark.parametrize("name", ["default", "claude-code"])
def test_instructions_fit_the_profiles_that_cap_them(name: str) -> None:
    profile = get_profile(name)
    assert profile.max_instructions_chars is not None
    assert len(INSTRUCTIONS) <= profile.max_instructions_chars


def test_registry_is_not_mutable_from_outside() -> None:
    before = get_profile("default")
    names_before = profile_names()

    mutable_profile: Profile = before
    with pytest.raises(AttributeError):
        mutable_profile.name = "tampered"  # type: ignore[misc]

    assert profile_names() == names_before
    assert get_profile("default") == before

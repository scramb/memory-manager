# SPDX-License-Identifier: AGPL-3.0-only
"""Smoke test for the empty package skeleton."""

from memory_manager import __version__


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)

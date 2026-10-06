# SPDX-License-Identifier: AGPL-3.0-only
"""Self-hosted long-term memory for Claude."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("memory-manager")
except PackageNotFoundError:  # pragma: no cover - only hit outside an installed env
    __version__ = "0.0.0"

__all__ = ["__version__"]

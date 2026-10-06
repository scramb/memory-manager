# SPDX-License-Identifier: AGPL-3.0-only
"""Self-hosted long-term memory for Claude."""

import os
from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("memory-manager")
except PackageNotFoundError:  # pragma: no cover - only hit outside an installed env
    __version__ = "0.0.0"

#: The git commit this process was built from, for `/healthz` (ADR-0002 §13:
#: a modified deployment's build must point back at its own source). Set by
#: the container build (`MM_GIT_SHA`); `"unknown"` outside a built image
#: (e.g. `uv run` from a checkout) rather than failing the whole process.
__commit__: str = os.environ.get("MM_GIT_SHA", "unknown")

__all__ = ["__commit__", "__version__"]

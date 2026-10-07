# SPDX-License-Identifier: AGPL-3.0-only
"""Every `memory_manager` module must import on its own (#118).

`tests/mcp` used to fail to collect at all - but only when collected on its
own, never as part of the full `make check` run - because
`memory_manager.mcp.authz` and `memory_manager.auth` imported each other
(`mcp.authz` needed `auth.tokens.ALL_NAMESPACES`, `auth.prm` needed
`mcp.authz.READ_SCOPE`/`WRITE_SCOPE`, and `auth.prm` runs eagerly from
`auth/__init__.py`). Which module happens to import first inside one
`pytest` process decides which side of the cycle fails, so `tests/auth`
running first in a full `make check` hid it entirely.

Importing every module in its own fresh interpreter, one `import` per
subprocess, catches a cycle like this regardless of import order - an
in-process `importlib.reload`/`sys.modules`-clearing test would not: a
module already fully imported earlier in the same process masks exactly
the partial-initialization state a cycle produces.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys

import pytest

import memory_manager


def _discover_modules() -> list[str]:
    return sorted(
        module.name
        for module in pkgutil.walk_packages(memory_manager.__path__, prefix="memory_manager.")
    )


@pytest.mark.parametrize("module_name", _discover_modules())
def test_module_imports_in_isolation(module_name: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed executable, argument list, no shell
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

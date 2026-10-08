# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for the Helm chart's own tests (#249).

`helm template` renders `charts/memory-manager`; `MM_REQUIRE_HELM=1` turns
a missing `helm` binary into a hard failure instead of a local skip - the
same "skip locally, fail in CI" shape `tests/conftest.py`'s own
`admin_database_url` uses for `MM_TEST_DATABASE_URL` (the deploy job in
`.github/workflows/validate.yml` sets `MM_REQUIRE_HELM=1` directly).

Kept as a single file rather than split into a sibling module: every test
package here has its own `conftest.py` (`tests/vault`, `tests/chart`,
...), and mypy's `explicit_package_bases` (needed so same-named
`conftest.py` files don't collide as duplicate modules, see
`pyproject.toml`'s own comment) resolves a bare `from <name> import X`
to whichever file namespace-package rules give that top-level name -
not necessarily the file in this directory. `ChartRender` therefore
stays local to this file; `test_backend_guard.py` types the `render`
fixture structurally instead of importing it.

`bare_remote`/`human_commit`/`human_delete`/`human_rename`/`vault_config`
are re-exported from `tests/git_fixtures.py`, same as `tests/conftest.py`,
`tests/vault/conftest.py`, `tests/auth/conftest.py` and
`tests/mcp/conftest.py` all do (see `tests/auth/conftest.py`'s own
docstring): at runtime, every `conftest.py` under `tests/` ends up
importable under the bare name `conftest` (no `__init__.py` anywhere
under `tests/`), and whichever one Python's import system resolves first
for a given run is the one a plain `from conftest import human_commit`
elsewhere in the suite actually gets - so every `conftest.py` that could
win that race must carry the same superset, this one included.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from git_fixtures import bare_remote, human_commit, human_delete, human_rename, vault_config

__all__ = [
    "bare_remote",
    "human_commit",
    "human_delete",
    "human_rename",
    "render",
    "vault_config",
]

CHART_DIR = Path(__file__).resolve().parents[2] / "charts" / "memory-manager"


@dataclass(frozen=True)
class ChartRender:
    """The result of one `helm template` invocation."""

    returncode: int
    stdout: str
    stderr: str

    def documents(self) -> list[dict[str, Any]]:
        """Every non-empty YAML document the render produced.

        Raises `AssertionError` if the render itself failed - callers that
        expect a failed render check `returncode`/`stderr` instead.
        """
        if self.returncode != 0:
            raise AssertionError(f"helm template failed (exit {self.returncode}): {self.stderr}")
        return [doc for doc in yaml.safe_load_all(self.stdout) if doc]

    def find(self, kind: str) -> dict[str, Any]:
        """The one rendered object of the given `kind`."""
        matches = [doc for doc in self.documents() if doc.get("kind") == kind]
        if len(matches) != 1:
            raise AssertionError(f"expected exactly one {kind}, found {len(matches)}")
        return matches[0]


def _helm_binary() -> str:
    helm = shutil.which("helm")
    if helm:
        return helm
    reason = "helm is not installed"
    if os.environ.get("MM_REQUIRE_HELM"):
        pytest.fail(f"{reason} (required by MM_REQUIRE_HELM=1)")
    pytest.skip(reason)


@pytest.fixture
def render() -> Callable[..., ChartRender]:
    """`render(values_files=(), set_values=None)` - runs `helm template`
    against `charts/memory-manager` with release name "t" and returns a
    `ChartRender`. `set_values` keys use Helm's own dotted `--set` syntax
    (e.g. `{"storage.backend": "postgres"}`).
    """
    helm = _helm_binary()

    def _render(
        values_files: Sequence[Path | str] = (),
        set_values: Mapping[str, str] | None = None,
    ) -> ChartRender:
        cmd = [helm, "template", "t", str(CHART_DIR)]
        for values_file in values_files:
            cmd += ["-f", str(values_file)]
        if set_values:
            cmd += ["--set", ",".join(f"{key}={value}" for key, value in set_values.items())]
        proc = subprocess.run(  # noqa: S603 - fixed executable, argument list, no shell
            cmd, capture_output=True, text=True, check=False
        )
        return ChartRender(proc.returncode, proc.stdout, proc.stderr)

    return _render

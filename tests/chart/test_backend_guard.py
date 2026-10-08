# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the storage.backend <-> scaling contract (#249, ADR-0007,
ADR-0009 §6): the Git backend stays single replica with no autoscaler;
only storage.backend "postgres" may scale. Exercised through a real `helm
template` render, so both the schema's own "if"/"then"
(values.schema.json) and the template-side guard
(templates/_helpers.tpl's "memory-manager.validate") are proven, not just
read.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import yaml

FIXTURES = Path(__file__).parent / "fixtures"


class _ChartRender(Protocol):
    """Structural stand-in for conftest.py's own `ChartRender` - see that
    file's docstring for why this test does not import it by name."""

    returncode: int
    stdout: str
    stderr: str

    def documents(self) -> list[dict[str, Any]]: ...
    def find(self, kind: str) -> dict[str, Any]: ...


def _without_storage_backend_env(deployment: dict[str, Any]) -> dict[str, Any]:
    """The rendered Deployment, minus the one env entry this task adds -
    everything else must stay exactly what it was before (the Deployment
    selector is immutable; existing single-user installs must upgrade
    without a diff).
    """
    result = copy.deepcopy(deployment)
    containers = result["spec"]["template"]["spec"]["containers"]
    containers[0]["env"] = [e for e in containers[0]["env"] if e["name"] != "STORAGE_BACKEND"]
    return result


def test_default_render_matches_golden_objects(render: Callable[..., _ChartRender]) -> None:
    golden = [
        doc
        for doc in yaml.safe_load_all((FIXTURES / "golden-default-render.yaml").read_text())
        if doc
    ]
    current = render().documents()

    assert len(current) == len(golden)
    for before, after in zip(golden, current, strict=True):
        if after.get("kind") == "Deployment":
            assert after["spec"]["template"]["spec"]["containers"][0]["env"][1] == {
                "name": "STORAGE_BACKEND",
                "value": "git",
            }
            after = _without_storage_backend_env(after)
        assert after == before


def test_git_backend_refuses_more_than_one_replica(render: Callable[..., _ChartRender]) -> None:
    result = render(set_values={"replicaCount": "3"})

    assert result.returncode != 0
    assert "replica" in (result.stdout + result.stderr).lower()


def test_git_backend_refuses_an_autoscaler(render: Callable[..., _ChartRender]) -> None:
    result = render(set_values={"autoscaling.enabled": "true"})

    assert result.returncode != 0
    assert "autoscaler" in (result.stdout + result.stderr).lower()


def test_postgres_backend_allows_more_than_one_replica(render: Callable[..., _ChartRender]) -> None:
    result = render(set_values={"storage.backend": "postgres", "replicaCount": "3"})

    deployment = result.find("Deployment")
    assert deployment["spec"]["replicas"] == 3

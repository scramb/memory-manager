# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the storage.backend <-> scaling contract (#249, ADR-0007,
ADR-0009 §6): the Git backend stays single replica with no autoscaler;
only storage.backend "postgres" may scale. Also guards login.mode "entra"
against the Git backend (#257, ADR-0006 addendum 2026-10-08: the server's
own build_authenticator, http.py, refuses LOGIN_MODE=entra without
STORAGE_BACKEND=postgres). Exercised through a real `helm template`
render, so both the schema's own "if"/"then" (values.schema.json) and the
template-side guard (templates/_helpers.tpl's "memory-manager.validate")
are proven, not just read.
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


def test_git_backend_refuses_entra_login(render: Callable[..., _ChartRender]) -> None:
    # Caught by values.schema.json's own "if"/"then" (login.mode's enum
    # drops "entra" once storage.backend is "git") before
    # templates/_helpers.tpl's "memory-manager.validate" guard ever runs -
    # same layering test_git_backend_refuses_more_than_one_replica above
    # exercises for replicaCount.
    result = render(set_values={"login.mode": "entra"})

    assert result.returncode != 0
    error = (result.stdout + result.stderr).lower()
    assert "login" in error and "mode" in error
    assert "oidc" in error and "password" in error


def test_postgres_backend_allows_more_than_one_replica(render: Callable[..., _ChartRender]) -> None:
    # The generic Deployment above only ever renders for storage.backend
    # "git" (#250); "postgres" renders the separate api Deployment
    # instead (templates/api-deployment.yaml), scaled by api.replicaCount
    # rather than the top-level replicaCount.
    result = render(set_values={"storage.backend": "postgres", "api.replicaCount": "3"})

    deployments = [doc for doc in result.documents() if doc.get("kind") == "Deployment"]
    api_deployment = next(
        d for d in deployments if d["metadata"]["labels"]["app.kubernetes.io/component"] == "api"
    )
    assert api_deployment["spec"]["replicas"] == 3

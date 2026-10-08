# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the api/worker split for storage.backend "postgres" (#250,
ADR-0009 §4/§5): distinct Deployments and selectors, the api PDB, the
preStop/grace wiring, that DATABASE_APP_ROLE never reaches the worker,
the Service only ever targeting api pods, and the storage.backend "git"
render staying free of all three (unchanged single Deployment). Exercised
through a real `helm template` render against values-enterprise.yaml -
see conftest.py's own docstring for why this file types the `render`
fixture structurally instead of importing it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

CHART_DIR = Path(__file__).resolve().parents[2] / "charts" / "memory-manager"
ENTERPRISE_VALUES = CHART_DIR / "values-enterprise.yaml"


class _ChartRender(Protocol):
    """Structural stand-in for conftest.py's own `ChartRender` - see that
    file's docstring for why this test does not import it by name."""

    returncode: int
    stdout: str
    stderr: str

    def documents(self) -> list[dict[str, Any]]: ...
    def find(self, kind: str) -> dict[str, Any]: ...


def _deployments_by_component(render_result: _ChartRender) -> dict[str, dict[str, Any]]:
    """Every rendered Deployment, keyed by its own
    `app.kubernetes.io/component` label - absent (not `None`) for the
    "git"-mode Deployment, which carries no component label at all.
    """
    result: dict[str, dict[str, Any]] = {}
    for doc in render_result.documents():
        if doc.get("kind") != "Deployment":
            continue
        component = doc["metadata"]["labels"].get("app.kubernetes.io/component")
        if component:
            result[component] = doc
    return result


def test_postgres_backend_renders_separate_api_and_worker_deployments(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    deployments = _deployments_by_component(result)
    assert set(deployments) == {"api", "worker"}

    api_selector = deployments["api"]["spec"]["selector"]["matchLabels"]
    worker_selector = deployments["worker"]["spec"]["selector"]["matchLabels"]
    assert api_selector["app.kubernetes.io/component"] == "api"
    assert worker_selector["app.kubernetes.io/component"] == "worker"
    assert api_selector != worker_selector


def test_postgres_backend_renders_the_api_pdb(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    pdb = result.find("PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] == 2
    assert pdb["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "api"


def test_postgres_backend_wires_graceful_shutdown(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    for component, deployment in _deployments_by_component(result).items():
        pod_spec = deployment["spec"]["template"]["spec"]
        assert pod_spec["terminationGracePeriodSeconds"] == 40, component
        container = pod_spec["containers"][0]
        assert container["lifecycle"]["preStop"]["exec"]["command"] == [
            "/bin/sh",
            "-c",
            "sleep 10",
        ], component
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["SHUTDOWN_GRACE_SECONDS"] == "20", component


def test_postgres_backend_worker_never_gets_database_app_role(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])
    deployments = _deployments_by_component(result)

    worker_env = {
        e["name"] for e in deployments["worker"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "DATABASE_APP_ROLE" not in worker_env

    api_env = {
        e["name"]: e.get("value")
        for e in deployments["api"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert api_env["DATABASE_APP_ROLE"]


def test_postgres_backend_service_targets_api_only(render: Callable[..., _ChartRender]) -> None:
    # Two Services render in postgres mode since #264 added a worker one
    # for the ServiceMonitor (templates/worker-service.yaml) - "by name"
    # instead of result.find("Service"), which assumes exactly one.
    result = render(values_files=[ENTERPRISE_VALUES])

    services = {
        doc["metadata"]["name"]: doc for doc in result.documents() if doc["kind"] == "Service"
    }
    service = services["t-memory-manager"]
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "api"


def test_git_backend_render_has_no_worker_deployment_or_pdb(
    render: Callable[..., _ChartRender],
) -> None:
    result = render()

    kinds = {doc.get("kind") for doc in result.documents()}
    assert "PodDisruptionBudget" not in kinds
    assert "worker" not in _deployments_by_component(result)

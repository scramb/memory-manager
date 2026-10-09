# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the ServiceMonitor and the Services it scrapes (#264):
storage.backend "git" keeps the single, unchanged endpoint selecting the
one Service this chart has always rendered; storage.backend "postgres"
instead renders a worker metrics Service alongside the existing api one
and a ServiceMonitor with one endpoint per component, each keeping only
the matching Service's own series and relabeling a "component" label onto
them. Exercised through a real `helm template` render - see conftest.py's
own docstring for why this file types the `render` fixture structurally
instead of importing it.
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


def _services_by_name(render_result: _ChartRender) -> dict[str, dict[str, Any]]:
    return {
        doc["metadata"]["name"]: doc
        for doc in render_result.documents()
        if doc.get("kind") == "Service"
    }


def _deployments_by_name(render_result: _ChartRender) -> dict[str, dict[str, Any]]:
    return {
        doc["metadata"]["name"]: doc
        for doc in render_result.documents()
        if doc.get("kind") == "Deployment"
    }


def test_git_mode_servicemonitor_has_one_unchanged_endpoint(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"serviceMonitor.enabled": "true"})

    service_monitor = result.find("ServiceMonitor")
    assert service_monitor["spec"]["endpoints"] == [
        {"port": "http", "path": "/metrics", "interval": "30s"}
    ]


def test_git_mode_renders_no_worker_service(render: Callable[..., _ChartRender]) -> None:
    result = render(set_values={"serviceMonitor.enabled": "true"})

    assert "t-memory-manager-worker" not in _services_by_name(result)


def test_postgres_mode_servicemonitor_has_two_component_scoped_endpoints(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    endpoints = result.find("ServiceMonitor")["spec"]["endpoints"]
    assert len(endpoints) == 2

    by_replacement = {endpoint["relabelings"][1]["replacement"]: endpoint for endpoint in endpoints}
    assert set(by_replacement) == {"api", "worker"}
    for component, endpoint in by_replacement.items():
        keep_rule = endpoint["relabelings"][0]
        assert keep_rule["action"] == "keep"
        assert keep_rule["regex"] == component
        assert keep_rule["sourceLabels"] == [
            "__meta_kubernetes_service_label_app_kubernetes_io_component"
        ]
        assert endpoint["relabelings"][1]["targetLabel"] == "component"
        assert endpoint["port"] == "http"
        assert endpoint["path"] == "/metrics"


def test_postgres_mode_renders_an_api_and_a_worker_service(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    services = _services_by_name(result)
    assert set(services) == {"t-memory-manager", "t-memory-manager-worker"}
    assert services["t-memory-manager"]["metadata"]["labels"]["app.kubernetes.io/component"] == (
        "api"
    )
    assert (
        services["t-memory-manager-worker"]["metadata"]["labels"]["app.kubernetes.io/component"]
        == "worker"
    )


def test_postgres_mode_service_selectors_match_the_deployments_own_labels(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    services = _services_by_name(result)
    deployments = _deployments_by_name(result)

    api_service = services["t-memory-manager"]
    api_deployment = deployments["t-memory-manager-api"]
    assert api_service["spec"]["selector"] == api_deployment["spec"]["selector"]["matchLabels"]

    worker_service = services["t-memory-manager-worker"]
    worker_deployment = deployments["t-memory-manager-worker"]
    assert (
        worker_service["spec"]["selector"] == worker_deployment["spec"]["selector"]["matchLabels"]
    )

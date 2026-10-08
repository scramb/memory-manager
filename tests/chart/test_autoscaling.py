# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the api/worker autoscaling contract (#251, ADR-0009 addendum
2026-10-08): a CPU HorizontalPodAutoscaler for both Deployments by
default, an optional KEDA ScaledObject instead of the api HPA, never
both for the same Deployment, and the storage.backend "git" guard
extended to the component-scoped api.autoscaling/api.keda/worker.autoscaling
keys (not just the generic top-level "autoscaling" block
test_backend_guard.py already covers). Exercised through a real `helm
template` render against values-enterprise.yaml - see conftest.py's own
docstring for why this file types the `render` fixture structurally
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


def _hpas_by_component(render_result: _ChartRender) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for doc in render_result.documents():
        if doc.get("kind") != "HorizontalPodAutoscaler":
            continue
        component = doc["metadata"]["labels"]["app.kubernetes.io/component"]
        result[component] = doc
    return result


def test_enterprise_default_renders_cpu_hpa_for_api_and_worker(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    hpas = _hpas_by_component(result)
    assert set(hpas) == {"api", "worker"}
    assert not any(doc.get("kind") == "ScaledObject" for doc in result.documents())

    api_hpa = hpas["api"]
    assert api_hpa["spec"]["scaleTargetRef"]["name"] == "t-memory-manager-api"
    assert api_hpa["spec"]["minReplicas"] == 3
    assert api_hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70

    worker_hpa = hpas["worker"]
    assert worker_hpa["spec"]["scaleTargetRef"]["name"] == "t-memory-manager-worker"
    assert worker_hpa["spec"]["minReplicas"] == 2

    api_deployment = next(
        doc
        for doc in result.documents()
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["labels"]["app.kubernetes.io/component"] == "api"
    )
    assert "replicas" not in api_deployment["spec"]


def test_keda_enabled_replaces_the_api_hpa_but_not_the_worker_one(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES],
        set_values={"api.autoscaling.enabled": "false", "api.keda.enabled": "true"},
    )

    hpas = _hpas_by_component(result)
    assert set(hpas) == {"worker"}

    scaled_object = result.find("ScaledObject")
    assert scaled_object["apiVersion"] == "keda.sh/v1alpha1"
    assert scaled_object["spec"]["scaleTargetRef"]["name"] == "t-memory-manager-api"
    assert scaled_object["spec"]["minReplicaCount"] == 3

    triggers = {t["type"]: t for t in scaled_object["spec"]["triggers"]}
    assert set(triggers) == {"cpu", "prometheus"}
    assert triggers["cpu"]["metadata"]["value"] == "70"
    assert triggers["prometheus"]["metadata"]["serverAddress"]
    assert triggers["prometheus"]["metadata"]["query"]
    assert triggers["prometheus"]["metadata"]["threshold"]

    api_deployment = next(
        doc
        for doc in result.documents()
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["labels"]["app.kubernetes.io/component"] == "api"
    )
    assert "replicas" not in api_deployment["spec"]


def test_hpa_and_keda_together_for_api_fails(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"api.keda.enabled": "true"})

    assert result.returncode != 0
    assert "mutually exclusive" in (result.stdout + result.stderr).lower()


def test_git_backend_refuses_the_component_scoped_autoscaler(
    render: Callable[..., _ChartRender],
) -> None:
    for key in ("api.autoscaling.enabled", "api.keda.enabled", "worker.autoscaling.enabled"):
        result = render(set_values={key: "true"})
        assert result.returncode != 0, key
        assert "autoscaler" in (result.stdout + result.stderr).lower(), key

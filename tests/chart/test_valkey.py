# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the optional, no-persistence Valkey Deployment for shared state
(#254, ADR-0009 §2): off by default; once enabled, no persistence
(`--save ""`, `--appendonly no`) and no PVC; `VALKEY_URL` reaches `api`
and `worker`; `valkey.externalUrl` wires the same env without rendering a
Valkey Deployment; `valkey.enabled` and `valkey.externalUrl` are mutually
exclusive. Exercised through a real `helm template` render against
`values-enterprise.yaml` for the `api`/`worker` env assertions - those two
Deployments only render for `storage.backend` `postgres`
(`templates/api-deployment.yaml`/`templates/worker-deployment.yaml`,
ADR-0009 §4) - see conftest.py's own docstring for why this file types
the `render` fixture structurally instead of importing it.
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


def _env(container: dict[str, Any], name: str) -> dict[str, Any] | None:
    entry: dict[str, Any]
    for entry in container.get("env", []):
        if entry["name"] == name:
            return entry
    return None


def _deployments_by_component(render_result: _ChartRender) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for doc in render_result.documents():
        if doc.get("kind") != "Deployment":
            continue
        component = doc["metadata"]["labels"].get("app.kubernetes.io/component")
        if component:
            result[component] = doc
    return result


def test_default_render_has_no_valkey_objects_or_env(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    kinds_by_name = {
        (doc.get("kind"), doc["metadata"]["name"]) for doc in result.documents() if doc.get("kind")
    }
    assert not any(name.endswith("-valkey") for _, name in kinds_by_name)

    deployments = _deployments_by_component(result)
    for component in ("api", "worker"):
        container = deployments[component]["spec"]["template"]["spec"]["containers"][0]
        assert _env(container, "VALKEY_URL") is None
        assert _env(container, "VALKEY_PASSWORD") is None


def test_enabled_renders_a_deployment_without_persistence_or_a_pvc(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"valkey.enabled": "true"})

    kinds = {doc.get("kind") for doc in result.documents()}
    assert "PersistentVolumeClaim" not in kinds

    deployments = [
        doc
        for doc in result.documents()
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == "valkey"
    ]
    assert len(deployments) == 1
    valkey_deployment = deployments[0]
    container = valkey_deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][:4] == ["--save", "", "--appendonly", "no"]

    volumes = valkey_deployment["spec"]["template"]["spec"]["volumes"]
    assert all(v.get("persistentVolumeClaim") is None for v in volumes)
    assert any(v.get("emptyDir") is not None for v in volumes)

    services = [
        doc
        for doc in result.documents()
        if doc.get("kind") == "Service"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == "valkey"
    ]
    assert len(services) == 1


def test_enabled_sets_valkey_url_on_api_and_worker(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"valkey.enabled": "true"})

    deployments = _deployments_by_component(result)
    for component in ("api", "worker"):
        container = deployments[component]["spec"]["template"]["spec"]["containers"][0]
        url_env = _env(container, "VALKEY_URL")
        assert url_env is not None
        assert url_env["value"].startswith("redis://")
        assert "-valkey:6379/0" in url_env["value"]
        assert _env(container, "VALKEY_PASSWORD") is None


def test_enabled_with_existing_secret_wires_requirepass_and_the_url(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES],
        set_values={
            "valkey.enabled": "true",
            "valkey.existingSecret": "my-valkey-secret",
            "valkey.passwordKey": "VALKEY_PASSWORD_KEY",
        },
    )

    deployments = [
        doc
        for doc in result.documents()
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == "valkey"
    ]
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    password_env = _env(container, "VALKEY_PASSWORD")
    assert password_env is not None
    assert password_env["valueFrom"]["secretKeyRef"] == {
        "name": "my-valkey-secret",
        "key": "VALKEY_PASSWORD_KEY",
    }
    assert "--requirepass" in container["args"]
    assert "$(VALKEY_PASSWORD)" in container["args"]

    api_deployment = _deployments_by_component(result)["api"]
    api_container = api_deployment["spec"]["template"]["spec"]["containers"][0]
    assert _env(api_container, "VALKEY_PASSWORD") is not None
    api_url_env = _env(api_container, "VALKEY_URL")
    assert api_url_env is not None
    assert "$(VALKEY_PASSWORD)" in api_url_env["value"]


def test_external_url_wires_the_env_without_a_valkey_deployment(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES],
        set_values={"valkey.externalUrl": "redis://valkey.example.net:6379/0"},
    )

    kinds_by_name = {
        (doc.get("kind"), doc["metadata"]["name"]) for doc in result.documents() if doc.get("kind")
    }
    assert not any(name.endswith("-valkey") for _, name in kinds_by_name)

    deployments = _deployments_by_component(result)
    for component in ("api", "worker"):
        container = deployments[component]["spec"]["template"]["spec"]["containers"][0]
        url_env = _env(container, "VALKEY_URL")
        assert url_env is not None
        assert url_env["value"] == "redis://valkey.example.net:6379/0"


def test_enabled_and_external_url_together_fail_the_render(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES],
        set_values={
            "valkey.enabled": "true",
            "valkey.externalUrl": "redis://valkey.example.net:6379/0",
        },
    )

    assert result.returncode != 0
    assert "mutually exclusive" in (result.stdout + result.stderr).lower()

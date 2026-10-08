# SPDX-License-Identifier: AGPL-3.0-only
"""Guards `deploy/flux/enterprise/helmrelease.yaml` against drifting from
the chart it installs (#257): extracts that HelmRelease's own `spec.values`
and renders the local chart with them - fails if a key the example sets no
longer exists, no longer validates against `values.schema.json`, or trips
one of the chart's own `fail`-ing template guards (the "29a" storage-backend
guard `test_backend_guard.py` covers, `validateEntra`, `validateCnpgInstances`,
...). Exercised through a real `helm template` render - see conftest.py's
own docstring for why this file types the `render` fixture structurally
instead of importing it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTERPRISE_DIR = REPO_ROOT / "deploy" / "flux" / "enterprise"
HELMRELEASE = ENTERPRISE_DIR / "helmrelease.yaml"
EXTERNALSECRET = ENTERPRISE_DIR / "externalsecret.yaml"


class _ChartRender(Protocol):
    """Structural stand-in for conftest.py's own `ChartRender` - see that
    file's docstring for why this test does not import it by name."""

    returncode: int
    stdout: str
    stderr: str

    def documents(self) -> list[dict[str, Any]]: ...
    def find(self, kind: str) -> dict[str, Any]: ...


def _helmrelease_values() -> dict[str, Any]:
    """The `spec.values` block of `deploy/flux/enterprise/helmrelease.yaml`
    - the only document in that file."""
    document = yaml.safe_load(HELMRELEASE.read_text(encoding="utf-8"))
    values: dict[str, Any] = document["spec"]["values"]
    return values


def _write_values(values: dict[str, Any], tmp_path: Path) -> Path:
    values_file = tmp_path / "flux-enterprise-values.yaml"
    values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    return values_file


def test_helmrelease_values_render_against_the_chart(
    render: Callable[..., _ChartRender], tmp_path: Path
) -> None:
    result = render(values_files=[_write_values(_helmrelease_values(), tmp_path)])

    assert result.returncode == 0, result.stderr
    kinds = {doc.get("kind") for doc in result.documents()}
    # The enterprise profile this HelmRelease installs (ADR-0009 §4,
    # #250, #251, #252, #255) - present only once storage.backend is
    # "postgres" and the example's own api/worker/cnpg/networkPolicy
    # values actually turned each one on.
    assert {
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "Cluster",
        "ObjectStore",
        "ScheduledBackup",
        "NetworkPolicy",
    } <= kinds


def test_helmrelease_values_wire_entra_login(
    render: Callable[..., _ChartRender], tmp_path: Path
) -> None:
    values = _helmrelease_values()
    result = render(values_files=[_write_values(values, tmp_path)])

    api_deployment = next(
        doc
        for doc in result.documents()
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == "api"
    )
    env_by_name = {
        e["name"]: e for e in api_deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env_by_name["ENTRA_TENANT_ID"]["value"] == values["login"]["entra"]["tenantId"]
    assert env_by_name["ENTRA_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "memory-manager-secrets",
        "key": "ENTRA_CLIENT_SECRET",
    }


def test_helmrelease_backup_credentials_secret_name_matches_externalsecret(
    render: Callable[..., _ChartRender], tmp_path: Path
) -> None:
    values = _helmrelease_values()
    externalsecret_doc = next(
        doc
        for doc in yaml.safe_load_all(EXTERNALSECRET.read_text(encoding="utf-8"))
        if doc["metadata"]["name"] == "memory-manager-backup-credentials"
    )
    assert (
        values["database"]["cnpg"]["backup"]["existingSecret"]
        == externalsecret_doc["spec"]["target"]["name"]
    )

    result = render(values_files=[_write_values(values, tmp_path)])
    object_store = result.find("ObjectStore")
    s3_credentials = object_store["spec"]["configuration"]["s3Credentials"]
    assert s3_credentials["accessKeyId"]["name"] == "memory-manager-backup-credentials"
    assert s3_credentials["secretAccessKey"]["name"] == "memory-manager-backup-credentials"

# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the CNPG `Cluster`'s own app role bootstrap and the Barman Cloud
plugin backup objects (#252, ADR-0007, ADR-0009 §6): the app role request
transactions switch to under FORCE ROW LEVEL SECURITY
(`src/memory_manager/db/rls.py`'s own `check_app_role`) is created and
granted to the owner at bootstrap; more than one CNPG instance requires
storage.backend "postgres"; the ObjectStore/plugin entry/ScheduledBackup
only render while database.cnpg.backup.enabled is true, never the
deprecated in-tree `barmanObjectStore` field. Exercised through a real
`helm template` render - see conftest.py's own docstring for why this
file types the `render` fixture structurally instead of importing it.
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


def test_default_render_creates_and_grants_the_app_role(
    render: Callable[..., _ChartRender],
) -> None:
    result = render()

    cluster = result.find("Cluster")
    statements = cluster["spec"]["bootstrap"]["initdb"]["postInitApplicationSQL"]
    assert 'CREATE ROLE "memory_manager_app" NOLOGIN;' in statements
    assert 'GRANT "memory_manager_app" TO memory_manager;' in statements


def test_app_role_sql_follows_a_custom_app_role_value(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"database.appRole": "custom_role"})

    cluster = result.find("Cluster")
    statements = cluster["spec"]["bootstrap"]["initdb"]["postInitApplicationSQL"]
    assert 'CREATE ROLE "custom_role" NOLOGIN;' in statements
    assert 'GRANT "custom_role" TO memory_manager;' in statements


def test_enterprise_profile_runs_three_cnpg_instances(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    cluster = result.find("Cluster")
    assert cluster["spec"]["instances"] == 3


def test_git_backend_refuses_more_than_one_cnpg_instance(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"database.cnpg.instances": "3"})

    assert result.returncode != 0
    assert "instances" in (result.stdout + result.stderr).lower()


def test_default_render_has_no_backup_objects(render: Callable[..., _ChartRender]) -> None:
    result = render()

    kinds = {doc.get("kind") for doc in result.documents()}
    assert "ObjectStore" not in kinds
    assert "ScheduledBackup" not in kinds

    cluster = result.find("Cluster")
    assert "plugins" not in cluster["spec"]


def test_enterprise_profile_renders_backup_objects_with_default_retention(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    object_store = result.find("ObjectStore")
    assert object_store["spec"]["retentionPolicy"] == "30d"
    assert "barmanObjectStore" not in object_store["spec"]
    assert "barmanObjectStore" not in str(object_store)

    cluster = result.find("Cluster")

    scheduled_backup = result.find("ScheduledBackup")
    assert scheduled_backup["spec"]["method"] == "plugin"
    assert scheduled_backup["spec"]["pluginConfiguration"]["name"] == (
        "barman-cloud.cloudnative-pg.io"
    )
    assert scheduled_backup["spec"]["cluster"]["name"] == cluster["metadata"]["name"]

    plugin = cluster["spec"]["plugins"][0]
    assert plugin["name"] == "barman-cloud.cloudnative-pg.io"
    assert plugin["isWALArchiver"] is True
    assert plugin["parameters"]["barmanObjectName"] == object_store["metadata"]["name"]
    assert "barmanObjectStore" not in cluster["spec"].get("backup", {})


def test_no_rendered_object_uses_the_deprecated_in_tree_field(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    for doc in result.documents():
        assert "barmanObjectStore" not in str(doc), doc.get("kind")

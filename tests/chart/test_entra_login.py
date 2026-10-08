# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the chart's own login.mode "entra" wiring (#257): the
ENTRA_TENANT_ID/ENTRA_CLIENT_ID/ENTRA_CLIENT_SECRET envs
`EntraAuthenticator.from_env` (src/memory_manager/auth/login_entra.py)
requires, the optional ENTRA_* overrides rendered only when set, that no
OIDC_* env leaks into this mode, the validateEntra guard against a missing
tenantId/clientId, and the chart-managed Secret's own ENTRA_CLIENT_SECRET
key. Exercised through a real `helm template` render against
values-enterprise.yaml - see conftest.py's own docstring for why this file
types the `render` fixture structurally instead of importing it.
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


def _api_container(render_result: _ChartRender) -> dict[str, Any]:
    deployment = next(
        doc
        for doc in render_result.documents()
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["labels"].get("app.kubernetes.io/component") == "api"
    )
    container: dict[str, Any] = deployment["spec"]["template"]["spec"]["containers"][0]
    return container


def test_entra_mode_renders_the_required_envs(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])
    container = _api_container(result)

    env_by_name = {e["name"]: e for e in container["env"]}
    assert env_by_name["ENTRA_TENANT_ID"]["value"] == "00000000-0000-0000-0000-000000000000"
    assert env_by_name["ENTRA_CLIENT_ID"]["value"] == "00000000-0000-0000-0000-000000000000"
    assert env_by_name["ENTRA_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "t-memory-manager-secrets",
        "key": "ENTRA_CLIENT_SECRET",
    }

    # Unused in this mode (EntraAuthenticator.from_env never reads it).
    assert "OIDC_ISSUER" not in env_by_name
    assert "OIDC_CLIENT_ID" not in env_by_name
    assert "OIDC_CLIENT_SECRET" not in env_by_name
    assert "OIDC_ALLOWED_EMAILS" not in env_by_name

    # Optional overrides stay unset when values-enterprise.yaml leaves them
    # at the chart's own empty/false/null defaults.
    for optional_env in (
        "ENTRA_ALLOWED_TENANTS",
        "ENTRA_AUTHORITY",
        "ENTRA_GRAPH_URL",
        "ENTRA_ALLOW_INSECURE_AUTHORITY",
        "ENTRA_GROUPS_TTL_SECONDS",
        "ENTRA_ACCESS_TOKEN_MINUTES",
        "ENTRA_MAX_SESSION",
    ):
        assert optional_env not in env_by_name


def test_entra_mode_renders_optional_overrides_when_set(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES],
        set_values={
            "login.entra.allowedTenants": "11111111-1111-1111-1111-111111111111",
            "login.entra.authority": "https://login.example.com",
            "login.entra.graphUrl": "https://graph.example.com",
            "login.entra.allowInsecureAuthority": "true",
            "login.entra.groupsTtlSeconds": "900",
            "login.entra.accessTokenMinutes": "15",
            "login.entra.maxSessionSeconds": "43200",
        },
    )
    container = _api_container(result)
    env_by_name = {e["name"]: e.get("value") for e in container["env"]}

    assert env_by_name["ENTRA_ALLOWED_TENANTS"] == "11111111-1111-1111-1111-111111111111"
    assert env_by_name["ENTRA_AUTHORITY"] == "https://login.example.com"
    assert env_by_name["ENTRA_GRAPH_URL"] == "https://graph.example.com"
    assert env_by_name["ENTRA_ALLOW_INSECURE_AUTHORITY"] == "true"
    assert env_by_name["ENTRA_GROUPS_TTL_SECONDS"] == "900"
    assert env_by_name["ENTRA_ACCESS_TOKEN_MINUTES"] == "15"  # noqa: S105 - not a secret
    assert env_by_name["ENTRA_MAX_SESSION"] == "43200"


def test_entra_mode_without_tenant_or_client_id_fails(render: Callable[..., _ChartRender]) -> None:
    for key in ("login.entra.tenantId", "login.entra.clientId"):
        result = render(values_files=[ENTERPRISE_VALUES], set_values={key: ""})
        assert result.returncode != 0, key
        assert (
            "login.entra.tenantid and login.entra.clientid"
            in (result.stdout + result.stderr).lower()
        ), key


def test_entra_client_secret_key_in_chart_managed_secret(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES],
        set_values={
            "secrets.create": "true",
            "secrets.values.entraClientSecret": "s3cr3t",
        },
    )

    secret = result.find("Secret")
    assert secret["stringData"]["ENTRA_CLIENT_SECRET"] == "s3cr3t"  # noqa: S105 - test fixture value, not a real secret

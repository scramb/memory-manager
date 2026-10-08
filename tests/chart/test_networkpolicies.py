# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the NetworkPolicy objects storage.backend "git" and "postgres"
each render once networkPolicy.enabled is true (#255): storage.backend
"git" keeps the single policy this chart has always rendered, unchanged;
storage.backend "postgres" instead renders one policy per component -
api, worker, the optional Valkey Deployment and the CNPG Cluster's own
instance pods - each selected by exactly one policy, none of them with
an unconditional allow-all ingress rule, and the worker policy with no
gateway ingress source at all (templates/networkpolicy.yaml, ADR-0009
§4). Exercised through a real `helm template` render - see conftest.py's
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


def _network_policies(render_result: _ChartRender) -> list[dict[str, Any]]:
    return [doc for doc in render_result.documents() if doc.get("kind") == "NetworkPolicy"]


def _network_policies_by_name(render_result: _ChartRender) -> dict[str, dict[str, Any]]:
    return {np["metadata"]["name"]: np for np in _network_policies(render_result)}


def _has_allow_all_ingress(policy: dict[str, Any]) -> bool:
    """True if any ingress rule has neither a "from" nor a "ports" -
    the literal NetworkPolicyIngressRule shape that admits every source
    on every port, the one the git-mode policy's own egress uses for
    its "allowAll" knob (`- {}`) but which no ingress rule in this chart
    ever should.
    """
    return any(
        "from" not in rule and "ports" not in rule for rule in policy["spec"].get("ingress", [])
    )


def test_default_render_has_no_network_policy(render: Callable[..., _ChartRender]) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"networkPolicy.enabled": "false"})

    assert _network_policies(result) == []


def test_git_backend_renders_the_single_unchanged_policy(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"networkPolicy.enabled": "true"})

    policies = _network_policies(result)
    assert len(policies) == 1
    policy = policies[0]
    assert policy["metadata"]["name"] == "t-memory-manager"
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "memory-manager",
        "app.kubernetes.io/instance": "t",
    }
    assert policy["spec"]["ingress"] == [
        {
            "from": [{"namespaceSelector": {}, "podSelector": {}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]
    assert policy["spec"]["egress"][-1] == {}


def test_postgres_backend_renders_one_policy_per_component(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"valkey.enabled": "true"})

    policies_by_name = _network_policies_by_name(result)
    assert set(policies_by_name) == {
        "t-memory-manager-api",
        "t-memory-manager-worker",
        "t-memory-manager-valkey",
        "t-memory-manager-db",
    }

    for policy in policies_by_name.values():
        assert not _has_allow_all_ingress(policy), policy["metadata"]["name"]


def test_postgres_backend_selects_each_component_exactly_once(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"valkey.enabled": "true"})

    api_pods = {
        "app.kubernetes.io/name": "memory-manager",
        "app.kubernetes.io/instance": "t",
        "app.kubernetes.io/component": "api",
    }
    worker_pods = {**api_pods, "app.kubernetes.io/component": "worker"}
    valkey_pods = {**api_pods, "app.kubernetes.io/component": "valkey"}
    cnpg_pods = {"cnpg.io/cluster": "t-memory-manager-db", "cnpg.io/podRole": "instance"}

    policies_by_name = _network_policies_by_name(result)
    assert policies_by_name["t-memory-manager-api"]["spec"]["podSelector"]["matchLabels"] == (
        api_pods
    )
    assert policies_by_name["t-memory-manager-worker"]["spec"]["podSelector"]["matchLabels"] == (
        worker_pods
    )
    assert policies_by_name["t-memory-manager-valkey"]["spec"]["podSelector"]["matchLabels"] == (
        valkey_pods
    )
    assert policies_by_name["t-memory-manager-db"]["spec"]["podSelector"]["matchLabels"] == (
        cnpg_pods
    )

    # Every podSelector above is a distinct set of matchLabels - no two
    # policies can ever select the same pod.
    selectors = [
        tuple(sorted(np["spec"]["podSelector"]["matchLabels"].items()))
        for np in policies_by_name.values()
    ]
    assert len(selectors) == len(set(selectors))


def test_postgres_backend_worker_has_no_gateway_ingress(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    worker_policy = _network_policies_by_name(result)["t-memory-manager-worker"]
    ingress_rules = worker_policy["spec"]["ingress"]
    assert len(ingress_rules) == 1
    assert len(ingress_rules[0]["from"]) == 1

    api_policy = _network_policies_by_name(result)["t-memory-manager-api"]
    assert len(api_policy["spec"]["ingress"][0]["from"]) == 2


def test_postgres_backend_api_and_worker_egress_reaches_postgres_and_https(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    policies_by_name = _network_policies_by_name(result)
    for name in ("t-memory-manager-api", "t-memory-manager-worker"):
        egress_ports = {
            rule["ports"][0]["port"] for rule in policies_by_name[name]["spec"]["egress"]
        }
        assert {53, 5432, 443} <= egress_ports


def test_postgres_backend_without_valkey_renders_no_valkey_policy(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    assert "t-memory-manager-valkey" not in _network_policies_by_name(result)


def test_postgres_backend_without_cnpg_renders_no_cnpg_policy(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES], set_values={"database.cnpg.enabled": "false"})

    assert "t-memory-manager-db" not in _network_policies_by_name(result)


def test_postgres_backend_cnpg_policy_accepts_app_pods_and_peers_only(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])

    cnpg_policy = _network_policies_by_name(result)["t-memory-manager-db"]
    postgres_ingress = cnpg_policy["spec"]["ingress"][0]
    assert postgres_ingress["ports"] == [{"protocol": "TCP", "port": 5432}]
    sources = postgres_ingress["from"]
    assert len(sources) == 3
    # cnpg.io/cluster alone, not cnpg.io/podRole: instance too - see
    # test_postgres_backend_cnpg_policy_peers_match_cluster_label_only below
    # for why the peer selector has to stay this broad.
    assert {"cnpg.io/cluster": "t-memory-manager-db"} in [
        peer["podSelector"]["matchLabels"] for peer in sources
    ]

    operator_ingress = cnpg_policy["spec"]["ingress"][1]
    assert operator_ingress["ports"] == [{"protocol": "TCP", "port": 8000}]


def test_postgres_backend_cnpg_policy_peers_match_cluster_label_only(
    render: Callable[..., _ChartRender],
) -> None:
    """A joining replica's own pg_basebackup step runs as a Job, not an
    "instance" pod (CNPG `pkg/specs/jobs.go`'s own `JoinReplicaInstance`/
    `CreatePrimaryJob`, release-1.26) - labelled cnpg.io/cluster and
    cnpg.io/jobRole, never cnpg.io/podRole: instance. Both the ingress "from"
    and the egress "to" peer selector must therefore match cnpg.io/cluster
    alone: requiring cnpg.io/podRole: instance too would let the primary
    accept a connection from an already-running replica but reject the very
    Job that is still joining - confirmed locally (the kind E2E, #258, ran
    with networkPolicy.enabled and never got a CNPG Cluster past 2/3 ready
    instances until this test's own assertion held)."""
    result = render(values_files=[ENTERPRISE_VALUES])

    cnpg_policy = _network_policies_by_name(result)["t-memory-manager-db"]
    peer_selector = {"cnpg.io/cluster": "t-memory-manager-db"}

    postgres_ingress = cnpg_policy["spec"]["ingress"][0]
    ingress_peers = [peer["podSelector"]["matchLabels"] for peer in postgres_ingress["from"]]
    assert peer_selector in ingress_peers
    assert not any("cnpg.io/podRole" in peer for peer in ingress_peers)

    peer_egress_rules = [
        rule
        for rule in cnpg_policy["spec"]["egress"]
        if rule.get("ports") == [{"protocol": "TCP", "port": 5432}]
    ]
    assert len(peer_egress_rules) == 1
    egress_peers = [peer["podSelector"]["matchLabels"] for peer in peer_egress_rules[0]["to"]]
    assert egress_peers == [peer_selector]


def test_postgres_backend_cnpg_policy_egress_https_only_with_backup_enabled(
    render: Callable[..., _ChartRender],
) -> None:
    without_backup = render(
        values_files=[ENTERPRISE_VALUES], set_values={"database.cnpg.backup.enabled": "false"}
    )
    cnpg_policy = _network_policies_by_name(without_backup)["t-memory-manager-db"]
    egress_ports = {rule["ports"][0]["port"] for rule in cnpg_policy["spec"]["egress"]}
    assert 443 not in egress_ports

    with_backup = render(values_files=[ENTERPRISE_VALUES])
    cnpg_policy = _network_policies_by_name(with_backup)["t-memory-manager-db"]
    egress_ports = {rule["ports"][0]["port"] for rule in cnpg_policy["spec"]["egress"]}
    assert 443 in egress_ports

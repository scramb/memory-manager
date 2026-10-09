# SPDX-License-Identifier: AGPL-3.0-only
"""Guards the Grafana dashboard and the PrometheusRule alerts (#265): every
`mm_*` metric name either one references must exist in
`observability/metrics.py`'s own Prometheus registry, so a renamed or
removed metric breaks this test instead of silently going stale in a
dashboard panel or an alert expression nobody notices never fires.
`storage.backend` `postgres` adds two more alerts (api replica count, CNPG
backup failure) that reference non-`mm_*` metrics (`kube_deployment_...`,
`barman_cloud_...`) this chart's own server never exports - out of this
test's own scope (`docs/research/cnpg-backups.md` is where those are
verified instead), checked here only for "the extra alerts render", not
for their own metric names.

Also covers the CNPG `PodMonitor` (`templates/cnpg-podmonitor.yaml`) that
`serviceMonitor.enabled` renders alongside the `Cluster` once
`storage.backend` is `postgres` - the scrape path the backup alert's own
metrics need, see `docs/research/cnpg-backups.md` for why that alert
still stays dormant below CNPG 1.27 regardless.

Exercised through a real `helm template` render for the PrometheusRule -
see conftest.py's own docstring for why this file types the `render`
fixture structurally instead of importing it - and a direct read of
`dashboards/memory-manager.json` for the dashboard, which is a static
chart file `templates/grafana-dashboard-configmap.yaml` loads verbatim
with Helm's own file-inclusion helper rather than templating.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from prometheus_client import REGISTRY

from memory_manager.observability.metrics import set_embedding_lag_seconds

CHART_DIR = Path(__file__).resolve().parents[2] / "charts" / "memory-manager"
ENTERPRISE_VALUES = CHART_DIR / "values-enterprise.yaml"
DASHBOARD_PATH = CHART_DIR / "dashboards" / "memory-manager.json"

_MM_METRIC_NAME = re.compile(r"\bmm_[A-Za-z0-9_]+\b")


class _ChartRender(Protocol):
    """Structural stand-in for conftest.py's own `ChartRender` - see that
    file's docstring for why this test does not import it by name."""

    returncode: int
    stdout: str
    stderr: str

    def documents(self) -> list[dict[str, Any]]: ...
    def find(self, kind: str) -> dict[str, Any]: ...


def _exported_metric_names() -> set[str]:
    """Every full metric name `/metrics` can ever expose, derived from the
    real registry's own `Collector.collect()` rather than from a hand-kept
    list here - a metric with a label dimension and no recorded sample yet
    (every counter/histogram in `metrics.py`) still shows up in `collect()`
    as an empty family, so this works without having to drive each one
    through a real call first. Suffixed per Prometheus client conventions
    (`CollectorRegistry`'s own behaviour, verified directly against a real
    collect() call): a counter's family name already has any `_total` the
    caller gave it stripped off, a histogram's family name carries none of
    `_bucket`/`_sum`/`_count` yet, gauge/info are exactly what they look
    like once suffixed.

    `set_embedding_lag_seconds` is called first so `mm_embedding_lag_seconds`
    (the one metric here that is a custom `Collector`, absent from
    `collect()` altogether while unset - `metrics.py`'s own module
    docstring) is actually present to collect.
    """
    set_embedding_lag_seconds(1.0)
    names: set[str] = set()
    for family in REGISTRY.collect():
        if not family.name.startswith("mm_") and family.name != "mm_build":
            continue
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names.update({f"{family.name}_bucket", f"{family.name}_sum", f"{family.name}_count"})
        elif family.type == "info":
            names.add(f"{family.name}_info")
        else:
            names.add(family.name)
    return names


def _assert_every_mm_metric_is_exported(text: str, exported: set[str]) -> None:
    found = set(_MM_METRIC_NAME.findall(text))
    assert found, "expected at least one mm_* metric name in the rendered text"
    unknown = found - exported
    assert not unknown, f"mm_* names not in the exported registry: {sorted(unknown)}"


def test_dashboard_metric_names_exist_in_the_registry() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    _assert_every_mm_metric_is_exported(json.dumps(dashboard), _exported_metric_names())


def test_dashboard_has_a_panel_for_every_issue_topic() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text())
    exprs = " ".join(target["expr"] for panel in dashboard["panels"] for target in panel["targets"])
    for expected in (
        "mm_tool_calls_total",
        "mm_tool_duration_seconds_bucket",
        "mm_search_duration_seconds_bucket",
        "mm_rate_limit_hits_total",
        "mm_jobs_pending",
        "mm_jobs_oldest_pending_age_seconds",
        "mm_embedding_lag_seconds",
        "kube_deployment_status_replicas_ready",
        "mm_build_info",
    ):
        assert expected in exprs, f"no panel references {expected}"


def test_prometheus_rule_metric_names_exist_in_the_registry(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"prometheusRule.enabled": "true"})
    rule = result.find("PrometheusRule")
    _assert_every_mm_metric_is_exported(json.dumps(rule), _exported_metric_names())


def test_prometheus_rule_renders_seven_rules_by_default(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"prometheusRule.enabled": "true"})
    rule = result.find("PrometheusRule")
    alerts = [r["alert"] for group in rule["spec"]["groups"] for r in group["rules"]]
    assert alerts == [
        "MemoryManagerSearchLatencyHigh",
        "MemoryManagerReadLatencyHigh",
        "MemoryManagerWriteLatencyHigh",
        "MemoryManagerErrorRatioHigh",
        "MemoryManagerRateLimitHitsSustained",
        "MemoryManagerEmbeddingLagHigh",
        "MemoryManagerJobsBacklogStuck",
    ]


def test_prometheus_rule_adds_api_and_backup_alerts_for_the_postgres_backend(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])
    rule = result.find("PrometheusRule")
    alerts = {r["alert"] for group in rule["spec"]["groups"] for r in group["rules"]}
    assert "MemoryManagerApiReplicasLow" in alerts
    assert "MemoryManagerCnpgBackupFailed" in alerts


def test_prometheus_rule_thresholds_are_settable(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        set_values={
            "prometheusRule.enabled": "true",
            "prometheusRule.thresholds.searchP95Seconds": "1.5",
        }
    )
    rule = result.find("PrometheusRule")
    search_rule = next(
        r
        for group in rule["spec"]["groups"]
        for r in group["rules"]
        if r["alert"] == "MemoryManagerSearchLatencyHigh"
    )
    assert "> 1.5" in search_rule["expr"]


def test_prometheus_rule_is_off_by_default(render: Callable[..., _ChartRender]) -> None:
    result = render()
    assert [d for d in result.documents() if d.get("kind") == "PrometheusRule"] == []


def test_grafana_dashboard_configmap_carries_the_same_json_as_the_static_file(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"grafanaDashboard.enabled": "true"})
    config_map = result.find("ConfigMap")
    rendered = json.loads(config_map["data"]["memory-manager.json"])
    assert rendered == json.loads(DASHBOARD_PATH.read_text())


def test_grafana_dashboard_configmap_carries_the_sidecar_label(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"grafanaDashboard.enabled": "true"})
    config_map = result.find("ConfigMap")
    assert config_map["metadata"]["labels"]["grafana_dashboard"] == "1"


def test_grafana_dashboard_configmap_is_off_by_default(
    render: Callable[..., _ChartRender],
) -> None:
    result = render()
    assert [d for d in result.documents() if d.get("kind") == "ConfigMap"] == []


def test_cnpg_podmonitor_renders_for_the_postgres_backend(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(values_files=[ENTERPRISE_VALUES])
    pod_monitor = result.find("PodMonitor")
    assert pod_monitor["spec"]["selector"]["matchLabels"] == {
        "cnpg.io/cluster": "t-memory-manager-db"
    }
    assert pod_monitor["spec"]["podMetricsEndpoints"] == [{"port": "metrics", "interval": "30s"}]


def test_cnpg_podmonitor_is_off_for_the_git_backend_even_with_servicemonitor_on(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(set_values={"serviceMonitor.enabled": "true"})
    assert [d for d in result.documents() if d.get("kind") == "PodMonitor"] == []


def test_cnpg_podmonitor_is_off_without_servicemonitor_even_for_the_postgres_backend(
    render: Callable[..., _ChartRender],
) -> None:
    result = render(
        values_files=[ENTERPRISE_VALUES], set_values={"serviceMonitor.enabled": "false"}
    )
    assert [d for d in result.documents() if d.get("kind") == "PodMonitor"] == []

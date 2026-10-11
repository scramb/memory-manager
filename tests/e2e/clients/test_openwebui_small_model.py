# SPDX-License-Identifier: AGPL-3.0-only
"""#151's own measurement (WP-44, F-02, M13): does the filter mode (#150) help a
small, local 7-8B model (via Ollama) give usable answers, against memory-manager's
own fictional `examples/vault`.

Opt-in only (`MM_E2E_OPENWEBUI_SMALL_MODEL=1`, same pattern as `MM_E2E_OPENWEBUI` in
`test_openwebui.py` - grep that name): brings up a real, from-scratch Open WebUI
stack with the `ollama` compose profile added (`deploy/openwebui/
examples-vault-overlay.yml`), pulls a real, pinned ~5 GB model and runs every
question in `small_model_questions.yaml` once with the filter on and once with it
off, against the real model - far too slow for anything but a scheduled or manual
run (#151's own Definition of Done: nightly, never per-PR). Deliberately its own,
thin subprocess/HTTP helpers rather than importing anything from `test_openwebui.py`:
that module's fixtures bring up the `oidc-git-overlay.yml`/`postgres-entra-
overlay.yml` stacks and a scripted stub model, none of which this measurement needs
or should depend on.

Stack lifecycle, in order (each step its own helper below):
1. `_refuse_if_stack_already_running` - never touches a `mm-openwebui` project this
   module did not itself start (same reasoning as `test_openwebui.py`'s own check;
   this module is this stack's only user for the duration of its run).
2. `_bring_up_ollama` - starts only the `ollama` compose-profile service (not part
   of a plain `make openwebui-up`, see `docker-compose.yml`'s own header comment),
   waited for healthy the same way `scripts/openwebui-up.sh` waits for every other
   long-running service.
3. `_pull_model` - `ollama pull` the pinned tag (`_MODEL_TAG`) inside that
   container, with a few retries (a multi-GB pull over a real network flakes
   sometimes), then reads the pulled model's own short id back via `ollama list`
   and checks it is a prefix of `_MODEL_DIGEST` - the one live check that the tag
   this run actually pulled is the same bytes `_MODEL_DIGEST` was retrieved
   against (recorded in the written results, never only asserted silently).
4. `_bring_up_stack` - `make openwebui-up OPENWEBUI_EXTRA_COMPOSE_FILES=
   deploy/openwebui/examples-vault-overlay.yml`: the full stack, with that overlay
   seeding the vault "remote" with a verbatim copy of `examples/vault` (instead of
   the base file's single welcome note) before memory-manager's own startup
   `reindex` ever runs, and raising Open WebUI's own HTTP client timeout/enabling
   its Ollama API (that overlay's own header comment explains both).
5. `_tear_down_stack` (always run, in `finally`) - `make openwebui-down` with the
   same overlay, which also removes every `ollama`-profile container and volume
   (`Makefile`'s own `openwebui-down` target already passes `--profile ollama
   down -v` unconditionally) - the pulled model is not cached between runs, by
   design (`Nicht dabei`: no model cache in CI).

Within the stack, the one test function (`test_filter_on_vs_off_recall`) drives:
admin signup, installing and activating the filter (`#150`'s own Function API
dance, unmodified from `test_openwebui.py`'s own `install_filter`/`activate_filter`
pattern), setting `Filter.Valves` to the *shipped defaults* (`max_notes=5`,
`token_budget=400` - never tuned for this measurement, owner decision 2026-10-11:
"report honestly ... do NOT tune"), disabling Open WebUI's own built-in memory, and
creating one personal token (`memory-manager token create --kind personal --owner
... --role Memory.User`, no `--namespace` - `LOGIN_NAMESPACES=*`, set by the
overlay, is what lets this token actually read `examples/vault`'s own namespaces,
see that overlay's own comment) that becomes the admin's own `UserValves.token`.

Per question: one direct `memory_search` call (the filter's own query/limit) as a
diagnostic ("fact in context" - would the filter's own search even have surfaced
the fact, independent of what the model then does with it), then one real chat
turn with the filter on, then - once every question has run once - `Valves.
enabled=false` and the same questions again with the filter off. A fact counts as
present in an answer by a case-insensitive, umlaut-folded substring match against
any of its `expected_any` variants (`_fact_present`); `_validate_questions` checks
once, before any of this runs, that no such variant is itself a substring of its
own question (a model could otherwise "pass" by parroting the question).

Results (full detail: every question, both answers, latencies, the diagnostic
column, and the Ollama token-rate info Open WebUI's own chat-detail JSON carries
when present) are written as `results.json` and `results.md` under
`MM_SMALL_MODEL_RESULTS_DIR` (default: `tests/e2e/clients/.small-model-results/`,
gitignored). The on/off hit rates are asserted last, never silently swallowed into
a report-only shape (owner decision 2026-10-11): `on >= 0.7` and `on > off`, or the
test fails - a red result here is itself the measurement #151 asks for, not a bug
to work around.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from e2e.openwebui_driver import OpenWebUiDriver

pytestmark = pytest.mark.skipif(
    os.environ.get("MM_E2E_OPENWEBUI_SMALL_MODEL") != "1",
    reason="opt-in only (MM_E2E_OPENWEBUI_SMALL_MODEL=1) - pulls a real ~5GB model "
    "and drives a real Open WebUI + Ollama stack end to end; see this module's own "
    "docstring",
)

__all__: list[str] = []

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROJECT = "mm-openwebui"
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "openwebui" / "docker-compose.yml"
_OVERLAY = _REPO_ROOT / "deploy" / "openwebui" / "examples-vault-overlay.yml"
_OVERLAY_RELATIVE = "deploy/openwebui/examples-vault-overlay.yml"
_FILTER_PATH = _REPO_ROOT / "integrations" / "openwebui" / "filter_memory.py"
_QUESTIONS_PATH = Path(__file__).resolve().parent / "small_model_questions.yaml"

_MCP_URL = "http://localhost:18081/mcp"
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}

_PODMAN = shutil.which("podman") or "podman"
_MAKE = shutil.which("make") or "make"

# --- #151's own pinned model (decided 2026-10-11, retrieved against the tag below
# that day): Apache-2.0, small enough (~4.7 GB, q4_K_M) to fit this benchmark's own
# budget. Kept as constants here and in docs/benchmarks/openwebui-small-model.md,
# never in deploy/ YAML (scripts/check-deploy-placeholders.sh would flag the tag's
# own colon-separated shape there the same way it flags an operator hostname).
_MODEL_TAG = "qwen2.5:7b-instruct-q4_K_M"
_MODEL_DIGEST = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"
_MODEL_LICENSE = "Apache-2.0"
_MODEL_RETRIEVED = "2026-10-11"

_OWNER_OID = "small-model-benchmark-user"
_FILTER_FUNCTION_ID = "memmgr_filter"
# Shipped defaults (`integrations/openwebui/filter_memory.py`'s own `Filter.Valves`)
# - never tuned for this measurement (owner decision 2026-10-11).
_MAX_NOTES = 5
_TOKEN_BUDGET = 400
_MAX_ANSWER_TOKENS = 220

_ON_THRESHOLD = 0.7

_PULL_TIMEOUT_SECONDS = 1800.0
_PULL_RETRIES = 3
_UP_TIMEOUT_SECONDS = 900.0
_DOWN_TIMEOUT_SECONDS = 240.0
_SERVICE_HEALTHY_TIMEOUT_SECONDS = 120.0
_CHAT_TIMEOUT_SECONDS = 900.0

_DEFAULT_RESULTS_DIR = _REPO_ROOT / "tests" / "e2e" / "clients" / ".small-model-results"
_RESULTS_DIR = Path(os.environ.get("MM_SMALL_MODEL_RESULTS_DIR", str(_DEFAULT_RESULTS_DIR)))


# --- subprocess helpers (podman/make, never a shell; own copy, not shared with
# test_openwebui.py on purpose - see module docstring) -----------------------------


def _run(
    cmd: Sequence[str], *, cwd: Path | None = None, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable (resolved via shutil.which), no shell
        list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _container(service: str) -> str:
    """Compose's own container-naming scheme (`scripts/openwebui-up.sh`'s own
    `container_name()`): `"<project>_<service>_1"`."""
    return f"{_PROJECT}_{service}_1"


def _refuse_if_stack_already_running() -> None:
    """Never tear down or reuse someone else's stack (fixed ports 3000/18081):
    a non-empty answer here means a `mm-openwebui` project is already up, from this
    worktree or another one, and this fixture must not touch it at all."""
    result = _run(
        [
            _PODMAN,
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={_PROJECT}",
            "--format",
            "{{.Names}}",
        ]
    )
    names = sorted(line for line in result.stdout.splitlines() if line.strip())
    if names:
        pytest.fail(
            f"project {_PROJECT!r} already has containers running ({names}) - refusing to "
            "start a second instance; stop it first (this test is the only user of the "
            "mm-openwebui stack)",
            pytrace=False,
        )


def _compose_file_args() -> list[str]:
    return ["-f", str(_COMPOSE_FILE), "-f", str(_OVERLAY)]


def _long_running_healthy(service: str) -> bool:
    result = _run([_PODMAN, "inspect", _container(service), "--format", "{{.State.Health.Status}}"])
    return result.returncode == 0 and result.stdout.strip() == "healthy"


def _wait_for_healthy(service: str, *, timeout: float) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if _long_running_healthy(service):
            return
        time.sleep(2)
    raise RuntimeError(f"{service!r} did not become healthy within {timeout}s")


def _bring_up_ollama() -> None:
    """Starts only the `ollama` compose-profile service (`.env.example`'s own
    comment on `ENABLE_OLLAMA_API`: "start that service explicitly first") - ahead
    of `_bring_up_stack` so the (possibly multi-minute) model pull below never
    counts against that make target's own startup timeout."""
    result = _run(
        [
            _PODMAN,
            "compose",
            *_compose_file_args(),
            "-p",
            _PROJECT,
            "--profile",
            "ollama",
            "up",
            "-d",
            "ollama",
        ],
        cwd=_REPO_ROOT,
        timeout=_UP_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"starting 'ollama' failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    _wait_for_healthy("ollama", timeout=_SERVICE_HEALTHY_TIMEOUT_SECONDS)


def _pull_model() -> str:
    """`ollama pull _MODEL_TAG` inside the running `ollama` container, with a few
    retries (a multi-GB pull over a real network flakes sometimes), then reads the
    pulled model's own short id back via `ollama list` - returns that id for the
    caller to record (`_verify_model_digest` below)."""
    container = _container("ollama")
    last_error = ""
    for attempt in range(1, _PULL_RETRIES + 1):
        result = _run(
            [_PODMAN, "exec", container, "ollama", "pull", _MODEL_TAG],
            timeout=_PULL_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            break
        last_error = f"exit {result.returncode}: {result.stdout}\n{result.stderr}"
        print(f"ollama pull attempt {attempt}/{_PULL_RETRIES} failed: {last_error}", flush=True)
    else:
        raise RuntimeError(
            f"'ollama pull {_MODEL_TAG}' failed after {_PULL_RETRIES} attempts: {last_error}"
        )

    list_result = _run([_PODMAN, "exec", container, "ollama", "list"])
    if list_result.returncode != 0:
        raise RuntimeError(f"'ollama list' failed: {list_result.stderr}")
    pulled_id = ""
    for line in list_result.stdout.splitlines()[1:]:
        fields = line.split()
        if fields and fields[0] == _MODEL_TAG:
            pulled_id = fields[1] if len(fields) > 1 else ""
            break
    if not pulled_id:
        raise RuntimeError(f"'ollama list' never reported {_MODEL_TAG!r}:\n{list_result.stdout}")
    return pulled_id


def _verify_model_digest(pulled_id: str) -> str:
    """Checks `pulled_id` (`ollama list`'s own short id column) against
    `_MODEL_DIGEST` (the full digest this benchmark's model tag was retrieved
    against, 2026-10-11) - a prefix match, since `ollama list` only ever shows a
    short, truncated form of the full manifest digest. Returns a one-line record
    of the comparison for the results file; never raises on a mismatch by itself
    (a changed upstream tag is evidence to report, not a reason to abort a run that
    otherwise completed) - `test_filter_on_vs_off_recall` decides whether that is
    fatal."""
    matches = _MODEL_DIGEST.lower().startswith(pulled_id.lower())
    return (
        f"pulled id {pulled_id!r} {'matches' if matches else 'does NOT match'} the pinned "
        f"digest sha256:{_MODEL_DIGEST} (prefix check)"
    )


def _bring_up_stack() -> None:
    result = _run(
        [_MAKE, "openwebui-up", f"OPENWEBUI_EXTRA_COMPOSE_FILES={_OVERLAY_RELATIVE}"],
        cwd=_REPO_ROOT,
        timeout=_UP_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"'make openwebui-up OPENWEBUI_EXTRA_COMPOSE_FILES={_OVERLAY_RELATIVE}' failed "
            f"(exit {result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _tear_down_stack() -> None:
    result = _run(
        [_MAKE, "openwebui-down", f"OPENWEBUI_EXTRA_COMPOSE_FILES={_OVERLAY_RELATIVE}"],
        cwd=_REPO_ROOT,
        timeout=_DOWN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"'make openwebui-down OPENWEBUI_EXTRA_COMPOSE_FILES={_OVERLAY_RELATIVE}' failed "
            f"(exit {result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _create_personal_token(name: str, *, oid: str) -> str:
    """`memory-manager token create` inside the running `memory-manager` container
    - a read-only personal token for `UserValves.token` (the filter's `inlet` only
    ever reads); `--owner`/`--role`, no `--namespace` (the overlay's own
    `LOGIN_NAMESPACES=*` is what makes that resolve to every namespace instead of
    the base file's single `demo`). The plaintext is the command's own first stdout
    line (CLAUDE.md "token hashes only" - never logged or asserted on beyond this)."""
    result = _run(
        [
            _PODMAN,
            "exec",
            _container("memory-manager"),
            "memory-manager",
            "token",
            "create",
            name,
            "--kind",
            "personal",
            "--owner",
            oid,
            "--role",
            "Memory.User",
            "--expires-days",
            "1",
            "--scope",
            "memory:read",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"token create failed (exit {result.returncode}): {result.stderr}")
    lines = result.stdout.splitlines()
    assert lines and lines[0], f"token create printed no plaintext line: {result.stdout!r}"
    return lines[0]


async def _mcp_call_tool(
    client: httpx.AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One raw `tools/call` against the live stack's own MCP endpoint
    (`test_openwebui.py`'s own `_mcp_call_tool` pattern) - used only for the
    per-question diagnostic `memory_search` call here, never through Open WebUI's
    own native tool loop."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token}"}
    response = await client.post(_MCP_URL, json=body, headers=headers)
    assert response.status_code == httpx.codes.OK, response.text
    payload = response.json()
    assert "error" not in payload, payload
    result = payload["result"]
    assert isinstance(result, dict) and not result.get("isError"), result
    return result


# --- question set, scoring (case-insensitive, umlaut-folded substring any-of) ----

_UMLAUT_MAP = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}


def _fold(text: str) -> str:
    """Umlaut-folded, casefolded `text`, decomposed first (`unicodedata.normalize`)
    so a precomposed (`ü`) and a decomposed (`u` + combining diaeresis) spelling of
    the same character both fold the same way, before the literal digraph
    substitution below."""
    decomposed = unicodedata.normalize("NFC", text)
    folded = decomposed
    for umlaut, replacement in _UMLAUT_MAP.items():
        folded = folded.replace(umlaut, replacement)
    return folded.casefold()


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    lang: str
    expected_any: tuple[str, ...]
    source_path: str


def _load_questions() -> list[Question]:
    raw = yaml.safe_load(_QUESTIONS_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and raw, f"{_QUESTIONS_PATH} is empty or not a list"
    questions = [
        Question(
            id=str(entry["id"]),
            question=str(entry["question"]),
            lang=str(entry["lang"]),
            expected_any=tuple(str(variant) for variant in entry["expected_any"]),
            source_path=str(entry["source_path"]),
        )
        for entry in raw
    ]
    assert len(questions) >= 20, f"expected at least 20 questions, got {len(questions)}"
    return questions


def _validate_questions(questions: list[Question]) -> None:
    """Checked once, before any of this runs (module docstring): no
    `expected_any` variant may be a substring of its own question - a model could
    otherwise "pass" by merely echoing the question back."""
    for question in questions:
        folded_question = _fold(question.question)
        for variant in question.expected_any:
            assert _fold(variant) not in folded_question, (
                f"{question.id}: expected variant {variant!r} is a substring of its own "
                f"question {question.question!r}"
            )


def _fact_present(answer: str, expected_any: Sequence[str]) -> bool:
    folded_answer = _fold(answer)
    return any(_fold(variant) in folded_answer for variant in expected_any)


# --- Ollama usage/timing, read back from Open WebUI's own chat-detail JSON -------


def _ollama_usage(usage: dict[str, object] | None) -> dict[str, float] | None:
    """Tokens/sec derived from Ollama's own `eval_count`/`eval_duration` (ns),
    whichever of `usage`'s own shape (Open WebUI's `usage` or `info` field,
    passed through close to verbatim for the `ollama` backend) carries them -
    `None` if neither is present (#151's own "if available", never required)."""
    if not usage:
        return None
    eval_count = usage.get("eval_count")
    eval_duration = usage.get("eval_duration")
    if not isinstance(eval_count, (int, float)) or not isinstance(eval_duration, (int, float)):
        return None
    if eval_duration <= 0:
        return None
    return {
        "eval_count": float(eval_count),
        "eval_duration_s": float(eval_duration) / 1e9,
        "tokens_per_second": float(eval_count) / (float(eval_duration) / 1e9),
    }


# --- per-question result, report writing -----------------------------------------


@dataclass
class QuestionResult:
    question: Question
    fact_in_context: bool
    on_answer: str
    on_hit: bool
    on_latency_s: float
    on_usage: dict[str, float] | None
    off_answer: str
    off_hit: bool
    off_latency_s: float
    off_usage: dict[str, float] | None


@dataclass
class RunReport:
    model_tag: str = _MODEL_TAG
    model_digest: str = _MODEL_DIGEST
    digest_check: str = ""
    on_rate: float = 0.0
    off_rate: float = 0.0
    results: list[QuestionResult] = field(default_factory=list)


def _write_report(report: RunReport) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())

    payload = {
        "model_tag": report.model_tag,
        "model_digest": report.model_digest,
        "digest_check": report.digest_check,
        "on_rate": report.on_rate,
        "off_rate": report.off_rate,
        "questions": [
            {
                "id": result.question.id,
                "lang": result.question.lang,
                "source_path": result.question.source_path,
                "question": result.question.question,
                "expected_any": list(result.question.expected_any),
                "fact_in_context": result.fact_in_context,
                "on": {
                    "answer": result.on_answer,
                    "hit": result.on_hit,
                    "latency_s": result.on_latency_s,
                    "usage": result.on_usage,
                },
                "off": {
                    "answer": result.off_answer,
                    "hit": result.off_hit,
                    "latency_s": result.off_latency_s,
                    "usage": result.off_usage,
                },
            }
            for result in report.results
        ],
    }
    (_RESULTS_DIR / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    on_rates = [1.0 if result.on_hit else 0.0 for result in report.results]
    off_rates = [1.0 if result.off_hit else 0.0 for result in report.results]
    on_rates_tps = [
        result.on_usage["tokens_per_second"] for result in report.results if result.on_usage
    ]
    lines = [
        f"# Local run {timestamp}",
        "",
        f"Model: `{report.model_tag}` (sha256:{report.model_digest}) - {report.digest_check}",
        "",
        f"- on rate: {statistics.fmean(on_rates):.3f} ({sum(on_rates):.0f}/{len(on_rates)})",
        f"- off rate: {statistics.fmean(off_rates):.3f} ({sum(off_rates):.0f}/{len(off_rates)})",
    ]
    if on_rates_tps:
        lines.append(f"- on mean tokens/s: {statistics.fmean(on_rates_tps):.2f}")
    lines += [
        "",
        "| id | lang | fact in context | on hit | off hit | on latency (s) | off latency (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in report.results:
        lines.append(
            f"| {result.question.id} | {result.question.lang} | "
            f"{'yes' if result.fact_in_context else 'no'} | "
            f"{'yes' if result.on_hit else 'no'} | {'yes' if result.off_hit else 'no'} | "
            f"{result.on_latency_s:.1f} | {result.off_latency_s:.1f} |"
        )
    (_RESULTS_DIR / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- the test itself ---------------------------------------------------------------


@pytest.fixture(scope="module")
def small_model_stack() -> Any:
    _refuse_if_stack_already_running()
    _bring_up_ollama()
    pulled_id = _pull_model()
    digest_check = _verify_model_digest(pulled_id)
    print(digest_check, flush=True)
    try:
        _bring_up_stack()
        yield digest_check
    finally:
        _tear_down_stack()


async def test_filter_on_vs_off_recall(small_model_stack: str) -> None:
    digest_check = small_model_stack
    questions = _load_questions()
    _validate_questions(questions)

    admin_email, admin_password = "admin@example.test", "small-model-benchmark-pw-01!"

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as admin_client:
        admin_driver = OpenWebUiDriver(admin_client)
        await admin_driver.signup_admin(name="admin", email=admin_email, password=admin_password)

        model_ids = await admin_driver.list_model_ids()
        assert _MODEL_TAG in model_ids, (
            f"{_MODEL_TAG!r} is not among Open WebUI's own /api/models "
            f"({model_ids!r}) - the ollama compose profile or ENABLE_OLLAMA_API "
            "did not wire up correctly"
        )

        await admin_driver.install_filter(
            function_id=_FILTER_FUNCTION_ID,
            name="memory-manager filter",
            content=_FILTER_PATH.read_text(encoding="utf-8"),
            description="memory-manager inlet/outlet filter (#151 small-model benchmark)",
        )
        await admin_driver.activate_filter(function_id=_FILTER_FUNCTION_ID)
        await admin_driver.update_filter_valves(
            function_id=_FILTER_FUNCTION_ID,
            valves={
                "server_url": _MCP_URL,
                "max_notes": _MAX_NOTES,
                "token_budget": _TOKEN_BUDGET,
                "outlet_enabled": False,
            },
        )
        await admin_driver.disable_builtin_memories()

        token = _create_personal_token("small-model-benchmark", oid=_OWNER_OID)
        await admin_driver.update_filter_user_valves(
            function_id=_FILTER_FUNCTION_ID, valves={"token": token}
        )

        results: list[QuestionResult] = []

        async with httpx.AsyncClient(timeout=30.0) as mcp_client:
            fact_in_context: dict[str, bool] = {}
            for question in questions:
                search_result = await _mcp_call_tool(
                    mcp_client,
                    token,
                    "memory_search",
                    {"query": question.question, "limit": _MAX_NOTES},
                )
                hits = search_result.get("structuredContent", {}).get("results", [])
                combined = " ".join(
                    str(hit.get(field, ""))
                    for hit in hits
                    for field in ("title", "description", "snippet")
                )
                fact_in_context[question.id] = _fact_present(combined, question.expected_any)
                print(f"{question.id}: fact_in_context={fact_in_context[question.id]}", flush=True)

        # --- filter on: every question, once each -----------------------------------
        on_answers: dict[str, tuple[str, float, dict[str, float] | None]] = {}
        for question in questions:
            start = time.monotonic()
            chat_result = await admin_driver.run_text_chat(
                model_id=_MODEL_TAG,
                message=question.question,
                temperature=0.0,
                seed=42,
                max_tokens=_MAX_ANSWER_TOKENS,
                timeout=_CHAT_TIMEOUT_SECONDS,
            )
            latency = time.monotonic() - start
            on_answers[question.id] = (
                chat_result.content,
                latency,
                _ollama_usage(chat_result.usage),
            )
            print(f"{question.id}: on done in {latency:.1f}s", flush=True)

        # --- filter off: admin-level toggle, every question again -------------------
        await admin_driver.update_filter_valves(
            function_id=_FILTER_FUNCTION_ID,
            valves={
                "server_url": _MCP_URL,
                "max_notes": _MAX_NOTES,
                "token_budget": _TOKEN_BUDGET,
                "outlet_enabled": False,
                "enabled": False,
            },
        )
        off_answers: dict[str, tuple[str, float, dict[str, float] | None]] = {}
        for question in questions:
            start = time.monotonic()
            chat_result = await admin_driver.run_text_chat(
                model_id=_MODEL_TAG,
                message=question.question,
                temperature=0.0,
                seed=42,
                max_tokens=_MAX_ANSWER_TOKENS,
                timeout=_CHAT_TIMEOUT_SECONDS,
            )
            latency = time.monotonic() - start
            off_answers[question.id] = (
                chat_result.content,
                latency,
                _ollama_usage(chat_result.usage),
            )
            print(f"{question.id}: off done in {latency:.1f}s", flush=True)

        for question in questions:
            on_answer, on_latency, on_usage = on_answers[question.id]
            off_answer, off_latency, off_usage = off_answers[question.id]
            results.append(
                QuestionResult(
                    question=question,
                    fact_in_context=fact_in_context[question.id],
                    on_answer=on_answer,
                    on_hit=_fact_present(on_answer, question.expected_any),
                    on_latency_s=on_latency,
                    on_usage=on_usage,
                    off_answer=off_answer,
                    off_hit=_fact_present(off_answer, question.expected_any),
                    off_latency_s=off_latency,
                    off_usage=off_usage,
                )
            )

    on_rate = statistics.fmean(1.0 if result.on_hit else 0.0 for result in results)
    off_rate = statistics.fmean(1.0 if result.off_hit else 0.0 for result in results)
    report = RunReport(
        digest_check=digest_check, on_rate=on_rate, off_rate=off_rate, results=results
    )
    _write_report(report)

    on_hits = sum(1 for result in results if result.on_hit)
    off_hits = sum(1 for result in results if result.off_hit)
    print(
        f"small-model benchmark summary: model={report.model_tag} "
        f"digest=sha256:{report.model_digest} n={len(results)} "
        f"on_hits={on_hits} on_rate={on_rate:.3f} "
        f"off_hits={off_hits} off_rate={off_rate:.3f}",
        flush=True,
    )

    # Owner decision 2026-10-11: report honestly, never tune after a measurement -
    # a red result here (below threshold, or not better than off) is the
    # measurement #151 asks for, not a bug to work around.
    assert on_rate >= _ON_THRESHOLD, (
        f"on_rate {on_rate:.3f} is below the {_ON_THRESHOLD} threshold - see "
        f"{_RESULTS_DIR / 'results.md'}"
    )
    assert on_rate > off_rate, (
        f"on_rate {on_rate:.3f} is not better than off_rate {off_rate:.3f} - see "
        f"{_RESULTS_DIR / 'results.md'}"
    )

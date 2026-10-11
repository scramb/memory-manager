# SPDX-License-Identifier: AGPL-3.0-only
"""#144's own acceptance test (WP-42, F-02, M13): per-user identity through Open WebUI
survives two real backends, not just the single-admin-user spike #314 already proved.

Opt-in only (`MM_E2E_OPENWEBUI=1`, pattern `MM_REQUIRE_HELM` in `tests/chart/conftest.py` -
grep that name): this drives two real, from-scratch `make openwebui-up` stacks (container
builds, a real Open WebUI, a mock IdP) end to end, which is far too slow and far too
environment-dependent for the default `make test` run. `#156` is tracked separately to make
this required in CI.

Everything this module needs to drive two users through two backends already exists from
`#313`/`#314` - see `docs/research/clients/openwebui.md`'s "Spike results" sections "#313:
two real subjects per backend, via the mock IdP" and "#314: a scripted native MCP tool call,
deterministic and read back" for the live evidence behind every choice below:

- `openwebui_stack` (module-scoped, parametrized `git`/`postgres`) brings up
  `deploy/openwebui/docker-compose.yml` plus `oidc-git-overlay.yml`/`postgres-entra-overlay.yml`
  (`make openwebui-up OPENWEBUI_EXTRA_COMPOSE_FILES=...`), registers the mock IdP's OIDC/Entra
  client and two users (`A`/`B`, role `Memory.User`) under that overlay's own tenant, signs an
  Open WebUI admin up, runs DCR against memory-manager's embedded AS once
  (`register_oauth_client`/`configure_tool_server`, already carrying the public-read
  `access_grants` #314 found load-bearing on the *first* call), and adds `A`/`B` as Open
  WebUI's own local accounts - then tears the whole stack down with `down -v` in `finally`,
  whatever the test did.
- `test_isolation` then, per user, on that user's *own* `httpx.AsyncClient` (own cookie jar -
  `OpenWebUiDriver`'s own docstring: a real browser would never share one across accounts):
  signs in, points the mock IdP's single `pending_signin` pointer at that user's `oid`
  (`tests/mock_idp/app.py`'s own `_control_select_signin` - a *global*, per-tenant pointer,
  so this must happen right before that user's own consent, never both at once), and runs the
  per-user MCP OAuth consent (`OpenWebUiDriver.run_oauth_flow`) - unmodified from #314, since
  `_complete_login_page`'s own interstitial hook already drives straight through
  memory-manager's `/login` to the mock IdP's `/authorize` and back without ever needing a
  password (confirmed live by #313, see the research note's own section for why).
- `A` then runs one scripted chat turn that writes a fact note and immediately searches for
  it (the search is this test's own positive control: an empty search result from `B` later
  is only meaningful once something proves the index is not simply still catching up - see
  `docs/research/clients/openwebui.md` "Not included" bullets, "worker/embeddings in postgres
  mode" aside, `queue.WriteQueue._run_write_job`'s own docstring already orders the index hook
  *before* the write's own future resolves, so no polling is needed here either way).
- The write's own real, stored path (never `me` - `mcp/namespaces.py`'s own docstring: "a
  client must always say `me`, never the namespace id-derived alias") is read back from
  `audit_log` (`path` there is `result.path`, the stored path, `memory_manager.app.
  _audit_write_hook`) together with `actor`/`client`/`detail->>'profile'` - the one query
  this module needs to check all four `Fertig wenn` claims about that row at once, and to
  hand `B`'s own next turn a *real* path to probe instead of a made-up one.
- `B` then runs one scripted chat turn that searches for the same fact (expecting no hit) and
  attempts `memory_read` on `A`'s real stored path (expecting a per-item `"error"`, never an
  exception - `mcp/server.py`'s own `_read_items` never raises for an unreadable/rejected
  item, it returns `{"item": ..., "error": {...}}` instead, which is the one shape this test
  checks for, not any specific error name - the git backend answers `NotFound` via the
  readable-namespace check, the postgres backend answers `PathRejected` via `Resolution.
  to_stored`'s unconditional `u-*` rejection, see `namespaces.py`'s own docstring for why
  those two differ and `mcp/errors.py.error_to_dict` for the common `{"error": ..., ...}`
  shape both end up in).

Never touches `mm-pg`/`mm-valkey`/`cockpit_db` (CLAUDE.md's "verify, don't recall" aside: this
stack's own `postgres` container is a disposable, project-scoped one, distinct from the
developer-machine fixtures those names refer to) - every `podman`/`psql`/`git` call below
addresses only `mm-openwebui`'s own containers, found by this project's compose-assigned
names (`scripts/openwebui-up.sh`'s own `container_name()`: `"<project>_<service>_1"`).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e.openwebui_driver import OpenWebUiDriver, OpenWebUiDriverError
from integrations.openwebui.filter_memory import SHORT
from memory_manager.auth.login_oidc import CALLBACK_PATH
from memory_manager.config import canonical_resource_url

pytestmark = pytest.mark.skipif(
    os.environ.get("MM_E2E_OPENWEBUI") != "1",
    reason="opt-in only (MM_E2E_OPENWEBUI=1) - drives two real, from-scratch Open WebUI "
    "stacks; see this module's own docstring",
)

__all__: list[str] = []

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROJECT = "mm-openwebui"
_SERVER_ID = "memmgr"
_MODEL_ID = "scripted-model"
_MOCK_IDP_URL = "http://localhost:18082"
_RESOURCE_SERVER_URL = "http://localhost:18081"
_MCP_URL = f"{_RESOURCE_SERVER_URL}/mcp"
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
# Both overlays put memory-manager's own `PUBLIC_URL` at 18081 (docker-compose.yml's own
# "Networking" comment) - the same `canonical_resource_url`/`CALLBACK_PATH` memory-manager's
# own `OidcAuthenticator.from_env`/`EntraAuthenticator.from_env` build their `redirect_uri`
# from (`login_oidc.py`/`login_entra.py` module docstrings: "both modes mount the same
# callback path").
_REDIRECT_URI = canonical_resource_url("http://localhost:18081", "") + CALLBACK_PATH
_MEMORY_USER_ROLE = "Memory.User"

_SCRIPT_PATH = _REPO_ROOT / "tests" / "e2e" / "stubs" / "live-script.json"

_UP_TIMEOUT_SECONDS = 900.0
_DOWN_TIMEOUT_SECONDS = 180.0

# --- #150: the filter installed as an Open WebUI Function, and the personal tokens its
# per-user UserValves need --------------------------------------------------------------

_FILTER_FUNCTION_ID = "memmgr_filter"
_FILTER_PATH = _REPO_ROOT / "integrations" / "openwebui" / "filter_memory.py"
#: Tuned live against the pinned stack (#150, docs/research/clients/openwebui.md): large
#: enough that the one relevant note's own entry always fits, small enough that not every
#: filler note fits alongside it - see `test_filter_injects_relevant_notes_within_budget`.
_FILTER_TOKEN_BUDGET = 300
_TOKEN_SCOPES = ("memory:read", "memory:write")

_PODMAN = shutil.which("podman") or "podman"
_MAKE = shutil.which("make") or "make"


@dataclass(frozen=True)
class _BackendConfig:
    """Everything that differs between the `git`/`postgres` overlays (#313's own
    `.env-oidc-entra`, generated once by `scripts/openwebui-up.sh` and read back below)."""

    name: str
    overlay: str
    tid: str
    client_id: str
    credential_env_var: str
    oid_a: str
    oid_b: str
    #: The `git` backend's `LOGIN_NAMESPACE_MAP` namespace per user - `None` on `postgres`,
    #: which has no such map at all (every subject gets `["*"]`, ADR-0008's own matrix
    #: decides access instead, `login_entra.EntraAuthenticator.handle_callback`).
    namespace_a: str | None
    namespace_b: str | None


_BACKENDS: dict[str, _BackendConfig] = {
    "git": _BackendConfig(
        name="git",
        overlay="deploy/openwebui/oidc-git-overlay.yml",
        tid="openwebui-git",
        client_id="openwebui-git-client",
        credential_env_var="OIDC_CLIENT_SECRET",
        oid_a="openwebui-git-user-a",
        oid_b="openwebui-git-user-b",
        namespace_a="demo-a",
        namespace_b="demo-b",
    ),
    "postgres": _BackendConfig(
        name="postgres",
        overlay="deploy/openwebui/postgres-entra-overlay.yml",
        tid="openwebui-pg",
        client_id="openwebui-pg-client",
        credential_env_var="ENTRA_CLIENT_SECRET",
        oid_a="openwebui-pg-user-a",
        oid_b="openwebui-pg-user-b",
        namespace_a=None,
        namespace_b=None,
    ),
}


@dataclass(frozen=True)
class _StackContext:
    """What `openwebui_stack` hands the test once both backends' own one-time setup
    (mock IdP client/users, Open WebUI admin + DCR + two local accounts) is done."""

    config: _BackendConfig
    admin_email: str
    admin_password: str
    user_a_email: str
    user_a_password: str
    user_b_email: str
    user_b_password: str


# --- subprocess helpers (podman/make, never a shell) ------------------------------------


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
    """Never tear down someone else's stack (fixed ports 3000/18081/18082): a non-empty
    answer here means a `mm-openwebui` project is already up, from this worktree or another
    one, and this fixture must not touch it at all - not even to inspect it further."""
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


def _bring_up(overlay: str) -> None:
    result = _run(
        [_MAKE, "openwebui-up", f"OPENWEBUI_EXTRA_COMPOSE_FILES={overlay}"],
        cwd=_REPO_ROOT,
        timeout=_UP_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"'make openwebui-up OPENWEBUI_EXTRA_COMPOSE_FILES={overlay}' failed "
            f"(exit {result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _tear_down(overlay: str) -> None:
    result = _run(
        [_MAKE, "openwebui-down", f"OPENWEBUI_EXTRA_COMPOSE_FILES={overlay}"],
        cwd=_REPO_ROOT,
        timeout=_DOWN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"'make openwebui-down OPENWEBUI_EXTRA_COMPOSE_FILES={overlay}' failed "
            f"(exit {result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _psql(query: str) -> str:
    """One `psql -tA` (unaligned, no header) query against this project's own `postgres`
    container, as the superuser `mm` - bypasses `FORCE RLS` on purpose (`Fertig wenn`'s own
    "psql -U mm (superuser bypasses FORCE RLS)")."""
    result = _run(
        [
            _PODMAN,
            "exec",
            _container("postgres"),
            "psql",
            "-U",
            "mm",
            "-d",
            "mm",
            "-tA",
            "-c",
            query,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"psql query failed (exit {result.returncode}): {result.stderr}\nquery: {query}"
        )
    return result.stdout.strip()


def _git_ls_tree() -> list[str]:
    """Every path the bare "remote" (`/data/remote.git`, `main`) carries, from inside the
    `memory-manager` container - the one long-running service that mounts it."""
    result = _run(
        [
            _PODMAN,
            "exec",
            _container("memory-manager"),
            "git",
            "-C",
            "/data/remote.git",
            "ls-tree",
            "-r",
            "main",
            "--name-only",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ls-tree failed (exit {result.returncode}): {result.stderr}")
    return result.stdout.splitlines()


def _read_env_file(path: Path) -> dict[str, str]:
    """A plain `KEY=value` file (`scripts/openwebui-up.sh`'s own `.env-oidc-entra`) - no
    quoting, no interpolation, same shape that script itself writes."""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key] = value
    return result


# --- mock IdP control API (plain HTTP, tests/mock_idp/app.py's own `/_mock/` routes) -----


async def _register_mock_client(
    client: httpx.AsyncClient, config: _BackendConfig, *, secret: str
) -> None:
    response = await client.post(
        f"{_MOCK_IDP_URL}/_mock/clients",
        json={
            "tid": config.tid,
            "client_id": config.client_id,
            "client_secret": secret,
            "redirect_uris": [_REDIRECT_URI],
        },
    )
    if response.status_code != httpx.codes.CREATED:
        raise RuntimeError(
            f"mock IdP client registration failed: {response.status_code} {response.text}"
        )


async def _create_mock_user(client: httpx.AsyncClient, config: _BackendConfig, *, oid: str) -> None:
    response = await client.post(
        f"{_MOCK_IDP_URL}/_mock/users",
        json={"tid": config.tid, "oid": oid, "roles": [_MEMORY_USER_ROLE], "groups": []},
    )
    if response.status_code != httpx.codes.CREATED:
        raise RuntimeError(
            f"mock IdP user creation failed for {oid!r}: {response.status_code} {response.text}"
        )


async def _select_signin(config: _BackendConfig, *, oid: str) -> None:
    """Points the mock IdP's *single*, per-tenant `pending_signin` pointer at `oid` - must
    run right before that user's own `run_oauth_flow`, never both users' at once
    (`tests/mock_idp/app.py`'s own `_control_select_signin`: one pointer per tenant, not a
    queue)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_MOCK_IDP_URL}/_mock/select-signin", json={"tid": config.tid, "oid": oid}
        )
    if response.status_code != httpx.codes.OK:
        raise RuntimeError(
            f"mock IdP select-signin failed for {oid!r}: {response.status_code} {response.text}"
        )


async def _consent(
    driver: OpenWebUiDriver, config: _BackendConfig, *, oid: str, email: str, password: str
) -> None:
    """The per-user consent block `test_isolation` and `test_filter_injects_relevant_notes_
    within_budget` below both need (module docstring's own bullet on `run_oauth_flow`):
    points the mock IdP's single pending-signin pointer at `oid`, signs this driver's own
    session in, and runs the per-user MCP OAuth consent - unmodified from #314/#144, since
    `_complete_login_page`'s own interstitial hook already drives straight through without
    ever needing a password at memory-manager's own `/login`."""
    await _select_signin(config, oid=oid)
    await driver.sign_in(email=email, password=password)
    consent = await driver.run_oauth_flow(server_id=_SERVER_ID, password=password)
    if not consent.authorize_followed_to_webui:
        raise OpenWebUiDriverError(
            f"OAuth consent for {oid!r} did not return to Open WebUI: {consent}"
        )


def _create_personal_token(name: str, *, oid: str) -> str:
    """`memory-manager token create` inside the running `memory-manager` container (#150) -
    a personal token for `UserValves.token`, scoped `memory:read`+`memory:write` (the second
    scope is only this test's own need to seed/read notes directly over MCP, never the
    filter's - its `inlet` only ever reads). The plaintext is the command's own first stdout
    line (`cli.py`'s `_run_token_create`: `print(plaintext)` to stdout, the human-readable
    summary - never logged or asserted on, CLAUDE.md "token hashes only" - to stderr)."""
    args = [
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
        _MEMORY_USER_ROLE,
        "--expires-days",
        "1",
    ]
    for scope in _TOKEN_SCOPES:
        args += ["--scope", scope]
    result = _run(args)
    if result.returncode != 0:
        raise RuntimeError(f"token create failed (exit {result.returncode}): {result.stderr}")
    lines = result.stdout.splitlines()
    assert lines and lines[0], f"token create printed no plaintext line: {result.stdout!r}"
    return lines[0]


async def _mcp_call_tool(
    client: httpx.AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One raw `tools/call` against the live stack's own MCP endpoint
    (`tests/e2e/test_replicas.py`'s own `_call_tool` pattern, :225-242) - used only to
    seed/inspect notes directly here, never through Open WebUI's own native tool loop (the
    filter test below never drives a tool call through Open WebUI at all, only its `inlet`'s
    own direct HTTP call to this same endpoint)."""
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


# --- one-time stack setup (admin signup, DCR, two local Open WebUI accounts) -------------


async def _provision(config: _BackendConfig) -> _StackContext:
    secret = _read_env_file(_REPO_ROOT / "deploy" / "openwebui" / ".env-oidc-entra")[
        config.credential_env_var
    ]

    async with httpx.AsyncClient(timeout=15.0) as mock_idp:
        await _register_mock_client(mock_idp, config, secret=secret)
        await _create_mock_user(mock_idp, config, oid=config.oid_a)
        await _create_mock_user(mock_idp, config, oid=config.oid_b)

    admin_email, admin_password = "admin@example.test", secrets.token_urlsafe(24)
    user_a_email, user_a_password = "user-a@example.test", secrets.token_urlsafe(24)
    user_b_email, user_b_password = "user-b@example.test", secrets.token_urlsafe(24)

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as admin_client:
        admin_driver = OpenWebUiDriver(admin_client)
        await admin_driver.signup_admin(name="admin", email=admin_email, password=admin_password)
        await admin_driver.register_oauth_client(server_id=_SERVER_ID)
        await admin_driver.configure_tool_server(server_id=_SERVER_ID)
        await admin_driver.add_user(name="user a", email=user_a_email, password=user_a_password)
        await admin_driver.add_user(name="user b", email=user_b_email, password=user_b_password)

    return _StackContext(
        config=config,
        admin_email=admin_email,
        admin_password=admin_password,
        user_a_email=user_a_email,
        user_a_password=user_a_password,
        user_b_email=user_b_email,
        user_b_password=user_b_password,
    )


@pytest.fixture(scope="module", params=["git", "postgres"])
def openwebui_stack(request: pytest.FixtureRequest) -> Iterator[_StackContext]:
    """Brings up `deploy/openwebui/docker-compose.yml` plus the backend's own overlay
    (`#313`), provisions the mock IdP client/users and an Open WebUI admin with two local
    accounts, and tears the whole project down with `down -v` in `finally` - whatever the
    test using it did. Parametrized so `test_isolation[git]`/`test_isolation[postgres]`
    each get their own, from-scratch stack, never both at once."""
    config = _BACKENDS[str(request.param)]
    _refuse_if_stack_already_running()
    _bring_up(config.overlay)
    try:
        ctx = asyncio.run(_provision(config))
        yield ctx
    finally:
        _tear_down(config.overlay)


# --- note content, the live script, and reading a tool call's own text back -------------


def _note_content(*, fact_token: str) -> str:
    return (
        "---\n"
        f"title: Isolation fixture {fact_token}\n"
        "description: WP-42 per-user isolation test fixture note.\n"
        "type: fact\n"
        f"tags: [{fact_token}]\n"
        "---\n"
        f"The fact token is {fact_token}.\n"
    )


def _note_path(namespace: str | None, slug: str) -> str:
    """The real, stored path for a note written as a user whose `git`-backend login
    namespace is `namespace` (`<namespace>/fact/<slug>.md`), or, on the `postgres` backend
    (`namespace is None` - it has no such map, ADR-0008's own `me`), the caller's own
    personal namespace via the `me` alias - `mcp/namespaces.py`'s own docstring: "a client
    must always say `me`, never the namespace id-derived alias"."""
    return f"{namespace}/fact/{slug}.md" if namespace is not None else f"me/fact/{slug}.md"


def _write_live_script(turns: list[dict[str, Any]]) -> None:
    """Writes `tests/e2e/stubs/live-script.json`, re-read by the stub on every chat
    completion request (`scripted_model.py`'s own module docstring, #314) - 0644/0755 so the
    container's own UID 10001 can read it through the read-only bind mount."""
    _SCRIPT_PATH.write_text(json.dumps(turns), encoding="utf-8")
    _SCRIPT_PATH.chmod(0o644)
    _SCRIPT_PATH.parent.chmod(0o755)


def _text_of(function_call_output: dict[str, Any]) -> str:
    """Every `input_text` part of one `function_call_output` item, joined - the JSON text
    a `memory_*` tool call actually returned (`openwebui_driver.ChatToolCallResult.
    output_text`'s own per-item twin)."""
    return "\n".join(
        str(part.get("text", ""))
        for part in function_call_output.get("output", [])
        if part.get("type") == "input_text"
    )


async def _run_single_tool_call(
    driver: OpenWebUiDriver, *, tool_name: str, arguments: dict[str, Any], message: str
) -> dict[str, Any]:
    """Runs one chat that scripts exactly one tool call, then a plain text reply, and
    returns that one `function_call_output` item.

    One tool call per chat, not several in a row (`scripted_model.py`'s own `_turn_index`
    counts `role == "assistant"` messages in the request - found live, by this task, to
    never reach 2 under Open WebUI 0.11.4's own Responses-API tool loop
    (`utils/middleware.py`'s `while tool_calls` loop keeps appending `"tool"` messages
    without ever appending a second `"assistant"` one), so a script with more than one
    tool-call turn gets stuck replaying turn 1 forever - `RATE_LIMIT_MCP_BURST` (30) is
    what eventually cut that off live, as a `500` from Open WebUI's own cancelled request,
    never a clean "Tool ... not found"/error shape. Splitting every scripted turn into its
    own chat sidesteps the stuck index entirely: each chat's own `messages` starts fresh at
    `["user"]`, `turn_index` reaches exactly 1 once (the one tool call), then 2 (this
    turn's own "Done." reply) - the same shape #314's own single-tool-call spike already
    proved works. Reported, not fixed here: `scripted_model.py` is outside this task's own
    Blast-Radius."""
    _write_live_script(
        [
            {"tool_calls": [{"name": tool_name, "arguments": arguments}]},
            {"content": "Done."},
        ]
    )
    result = await driver.run_scripted_tool_chat(
        model_id=_MODEL_ID, server_id=_SERVER_ID, message=message
    )
    assert len(result.function_call_outputs) >= 1, result
    return result.function_call_outputs[0]


# --- the test itself -----------------------------------------------------------------


async def test_isolation(openwebui_stack: _StackContext) -> None:
    config = openwebui_stack.config
    fact_token = f"mmisolation{secrets.token_hex(8)}"
    slug = f"isolation-{secrets.token_hex(6)}"
    write_path = _note_path(config.namespace_a, slug)

    try:
        # --- A: write a fact, then search for it (positive control) ---------------------
        a_client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            a_driver = OpenWebUiDriver(a_client)
            await _consent(
                a_driver,
                config,
                oid=config.oid_a,
                email=openwebui_stack.user_a_email,
                password=openwebui_stack.user_a_password,
            )

            write_output = await _run_single_tool_call(
                a_driver,
                tool_name=f"{_SERVER_ID}_memory_write",
                arguments={
                    "path": write_path,
                    "content": _note_content(fact_token=fact_token),
                    "if_version": "new",
                },
                message="Please remember this fact for me.",
            )
            search_output = await _run_single_tool_call(
                a_driver,
                tool_name=f"{_SERVER_ID}_memory_search",
                arguments={"query": fact_token},
                message="What do you remember about that fact?",
            )
        finally:
            await a_client.aclose()

        assert write_output.get("status") == "completed", write_output
        assert "error" not in _text_of(write_output).lower(), _text_of(write_output)
        search_text = _text_of(search_output)
        assert search_output.get("status") == "completed", search_output
        assert fact_token in search_text, search_text  # A's own positive control

        # --- verify the write independently of what Open WebUI/the tool call reported --
        row = _psql(
            "select path, actor, client, coalesce(detail->>'profile', '') "  # noqa: S608 - `slug` is test-generated, alnum/hyphen only
            "from audit_log where op = 'write' and path like "
            f"'%/fact/{slug}.md' order by at desc limit 1"
        )
        assert row, "no audit_log row found for A's write"
        stored_path, actor, client_label, profile = row.split("|", 3)
        assert actor == config.oid_a, row
        assert client_label == "openwebui", row
        assert profile == "default", row

        if config.namespace_a is not None:
            assert stored_path in _git_ls_tree()
        else:
            note_count = _psql(f"select count(*) from vault_notes where path = '{stored_path}'")  # noqa: S608 - stored_path came from our own audit_log row above, not caller input
            assert note_count == "1", note_count
            owner_namespace_alias = stored_path.split("/", 1)[0]
            owner_oid = _psql(
                "select external_key from namespaces where kind = 'user' and "  # noqa: S608 - alias came from our own audit_log row above
                f"alias = '{owner_namespace_alias}'"
            )
            assert owner_oid == config.oid_a, owner_oid

        # --- B: search finds nothing, reading A's real stored path errors or is empty ---
        b_client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            b_driver = OpenWebUiDriver(b_client)
            await _consent(
                b_driver,
                config,
                oid=config.oid_b,
                email=openwebui_stack.user_b_email,
                password=openwebui_stack.user_b_password,
            )

            b_search_output = await _run_single_tool_call(
                b_driver,
                tool_name=f"{_SERVER_ID}_memory_search",
                arguments={"query": fact_token},
                message="Search for that fact.",
            )
            b_read_output = await _run_single_tool_call(
                b_driver,
                tool_name=f"{_SERVER_ID}_memory_read",
                arguments={"items": [stored_path]},
                message="Read that exact note for me.",
            )
        finally:
            await b_client.aclose()

        b_search_text = _text_of(b_search_output)
        assert fact_token not in b_search_text, b_search_text

        b_read_text = _text_of(b_read_output).strip()
        if b_read_text:
            read_payload: Any = json.loads(b_read_text)
            # `memory_read`'s own `list[MemoryReadItem]` return, read back for one
            # requested item - a single-element result serializes as that one item's
            # own dict, not a one-element list (confirmed live by this task, both
            # backends), so accept either shape rather than assume one.
            read_items = [read_payload] if isinstance(read_payload, dict) else read_payload
            assert not read_items or all("error" in item for item in read_items), read_payload
    finally:
        _SCRIPT_PATH.unlink(missing_ok=True)


# --- #150: the filter, installed live, injects relevant notes within its budget --------


def _relevant_note_content(*, marker: str) -> str:
    """A short, tightly-focused note (#150: `ts_rank_cd`'s length normalisation rewards
    this over a longer filler note that merely shares one word) sharing three distinct
    words with `_coffee_question`'s own query text: "coffee", "order" and `marker` itself."""
    return (
        "---\n"
        f"title: Coffee order {marker}\n"
        "description: WP-44 filter fixture - the one note the question below should surface.\n"
        "type: fact\n"
        "---\n"
        f"The user's coffee order is a flat white with oat milk, marker {marker}.\n"
    )


def _filler_note_content(*, filler_marker: str) -> str:
    """A longer, unrelated note sharing exactly one word ("coffee") with `_coffee_question`'s
    query - padded well past the relevant note's own length, `ts_rank_cd`'s normalisation
    (#150) should rank it below the single, tight match."""
    return (
        "---\n"
        f"title: Filler note {filler_marker}\n"
        "description: WP-44 filter fixture filler note - shares only one word with the "
        "question below, never the user's actual order.\n"
        "type: fact\n"
        "---\n"
        "This note is unrelated filler, padded with extra words so it stays noticeably "
        f"longer than the relevant note. Coffee gets a single, passing mention here "
        f"(filler marker {filler_marker}), but nothing about the user's actual coffee "
        "order is ever recorded in this note - it exists only to pad out the vault with "
        "text that should not end up in the injected context block once the character "
        "budget for the block runs out.\n"
    )


def _coffee_question(*, marker: str) -> str:
    return f"What's my coffee order? (marker {marker})"


async def test_filter_injects_relevant_notes_within_budget(
    openwebui_stack: _StackContext,
) -> None:
    """#150's own acceptance test: the filter, installed live through Open WebUI's own
    Function API (ADR-0011), injects the one relevant note into the chat's system message,
    trims out at least one less-relevant filler note once the character budget runs out, and
    never injects anything of A's into B's own chat - even when B's own question literally
    repeats A's marker."""
    config = openwebui_stack.config

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as admin_client:
            admin_driver = OpenWebUiDriver(admin_client)
            await admin_driver.sign_in(
                email=openwebui_stack.admin_email, password=openwebui_stack.admin_password
            )
            await admin_driver.install_filter(
                function_id=_FILTER_FUNCTION_ID,
                name="memory-manager filter",
                content=_FILTER_PATH.read_text(encoding="utf-8"),
                description="memory-manager inlet/outlet filter (#150 live check)",
            )
            await admin_driver.activate_filter(function_id=_FILTER_FUNCTION_ID)
            write_namespace = config.namespace_a if config.namespace_a is not None else "me"
            await admin_driver.update_filter_valves(
                function_id=_FILTER_FUNCTION_ID,
                valves={
                    "server_url": _MCP_URL,
                    "token_budget": _FILTER_TOKEN_BUDGET,
                    # #150's own optional step: the outlet round trip below only ever
                    # triggers on A's explicit "remember ..." turn, never on B's plain
                    # questions, so turning it on globally here does not affect B at all.
                    "outlet_enabled": True,
                    "write_namespace": write_namespace,
                },
            )
            await admin_driver.disable_builtin_memories()

        # --- A: consent first (postgres needs a `users` row before a token can name this
        # owner), then seed one relevant note plus several fillers, then ask about it ----
        marker = f"mmfiltera{secrets.token_hex(6)}"
        filler_markers = [f"mmfillerb{index}{secrets.token_hex(4)}" for index in range(4)]

        a_client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            a_driver = OpenWebUiDriver(a_client)
            await _consent(
                a_driver,
                config,
                oid=config.oid_a,
                email=openwebui_stack.user_a_email,
                password=openwebui_stack.user_a_password,
            )
            token_a = _create_personal_token(f"filter-a-{secrets.token_hex(4)}", oid=config.oid_a)

            async with httpx.AsyncClient(timeout=15.0) as mcp_client:
                await _mcp_call_tool(
                    mcp_client,
                    token_a,
                    "memory_write",
                    {
                        "path": _note_path(config.namespace_a, f"coffee-{secrets.token_hex(6)}"),
                        "content": _relevant_note_content(marker=marker),
                        "if_version": "new",
                    },
                )
                for filler_marker in filler_markers:
                    await _mcp_call_tool(
                        mcp_client,
                        token_a,
                        "memory_write",
                        {
                            "path": _note_path(
                                config.namespace_a, f"filler-{secrets.token_hex(6)}"
                            ),
                            "content": _filler_note_content(filler_marker=filler_marker),
                            "if_version": "new",
                        },
                    )

            await a_driver.update_filter_user_valves(
                function_id=_FILTER_FUNCTION_ID, valves={"token": token_a}
            )

            _write_live_script([{"echo_system": True}])
            a_text = await a_driver.run_scripted_text_chat(
                model_id=_MODEL_ID, message=_coffee_question(marker=marker)
            )

            # --- #150's own optional step: one outlet round trip (a "remember ..."
            # turn, never a question - `_detect_trigger` never triggers on one) --------
            remember_marker = f"mmremember{secrets.token_hex(6)}"
            _write_live_script([{"content": "Okay."}])
            remember_text = await a_driver.run_scripted_text_chat(
                model_id=_MODEL_ID,
                message=f"remember that my lucky number is 42, marker {remember_marker}",
            )
        finally:
            await a_client.aclose()

        assert a_text.count("<memory-context>") == 1, a_text
        assert a_text.startswith("<memory-context>") and a_text.endswith("</memory-context>"), (
            a_text
        )
        assert len(a_text) <= _FILTER_TOKEN_BUDGET * 4, a_text
        assert SHORT.splitlines()[0] in a_text, a_text
        assert marker in a_text, a_text  # the relevant note surfaced
        assert any(filler_marker not in a_text for filler_marker in filler_markers), a_text

        assert "remembered it" in remember_text, remember_text
        async with httpx.AsyncClient(timeout=15.0) as mcp_client:
            search_result = await _mcp_call_tool(
                mcp_client, token_a, "memory_search", {"query": remember_marker}
            )
        remember_hits = search_result["structuredContent"]["results"]
        assert len(remember_hits) == 1, remember_hits
        assert remember_marker in remember_hits[0]["title"], remember_hits

        # --- B: consent first (same ordering reason as A above), a different note, a
        # question repeating A's own marker ----------------------------------------------
        b_marker = f"mmfilterb{secrets.token_hex(6)}"

        b_client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            b_driver = OpenWebUiDriver(b_client)
            await _consent(
                b_driver,
                config,
                oid=config.oid_b,
                email=openwebui_stack.user_b_email,
                password=openwebui_stack.user_b_password,
            )
            token_b = _create_personal_token(f"filter-b-{secrets.token_hex(4)}", oid=config.oid_b)

            async with httpx.AsyncClient(timeout=15.0) as mcp_client:
                await _mcp_call_tool(
                    mcp_client,
                    token_b,
                    "memory_write",
                    {
                        "path": _note_path(config.namespace_b, f"coffee-{secrets.token_hex(6)}"),
                        "content": _relevant_note_content(marker=b_marker),
                        "if_version": "new",
                    },
                )

            await b_driver.update_filter_user_valves(
                function_id=_FILTER_FUNCTION_ID, valves={"token": token_b}
            )

            _write_live_script([{"echo_system": True}])
            b_text = await b_driver.run_scripted_text_chat(
                model_id=_MODEL_ID,
                message=f"What's my coffee order? (markers {b_marker} and {marker})",
            )
        finally:
            await b_client.aclose()

        assert b_marker in b_text, b_text  # B's own positive control
        assert marker not in b_text, b_text  # never A's note, not even on a shared question
    finally:
        _SCRIPT_PATH.unlink(missing_ok=True)

# SPDX-License-Identifier: AGPL-3.0-only
"""M8's own acceptance test (#225, `docs/features/F-01-enterprise-scale.md`):
Entra sign-in, full-text-first search and deprovisioning, driven against real
`memory-manager serve --http`, `memory-manager worker` and mock-IdP
*processes* - the milestone closes once this is green in CI.

Building blocks, each already proven in isolation and only combined here:

- the mock IdP as a real subprocess (`mock_idp_server`, `tests/mock_idp_fixtures.py`)
  rather than its in-process ASGI counterpart (`mock_idp_client`) - the facade
  and the worker below are themselves real subprocesses, so they need a real
  base URL to dial, the same reason that fixture's own docstring gives.
- the DCR/PKCE/Entra-login choreography (`_pkce_pair`, `_register_mcp_client`,
  `_start_authorize`, `_confirm_interstitial`, `_drive_entra_login`,
  `_register_entra_client`, `_create_entra_user`, `_select_signin`) duplicated
  from `tests/auth/test_login_entra.py` rather than imported - that module
  exports none of its own private helpers, the same "duplicated instead of
  imported" reasoning that module's and `tests/auth/test_disable_user.py`'s own
  docstrings give for their own duplicated `entra_environ`.
- a disposable Postgres owner/app-role pair (`_acceptance_db`), the same shape
  `tests/e2e/test_replicas.py`'s own `_replica_db` builds (duplicated here
  rather than imported, for the identical reason: that fixture is private to
  its own module too) - the facade authenticates requests against it, the
  worker connects to it as the owner (ADR-0008 addendum: "the worker is a
  system identity").
- a real `memory-manager worker` subprocess (`_run_worker_server`, duplicated
  from `tests/worker/test_singleton.py`'s own private helper of the same
  name) running the `entra_delta_sync` job (#223) on a short
  `ENTRA_DELTA_SYNC_SECONDS` and the `embed_note` jobs-outbox consumer (#219)
  on a short `JOBS_POLL_SECONDS` - both bounded, polled-for intervals rather
  than long sleeps.
- a tiny, real OpenAI-compatible `POST /embeddings` stub (`_run_embedding_stub`),
  served in-process on a real port for the duration of one test (not a
  subprocess, not the separate `loadtest/embedding_stub.py` module that exists
  only on another branch) - `index/embeddings.py`'s `OpenAICompatibleProvider`
  opens its own `httpx.AsyncClient` with no transport seam, so the facade and
  worker subprocesses need a real network endpoint to call, the same reason
  the mock IdP itself is a real subprocess rather than an ASGI transport here.
  Always returns 1024-dimensional vectors: `chunks.embedding` is a fixed
  `halfvec(1024)` column in the ADR-0016 layout (`migrations/postgres/
  0012_vector_layout.sql`) regardless of `EMBEDDING_DIMENSIONS`, so any other
  width would fail `Indexer._apply_embeddings_vector_layout`'s pin check or
  the column's own typmod.

One test function walks the three scenarios #225's own Implementation
checklist lists, end to end, reusing the one facade/worker/mock-IdP/stub set
started for it (process startup, not the scenarios themselves, is what would
make three separate tests expensive here):

1. DCR + PKCE + Entra sign-in, a `memory_write` into `me/...`, read back.
2. Before any worker runs, `memory_search` already finds the note through
   full text while its chunk has no embedding yet (checked directly against
   `chunks.embedding`); once the worker is started, the pending `embed_note`
   job (enqueued by the write itself, `worker.py`'s own module docstring) is
   picked up and the embedding appears.
3. The mock disables the signed-in user; within one `entra_delta_sync` round
   the still-unexpired access token's next MCP call answers 401
   (`auth.users.disable_user` revokes it outright, `tests/auth/
   test_disable_user.py`), and refreshing it fails with `invalid_grant`
   (`tests/auth/test_oauth_flow.py`'s own replay-revocation assertion shape).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import os
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg
import httpx
import pytest_asyncio
import uvicorn
from http_fixtures import CLI_ARGS, Server, free_port, wait_until_ready
from mock_idp_fixtures import MockIdpServer, mock_idp_server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from memory_manager.auth.login import LOGIN_PATH
from memory_manager.auth.login_entra import CALLBACK_PATH

__all__ = ["mock_idp_server"]

#: The facade's `PUBLIC_URL` must actually be reachable here (unlike
#: `tests/auth/test_login_entra.py`'s own fixed `_PUBLIC_URL`, fine there
#: only because every client in that module talks to an in-process ASGI
#: transport that ignores the host entirely): this test's facade is a real
#: subprocess, and the Entra sign-in flow redirects the real `httpx` client
#: through this URL's own `/login` and `CALLBACK_PATH` by absolute
#: `Location` header - so it is built from the facade's own pre-chosen port
#: (`_run_facade_server`) instead of a fixed, non-resolvable name.
_MCP_PATH = "/mcp"
_MCP_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key

_TID = "e2e-tenant"
_ENTRA_CLIENT_ID = "e2e-entra-client"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - fake test credential
_OID = "oid-e2e-acceptance"

_EMBEDDING_DIMENSIONS = 1024
_EMBEDDING_MODEL = "stub-embedding"

#: Short enough that scenario 3's poll below finishes well inside
#: `_POLL_TIMEOUT_SECONDS`, long enough that the first round never races the
#: worker's own startup (`_job_loop` sleeps `entra_delta_sync_seconds` before
#: its very first run, same as `worker.py`'s own `_cleanup_job`).
_ENTRA_DELTA_SYNC_SECONDS = 1.0
_JOBS_POLL_SECONDS = 1.0

_POLL_INTERVAL_SECONDS = 0.2
_POLL_TIMEOUT_SECONDS = 20.0

_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


# === a disposable Postgres owner/app role (duplicated from test_replicas.py) =======


@dataclass(frozen=True)
class _AcceptanceDb:
    owner_url: str
    app_role: str
    pool: asyncpg.Pool
    #: The admin connection's own credentials, pointed at this fixture's
    #: database instead of `owner_url`/`app_role` - `notes`/`chunks` are
    #: `FORCE ROW LEVEL SECURITY` tables (`db/rls.py`'s own module
    #: docstring), so even the non-superuser *owner* connection above sees
    #: them filtered to nothing without a request principal's `SET LOCAL`
    #: GUCs. `admin_database_url` is the Postgres image's own superuser
    #: locally and in CI (`Makefile`'s `db-up`: `POSTGRES_USER=mm`) and so
    #: bypasses RLS outright - used here only for this test's own direct
    #: verification queries, never by the facade/worker subprocesses.
    verify_url: str


@pytest_asyncio.fixture
async def _acceptance_db(admin_database_url: str) -> AsyncIterator[_AcceptanceDb]:
    db_name = f"mm_test_e2e_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await admin_conn.execute(f'grant "{app_role}" to "{owner_role}"')

        parsed = urlsplit(admin_database_url)
        base, _, _ = admin_database_url.rpartition("/")
        bootstrap_conn = await asyncpg.connect(f"{base}/{db_name}")
        try:
            await bootstrap_conn.execute("create extension if not exists vector")
        finally:
            await bootstrap_conn.close()

        owner_url = urlunsplit(
            (
                parsed.scheme,
                f"{owner_role}:{owner_password}@{parsed.hostname}:{parsed.port}",
                f"/{db_name}",
                "",
                "",
            )
        )
        owner_conn = await asyncpg.connect(owner_url)
        pool = await asyncpg.create_pool(owner_url)
        yield _AcceptanceDb(
            owner_url=owner_url, app_role=app_role, pool=pool, verify_url=f"{base}/{db_name}"
        )
    finally:
        if pool is not None:
            await pool.close()
        if owner_conn is not None:
            await owner_conn.close()
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'drop role if exists "{app_role}"')
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.close()


# === a real OpenAI-compatible embedding stub, served in-test ========================


@asynccontextmanager
async def _run_embedding_stub() -> AsyncIterator[str]:
    """A real `POST /embeddings` endpoint on a real, free port - every call
    answers with `_EMBEDDING_DIMENSIONS`-wide vectors, one per `input` item,
    the shape `index/embeddings.py`'s `OpenAICompatibleProvider` parses
    (module docstring: that provider opens its own `httpx.AsyncClient`, so
    this needs to be a reachable network endpoint, not an ASGI transport).
    """

    async def embeddings(request: Request) -> JSONResponse:
        body = await request.json()
        texts = body.get("input") or []
        data = [
            {"index": i, "embedding": [0.01 * ((i + 1) % 7 + 1)] * _EMBEDDING_DIMENSIONS}
            for i in range(len(texts))
        ]
        return JSONResponse({"data": data, "model": body.get("model", _EMBEDDING_MODEL)})

    app = Starlette(routes=[Route("/embeddings", embeddings, methods=["POST"])])
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not server.started:
            if task.done():
                task.result()
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("embedding stub did not start in time")
            await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# === a real `memory-manager worker` subprocess (duplicated from test_singleton.py) =


_WORKER_CLI_ARGS = ("-m", "memory_manager.cli", "worker")
#: Shared by `_run_worker_server` and `_run_facade_server` below - both tear
#: down a `memory-manager` subprocess the identical way `http_fixtures.
#: run_http_server` does.
_SUBPROCESS_SHUTDOWN_TIMEOUT = 5.0


@asynccontextmanager
async def _run_worker_server(env: Mapping[str, str]) -> AsyncIterator[Server]:
    port = free_port()
    full_env = {**os.environ, **env, "WORKER_HOST": "127.0.0.1", "WORKER_PORT": str(port)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *_WORKER_CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await wait_until_ready(process, base_url)
        yield Server(process=process, base_url=base_url, mcp_url=f"{base_url}/mcp")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SUBPROCESS_SHUTDOWN_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()


# === a real `memory-manager serve --http` facade subprocess on a pre-chosen port ====


@asynccontextmanager
async def _run_facade_server(env: Mapping[str, str], *, port: int) -> AsyncIterator[Server]:
    """`http_fixtures.run_http_server`'s own subprocess-launch shape, duplicated
    here only because that helper always picks its own random port (its own
    docstring: "HOST/PORT are always overridden, last") - this test needs the
    port fixed *before* starting the facade, so `env["PUBLIC_URL"]` (module
    docstring: must be a real, reachable origin here) can name it.
    """
    full_env = {**os.environ, **env, "HOST": "127.0.0.1", "PORT": str(port)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await wait_until_ready(process, base_url)
        yield Server(process=process, base_url=base_url, mcp_url=f"{base_url}/mcp")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SUBPROCESS_SHUTDOWN_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()


# === Entra/DCR/PKCE choreography (duplicated from test_login_entra.py) ==============


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def _register_entra_client(
    mock_idp: httpx.AsyncClient,
    *,
    tenant_id: str = _TID,
    client_id: str = _ENTRA_CLIENT_ID,
    client_secret: str = _ENTRA_CLIENT_SECRET,
    redirect_uri: str,
    graph_roles: list[str] | None = None,
) -> None:
    response = await mock_idp.post(
        "/_mock/clients",
        json={
            "tid": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [redirect_uri],
            "graph_roles": graph_roles or [],
        },
    )
    assert response.status_code == 201, response.text


async def _create_entra_user(
    mock_idp: httpx.AsyncClient, oid: str, *, tenant_id: str = _TID, **fields: Any
) -> None:
    response = await mock_idp.post("/_mock/users", json={"tid": tenant_id, "oid": oid, **fields})
    assert response.status_code == 201, response.text


async def _select_signin(mock_idp: httpx.AsyncClient, oid: str, *, tenant_id: str = _TID) -> None:
    response = await mock_idp.post("/_mock/select-signin", json={"tid": tenant_id, "oid": oid})
    assert response.status_code == 200, response.text


async def _disable_entra_user(
    mock_idp: httpx.AsyncClient, oid: str, *, tenant_id: str = _TID
) -> None:
    response = await mock_idp.patch(
        f"/_mock/users/{tenant_id}/{oid}", json={"account_enabled": False}
    )
    assert response.status_code == 200, response.text


async def _register_mcp_client(
    client: httpx.AsyncClient, *, redirect_uri: str = _MCP_REDIRECT_URI
) -> str:
    response = await client.post(
        "/register",
        json={"redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _start_authorize(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    resource: str,
    redirect_uri: str = _MCP_REDIRECT_URI,
    state: str = "xyz",
) -> httpx.Response:
    authorize_response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": resource,
        },
    )
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == LOGIN_PATH
    return await client.get(login_url)


async def _confirm_interstitial(
    client: httpx.AsyncClient, interstitial_response: httpx.Response
) -> httpx.Response:
    assert interstitial_response.status_code == 200, interstitial_response.text
    marker = 'class="button" href="'
    start = interstitial_response.text.index(marker) + len(marker)
    end = interstitial_response.text.index('"', start)
    continue_url = html.unescape(interstitial_response.text[start:end])
    return await client.get(continue_url)


async def _drive_entra_login(
    client: httpx.AsyncClient,
    mock_idp: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    resource: str,
    redirect_uri: str = _MCP_REDIRECT_URI,
) -> httpx.Response:
    """`/authorize` -> `/login` (interstitial) -> "Continue to sign in" -> the
    mock's own redirect back to `CALLBACK_PATH`, replayed as a direct `GET`
    against `client` - the facade's own `PUBLIC_URL` is a real, reachable
    origin here (module docstring), so every absolute redirect along this
    chain resolves on its own; `client.get(CALLBACK_PATH, ...)` below is
    still relative to `client`'s own `base_url` rather than replaying the
    mock's literal `Location`, the same shape `test_login_entra.py`'s own
    `_drive_entra_login` uses.
    """
    interstitial_response = await _start_authorize(
        client,
        client_id=client_id,
        code_challenge=code_challenge,
        resource=resource,
        redirect_uri=redirect_uri,
    )
    redirect_response = await _confirm_interstitial(client, interstitial_response)
    assert redirect_response.status_code == 302, redirect_response.text
    mock_authorize_url = _location(redirect_response)

    mock_redirect = await mock_idp.get(mock_authorize_url)
    assert mock_redirect.status_code == 302, mock_redirect.text
    callback_params = {key: values[0] for key, values in _query(_location(mock_redirect)).items()}

    return await client.get(CALLBACK_PATH, params=callback_params)


# === MCP tool calls, raw JSON-RPC (duplicated from test_replicas.py) ================


@dataclass(frozen=True)
class _ToolCallOutcome:
    status_code: int
    result: dict[str, Any] | None


async def _call_tool(
    client: httpx.AsyncClient, server: Server, token: str, tool: str, arguments: dict[str, object]
) -> _ToolCallOutcome:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token}"}
    response = await client.post(server.mcp_url, json=body, headers=headers)
    if response.status_code != 200:
        return _ToolCallOutcome(status_code=response.status_code, result=None)
    payload = response.json()
    assert "error" not in payload, payload
    result = payload["result"]
    assert isinstance(result, dict)
    return _ToolCallOutcome(status_code=200, result=result)


def _note_content(unique_term: str) -> str:
    return (
        "---\n"
        "title: Acceptance fixture note\n"
        f"description: Mentions {unique_term} exactly once.\n"
        "type: fact\n"
        "---\n"
        f"The unique term is {unique_term}.\n"
    )


# === environments for the facade and worker subprocesses ============================


def _entra_env(db: _AcceptanceDb, mock_idp: MockIdpServer, embedding_url: str) -> dict[str, str]:
    return {
        "ENTRA_TENANT_ID": _TID,
        "ENTRA_CLIENT_ID": _ENTRA_CLIENT_ID,
        "ENTRA_CLIENT_SECRET": _ENTRA_CLIENT_SECRET,
        "ENTRA_AUTHORITY": mock_idp.base_url,
        "ENTRA_GRAPH_URL": f"{mock_idp.base_url}/graph/v1.0",
        "ENTRA_ALLOW_INSECURE_AUTHORITY": "1",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_URL": embedding_url,
        "EMBEDDING_MODEL": _EMBEDDING_MODEL,
        "EMBEDDING_API_KEY": "stub-key",
        "EMBEDDING_DIMENSIONS": str(_EMBEDDING_DIMENSIONS),
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": db.owner_url,
    }


def _facade_env(
    db: _AcceptanceDb, mock_idp: MockIdpServer, embedding_url: str, *, public_url: str
) -> dict[str, str]:
    return {
        **_entra_env(db, mock_idp, embedding_url),
        "LOGIN_MODE": "entra",
        "PUBLIC_URL": public_url,
        "MCP_PATH": _MCP_PATH,
        "DATABASE_APP_ROLE": db.app_role,
        "OAUTH_CLIENT_SECRET_KEY": _CLIENT_SECRET_KEY,
    }


def _worker_env(db: _AcceptanceDb, mock_idp: MockIdpServer, embedding_url: str) -> dict[str, str]:
    return {
        **_entra_env(db, mock_idp, embedding_url),
        "SHUTDOWN_GRACE_SECONDS": "5",
        "ENTRA_DELTA_SYNC_SECONDS": str(_ENTRA_DELTA_SYNC_SECONDS),
        "JOBS_POLL_SECONDS": str(_JOBS_POLL_SECONDS),
    }


async def _poll_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float = _POLL_TIMEOUT_SECONDS
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met within the poll timeout")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


# === the acceptance test itself =======================================================


async def test_entra_signin_full_text_first_search_and_deprovisioning(
    _acceptance_db: _AcceptanceDb,
    mock_idp_server: MockIdpServer,
) -> None:
    facade_port = free_port()
    facade_public_url = f"http://127.0.0.1:{facade_port}"
    resource = f"{facade_public_url}{_MCP_PATH}"

    async with (
        _run_embedding_stub() as embedding_url,
        httpx.AsyncClient(base_url=mock_idp_server.base_url, timeout=10.0) as mock_idp,
    ):
        await _register_entra_client(
            mock_idp,
            redirect_uri=f"{facade_public_url}{CALLBACK_PATH}",
            graph_roles=["User.Read.All"],
        )
        await _create_entra_user(mock_idp, _OID, roles=["Memory.User"], groups=[])
        await _select_signin(mock_idp, _OID)

        facade_env = _facade_env(
            _acceptance_db, mock_idp_server, embedding_url, public_url=facade_public_url
        )
        async with (
            _run_facade_server(facade_env, port=facade_port) as facade,
            httpx.AsyncClient(
                base_url=facade.base_url, timeout=10.0, follow_redirects=False
            ) as client,
        ):
            # --- scenario 1: DCR, PKCE, Entra sign-in, write, read -------------

            client_id = await _register_mcp_client(client)
            _verifier, code_challenge = _pkce_pair()
            callback_response = await _drive_entra_login(
                client,
                mock_idp,
                client_id=client_id,
                code_challenge=code_challenge,
                resource=resource,
            )
            assert callback_response.status_code == 302, callback_response.text
            code = _query(_location(callback_response))["code"][0]

            token_response = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _MCP_REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": _verifier,
                },
            )
            assert token_response.status_code == 200, token_response.text
            tokens = token_response.json()
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]

            unique_term = f"zyxacceptance{secrets.token_hex(4)}"
            written = await _call_tool(
                client,
                facade,
                access_token,
                "memory_write",
                {
                    "path": "me/fact/acceptance-check.md",
                    "content": _note_content(unique_term),
                    "if_version": "new",
                },
            )
            assert written.result is not None and written.result["isError"] is False
            note_path = written.result["structuredContent"]["path"]
            assert note_path == "me/fact/acceptance-check.md"

            read_back = await _call_tool(
                client, facade, access_token, "memory_read", {"items": [note_path]}
            )
            assert read_back.result is not None and read_back.result["isError"] is False
            assert unique_term in read_back.result["structuredContent"]["result"][0]["content"]

            # --- scenario 2: full-text-first search, then the embedding --------

            async def _chunks_without_embedding() -> int:
                # A fresh superuser connection each time (`verify_url`'s own
                # docstring: bypasses `FORCE ROW LEVEL SECURITY`), not
                # `_acceptance_db.pool` - that pool authenticates as the
                # facade/worker's own owner role, which `notes`/`chunks`'
                # policies filter to nothing without a request principal.
                # No `notes.path` filter: `notes.path` is the canonical,
                # namespace-alias-resolved form ("u-1/...", not the literal
                # "me/..." `note_path` names) and this fixture's database
                # holds exactly the one note this scenario just wrote.
                verify_conn = await asyncpg.connect(_acceptance_db.verify_url)
                try:
                    count = await verify_conn.fetchval(
                        "select count(*) from chunks where embedding is null"
                    )
                finally:
                    await verify_conn.close()
                assert isinstance(count, int)
                return count

            assert await _chunks_without_embedding() > 0

            before_worker = await _call_tool(
                client, facade, access_token, "memory_search", {"query": unique_term}
            )
            assert before_worker.result is not None and before_worker.result["isError"] is False
            before_body = before_worker.result["structuredContent"]
            assert before_body["mode"] == "hybrid"
            assert any(hit["path"] == note_path for hit in before_body["results"])

            async def _embedding_has_arrived() -> bool:
                return await _chunks_without_embedding() == 0

            worker_env = _worker_env(_acceptance_db, mock_idp_server, embedding_url)
            async with _run_worker_server(worker_env) as worker:
                await _poll_until(_embedding_has_arrived)

                after_worker = await _call_tool(
                    client, facade, access_token, "memory_search", {"query": unique_term}
                )
                assert after_worker.result is not None and after_worker.result["isError"] is False
                after_body = after_worker.result["structuredContent"]
                assert any(hit["path"] == note_path for hit in after_body["results"])

                # --- scenario 3: deprovisioning -----------------------------

                await _disable_entra_user(mock_idp, _OID)

                async def _next_call_is_401() -> bool:
                    outcome = await _call_tool(
                        client, facade, access_token, "memory_search", {"query": unique_term}
                    )
                    return outcome.status_code == 401

                await _poll_until(_next_call_is_401)

                refresh_response = await client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                    },
                )
                assert refresh_response.status_code == 400, refresh_response.text
                assert refresh_response.json()["error"] == "invalid_grant"

                assert worker.process.returncode is None

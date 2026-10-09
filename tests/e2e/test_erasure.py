# SPDX-License-Identifier: AGPL-3.0-only
"""M9's own acceptance test (#241, `docs/features/F-01-enterprise-scale.md`):
after a user is erased, no content of theirs remains anywhere in Postgres,
and their traces inside notes they do not own (a shared note's own author,
an audit row about one of their notes) are pseudonymized or redacted
instead of deleted outright - the milestone closes once this is green.

Driven against real `memory-manager serve --http`, `memory-manager worker`
and mock-IdP *processes*, the same shape `tests/e2e/test_entra_acceptance.py`
already proved for M8 - most of the subprocess/Entra-choreography machinery
below is a trimmed copy of that module's own private helpers (that module's
own docstring: duplicated rather than imported, since it itself exports
nothing but its `mock_idp_server` fixture).

One user's worth of setup (`_setup_one_user`) is run twice, once per way a
user is actually deleted - once erased through the admin area, once through
the retention job's own shifted clock:

1. Personal notes, each carrying a token unique to one frontmatter field
   (title/body/tags/slug, never shared between fields or notes) so a later
   per-field sentinel scan can tell exactly which field, if any, survived -
   three of them (write+edit, write+archive, write-then-leave-for-the-
   pending-job) are purely personal and feed `_SetupResult.personal_tokens`,
   the "zero hits anywhere" set.
2. A fourth note is written and promoted into a shared (group) namespace -
   `memory_promote`'s own copy keeps every one of its fields forever, by
   design (ADR-0007 §3 addendum 2026-10-08: notes in shared namespaces stay),
   so none of its tokens join `personal_tokens`; only its `body` token is
   tracked at all (`_SetupResult.promoted_token`), checked to survive in the
   shared namespace and vanish from the personal one.
3. A second shared note, written directly into that same namespace and then
   edited - "edits another shared note" (its own token, `_SetupResult.
   shared_token`, is shared content from the start and is never expected to
   disappear either; only the user's own authorship of it is).
4. The worker runs once, draining every job the writes above enqueued, then
   is stopped - the one note written *after* that point leaves its own
   `embed_note` job `pending` forever, the "worker runs once and leaves one
   job pending" the issue's own Implementation checklist asks for.
5. A `Memory.Admin` requests, then (with `BREAK_GLASS_APPROVERS=1`)
   self-approves, break-glass access to the user's personal namespace - the
   approval writes a `reference` note into that namespace (`account.
   break_glass._write_break_glass_notice`) and the admin then reads one
   personal note (the one left with a pending job) through the read-only
   viewer (`account.break_glass_viewer`), which records `audit_log.path` as
   that note's own, token-bearing path. This is "the break-glass reference
   note case": both the note and the audit trail about it must be gone (or
   redacted) afterwards, exactly like everything else personal.

`_assert_token_gone_everywhere`/`_sentinel_hits` then check, for every table
named in #241's own Implementation checklist plus every other table that
could plausibly carry personal data (`account_sessions`, `break_glass_grants`,
`entra_delta_cursor`, `namespaces`, `users`, `user_groups`, `static_tokens`,
`oauth_tokens`, `oauth_auth_codes`) - every column of each, cast to text
(`convert_from` for `bytea`, so a note's own content is read as UTF-8 rather
than its raw hex dump) - for zero hits of any purely-personal token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

from memory_manager.account.admin import CONFIRM_FIELD_NAME, CSRF_FORM_ERASE, ERASE_PATH
from memory_manager.account.break_glass import (
    APPROVE_PATH,
    CSRF_FORM_APPROVE,
    CSRF_FORM_REQUEST,
    REQUEST_PATH,
)
from memory_manager.account.break_glass_viewer import NOTE_PATH, VIEW_PATH
from memory_manager.account.routes import SESSION_COOKIE
from memory_manager.account.sessions import csrf_token
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.auth.login import LOGIN_PATH
from memory_manager.auth.login_entra import CALLBACK_PATH
from memory_manager.auth.users import mark_disabled
from memory_manager.worker import _RETENTION_ACTOR, _retention_job

__all__ = ["mock_idp_server"]

_MCP_PATH = "/mcp"
_MCP_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key
_ACCOUNT_PATH = "/account"

_TID = "e2e-erasure-tenant"
_ENTRA_CLIENT_ID = "e2e-erasure-entra-client"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - fake test credential

_EMBEDDING_DIMENSIONS = 1024
_EMBEDDING_MODEL = "stub-embedding"

_JOBS_POLL_SECONDS = 1.0
_ENTRA_DELTA_SYNC_SECONDS = 60.0  # not exercised here; just needs to be short enough to not block

_POLL_INTERVAL_SECONDS = 0.2
_POLL_TIMEOUT_SECONDS = 20.0

_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}

_RETENTION_DAYS = 30

#: Every table #241's own Implementation checklist names, plus every other
#: table that could plausibly carry personal data (module docstring's own
#: list; the task's own guideline) - scanned, in full, by `_scan_for_sentinel`
#: below.
_SCANNED_TABLES = (
    "vault_notes",
    "vault_revisions",
    "notes",
    "chunks",
    "links",
    "jobs",
    "audit_log",
    "erasure_log",
    "account_sessions",
    "break_glass_grants",
    "entra_delta_cursor",
    "namespaces",
    "users",
    "user_groups",
    "static_tokens",
    "oauth_tokens",
    "oauth_auth_codes",
)

#: Column data types with no textual representation worth scanning: a
#: `tsvector` is already derived from `chunks.text` (scanned directly), a
#: pgvector `vector`/`halfvec` embedding never carries personal text, and
#: `xid8` is a bare transaction id.
_SKIP_DATA_TYPES = frozenset({"tsvector", "USER-DEFINED", "xid8"})


# === a disposable Postgres owner/app role (duplicated from test_entra_acceptance.py) =


@dataclass(frozen=True)
class _AcceptanceDb:
    owner_url: str
    app_role: str
    pool: asyncpg.Pool
    #: A superuser connection, pointed at this fixture's own database - every
    #: content table is `FORCE ROW LEVEL SECURITY`, so even the non-superuser
    #: owner connection above would see them filtered to nothing without a
    #: request principal's own `SET LOCAL` GUCs (`test_entra_acceptance.py`'s
    #: own `_AcceptanceDb.verify_url` gives the identical reasoning). Used
    #: here for every direct seed/verification query; never by the facade or
    #: worker subprocesses themselves.
    verify_url: str


@pytest_asyncio.fixture
async def _acceptance_db(admin_database_url: str) -> AsyncIterator[_AcceptanceDb]:
    db_name = f"mm_test_e2e_erasure_{secrets.token_hex(8)}"
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


# === a real OpenAI-compatible embedding stub (duplicated from test_entra_acceptance.py) =


@asynccontextmanager
async def _run_embedding_stub() -> AsyncIterator[str]:
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


# === real `memory-manager worker`/`serve --http` subprocesses (duplicated) ===========

_WORKER_CLI_ARGS = ("-m", "memory_manager.cli", "worker")
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


@asynccontextmanager
async def _run_facade_server(env: Mapping[str, str], *, port: int) -> AsyncIterator[Server]:
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


# === Entra/DCR/PKCE choreography (duplicated from test_entra_acceptance.py) =========


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def _register_entra_client(
    mock_idp: httpx.AsyncClient, *, redirect_uri: str, graph_roles: list[str] | None = None
) -> None:
    response = await mock_idp.post(
        "/_mock/clients",
        json={
            "tid": _TID,
            "client_id": _ENTRA_CLIENT_ID,
            "client_secret": _ENTRA_CLIENT_SECRET,
            "redirect_uris": [redirect_uri],
            "graph_roles": graph_roles or [],
        },
    )
    assert response.status_code == 201, response.text


async def _create_entra_user(mock_idp: httpx.AsyncClient, oid: str, *, roles: list[str]) -> None:
    response = await mock_idp.post(
        "/_mock/users", json={"tid": _TID, "oid": oid, "roles": roles, "groups": []}
    )
    assert response.status_code == 201, response.text


async def _select_signin(mock_idp: httpx.AsyncClient, oid: str) -> None:
    response = await mock_idp.post("/_mock/select-signin", json={"tid": _TID, "oid": oid})
    assert response.status_code == 200, response.text


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _confirm_interstitial(
    client: httpx.AsyncClient, interstitial_response: httpx.Response
) -> httpx.Response:
    assert interstitial_response.status_code == 200, interstitial_response.text
    marker = 'class="button" href="'
    start = interstitial_response.text.index(marker) + len(marker)
    end = interstitial_response.text.index('"', start)
    continue_url = html.unescape(interstitial_response.text[start:end])
    return await client.get(continue_url)


async def _register_mcp_client(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/register",
        json={"redirect_uris": [_MCP_REDIRECT_URI], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


async def _drive_entra_login_to_callback(
    client: httpx.AsyncClient, mock_idp: httpx.AsyncClient, *, login_response: httpx.Response
) -> httpx.Response:
    """From a `/login` interstitial response (either an MCP `/authorize` or an
    `/account/login` pending attempt - both share the one `/login` route and
    its interstitial template, `account.routes`' own module docstring) all
    the way to the facade's own `CALLBACK_PATH`, replayed as a `GET` against
    `client` - the one piece of the Entra choreography both kinds of login
    share from this point on."""
    redirect_response = await _confirm_interstitial(client, login_response)
    assert redirect_response.status_code == 302, redirect_response.text
    mock_authorize_url = _location(redirect_response)

    mock_redirect = await mock_idp.get(mock_authorize_url)
    assert mock_redirect.status_code == 302, mock_redirect.text
    callback_params = {key: values[0] for key, values in _query(_location(mock_redirect)).items()}

    return await client.get(CALLBACK_PATH, params=callback_params)


async def _mcp_login(
    client: httpx.AsyncClient, mock_idp: httpx.AsyncClient, *, oid: str, resource: str
) -> str:
    """DCR + PKCE + Entra sign-in for `oid`, returning its MCP access token -
    `client` and `mock_idp` are otherwise untouched by this (no persistent
    state beyond the one access token it hands back)."""
    client_id = await _register_mcp_client(client)
    verifier, code_challenge = _pkce_pair()

    authorize_response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _MCP_REDIRECT_URI,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": resource,
        },
    )
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == LOGIN_PATH
    login_response = await client.get(login_url)

    callback_response = await _drive_entra_login_to_callback(
        client, mock_idp, login_response=login_response
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
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    access_token = token_response.json()["access_token"]
    assert isinstance(access_token, str)
    return access_token


def _account_headers(session_id: str) -> dict[str, str]:
    """The session cookie, attached by hand rather than left to `client`'s own
    cookie jar: `with_session_cookie` marks it `Secure` (ADR-0008 addendum), so
    a real HTTP (not HTTPS) client talking to the subprocess facade below -
    unlike `tests/account/test_admin_erasure.py`'s own `https://testserver`
    ASGI transport - would otherwise have every reply to it silently dropped
    by `httpx`'s own cookie jar, the same `Secure`-attribute policy a browser
    enforces (a real deployment always terminates TLS in front of this, so
    this gap is a property of driving a plain-HTTP subprocess in a test, not
    of production)."""
    return {"Cookie": f"{SESSION_COOKIE}={session_id}"}


async def _account_login(client: httpx.AsyncClient, mock_idp: httpx.AsyncClient) -> str:
    """The cookie-session login `/account` itself uses (`account.routes`'
    own `LOGIN_PATH_START` -> the shared `/login` -> `CALLBACK_PATH`
    choreography) - returns the plaintext `mm_session` cookie value
    `account.sessions.csrf_token` needs for every subsequent form on this
    session, read straight out of `client`'s own cookie jar rather than
    scraped from a page's hidden `<input>` (`account.sessions.csrf_token`
    is a pure HMAC of `(session_id, form_label)`, so there is nothing a
    page render would tell this test that computing it directly does not).
    """
    start_response = await client.get(f"{_ACCOUNT_PATH}/login")
    assert start_response.status_code == 302, start_response.text
    login_response = await client.get(_location(start_response))

    callback_response = await _drive_entra_login_to_callback(
        client, mock_idp, login_response=login_response
    )
    assert callback_response.status_code == 302, callback_response.text
    assert _location(callback_response) == _ACCOUNT_PATH

    session_id = client.cookies.get(SESSION_COOKIE)
    assert session_id is not None, "the account login did not set a session cookie"
    return session_id


# === MCP tool calls, raw JSON-RPC (duplicated from test_entra_acceptance.py) ========


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


async def _tool_call_ok(
    client: httpx.AsyncClient, server: Server, token: str, tool: str, arguments: dict[str, object]
) -> dict[str, Any]:
    outcome = await _call_tool(client, server, token, tool, arguments)
    assert outcome.result is not None and outcome.result["isError"] is False, outcome
    structured = outcome.result["structuredContent"]
    assert isinstance(structured, dict)
    return structured


def _note_content(*, title: str, description: str, tag: str, body: str) -> str:
    return (
        f"---\ntitle: {title}\ndescription: {description}\ntype: fact\ntags: [{tag}]\n---\n{body}\n"
    )


@dataclass(frozen=True)
class _PersonalNote:
    """One personal note's own, independent token per field - `title`,
    `body`, `tag` and `slug` (module docstring: "unique per field") never
    share a value, so a scan that finds one of them pins down exactly which
    field, if any, survived. The frontmatter `description` field itself
    carries no token at all - it only exists to satisfy `vault.note.parse`'s
    required fields."""

    title: str
    body: str
    tag: str
    slug: str

    @classmethod
    def new(cls, label: str) -> _PersonalNote:
        base = secrets.token_hex(5)
        return cls(
            title=f"mm{base}ti{label}",
            body=f"mm{base}bo{label}",
            tag=f"mm{base}ta{label}",
            slug=f"mm{base}sl{label}",
        )

    def tokens(self) -> tuple[str, ...]:
        return (self.title, self.body, self.tag, self.slug)

    def path(self, *, namespace: str = "me") -> str:
        return f"{namespace}/fact/{self.slug}.md"

    def content(self) -> str:
        return _note_content(
            title=f"Erasure fixture {self.title}",
            description="Fixture note for the erasure acceptance test.",
            tag=self.tag,
            body=f"The body token is {self.body}.",
        )


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
        #: Lets one admin request *and* approve their own break-glass grant
        #: (ADR-0008 "operators may lower it to 1") - this test has only one
        #: admin identity to spare.
        "BREAK_GLASS_APPROVERS": "1",
    }


def _worker_env(db: _AcceptanceDb, mock_idp: MockIdpServer, embedding_url: str) -> dict[str, str]:
    return {
        **_entra_env(db, mock_idp, embedding_url),
        "SHUTDOWN_GRACE_SECONDS": "5",
        "ENTRA_DELTA_SYNC_SECONDS": str(_ENTRA_DELTA_SYNC_SECONDS),
        "JOBS_POLL_SECONDS": str(_JOBS_POLL_SECONDS),
    }


# === direct, RLS-bypassing seed/verification queries (verify_url) ===================


async def _seed_shared_namespace(conn: asyncpg.Connection, *, alias: str, oid: str) -> None:
    """A group namespace the user is a member of, direct against the
    superuser connection (`namespaces`/`user_groups` carry no RLS of their
    own, `0005_rls.sql`'s module docstring) - the shortest path to a shared
    namespace this test can write to, without going through the admin UI's
    own namespace-creation form (not itself under test here)."""
    await conn.execute(
        "insert into namespaces (kind, external_key, alias) values ('group', $1, $2)",
        f"grp-{alias}",
        alias,
    )
    await conn.execute(
        "insert into user_groups (oid, group_id) values ($1, $2)", oid, f"grp-{alias}"
    )


async def _personal_alias(conn: asyncpg.Connection, oid: str) -> str:
    alias = await conn.fetchval(
        "select alias from namespaces where kind = 'user' and external_key = $1", oid
    )
    assert isinstance(alias, str)
    return alias


async def _pending_jobs(conn: asyncpg.Connection) -> int:
    count = await conn.fetchval("select count(*) from jobs where state = 'pending'")
    return int(count or 0)


async def _text_columns(conn: asyncpg.Connection, table: str) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = 'public' and table_name = $1",
        table,
    )
    return [
        (row["column_name"], row["data_type"])
        for row in rows
        if row["data_type"] not in _SKIP_DATA_TYPES
    ]


async def _sentinel_hits(conn: asyncpg.Connection, table: str, token: str) -> int:
    """How many rows of `table` mention `token` in any column, cast to text -
    `bytea` columns (every note's own `content`) decoded as UTF-8 first
    (module docstring) so the substring search sees the note's own text,
    not a hex dump of its bytes."""
    columns = await _text_columns(conn, table)
    if not columns:
        return 0
    casts = [
        f"convert_from(\"{name}\", 'UTF8')" if data_type == "bytea" else f'"{name}"::text'
        for name, data_type in columns
    ]
    predicate = " or ".join(f"{cast} like $1" for cast in casts)
    query = f'select count(*) from "{table}" where {predicate}'  # noqa: S608 - fixed table list
    count = await conn.fetchval(query, f"%{token}%")
    return int(count or 0)


async def _assert_token_gone_everywhere(conn: asyncpg.Connection, token: str) -> None:
    for table in _SCANNED_TABLES:
        hits = await _sentinel_hits(conn, table, token)
        assert hits == 0, f"sentinel {token!r} still present in {table!r}"


# === one user's worth of setup, shared by both erasure variants =====================


@dataclass(frozen=True)
class _SetupResult:
    oid: str
    personal_alias: str
    #: The four purely-personal notes' own tokens - every one of these must
    #: have zero hits, anywhere, once this user is erased.
    personal_tokens: tuple[str, ...]
    #: The token of the note promoted into `shared_namespace` - expected to
    #: *survive* there, never part of the "zero hits" set above.
    promoted_token: str
    #: The token of the second shared note, written directly into
    #: `shared_namespace` and then edited - shared content from the start,
    #: also expected to survive; only its author's pseudonymization is
    #: checked.
    shared_token: str
    shared_namespace: str
    shared_edit_path: str
    #: The personal note a break-glass grant's own viewer read - its path
    #: carries one of `personal_tokens` (via its slug), so `audit_log.path`
    #: for that view must be redacted along with everything else.
    viewed_note_path: str


async def _setup_one_user(
    *,
    account: httpx.AsyncClient,
    worker_env: dict[str, str],
    mock_idp: httpx.AsyncClient,
    mcp_client: httpx.AsyncClient,
    facade: Server,
    resource: str,
    verify_conn: asyncpg.Connection,
    admin_session_id: str,
    label: str,
) -> _SetupResult:
    oid = f"oid-erasure-{label}-{secrets.token_hex(4)}"
    shared_namespace = f"team-erasure-{label}"

    await _create_entra_user(mock_idp, oid, roles=["Memory.User"])
    await _select_signin(mock_idp, oid)
    access_token = await _mcp_login(mcp_client, mock_idp, oid=oid, resource=resource)

    note_edit = _PersonalNote.new(f"{label}edit")
    note_archive = _PersonalNote.new(f"{label}archive")
    note_promote = _PersonalNote.new(f"{label}promote")
    note_pending = _PersonalNote.new(f"{label}pending")

    written = await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_write",
        {"path": note_edit.path(), "content": note_edit.content(), "if_version": "new"},
    )
    await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_edit",
        {
            "path": note_edit.path(),
            "old_str": "The body token is",
            "new_str": "The edited body token is",
            "if_version": written["version"],
        },
    )

    written = await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_write",
        {"path": note_archive.path(), "content": note_archive.content(), "if_version": "new"},
    )
    await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_archive",
        {"path": note_archive.path(), "if_version": written["version"]},
    )

    personal_alias = await _personal_alias(verify_conn, oid)
    await _seed_shared_namespace(verify_conn, alias=shared_namespace, oid=oid)

    written = await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_write",
        {"path": note_promote.path(), "content": note_promote.content(), "if_version": "new"},
    )
    await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_promote",
        {
            "path": note_promote.path(),
            "target_namespace": shared_namespace,
            "if_version": written["version"],
        },
    )

    shared_note = _PersonalNote.new(f"{label}shared")
    shared_path = shared_note.path(namespace=shared_namespace)
    written = await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_write",
        {"path": shared_path, "content": shared_note.content(), "if_version": "new"},
    )
    await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_edit",
        {
            "path": shared_path,
            "old_str": "The body token is",
            "new_str": "The edited (by this user) body token is",
            "if_version": written["version"],
        },
    )

    async with _run_worker_server(worker_env) as worker:
        await _poll_until(lambda: _no_pending(verify_conn))
        assert worker.process.returncode is None

    await _tool_call_ok(
        mcp_client,
        facade,
        access_token,
        "memory_write",
        {"path": note_pending.path(), "content": note_pending.content(), "if_version": "new"},
    )
    pending = await _pending_jobs(verify_conn)
    assert pending == 1, f"expected exactly one pending job, got {pending}"

    request_response = await account.post(
        REQUEST_PATH,
        data={
            CSRF_FIELD_NAME: csrf_token(admin_session_id, CSRF_FORM_REQUEST),
            "oid": oid,
            "reason": f"e2e erasure acceptance test ({label})",
        },
        headers=_account_headers(admin_session_id),
    )
    assert request_response.status_code == 302, request_response.text
    grant_id = await verify_conn.fetchval(
        "select g.id from break_glass_grants g join namespaces n on n.id = g.namespace_id "
        "where n.external_key = $1 order by g.id desc limit 1",
        oid,
    )
    assert grant_id is not None

    approve_response = await account.post(
        APPROVE_PATH,
        data={
            CSRF_FIELD_NAME: csrf_token(admin_session_id, CSRF_FORM_APPROVE),
            "grant_id": str(grant_id),
        },
        headers=_account_headers(admin_session_id),
    )
    assert approve_response.status_code == 302, approve_response.text

    viewed_note_path = f"{personal_alias}/fact/{note_pending.slug}.md"
    view_response = await account.get(
        VIEW_PATH, params={"grant_id": grant_id}, headers=_account_headers(admin_session_id)
    )
    assert view_response.status_code == 200, view_response.text
    note_view_response = await account.get(
        NOTE_PATH,
        params={"grant_id": grant_id, "path": viewed_note_path},
        headers=_account_headers(admin_session_id),
    )
    assert note_view_response.status_code == 200, note_view_response.text

    return _SetupResult(
        oid=oid,
        personal_alias=personal_alias,
        # `note_promote`'s own tokens are deliberately excluded here: `memory_promote`
        # copies its entire content (title/body/tag/slug alike) into the shared
        # namespace, where it is meant to survive forever (ADR-0007 §3 addendum) -
        # only its `body` token is tracked at all, separately, as `promoted_token`
        # below, checked to survive there and vanish from the personal namespace.
        personal_tokens=(
            *note_edit.tokens(),
            *note_archive.tokens(),
            *note_pending.tokens(),
        ),
        promoted_token=note_promote.body,
        shared_token=shared_note.body,
        shared_namespace=shared_namespace,
        shared_edit_path=shared_path,
        viewed_note_path=viewed_note_path,
    )


async def _no_pending(conn: asyncpg.Connection) -> bool:
    return await _pending_jobs(conn) == 0


async def _assert_erasure_result(
    verify_conn: asyncpg.Connection, setup: _SetupResult, *, expected_actor: str | None
) -> None:
    for token in setup.personal_tokens:
        await _assert_token_gone_everywhere(verify_conn, token)

    personal_hits_for_promoted = await verify_conn.fetchval(
        "select count(*) from vault_notes where namespace = $1 "
        "and convert_from(content, 'UTF8') like $2",
        setup.personal_alias,
        f"%{setup.promoted_token}%",
    )
    assert personal_hits_for_promoted == 0

    shared_hits_for_promoted = await verify_conn.fetchval(
        "select count(*) from vault_notes where namespace = $1 "
        "and convert_from(content, 'UTF8') like $2",
        setup.shared_namespace,
        f"%{setup.promoted_token}%",
    )
    assert shared_hits_for_promoted == 1, "the promoted note must survive in its shared namespace"

    shared_note_row = await verify_conn.fetchrow(
        "select content from vault_notes where path = $1", setup.shared_edit_path
    )
    assert shared_note_row is not None, "the edited shared note must survive"
    assert setup.shared_token in shared_note_row["content"].decode("utf-8")

    pseudonymized = await verify_conn.fetch(
        "select author, author_oid from vault_revisions where path = $1", setup.shared_edit_path
    )
    assert pseudonymized, "the shared note's own revisions must still exist"
    for row in pseudonymized:
        assert row["author_oid"] == "erased"
        assert row["author"] == "erased"

    still_referenced = await verify_conn.fetchval(
        "select count(*) from vault_revisions where author_oid = $1", setup.oid
    )
    assert still_referenced == 0

    remaining_personal_namespace = await verify_conn.fetchval(
        "select count(*) from namespaces where kind = 'user' and external_key = $1", setup.oid
    )
    assert remaining_personal_namespace == 0

    remaining_shared_namespace = await verify_conn.fetchval(
        "select count(*) from namespaces where alias = $1", setup.shared_namespace
    )
    assert remaining_shared_namespace == 1, "the shared namespace itself must survive"

    remaining_grants = await verify_conn.fetchval(
        "select count(*) from break_glass_grants g join namespaces n on n.id = g.namespace_id "
        "where n.external_key = $1",
        setup.oid,
    )
    assert remaining_grants == 0, "the break-glass grant must cascade away with its namespace"

    remaining_user = await verify_conn.fetchval(
        "select count(*) from users where oid = $1", setup.oid
    )
    assert remaining_user == 0

    remaining_groups = await verify_conn.fetchval(
        "select count(*) from user_groups where oid = $1", setup.oid
    )
    assert remaining_groups == 0

    remaining_oauth_tokens = await verify_conn.fetchval(
        "select count(*) from oauth_tokens where user_oid = $1", setup.oid
    )
    assert remaining_oauth_tokens == 0

    remaining_oauth_codes = await verify_conn.fetchval(
        "select count(*) from oauth_auth_codes where user_oid = $1", setup.oid
    )
    assert remaining_oauth_codes == 0

    log_row = await verify_conn.fetchrow(
        "select actor, target_kind, row_counts from erasure_log where target_ids = $1",
        [setup.oid],
    )
    assert log_row is not None
    assert log_row["target_kind"] == "user"
    if expected_actor is not None:
        assert log_row["actor"] == expected_actor
    counts = json.loads(log_row["row_counts"])
    assert counts["jobs"] >= 1, "the one pending job must be deleted along with everything else"


async def test_erasure_leaves_no_personal_content_and_pseudonymizes_shared_traces(
    _acceptance_db: _AcceptanceDb,
    mock_idp_server: MockIdpServer,
) -> None:
    facade_port = free_port()
    facade_public_url = f"http://127.0.0.1:{facade_port}"
    resource = f"{facade_public_url}{_MCP_PATH}"

    admin_oid = f"oid-erasure-admin-{secrets.token_hex(4)}"

    async with (
        _run_embedding_stub() as embedding_url,
        httpx.AsyncClient(base_url=mock_idp_server.base_url, timeout=10.0) as mock_idp,
    ):
        await _register_entra_client(mock_idp, redirect_uri=f"{facade_public_url}{CALLBACK_PATH}")
        await _create_entra_user(mock_idp, admin_oid, roles=["Memory.Admin"])

        facade_env = _facade_env(
            _acceptance_db, mock_idp_server, embedding_url, public_url=facade_public_url
        )
        worker_env = _worker_env(_acceptance_db, mock_idp_server, embedding_url)

        async with (
            _run_facade_server(facade_env, port=facade_port) as facade,
            httpx.AsyncClient(
                base_url=facade.base_url, timeout=10.0, follow_redirects=False
            ) as mcp_client,
            httpx.AsyncClient(
                base_url=facade.base_url, timeout=10.0, follow_redirects=False
            ) as account_client,
        ):
            await _select_signin(mock_idp, admin_oid)
            admin_session_id = await _account_login(account_client, mock_idp)

            verify_conn = await asyncpg.connect(_acceptance_db.verify_url)
            try:
                # --- variant 1: admin erases the user through /account -----

                setup_1 = await _setup_one_user(
                    account=account_client,
                    worker_env=worker_env,
                    mock_idp=mock_idp,
                    mcp_client=mcp_client,
                    facade=facade,
                    resource=resource,
                    verify_conn=verify_conn,
                    admin_session_id=admin_session_id,
                    label="v1",
                )

                erase_response = await account_client.post(
                    ERASE_PATH,
                    data={
                        CSRF_FIELD_NAME: csrf_token(admin_session_id, CSRF_FORM_ERASE),
                        "target_kind": "user",
                        "target": setup_1.oid,
                        CONFIRM_FIELD_NAME: setup_1.oid,
                        "reason": "e2e erasure acceptance test (admin erase)",
                    },
                    headers=_account_headers(admin_session_id),
                )
                assert erase_response.status_code == 302, erase_response.text

                await _assert_erasure_result(verify_conn, setup_1, expected_actor=admin_oid)

                # --- variant 2: disabled, then the retention job (shifted clock) -----

                setup_2 = await _setup_one_user(
                    account=account_client,
                    worker_env=worker_env,
                    mock_idp=mock_idp,
                    mcp_client=mcp_client,
                    facade=facade,
                    resource=resource,
                    verify_conn=verify_conn,
                    admin_session_id=admin_session_id,
                    label="v2",
                )

                await mark_disabled(_acceptance_db.pool, setup_2.oid)

                def _shifted_clock() -> datetime:
                    return datetime.now(UTC) + timedelta(days=_RETENTION_DAYS + 1)

                await _retention_job(
                    _acceptance_db.pool, retention_days=_RETENTION_DAYS, clock=_shifted_clock
                )

                await _assert_erasure_result(verify_conn, setup_2, expected_actor=_RETENTION_ACTOR)
            finally:
                await verify_conn.close()

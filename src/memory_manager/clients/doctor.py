# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager doctor --client <name>` (#138): proves a client's own, already-written
config actually works, one step at a time - config found, URL reachable, authenticated, the
right compatibility profile, then a full write/read/edit/archive round trip in a disposable
namespace (`NAMESPACE`) nothing else ever touches.

Each step is reported independently (`Step`) rather than raising on the first failure: a
client with a stale token still gets to see "the vault would be reachable, only the token is
wrong" instead of stopping at "could not reach the server" from a connection that in fact
succeeded. `DoctorClientReport.ok` is the only thing `cli.py`'s exit code reads - `True` iff
every non-`"skip"` step passed.

No new server API: the "profile resolved" step never asks the server which profile it picked
(that stays unechoed, ADR-0010 - "Not included" below). It instead computes the profile this
request *should* have resolved to, purely from this process's own `compat.select.resolve_profile`
and the client's own `clientInfo.name`/override, then checks only what a real client could
observe for itself: the server did not reject the override with 400 (the "auth" step already
proves that), and `Client.instructions` matches what that profile's `delivery_mode` predicts.
Every "profile resolved" detail says so explicitly ("inferred, not echoed by the server") -
never an exact-text compare against this process's own `mcp.instructions.INSTRUCTIONS`, which
would just be comparing this module to itself.

OAuth is never driven interactively here (`"Nicht dabei"`: no browser flow). An HTTP entry
with no `Authorization` header that gets a 401 is given exactly one more chance to explain
itself - Protected Resource Metadata (RFC 9728), then Authorization Server metadata (RFC 8414)
- before `doctor` gives up on it: finding both skips every later step with exit 0 ("OAuth is
set up, a human still has to sign in"), finding neither fails with a hint to use a static
token instead (`connect ... --token-env`).
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, Implementation, TextContent

from memory_manager.auth.scopes import READ_SCOPE, WRITE_SCOPE
from memory_manager.clients.base import ClientAdapter, ClientEntry
from memory_manager.clients.jsonconfig import ClientConfigError
from memory_manager.compat.profiles import UnknownProfile
from memory_manager.compat.select import resolve_profile
from memory_manager.config import StorageConfigError, storage_backend_from_env

__all__ = ["NAMESPACE", "DoctorClientReport", "Step", "render_text", "run_client_doctor", "to_json"]

#: The one namespace every `doctor --client` write/read/edit/archive round trip touches -
#: never any other (CLAUDE.md: "writes only into namespace `mm-doctor`").
NAMESPACE = "mm-doctor"

_HTTP_TIMEOUT = 5.0
_IMPLEMENTATION_VERSION = "0.0.0-doctor"

_MEMORY_STEP_NAMES: tuple[str, ...] = (
    "memory_index",
    "memory_write",
    "memory_read",
    "memory_edit",
    "memory_archive",
)

StepStatus = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class Step:
    """One checked (or deliberately skipped) fact about a client's connection."""

    name: str
    status: StepStatus
    detail: str
    hint: str | None = None


@dataclass(frozen=True)
class DoctorClientReport:
    """Every `Step` `run_client_doctor` produced, in the order it checked them."""

    client: str
    steps: tuple[Step, ...]

    @property
    def ok(self) -> bool:
        """`True` iff no step other than a skipped one failed."""
        return all(step.status != "fail" for step in self.steps)


async def run_client_doctor(
    adapter: ClientAdapter,
    *,
    home: Path,
    project_dir: Path,
    env: Mapping[str, str],
    read_only: bool,
) -> DoctorClientReport:
    """Run every step for `adapter`'s configured memory-manager server.

    `home`/`project_dir`/`env` are exactly `clients.connect`'s own inputs - the same config
    `connect <client>` would have written is what this reads back. `read_only=True` skips the
    write/read/edit/archive round trip, leaving only `memory_index` as proof of read access.
    """
    steps: list[Step] = []
    entry = _find_entry(adapter, home=home, project_dir=project_dir, env=env, steps=steps)
    if entry is None:
        return DoctorClientReport(client=adapter.name, steps=tuple(steps))

    if entry.transport == "stdio":
        await _check_stdio(adapter, entry, read_only=read_only, steps=steps)
    else:
        await _check_http(adapter, entry, read_only=read_only, steps=steps)

    return DoctorClientReport(client=adapter.name, steps=tuple(steps))


def render_text(report: DoctorClientReport) -> str:
    """Human-readable rendering of `report` - never contains a token value (every detail/hint
    this module builds names an env var, never a secret's own value)."""
    marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    lines = [f"memory-manager doctor --client {report.client}"]
    for step in report.steps:
        lines.append(f"{marker[step.status]}: {step.name}: {step.detail}")
        if step.hint:
            lines.append(f"  hint: {step.hint}")
    lines.append("ok" if report.ok else "failed")
    return "\n".join(lines)


def to_json(report: DoctorClientReport) -> dict[str, Any]:
    """`report` as a JSON-serializable mapping - the same no-token-value guarantee as
    `render_text` above."""
    return {
        "client": report.client,
        "ok": report.ok,
        "steps": [
            {"name": step.name, "status": step.status, "detail": step.detail, "hint": step.hint}
            for step in report.steps
        ],
    }


# --- Step 1: find the entry a running client would actually use ------------------------


def _find_entry(
    adapter: ClientAdapter,
    *,
    home: Path,
    project_dir: Path,
    env: Mapping[str, str],
    steps: list[Step],
) -> ClientEntry | None:
    if not adapter.scopes:
        steps.append(
            Step(
                "config found",
                "fail",
                f"{adapter.name} has no local config file to read",
                hint=f"follow the manual setup steps in docs/clients/{adapter.name}.md and "
                "verify the connector directly in that client's own UI",
            )
        )
        return None

    tried: list[str] = []
    for scope in adapter.scopes:
        path = adapter.locate_config(scope=scope, home=home, project_dir=project_dir, env=env)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            tried.append(f"{scope}: {path} (not found)")
            continue
        except OSError as exc:
            steps.append(Step("config found", "fail", f"could not read {path}: {exc}"))
            return None

        try:
            entry = adapter.read_entry(text, scope=scope, project_dir=project_dir, env=env)
        except ClientConfigError as exc:
            steps.append(
                Step("config found", "fail", f"{path} is not a config `doctor` can read: {exc}")
            )
            return None

        if entry is None:
            tried.append(f"{scope}: {path} (no memory-manager entry)")
            continue

        detail = f"found in {path} ({scope} scope)"
        if entry.unresolved_vars:
            detail += f"; unresolved variable(s): {', '.join(entry.unresolved_vars)}"
        steps.append(Step("config found", "pass", detail))
        return entry

    steps.append(
        Step(
            "config found",
            "fail",
            f"no memory-manager entry found (tried: {'; '.join(tried)})",
            hint=f"run `memory-manager connect {adapter.name} --url <url>` first",
        )
    )
    return None


# --- Step 2+: stdio -----------------------------------------------------------------------


async def _check_stdio(
    adapter: ClientAdapter, entry: ClientEntry, *, read_only: bool, steps: list[Step]
) -> None:
    steps.append(Step("URL reachable", "skip", "stdio transport has no URL to reach"))
    steps.append(Step("auth", "skip", "stdio transport has no HTTP authentication to check"))

    if entry.command is None:
        steps.append(Step("profile resolved", "fail", "the stdio entry has no `command` set"))
        _skip_memory_steps(steps, "no working connection")
        return

    params = StdioServerParameters(
        command=entry.command, args=list(entry.args), env={**os.environ, **entry.env}
    )
    client_info = _client_info(adapter)
    try:
        async with Client(params, mode="auto", client_info=client_info) as client:
            _check_profile(adapter, client, override=None, steps=steps)
            await _run_memory_steps(client, read_only=read_only, steps=steps)
    except OSError as exc:
        steps.append(Step("profile resolved", "fail", f"could not spawn {entry.command}: {exc}"))
        _skip_memory_steps(steps, "no working connection")
    except Exception as exc:
        # Any handshake failure (a bad `StdioServerParameters`, a subprocess that exits
        # early, ...) is a doctor fail, not a crash - the step this failed on is the one
        # the caller already knows was about to run.
        steps.append(Step("profile resolved", "fail", f"could not connect over stdio: {exc}"))
        _skip_memory_steps(steps, "no working connection")


# --- Step 2+: HTTP -------------------------------------------------------------------------


async def _check_http(
    adapter: ClientAdapter, entry: ClientEntry, *, read_only: bool, steps: list[Step]
) -> None:
    if entry.url is None:
        steps.append(Step("URL reachable", "fail", "the http entry has no `url` configured"))
        steps.append(Step("auth", "skip", "no URL to check"))
        _skip_profile_and_memory(steps, "no URL configured")
        return

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http_client:
        reachable_step = await _check_healthz(http_client, entry.url)
        steps.append(reachable_step)
        if reachable_step.status != "pass":
            steps.append(Step("auth", "skip", "the server was not reachable at /healthz"))
            _skip_profile_and_memory(steps, "the server was not reachable")
            return

        auth_step = await _check_auth(http_client, entry, client_name=adapter.name)
        steps.append(auth_step)
        if auth_step.status != "pass":
            reason = (
                "OAuth login required" if auth_step.status == "skip" else "authentication failed"
            )
            _skip_profile_and_memory(steps, reason)
            return

    override = _profile_override(entry)
    http_client_for_mcp = httpx2.AsyncClient(headers=dict(entry.headers))
    transport = streamable_http_client(entry.url, http_client=http_client_for_mcp)
    client_info = _client_info(adapter)
    try:
        async with Client(transport, mode="auto", client_info=client_info) as client:
            _check_profile(adapter, client, override=override, steps=steps)
            await _run_memory_steps(client, read_only=read_only, steps=steps)
    except Exception as exc:
        # Same reasoning as `_check_stdio`'s own catch-all above.
        steps.append(Step("profile resolved", "fail", f"could not open an MCP session: {exc}"))
        _skip_memory_steps(steps, "no working connection")


def _client_info(adapter: ClientAdapter) -> Implementation | None:
    if adapter.client_info_name is None:
        return None
    return Implementation(name=adapter.client_info_name, version=_IMPLEMENTATION_VERSION)


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _check_healthz(http_client: httpx.AsyncClient, mcp_url: str) -> Step:
    healthz_url = f"{_origin(mcp_url)}/healthz"
    hint = (
        "if the server sits behind a reverse-proxy path prefix, check that /healthz is "
        "reachable at that same prefix too"
    )
    try:
        response = await http_client.get(healthz_url)
    except httpx.TransportError as exc:
        return Step("URL reachable", "fail", f"could not reach {healthz_url}: {exc}", hint)
    if response.status_code != 200:
        return Step("URL reachable", "fail", f"{healthz_url} returned {response.status_code}", hint)
    return Step("URL reachable", "pass", f"{healthz_url} is reachable")


def _profile_override(entry: ClientEntry) -> str | None:
    if entry.url is not None:
        values = parse_qs(urlsplit(entry.url).query).get("profile")
        if values:
            return values[0]
    for key, value in entry.headers.items():
        if key.lower() == "mm-client-profile":
            return value
    return None


async def _check_auth(
    http_client: httpx.AsyncClient, entry: ClientEntry, *, client_name: str
) -> Step:
    if entry.url is None:  # pragma: no cover - `_check_http` already returned on this
        return Step("auth", "fail", "no url configured")
    has_authorization = "Authorization" in entry.headers
    headers = {
        **entry.headers,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        response = await http_client.post(
            entry.url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
        )
    except httpx.TransportError as exc:
        return Step("auth", "fail", f"could not reach {entry.url}: {exc}")

    if response.status_code == 400:
        return Step(
            "auth",
            "fail",
            "the server rejected the configured profile override with 400",
            hint="remove the invalid `?profile=`/`MM-Client-Profile` override, or correct it "
            "to a profile `memory-manager compat lint` recognizes",
        )

    if response.status_code == 401:
        if has_authorization:
            hint = (
                f"check ${entry.token_env}: was it revoked, or does "
                f"`connect {client_name} --token-env` need to be rerun with a fresh token?"
                if entry.token_env
                else f"the configured Authorization header was rejected; reconnect with "
                f"`connect {client_name} --token-env` using a fresh token"
            )
            return Step("auth", "fail", "the server rejected the configured token (401)", hint)

        if await _discover_oauth(http_client, response, entry.url) is not None:
            return Step("auth", "skip", "OAuth configured, interactive login required")
        return Step(
            "auth",
            "fail",
            "the server requires authentication, no token is configured and no OAuth "
            "authorization server could be discovered",
            hint=f"run `memory-manager connect {client_name} --token-env` with a static token",
        )

    if response.status_code == 200:
        detail = (
            "the configured token was accepted"
            if has_authorization
            else ("the server requires no authentication")
        )
        return Step("auth", "pass", detail)

    return Step("auth", "fail", f"unexpected status {response.status_code} from {entry.url}")


_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]+)"')


def _resource_metadata_url(response: httpx.Response, mcp_url: str) -> str:
    match = _RESOURCE_METADATA_RE.search(response.headers.get("WWW-Authenticate", ""))
    if match is not None:
        return match.group(1)
    parsed = urlsplit(mcp_url)
    suffix = f"/{parsed.path.strip('/')}" if parsed.path.strip("/") else ""
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{suffix}"


async def _discover_oauth(
    http_client: httpx.AsyncClient, response: httpx.Response, mcp_url: str
) -> dict[str, Any] | None:
    """`None` unless both Protected Resource Metadata (RFC 9728) and the Authorization Server
    metadata (RFC 8414) it names are reachable - "PRM missing" (the module docstring's wording)
    covers every other outcome, including a 404 for a static-token-only deployment that has no
    embedded authorization server at all (`auth.prm.serve_protected_resource_metadata`)."""
    prm_url = _resource_metadata_url(response, mcp_url)
    try:
        prm_response = await http_client.get(prm_url)
    except httpx.TransportError:
        return None
    if prm_response.status_code != 200:
        return None
    try:
        data = prm_response.json()
    except ValueError:
        return None
    servers = data.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        return None

    as_url = f"{str(servers[0]).rstrip('/')}/.well-known/oauth-authorization-server"
    try:
        as_response = await http_client.get(as_url)
    except httpx.TransportError:
        return None
    if as_response.status_code != 200:
        return None
    return dict(data)


# --- Step: profile resolved (locally inferred, never echoed by the server) ----------------


def _check_profile(
    adapter: ClientAdapter, client: Client, *, override: str | None, steps: list[Step]
) -> None:
    try:
        profile = resolve_profile(adapter.client_info_name, override=override)
    except UnknownProfile as exc:
        steps.append(Step("profile resolved", "fail", str(exc)))
        return

    if profile.delivery_mode == "descriptions":
        steps.append(
            Step(
                "profile resolved",
                "pass",
                f"profile {profile.name!r} (descriptions mode): usage rules are expected to "
                "be folded into tool descriptions rather than `instructions` (inferred, not "
                "echoed by the server)",
            )
        )
        return

    instructions = client.instructions
    if not instructions:
        steps.append(
            Step(
                "profile resolved",
                "fail",
                f"profile {profile.name!r} ({profile.delivery_mode}) expects `instructions` "
                "at initialize, but none came back",
            )
        )
        return
    max_chars = profile.max_instructions_chars
    if max_chars is not None and len(instructions) > max_chars:
        steps.append(
            Step(
                "profile resolved",
                "fail",
                f"`instructions` is {len(instructions)} chars, over profile {profile.name!r}'s "
                f"{profile.max_instructions_chars}-char limit",
            )
        )
        return
    steps.append(
        Step(
            "profile resolved",
            "pass",
            f"profile {profile.name!r} ({profile.delivery_mode}): `instructions` present, "
            f"{len(instructions)} chars (inferred, not echoed by the server)",
        )
    )


# --- Steps: the write/read/edit/archive round trip, in `NAMESPACE` only -------------------


def _note_content(slug: str) -> str:
    return (
        "---\n"
        "title: memory-manager doctor check\n"
        "description: Written by `memory-manager doctor --client` to verify the write path.\n"
        "type: fact\n"
        "---\n"
        f"Doctor run {slug}.\n"
    )


def _result_text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def _write_hint(text: str) -> str | None:
    if f"token lacks {WRITE_SCOPE}" in text:
        return (
            f"the configured token has no {WRITE_SCOPE} scope; create one with "
            f"`memory-manager token create --scopes {READ_SCOPE} {WRITE_SCOPE} ...`"
        )
    if f"may not write to namespace {NAMESPACE!r}" in text:
        try:
            backend = storage_backend_from_env(dict(os.environ))
        except StorageConfigError:
            backend = "git"
        if backend == "postgres":
            return (
                f"namespace {NAMESPACE!r} must exist and be writable by the token (ADR-0008) "
                "- register it, or use a token whose namespaces already include it"
            )
        return (
            f"the configured token's namespace list does not include {NAMESPACE!r}; recreate "
            f"it with `memory-manager token create --namespaces {NAMESPACE} ...`, or omit "
            "--namespaces for every namespace"
        )
    return None


def _skip_memory_steps(steps: list[Step], reason: str) -> None:
    for name in _MEMORY_STEP_NAMES:
        steps.append(Step(name, "skip", f"skipped: {reason}"))


def _skip_profile_and_memory(steps: list[Step], reason: str) -> None:
    steps.append(Step("profile resolved", "skip", f"skipped: {reason}"))
    _skip_memory_steps(steps, reason)


async def _run_memory_steps(client: Client, *, read_only: bool, steps: list[Step]) -> None:
    index_result = await client.call_tool("memory_index", {"namespace": NAMESPACE})
    if index_result.is_error:
        steps.append(Step("memory_index", "fail", _result_text(index_result)))
    else:
        steps.append(Step("memory_index", "pass", f"listed namespace {NAMESPACE!r}"))

    if read_only:
        for name in ("memory_write", "memory_read", "memory_edit", "memory_archive"):
            steps.append(Step(name, "skip", "skipped: --read-only"))
        return

    slug = f"doctor-{secrets.token_hex(4)}"
    path = f"{NAMESPACE}/fact/{slug}.md"
    content = _note_content(slug)

    write_result = await client.call_tool(
        "memory_write", {"path": path, "content": content, "if_version": "new"}
    )
    if write_result.is_error:
        text = _result_text(write_result)
        steps.append(Step("memory_write", "fail", text, _write_hint(text)))
        for name in ("memory_read", "memory_edit", "memory_archive"):
            steps.append(Step(name, "skip", "skipped: no note was written to check this against"))
        return

    write_payload = write_result.structured_content or {}
    version = str(write_payload.get("version", ""))
    steps.append(Step("memory_write", "pass", f"wrote {path}"))

    read_result = await client.call_tool("memory_read", {"items": [path]})
    read_items = (read_result.structured_content or {}).get("result", [])
    read_item = read_items[0] if isinstance(read_items, list) and read_items else None
    if (
        read_result.is_error
        or not isinstance(read_item, dict)
        or read_item.get("error") is not None
    ):
        detail = _result_text(read_result) or f"{path} could not be read back"
        steps.append(Step("memory_read", "fail", detail))
    else:
        steps.append(Step("memory_read", "pass", f"read {path} back"))

    edit_result = await client.call_tool(
        "memory_edit",
        {
            "path": path,
            "old_str": f"Doctor run {slug}.",
            "new_str": f"Doctor run {slug} (edited).",
            "if_version": version,
        },
    )
    if edit_result.is_error:
        text = _result_text(edit_result)
        steps.append(Step("memory_edit", "fail", text, _write_hint(text)))
    else:
        edit_payload = edit_result.structured_content or {}
        version = str(edit_payload.get("version", version))
        steps.append(Step("memory_edit", "pass", f"edited {path}"))

    archive_result = await client.call_tool("memory_archive", {"path": path, "if_version": version})
    if archive_result.is_error:
        text = _result_text(archive_result)
        hint = _write_hint(text) or (
            f"{path} was left behind in the vault; archive or ignore it manually - it is "
            "harmless cleanup debris in the test namespace"
        )
        steps.append(Step("memory_archive", "fail", text, hint))
    else:
        steps.append(Step("memory_archive", "pass", f"archived {path}"))

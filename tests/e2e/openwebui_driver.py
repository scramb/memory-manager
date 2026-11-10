# SPDX-License-Identifier: AGPL-3.0-only
"""Headless driver for Open WebUI's `oauth_2.1` (DCR) tool-server connection
(#142, WP-42), against the pinned stack in `deploy/openwebui/docker-compose.yml`.

Named `openwebui_driver.py`, not `test_*.py`, on purpose: it drives a *live*,
already-running stack (`make openwebui-up`) rather than starting its own
fixtures, so pytest must never collect it (CLAUDE.md "no tests that only
mirror structure" aside - this one would also just hang or fail on a
developer machine with no stack up). Run it as a script instead:

    python3 tests/e2e/openwebui_driver.py connect \\
        --admin-password-file deploy/openwebui/.admin-password \\
        --server-id memmgr

    python3 tests/e2e/openwebui_driver.py chat \\
        --admin-password-file deploy/openwebui/.admin-password \\
        --server-id memmgr --model-id scripted-model \\
        --message "search my notes"

It reuses one cookie jar across both origins (`:3000` Open WebUI, `:18081`
memory-manager's embedded AS) deliberately - a real browser driving this
same flow would do exactly that, since both answer on "localhost" and
cookies are scoped by domain, not port (RFC 6265).

Every step below mirrors the live shapes `docs/research/clients/openwebui.md`
"Spike results" records; see there for the evidence this module gathered.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Open WebUI's own `type:<id>` client-id shape (`utils/oauth.py`,
#: `ensure_client_from_config`) - fixed, not a secret.
_OAUTH_CLIENT_TYPE = "mcp"

_DEFAULT_SERVER_ID = "memmgr"
_DEFAULT_WEBUI_URL = "http://localhost:3000"
_DEFAULT_RESOURCE_SERVER_URL = "http://localhost:18081"

#: Every user and group may read an MCP tool-server connection it is attached to
#: (`utils/access_control.has_access`'s "public read" shape, v0.11.4) - set on the
#: *first* `configure_tool_server` call (#314) because every later
#: `POST /api/v1/configs/tool_servers` re-creates the connection (and its OAuth
#: client) from scratch, there is no "patch just this field" endpoint
#: (`routers/configs.py.set_tool_servers_config`).
_PUBLIC_READ_ACCESS_GRANTS: list[dict[str, str]] = [
    {"principal_type": "user", "principal_id": "*", "permission": "read"}
]

#: `templates.oidc_interstitial_page` ("Continue to sign in") - only the
#: continue-link's `href`, never anything from the upstream IdP's own page.
_INTERSTITIAL_CONTINUE_RE = re.compile(r'<a class="button" href="([^"]+)">')


def _output_text(output: object) -> str:
    """`get_output_text` (`utils/misc.py`, v0.11.4), reimplemented here since
    this module cannot import Open WebUI's own backend package: every
    `output` item of type `"message"`, its `content` parts' own `"text"`
    fields concatenated, non-empty entries joined with `"\\n"` - the
    `output`-side counterpart to a plain `content` string (#150,
    `run_scripted_text_chat`'s own read-back)."""
    if not isinstance(output, list):
        return ""
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        text = "".join(
            str(part.get("text"))
            for part in parts
            if isinstance(part, dict) and part.get("text") is not None
        )
        if text and not text.isspace():
            texts.append(text)
    return "\n".join(texts)


class OpenWebUiDriverError(RuntimeError):
    """Any step of the driven flow did not end up where it should have -
    carries the response body (never a secret: this module never prints a
    token, client secret, authorization code or password) for diagnosis."""


@dataclass(frozen=True)
class ConnectResult:
    """What `connect_mcp_server` observed, for the research note and for the
    CLI's own exit code - never the token or client secret themselves."""

    oauth_client_registered: bool
    tool_server_configured: bool
    authorize_followed_to_webui: bool
    final_url: str
    final_status_code: int


@dataclass(frozen=True)
class ChatToolCallResult:
    """What `OpenWebUiDriver.run_scripted_tool_chat` (#314) read back from
    `GET /api/v1/chats/{chat_id}` - the chat/message ids so a caller can look
    the conversation up again, and every `function_call_output` item found on
    the assistant message (`output` list, `utils/middleware.py`, v0.11.4):
    `{"type": "function_call_output", "call_id": ..., "output": [{"type":
    "input_text", "text": ...}, ...], "status": "completed"|"failed"}`."""

    chat_id: str
    assistant_message_id: str
    function_call_outputs: list[dict[str, Any]] = field(default_factory=list)

    def output_text(self) -> str:
        """All `input_text` parts of every `function_call_output`, joined -
        what a caller checks for "Tool … not found" (#314's own Definition
        of Done) without having to walk the nested shape itself."""
        return "\n".join(
            str(part.get("text", ""))
            for item in self.function_call_outputs
            for part in item.get("output", [])
            if part.get("type") == "input_text"
        )


@dataclass(frozen=True)
class TextChatResult:
    """What `OpenWebUiDriver.run_text_chat` (#151, WP-44's own small-model benchmark)
    read back: the answer text, plus the persisted assistant message's own `usage`
    dict verbatim, when present (`run_text_chat`'s own docstring)."""

    content: str
    usage: dict[str, object] | None


class OpenWebUiDriver:
    """Drives one Open WebUI instance through the oauth_2.1 (DCR) dance
    against one memory-manager instance, as the *first* (hence admin) user."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        webui_url: str = _DEFAULT_WEBUI_URL,
        resource_server_url: str = _DEFAULT_RESOURCE_SERVER_URL,
    ) -> None:
        self._client = client
        self._webui_url = webui_url.rstrip("/")
        self._resource_server_url = resource_server_url.rstrip("/")
        self._oauth_client_info: str = ""

    async def signup_admin(self, *, name: str, email: str, password: str) -> None:
        """`POST /api/v1/auths/signup` - the first signup on a fresh
        `openwebui-data` volume becomes the admin; a non-first signup (a
        stack kept from an earlier run) raises on the 400 Open WebUI
        answers with instead ("email taken" or signup disabled)."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/auths/signup",
            json={"name": name, "email": email, "password": password},
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(f"signup failed: {response.status_code} {response.text}")

    async def register_oauth_client(self, *, server_id: str) -> None:
        """`POST /api/v1/configs/oauth/clients/register?type=mcp` - runs DCR
        against memory-manager's AS and stores the (encrypted) result under
        Open WebUI's own `mcp:<server_id>` key. Raises on anything but 200,
        which is this spike's live answer to "does our DCR endpoint accept
        Open WebUI's registration request" (ADR-0011 §"Consequences")."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/configs/oauth/clients/register",
            params={"type": _OAUTH_CLIENT_TYPE},
            json={
                "url": f"{self._resource_server_url}/mcp",
                "client_id": server_id,
            },
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"oauth client registration failed: {response.status_code} {response.text}"
            )
        body = response.json()
        if not body.get("status") or not body.get("oauth_client_info"):
            raise OpenWebUiDriverError(f"oauth client registration returned no client info: {body}")
        self._oauth_client_info = body["oauth_client_info"]

    async def configure_tool_server(self, *, server_id: str) -> None:
        """`POST /api/v1/configs/tool_servers` - the admin step that adds the
        MCP connection, `auth_type=oauth_2.1`, carrying the encrypted blob
        `register_oauth_client` just produced in `info.oauth_client_info`
        (`utils/oauth.py.ensure_client_from_config`'s own read path).

        `url` carries the full `/mcp` path and `path` is sent only because
        `ToolServerConnection.path` is a required field - Open WebUI's own
        `connect_mcp_server` (`utils/middleware.py`, v0.11.4) uses `url`
        *verbatim* as the Streamable HTTP endpoint and never appends `path`
        (#314's load-bearing fix: `url=<resource server>` alone 404s on
        every request and the chat silently ends with no tools). `config`
        carries `_PUBLIC_READ_ACCESS_GRANTS` already on this first call,
        since every later `POST` here re-creates the connection (and its
        OAuth client) from scratch - there is no "add a grant" endpoint."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/configs/tool_servers",
            json={
                "TOOL_SERVER_CONNECTIONS": [
                    {
                        "url": f"{self._resource_server_url}/mcp",
                        "path": "mcp",
                        "type": "mcp",
                        "auth_type": "oauth_2.1",
                        "forward_cookies": False,
                        "headers": None,
                        "key": None,
                        "config": {"enable": True, "access_grants": _PUBLIC_READ_ACCESS_GRANTS},
                        "info": {
                            "id": server_id,
                            "oauth_client_info": self._oauth_client_info,
                        },
                    }
                ]
            },
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"tool server configuration failed: {response.status_code} {response.text}"
            )

    async def _complete_login_page(
        self, response: httpx.Response, *, password: str
    ) -> httpx.Response:
        """Finishes whatever memory-manager's `/login` shows, detected from the
        page's own markup rather than a configured `LOGIN_MODE` (#314, so this
        one hook keeps working once #313 switches to `oidc`):

        - the password form (`templates.login_password_page`, the only page
          `LOGIN_MODE=password` - every live run this task's own Definition of
          Done needs - ever shows): submits it with `password`.
        - the OIDC interstitial (`templates.oidc_interstitial_page`, "Continue
          to sign in"): follows its `confirm=1` link onward and returns that
          response unfinished - driving the upstream IdP's own page after that
          is #313's job, not this generic hook's.

        Raises if the page is neither (an unexpected error page, for
        instance) and never logs or returns the password itself."""
        page = response
        while 'name="password"' not in page.text:
            match = _INTERSTITIAL_CONTINUE_RE.search(page.text)
            if match is None:
                raise OpenWebUiDriverError(
                    f"'/login' page is neither a password form nor an interstitial: {page.url}"
                )
            continue_url = httpx.URL(str(page.url)).join(html.unescape(match.group(1)))
            page = await self._client.get(continue_url)
            if "/login" not in str(page.url):
                # Left memory-manager's own /login (e.g. redirected onward to
                # an upstream IdP) - whatever comes next is a different
                # authenticator's own flow (#313), not this hook's.
                return page

        pending = page.url.params.get("pending", "")
        if not pending:
            raise OpenWebUiDriverError(f"no 'pending' id in the /login URL: {page.url}")
        login_base = str(page.url).split("?", 1)[0]
        return await self._client.post(login_base, data={"pending": pending, "password": password})

    async def run_oauth_flow(self, *, server_id: str, password: str) -> ConnectResult:
        """Drives the per-user consent: `GET /oauth/clients/mcp:<id>/authorize`
        (Open WebUI) -> memory-manager's `/authorize` -> `/login?pending=...`
        -> `_complete_login_page` (password form or OIDC interstitial) -> back
        through memory-manager's `/authorize` completion -> Open WebUI's own
        `/oauth/clients/mcp:<id>/callback`. One `httpx.AsyncClient` with a
        shared cookie jar and `follow_redirects=True` carries every hop
        except the password form itself, exactly like one browser tab
        would. Success is a final response with no `?error=` in its URL,
        the live check ADR-0011's "Consequences" asks WP-42 to make
        (STOP-and-Gate condition if this comes back `invalid_redirect_uri`
        or `invalid_client` instead)."""
        client_id = f"{_OAUTH_CLIENT_TYPE}:{server_id}"
        authorize_response = await self._client.get(
            f"{self._webui_url}/oauth/clients/{client_id}/authorize"
        )
        login_url = str(authorize_response.url)
        if "/login" not in login_url:
            raise OpenWebUiDriverError(
                f"expected to land on memory-manager's /login, got {login_url} "
                f"({authorize_response.status_code})"
            )

        submit_response = await self._complete_login_page(authorize_response, password=password)
        final_url = str(submit_response.url)
        if "error" in httpx.URL(final_url).params:
            raise OpenWebUiDriverError(
                f"login/authorize completion carries an error: {final_url} "
                f"({submit_response.status_code}) {submit_response.text[:500]}"
            )

        return ConnectResult(
            oauth_client_registered=True,
            tool_server_configured=True,
            authorize_followed_to_webui=final_url.startswith(self._webui_url),
            final_url=final_url,
            final_status_code=submit_response.status_code,
        )

    async def connect(
        self,
        *,
        admin_name: str,
        admin_email: str,
        admin_password: str,
        server_id: str,
    ) -> ConnectResult:
        """The full, first-user path: signup -> DCR -> configure -> consent."""
        await self.signup_admin(name=admin_name, email=admin_email, password=admin_password)
        await self.register_oauth_client(server_id=server_id)
        await self.configure_tool_server(server_id=server_id)
        return await self.run_oauth_flow(server_id=server_id, password=admin_password)

    async def add_user(self, *, name: str, email: str, password: str, role: str = "user") -> None:
        """`POST /api/v1/auths/add` (admin-only, `role="user"` by default) -
        creates a second local account next to the admin `signup_admin`
        made, so a caller can later sign that user in on their *own*
        `httpx.AsyncClient`/cookie jar (see `sign_in`) instead of sharing the
        admin's session - #144's own job, not exercised by this task's live
        run (one admin user is enough for #314's Definition of Done), but
        the seam this method and `sign_in` give it."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/auths/add",
            json={"name": name, "email": email, "password": password, "role": role},
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(f"add user failed: {response.status_code} {response.text}")

    async def sign_in(self, *, email: str, password: str) -> None:
        """`POST /api/v1/auths/signin` on *this* driver's own `httpx.AsyncClient` -
        sets the `token` session cookie (`create_session_response`,
        `routers/auths.py`, v0.11.4) a chat call needs. Run this on a driver
        built with its own client per user (one cookie jar each - a user
        added through `add_user` must never share the admin's); `connect`'s
        own `signup_admin` already leaves the admin signed in, so a fresh
        `connect` run does not need this call for the admin itself."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/auths/signin",
            json={"email": email, "password": password},
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(f"signin failed: {response.status_code} {response.text}")

    async def install_filter(
        self, *, function_id: str, name: str, content: str, description: str
    ) -> None:
        """`POST /api/v1/functions/create` (#150, `routers/functions.py:200-262`,
        admin-only): installs `content` (a single file's source text, ADR-0011)
        as Function `function_id` - which must satisfy `str.isidentifier()`
        and is lowercased server-side (`routers/functions.py:207-213`), so
        callers should already pass a lowercase identifier. A brand-new
        function starts both inactive and non-global
        (`models/functions.py`'s own `FunctionModel.is_active`/`is_global`
        default `False`) - `activate_filter` below flips both."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/functions/create",
            json={
                "id": function_id,
                "name": name,
                "content": content,
                "meta": {"description": description, "manifest": {}},
            },
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"filter installation failed: {response.status_code} {response.text}"
            )

    async def activate_filter(self, *, function_id: str) -> None:
        """`POST /id/{id}/toggle` then `POST /id/{id}/toggle/global`
        (admin-only) - both endpoints *toggle* the current value
        (`routers/functions.py:288-371`), so each is called exactly once
        here, right after `install_filter`'s own fresh (inactive,
        non-global) function. Raises unless both responses come back with
        `is_active`/`is_global` `True`, the one live check that these two
        calls actually activated the filter rather than toggling some
        unrelated, pre-existing state."""
        active_response = await self._client.post(
            f"{self._webui_url}/api/v1/functions/id/{function_id}/toggle"
        )
        if active_response.status_code != httpx.codes.OK or not active_response.json().get(
            "is_active"
        ):
            raise OpenWebUiDriverError(
                f"activating the filter failed: {active_response.status_code} "
                f"{active_response.text}"
            )

        global_response = await self._client.post(
            f"{self._webui_url}/api/v1/functions/id/{function_id}/toggle/global"
        )
        if global_response.status_code != httpx.codes.OK or not global_response.json().get(
            "is_global"
        ):
            raise OpenWebUiDriverError(
                f"making the filter global failed: {global_response.status_code} "
                f"{global_response.text}"
            )

    async def update_filter_valves(self, *, function_id: str, valves: dict[str, object]) -> None:
        """`POST /id/{id}/valves/update` (admin-only, any user) - the
        `Filter.Valves` fields set for every chat (`routers/functions.py:
        517-561`)."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/functions/id/{function_id}/valves/update", json=valves
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"updating the filter's valves failed: {response.status_code} {response.text}"
            )

    async def update_filter_user_valves(
        self, *, function_id: str, valves: dict[str, object]
    ) -> None:
        """`POST /id/{id}/valves/user/update`, run on *this* driver's own
        session (a verified user, never admin-only) - the `Filter.
        UserValves` fields for the signed-in user alone. Only works once
        the filter is active (`routers/functions.py:618-668`: a 400
        otherwise), so this must run after `activate_filter`."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/functions/id/{function_id}/valves/user/update", json=valves
        )
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"updating the filter's user valves failed: {response.status_code} {response.text}"
            )

    async def disable_builtin_memories(self) -> None:
        """`GET` then `POST /api/v1/auths/admin/config` (admin-only,
        `routers/auths.py:1204-1296`): reads the full `AdminConfig` back and
        posts it with only `ENABLE_MEMORIES` flipped to `False` - the
        endpoint has no "patch just this field" shape, every `POST` expects
        the whole object (#150, `docs/research/clients/openwebui.md`
        "Built-in memory")."""
        get_response = await self._client.get(f"{self._webui_url}/api/v1/auths/admin/config")
        if get_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"reading the admin config failed: {get_response.status_code} {get_response.text}"
            )
        config = get_response.json()
        config["ENABLE_MEMORIES"] = False
        post_response = await self._client.post(
            f"{self._webui_url}/api/v1/auths/admin/config", json=config
        )
        if post_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"disabling built-in memories failed: {post_response.status_code} "
                f"{post_response.text}"
            )

    async def run_scripted_tool_chat(
        self, *, model_id: str, server_id: str, message: str
    ) -> ChatToolCallResult:
        """Drives one user turn through Open WebUI's native MCP tool loop
        (#314): `POST /api/v1/chats/new` for a real `chat_id`, then `POST
        /api/chat/completions` with `stream: true`, `tool_ids:
        ["server:mcp:<server_id>"]` (`utils/middleware.py`'s own
        `server:mcp:` prefix, v0.11.4), this `chat_id` and a fresh assistant
        `id` - deliberately *without* `session_id`, so `main.py`'s
        `chat_completion` takes its synchronous branch (the `if
        metadata.get('session_id') and metadata.get('chat_id')` fan-out is
        for the WebSocket/multi-model case only) and the whole tool loop
        (`get_event_emitter_and_caller`/`streaming_chat_response_handler`)
        runs - and is persisted to the chat - before the HTTP response
        returns. That response is `200 null` by design
        (`streaming_chat_response_handler`'s own `response_handler` has no
        `return` statement), so the actual result is read back with `GET
        /api/v1/chats/{chat_id}` -> `chat.history.messages[<assistant
        id>].output`'s `function_call_output` items, this method's return
        value (`ChatToolCallResult`)."""
        new_chat_response = await self._client.post(
            f"{self._webui_url}/api/v1/chats/new",
            json={
                "chat": {
                    "models": [model_id],
                    "history": {"messages": {}, "currentId": None},
                    "messages": [],
                    "title": "New Chat",
                    "timestamp": int(time.time() * 1000),
                }
            },
        )
        if new_chat_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"chats/new failed: {new_chat_response.status_code} {new_chat_response.text}"
            )
        chat_id = new_chat_response.json().get("id", "")
        if not chat_id:
            raise OpenWebUiDriverError(f"chats/new returned no chat id: {new_chat_response.json()}")

        assistant_message_id = str(uuid.uuid4())
        completion_response = await self._client.post(
            f"{self._webui_url}/api/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": message}],
                "stream": True,
                "tool_ids": [f"server:mcp:{server_id}"],
                "chat_id": chat_id,
                "id": assistant_message_id,
            },
        )
        if completion_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"chat completion failed: {completion_response.status_code} "
                f"{completion_response.text}"
            )

        chat_detail_response = await self._client.get(f"{self._webui_url}/api/v1/chats/{chat_id}")
        if chat_detail_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"reading the chat back failed: {chat_detail_response.status_code} "
                f"{chat_detail_response.text}"
            )
        chat_detail = chat_detail_response.json() or {}
        messages = (chat_detail.get("chat") or {}).get("history", {}).get("messages", {})
        assistant_message = messages.get(assistant_message_id) or {}
        output = assistant_message.get("output") or []
        function_call_outputs = [
            item for item in output if item.get("type") == "function_call_output"
        ]

        return ChatToolCallResult(
            chat_id=chat_id,
            assistant_message_id=assistant_message_id,
            function_call_outputs=function_call_outputs,
        )

    async def run_scripted_text_chat(self, *, model_id: str, message: str) -> str:
        """One plain-text turn (#150), deliberately with no `tool_ids`: a
        filter's `inlet`/`outlet` still runs (`utils/filter.py`'s
        `process_filter_function` is unconditional - there is no "only with
        tools" branch), but the model itself never enters Open WebUI's
        native MCP tool loop, so a script need not worry about `#144`'s own
        one-tool-call-per-chat finding here. Same synchronous, `200 null`
        shape as `run_scripted_tool_chat` (see that method's own docstring)
        - the assistant's actual text is read back from `GET
        /api/v1/chats/{chat_id}`, `content` if set, else
        `_output_text(output)` (`utils/middleware.py:4129`'s own
        `m.get('content') or get_output_text(m.get('output'))`, mirrored
        here since this module cannot import Open WebUI's backend package).

        The request also carries a top-level `user_message` with its own
        `id` (`main.py:1231,1521-1526`: `form_data.pop('user_message',
        ...)`, then persisted only `if user_message.get('id')`) - needed
        for the outlet's own report (#150's optional round trip) to land on
        a message Open WebUI actually saved, harmless for a plain inlet-only
        chat otherwise."""
        new_chat_response = await self._client.post(
            f"{self._webui_url}/api/v1/chats/new",
            json={
                "chat": {
                    "models": [model_id],
                    "history": {"messages": {}, "currentId": None},
                    "messages": [],
                    "title": "New Chat",
                    "timestamp": int(time.time() * 1000),
                }
            },
        )
        if new_chat_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"chats/new failed: {new_chat_response.status_code} {new_chat_response.text}"
            )
        chat_id = new_chat_response.json().get("id", "")
        if not chat_id:
            raise OpenWebUiDriverError(f"chats/new returned no chat id: {new_chat_response.json()}")

        user_message_id = str(uuid.uuid4())
        assistant_message_id = str(uuid.uuid4())
        completion_response = await self._client.post(
            f"{self._webui_url}/api/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": message}],
                "stream": True,
                "chat_id": chat_id,
                "id": assistant_message_id,
                "user_message": {"id": user_message_id, "role": "user", "content": message},
            },
        )
        if completion_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"chat completion failed: {completion_response.status_code} "
                f"{completion_response.text}"
            )

        chat_detail_response = await self._client.get(f"{self._webui_url}/api/v1/chats/{chat_id}")
        if chat_detail_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"reading the chat back failed: {chat_detail_response.status_code} "
                f"{chat_detail_response.text}"
            )
        chat_detail = chat_detail_response.json() or {}
        messages = (chat_detail.get("chat") or {}).get("history", {}).get("messages", {})
        assistant_message = messages.get(assistant_message_id) or {}
        content = assistant_message.get("content")
        if isinstance(content, str) and content:
            return content
        return _output_text(assistant_message.get("output"))

    async def list_model_ids(self) -> list[str]:
        """`GET /api/models` - every model id Open WebUI currently reports, combining
        every configured provider (Ollama included once its `OLLAMA_BASE_URL` answers).
        A fast precondition check (#151, WP-44's own small-model benchmark): if the
        pinned model tag never shows up here, a caller can fail fast with a clear reason
        instead of a slow real-model chat call hanging against a model Open WebUI never
        found."""
        response = await self._client.get(f"{self._webui_url}/api/models")
        if response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"listing models failed: {response.status_code} {response.text}"
            )
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise OpenWebUiDriverError(f"/api/models returned no 'data' list: {body}")
        return [str(item["id"]) for item in data if isinstance(item, dict) and "id" in item]

    async def run_text_chat(
        self,
        *,
        model_id: str,
        message: str,
        temperature: float | None = None,
        seed: int | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> TextChatResult:
        """One plain-text turn against a real model (#151, WP-44's own small-model
        benchmark) - `run_scripted_text_chat`'s own twin: same request/read-back shape
        (that method's own docstring explains both), but against a real model instead
        of the deterministic stub, with optional, OpenAI-style generation params
        (`temperature`/`seed`/`max_tokens`) passed through verbatim on the completion
        request body - Open WebUI's own `/api/chat/completions` forwards whichever of
        these the active backend understands - and an optional per-call `httpx` timeout
        override, since one real, CPU-bound 7-8B completion can run for minutes, unlike
        the scripted stub's instant reply.

        Returns the answer together with the persisted assistant message's own `usage`
        dict (confirmed live, v0.11.4: for the `ollama` backend this carries Ollama's
        own `eval_count`/`eval_duration` alongside the OpenAI-shaped `prompt_tokens`/
        `completion_tokens`) - `None` if that key is absent, e.g. a different backend."""
        new_chat_response = await self._client.post(
            f"{self._webui_url}/api/v1/chats/new",
            json={
                "chat": {
                    "models": [model_id],
                    "history": {"messages": {}, "currentId": None},
                    "messages": [],
                    "title": "New Chat",
                    "timestamp": int(time.time() * 1000),
                }
            },
        )
        if new_chat_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"chats/new failed: {new_chat_response.status_code} {new_chat_response.text}"
            )
        chat_id = new_chat_response.json().get("id", "")
        if not chat_id:
            raise OpenWebUiDriverError(f"chats/new returned no chat id: {new_chat_response.json()}")

        user_message_id = str(uuid.uuid4())
        assistant_message_id = str(uuid.uuid4())
        completion_body: dict[str, object] = {
            "model": model_id,
            "messages": [{"role": "user", "content": message}],
            "stream": True,
            "chat_id": chat_id,
            "id": assistant_message_id,
            "user_message": {"id": user_message_id, "role": "user", "content": message},
        }
        if temperature is not None:
            completion_body["temperature"] = temperature
        if seed is not None:
            completion_body["seed"] = seed
        if max_tokens is not None:
            completion_body["max_tokens"] = max_tokens

        completion_response = await self._client.post(
            f"{self._webui_url}/api/chat/completions",
            json=completion_body,
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        if completion_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"chat completion failed: {completion_response.status_code} "
                f"{completion_response.text}"
            )

        chat_detail_response = await self._client.get(f"{self._webui_url}/api/v1/chats/{chat_id}")
        if chat_detail_response.status_code != httpx.codes.OK:
            raise OpenWebUiDriverError(
                f"reading the chat back failed: {chat_detail_response.status_code} "
                f"{chat_detail_response.text}"
            )
        chat_detail = chat_detail_response.json() or {}
        messages = (chat_detail.get("chat") or {}).get("history", {}).get("messages", {})
        assistant_message = messages.get(assistant_message_id) or {}
        content = assistant_message.get("content")
        text = (
            content
            if isinstance(content, str) and content
            else _output_text(assistant_message.get("output"))
        )
        usage = assistant_message.get("usage")
        return TextChatResult(content=text, usage=usage if isinstance(usage, dict) else None)


def _read_password(args: argparse.Namespace) -> str:
    if args.admin_password_file:
        with open(args.admin_password_file, encoding="utf-8") as handle:
            return handle.read().strip()
    if args.admin_password:
        return str(args.admin_password)
    raise SystemExit("one of --admin-password-file/--admin-password is required")


async def _run_connect(args: argparse.Namespace) -> int:
    password = _read_password(args)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        driver = OpenWebUiDriver(
            client, webui_url=args.webui_url, resource_server_url=args.resource_server_url
        )
        result = await driver.connect(
            admin_name=args.admin_name,
            admin_email=args.admin_email,
            admin_password=password,
            server_id=args.server_id,
        )
    print(result)
    return 0 if result.authorize_followed_to_webui else 1


async def _run_chat(args: argparse.Namespace) -> int:
    """Signs the admin back in (a fresh process has no cookie jar from an
    earlier `connect` run) and drives one scripted tool-calling turn - run
    this *after* `connect` has registered the MCP server and completed that
    user's OAuth consent, or `connect_mcp_server` finds no stored token
    and the chat ends with no tools, same as an un-consented user."""
    password = _read_password(args)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        driver = OpenWebUiDriver(
            client, webui_url=args.webui_url, resource_server_url=args.resource_server_url
        )
        await driver.sign_in(email=args.admin_email, password=password)
        result = await driver.run_scripted_tool_chat(
            model_id=args.model_id, server_id=args.server_id, message=args.message
        )
    print(result)
    if not result.function_call_outputs:
        print("no function_call_output items on the assistant message", file=sys.stderr)
        return 1
    if "not found" in result.output_text().lower():
        print("a function_call_output reports a missing tool", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect_parser = subparsers.add_parser(
        "connect", help="run signup + DCR + tool-server setup + one user's OAuth consent"
    )
    connect_parser.add_argument("--webui-url", default=_DEFAULT_WEBUI_URL)
    connect_parser.add_argument("--resource-server-url", default=_DEFAULT_RESOURCE_SERVER_URL)
    connect_parser.add_argument("--server-id", default=_DEFAULT_SERVER_ID)
    connect_parser.add_argument("--admin-name", default="spike admin")
    connect_parser.add_argument("--admin-email", default="spike-admin@example.com")
    connect_parser.add_argument("--admin-password-file", default=None)
    connect_parser.add_argument("--admin-password", default=None)

    chat_parser = subparsers.add_parser(
        "chat", help="sign in as the admin and run one scripted, tool-calling chat turn"
    )
    chat_parser.add_argument("--webui-url", default=_DEFAULT_WEBUI_URL)
    chat_parser.add_argument("--resource-server-url", default=_DEFAULT_RESOURCE_SERVER_URL)
    chat_parser.add_argument("--server-id", default=_DEFAULT_SERVER_ID)
    chat_parser.add_argument("--model-id", default="scripted-model")
    chat_parser.add_argument("--admin-email", default="spike-admin@example.com")
    chat_parser.add_argument("--admin-password-file", default=None)
    chat_parser.add_argument("--admin-password", default=None)
    chat_parser.add_argument("--message", default="search my notes")

    args = parser.parse_args(argv)
    if args.command == "connect":
        return asyncio.run(_run_connect(args))
    if args.command == "chat":
        return asyncio.run(_run_chat(args))
    raise SystemExit(f"unknown command: {args.command}")  # pragma: no cover - argparse guards this


if __name__ == "__main__":
    sys.exit(main())

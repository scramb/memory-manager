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
import sys
from dataclasses import dataclass

import httpx

#: Open WebUI's own `type:<id>` client-id shape (`utils/oauth.py`,
#: `ensure_client_from_config`) - fixed, not a secret.
_OAUTH_CLIENT_TYPE = "mcp"

_DEFAULT_SERVER_ID = "memmgr"
_DEFAULT_WEBUI_URL = "http://localhost:3000"
_DEFAULT_RESOURCE_SERVER_URL = "http://localhost:18081"


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
        (`utils/oauth.py.ensure_client_from_config`'s own read path)."""
        response = await self._client.post(
            f"{self._webui_url}/api/v1/configs/tool_servers",
            json={
                "TOOL_SERVER_CONNECTIONS": [
                    {
                        "url": self._resource_server_url,
                        "path": "mcp",
                        "type": "mcp",
                        "auth_type": "oauth_2.1",
                        "forward_cookies": False,
                        "headers": None,
                        "key": None,
                        "config": {"enable": True},
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

    async def run_oauth_flow(self, *, server_id: str, password: str) -> ConnectResult:
        """Drives the per-user consent: `GET /oauth/clients/mcp:<id>/authorize`
        (Open WebUI) -> memory-manager's `/authorize` -> `/login?pending=...`
        -> `POST /login` with the admin password -> back through
        memory-manager's `/authorize` completion -> Open WebUI's own
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
        pending = httpx.URL(login_url).params.get("pending", "")
        if not pending:
            raise OpenWebUiDriverError(f"no 'pending' id in the /login URL: {login_url}")

        login_base = f"{login_url.split('?', 1)[0]}"
        submit_response = await self._client.post(
            login_base,
            data={"pending": pending, "password": password},
        )
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

    args = parser.parse_args(argv)
    if args.command == "connect":
        return asyncio.run(_run_connect(args))
    raise SystemExit(f"unknown command: {args.command}")  # pragma: no cover - argparse guards this


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: AGPL-3.0-only
"""Microsoft Graph client: app-only `user_state`/`member_groups` (#214, ADR-0006
§4-§5, ADR-0009 §3).

Built on `httpx` alone, the same "no new dependency" choice `auth/login_oidc.py`
already made for the upstream OIDC client - Graph is called with plain
`client_credentials` and two JSON endpoints, nothing an SDK buys here. Shapes are
pinned against `docs/research/entra-contract.md` (§3 client-credentials grant, §5
`getMemberGroups`, §7 `429`/`Retry-After`, §8 `503`), verified against
`tests/mock_idp`.

`GraphClient` owns exactly two things ADR-0006 needs from Graph:

- `user_state(oid)` - `accountEnabled`/missing, consulted on every refresh (ADR-0006
  §5) to cut off a disabled or deleted user immediately.
- `member_groups(oid)` - the groups-overage fallback (ADR-0006 §4) when the ID
  token's `groups` claim was replaced by the `_claim_names`/`_claim_sources`
  overage signal.

The app-only token (`client_credentials`, scope `https://graph.microsoft.com/.default`)
is cached on this instance until shortly before `expires_in` runs out (ADR-0009 §3:
a per-replica cache, never shared - losing it just costs one extra `/token` round
trip on the next call). An `asyncio.Lock` collapses concurrent callers that find the
cache expired into a single `/token` request rather than one each.

Not included here (#214 "Not included"): the `users/delta` query (#223/WP-24, added
to this same module later), calling this client from login or refresh (#215, #216),
and OTel spans/metrics (WP-31).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any

import httpx

from memory_manager.config import ServerConfigError

__all__ = ["GraphClient", "GraphError", "UserState"]

_logger = logging.getLogger(__name__)

#: Literal per the contract (entra-contract.md §3): "All scopes included must be
#: for a single resource" - Graph's own resource identifier plus `.default`,
#: independent of wherever `graph_base_url` itself points (the mock IdP serves
#: Graph under its own host, but still expects exactly this scope value).
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_DEFAULT_AUTHORITY_BASE_URL = "https://login.microsoftonline.com"
_DEFAULT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_HTTP_TIMEOUT_SECONDS = 10.0
#: An app token is refetched this long before its advertised `expires_in` - never
#: used right up to the second, so a token that was "almost" valid when `_app_token`
#: read it does not expire mid-flight on the Graph call that follows.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
#: Matches `index/embeddings.py`'s `_HttpEmbeddingProvider` budget: up to two
#: retries (three attempts total) on a transient failure.
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class GraphError(Exception):
    """A Graph request failed after retries, or its response was malformed.

    The message carries only the endpoint and status code (#214) - never the
    bearer token or a response body, since this exception is expected to reach a
    log an operator reads, and Graph's own error bodies can echo back request
    details that should not be repeated there.
    """


class UserState(enum.Enum):
    """What `GraphClient.user_state` found for one `oid` (ADR-0006 §5: a refresh
    re-checks `accountEnabled` and existence)."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    MISSING = "missing"


class GraphClient:
    """App-only Microsoft Graph access for `oid` lookups.

    `authority_base_url`/`graph_base_url` default to the real Entra/Graph hosts;
    a test points them at `tests/mock_idp` instead. Both must be `https://` unless
    `allow_insecure_authority` is set (the flag `ENTRA_ALLOW_INSECURE_AUTHORITY`
    will plumb through from `config.py` once #22d adds it - this client only takes
    the already-decided boolean, it does not read the environment itself).
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        authority_base_url: str = _DEFAULT_AUTHORITY_BASE_URL,
        graph_base_url: str = _DEFAULT_GRAPH_BASE_URL,
        allow_insecure_authority: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not allow_insecure_authority:
            for name, url in (
                ("authority_base_url", authority_base_url),
                ("graph_base_url", graph_base_url),
            ):
                if not url.startswith("https://"):
                    raise ServerConfigError(
                        f"GraphClient {name} {url!r} must be https:// unless "
                        "allow_insecure_authority is set"
                    )
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._authority_base_url = authority_base_url.rstrip("/")
        self._graph_base_url = graph_base_url.rstrip("/")
        # Only a self-created client is this instance's to close (`aclose`) - a
        # caller-supplied one (every test) stays the caller's own responsibility,
        # the same split `OidcAuthenticator` already uses.
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Closes the internally created `httpx.AsyncClient`, if `__init__` made one
        itself - mirrors `OidcAuthenticator.aclose`."""
        if self._owns_http_client:
            await self._http.aclose()

    # ---- public API -----------------------------------------------------------

    async def user_state(self, oid: str) -> UserState:
        """`GET /users/{oid}` → `UserState.ENABLED`/`DISABLED` from `accountEnabled`,
        or `UserState.MISSING` on a `404` (ADR-0006 §5, §6)."""
        url = f"{self._graph_base_url}/users/{oid}"
        response = await self._graph_request("GET", url)
        if response.status_code == 404:
            return UserState.MISSING
        self._raise_for_status(response, url)
        data = self._json(response, url)
        try:
            enabled = bool(data["accountEnabled"])
        except (KeyError, TypeError) as exc:
            raise GraphError(f"GET {url} response is missing 'accountEnabled'") from exc
        return UserState.ENABLED if enabled else UserState.DISABLED

    async def member_groups(self, oid: str) -> list[str]:
        """`POST /users/{oid}/getMemberGroups` → every group id the user is a member
        of (ADR-0006 §4, groups-overage fallback).

        `getMemberGroups` is a single Graph *action*, not a paged collection -
        `docs/research/entra-contract.md` §5 shows only `@odata.context` + `value`
        for it, unlike `users/delta`'s own `@odata.nextLink`/`@odata.deltaLink`
        paging (§6, out of scope here per #214). Nothing here follows a
        `@odata.nextLink`, since the contract records that this endpoint does not
        send one.
        """
        url = f"{self._graph_base_url}/users/{oid}/getMemberGroups"
        response = await self._graph_request("POST", url, json_body={"securityEnabledOnly": False})
        if response.status_code == 404:
            raise GraphError(f"POST {url} failed with status 404 (user not found)")
        self._raise_for_status(response, url)
        data = self._json(response, url)
        values = data.get("value") if isinstance(data, dict) else None
        if not isinstance(values, list):
            raise GraphError(f"POST {url} response has no 'value' list")
        return [str(value) for value in values]

    # ---- app token (ADR-0009 §3: per-replica cache until expiry) --------------

    async def _app_token(self) -> str:
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            # Re-check: another caller may have refreshed it while this one
            # waited for the lock - collapses N concurrent expired-cache callers
            # into exactly one `/token` request.
            now = time.monotonic()
            if self._token is not None and now < self._token_expires_at:
                return self._token
            token, expires_in = await self._fetch_app_token()
            self._token = token
            self._token_expires_at = now + max(expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS, 0.0)
            return token

    async def _fetch_app_token(self) -> tuple[str, float]:
        url = f"{self._authority_base_url}/{self._tenant_id}/oauth2/v2.0/token"
        try:
            response = await self._http.post(
                url,
                data={"grant_type": "client_credentials", "scope": _GRAPH_SCOPE},
                auth=(self._client_id, self._client_secret),
            )
        except httpx.HTTPError as exc:
            raise GraphError(f"app token request to {url} failed") from exc
        if response.status_code >= 400:
            raise GraphError(
                f"app token request to {url} failed with status {response.status_code}"
            )
        data = self._json(response, url)
        try:
            token = data["access_token"]
            expires_in = float(data["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphError(f"app token response from {url} is malformed") from exc
        if not isinstance(token, str) or not token:
            raise GraphError(f"app token response from {url} carried no access_token")
        return token, expires_in

    # ---- Graph requests: bearer, bounded retry on 429/5xx ---------------------

    async def _graph_request(
        self, method: str, url: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        token = await self._app_token()
        headers = {"Authorization": f"Bearer {token}"}
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._http.request(method, url, headers=headers, json=json_body)
            except httpx.HTTPError as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise GraphError(f"{method} {url} failed after {attempt} attempt(s)") from exc
                await _sleep_before_retry(attempt, retry_after=None)
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                await _sleep_before_retry(attempt, retry_after=_retry_after_seconds(response))
                continue
            return response

    def _raise_for_status(self, response: httpx.Response, url: str) -> None:
        if response.status_code == 403:
            raise GraphError(
                f"request to {url} denied with status 403 (missing Graph application permission)"
            )
        if response.status_code >= 400:
            raise GraphError(f"request to {url} failed with status {response.status_code}")

    def _json(self, response: httpx.Response, url: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise GraphError(f"response from {url} is not valid JSON") from exc
        if not isinstance(data, dict):
            raise GraphError(f"response from {url} is not a JSON object")
        return data


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


async def _sleep_before_retry(attempt: int, *, retry_after: float | None) -> None:
    delay = (
        retry_after if retry_after is not None else _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    )
    await asyncio.sleep(delay)

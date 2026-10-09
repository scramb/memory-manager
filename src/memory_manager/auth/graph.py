# SPDX-License-Identifier: AGPL-3.0-only
"""Microsoft Graph client: app-only `user_state`/`member_groups`/`users_delta`
(#214, #223, ADR-0006 §4-§6, ADR-0009 §3).

Built on `httpx` alone, the same "no new dependency" choice `auth/login_oidc.py`
already made for the upstream OIDC client - Graph is called with plain
`client_credentials` and plain JSON endpoints, nothing an SDK buys here. Shapes are
pinned against `docs/research/entra-contract.md` (§3 client-credentials grant, §5
`getMemberGroups`, §6 `users/delta`, §7 `429`/`Retry-After`, §8 `503`), verified
against `tests/mock_idp`.

`GraphClient` owns exactly three things ADR-0006 needs from Graph:

- `user_state(oid)` - `accountEnabled`/missing, consulted on every refresh (ADR-0006
  §5) to cut off a disabled or deleted user immediately.
- `member_groups(oid)` - the groups-overage fallback (ADR-0006 §4) when the ID
  token's `groups` claim was replaced by the `_claim_names`/`_claim_sources`
  overage signal.
- `users_delta(delta_link)` - the worker's own deprovisioning sweep (ADR-0006 §6,
  #223): one full round of `users/delta`, following every `@odata.nextLink` page
  itself and returning the round's changes plus the `@odata.deltaLink` to store for
  next time. `memory_manager.worker` is the only caller.

The app-only token (`client_credentials`, scope `https://graph.microsoft.com/.default`)
is cached on this instance until shortly before `expires_in` runs out (ADR-0009 §3:
a per-replica cache, never shared - losing it just costs one extra `/token` round
trip on the next call). An `asyncio.Lock` collapses concurrent callers that find the
cache expired into a single `/token` request rather than one each.

`from_env` builds this client from the same `ENTRA_*` variables `auth.login_entra.
EntraAuthenticator.from_env` already parses for its own app-only Graph client -
`memory_manager.worker`'s own build-up (a separate process, never an
`EntraAuthenticator`, which also owns OIDC discovery and login state this job has
no use for) is this classmethod's one caller; `None` means Entra is not configured
for this deployment at all, the signal `worker.build_jobs` uses to skip registering
the delta-sync job outright.

Not included here (#214 "Not included"): calling this client from login or refresh
(#215, #216, already wired into `auth.login_entra` directly), and OTel spans/metrics
(WP-31).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from memory_manager.config import ServerConfigError

__all__ = [
    "GraphClient",
    "GraphDeltaExpired",
    "GraphError",
    "UserDeltaChange",
    "UserState",
    "UsersDeltaResult",
]

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


class GraphDeltaExpired(GraphError):
    """`users/delta` answered `410 Gone` (entra-contract.md §6): the stored delta
    link is older than Entra's 7-day retention, or was reset upstream.

    A subclass of `GraphError`, not a sibling - a caller that only wants "Graph
    failed, retry next interval" can still catch `GraphError` alone; `worker.
    _entra_delta_sync_job` is the one caller that tells this apart, to retry once
    with a full sync (`users_delta(None)`) rather than just leaving the cursor
    untouched for the next scheduled attempt.
    """


class UserState(enum.Enum):
    """What `GraphClient.user_state` found for one `oid` (ADR-0006 §5: a refresh
    re-checks `accountEnabled` and existence)."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    MISSING = "missing"


@dataclass(frozen=True)
class UserDeltaChange:
    """One `users/delta` entry, already normalised to what `worker.
    _entra_delta_sync_job` needs to apply it: `enabled=False` for both
    `accountEnabled=false` **and** a `@removed` entry (ADR-0006 §6: either one
    disables), `enabled=True` for `accountEnabled=true`. A `value` entry whose
    `accountEnabled` is absent and which is not `@removed` either (a delta round
    reports every changed field, not just this one) never becomes one of these -
    `GraphClient.users_delta` drops it rather than guessing a state that did not
    actually change.
    """

    oid: str
    enabled: bool


@dataclass(frozen=True)
class UsersDeltaResult:
    """One full `users/delta` round (`GraphClient.users_delta`'s own return value):
    every page already followed, `changes` is every entry across all of them, and
    `delta_link` is the `@odata.deltaLink` the round completed with - the value to
    store and pass back in as `delta_link` on the next round."""

    changes: tuple[UserDeltaChange, ...]
    delta_link: str


class GraphClient:
    """App-only Microsoft Graph access for `oid` lookups.

    `authority_base_url`/`graph_base_url` default to the real Entra/Graph hosts;
    a test points them at `tests/mock_idp` instead. Both must be `https://` unless
    `allow_insecure_authority` is set - `from_env` is the one place this flag
    (`ENTRA_ALLOW_INSECURE_AUTHORITY`) is actually read out of the environment;
    `__init__` itself only ever takes the already-decided boolean.
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

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str], *, http_client: httpx.AsyncClient | None = None
    ) -> GraphClient | None:
        """Builds a `GraphClient` from the same `ENTRA_TENANT_ID`/`ENTRA_CLIENT_ID`/
        `ENTRA_CLIENT_SECRET`/`ENTRA_AUTHORITY`/`ENTRA_GRAPH_URL`/
        `ENTRA_ALLOW_INSECURE_AUTHORITY` variables `auth.login_entra.
        EntraAuthenticator.from_env` already parses for its own app-only Graph
        client - `None` if `ENTRA_TENANT_ID` is unset, meaning Entra is not
        configured for this deployment at all (`worker.build_jobs`'s own signal
        for whether to register the delta-sync job, #223).

        Raises `ServerConfigError` if `ENTRA_TENANT_ID` is set but
        `ENTRA_CLIENT_ID`/`ENTRA_CLIENT_SECRET` is missing, or (`__init__`'s own
        check) `ENTRA_AUTHORITY`/`ENTRA_GRAPH_URL` is not `https://` and
        `ENTRA_ALLOW_INSECURE_AUTHORITY` is not set.
        """
        tenant_id = environ.get("ENTRA_TENANT_ID")
        if not tenant_id:
            return None
        required = {
            "ENTRA_CLIENT_ID": environ.get("ENTRA_CLIENT_ID"),
            "ENTRA_CLIENT_SECRET": environ.get("ENTRA_CLIENT_SECRET"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ServerConfigError(f"ENTRA_TENANT_ID is set but {', '.join(missing)} is missing")
        client_id = required["ENTRA_CLIENT_ID"]
        client_secret = required["ENTRA_CLIENT_SECRET"]
        if client_id is None or client_secret is None:
            raise AssertionError("unreachable: 'missing' above already covers every None case")
        authority = environ.get("ENTRA_AUTHORITY") or _DEFAULT_AUTHORITY_BASE_URL
        graph_url = environ.get("ENTRA_GRAPH_URL") or _DEFAULT_GRAPH_BASE_URL
        allow_insecure_authority = _parse_bool_env(environ.get("ENTRA_ALLOW_INSECURE_AUTHORITY"))
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            authority_base_url=authority,
            graph_base_url=graph_url,
            allow_insecure_authority=allow_insecure_authority,
            http_client=http_client,
        )

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
        paging (§6, `users_delta` below). Nothing here follows a `@odata.nextLink`,
        since the contract records that this endpoint does not send one.
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

    async def users_delta(self, delta_link: str | None) -> UsersDeltaResult:
        """One full `users/delta` round (ADR-0006 §6, #223): `GET /users/delta`
        (`delta_link is None`, a full sync) or `GET {delta_link}` (a prior round's
        own `@odata.deltaLink`), following every `@odata.nextLink` page of this
        round itself before returning - the caller never sees an individual page,
        only the round's complete `UsersDeltaResult`.

        Raises `GraphDeltaExpired` on a `410` from any page (entra-contract.md §6:
        the delta token is older than Entra's 7-day retention, or was reset
        upstream) - the caller decides whether and how to restart with a full sync
        (`users_delta(None)`); this method never retries that on its own. Any other
        failure (after `_graph_request`'s own bounded retries) raises plain
        `GraphError`, with nothing from a page already fetched returned to the
        caller - a partial round is never half-applied.
        """
        url = delta_link if delta_link is not None else f"{self._graph_base_url}/users/delta"
        changes: list[UserDeltaChange] = []
        while True:
            response = await self._graph_request("GET", url)
            if response.status_code == 410:
                raise GraphDeltaExpired(f"GET {url} failed with status 410 (delta link expired)")
            self._raise_for_status(response, url)
            data = self._json(response, url)
            values = data.get("value")
            if not isinstance(values, list):
                raise GraphError(f"GET {url} response has no 'value' list")
            changes.extend(change for entry in values if (change := _parse_delta_entry(entry)))

            next_link = data.get("@odata.nextLink")
            if isinstance(next_link, str) and next_link:
                url = next_link
                continue
            new_delta_link = data.get("@odata.deltaLink")
            if not isinstance(new_delta_link, str) or not new_delta_link:
                raise GraphError(
                    f"GET {url} response carried neither '@odata.nextLink' nor '@odata.deltaLink'"
                )
            return UsersDeltaResult(changes=tuple(changes), delta_link=new_delta_link)

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


def _parse_bool_env(raw: str | None) -> bool:
    """Matches `auth.login_entra._parse_bool` exactly - duplicated, not imported
    (that function is private to that module), for `from_env`'s own
    `ENTRA_ALLOW_INSECURE_AUTHORITY` parsing."""
    if raw is None:
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_delta_entry(entry: Any) -> UserDeltaChange | None:
    """One `users/delta` `value` entry, or `None` if it carries no usable account
    state - `GraphClient.users_delta`'s own filter (`UserDeltaChange`'s docstring
    explains why a changed-but-not-`accountEnabled` entry is dropped rather than
    guessed at)."""
    if not isinstance(entry, dict):
        return None
    oid = entry.get("id")
    if not isinstance(oid, str) or not oid:
        return None
    if "@removed" in entry:
        return UserDeltaChange(oid=oid, enabled=False)
    enabled = entry.get("accountEnabled")
    if not isinstance(enabled, bool):
        return None
    return UserDeltaChange(oid=oid, enabled=enabled)

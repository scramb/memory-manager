# SPDX-License-Identifier: AGPL-3.0-only
"""The ASGI app itself: Entra v2 OIDC endpoints, a slice of Microsoft Graph, and a
`/_mock/` control API a test drives directly (no browser, no real tenant).

Shapes are pinned against `docs/research/entra-contract.md` (Microsoft Learn,
retrieved 2026-10-08), not recalled: discovery without
`code_challenge_methods_supported`, authorize/token parameters and PKCE S256,
ID-token claims (`iss`/`aud`/`tid`/`oid`/`nonce`/`roles`/`groups`, overage via
`_claim_names`/`_claim_sources`), the `client_credentials` grant for
`https://graph.microsoft.com/.default`, Graph `getMemberGroups` and
`users/delta` (paging via `@odata.nextLink`, round-completion via
`@odata.deltaLink`, removals via `@removed`), and the 429 `Retry-After` shape.

No JWKS, no signature verification against a public key (ADR-0006 §2, §8: the
facade never checks the ID token's signature, since it comes TLS-direct from
this mock's own token endpoint). Every JWT here - ID tokens and Graph app
tokens alike - is HS256-signed with a random per-process key via stdlib
`hmac`/`hashlib`, so a test can still decode and assert on claims, and the
mock's own Graph endpoints can still tell a genuine mock-issued bearer from
garbage, without pulling in a JOSE dependency CLAUDE.md's "few dependencies"
guardrail and ADR-0006 both reject for this project.

State lives in one `_State` per `create_app()` call, mutated only through the
routes below - there is no second writer (no background thread, no shared
module-level global) so a `dataclass` with plain `dict`s is enough; the
`tests/mock_idp_fixtures.py` subprocess fixture starts a fresh process (and
thus a fresh `_State`) per test, the same isolation
`tests/http_fixtures.py`'s `run_http_server` gives the real server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

__all__ = ["create_app"]

_ID_TOKEN_TTL_SECONDS = 300
_ACCESS_TOKEN_TTL_SECONDS = 3600
_GRAPH_RESOURCE = "https://graph.microsoft.com"
_GRAPH_SCOPE = f"{_GRAPH_RESOURCE}/.default"


# --- state -------------------------------------------------------------------


@dataclass
class _User:
    tid: str
    oid: str
    display_name: str
    roles: list[str]
    groups: list[str]
    account_enabled: bool = True
    overage: bool = False
    deleted: bool = False
    revision: int = 0


@dataclass
class _Client:
    tid: str
    client_id: str
    client_secret: str
    redirect_uris: list[str]
    graph_roles: list[str] = field(default_factory=list)


@dataclass
class _AuthCode:
    tid: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    oid: str
    nonce: str | None
    scope: str
    used: bool = False


@dataclass
class _Fault:
    status: int
    retry_after: int | None
    remaining: int


@dataclass
class _State:
    issuer_base: str | None
    signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    users: dict[tuple[str, str], _User] = field(default_factory=dict)
    clients: dict[tuple[str, str], _Client] = field(default_factory=dict)
    codes: dict[str, _AuthCode] = field(default_factory=dict)
    pending_signin: dict[str, str] = field(default_factory=dict)
    revision: dict[str, int] = field(default_factory=dict)
    delta_page_size: dict[str, int] = field(default_factory=dict)
    graph_faults: dict[str, list[_Fault]] = field(default_factory=dict)
    call_counts: dict[str, int] = field(default_factory=dict)


def _bump_revision(state: _State, tid: str) -> int:
    state.revision[tid] = state.revision.get(tid, 0) + 1
    return state.revision[tid]


# --- JWT helpers (HS256 via stdlib hmac, see module docstring) ---------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _sign_jwt(payload: dict[str, Any], key: bytes) -> str:
    header_segment = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload_segment = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_b64url_encode(signature)}"


def _decode_jwt(token: str, key: bytes) -> dict[str, Any] | None:
    """Verifies the HS256 signature against `key` and returns the payload, or
    `None` on any malformed or mis-signed token - used only by the mock's own
    Graph endpoints to read back a token it itself issued."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_segment, payload_segment, signature_segment = parts
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = hmac.new(key, signing_input, hashlib.sha256).digest()
    try:
        actual = _b64url_decode(signature_segment)
    except ValueError:  # malformed base64 -> reject
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(payload_segment))
    except ValueError:  # malformed base64 or JSON -> reject
        return None
    return payload


def _pairwise_sub(client_id: str, oid: str) -> str:
    """A stable but per-`client_id` value, standing in for Entra's real pairwise
    `sub` (docs/research/entra-contract.md §4) - deliberately not `oid`, so a
    test cannot come to rely on `sub == oid`, which real Entra never gives."""
    return _b64url_encode(hashlib.sha256(f"{client_id}:{oid}".encode()).digest())


def _code_challenge_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url_encode(digest) == challenge


# --- issuer derivation ---------------------------------------------------


def _issuer_base(state: _State, request: Request) -> str:
    if state.issuer_base is not None:
        return state.issuer_base
    host = request.headers.get("host", request.url.netloc)
    return f"{request.url.scheme}://{host}"


def _issuer(state: _State, request: Request, tid: str) -> str:
    return f"{_issuer_base(state, request)}/{tid}/v2.0"


# --- OIDC discovery -----------------------------------------------------------


async def _discovery(request: Request) -> Response:
    state: _State = request.app.state.mock
    tid = request.path_params["tid"]
    issuer = _issuer(state, request, tid)
    base = issuer.removesuffix("/v2.0")
    # No "code_challenge_methods_supported" - real Entra omits it too
    # (docs/research/entra-contract.md §1), and ADR-0006 §1 requires the
    # facade to work without it.
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
            "token_endpoint": f"{base}/oauth2/v2.0/token",
            "jwks_uri": f"{base}/discovery/v2.0/keys",
            "response_types_supported": ["code", "id_token", "code id_token"],
            "response_modes_supported": ["query", "fragment", "form_post"],
            "scopes_supported": ["openid", "profile", "email", "offline_access"],
            "subject_types_supported": ["pairwise"],
            "id_token_signing_alg_values_supported": ["HS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }
    )


# --- authorize -----------------------------------------------------------


def _redirect_with(redirect_uri: str, state_param: str | None, **params: str) -> Response:
    query = dict(params)
    if state_param is not None:
        query["state"] = state_param
    return RedirectResponse(f"{redirect_uri}?{urlencode(query)}", status_code=302)


async def _authorize(request: Request) -> Response:
    state: _State = request.app.state.mock
    tid = request.path_params["tid"]
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    response_type = params.get("response_type", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    scope = params.get("scope", "")
    state_param = params.get("state")
    nonce = params.get("nonce")

    client = state.clients.get((tid, client_id))
    if client is None or redirect_uri not in client.redirect_uris:
        # No trusted redirect target to bounce the error to - a real AS
        # refuses outright here too.
        return JSONResponse({"error": "unauthorized_client"}, status_code=400)
    if response_type != "code":
        return _redirect_with(redirect_uri, state_param, error="unsupported_response_type")
    if code_challenge_method != "S256" or not code_challenge:
        return _redirect_with(redirect_uri, state_param, error="invalid_request")

    oid = state.pending_signin.get(tid)
    if oid is None:
        return JSONResponse({"error": "no_signin_selected"}, status_code=400)
    user = state.users.get((tid, oid))
    if user is None or user.deleted:
        return _redirect_with(redirect_uri, state_param, error="access_denied")

    code = secrets.token_urlsafe(24)
    state.codes[code] = _AuthCode(
        tid=tid,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        oid=oid,
        nonce=nonce,
        scope=scope,
    )
    return _redirect_with(redirect_uri, state_param, code=code)


# --- token ---------------------------------------------------------------


def _client_credentials_from_request(
    request: Request, form: FormData
) -> tuple[str | None, str | None]:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        decoded = base64.b64decode(auth.removeprefix("Basic ")).decode("utf-8")
        client_id, _, client_secret = decoded.partition(":")
        return client_id, client_secret
    form_client_id = form.get("client_id")
    form_client_secret = form.get("client_secret")
    return (
        str(form_client_id) if form_client_id is not None else None,
        str(form_client_secret) if form_client_secret is not None else None,
    )


def _token_from_code(
    state: _State, tid: str, client: _Client, form: FormData, issuer: str
) -> Response:
    code = str(form.get("code") or "")
    verifier = str(form.get("code_verifier") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    issued = state.codes.get(code)
    if (
        issued is None
        or issued.used
        or issued.tid != tid
        or issued.client_id != client.client_id
        or issued.redirect_uri != redirect_uri
    ):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not _code_challenge_matches(verifier, issued.code_challenge):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    user = state.users.get((tid, issued.oid))
    if user is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    issued.used = True

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": client.client_id,
        "tid": tid,
        "oid": user.oid,
        "sub": _pairwise_sub(client.client_id, user.oid),
        "ver": "2.0",
        "iat": now,
        "nbf": now,
        "exp": now + _ID_TOKEN_TTL_SECONDS,
        "name": user.display_name,
        "roles": list(user.roles),
    }
    if issued.nonce is not None:
        claims["nonce"] = issued.nonce
    if user.overage:
        claims["_claim_names"] = {"groups": "src1"}
        claims["_claim_sources"] = {
            "src1": {"endpoint": f"{_GRAPH_RESOURCE}/v1.0/users/{user.oid}/getMemberObjects"}
        }
    else:
        claims["groups"] = list(user.groups)

    id_token = _sign_jwt(claims, state.signing_key)
    return JSONResponse(
        {
            "token_type": "Bearer",
            "scope": issued.scope,
            "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            "access_token": secrets.token_urlsafe(24),
            "id_token": id_token,
        }
    )


def _token_client_credentials(
    state: _State, tid: str, client: _Client, form: FormData, issuer: str
) -> Response:
    scope = str(form.get("scope") or "")
    if scope != _GRAPH_SCOPE:
        return JSONResponse({"error": "invalid_scope"}, status_code=400)
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": _GRAPH_RESOURCE,
        "tid": tid,
        "appid": client.client_id,
        "idtyp": "app",
        "roles": list(client.graph_roles),
        "iat": now,
        "nbf": now,
        "exp": now + _ACCESS_TOKEN_TTL_SECONDS,
    }
    access_token = _sign_jwt(claims, state.signing_key)
    return JSONResponse(
        {
            "token_type": "Bearer",
            "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            "access_token": access_token,
        }
    )


async def _token(request: Request) -> Response:
    state: _State = request.app.state.mock
    tid = request.path_params["tid"]
    form = await request.form()
    grant_type = str(form.get("grant_type") or "")
    client_id, client_secret = _client_credentials_from_request(request, form)
    client = state.clients.get((tid, client_id or ""))
    if client is None or client_secret != client.client_secret:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    issuer = _issuer(state, request, tid)
    if grant_type == "authorization_code":
        return _token_from_code(state, tid, client, form, issuer)
    if grant_type == "client_credentials":
        return _token_client_credentials(state, tid, client, form, issuer)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# --- Microsoft Graph -------------------------------------------------------


def _bearer_claims(request: Request, state: _State) -> dict[str, Any] | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return _decode_jwt(auth.removeprefix("Bearer "), state.signing_key)


def _count_call(state: _State, endpoint: str) -> None:
    state.call_counts[endpoint] = state.call_counts.get(endpoint, 0) + 1


def _consume_fault(state: _State, endpoint: str) -> _Fault | None:
    for key in (endpoint, "any"):
        queue = state.graph_faults.get(key)
        if queue:
            fault = queue[0]
            fault.remaining -= 1
            if fault.remaining <= 0:
                queue.pop(0)
            return fault
    return None


def _fault_response(fault: _Fault) -> Response:
    headers = {"Retry-After": str(fault.retry_after)} if fault.retry_after is not None else {}
    code = "TooManyRequests" if fault.status == 429 else "ServiceUnavailable"
    body = {"error": {"code": code, "message": "mock-injected fault"}}
    return JSONResponse(body, status_code=fault.status, headers=headers)


def _user_resource(user: _User) -> dict[str, Any]:
    return {
        "id": user.oid,
        "displayName": user.display_name,
        "accountEnabled": user.account_enabled,
    }


async def _graph_get_user(request: Request) -> Response:
    state: _State = request.app.state.mock
    _count_call(state, "users.get")
    fault = _consume_fault(state, "users.get")
    if fault is not None:
        return _fault_response(fault)
    claims = _bearer_claims(request, state)
    if claims is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    roles = set(claims.get("roles", []))
    if not ({"User.Read.All", "User.ReadBasic.All"} & roles):
        return JSONResponse({"error": "insufficient_permissions"}, status_code=403)
    tid = str(claims.get("tid", ""))
    oid = request.path_params["oid"]
    user = state.users.get((tid, oid))
    if user is None or user.deleted:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(_user_resource(user))


async def _graph_get_member_groups(request: Request) -> Response:
    state: _State = request.app.state.mock
    _count_call(state, "getMemberGroups")
    fault = _consume_fault(state, "getMemberGroups")
    if fault is not None:
        return _fault_response(fault)
    claims = _bearer_claims(request, state)
    if claims is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    roles = set(claims.get("roles", []))
    has_user_scope = bool({"User.Read.All", "User.ReadBasic.All"} & roles)
    if not (has_user_scope and "GroupMember.Read.All" in roles):
        return JSONResponse({"error": "insufficient_permissions"}, status_code=403)
    tid = str(claims.get("tid", ""))
    oid = request.path_params["oid"]
    user = state.users.get((tid, oid))
    if user is None or user.deleted:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(
        {
            "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#Collection(Edm.String)",
            "value": list(user.groups),
        }
    )


def _delta_entry(user: _User) -> dict[str, Any]:
    if user.deleted:
        return {"id": user.oid, "@removed": {"reason": "changed"}}
    return {
        "id": user.oid,
        "displayName": user.display_name,
        "accountEnabled": user.account_enabled,
    }


def _delta_cursor_encode(cursor: dict[str, int]) -> str:
    return _b64url_encode(json.dumps(cursor).encode())


def _delta_cursor_decode(token: str) -> dict[str, int]:
    result: dict[str, int] = json.loads(_b64url_decode(token))
    return result


async def _graph_users_delta(request: Request) -> Response:
    state: _State = request.app.state.mock
    _count_call(state, "users.delta")
    fault = _consume_fault(state, "users.delta")
    if fault is not None:
        return _fault_response(fault)
    claims = _bearer_claims(request, state)
    if claims is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    if "User.Read.All" not in claims.get("roles", []):
        return JSONResponse({"error": "insufficient_permissions"}, status_code=403)
    tid = str(claims.get("tid", ""))

    skiptoken = request.query_params.get("$skiptoken")
    deltatoken = request.query_params.get("$deltatoken")
    if skiptoken is not None:
        cursor = _delta_cursor_decode(skiptoken)
        since, offset = cursor["since"], cursor["offset"]
    elif deltatoken is not None:
        cursor = _delta_cursor_decode(deltatoken)
        since, offset = cursor["since"], 0
    else:
        since, offset = -1, 0

    changed = sorted(
        (
            user
            for (user_tid, _oid), user in state.users.items()
            if user_tid == tid and user.revision > since
        ),
        key=lambda user: user.revision,
    )
    page_size = state.delta_page_size.get(tid) or max(len(changed), 1)
    page = changed[offset : offset + page_size]
    current_max = state.revision.get(tid, 0)
    value = [_delta_entry(user) for user in page]

    base_url = f"{_issuer_base(state, request)}/graph/v1.0/users/delta"
    body: dict[str, Any] = {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
        "value": value,
    }
    next_offset = offset + page_size
    if next_offset < len(changed):
        token = _delta_cursor_encode({"since": since, "offset": next_offset})
        body["@odata.nextLink"] = f"{base_url}?$skiptoken={token}"
    else:
        token = _delta_cursor_encode({"since": current_max, "offset": 0})
        body["@odata.deltaLink"] = f"{base_url}?$deltatoken={token}"
    return JSONResponse(body)


# --- control API (/_mock/) -------------------------------------------------


async def _control_health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def _control_create_user(request: Request) -> Response:
    state: _State = request.app.state.mock
    data = await request.json()
    tid = str(data["tid"])
    oid = str(data["oid"])
    revision = _bump_revision(state, tid)
    state.users[(tid, oid)] = _User(
        tid=tid,
        oid=oid,
        display_name=str(data.get("display_name", oid)),
        roles=list(data.get("roles", [])),
        groups=list(data.get("groups", [])),
        account_enabled=bool(data.get("account_enabled", True)),
        overage=bool(data.get("overage", False)),
        revision=revision,
    )
    return JSONResponse({"tid": tid, "oid": oid}, status_code=201)


async def _control_update_user(request: Request) -> Response:
    state: _State = request.app.state.mock
    tid, oid = request.path_params["tid"], request.path_params["oid"]
    user = state.users.get((tid, oid))
    if user is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    data = await request.json()
    if "display_name" in data:
        user.display_name = str(data["display_name"])
    if "roles" in data:
        user.roles = list(data["roles"])
    if "groups" in data:
        user.groups = list(data["groups"])
    if "account_enabled" in data:
        user.account_enabled = bool(data["account_enabled"])
    if "overage" in data:
        user.overage = bool(data["overage"])
    user.revision = _bump_revision(state, tid)
    return JSONResponse({"tid": tid, "oid": oid})


async def _control_delete_user(request: Request) -> Response:
    state: _State = request.app.state.mock
    tid, oid = request.path_params["tid"], request.path_params["oid"]
    user = state.users.get((tid, oid))
    if user is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    user.deleted = True
    user.revision = _bump_revision(state, tid)
    return Response(status_code=204)


async def _control_create_client(request: Request) -> Response:
    state: _State = request.app.state.mock
    data = await request.json()
    tid = str(data["tid"])
    client_id = str(data["client_id"])
    state.clients[(tid, client_id)] = _Client(
        tid=tid,
        client_id=client_id,
        client_secret=str(data["client_secret"]),
        redirect_uris=list(data.get("redirect_uris", [])),
        graph_roles=list(data.get("graph_roles", [])),
    )
    return JSONResponse({"tid": tid, "client_id": client_id}, status_code=201)


async def _control_select_signin(request: Request) -> Response:
    state: _State = request.app.state.mock
    data = await request.json()
    tid, oid = str(data["tid"]), str(data["oid"])
    state.pending_signin[tid] = oid
    return JSONResponse({"tid": tid, "oid": oid})


async def _control_set_delta_page_size(request: Request) -> Response:
    state: _State = request.app.state.mock
    data = await request.json()
    tid = str(data["tid"])
    state.delta_page_size[tid] = int(data["page_size"])
    return JSONResponse({"tid": tid, "page_size": state.delta_page_size[tid]})


async def _control_inject_fault(request: Request) -> Response:
    state: _State = request.app.state.mock
    data = await request.json()
    endpoint = str(data.get("endpoint", "any"))
    fault = _Fault(
        status=int(data["status"]),
        retry_after=data.get("retry_after"),
        remaining=int(data.get("count", 1)),
    )
    state.graph_faults.setdefault(endpoint, []).append(fault)
    return JSONResponse({"queued": True}, status_code=201)


async def _control_calls(request: Request) -> Response:
    state: _State = request.app.state.mock
    return JSONResponse(dict(state.call_counts))


def create_app(*, issuer_base: str | None = None) -> Starlette:
    """Builds the mock. `issuer_base` overrides the per-request `Host`-header
    issuer derivation (`_issuer_base`) - needed in-cluster (WP-30), where the
    service name the facade dials may not be what `Host` carries; also settable
    via `--issuer-base`/`MOCK_IDP_ISSUER_BASE` in `__main__.py`.
    """
    routes = [
        Route(
            "/{tid}/v2.0/.well-known/openid-configuration",
            _discovery,
            methods=["GET"],
        ),
        Route("/{tid}/oauth2/v2.0/authorize", _authorize, methods=["GET"]),
        Route("/{tid}/oauth2/v2.0/token", _token, methods=["POST"]),
        # More specific Graph paths before the generic "/users/{oid}" route.
        Route(
            "/graph/v1.0/users/{oid}/getMemberGroups", _graph_get_member_groups, methods=["POST"]
        ),
        Route("/graph/v1.0/users/delta", _graph_users_delta, methods=["GET"]),
        Route("/graph/v1.0/users/{oid}", _graph_get_user, methods=["GET"]),
        Route("/_mock/health", _control_health, methods=["GET"]),
        Route("/_mock/users", _control_create_user, methods=["POST"]),
        Route("/_mock/users/{tid}/{oid}", _control_update_user, methods=["PATCH"]),
        Route("/_mock/users/{tid}/{oid}", _control_delete_user, methods=["DELETE"]),
        Route("/_mock/clients", _control_create_client, methods=["POST"]),
        Route("/_mock/select-signin", _control_select_signin, methods=["POST"]),
        Route("/_mock/graph/delta-page-size", _control_set_delta_page_size, methods=["POST"]),
        Route("/_mock/graph/fault", _control_inject_fault, methods=["POST"]),
        Route("/_mock/calls", _control_calls, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.mock = _State(issuer_base=issuer_base)
    return app

# SPDX-License-Identifier: AGPL-3.0-only
"""Proves `tests/mock_idp.app` against the three #212 Definition-of-Done
scenarios (code flow with PKCE, groups overage, disable/delete surfacing in
the next delta page), plus the Graph-permission and fault-injection control
API bullets from the same issue's Implementation checklist. All assertions
are against the mock's own documented contract
(`docs/research/entra-contract.md`), not against ADR-0006's facade - that
facade does not exist yet (WP-24); this module only has to prove the mock a
future facade test will run against actually behaves like the contract.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

_TID = "11111111-1111-1111-1111-111111111111"
_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
_CLIENT_SECRET = "mock-client-secret"  # noqa: S105 - fake test credential
_REDIRECT_URI = "https://facade.example.test/callback"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    _header, payload_segment, _signature = token.split(".")
    result: dict[str, Any] = json.loads(_b64url_decode(payload_segment))
    return result


def _basic_auth_header(client_id: str, client_secret: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


async def _register_client(
    client: httpx.AsyncClient, *, graph_roles: list[str] | None = None
) -> None:
    response = await client.post(
        "/_mock/clients",
        json={
            "tid": _TID,
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "redirect_uris": [_REDIRECT_URI],
            "graph_roles": graph_roles or [],
        },
    )
    assert response.status_code == 201


async def _create_user(client: httpx.AsyncClient, oid: str, **fields: Any) -> None:
    response = await client.post("/_mock/users", json={"tid": _TID, "oid": oid, **fields})
    assert response.status_code == 201


async def _select_signin(client: httpx.AsyncClient, oid: str) -> None:
    response = await client.post("/_mock/select-signin", json={"tid": _TID, "oid": oid})
    assert response.status_code == 200


async def _run_code_flow(
    client: httpx.AsyncClient,
    *,
    nonce: str = "test-nonce",
    scope: str = "openid profile offline_access",
) -> dict[str, Any]:
    """Drives `/authorize` + `/token` end to end and returns the token response."""
    verifier, challenge = _pkce_pair()
    authorize_response = await client.get(
        f"/{_TID}/oauth2/v2.0/authorize",
        params={
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": scope,
            "state": "opaque-state",
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert authorize_response.status_code == 302
    location = authorize_response.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["opaque-state"]
    code = query["code"][0]

    token_response = await client.post(
        f"/{_TID}/oauth2/v2.0/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers=_basic_auth_header(_CLIENT_ID, _CLIENT_SECRET),
    )
    assert token_response.status_code == 200
    result: dict[str, Any] = token_response.json()
    return result


async def test_discovery_omits_code_challenge_methods_supported(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    """Real Entra omits this field too (docs/research/entra-contract.md §1) -
    the mock must not "helpfully" add it, since ADR-0006 §1 requires the
    facade to work without it."""
    response = await mock_idp_client.get(f"/{_TID}/v2.0/.well-known/openid-configuration")
    assert response.status_code == 200
    body = response.json()
    assert "code_challenge_methods_supported" not in body
    assert body["issuer"].endswith(f"/{_TID}/v2.0")


async def test_code_flow_with_pkce_yields_id_token_with_selected_user_claims(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    await _register_client(mock_idp_client)
    await _create_user(
        mock_idp_client,
        "user-1",
        display_name="Ada Lovelace",
        roles=["Memory.User"],
        groups=["group-a", "group-b"],
    )
    await _select_signin(mock_idp_client, "user-1")

    tokens = await _run_code_flow(mock_idp_client, nonce="nonce-abc")
    claims = _decode_jwt_payload(tokens["id_token"])

    assert claims["iss"].endswith(f"/{_TID}/v2.0")
    assert claims["aud"] == _CLIENT_ID
    assert claims["tid"] == _TID
    assert claims["oid"] == "user-1"
    assert claims["nonce"] == "nonce-abc"
    assert claims["roles"] == ["Memory.User"]
    assert claims["groups"] == ["group-a", "group-b"]
    assert "_claim_names" not in claims


async def test_overage_user_gets_claim_names_and_no_groups(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    await _register_client(mock_idp_client)
    await _create_user(
        mock_idp_client, "user-2", roles=["Memory.User"], groups=["group-a"], overage=True
    )
    await _select_signin(mock_idp_client, "user-2")

    tokens = await _run_code_flow(mock_idp_client)
    claims = _decode_jwt_payload(tokens["id_token"])

    assert "groups" not in claims
    assert claims["_claim_names"] == {"groups": "src1"}
    assert "src1" in claims["_claim_sources"]


async def test_disabling_and_deleting_user_show_up_in_next_delta_page(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    await _register_client(mock_idp_client, graph_roles=["User.Read.All", "GroupMember.Read.All"])
    await _create_user(mock_idp_client, "user-3", account_enabled=True)
    await _create_user(mock_idp_client, "user-4", account_enabled=True)

    token_response = await mock_idp_client.post(
        f"/{_TID}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"},
        headers=_basic_auth_header(_CLIENT_ID, _CLIENT_SECRET),
    )
    assert token_response.status_code == 200
    app_token = token_response.json()["access_token"]
    graph_headers = {"Authorization": f"Bearer {app_token}"}

    initial = await mock_idp_client.get("/graph/v1.0/users/delta", headers=graph_headers)
    assert initial.status_code == 200
    initial_body = initial.json()
    assert "@odata.deltaLink" in initial_body
    delta_link = initial_body["@odata.deltaLink"]
    delta_token = parse_qs(urlsplit(delta_link).query)["$deltatoken"][0]

    patch_response = await mock_idp_client.patch(
        f"/_mock/users/{_TID}/user-3", json={"account_enabled": False}
    )
    assert patch_response.status_code == 200
    delete_response = await mock_idp_client.delete(f"/_mock/users/{_TID}/user-4")
    assert delete_response.status_code == 204

    next_page = await mock_idp_client.get(
        "/graph/v1.0/users/delta", params={"$deltatoken": delta_token}, headers=graph_headers
    )
    assert next_page.status_code == 200
    entries = {entry["id"]: entry for entry in next_page.json()["value"]}
    assert entries["user-3"]["accountEnabled"] is False
    assert entries["user-4"]["@removed"] == {"reason": "changed"}


async def test_graph_endpoint_rejects_token_without_required_role(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    await _register_client(
        mock_idp_client, graph_roles=["User.Read.All"]
    )  # no GroupMember.Read.All
    await _create_user(mock_idp_client, "user-5", groups=["group-a"])

    token_response = await mock_idp_client.post(
        f"/{_TID}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"},
        headers=_basic_auth_header(_CLIENT_ID, _CLIENT_SECRET),
    )
    app_token = token_response.json()["access_token"]

    response = await mock_idp_client.post(
        "/graph/v1.0/users/user-5/getMemberGroups",
        json={"securityEnabledOnly": False},
        headers={"Authorization": f"Bearer {app_token}"},
    )
    assert response.status_code == 403


async def test_graph_fault_injection_returns_retry_after(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    await _register_client(mock_idp_client, graph_roles=["User.Read.All"])
    inject_response = await mock_idp_client.post(
        "/_mock/graph/fault",
        json={"endpoint": "users.delta", "status": 429, "retry_after": 7, "count": 1},
    )
    assert inject_response.status_code == 201

    token_response = await mock_idp_client.post(
        f"/{_TID}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"},
        headers=_basic_auth_header(_CLIENT_ID, _CLIENT_SECRET),
    )
    app_token = token_response.json()["access_token"]

    first = await mock_idp_client.get(
        "/graph/v1.0/users/delta", headers={"Authorization": f"Bearer {app_token}"}
    )
    assert first.status_code == 429
    assert first.headers["retry-after"] == "7"

    second = await mock_idp_client.get(
        "/graph/v1.0/users/delta", headers={"Authorization": f"Bearer {app_token}"}
    )
    assert second.status_code == 200

    calls_response = await mock_idp_client.get("/_mock/calls")
    assert calls_response.json()["users.delta"] == 2

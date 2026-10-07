# SPDX-License-Identifier: AGPL-3.0-only
"""Authorization server metadata (RFC 8414), overridden for CIMD (#38, ADR-0004).

What the installed SDK already does on its own, confirmed by reading
`mcp/server/auth/routes.py`'s `build_metadata`/`create_auth_routes` (called from
`mcp/server/lowlevel/server.py`'s `streamable_http_app`) rather than assumed: it
mounts exactly one `/.well-known/oauth-authorization-server` route, with
`token_endpoint_auth_methods_supported` hardcoded to
`["client_secret_post", "client_secret_basic"]` - no `"none"` at all - and
`client_id_metadata_document_supported` never set (the model has the field,
`build_metadata` just never assigns it).

claude.ai only tries CIMD when the AS metadata document carries *both*
`client_id_metadata_document_supported: true` *and* `"none"` in
`token_endpoint_auth_methods_supported` (docs/research/mcp-auth-and-connectors.md
§4) - there is no SDK seam to add either to its own route. `build_authorization_server_
metadata` instead rebuilds the same `OAuthMetadata` document with `build_metadata`
itself (so issuer/registration/revocation URLs, grant types, scopes etc. can never
drift from what the SDK's own mounting on `mcp_app` would have produced, since both
read the one `AuthSettings` `http.py` already built), then - only when CIMD is
enabled - patches in `"none"` and `client_id_metadata_document_supported: true`.
`http.py` serves the result at the same well-known path, on the outer app, ahead of
the `Mount` that forwards everything else to the SDK's sub-app underneath - the same
shadowing technique `auth.prm` already uses for Protected Resource Metadata (#35).

With CIMD disabled, the document this module builds is byte-identical to the SDK's
own, so shadowing it is a no-op in content, not just in effect.
"""

from __future__ import annotations

from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthMetadata

__all__ = ["WELL_KNOWN_PATH", "build_authorization_server_metadata"]

WELL_KNOWN_PATH = "/.well-known/oauth-authorization-server"

#: RFC 7591/the SDK already advertises `client_secret_post`/`client_secret_basic` for
#: confidential DCR clients - `"none"` is appended for CIMD's public clients, never
#: replacing what a confidential client still relies on.
_PUBLIC_CLIENT_AUTH_METHOD = "none"


def build_authorization_server_metadata(auth: AuthSettings, *, cimd_enabled: bool) -> OAuthMetadata:
    """The RFC 8414 document to serve at `WELL_KNOWN_PATH`, built from the same
    `AuthSettings` `http.py` passes to the SDK's own (shadowed) mounting."""
    metadata = build_metadata(
        auth.issuer_url,
        auth.service_documentation_url,
        # Same fallback `create_auth_routes` itself applies before calling `build_metadata` -
        # kept here too so this document matches the SDK's exactly even if `auth` ever arrives
        # with either left unset.
        auth.client_registration_options or ClientRegistrationOptions(),
        auth.revocation_options or RevocationOptions(),
        supports_identity_assertion=auth.identity_assertion_enabled,
    )
    if not cimd_enabled:
        return metadata

    existing_methods = metadata.token_endpoint_auth_methods_supported or []
    if _PUBLIC_CLIENT_AUTH_METHOD not in existing_methods:
        metadata.token_endpoint_auth_methods_supported = [
            *existing_methods,
            _PUBLIC_CLIENT_AUTH_METHOD,
        ]
    metadata.client_id_metadata_document_supported = True
    return metadata

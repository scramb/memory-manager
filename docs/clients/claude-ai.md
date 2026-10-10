# Connecting claude.ai to memory-manager

claude.ai (web and mobile) connects over the remote HTTP endpoint as a custom connector. The
examples use `https://memory.example.com`; replace it with your `PUBLIC_URL`.

Checked against the claude.ai custom connector flow on **2026-10-07**, against memory-manager
**0.1.2**. Command syntax and auth details also recorded in
[`docs/research/mcp-auth-and-connectors.md`](../research/mcp-auth-and-connectors.md#4-claudeai-custom-connectors-remote-mcp).

## Tested version

Verified 2026-10-07 against memory-manager 0.1.2: claude.ai (mobile web) connected through CIMD
and OIDC login, and wrote notes, which became commits authored by `claude-ai`.

## Setup

### Global

Before connecting:

- The server runs with `LOGIN_MODE=oidc` or `LOGIN_MODE=password`, `PUBLIC_URL` and
  `OAUTH_CLIENT_SECRET_KEY` (see `deploy/README.md`).
- `curl https://memory.example.com/.well-known/oauth-authorization-server` lists `S256` and
  `client_id_metadata_document_supported: true`.
- `curl -i -X POST https://memory.example.com/mcp` answers `401` with a `WWW-Authenticate` header
  that points to the protected resource metadata.
- With OIDC login, the account is on the allowlist (`OIDC_ALLOWED_EMAILS` or
  `OIDC_ALLOWED_SUBJECTS`), and the IdP reports the e-mail address as verified.

To connect:

1. Open **Settings → Connectors → Add custom connector**.
2. URL: `https://memory.example.com/mcp`. Leave client ID and secret empty, so claude.ai
   registers itself through a Client ID Metadata Document.
3. Select **Connect**. A memory-manager page names the client and its redirect host first;
   continue to the sign-in page and sign in.
4. In a chat with the connector enabled, ask Claude to remember something. Claude calls
   `memory_write`, and a commit authored by `claude-ai` appears in the vault repository.

### Project

claude.ai connectors are per account, not per project. On Team and Enterprise, an Owner adds the
connector once for the organisation, and members then connect with their own account (see
"Org rollout" below); there is no separate project scope on claude.ai itself.

## Auth variants

| Variant | Notes | Availability |
|---|---|---|
| `oauth_cimd` | Claude registers itself via a Client ID Metadata Document; the default in the connector dialog ("Use Claude's published identity") | Default |
| `oauth_dcr` | Automatic registration via RFC 7591 DCR; used when the AS does not advertise CIMD | Default |
| own OAuth client | "Use your own OAuth client" — enter a client ID registered with the server; leave the secret blank unless the AS requires one, in which case Claude acts as a public client | Custom connector dialog |
| `static_headers` | A fixed API key or bearer token sent as a header on every call, entered once per connector ("Request headers"); the credential is per organization, not per user, and `Authorization` cannot be set this way on an OAuth connection | **Beta, limited orgs** |
| `none` | Authless | Default |

A 401 is required to start sign-in; `WWW-Authenticate` on a 200 is ignored. Auth settings cannot
be edited after the connector is added — remove and re-add it to change them.

## Instructions file

claude.ai loads the server's MCP `instructions` and the `memory_guide` prompt automatically; no
separate instructions file is needed or supported for this client.

## Known limits

- Tool result size: **~150,000 characters**.
- Tool call timeout: **240 s**.
- Free plan: **one** custom connector.
- Requests reach the server **from Anthropic's cloud, not from the user's device**, even in
  Claude Desktop. A server that is not publicly reachable needs a tunnel; see
  [`../guides/cloudflare-tunnel.md`](../guides/cloudflare-tunnel.md) rather than allowlisting an
  IP range by hand.
- Not supported: resource subscriptions, sampling, `client_credentials` (M2M).

## Org rollout

On Team and Enterprise, an Owner adds the connector once; members then connect with their own
account and get their own OAuth session. The `static_headers` beta credential, where enabled,
is shared by the whole organisation rather than issued per member.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Authorization failed ("Autorisierung fehlgeschlagen") | the server's `/authorize` redirected back with an error | check the server log; since 0.1.2, CIMD rejections are logged as `CIMD client_id … rejected: <reason>` |
| `Client ID 'https://claude.ai/…' not found` | the server could not fetch claude.ai's metadata document | requires outbound HTTPS to `claude.ai`; fixed for IPv4-only clusters in 0.1.2 (#82) |
| `invalid_scope` | CIMD document without `scope` | fixed in 0.1.1 (#80) |
| sign-in page says access denied | account not on the allowlist, or e-mail not verified at the IdP | add the address or the IdP subject to the allowlist |
| scripts get HTTP 403 from a CDN in front of the server | bot protection blocks default library user agents | send a real `User-Agent`; claude.ai is unaffected |

`doctor --client claude-ai` is planned (#138) and will cover this connector once it ships.

## Check across clients

Save a fact in claude.ai ("Remember: my favourite editor is Zed"). Then ask Claude Code ("Which
editor do I prefer?"). Claude Code should call `memory_search` and find the note.

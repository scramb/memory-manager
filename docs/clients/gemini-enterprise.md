# Connecting Gemini Enterprise to memory-manager

Gemini Enterprise (formerly Agentspace) connects over the remote HTTP endpoint as a **custom MCP
server data store**, set up by an admin in the Google Cloud console. The examples use
`https://memory.example.com`; replace it with your `PUBLIC_URL`.

**Status: not checked against a live tenant.** memory-manager has no Gemini Enterprise tenant to
verify against; everything below is written from the vendor documentation retrieved
2026-10-11 — "Set up your custom MCP server data store"
([`docs.cloud.google.com`](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server),
page itself last updated 2026-10-09) and the Business-edition counterpart
([`support.google.com/g/answer/17106276`](https://support.google.com/g/answer/17106276)) — plus
inference from memory-manager's own authorization-server code, marked below wherever it is
untested live. Command syntax and sources also recorded in
[`../research/clients/gemini-app.md`](../research/clients/gemini-app.md).

## Tested version

Not verified. No "Verified" line exists yet; one is added once a tenant runs it.

## Setup

### Global

Before connecting, on the memory-manager server:

- `LOGIN_MODE=oidc` or `LOGIN_MODE=entra` — Gemini Enterprise's own OAuth client has no identity
  of its own; per-user attribution needs the server's own login to resolve each authorizing user,
  the same as claude.ai (see [`claude-ai.md`](./claude-ai.md) → Setup/Global).
- `PUBLIC_URL` and `OAUTH_CLIENT_SECRET_KEY` set (see `deploy/README.md`), the same prerequisite
  every OAuth client needs.
- An operator-registered confidential OAuth client (ADR-0015), because Gemini Enterprise's data
  store has no DCR and no CIMD support — only a fixed client id and secret an admin enters once:

  ```
  memory-manager oauth-client create gemini-enterprise \
    --redirect-uri https://vertexaisearch.cloud.google.com/oauth-redirect
  ```

  The redirect URI is fixed by Google, not an operator value. The command prints the client id
  and secret exactly once; store the secret the same way any other admin-facing secret is stored,
  it cannot be shown again (`memory-manager oauth-client list` shows metadata, never the secret).

  `--auth-method` defaults to `client_secret_post`. Gemini Enterprise's form does not say which
  method it uses for `/token`; if it turns out to send `Authorization: Basic`, re-create with
  `--auth-method client_secret_basic` — but note that this server's `/token` handler still reads
  `client_id` from the request body regardless of auth method, and 400s with "Missing client_id"
  if it is missing there (see `src/memory_manager/auth/registered_clients.py`'s module docstring).
  Google's console does not expose a way to add extra form fields beyond what is documented
  below, so whether it fills `client_id` into the body under `client_secret_basic` is untested.

- `curl https://memory.example.com/.well-known/oauth-protected-resource` — the admin needs the
  exact `resource` value from this document for the Authorization URL Parameters field below.

### Project

Gemini Enterprise has no per-project scope on memory-manager's side: one admin registers one data
store, organisation-wide; see "Org rollout" below.

To connect, in the Google Cloud console (Gemini Enterprise → data stores → Custom MCP server,
preview):

| Google console field | Value |
|---|---|
| MCP Server URL | `https://memory.example.com/mcp` |
| Authorization URL | `https://memory.example.com/authorize` |
| Authorization URL Parameters | `&resource=<URL-encoded resource>`, where `resource` is read from `https://memory.example.com/.well-known/oauth-protected-resource`. memory-manager's authorization server requires an RFC 8707 `resource` parameter on every `/authorize` request and rejects a request without one with `invalid_target` — derived from `src/memory_manager/auth/provider.py`, not confirmed against a live Gemini Enterprise request. |
| Token URL | `https://memory.example.com/token` |
| Client ID | from `oauth-client create`'s output |
| Client Secret | from `oauth-client create`'s output, shown once |
| Enable PKCE Support | **On.** This server's authorization server only accepts `code_challenge_method=S256` (the MCP SDK hard-codes `"S256"`); a request without PKCE or with `plain` fails. The Business edition's setup page has no PKCE checkbox at all — see "Known limits". |
| Scopes | `memory:read memory:write` exactly — **not** `offline_access`. This server rejects a scope it does not recognise with `invalid_scope`, so Google's own "a common scope is `offline_access`" suggestion does not apply here. A refresh token is issued on every authorization regardless of requested scope (access token 1 hour, refresh token 30 days), so there is nothing to additionally request. |

After the form, click **Verify Auth**, then continue through org policy, egress allowlisting and
action enablement (see "Known limits" and "Org rollout").

## Auth variants

| Variant | Notes | Availability |
|---|---|---|
| pre-registered confidential client | the only variant Gemini Enterprise's custom MCP data store supports against this server; client id + secret from `oauth-client create` (ADR-0015) | Default and only option |

Gemini Enterprise also offers **No authentication** in its own form, but memory-manager's `/mcp`
endpoint always requires a bearer token (ADR-0004) — "No authentication" is not a usable variant
against this server. There is no DCR, no CIMD, and no static-header variant on Gemini
Enterprise's side.

Each user still authorizes individually on first call — the admin-registered client identifies
the *application*, not the end user; `LOGIN_MODE=oidc`/`entra` resolves each user's own identity
at `/authorize` the same way it does for claude.ai.

## Instructions file

Whether Gemini Enterprise's agents load the server's MCP `instructions` automatically is
`[unverified]` — not documented on the setup page read for this page, and there is no tenant here
to check live. Agent Designer, Gemini Enterprise's no-code agent builder, lets an admin or user
write agent instructions that could carry the `memory_guide` workflow by hand; this is reported
only by a secondary source (see [`../research/clients/gemini-app.md`](../research/clients/gemini-app.md) [G12] `[secondary]`),
not confirmed.

## Known limits

- **≤ 100 enabled actions per data store.** All tools start disabled; an admin enables them
  individually and clicks "Reload custom actions". memory-manager exposes 8 MCP tools today, far
  under the cap, but every future tool addition shares this one budget with whatever else the
  same data store exposes.
- **Write tools ask for confirmation unless `readOnlyHint: true`.** Google's console recommends
  tagging read-only tools with `readOnlyHint` to skip the per-call confirmation; tools without it
  show a confirmation prompt to the end user on every call, including memory-manager's write
  tools. This is Google's behaviour, not something memory-manager's tool annotations currently
  drive one way or the other.
- **Business edition is likely incompatible.** Its setup page has no PKCE field, and this
  server's authorization server requires PKCE (S256) on every authorization request; a Business
  edition tenant may not be able to complete the flow at all. Unconfirmed — no Business tenant to
  test against.
- **Writes commit as `gemini-enterprise`.** `_client_label_for`
  (`src/memory_manager/auth/provider.py`) recognizes the registered client's redirect URI host,
  `vertexaisearch.cloud.google.com` — fixed by Google, never supplied by the client itself — and
  attributes every write made through it to `gemini-enterprise`, both in the audit log and as the
  commit author in the vault's Git history (#322).
- **No compatibility profile registered for this client.** There is no live connection to source
  a `clientInfo.name` from, the vendor-documented action cap (≤ 100) is far above memory-manager's
  8 tools, and the token-endpoint auth method Gemini Enterprise actually sends is unknown — a
  profile would be guessing at a shape nobody has observed. `default` applies until a live test
  exists.

## Org rollout

One admin registers one `oauth-client` and one Gemini Enterprise data store for the whole
organisation (no per-user or per-project registration on either side). The admin enables the
data store's actions and attaches it to one or more Gemini Enterprise apps; from there, every
user who is allowed to use that app authorizes individually on first call, through
memory-manager's own `LOGIN_MODE=oidc`/`entra` login. To revoke organisation-wide access, run
`memory-manager oauth-client revoke <client-id>` — this also soft-revokes every token the client
has issued, so no silent re-use of an access token already in flight is possible after revocation.

Org policy must allow custom MCP data stores before the console form in "Setup/Project" is even
reachable; if the org runs with VPC Service Controls, the project's egress allowlist
(`allowedEgressFqdns`) and `allowedDataSources` org-policy constraints must include
memory-manager's own host and whatever host issues its OAuth tokens (the same host here, since
the authorization server is embedded).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `invalid_target` on `/authorize` | the Authorization URL Parameters field is missing `&resource=...`, or the value does not match this server's protected-resource metadata exactly | set it from `curl https://memory.example.com/.well-known/oauth-protected-resource`, URL-encoded |
| `invalid_scope` | Scopes field contains `offline_access` or any scope other than `memory:read`/`memory:write` | set Scopes to exactly `memory:read memory:write` |
| `invalid_client`, or `/token` responds "Missing client_id" | the registered auth method does not match what Gemini Enterprise sends, or the client id/secret was mistyped | re-check against `memory-manager oauth-client list`; re-create with `--auth-method client_secret_basic` if Basic auth is confirmed needed |
| PKCE-related rejection at `/authorize` or `/token` | PKCE not enabled in the console form, or the authorization server received `code_challenge_method=plain` | enable "Enable PKCE Support"; this server only accepts S256 |

`doctor --client gemini-enterprise` is not available for this client — there is no local config
file on the memory-manager side for `doctor` to check, and no tenant here to verify the live
connection against either. Verify the connection directly in the Google Cloud console instead
(the "Verify Auth" step in "Setup/Project" above).

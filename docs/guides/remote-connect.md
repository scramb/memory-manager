# Connect claude.ai and Claude Code to a deployed server

This guide connects both clients to one deployed memory-manager over HTTPS with OAuth. Once both are connected, a note saved in one client can be found from the other. The examples use `https://memory.example.com`; replace it with your `PUBLIC_URL`.

## Before you start

- The server runs with `LOGIN_MODE=oidc` or `LOGIN_MODE=password`, `PUBLIC_URL` and `OAUTH_CLIENT_SECRET_KEY` (see `deploy/README.md`).
- `curl https://memory.example.com/.well-known/oauth-authorization-server` lists `S256` and `client_id_metadata_document_supported: true`.
- `curl -i -X POST https://memory.example.com/mcp` answers `401` with a `WWW-Authenticate` header that points to the protected resource metadata.
- With OIDC login, your account is on the allowlist (`OIDC_ALLOWED_EMAILS` or `OIDC_ALLOWED_SUBJECTS`). The IdP must report the e-mail address as verified.

## claude.ai (web and mobile)

1. Open **Settings → Connectors → Add custom connector**.
2. URL: `https://memory.example.com/mcp`. Leave client ID and secret empty, so claude.ai registers itself through a Client ID Metadata Document.
3. Select **Connect**. You first see a memory-manager page that names the client and its redirect host. Continue to your sign-in page and sign in.
4. In a chat with the connector enabled, ask Claude to remember something. Claude calls `memory_write`, and a commit authored by `claude-ai` appears in the vault repository.

## Claude Code

```bash
claude mcp add --transport http --scope user memory https://memory.example.com/mcp
```

In a session, run `/mcp`, choose `memory`, then **Authenticate**. The sign-in opens in the browser. Without a browser, use a static token instead:

```bash
memory-manager token create my-laptop --scope memory:read --scope memory:write --namespace '*'
claude mcp add --transport http --scope user memory https://memory.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

To keep the rules in reach, install the skill (`integrations/claude-code/README.md`).

## Check across clients

Save a fact in claude.ai ("Remember: my favourite editor is Zed"). Then ask Claude Code ("Which editor do I prefer?"). Claude Code should call `memory_search` and find the note.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| claude.ai: "Autorisierung fehlgeschlagen" / authorization failed | the server's `/authorize` redirected back with an error | check the server log; since 0.1.2, CIMD rejections are logged as `CIMD client_id … rejected: <reason>` |
| `Client ID 'https://claude.ai/…' not found` | the server could not fetch claude.ai's metadata document | requires outbound HTTPS to `claude.ai`; fixed for IPv4-only clusters in 0.1.2 (#82) |
| `invalid_scope` | CIMD document without `scope` | fixed in 0.1.1 (#80) |
| sign-in page says access denied | account not on the allowlist, or e-mail not verified at the IdP | add the address or the IdP subject to the allowlist |
| scripts get HTTP 403 from a CDN in front of the server | bot protection blocks default library user agents | send a real `User-Agent`; claude.ai and Claude Code are unaffected |

## Verified

The owner verified this on 2026-10-07 against memory-manager 0.1.2. claude.ai (mobile web) connected through CIMD and OIDC login, and wrote notes, which became commits authored by `claude-ai`. Claude Code connected over HTTP with OAuth and found those notes through `memory_search`.

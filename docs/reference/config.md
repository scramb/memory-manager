# Config reference

> **Stable since 1.0.** Nothing on this page carries any compatibility guarantee before
> `1.0.0` ships - every `0.x` release may still change it without notice. See
> [../compatibility.md](../compatibility.md) (ADR-0017) for what "Stable" means from
> `1.0.0` on.

Every environment variable `memory-manager` reads, grouped by area, with its default
and purpose. Hand-written, but drift-checked both ways by `tests/reference_docs.py`
(`make lint` / `make reference-docs`): every name below is actually read somewhere in
[`../../src/memory_manager/`](../../src/memory_manager/), and every name read there
appears below (a short, explicitly commented exclusion list in
`tests/reference_docs.py` covers the handful of names that are deliberately not public
config - a client-side env var name, build metadata, a CI-only fallback). Only the
*name* and whether it is documented at all is checked; the default/purpose text in this
table is not verified against the source and can drift until someone notices - read the
linked source file for the authoritative value if in doubt.

File formats a variable points at (`BLOCKLIST_FILE`, `ERASURE_LOG_REPLAY_FILE`,
`LOGIN_NAMESPACE_MAP`) are Stable too (ADR-0017); the blocklist file's own schema is
documented in [../guides/blocklist.md](../guides/blocklist.md), not repeated here.

## Vault (`git` backend)

Read by [`../../src/memory_manager/config.py`](../../src/memory_manager/config.py)'s
`VaultConfig.from_env`, only when `STORAGE_BACKEND=git` (the default).

| Variable | Default | Purpose |
|---|---|---|
| `VAULT_REMOTE` | *(required)* | the git remote `memory-manager` clones and pushes to |
| `VAULT_DIR` | *(required)* | local working copy path for the clone |
| `VAULT_BRANCH` | `main` | branch the vault lives on |
| `VAULT_SSH_KEY_FILE` | unset | SSH private key file, for an `ssh://`/`git@` remote |
| `VAULT_SSH_KNOWN_HOSTS` | unset | known-hosts file checked against that remote's host key |
| `VAULT_HTTPS_TOKEN` | unset | bearer token for an `https://` remote that needs one |
| `VAULT_POLL_SECONDS` | `60` | how often a `serve --http` process without a webhook polls the remote for changes made elsewhere |
| `VAULT_WEBHOOK_SECRET` | unset | HMAC secret `/hooks/vault` checks a push notification's signature against (unrelated to git access itself) |

## Storage backend

Read by `config.py`'s `storage_backend_from_env`/`database_app_role_from_env`/
`break_glass_approvers_from_env`/`static_token_max_days_from_env`/
`personal_token_max_days_from_env`/`blocklist_file_from_env`/
`erasure_log_replay_file_from_env`.

| Variable | Default | Purpose |
|---|---|---|
| `STORAGE_BACKEND` | `git` | `git` or `postgres` (ADR-0007); the source of truth for this deployment |
| `DATABASE_URL` | unset | Postgres connection string; required when `STORAGE_BACKEND=postgres`, optional under `git` (adds search/index) |
| `DATABASE_APP_ROLE` | unset | non-owner role request transactions switch to under row-level security; required when `STORAGE_BACKEND=postgres` |
| `BREAK_GLASS_APPROVERS` | `2` | how many distinct admins a break-glass grant needs (`1` or `2`, ADR-0008) |
| `STATIC_TOKEN_MAX_DAYS` | `90` | enterprise static-token expiry ceiling (ADR-0006 §7) |
| `PERSONAL_TOKEN_MAX_DAYS` | `90` | personal-token expiry ceiling (ADR-0012) |
| `BLOCKLIST_FILE` | unset | path to a TOML operator blocklist (off when unset) |
| `ERASURE_LOG_REPLAY_FILE` | unset | path to an exported erasure-log file to replay after a backup restore (ADR-0007 §3 addendum) |

## Embeddings

Read by `config.py`'s `EmbeddingConfig.from_env`.

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_PROVIDER` | `none` | `none` (full-text only), `ollama` or `openai` |
| `EMBEDDING_URL` | *(required unless provider is `none`)* | the provider's endpoint |
| `EMBEDDING_MODEL` | `bge-m3` for `ollama`, required for `openai` | embedding model name |
| `EMBEDDING_API_KEY` | unset | bearer credential for the provider, if it needs one |
| `EMBEDDING_DIMENSIONS` | unset | expected vector width; pinned at first migration (ADR-0016) |

## Audit

| Variable | Default | Purpose |
|---|---|---|
| `AUDIT_EXPORT` | `off` | comma-separated export targets for every audit record: `stdout`, `otlp`, or `off` |

## HTTP server

Read by `config.py`'s `ServerConfig.from_env`, for `memory-manager serve --http`.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | bind address |
| `PORT` | `8080` | bind port |
| `PUBLIC_URL` | unset | this server's externally reachable origin; required once bearer-token auth is enabled |
| `MCP_PATH` | `/mcp` | mount path of the Streamable HTTP transport |
| `ALLOWED_ORIGINS` | unset | comma-separated list of browser origins allowed, beyond `PUBLIC_URL`'s own |
| `MCP_JSON_RESPONSE` | `true` | whether `/mcp` responds with plain JSON instead of an SSE stream |
| `LOGIN_MODE` | unset | which embedded OAuth login the authorization server offers: `password`, `oidc` or `entra` |
| `OAUTH_CLIENT_SECRET_KEY` | unset | Fernet key a dynamically registered client's secret is encrypted with at rest; required once the OAuth authorization server is on |
| `VALKEY_URL` | unset | `redis://`/`rediss://`/`unix://` Valkey instance backing rate limits and pending login state, instead of Postgres (ADR-0009 §2) |
| `CIMD_ENABLED` | `true` | whether Client ID Metadata Document registration is offered alongside dynamic client registration |
| `MAX_REQUEST_BYTES` | `1048576` | body-size cap on the Streamable HTTP mount |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | proxy IPs/CIDRs trusted to set the `X-Forwarded-For` header used for rate-limit keys |
| `SHUTDOWN_GRACE_SECONDS` | `20` | how long `serve --http`/`worker` keep draining in-flight requests after a termination signal |
| `MM_ALLOW_UNAUTHENTICATED` | unset (off) | set to `1` to let `serve --http` bind to a non-loopback host with no `DATABASE_URL`/bearer auth configured |

## Rate limiting

Each pair feeds one token-bucket limiter (`RATE_LIMIT_*_PER_MINUTE` is the refill rate,
`RATE_LIMIT_*_BURST` the bucket capacity).

| Variable | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_MCP_PER_MINUTE` | `120` | every request to `MCP_PATH` |
| `RATE_LIMIT_MCP_BURST` | `30` | burst capacity for the above |
| `RATE_LIMIT_WRITE_PER_MINUTE` | `30` | every write tool call, counted additionally against this tighter pair |
| `RATE_LIMIT_WRITE_BURST` | `10` | burst capacity for the above |
| `RATE_LIMIT_OAUTH_PER_MINUTE` | `30` | every request to `/register`, `/token`, `/authorize` |
| `RATE_LIMIT_OAUTH_BURST` | `10` | burst capacity for the above |
| `RATE_LIMIT_WEBHOOK_PER_MINUTE` | `30` | every request to the vault webhook |
| `RATE_LIMIT_WEBHOOK_BURST` | `10` | burst capacity for the above |

## Write and storage quotas (#242, #243)

Each defaults to `0`, which means off - a deployment opts a scope in explicitly.

| Variable | Default | Purpose |
|---|---|---|
| `QUOTA_WRITES_PER_MINUTE_USER` | `0` | writes per minute, keyed by the caller's own identity (`postgres` mode only) |
| `QUOTA_WRITES_PER_DAY_USER` | `0` | writes per day, same key |
| `QUOTA_WRITES_PER_MINUTE_NAMESPACE` | `0` | writes per minute, keyed by the target namespace |
| `QUOTA_WRITES_PER_DAY_NAMESPACE` | `0` | writes per day, same key |
| `QUOTA_WRITES_PER_MINUTE_TOKEN` | `0` | writes per minute, keyed by the bearer token |
| `QUOTA_WRITES_PER_DAY_TOKEN` | `0` | writes per day, same key |
| `QUOTA_MAX_NOTES_PERSONAL` | `0` | note-count budget for the caller's own namespace (`postgres` mode only) |
| `QUOTA_MAX_BYTES_PERSONAL` | `0` | byte-size budget for the caller's own namespace |
| `QUOTA_MAX_NOTES_SHARED` | `0` | note-count budget across every group/project/org namespace |
| `QUOTA_MAX_BYTES_SHARED` | `0` | byte-size budget across every group/project/org namespace |

## Worker (`memory-manager worker`)

Read by `config.py`'s `WorkerConfig.from_env`; requires `STORAGE_BACKEND=postgres`.

| Variable | Default | Purpose |
|---|---|---|
| `WORKER_HOST` | `127.0.0.1` | bind address for the worker's own health endpoint |
| `WORKER_PORT` | `8090` | bind port for the above |
| `JOBS_POLL_SECONDS` | `5` | fallback poll interval for the `jobs` outbox consumer |
| `ENTRA_DELTA_SYNC_SECONDS` | `300` | how often the Entra group delta sync job runs, when configured |
| `PERSONAL_RETENTION_DAYS` | `30` | days after deprovisioning before a user's personal namespace and identity are erased (ADR-0008) |
| `RETENTION_SWEEP_SECONDS` | `86400` | how often the retention job checks for deprovisioned users past that horizon |

## Login: password mode (`LOGIN_MODE=password`)

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD_HASH` | *(required)* | hash of the one admin password, from `memory-manager hash-password` |
| `LOGIN_NAMESPACES` | `*` (every namespace) | comma-separated default namespaces a logged-in principal gets, absent a `LOGIN_NAMESPACE_MAP` entry |
| `LOGIN_NAMESPACE_MAP` | unset | JSON object mapping a subject or email to its own list of namespaces |

## Login: OIDC mode (`LOGIN_MODE=oidc`)

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | *(required)* | upstream OpenID Connect issuer URL |
| `OIDC_CLIENT_ID` | *(required)* | this server's client id at that issuer |
| `OIDC_CLIENT_SECRET` | *(required)* | this server's client secret at that issuer |
| `OIDC_ALLOWED_EMAILS` | unset | comma-separated allowlist of emails; at least one of this or `OIDC_ALLOWED_SUBJECTS` is required |
| `OIDC_ALLOWED_SUBJECTS` | unset | comma-separated allowlist of subject claims |

## Login: Entra mode (`LOGIN_MODE=entra`, ADR-0006)

| Variable | Default | Purpose |
|---|---|---|
| `ENTRA_TENANT_ID` | *(required)* | Microsoft Entra tenant id |
| `ENTRA_CLIENT_ID` | *(required)* | this server's application (client) id |
| `ENTRA_CLIENT_SECRET` | *(required)* | this server's client secret |
| `ENTRA_ALLOWED_TENANTS` | `ENTRA_TENANT_ID` itself | comma-separated allowlist of tenant ids a token may come from |
| `ENTRA_AUTHORITY` | `https://login.microsoftonline.com` | Microsoft identity platform authority base URL |
| `ENTRA_GRAPH_URL` | `https://graph.microsoft.com/v1.0` | Microsoft Graph base URL for group membership lookups |
| `ENTRA_ALLOW_INSECURE_AUTHORITY` | `false` | allow a non-`https://` `ENTRA_AUTHORITY`/`ENTRA_GRAPH_URL` (test doubles only) |
| `ENTRA_GROUPS_TTL_SECONDS` | `3600` | how long a user's cached group membership is trusted before a Graph refresh |
| `ENTRA_ACCESS_TOKEN_MINUTES` | `15` | lifetime of this server's own access token to Graph |
| `ENTRA_MAX_SESSION` | `43200` (12 hours) | maximum session lifetime before a login must be repeated |

## Committer identity

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_CLIENT` | `claude-code` | the committer identity a stdio session with no OAuth access token writes as |

## Observability

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | "INFO" | minimum log level emitted |
| `LOG_FORMAT` | `json` | `json` (structured) or `text` |
| `METRICS_ENABLED` | `true` | whether `/metrics` serves the Prometheus text exposition format at all |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP collector endpoint; tracing is off until this is set |

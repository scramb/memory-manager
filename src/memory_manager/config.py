# SPDX-License-Identifier: AGPL-3.0-only
"""Vault, embedding and HTTP server configuration, read from the process environment.

`VaultConfig`, `EmbeddingConfig` and `ServerConfig` are the one place that
turn `VAULT_*`, `EMBEDDING_*` and the HTTP server's environment variables
into typed, validated configuration objects. Credentials (`https_token`,
`api_key`, `webhook_secret`) never appear in `repr()`/`str()` output, so
any of the three can be logged safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "AuditConfigError",
    "EmbeddingConfig",
    "EmbeddingConfigError",
    "EmbeddingDimensionPinError",
    "ServerConfig",
    "ServerConfigError",
    "StorageConfigError",
    "VaultConfig",
    "VaultConfigError",
    "WorkerConfig",
    "WorkerConfigError",
    "audit_export_targets_from_env",
    "blocklist_file_from_env",
    "canonical_resource_url",
    "database_app_role_from_env",
    "rate_limit_sweep_floor_seconds",
    "storage_backend_from_env",
]

_DEFAULT_BRANCH = "main"
_DEFAULT_POLL_SECONDS = 60
_DEFAULT_EMBEDDING_PROVIDER = "none"
_EMBEDDING_PROVIDERS = ("none", "ollama", "openai")
_DEFAULT_OLLAMA_MODEL = "bge-m3"
_DEFAULT_STORAGE_BACKEND = "git"
_STORAGE_BACKENDS = ("git", "postgres")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_DEFAULT_MCP_PATH = "/mcp"
_FALSY_BOOL_ENV = frozenset({"0", "false", "no", "off", ""})

# `memory-manager worker`'s own minimal Starlette app (#217, ADR-0009 §4) - a
# different default port than `_DEFAULT_PORT` (the `api` process's), so an
# operator running both on one host (or one `podman`/compose network) never
# has to pick one before the other binds.
_DEFAULT_WORKER_HOST = "127.0.0.1"
_DEFAULT_WORKER_PORT = 8090

# `WorkerConfig.jobs_poll_seconds` (#218): docs/research/enterprise.md
# §"Queue" names 1-5s polling as the `jobs` outbox's NOTIFY fallback.
_DEFAULT_JOBS_POLL_SECONDS = 5.0

# `WorkerConfig.entra_delta_sync_seconds` (#223, ADR-0006 §6): "a Graph delta
# query every 5 min (configurable), worst case bounded by the 15-min access
# token".
_DEFAULT_ENTRA_DELTA_SYNC_SECONDS = 300.0

# Rate-limit/body-size defaults (#39). Per-minute figures are refill rates;
# "burst" is the token bucket's capacity - how many calls a key can make
# back-to-back before the per-minute rate takes over. See `ServerConfig`'s
# docstring for which route class each pair gates.
_DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
_DEFAULT_MCP_PER_MINUTE = 120.0
_DEFAULT_MCP_BURST = 30.0
_DEFAULT_WRITE_PER_MINUTE = 30.0
_DEFAULT_WRITE_BURST = 10.0
_DEFAULT_OAUTH_PER_MINUTE = 30.0
_DEFAULT_OAUTH_BURST = 10.0
_DEFAULT_WEBHOOK_PER_MINUTE = 30.0
_DEFAULT_WEBHOOK_BURST = 10.0
_DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"

# Write quota defaults (#242, ADR-0009 §2). All six default to 0, which `quotas.
# QuotaChecker` reads as "off" - a deployment opts in per scope and per window
# explicitly, unlike `RATE_LIMIT_*` above, which is always on.
_DEFAULT_QUOTA_WRITES_PER_MINUTE = 0.0
_DEFAULT_QUOTA_WRITES_PER_DAY = 0.0

# Storage quota defaults (#243): a namespace's note-count/byte-size budget,
# Postgres mode only. All four default to 0, same "off unless a deployment
# opts in" meaning as the `QUOTA_WRITES_*` defaults above - see `quotas.
# StorageQuotaChecker`'s own docstring for what "personal"/"shared" mean.
_DEFAULT_QUOTA_MAX_NOTES = 0
_DEFAULT_QUOTA_MAX_BYTES = 0
# The fixed brute-force window `auth.login_password`'s own `_WINDOW_SECONDS`
# uses (ADR-0004: "5 failures / 10 min") - duplicated here rather than
# imported, since that name is private to its own module.
# `rate_limit_sweep_floor_seconds` below folds it into the sweep's threshold,
# so a running brute-force window is never swept away mid-window either.
_LOGIN_BRUTE_FORCE_WINDOW_SECONDS = 10 * 60.0

# Graceful-shutdown grace period (ADR-0009 §5, #105). uvicorn's own default for
# `timeout_graceful_shutdown` is `None` (wait forever); this picks a bounded
# value instead, so `SIGKILL` from an orchestrator is never what actually ends
# a draining process.
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 20


class VaultConfigError(ValueError):
    """A required `VAULT_*` environment variable is missing or invalid."""


@dataclass(frozen=True)
class VaultConfig:
    """Configuration for the vault's git remote and local working copy."""

    remote: str
    dir: Path
    branch: str = _DEFAULT_BRANCH
    ssh_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None
    https_token: str | None = field(default=None, repr=False)
    poll_seconds: int = _DEFAULT_POLL_SECONDS

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> VaultConfig:
        """Build a `VaultConfig` from `VAULT_*` entries of `environ`.

        Raises `VaultConfigError` with a message naming the offending
        variable if a required value is missing or malformed.
        """
        remote = _require(environ, "VAULT_REMOTE", VaultConfigError)
        vault_dir = _require(environ, "VAULT_DIR", VaultConfigError)
        branch = environ.get("VAULT_BRANCH", _DEFAULT_BRANCH)
        ssh_key_file_raw = environ.get("VAULT_SSH_KEY_FILE")
        ssh_known_hosts_file_raw = environ.get("VAULT_SSH_KNOWN_HOSTS")
        https_token = environ.get("VAULT_HTTPS_TOKEN")
        poll_seconds_raw = environ.get("VAULT_POLL_SECONDS")

        poll_seconds = _DEFAULT_POLL_SECONDS
        if poll_seconds_raw is not None:
            try:
                poll_seconds = int(poll_seconds_raw)
            except ValueError as exc:
                raise VaultConfigError(
                    f"VAULT_POLL_SECONDS must be an integer, got {poll_seconds_raw!r}"
                ) from exc
            if poll_seconds <= 0:
                raise VaultConfigError(f"VAULT_POLL_SECONDS must be positive, got {poll_seconds}")

        return cls(
            remote=remote,
            dir=Path(vault_dir),
            branch=branch,
            ssh_key_file=Path(ssh_key_file_raw) if ssh_key_file_raw else None,
            ssh_known_hosts_file=(
                Path(ssh_known_hosts_file_raw) if ssh_known_hosts_file_raw else None
            ),
            https_token=https_token,
            poll_seconds=poll_seconds,
        )


def _require(environ: dict[str, str], name: str, error: type[ValueError]) -> str:
    value = environ.get(name)
    if not value:
        raise error(f"{name} is required but not set")
    return value


class StorageConfigError(VaultConfigError):
    """`STORAGE_BACKEND` is set to a backend this build does not implement.

    A subclass of `VaultConfigError` (not a sibling `ValueError`), so every
    caller that already catches `VaultConfigError` to report a config
    problem with exit code 2 (`cli.py`'s `serve`/`import` commands) keeps
    doing so unchanged for this error too.
    """


def storage_backend_from_env(environ: dict[str, str]) -> str:
    """The configured storage backend's name (`STORAGE_BACKEND`, default `"git"`).

    `"git"` (`memory_manager.storage.git.GitBackend`) and `"postgres"`
    (`memory_manager.storage.postgres.PostgresBackend`, enterprise mode,
    ADR-0007 §2, WP-18) are both implemented; any other value raises
    `StorageConfigError` - the same `EMBEDDING_PROVIDER` pattern
    `EmbeddingConfig.from_env` uses above. `"postgres"` additionally
    requires `DATABASE_URL`: there is no vault to fall back to in that
    mode, so this raises `StorageConfigError` naming it if it is missing,
    before anything is migrated, connected to or cloned.
    """
    backend = environ.get("STORAGE_BACKEND", _DEFAULT_STORAGE_BACKEND)
    if backend not in _STORAGE_BACKENDS:
        raise StorageConfigError(
            f"STORAGE_BACKEND must be one of {_STORAGE_BACKENDS}, got {backend!r}"
        )
    if backend == "postgres" and not environ.get("DATABASE_URL"):
        raise StorageConfigError(
            "DATABASE_URL is required when STORAGE_BACKEND=postgres (ADR-0007 §2): "
            "the postgres backend is the source of truth and has no vault to fall back to"
        )
    return backend


def database_app_role_from_env(environ: dict[str, str]) -> str | None:
    """The non-owner role (`DATABASE_APP_ROLE`) request transactions switch to, or
    `None` for `STORAGE_BACKEND=git`, which has no row-level security at all.

    Required when `STORAGE_BACKEND=postgres` (ADR-0008 addendum "system identity
    under FORCE RLS", #116): every request transaction that touches content must
    switch to this role and the caller's identity before touching a
    `FORCE ROW LEVEL SECURITY` table (`db.rls.request_connection`) - raises
    `StorageConfigError` naming the variable if it is missing, before anything is
    migrated, connected to or granted. Checked, not merely read: callers of
    `app.open_services` rely on this raising before a connection pool is opened,
    the same "fails before anything is started" contract `storage_backend_from_env`
    already gives `STORAGE_BACKEND`/`DATABASE_URL`. Only `open_services` (the
    request-serving entry point) calls this - `open_storage` (the import CLI) and
    `cli.py`'s `reindex` command are system jobs that keep connecting as the owner
    (ADR-0008 addendum) and need no app role at all.
    """
    if environ.get("STORAGE_BACKEND", _DEFAULT_STORAGE_BACKEND) != "postgres":
        return None
    role = environ.get("DATABASE_APP_ROLE")
    if not role:
        raise StorageConfigError(
            "DATABASE_APP_ROLE is required when STORAGE_BACKEND=postgres (ADR-0008 "
            "addendum): the non-owner role every request transaction must switch to "
            "before touching a row-level-security-protected content table"
        )
    return role


def blocklist_file_from_env(environ: dict[str, str]) -> Path | None:
    """The configured operator blocklist file (`BLOCKLIST_FILE`), or `None`.

    `None` - the default, nothing set - means no blocklist at all:
    `vault.blocklist.check` is then a no-op, same "off unless a deployment
    opts in" behaviour the `QUOTA_*` variables above have. Unlike those,
    there is nothing to validate here beyond "is a value set" - whether the
    file at that path actually exists and compiles is `vault.blocklist.
    load_rules`'s job, called eagerly at startup (`app.open_storage`/
    `open_services`) so a malformed file refuses startup there, not here.
    """
    value = environ.get("BLOCKLIST_FILE")
    return Path(value) if value else None


class EmbeddingConfigError(ValueError):
    """A required `EMBEDDING_*` environment variable is missing or invalid."""


class EmbeddingDimensionPinError(EmbeddingConfigError):
    """`EMBEDDING_DIMENSIONS` (or a provider's actual response) disagrees with the
    dimension a Postgres-mode backend was first migrated with (PLAN O23,
    ADR-0016, #220: pinned at first `migrate(..., backend="postgres")`, immutable
    afterwards). Raised by `db.migrate.migrate` at startup and by
    `index.indexer.Indexer` the first time a provider's own response disagrees -
    a subclass of `EmbeddingConfigError` so every caller that already catches
    that broadly (`cli.py`'s command dispatch) reports this the same way,
    with exit code 2, rather than an unhandled traceback.
    """


@dataclass(frozen=True)
class EmbeddingConfig:
    """Configuration for the embedding provider used while indexing.

    `provider="none"` (the default) is the "no provider = full-text only"
    case (`CLAUDE.md`/PLAN: every embedding API is optional and pluggable);
    `url` and `model` are then both `None` and unused.
    """

    provider: str
    url: str | None = None
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    dimensions: int | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> EmbeddingConfig:
        """Build an `EmbeddingConfig` from `EMBEDDING_*` entries of `environ`.

        Raises `EmbeddingConfigError` with a message naming the offending
        variable if a required value is missing or malformed.
        """
        provider = environ.get("EMBEDDING_PROVIDER", _DEFAULT_EMBEDDING_PROVIDER)
        if provider not in _EMBEDDING_PROVIDERS:
            raise EmbeddingConfigError(
                f"EMBEDDING_PROVIDER must be one of {_EMBEDDING_PROVIDERS}, got {provider!r}"
            )
        if provider == "none":
            return cls(provider="none")

        url = _require(environ, "EMBEDDING_URL", EmbeddingConfigError)

        model = environ.get("EMBEDDING_MODEL") or (
            _DEFAULT_OLLAMA_MODEL if provider == "ollama" else None
        )
        if not model:
            raise EmbeddingConfigError(
                "EMBEDDING_MODEL is required when EMBEDDING_PROVIDER is 'openai'"
            )

        dimensions_raw = environ.get("EMBEDDING_DIMENSIONS")
        dimensions = None
        if dimensions_raw is not None:
            try:
                dimensions = int(dimensions_raw)
            except ValueError as exc:
                raise EmbeddingConfigError(
                    f"EMBEDDING_DIMENSIONS must be an integer, got {dimensions_raw!r}"
                ) from exc
            if dimensions <= 0:
                raise EmbeddingConfigError(
                    f"EMBEDDING_DIMENSIONS must be positive, got {dimensions}"
                )

        return cls(
            provider=provider,
            url=url,
            model=model,
            api_key=environ.get("EMBEDDING_API_KEY"),
            dimensions=dimensions,
        )


class ServerConfigError(ValueError):
    """An HTTP server environment variable is missing or invalid."""


class AuditConfigError(ServerConfigError):
    """`AUDIT_EXPORT` names an unknown target, or `otlp` without the `otel` extra.

    A subclass of `ServerConfigError` (not a sibling `ValueError`), so every
    caller that already catches `ServerConfigError` to report a startup
    config problem with exit code 2 (`cli.py`'s `_serve`) keeps doing so
    unchanged for this error too - the same reasoning `StorageConfigError`
    gives above for `VaultConfigError`.
    """


#: `AUDIT_EXPORT` targets this build can export to (#245) - `AuditWriter`
#: (`audit.py`) picks the matching `observability.audit_export.AuditExporter`
#: for whichever of these are named; any other value is a config error.
_AUDIT_EXPORT_TARGETS = frozenset({"stdout", "otlp"})
_DEFAULT_AUDIT_EXPORT = "off"


def audit_export_targets_from_env(environ: dict[str, str]) -> frozenset[str]:
    """The `AUDIT_EXPORT` targets to export every audit record to (#245).

    `AUDIT_EXPORT` is a comma-separated list drawn from `_AUDIT_EXPORT_TARGETS`
    (`"stdout"`, `"otlp"`), or `"off"` (the default): the empty `frozenset`
    means "export nothing", same as unset. Raises `AuditConfigError` naming
    the offending value if any entry is not one of these - whether `otlp`
    additionally needs the `otel` extra installed is not checked here (that
    happens only once `observability.audit_export.AuditExporter.from_env`
    actually tries to build the OTLP exporter, so a `"git"`/no-`otlp`
    deployment never needs the extra installed at all to pass this check).
    """
    raw = environ.get("AUDIT_EXPORT", _DEFAULT_AUDIT_EXPORT).strip()
    if not raw or raw == _DEFAULT_AUDIT_EXPORT:
        return frozenset()
    targets = frozenset(entry.strip() for entry in raw.split(",") if entry.strip())
    unknown = targets - _AUDIT_EXPORT_TARGETS
    if unknown:
        raise AuditConfigError(
            f"AUDIT_EXPORT must be 'off' or a comma-separated list drawn from "
            f"{sorted(_AUDIT_EXPORT_TARGETS)}, got {raw!r}"
        )
    return targets


@dataclass(frozen=True)
class ServerConfig:
    """Configuration for `memory-manager serve --http` (the Streamable HTTP transport, #33).

    `allowed_origins` is already the fully resolved set a request's `Origin`
    header is checked against - `ALLOWED_ORIGINS` plus `public_url`'s own
    origin, never just the raw `ALLOWED_ORIGINS` env value - so `http.py`
    never has to re-derive it.

    `login_mode` (`LOGIN_MODE`) names which `auth.login.Authenticator` the
    embedded OAuth authorization server should log a human in with
    (ADR-0004's L1/L2 - a single admin password or upstream OIDC). Neither
    is implemented yet (#37): `http.py` only ever enables the authorization
    server when it is handed an `Authenticator` directly (currently true
    only in tests, via `create_app`'s `authenticator` parameter), and raises
    `ServerConfigError` on startup if `login_mode` is set without one - a
    clear refusal rather than silently running with no OAuth login at all.
    `login_mode` unset is not an error: it means "no OAuth authorization
    server", the same static-tokens-only behaviour this server already had
    before #36.

    `oauth_client_secret_key` (`OAUTH_CLIENT_SECRET_KEY`) is the Fernet key
    `auth.store.ClientSecretCipher` encrypts a DCR client's `client_secret`
    with at rest (see that module's docstring for why it is encrypted, not
    hashed, unlike every other OAuth secret). Required only once the OAuth
    authorization server actually turns on (`http.py`, same condition as
    `public_url` above) - `http.py` raises `ServerConfigError` naming it if
    it is missing then, never falls back to running without it.

    `valkey_url` (`VALKEY_URL`) names a Valkey/Redis instance for
    `auth.shared_state.SharedState` to use instead of Postgres (ADR-0009 §2,
    #104) - optional; unset means "Postgres once a database is configured,
    in-process otherwise", the pre-#104 behaviour. When set, `http.py`'s
    `create_app` builds a `ValkeySharedState` on top of it, which then takes
    precedence over Postgres regardless of whether a database is configured
    too - rate limiting and pending login state are loss-tolerant either
    way (ADR-0009 §2), so there is nothing to migrate when switching between
    them. `from_env` only checks the scheme (`redis://`, `rediss://` or
    `unix://`, the three `redis.asyncio.Redis.from_url` accepts) is one of
    those three - never the URL itself, which may carry a password - and
    raises `ServerConfigError` naming the variable, not the value, if it is
    not. `http.py` raises a separate `ServerConfigError` at startup if the
    `redis` package (the optional `valkey` extra) is not installed.

    `cimd_enabled` (`CIMD_ENABLED`, default on) turns Client ID Metadata
    Document registration (SEP-991, #38) on or off alongside DCR: `http.py`
    only builds a `cimd.ClientMetadataFetcher` for `auth.provider.
    MemoryManagerOAuthProvider` when this is true, and the AS metadata
    document (`auth.metadata`) only advertises `client_id_metadata_document_
    supported`/`"none"` then too. Off has no effect on DCR, which is
    unconditional once the OAuth authorization server is enabled at all.

    `max_request_bytes` (`MAX_REQUEST_BYTES`) is `http.py`'s body-size cap on
    `mcp_path` (#39) - the note-file cap (16 KiB) is a separate, later check
    in `vault.validate`; this one exists so a request body is never buffered
    past this many bytes in the first place, counted as bytes actually
    received rather than trusted from `Content-Length`.

    `mcp_per_minute`/`mcp_burst`, `write_per_minute`/`write_burst`,
    `oauth_per_minute`/`oauth_burst` and `webhook_per_minute`/`webhook_burst`
    (`RATE_LIMIT_MCP_PER_MINUTE`/`RATE_LIMIT_MCP_BURST`/... ) feed one
    `auth.ratelimit.RateLimiter` each (#39), all built once in `http.py`'s
    `create_app`, on a shared `auth.shared_state.SharedState` (Valkey once
    `valkey_url` is set, Postgres once a database is configured otherwise,
    in-process if neither is - ADR-0009 §2, #103/#104): `burst`
    calls (`max(1, floor(burst))`) within a fixed window of `burst * 60 /
    per_minute` seconds, the average throughput a token bucket of that
    capacity and refill rate would allow. Every request to `mcp_path` counts
    against the `mcp_*` pair, keyed by the hashed bearer token (or the client
    IP, unauthenticated); every `memory_write`/`memory_edit`/`memory_supersede`/
    `memory_archive`/`memory_promote` tool call *additionally* against the
    tighter `write_*` pair, same key; every request to `/register`/`/token`/
    `/authorize` against `oauth_*`, keyed by client IP; every request to the
    vault webhook against `webhook_*`, keyed by client IP. A key over its
    limit gets a 429 with `Retry-After`.

    `forwarded_allow_ips` (`FORWARDED_ALLOW_IPS`, default `127.0.0.1`) names
    the proxy IPs/CIDRs this server should trust `X-Forwarded-For` from when
    picking the "client IP" the limits above key on - the same semantics as
    uvicorn's own `forwarded_allow_ips`, which is where this is actually
    enforced: `cli.py`'s `_serve_http` passes this value straight into
    `uvicorn.Config(forwarded_allow_ips=...)`, so by the time an ASGI app
    (this one included) sees `scope["client"]`, uvicorn's own
    `ProxyHeadersMiddleware` has already rewritten it from `X-Forwarded-For`
    if (and only if) the request came from one of these. Trusting every hop
    (`"*"`, this field's pre-#39 default) would let any caller spoof
    `X-Forwarded-For` to pick its own rate-limit bucket, or collapse every
    real client behind a reverse proxy onto that proxy's one bucket.

    `shutdown_grace_seconds` (`SHUTDOWN_GRACE_SECONDS`, default 20) is how
    long `cli.py`'s `_serve_http` tells uvicorn
    (`Config(timeout_graceful_shutdown=...)`) to keep draining in-flight
    requests after `SIGTERM`/`SIGINT` before cancelling whatever is still
    running (ADR-0009 §1/§5). `http.py`'s `GracefulShutdownServer` flips
    `/readyz` to 503 on that same signal, before this grace period even
    starts, so a load balancer stops routing new requests here while the
    ones already in flight still get the full grace period to finish.
    Kubernetes `preStop`/`terminationGracePeriodSeconds` (WP-29) sit outside
    this value entirely, on top of it.

    `quota_user_per_minute`/`quota_user_per_day`, `quota_namespace_per_minute`/
    `quota_namespace_per_day` and `quota_token_per_minute`/`quota_token_per_day`
    (`QUOTA_WRITES_PER_MINUTE_USER`/`QUOTA_WRITES_PER_DAY_USER`/
    `QUOTA_WRITES_PER_MINUTE_NAMESPACE`/`QUOTA_WRITES_PER_DAY_NAMESPACE`/
    `QUOTA_WRITES_PER_MINUTE_TOKEN`/`QUOTA_WRITES_PER_DAY_TOKEN`, #242) feed
    `quotas.QuotaChecker`, built by `http.py`'s `create_app` on the same
    `auth.shared_state.SharedState` the `RATE_LIMIT_*` limiters above share -
    held across replicas the same way (ADR-0009 §2). Unlike those, every one
    of these six defaults to `0`, which means "off": a deployment opts a scope
    and a window in explicitly, there is no quota at all otherwise, same
    behaviour this server always had. `user` and `token` are independent
    fixed windows from `namespace`'s, each enforced on its own, never summed;
    see `quotas.QuotaChecker`'s own docstring for which identity each scope
    keys on and why `user` only ever applies once a `db.rls.Principal` exists
    (`"postgres"` mode, ADR-0008 addendum) while `namespace`/`token` apply to
    both storage backends.

    `quota_max_notes_personal`/`quota_max_bytes_personal`/
    `quota_max_notes_shared`/`quota_max_bytes_shared` (`QUOTA_MAX_NOTES_PERSONAL`/
    `QUOTA_MAX_BYTES_PERSONAL`/`QUOTA_MAX_NOTES_SHARED`/`QUOTA_MAX_BYTES_SHARED`,
    #243) feed `quotas.StorageQuotaChecker`, built by `http.py`'s `create_app`
    only once `services.storage` is a `storage.postgres.PostgresBackend`
    ("postgres" mode - the Git backend's `vault_notes` always stays empty, so
    a note-count/byte-size budget against it would be meaningless). Each of
    the four defaults to `0`, same "off unless a deployment opts in"
    behaviour the six `QUOTA_WRITES_*` fields above have; "personal" is the
    caller's own namespace, "shared" every group/project/org namespace - see
    `quotas.StorageQuotaChecker`'s own docstring for exactly what counts
    toward each and why archived notes count toward size but not count.
    """

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    public_url: str | None = None
    mcp_path: str = _DEFAULT_MCP_PATH
    allowed_origins: tuple[str, ...] = ()
    webhook_secret: str | None = field(default=None, repr=False)
    json_response: bool = True
    login_mode: str | None = None
    oauth_client_secret_key: str | None = field(default=None, repr=False)
    valkey_url: str | None = field(default=None, repr=False)
    cimd_enabled: bool = True
    max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES
    mcp_per_minute: float = _DEFAULT_MCP_PER_MINUTE
    mcp_burst: float = _DEFAULT_MCP_BURST
    write_per_minute: float = _DEFAULT_WRITE_PER_MINUTE
    write_burst: float = _DEFAULT_WRITE_BURST
    oauth_per_minute: float = _DEFAULT_OAUTH_PER_MINUTE
    oauth_burst: float = _DEFAULT_OAUTH_BURST
    webhook_per_minute: float = _DEFAULT_WEBHOOK_PER_MINUTE
    webhook_burst: float = _DEFAULT_WEBHOOK_BURST
    forwarded_allow_ips: str = _DEFAULT_FORWARDED_ALLOW_IPS
    shutdown_grace_seconds: int = _DEFAULT_SHUTDOWN_GRACE_SECONDS
    quota_user_per_minute: float = _DEFAULT_QUOTA_WRITES_PER_MINUTE
    quota_user_per_day: float = _DEFAULT_QUOTA_WRITES_PER_DAY
    quota_namespace_per_minute: float = _DEFAULT_QUOTA_WRITES_PER_MINUTE
    quota_namespace_per_day: float = _DEFAULT_QUOTA_WRITES_PER_DAY
    quota_token_per_minute: float = _DEFAULT_QUOTA_WRITES_PER_MINUTE
    quota_token_per_day: float = _DEFAULT_QUOTA_WRITES_PER_DAY
    quota_max_notes_personal: int = _DEFAULT_QUOTA_MAX_NOTES
    quota_max_bytes_personal: int = _DEFAULT_QUOTA_MAX_BYTES
    quota_max_notes_shared: int = _DEFAULT_QUOTA_MAX_NOTES
    quota_max_bytes_shared: int = _DEFAULT_QUOTA_MAX_BYTES

    def resource_url(self) -> str:
        """The MCP server's own canonical URL (RFC 8707 "resource"), for
        `AuthSettings.resource_server_url` and Protected Resource Metadata's
        `resource` field (ADR-0004, #35).

        Always `canonical_resource_url(public_url, mcp_path)` - a client is
        told this exact value and must send it back unchanged as the RFC 8707
        `resource` indicator, so it can never be derived from a request's
        `Host` header (attacker- or proxy-controlled) or guessed from
        `host`/`port` (not necessarily the externally reachable address).

        Raises `ServerConfigError` if `public_url` is unset. This is only
        ever called once bearer-token auth is turning on (`http.py`'s
        lifespan, exactly when `services.pool is not None` - #34/ADR-0004),
        so that is also where this enforces "`PUBLIC_URL` is required when
        auth is enabled" - there is no safe default to invent instead.
        """
        if self.public_url is None:
            raise ServerConfigError(
                "PUBLIC_URL is required once bearer-token auth is enabled "
                "(DATABASE_URL is set): the canonical resource URL (ADR-0004) must "
                "never be derived from a request's Host header; set PUBLIC_URL to "
                "this server's externally reachable origin, e.g. https://memory.example.com"
            )
        return canonical_resource_url(self.public_url, self.mcp_path)

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> ServerConfig:
        """Build a `ServerConfig` from `HOST`/`PORT`/`PUBLIC_URL`/`MCP_PATH`/
        `ALLOWED_ORIGINS`/`VAULT_WEBHOOK_SECRET`/`MCP_JSON_RESPONSE`/`MAX_REQUEST_BYTES`/
        `RATE_LIMIT_*`/`FORWARDED_ALLOW_IPS`/`SHUTDOWN_GRACE_SECONDS`/`QUOTA_WRITES_*`/
        `QUOTA_MAX_*` entries of `environ`.

        Raises `ServerConfigError` with a message naming the offending
        variable if `PORT` is not a valid port number, any size/rate limit is
        not a positive number, or any `QUOTA_WRITES_*`/`QUOTA_MAX_*` variable
        is negative.
        """
        host = environ.get("HOST", _DEFAULT_HOST)
        port = _parse_port(environ.get("PORT"))
        public_url = environ.get("PUBLIC_URL") or None
        mcp_path = environ.get("MCP_PATH", _DEFAULT_MCP_PATH)
        allowed_origins = _resolve_allowed_origins(environ.get("ALLOWED_ORIGINS"), public_url)
        webhook_secret = environ.get("VAULT_WEBHOOK_SECRET") or None
        json_response = _parse_bool(environ.get("MCP_JSON_RESPONSE"), default=True)
        login_mode = environ.get("LOGIN_MODE") or None
        oauth_client_secret_key = environ.get("OAUTH_CLIENT_SECRET_KEY") or None
        valkey_url = _parse_valkey_url(environ.get("VALKEY_URL"))
        cimd_enabled = _parse_bool(environ.get("CIMD_ENABLED"), default=True)
        max_request_bytes = _parse_positive_int(
            environ, "MAX_REQUEST_BYTES", _DEFAULT_MAX_REQUEST_BYTES
        )
        mcp_per_minute = _parse_positive_float(
            environ, "RATE_LIMIT_MCP_PER_MINUTE", _DEFAULT_MCP_PER_MINUTE
        )
        mcp_burst = _parse_positive_float(environ, "RATE_LIMIT_MCP_BURST", _DEFAULT_MCP_BURST)
        write_per_minute = _parse_positive_float(
            environ, "RATE_LIMIT_WRITE_PER_MINUTE", _DEFAULT_WRITE_PER_MINUTE
        )
        write_burst = _parse_positive_float(environ, "RATE_LIMIT_WRITE_BURST", _DEFAULT_WRITE_BURST)
        oauth_per_minute = _parse_positive_float(
            environ, "RATE_LIMIT_OAUTH_PER_MINUTE", _DEFAULT_OAUTH_PER_MINUTE
        )
        oauth_burst = _parse_positive_float(environ, "RATE_LIMIT_OAUTH_BURST", _DEFAULT_OAUTH_BURST)
        webhook_per_minute = _parse_positive_float(
            environ, "RATE_LIMIT_WEBHOOK_PER_MINUTE", _DEFAULT_WEBHOOK_PER_MINUTE
        )
        webhook_burst = _parse_positive_float(
            environ, "RATE_LIMIT_WEBHOOK_BURST", _DEFAULT_WEBHOOK_BURST
        )
        forwarded_allow_ips = environ.get("FORWARDED_ALLOW_IPS") or _DEFAULT_FORWARDED_ALLOW_IPS
        shutdown_grace_seconds = _parse_positive_int(
            environ, "SHUTDOWN_GRACE_SECONDS", _DEFAULT_SHUTDOWN_GRACE_SECONDS
        )
        quota_user_per_minute = _parse_nonnegative_float(
            environ, "QUOTA_WRITES_PER_MINUTE_USER", _DEFAULT_QUOTA_WRITES_PER_MINUTE
        )
        quota_user_per_day = _parse_nonnegative_float(
            environ, "QUOTA_WRITES_PER_DAY_USER", _DEFAULT_QUOTA_WRITES_PER_DAY
        )
        quota_namespace_per_minute = _parse_nonnegative_float(
            environ, "QUOTA_WRITES_PER_MINUTE_NAMESPACE", _DEFAULT_QUOTA_WRITES_PER_MINUTE
        )
        quota_namespace_per_day = _parse_nonnegative_float(
            environ, "QUOTA_WRITES_PER_DAY_NAMESPACE", _DEFAULT_QUOTA_WRITES_PER_DAY
        )
        quota_token_per_minute = _parse_nonnegative_float(
            environ, "QUOTA_WRITES_PER_MINUTE_TOKEN", _DEFAULT_QUOTA_WRITES_PER_MINUTE
        )
        quota_token_per_day = _parse_nonnegative_float(
            environ, "QUOTA_WRITES_PER_DAY_TOKEN", _DEFAULT_QUOTA_WRITES_PER_DAY
        )
        quota_max_notes_personal = _parse_nonnegative_int(
            environ, "QUOTA_MAX_NOTES_PERSONAL", _DEFAULT_QUOTA_MAX_NOTES
        )
        quota_max_bytes_personal = _parse_nonnegative_int(
            environ, "QUOTA_MAX_BYTES_PERSONAL", _DEFAULT_QUOTA_MAX_BYTES
        )
        quota_max_notes_shared = _parse_nonnegative_int(
            environ, "QUOTA_MAX_NOTES_SHARED", _DEFAULT_QUOTA_MAX_NOTES
        )
        quota_max_bytes_shared = _parse_nonnegative_int(
            environ, "QUOTA_MAX_BYTES_SHARED", _DEFAULT_QUOTA_MAX_BYTES
        )

        return cls(
            host=host,
            port=port,
            public_url=public_url,
            mcp_path=mcp_path,
            allowed_origins=allowed_origins,
            webhook_secret=webhook_secret,
            json_response=json_response,
            login_mode=login_mode,
            oauth_client_secret_key=oauth_client_secret_key,
            valkey_url=valkey_url,
            cimd_enabled=cimd_enabled,
            max_request_bytes=max_request_bytes,
            mcp_per_minute=mcp_per_minute,
            mcp_burst=mcp_burst,
            write_per_minute=write_per_minute,
            write_burst=write_burst,
            oauth_per_minute=oauth_per_minute,
            oauth_burst=oauth_burst,
            webhook_per_minute=webhook_per_minute,
            webhook_burst=webhook_burst,
            forwarded_allow_ips=forwarded_allow_ips,
            shutdown_grace_seconds=shutdown_grace_seconds,
            quota_user_per_minute=quota_user_per_minute,
            quota_user_per_day=quota_user_per_day,
            quota_namespace_per_minute=quota_namespace_per_minute,
            quota_namespace_per_day=quota_namespace_per_day,
            quota_token_per_minute=quota_token_per_minute,
            quota_token_per_day=quota_token_per_day,
            quota_max_notes_personal=quota_max_notes_personal,
            quota_max_bytes_personal=quota_max_bytes_personal,
            quota_max_notes_shared=quota_max_notes_shared,
            quota_max_bytes_shared=quota_max_bytes_shared,
        )


def rate_limit_sweep_floor_seconds(config: ServerConfig) -> float:
    """The longest fixed window any `auth.ratelimit.RateLimiter`/the login
    brute-force check could have opened against `config`'s `RATE_LIMIT_*`
    settings - the floor `PostgresSharedState.sweep_expired_windows`'s
    `older_than_seconds` must never go under (that function's own docstring),
    so a window still being counted against is never swept away mid-window.

    One home for this formula, imported by both `http.py`'s own periodic
    cleanup sweep and `worker.py`'s equivalent job for the `"postgres"`
    backend (#217) - both read the same `RATE_LIMIT_*` environment into their
    own `ServerConfig` and must agree on this floor, which a second,
    independently maintained copy of the formula could only risk drifting
    out of.

    `RateLimiter.__init__`'s own `window_seconds = burst * 60 / per_minute`
    formula, recomputed here rather than read off a built `RateLimiter`
    (which keeps that value private) - the four `(burst, per_minute)` pairs
    `http.py`'s `create_app` builds its limiters from, plus the one fixed
    window `auth.login_password` uses, are the only windows either process
    ever opens on a `SharedState` backend.
    """
    pairs = (
        (config.mcp_burst, config.mcp_per_minute),
        (config.write_burst, config.write_per_minute),
        (config.oauth_burst, config.oauth_per_minute),
        (config.webhook_burst, config.webhook_per_minute),
    )
    windows = [burst * 60.0 / per_minute for burst, per_minute in pairs]
    windows.append(_LOGIN_BRUTE_FORCE_WINDOW_SECONDS)
    return max(windows)


class WorkerConfigError(ValueError):
    """A `memory-manager worker` environment variable is missing or invalid."""


@dataclass(frozen=True)
class WorkerConfig:
    """Configuration for `memory-manager worker` (#217, ADR-0009 §4: "worker:
    embedding queue, Graph delta sync, retention, OAuth cleanup, with its own
    HPA").

    `host`/`port` (`WORKER_HOST`/`WORKER_PORT`, default `127.0.0.1:8090`) is
    where the worker's own minimal Starlette app binds - `/healthz`,
    `/readyz`, `/metrics`, the same three paths `ServerConfig`'s `api`
    process serves, but on a different default port: a deployment running
    both processes on one host must be able to tell the two apart.

    `shutdown_grace_seconds` reads the same `SHUTDOWN_GRACE_SECONDS`
    variable and default (20s) `ServerConfig` does (ADR-0009 §1/§5) - one
    knob, not two, since both processes drain the same way on `SIGTERM`:
    stop scheduling new work, let whatever is already running finish within
    this many seconds, then exit.

    `jobs_poll_seconds` (`JOBS_POLL_SECONDS`, default 5s) is the `jobs`
    outbox consumer's poll fallback (#218, `worker.consume_jobs`,
    docs/research/enterprise.md §"Queue": "1-5 s polling as fallback") -
    `LISTEN/NOTIFY` is only ever a wake-up hint, so this is the ceiling on
    how long a claimable job can wait for a notification that never
    arrives, not how often the worker normally wakes up.

    `entra_delta_sync_seconds` (`ENTRA_DELTA_SYNC_SECONDS`, default 300s/5min)
    is how often `worker.build_jobs` schedules the `entra_delta_sync` job
    (#223, ADR-0006 §6) - read regardless of whether Entra is even configured
    for this deployment; `build_jobs` is what decides whether to register the
    job at all (`auth.graph.GraphClient.from_env` returning `None` otherwise),
    this field only ever carries the interval to use once it does.
    """

    host: str = _DEFAULT_WORKER_HOST
    port: int = _DEFAULT_WORKER_PORT
    shutdown_grace_seconds: int = _DEFAULT_SHUTDOWN_GRACE_SECONDS
    jobs_poll_seconds: float = _DEFAULT_JOBS_POLL_SECONDS
    entra_delta_sync_seconds: float = _DEFAULT_ENTRA_DELTA_SYNC_SECONDS

    @classmethod
    def from_env(cls, environ: dict[str, str]) -> WorkerConfig:
        """Build a `WorkerConfig` from `WORKER_HOST`/`WORKER_PORT`/
        `SHUTDOWN_GRACE_SECONDS`/`JOBS_POLL_SECONDS`/`ENTRA_DELTA_SYNC_SECONDS`
        entries of `environ`.

        Raises `WorkerConfigError` with a message naming the offending
        variable if `WORKER_PORT` is not a valid port number, or any of
        `SHUTDOWN_GRACE_SECONDS`/`JOBS_POLL_SECONDS`/`ENTRA_DELTA_SYNC_SECONDS`
        is not a positive number.
        """
        host = environ.get("WORKER_HOST", _DEFAULT_WORKER_HOST)
        try:
            port = _parse_port(
                environ.get("WORKER_PORT"), name="WORKER_PORT", default=_DEFAULT_WORKER_PORT
            )
            shutdown_grace_seconds = _parse_positive_int(
                environ, "SHUTDOWN_GRACE_SECONDS", _DEFAULT_SHUTDOWN_GRACE_SECONDS
            )
            jobs_poll_seconds = _parse_positive_float(
                environ, "JOBS_POLL_SECONDS", _DEFAULT_JOBS_POLL_SECONDS
            )
            entra_delta_sync_seconds = _parse_positive_float(
                environ, "ENTRA_DELTA_SYNC_SECONDS", _DEFAULT_ENTRA_DELTA_SYNC_SECONDS
            )
        except ServerConfigError as exc:
            # `_parse_port`/`_parse_positive_int`/`_parse_positive_float` raise
            # `ServerConfigError` (shared with `ServerConfig`, which reuses all three) -
            # re-raised as this module's own `WorkerConfigError` so a caller catching
            # errors for `memory-manager worker` never has to also know about
            # `ServerConfig`'s.
            raise WorkerConfigError(str(exc)) from exc
        return cls(
            host=host,
            port=port,
            shutdown_grace_seconds=shutdown_grace_seconds,
            jobs_poll_seconds=jobs_poll_seconds,
            entra_delta_sync_seconds=entra_delta_sync_seconds,
        )


def _parse_port(raw: str | None, *, name: str = "PORT", default: int = _DEFAULT_PORT) -> int:
    if raw is None:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ServerConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ServerConfigError(f"{name} must be between 1 and 65535, got {port}")
    return port


def _parse_positive_int(environ: dict[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ServerConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ServerConfigError(f"{name} must be positive, got {value}")
    return value


def _parse_positive_float(environ: dict[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ServerConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ServerConfigError(f"{name} must be positive, got {value}")
    return value


def _parse_nonnegative_float(environ: dict[str, str], name: str, default: float) -> float:
    """Like `_parse_positive_float`, but `0` is valid - the "off" value every
    `QUOTA_WRITES_*` variable (#242) uses, unlike a `RATE_LIMIT_*` pair, which
    is always enforced."""
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ServerConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ServerConfigError(f"{name} must be zero or positive, got {value}")
    return value


def _parse_nonnegative_int(environ: dict[str, str], name: str, default: int) -> int:
    """Like `_parse_nonnegative_float`, but for an integer count/byte-size budget -
    the "off" value every `QUOTA_MAX_*` variable (#243) uses."""
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ServerConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ServerConfigError(f"{name} must be zero or positive, got {value}")
    return value


#: Schemes `redis.asyncio.Redis.from_url` accepts (confirmed by reading `redis-py`'s
#: `from_url`, #104) - `VALKEY_URL` is checked against these without ever including
#: the value itself in an error message, since it may carry a password.
_VALKEY_URL_SCHEMES = frozenset({"redis", "rediss", "unix"})


def _parse_valkey_url(raw: str | None) -> str | None:
    if not raw:
        return None
    scheme = urlsplit(raw).scheme
    if scheme not in _VALKEY_URL_SCHEMES:
        raise ServerConfigError(
            f"VALKEY_URL must start with redis://, rediss:// or unix://, got a scheme of {scheme!r}"
        )
    return raw


def _resolve_allowed_origins(raw: str | None, public_url: str | None) -> tuple[str, ...]:
    """`ALLOWED_ORIGINS` (comma-separated), plus `public_url`'s own origin if set.

    Default (both unset) is the empty tuple: no `Origin` header is ever
    allowed, only requests carrying none at all (the MCP spec's DNS-rebinding
    guidance - a non-browser client never sends `Origin` in the first place).
    """
    origins = {origin.strip() for origin in (raw or "").split(",") if origin.strip()}
    if public_url:
        origins.add(_origin_of(public_url))
    return tuple(sorted(origins))


def _origin_of(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def canonical_resource_url(public_url: str, path: str) -> str:
    """`public_url` plus `path`, normalized to the canonical form clients compare
    against byte-for-byte (RFC 8707 `resource`; docs/research/mcp-auth-and-connectors.md
    §3: "lowercase scheme/host, no trailing slash").

    - scheme and host lowercased (`urlsplit().hostname` already lowercases; the
      scheme is lowercased here too)
    - the default port for the scheme (`:443` for `https`, `:80` for `http`) is
      dropped; any other port is kept
    - `path` is normalized to exactly one leading slash and no trailing slash,
      regardless of how many slashes it arrived with - except the empty string,
      which stays empty: `canonical_resource_url(public_url, "")` is the bare
      origin (no path at all), used for the AS issuer (ADR-0004: "the issuer =
      canonical origin"), as opposed to `canonical_resource_url(public_url,
      mcp_path)` for the resource URL itself, which always has a path
    - any path, query or fragment already present on `public_url` itself is
      dropped - `PUBLIC_URL` is documented as the origin only (ADR-0004: "`PUBLIC_URL`
      + `MCP_PATH` is canonicalised once at startup"), so `path` is the single
      source of truth for what comes after the origin
    """
    parsed = urlsplit(public_url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or ""
    port = parsed.port
    default_port = {"https": 443, "http": 80}.get(scheme)
    netloc = hostname if port is None or port == default_port else f"{hostname}:{port}"
    stripped_path = path.strip("/")
    normalized_path = f"/{stripped_path}" if stripped_path else ""
    return f"{scheme}://{netloc}{normalized_path}"


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSY_BOOL_ENV

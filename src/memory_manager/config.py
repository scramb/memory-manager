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
    "EmbeddingConfig",
    "EmbeddingConfigError",
    "ServerConfig",
    "ServerConfigError",
    "StorageConfigError",
    "VaultConfig",
    "VaultConfigError",
    "canonical_resource_url",
    "storage_backend_from_env",
]

_DEFAULT_BRANCH = "main"
_DEFAULT_POLL_SECONDS = 60
_DEFAULT_EMBEDDING_PROVIDER = "none"
_EMBEDDING_PROVIDERS = ("none", "ollama", "openai")
_DEFAULT_OLLAMA_MODEL = "bge-m3"
_DEFAULT_STORAGE_BACKEND = "git"
_STORAGE_BACKENDS = ("git",)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_DEFAULT_MCP_PATH = "/mcp"
_FALSY_BOOL_ENV = frozenset({"0", "false", "no", "off", ""})

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

    Only `"git"` (`memory_manager.storage.git.GitBackend`) is implemented
    today; `"postgres"` is enterprise scope (WP-18, ADR-0007). Raises
    `StorageConfigError` for any other value - the same
    `EMBEDDING_PROVIDER` pattern `EmbeddingConfig.from_env` uses above.
    """
    backend = environ.get("STORAGE_BACKEND", _DEFAULT_STORAGE_BACKEND)
    if backend not in _STORAGE_BACKENDS:
        raise StorageConfigError(
            f"STORAGE_BACKEND must be one of {_STORAGE_BACKENDS}, got {backend!r}"
        )
    return backend


class EmbeddingConfigError(ValueError):
    """A required `EMBEDDING_*` environment variable is missing or invalid."""


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
    IP, unauthenticated); every `memory_write`/`memory_edit`/
    `memory_supersede`/`memory_archive` tool call *additionally* against the
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
        `RATE_LIMIT_*`/`FORWARDED_ALLOW_IPS`/`SHUTDOWN_GRACE_SECONDS` entries of
        `environ`.

        Raises `ServerConfigError` with a message naming the offending
        variable if `PORT` is not a valid port number, or any size/rate
        limit is not a positive number.
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
        )


def _parse_port(raw: str | None) -> int:
    if raw is None:
        return _DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ServerConfigError(f"PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ServerConfigError(f"PORT must be between 1 and 65535, got {port}")
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

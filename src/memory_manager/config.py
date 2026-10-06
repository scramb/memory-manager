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
    "VaultConfig",
    "VaultConfigError",
    "canonical_resource_url",
]

_DEFAULT_BRANCH = "main"
_DEFAULT_POLL_SECONDS = 60
_DEFAULT_EMBEDDING_PROVIDER = "none"
_EMBEDDING_PROVIDERS = ("none", "ollama", "openai")
_DEFAULT_OLLAMA_MODEL = "bge-m3"

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_DEFAULT_MCP_PATH = "/mcp"
_FALSY_BOOL_ENV = frozenset({"0", "false", "no", "off", ""})


class VaultConfigError(ValueError):
    """A required `VAULT_*` environment variable is missing or invalid."""


@dataclass(frozen=True)
class VaultConfig:
    """Configuration for the vault's git remote and local working copy."""

    remote: str
    dir: Path
    branch: str = _DEFAULT_BRANCH
    ssh_key_file: Path | None = None
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
            https_token=https_token,
            poll_seconds=poll_seconds,
        )


def _require(environ: dict[str, str], name: str, error: type[ValueError]) -> str:
    value = environ.get(name)
    if not value:
        raise error(f"{name} is required but not set")
    return value


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
    """

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    public_url: str | None = None
    mcp_path: str = _DEFAULT_MCP_PATH
    allowed_origins: tuple[str, ...] = ()
    webhook_secret: str | None = field(default=None, repr=False)
    json_response: bool = True

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
        `ALLOWED_ORIGINS`/`VAULT_WEBHOOK_SECRET`/`MCP_JSON_RESPONSE` entries of `environ`.

        Raises `ServerConfigError` with a message naming the offending
        variable if `PORT` is not a valid port number.
        """
        host = environ.get("HOST", _DEFAULT_HOST)
        port = _parse_port(environ.get("PORT"))
        public_url = environ.get("PUBLIC_URL") or None
        mcp_path = environ.get("MCP_PATH", _DEFAULT_MCP_PATH)
        allowed_origins = _resolve_allowed_origins(environ.get("ALLOWED_ORIGINS"), public_url)
        webhook_secret = environ.get("VAULT_WEBHOOK_SECRET") or None
        json_response = _parse_bool(environ.get("MCP_JSON_RESPONSE"), default=True)

        return cls(
            host=host,
            port=port,
            public_url=public_url,
            mcp_path=mcp_path,
            allowed_origins=allowed_origins,
            webhook_secret=webhook_secret,
            json_response=json_response,
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

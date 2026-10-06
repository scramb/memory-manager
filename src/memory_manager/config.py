# SPDX-License-Identifier: AGPL-3.0-only
"""Vault and embedding configuration, read from the process environment.

`VaultConfig` and `EmbeddingConfig` are the one place that turn `VAULT_*`
and `EMBEDDING_*` environment variables into typed, validated configuration
objects. Credentials (`https_token`, `api_key`) never appear in
`repr()`/`str()` output, so either config can be logged safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["EmbeddingConfig", "EmbeddingConfigError", "VaultConfig", "VaultConfigError"]

_DEFAULT_BRANCH = "main"
_DEFAULT_POLL_SECONDS = 60
_DEFAULT_EMBEDDING_PROVIDER = "none"
_EMBEDDING_PROVIDERS = ("none", "ollama", "openai")
_DEFAULT_OLLAMA_MODEL = "bge-m3"


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

# SPDX-License-Identifier: AGPL-3.0-only
"""Vault configuration, read from the process environment.

`VaultConfig` is the one place that turns `VAULT_*` environment variables
into a typed, validated configuration object. Credentials (`https_token`)
never appear in `repr()`/`str()` output, so the config can be logged safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["VaultConfig", "VaultConfigError"]

_DEFAULT_BRANCH = "main"
_DEFAULT_POLL_SECONDS = 60


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
        remote = _require(environ, "VAULT_REMOTE")
        vault_dir = _require(environ, "VAULT_DIR")
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


def _require(environ: dict[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise VaultConfigError(f"{name} is required but not set")
    return value

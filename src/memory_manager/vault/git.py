# SPDX-License-Identifier: AGPL-3.0-only
"""Thin subprocess wrapper around the `git` CLI (ADR-0003).

`Git` is the one place in the codebase that knows how `git` is invoked:
argument lists (never a shell string), a timeout on every call, and an
environment that ignores the user's and the system's git config so that a
stray `~/.gitconfig` or `/etc/gitconfig` can never change the vault's
behaviour. Callers higher up (`vault/repo.py`) decide *what* to run; this
module only decides *how*.

Calls are synchronous (`subprocess.run`); async callers wrap a call in
`asyncio.to_thread`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from memory_manager.observability.metrics import track_git_operation

__all__ = ["Git", "GitError", "PushRejected"]

_DEFAULT_TIMEOUT = 60.0

# Resolved once, to an absolute path: avoids relying on a bare "git" lookup
# through $PATH at every call site.
_GIT_EXECUTABLE = shutil.which("git") or "git"

# Isolates every call from the invoking user's and the host's git config:
# only the config the caller explicitly sets via `-c` has any effect.
_FIXED_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

_REJECTION_MARKERS = (
    "[rejected]",
    "non-fast-forward",
    "fetch first",
    "[remote rejected]",
    "failed to update ref",
    "cannot lock ref",
    "stale info",
)


class GitError(RuntimeError):
    """A `git` invocation failed.

    `command` is the (redacted) argument list, `returncode` the process exit
    code, `stderr` the (redacted) standard error output. Redaction removes
    any configured secret (e.g. an HTTPS token) so it never reaches a log or
    an error surfaced to a caller.
    """

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        message = f"git {' '.join(self.command)} failed (exit {returncode}): {stderr}".strip()
        super().__init__(message)


class PushRejected(GitError):
    """`push()` was rejected because the remote moved ahead of the local branch."""


class Git:
    """Runs `git` in `cwd` with a fixed, isolated environment."""

    def __init__(
        self,
        cwd: Path,
        *,
        env_extra: Mapping[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        secrets: Sequence[str] = (),
    ) -> None:
        self._cwd = cwd
        self._env_extra = dict(env_extra or {})
        self._timeout = timeout
        self._secrets = tuple(s for s in secrets if s)

    def run(
        self,
        *args: str,
        input: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run `git <args>` in `cwd`.

        Raises `GitError` (or `PushRejected` for a rejected `push`) if
        `check` is true and the process exits non-zero, or if it times out.
        """
        with track_git_operation(args):
            env = {**os.environ, **_FIXED_ENV, **self._env_extra}
            try:
                result = subprocess.run(  # noqa: S603 - fixed executable, argument list, no shell
                    [_GIT_EXECUTABLE, *args],
                    cwd=self._cwd,
                    input=input,
                    capture_output=True,
                    env=env,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                stderr = self._scrub(_decode(exc.stderr))
                raise GitError(
                    self._redact(args), -1, f"timed out after {self._timeout}s - {stderr}".strip()
                ) from exc

            if check and result.returncode != 0:
                stderr = self._scrub(_decode(result.stderr))
                if "push" in args and _looks_like_push_rejection(stderr):
                    raise PushRejected(self._redact(args), result.returncode, stderr)
                raise GitError(self._redact(args), result.returncode, stderr)
            return result

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _redact(self, args: Sequence[str]) -> tuple[str, ...]:
        return tuple(self._scrub(_redact_auth_header(arg)) for arg in args)


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def _redact_auth_header(arg: str) -> str:
    if "extraHeader" in arg or "Authorization" in arg:
        return "http.extraHeader=Authorization: Basic ***"
    return arg


def _looks_like_push_rejection(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _REJECTION_MARKERS)

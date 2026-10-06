# SPDX-License-Identifier: AGPL-3.0-only
"""The vault's working copy: clone, commit-per-change, push (ADR-0003).

`Repo` owns exactly one local clone of the configured remote. Every write
is a single commit authored by the client that made it; the committer
identity (`memory-manager`) is the local git config, set once by
`ensure_clone()`. Rebase, conflict handling (#15) and pulling human changes
(#12) live elsewhere; `push()` here only has to recognize a non-fast-forward
rejection and surface it as `PushRejected`.
"""

from __future__ import annotations

import base64
import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from memory_manager.config import VaultConfig
from memory_manager.vault import paths
from memory_manager.vault.git import Git, GitError

__all__ = ["Author", "Repo", "author_for"]

_COMMITTER_NAME = "memory-manager"
_COMMITTER_EMAIL = "memory-manager@memory-manager.invalid"

_CLIENT_DOMAIN = "memory-manager.invalid"
_KNOWN_CLIENTS = ("claude-ai", "claude-code", "human", "import")


@dataclass(frozen=True)
class Author:
    """The identity a commit is authored as."""

    name: str
    email: str


def author_for(client: str) -> Author:
    """Map a client identifier to the `Author` it commits as.

    Raises `ValueError` if `client` is not one of the known clients
    (`claude-ai`, `claude-code`, `human`, `import`).
    """
    if client not in _KNOWN_CLIENTS:
        allowed = ", ".join(_KNOWN_CLIENTS)
        raise ValueError(f"unknown client {client!r}, expected one of ({allowed})")
    return Author(name=client, email=f"{client}@{_CLIENT_DOMAIN}")


class Repo:
    """The local working copy of the vault, bound to one `VaultConfig`."""

    def __init__(self, config: VaultConfig) -> None:
        self._config = config

    def ensure_clone(self) -> None:
        """Make sure `config.dir` holds a working copy of `config.remote`.

        Idempotent: clones if the directory is missing or empty, otherwise
        verifies the existing clone's `origin` matches `config.remote` and
        raises `GitError` if it does not. Either way, sets the local
        committer identity.
        """
        vault_dir = self._config.dir
        if vault_dir.exists() and any(vault_dir.iterdir()):
            self._verify_existing_clone()
        else:
            vault_dir.parent.mkdir(parents=True, exist_ok=True)
            self._clone()
        self._configure_identity()

    def head(self) -> str:
        """Return the current `HEAD` commit SHA."""
        result = self._git().run("rev-parse", "HEAD")
        return _decode(result.stdout).strip()

    def read_file(self, rel: str) -> bytes | None:
        """Return the bytes of `rel`, or `None` if it does not exist."""
        path = paths.resolve(self._config.dir, rel, allow_archive=True)
        if not path.exists():
            return None
        return path.read_bytes()

    def commit_file(self, rel: str, content: bytes, author: Author, message: str) -> str:
        """Write `content` to `rel` and commit it as a single commit by `author`.

        Raises `GitError("no changes")` if `content` is identical to what is
        already committed at `rel` (nothing to stage).
        """
        path = paths.resolve(self._config.dir, rel, allow_archive=True)
        _atomic_write(path, content)
        git = self._git()
        git.run("add", "--", rel)
        self._commit_staged(git, author, message)
        return self.head()

    def move_file(
        self, src_rel: str, dst_rel: str, content: bytes, author: Author, message: str
    ) -> str:
        """Move `src_rel` to `dst_rel`, writing `content`, in one commit.

        Used to archive a note: `git mv` plus the (possibly updated)
        content, committed together.
        """
        paths.resolve(self._config.dir, src_rel, allow_archive=True, must_exist=True)
        dst_path = paths.resolve(self._config.dir, dst_rel, allow_archive=True)
        git = self._git()
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        git.run("mv", "--", src_rel, dst_rel)
        _atomic_write(dst_path, content)
        git.run("add", "--", dst_rel)
        self._commit_staged(git, author, message)
        return self.head()

    def push(self) -> None:
        """Push the current branch to `origin`.

        Raises `PushRejected` if the remote has moved ahead (non-fast-forward).
        """
        git = self._git()
        git.run(*self._auth_args(), "push", "origin", f"HEAD:{self._config.branch}")

    def _commit_staged(self, git: Git, author: Author, message: str) -> None:
        diff = git.run("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            raise GitError(("commit",), 1, "no changes")
        git.run("commit", "-m", message, f"--author={author.name} <{author.email}>")

    def _clone(self) -> None:
        git = Git(
            cwd=self._config.dir.parent,
            env_extra=self._env_extra(),
            secrets=self._secrets(),
        )
        git.run(
            *self._auth_args(),
            "clone",
            "--origin",
            "origin",
            self._config.remote,
            str(self._config.dir),
        )
        self._ensure_branch(
            Git(cwd=self._config.dir, env_extra=self._env_extra(), secrets=self._secrets())
        )

    def _verify_existing_clone(self) -> None:
        git = self._git()
        inside = git.run("rev-parse", "--is-inside-work-tree", check=False)
        if inside.returncode != 0:
            raise GitError(("rev-parse",), inside.returncode, _decode(inside.stderr))

        origin = git.run("remote", "get-url", "origin", check=False)
        if origin.returncode != 0:
            raise GitError(
                ("remote", "get-url", "origin"), origin.returncode, _decode(origin.stderr)
            )
        origin_url = _decode(origin.stdout).strip()
        if origin_url != self._config.remote:
            raise GitError(
                ("remote", "get-url", "origin"),
                0,
                f"existing clone's origin is {origin_url!r}, expected {self._config.remote!r}",
            )

    def _ensure_branch(self, git: Git) -> None:
        target_ref = f"refs/heads/{self._config.branch}"

        current = git.run("symbolic-ref", "-q", "HEAD", check=False)
        if _decode(current.stdout).strip() == target_ref:
            return

        local_exists = git.run("show-ref", "--verify", "--quiet", target_ref, check=False)
        if local_exists.returncode == 0:
            git.run("checkout", self._config.branch)
            return

        remote_ref = f"refs/remotes/origin/{self._config.branch}"
        remote_exists = git.run("show-ref", "--verify", "--quiet", remote_ref, check=False)
        if remote_exists.returncode == 0:
            git.run("checkout", "-b", self._config.branch, remote_ref)
            return

        # Empty remote: no commit, no remote branch yet - point (the possibly
        # unborn) HEAD at the configured branch so the first commit creates it.
        git.run("symbolic-ref", "HEAD", target_ref)

    def _configure_identity(self) -> None:
        git = self._git()
        git.run("config", "user.name", _COMMITTER_NAME)
        git.run("config", "user.email", _COMMITTER_EMAIL)

    def _git(self) -> Git:
        return Git(cwd=self._config.dir, env_extra=self._env_extra(), secrets=self._secrets())

    def _env_extra(self) -> dict[str, str]:
        if self._config.ssh_key_file is None:
            return {}
        ssh_command = (
            f"ssh -i {self._config.ssh_key_file} "
            "-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )
        return {"GIT_SSH_COMMAND": ssh_command}

    def _secrets(self) -> tuple[str, ...]:
        token = self._config.https_token
        return (token,) if token else ()

    def _auth_args(self) -> tuple[str, ...]:
        token = self._config.https_token
        if not token or not self._config.remote.startswith(("http://", "https://")):
            return ()
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        return ("-c", f"http.extraHeader=Authorization: Basic {encoded}")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")

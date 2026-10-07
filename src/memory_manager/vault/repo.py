# SPDX-License-Identifier: AGPL-3.0-only
"""The vault's working copy: clone, commit-per-change, push (ADR-0003).

`Repo` owns exactly one local clone of the configured remote. Every write
is a single commit authored by the client that made it; the committer
identity (`memory-manager`) is the local git config, set once by
`ensure_clone()`. `push()` only recognizes a non-fast-forward rejection and
surfaces it as `PushRejected`; `rebase_onto_remote()`/`reset_to_remote()`/
`remote_head()`/`read_remote_file()`/`commit_internal_file()` are what the
write queue (`queue.py`, #15) uses to rebase a rejected push and, on a
rebase conflict, write a server-owned `*.conflict.md` beside the note.
Pulling human changes into the live working tree outside a write (#12)
lives elsewhere.
"""

from __future__ import annotations

import base64
import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from memory_manager.config import VaultConfig
from memory_manager.vault import paths
from memory_manager.vault.git import Git, GitError
from memory_manager.vault.paths import PathRejected
from memory_manager.vault.sync import ChangeSet

__all__ = ["Author", "Repo", "SyncDiverged", "author_for"]

_COMMITTER_NAME = "memory-manager"
_COMMITTER_EMAIL = "memory-manager@memory-manager.invalid"

_CLIENT_DOMAIN = "memory-manager.invalid"
_KNOWN_CLIENTS = ("claude-ai", "claude-code", "human", "import")

# The SHA-1 of the empty tree: diffing against it turns "every file in
# <new_head>" into the same add/modify/delete shape a normal diff produces,
# so the fresh-clone case reuses the regular diff/classify code path.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class SyncDiverged(GitError):
    """`sync()` found local commits that are not on the remote branch.

    Single-writer: the local clone is never supposed to carry commits the
    remote does not have except right after `commit_file`/`move_file`,
    before the next `push()`. A genuine divergence means something wrote
    to the local clone outside this process; `sync()` refuses to guess at
    a resolution (no merge, no rebase) and surfaces it instead.
    """


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
        # Filled in lazily, at most once, by `_resolved_ssh_key_file()`.
        self._resolved_ssh_key_file: Path | None = None

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

    def head_or_none(self) -> str | None:
        """`head()`, or `None` if `HEAD` is unborn (a brand new, still-empty clone)."""
        return self._current_head(self._git())

    def fetch(self) -> str:
        """Fetch `origin/<branch>` and return its current commit sha.

        The write queue (#16) calls this right after a rejected push, to
        learn the remote's new tip *before* deciding whether
        `rebase_onto_remote()` is actually safe for the path(s) this write
        touches - `remote_head()` alone would just report whatever was
        fetched last.
        """
        git = self._git()
        git.run(*self._auth_args(), "fetch", "origin", self._config.branch)
        return self.remote_head()

    def changed_between(self, old_rev: str | None, new_rev: str, rel: str) -> bool:
        """Whether `rel` differs between `old_rev` and `new_rev`.

        `old_rev=None` stands for "no commit yet" (the clone was still
        empty) - diffed against the empty tree, so a `rel` that is new in
        `new_rev` still counts as changed.

        Used by the write queue (#16) to tell a push rejection caused by an
        unrelated remote change from one where the remote touched the exact
        note this write is in the middle of committing: a clean
        `rebase_onto_remote()` can silently 3-way-merge the latter without
        ever reporting a conflict, even though the write's `if_version` no
        longer matches what is actually on the remote.
        """
        git = self._git()
        base = old_rev if old_rev is not None else _EMPTY_TREE
        result = git.run("diff", "--quiet", base, new_rev, "--", rel, check=False)
        return result.returncode != 0

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

    def commit_files(self, files: dict[str, bytes], author: Author, message: str) -> str:
        """Write every path in `files` and commit them all together, as one commit by `author`.

        Used for a write that touches more than one note at once (`supersede`,
        #19: the old note's `valid_to` and the new note's `supersedes`) -
        every path is written and staged individually, but all land in the
        same commit instead of one commit per note.
        """
        git = self._git()
        for rel, content in files.items():
            path = paths.resolve(self._config.dir, rel, allow_archive=True)
            _atomic_write(path, content)
            git.run("add", "--", rel)
        self._commit_staged(git, author, message)
        return self.head()

    def push(self) -> None:
        """Push the current branch to `origin`.

        Raises `PushRejected` if the remote has moved ahead (non-fast-forward).
        """
        git = self._git()
        git.run(*self._auth_args(), "push", "origin", f"HEAD:{self._config.branch}")

    def rebase_onto_remote(self) -> bool:
        """Fetch `origin/<branch>` and rebase the local commit(s) onto it.

        Returns `True` on a clean rebase (the local commit(s) now sit on top
        of the remote's current tip). Returns `False` if the rebase hits a
        conflict - the rebase is aborted first, so the clone ends up exactly
        where it started, just with `origin/<branch>` freshly fetched.
        """
        git = self._git()
        branch = self._config.branch
        git.run(*self._auth_args(), "fetch", "origin", branch)
        return self.rebase_onto(f"origin/{branch}")

    def rebase_onto(self, rev: str) -> bool:
        """Rebase the local commit(s) onto `rev`, without fetching first.

        Same clean/conflict contract as `rebase_onto_remote()`, for a
        caller that already fetched and needs the rebase to happen against
        the exact remote state it just inspected (the write queue, #16:
        checking whether the remote touched the write's own path and then
        rebasing must agree on which remote tip they are both talking
        about - an extra fetch in between could silently move the target
        past a change neither step ever checked).
        """
        git = self._git()
        result = git.run("rebase", rev, check=False)
        if result.returncode == 0:
            return True
        git.run("rebase", "--abort", check=False)
        return False

    def reset_to_remote(self) -> None:
        """Fetch `origin/<branch>` and hard-reset the local clone to it.

        Used whenever a local commit could not be pushed (persistent
        rejection, a rebase conflict, or any other git error after commit):
        the clone must never be left carrying a commit the remote does not
        have (`vault.repo.SyncDiverged` guards exactly that invariant
        elsewhere).
        """
        git = self._git()
        branch = self._config.branch
        git.run(*self._auth_args(), "fetch", "origin", branch)
        git.run("reset", "--hard", f"origin/{branch}")

    def remote_head(self) -> str:
        """The commit SHA of `origin/<branch>`, as of the last `fetch`.

        Callers fetch first (`rebase_onto_remote()` or `reset_to_remote()`
        already did, on the usual conflict path) - this only reads the
        already-fetched remote-tracking ref.
        """
        result = self._git().run("rev-parse", f"refs/remotes/origin/{self._config.branch}")
        return _decode(result.stdout).strip()

    def read_remote_file(self, rel: str) -> bytes | None:
        """The bytes of `rel` at `origin/<branch>`, or `None` if it is not there.

        Reads straight from the fetched remote-tracking ref via `git show`,
        never the working tree, so it still sees the remote's version after
        the caller has reset the working tree past it.
        """
        branch = self._config.branch
        result = self._git().run("show", f"origin/{branch}:{rel}", check=False)
        if result.returncode != 0:
            return None
        return result.stdout

    def commit_internal_file(self, rel: str, content: bytes, message: str) -> str:
        """Write `content` to `rel` and commit it as the `memory-manager` identity.

        `rel` must resolve via `paths.resolve_internal` (a conflict-file
        path) - this is for server-owned files, never a client write. If
        `content` is byte-identical to what is already committed there,
        does nothing and returns the current `head()` instead of failing on
        "no changes": overwriting a conflict file with fresh content is the
        normal case, not an error.
        """
        path = paths.resolve_internal(self._config.dir, rel)
        _atomic_write(path, content)
        git = self._git()
        git.run("add", "--", rel)
        diff = git.run("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            return self.head()
        committer = Author(name=_COMMITTER_NAME, email=_COMMITTER_EMAIL)
        git.run("commit", "-m", message, f"--author={committer.name} <{committer.email}>")
        return self.head()

    def sync(self) -> ChangeSet:
        """Fast-forward the local clone from `origin/<branch>` and report what changed.

        - Remote branch does not exist yet (nothing pushed there): an empty
          `ChangeSet`.
        - Local `HEAD` is unborn (clone made while the remote was empty):
          checks out the remote branch and reports every file in it as
          `added` (or `ignored`, if it is not a note path).
        - Local `HEAD` is an ancestor of the remote branch: fast-forwards
          (`merge --ff-only`) and reports the diff between the old and new
          `HEAD`, classified per `vault.paths.parse_note_path`. A rename is
          reported as a delete of the old path plus an add of the new one.
        - The remote branch is an ancestor of local `HEAD` (unpushed local
          commits, nothing new to pull): an empty `ChangeSet`, no error -
          the write queue pushes those commits later.
        - Neither is an ancestor of the other (diverged): raises
          `SyncDiverged`. Never merges, never rebases.
        """
        git = self._git()
        old_head = self._current_head(git)
        branch = self._config.branch

        remote_check = git.run(
            *self._auth_args(), "ls-remote", "--exit-code", "--heads", "origin", branch, check=False
        )
        if remote_check.returncode == 2:
            return ChangeSet(old_head=old_head, new_head=old_head)
        if remote_check.returncode != 0:
            raise GitError(
                ("ls-remote", "--exit-code", "--heads", "origin", branch),
                remote_check.returncode,
                _decode(remote_check.stderr),
            )

        git.run(*self._auth_args(), "fetch", "origin", branch)
        remote_head = _decode(git.run("rev-parse", f"refs/remotes/origin/{branch}").stdout).strip()

        if old_head is None:
            git.run("checkout", "-B", branch, f"origin/{branch}")
            new_head = self.head()
            return self._diff_changeset(git, None, new_head, _EMPTY_TREE, new_head)

        if old_head == remote_head:
            return ChangeSet(old_head=old_head, new_head=old_head)

        forward = git.run("merge-base", "--is-ancestor", old_head, remote_head, check=False)
        if forward.returncode == 0:
            git.run("merge", "--ff-only", f"origin/{branch}")
            new_head = self.head()
            return self._diff_changeset(git, old_head, new_head, old_head, new_head)

        backward = git.run("merge-base", "--is-ancestor", remote_head, old_head, check=False)
        if backward.returncode == 0:
            return ChangeSet(old_head=old_head, new_head=old_head)

        raise SyncDiverged(
            ("merge-base", "--is-ancestor", old_head, remote_head),
            1,
            f"local HEAD {old_head} and origin/{branch} ({remote_head}) have diverged",
        )

    def diff_since(self, rev: str | None) -> ChangeSet:
        """Diff `rev` (or the empty tree if `None`) against `refs/remotes/origin/<branch>`.

        Fetches the remote branch first, so this always reports against the
        remote's current tip, not whatever was fetched last. Returns an
        empty `ChangeSet` - not a `GitError` - if the remote branch does not
        exist yet (nothing has ever been pushed there).

        Unlike `sync()`, this never touches the working tree or local
        `HEAD`: it only reads the fetched remote-tracking ref and diffs it,
        so it is safe to call while the write queue is mid-write on this
        same clone (`storage.git.GitBackend.changes_since`, ADR-0007 §1) -
        at the cost of possibly racing it: the result can briefly lag or
        lead a concurrent `read()`/`write()` of the same path.
        """
        git = self._git()
        branch = self._config.branch

        remote_check = git.run(
            *self._auth_args(), "ls-remote", "--exit-code", "--heads", "origin", branch, check=False
        )
        if remote_check.returncode == 2:
            return ChangeSet(old_head=rev, new_head=rev)
        if remote_check.returncode != 0:
            raise GitError(
                ("ls-remote", "--exit-code", "--heads", "origin", branch),
                remote_check.returncode,
                _decode(remote_check.stderr),
            )

        git.run(*self._auth_args(), "fetch", "origin", branch)
        remote_head = _decode(git.run("rev-parse", f"refs/remotes/origin/{branch}").stdout).strip()
        diff_old_rev = rev if rev is not None else _EMPTY_TREE
        return self._diff_changeset(git, rev, remote_head, diff_old_rev, remote_head)

    def _current_head(self, git: Git) -> str | None:
        """The current `HEAD` commit SHA, or `None` if `HEAD` is unborn."""
        result = git.run("rev-parse", "--verify", "-q", "HEAD", check=False)
        if result.returncode != 0:
            return None
        return _decode(result.stdout).strip()

    def _diff_changeset(
        self,
        git: Git,
        old_head: str | None,
        new_head: str,
        diff_old_rev: str,
        diff_new_rev: str,
    ) -> ChangeSet:
        result = git.run("diff", "--name-status", "-M", "-z", diff_old_rev, diff_new_rev)
        added, modified, deleted, ignored = _classify_diff(result.stdout)
        return ChangeSet(
            old_head=old_head,
            new_head=new_head,
            added=added,
            modified=modified,
            deleted=deleted,
            ignored=ignored,
        )

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
        configured_key_file = self._config.ssh_key_file
        if configured_key_file is None:
            return {}
        key_file = self._ssh_key_file(configured_key_file)
        known_hosts = self._config.ssh_known_hosts_file
        # A pinned `known_hosts` is the production-recommended setting
        # (deploy/README.md): without one, `accept-new` trusts whatever host
        # key the remote presents on first contact, which is fine for a
        # throwaway/local remote but not for an operator's real deploy key.
        host_key_option = (
            f"-o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes"
            if known_hosts is not None
            else "-o StrictHostKeyChecking=accept-new"
        )
        ssh_command = f"ssh -i {key_file} -o IdentitiesOnly=yes {host_key_option}"
        return {"GIT_SSH_COMMAND": ssh_command}

    def _ssh_key_file(self, configured: Path) -> Path:
        """`configured`, or a private 0600 copy of it with a trailing newline.

        A Kubernetes Secret volume mounts its keys root-owned/0440 or 0644
        under `fsGroup` (there is no per-key `defaultMode` granular enough
        to land exactly on 0600 for one key among others in the same
        volume) - `ssh` refuses a key file that is group- or
        other-readable at all ("UNPROTECTED PRIVATE KEY FILE"), regardless
        of who can actually read it through that mode. A secret store can
        just as easily have dropped the key's own final `\n` (#77 - e.g. a
        shell `$(cat key)` substitution strips it on the way into the
        store); `ssh`/`libcrypto` then fails to load an otherwise-valid key
        with "error in libcrypto", permissions notwithstanding. When
        `configured` is not already safe to use as-is on both counts, it
        is copied once, the first time this is called, into a fresh
        `mkstemp` file (private to this process's uid, mode 0600 by
        construction, newline-terminated) and every later call reuses that
        same copy rather than copying again.
        """
        if self._resolved_ssh_key_file is not None:
            return self._resolved_ssh_key_file
        content = configured.read_bytes()
        if _is_private_key_file(configured) and content.endswith(b"\n"):
            self._resolved_ssh_key_file = configured
            return configured
        if not content.endswith(b"\n"):
            content += b"\n"

        fd, tmp_name = tempfile.mkstemp(prefix="memory-manager-ssh-key-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_name)
            raise
        self._resolved_ssh_key_file = Path(tmp_name)
        return self._resolved_ssh_key_file

    def _secrets(self) -> tuple[str, ...]:
        token = self._config.https_token
        return (token,) if token else ()

    def _auth_args(self) -> tuple[str, ...]:
        token = self._config.https_token
        if not token or not self._config.remote.startswith(("http://", "https://")):
            return ()
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        return ("-c", f"http.extraHeader=Authorization: Basic {encoded}")


def _is_private_key_file(path: Path) -> bool:
    """Whether `ssh` already accepts `path` as a private key file as-is.

    `ssh` checks this as an exact bit mask against the file's own mode -
    0600 or 0400, nothing else - plus ownership by the user running it; it
    is not the broader "can anyone but me read this" POSIX question. Any
    `OSError` (missing file, permission denied even to `stat` it) is "no",
    the same outcome ssh itself would eventually produce.
    """
    try:
        file_stat = path.stat()
    except OSError:
        return False
    mode = stat.S_IMODE(file_stat.st_mode)
    return file_stat.st_uid == os.getuid() and mode in (0o600, 0o400)


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


def _classify_diff(
    raw: bytes,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split `git diff --name-status -M -z` output into note vs. ignored buckets.

    A rename (`R...`) is reported as a delete of the old path plus an add
    of the new one, so a caller never has to special-case renames: the
    indexer just sees one path disappear and another appear.
    """
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    ignored: list[str] = []

    def bucket(rel: str, note_bucket: list[str]) -> None:
        if _is_note_path(rel):
            note_bucket.append(rel)
        else:
            ignored.append(rel)

    for status, path, other_path in _parse_name_status_z(raw):
        kind = status[0]
        if kind == "R":
            bucket(path, deleted)
            if other_path is not None:
                bucket(other_path, added)
        elif kind == "A":
            bucket(path, added)
        elif kind == "D":
            bucket(path, deleted)
        else:
            # "M" (modify) and everything else diff can report for a
            # tracked path (e.g. "T" typechange) are treated as a
            # modification - the file is still at the same path.
            bucket(path, modified)

    return tuple(added), tuple(modified), tuple(deleted), tuple(ignored)


def _parse_name_status_z(raw: bytes) -> list[tuple[str, str, str | None]]:
    """Parse the NUL-separated output of `git diff --name-status -z`.

    Each record is `status\\0path\\0` except a rename/copy
    (`R<score>`/`C<score>`), which is `status\\0old_path\\0new_path\\0`.
    """
    fields = raw.decode("utf-8").split("\0")
    if fields and fields[-1] == "":
        fields.pop()

    records: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            old_path = fields[index]
            new_path = fields[index + 1]
            index += 2
            records.append((status, old_path, new_path))
        else:
            path = fields[index]
            index += 1
            records.append((status, path, None))
    return records


def _is_note_path(rel: str) -> bool:
    try:
        paths.parse_note_path(rel, allow_archive=True)
    except PathRejected:
        return False
    return True

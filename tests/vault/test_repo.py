# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the vault's working copy: clone, commit-per-change, push (#11)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import human_commit

from memory_manager.config import VaultConfig
from memory_manager.vault.git import Git, GitError, PushRejected
from memory_manager.vault.paths import PathRejected
from memory_manager.vault.repo import Repo, author_for


def _log(remote: Path) -> list[tuple[str, ...]]:
    """The commit history of `remote` as `(sha, author_name, author_email,
    committer_name, committer_email)` tuples, newest first."""
    result = Git(cwd=remote).run("log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce")
    lines = result.stdout.decode("utf-8").strip("\n").split("\n")
    return [tuple(line.split("\x1f")) for line in lines if line]


class TestEnsureClone:
    def test_creates_working_copy_of_an_empty_remote(self, vault_config: VaultConfig) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        assert (vault_config.dir / ".git").is_dir()

    def test_is_idempotent(self, vault_config: VaultConfig) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        repo.ensure_clone()
        assert (vault_config.dir / ".git").is_dir()

    def test_rejects_existing_clone_with_foreign_origin(
        self, tmp_path: Path, bare_remote: Path
    ) -> None:
        other_remote = tmp_path / "other.git"
        other_remote.mkdir()
        Git(cwd=other_remote).run("init", "--bare", "-b", "main")
        vault_dir = tmp_path / "vault"
        Git(cwd=tmp_path).run("clone", "--origin", "origin", str(other_remote), str(vault_dir))

        config = VaultConfig(remote=str(bare_remote), dir=vault_dir, branch="main")
        with pytest.raises(GitError):
            Repo(config).ensure_clone()


class TestCommitFile:
    def test_two_clients_push_commits_with_correct_author_and_committer(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()

        sha1 = repo.commit_file(
            "personal/fact/a.md", b"first\n", author_for("claude-code"), "add a"
        )
        repo.push()
        sha2 = repo.commit_file("personal/fact/b.md", b"second\n", author_for("human"), "add b")
        repo.push()

        log = _log(bare_remote)
        assert log == [
            (
                sha2,
                "human",
                "human@memory-manager.invalid",
                "memory-manager",
                "memory-manager@memory-manager.invalid",
            ),
            (
                sha1,
                "claude-code",
                "claude-code@memory-manager.invalid",
                "memory-manager",
                "memory-manager@memory-manager.invalid",
            ),
        ]

    def test_raises_on_no_changes(self, vault_config: VaultConfig) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        repo.commit_file("personal/fact/a.md", b"same\n", author_for("human"), "add a")
        with pytest.raises(GitError):
            repo.commit_file("personal/fact/a.md", b"same\n", author_for("human"), "add a again")

    def test_invalid_path_is_rejected_before_any_write(self, vault_config: VaultConfig) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        with pytest.raises(PathRejected):
            repo.commit_file("../escape.md", b"x\n", author_for("human"), "escape")
        assert not (vault_config.dir.parent / "escape.md").exists()

    def test_unknown_client_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            author_for("robot")


class TestPush:
    def test_push_rejected_when_remote_moved_ahead(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        repo.commit_file("personal/fact/a.md", b"first\n", author_for("claude-code"), "add a")
        repo.push()

        human_commit(bare_remote, "personal/fact/b.md", b"human change\n")

        repo.commit_file("personal/fact/c.md", b"third\n", author_for("claude-code"), "add c")
        with pytest.raises(PushRejected):
            repo.push()

    def test_push_rejected_via_ref_lock_race_is_also_a_push_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A racing push can be rejected server-side with wording other than
        the usual client-side '[rejected] ... (non-fast-forward)': two pushes
        landing at almost the same time on the remote can instead produce
        '! [remote rejected] ... (failed to update ref)' plus a 'cannot lock
        ref' detail line (#16) - still "the remote moved", must still be a
        `PushRejected`, not a fatal `GitError`.
        """
        stderr = (
            b"remote: error: cannot lock ref 'refs/heads/main': "
            b"is at aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa but expected "
            b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            b"To /some/remote.git\n"
            b" ! [remote rejected] HEAD -> main (failed to update ref)\n"
            b"error: failed to push some refs to '/some/remote.git'\n"
        )
        fake_result = subprocess.CompletedProcess(
            args=["git", "push", "origin", "HEAD:main"], returncode=1, stdout=b"", stderr=stderr
        )
        monkeypatch.setattr(
            "memory_manager.vault.git.subprocess.run", lambda *args, **kwargs: fake_result
        )

        with pytest.raises(PushRejected):
            Git(cwd=tmp_path).run("push", "origin", "HEAD:main")


class TestSecurity:
    def test_https_token_never_appears_in_error_message(self, tmp_path: Path) -> None:
        token = "s3cr3t-token-value"  # noqa: S105
        config = VaultConfig(
            remote="https://127.0.0.1:1/vault.git",
            dir=tmp_path / "vault",
            branch="main",
            https_token=token,
        )
        with pytest.raises(GitError) as excinfo:
            Repo(config).ensure_clone()
        assert token not in str(excinfo.value)
        assert token not in excinfo.value.stderr
        assert token not in " ".join(excinfo.value.command)

    def test_global_git_config_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vault_config: VaultConfig
    ) -> None:
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        (fake_home / ".gitconfig").write_text(
            "[commit]\n\tgpgsign = true\n[user]\n\tsigningkey = DOES-NOT-EXIST\n"
        )
        monkeypatch.setenv("HOME", str(fake_home))

        repo = Repo(vault_config)
        repo.ensure_clone()
        sha = repo.commit_file("personal/fact/a.md", b"content\n", author_for("human"), "add a")
        assert sha

# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager connect <client>` (#137): the flow `cli.py`'s `connect` branch calls
directly, one function per subcommand - read the client's current config, merge the server
entry in with that client's `ClientAdapter`, print a diff, then back up and write unless
`--dry-run` or nothing actually changed.

`run_claude_code` is the only one with a config file to merge into; `run_claude_ai` prints the
manual connector setup steps instead (`ClaudeAiAdapter.web_steps`) - there is no local file for
claude.ai.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from memory_manager.clients.base import DEFAULT_TOKEN_ENV, SERVER_NAME, ServerEntry
from memory_manager.clients.claude_ai import ClaudeAiAdapter
from memory_manager.clients.claude_code import ClaudeCodeAdapter, instructions_content
from memory_manager.clients.files import ConcurrentModificationError, render_diff, write_config
from memory_manager.clients.jsonconfig import ClientConfigError

__all__ = ["run_claude_ai", "run_claude_code"]


def run_claude_code(
    *,
    scope: str,
    transport: str,
    url: str | None,
    token_env: str | None,
    inline_token: bool,
    with_instructions: bool,
    dry_run: bool,
    project_dir: Path,
    env: Mapping[str, str] | None = None,
) -> int:
    """`connect claude-code`: merge the memory-manager server into Claude Code's config."""
    env = dict(os.environ) if env is None else dict(env)
    project_dir = project_dir.resolve()

    if scope == "project" and inline_token:
        print(
            "connect claude-code: --inline-token is not allowed with --scope project - a "
            "committed .mcp.json must carry no credentials",
            file=sys.stderr,
        )
        return 2
    if scope == "project" and transport == "stdio":
        print(
            "connect claude-code: --transport stdio is not allowed with --scope project - "
            "its VAULT_* credentials stay in --scope user",
            file=sys.stderr,
        )
        return 2
    if transport == "http" and not url:
        print("connect claude-code: --url is required for --transport http", file=sys.stderr)
        return 2

    inline_token_value: str | None = None
    if inline_token:
        if token_env is None:
            token_env = DEFAULT_TOKEN_ENV
        inline_token_value = env.get(token_env, "")
        if not inline_token_value:
            print(
                f"connect claude-code: --inline-token needs {token_env} set and non-empty "
                "in this process's environment",
                file=sys.stderr,
            )
            return 2

    adapter = ClaudeCodeAdapter()
    home = Path(env.get("HOME") or str(Path.home()))
    config_path = adapter.locate_config(scope=scope, home=home, project_dir=project_dir, env=env)

    if transport == "http":
        entry = ServerEntry(
            name=SERVER_NAME,
            transport="http",
            url=url,
            token_env=token_env,
            inline_token_value=inline_token_value,
        )
    else:
        entry = ServerEntry(
            name=SERVER_NAME, transport="stdio", command="memory-manager", args=("serve", "--stdio")
        )

    exit_code = _apply_change(
        config_path,
        lambda current: adapter.merge(current, entry),
        dry_run=dry_run,
        mask=inline_token_value,
    )
    if exit_code != 0:
        return exit_code

    if scope == "user":
        _warn_if_local_scope_shadows(config_path, project_dir)

    if with_instructions:
        instructions_path = adapter.instructions_file(project_dir)
        exit_code = _apply_change(
            instructions_path, lambda _current: instructions_content(), dry_run=dry_run
        )
        if exit_code != 0:
            return exit_code

    if not dry_run:
        print()
        print(
            "Next step: run `claude mcp get memory-manager` or open `/mcp` in a Claude Code "
            "session and select Authenticate. Restart any already-running Claude Code "
            "sessions so they pick this up."
        )
        if transport == "http" and token_env is None:
            print(
                "No --token-env was set: without a token or an OAuth session, requests get a "
                "401 (no Authorization header is sent)."
            )
    return 0


def run_claude_ai(*, url: str) -> int:
    """`connect claude-ai`: print the custom-connector setup steps for `url`."""
    for step in ClaudeAiAdapter().web_steps(url):
        print(step)
    return 0


def _apply_change(
    path: Path,
    current_to_new: Callable[[str | None], str],
    *,
    dry_run: bool,
    mask: str | None = None,
) -> int:
    try:
        current_text: str | None = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current_text = None
    except OSError as exc:
        print(f"connect: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    try:
        new_text = current_to_new(current_text)
    except ClientConfigError as exc:
        print(f"connect: {path} is not safe to merge into: {exc}", file=sys.stderr)
        return 2

    if current_text is not None and new_text == current_text:
        print(f"{path}: already up to date")
        return 0

    diff = render_diff(current_text or "", new_text, path=path, mask=mask)
    sys.stdout.write(diff)

    if dry_run:
        print(f"{path}: --dry-run, nothing written")
        return 0

    try:
        backup = write_config(path, new_text, base_text=current_text)
    except ConcurrentModificationError as exc:
        print(f"connect: {exc}", file=sys.stderr)
        return 2

    if backup is not None:
        print(f"{path}: backed up the previous content to {backup}")
    print(f"{path}: written")
    return 0


def _warn_if_local_scope_shadows(config_path: Path, project_dir: Path) -> None:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return
    project_entry = projects.get(str(project_dir))
    if not isinstance(project_entry, dict):
        return
    local_servers = project_entry.get("mcpServers")
    if isinstance(local_servers, dict) and SERVER_NAME in local_servers:
        print(
            f"connect: a local-scope '{SERVER_NAME}' entry for {project_dir} already exists "
            "and takes precedence over the user-scope one just written (local > project > "
            "user); remove it with `claude mcp remove memory-manager --scope local` if you "
            "want the user-scope entry to apply here.",
            file=sys.stderr,
        )

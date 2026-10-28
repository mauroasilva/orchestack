# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Factory for constructing `AgentBackend` instances from `RepoConfig`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from orchestack.agents.antigravity import AntigravityAgent
from orchestack.agents.base import AgentBackend
from orchestack.agents.claude_code import ClaudeCodeAgent
from orchestack.agents.codex import CodexAgent
from orchestack.config import RepoConfig

_ANTIGRAVITY_ALIASES = {"agy", "gemini", "antigravity"}
_CLAUDE_ALIASES = {"claude", "claude-code", "claude_code", "claudecode"}
_CODEX_ALIASES = {"codex", "openai-codex", "openai_codex"}


def normalize_agent_backend_name(name: str) -> str:
    """Normalize an agent backend identifier to `'agy'`, `'claude'`, or `'codex'`."""
    cleaned = name.strip().lower()
    if cleaned in _ANTIGRAVITY_ALIASES:
        return "agy"
    if cleaned in _CLAUDE_ALIASES:
        return "claude"
    if cleaned in _CODEX_ALIASES:
        return "codex"
    msg = (
        f"Unsupported agent backend: {name!r}. "
        "Expected one of: agy, gemini, antigravity, claude, codex."
    )
    raise ValueError(msg)


def create_agent_backend(
    config: RepoConfig | str | None = None,
    *,
    model: str | None = None,
    effort: str | None = None,
    agent_bin: str | None = None,
    cli_log_path: Path | None = None,
    repo_root: Path | None = None,
    allowed_tools: Sequence[str] | None = None,
    sandbox_mode: str | None = None,
    full_auto: bool = True,
    add_dirs: Sequence[str] | None = None,
) -> AgentBackend:
    """Instantiate `AntigravityAgent`, `ClaudeCodeAgent`, or `CodexAgent` from `config`."""
    if isinstance(config, RepoConfig):
        raw_name = config.agent_backend or config.agent_type or "agy"
        resolved_root = repo_root or config.repo_root
    elif isinstance(config, str):
        raw_name = config
        resolved_root = repo_root or Path.cwd()
    else:
        raw_name = "agy"
        resolved_root = repo_root or Path.cwd()

    normalized = normalize_agent_backend_name(raw_name)
    if normalized == "agy":
        return AntigravityAgent(
            agy_bin=agent_bin or "agy",
            model=model,
            effort=effort,
            cli_log_file=cli_log_path,
        )
    if normalized == "claude":
        return ClaudeCodeAgent(
            claude_bin=agent_bin or "claude",
            model=model,
            allowed_tools=allowed_tools,
            cli_log_path=cli_log_path,
        )
    return CodexAgent(
        codex_bin=agent_bin or "codex",
        model=model,
        sandbox_mode=sandbox_mode,
        full_auto=full_auto,
        add_dirs=add_dirs,
        repo_root=resolved_root,
        cli_log_path=cli_log_path,
    )

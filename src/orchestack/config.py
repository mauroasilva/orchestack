# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Repository configuration and runtime path resolution for Orchestack."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")
_CHECK_TARGET_RE = re.compile(r"^check\s*:", re.MULTILINE)


def _parse_bool_env(value: str, var_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    msg = f"Invalid boolean value for {var_name}: {value!r}"
    raise ValueError(msg)


def detect_base_branch(repo_root: Path) -> str:
    """Auto-detect the repository's default base branch (`main`, `master`, etc.)."""
    try:
        res = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            ref = res.stdout.strip()
            prefix = "refs/remotes/origin/"
            if ref.startswith(prefix):
                branch = ref[len(prefix) :].strip()
                if branch:
                    return branch
    except (OSError, subprocess.SubprocessError):
        pass

    for candidate_ref, branch_name in (
        ("refs/remotes/origin/main", "main"),
        ("refs/remotes/origin/master", "master"),
        ("refs/heads/main", "main"),
        ("refs/heads/master", "master"),
    ):
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", candidate_ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if res.returncode == 0:
                return branch_name
        except (OSError, subprocess.SubprocessError):
            break

    return "main"


def detect_check_cmd(repo_root: Path) -> str:
    """Auto-detect the verification command for the target repository."""
    for name in _MAKEFILE_NAMES:
        makefile = repo_root / name
        if makefile.is_file():
            try:
                content = makefile.read_text(encoding="utf-8", errors="replace")
                if _CHECK_TARGET_RE.search(content):
                    return "make check"
            except OSError:
                pass

    if (repo_root / "pyproject.toml").is_file():
        return "pytest"
    if (repo_root / "package.json").is_file():
        return "npm test"
    if (repo_root / "Cargo.toml").is_file():
        return "cargo test"
    if (repo_root / "go.mod").is_file():
        return "go test ./..."
    return "true"


def _flatten_toml_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Extract normalized config keys from top-level and optional section tables."""
    merged: dict[str, Any] = {}

    for key, value in raw.items():
        if not isinstance(value, dict):
            merged[key] = value

    for section_name in ("orchestack", "repo", "dod", "agent", "forge"):
        section = raw.get(section_name)
        if isinstance(section, dict):
            for key, value in section.items():
                if not isinstance(value, dict):
                    merged[key] = value

    # Normalize aliases
    if "forge" in merged and "forge_type" not in merged and isinstance(merged["forge"], str):
        merged["forge_type"] = merged.pop("forge")
    if "provider" in merged and "forge_type" not in merged and isinstance(merged["provider"], str):
        merged["forge_type"] = merged.pop("provider")
    if (
        "forge_provider" in merged
        and "forge_type" not in merged
        and isinstance(merged["forge_provider"], str)
    ):
        merged["forge_type"] = merged.pop("forge_provider")
    if "host" in merged and "forge_host" not in merged and isinstance(merged["host"], str):
        merged["forge_host"] = merged.pop("host")
    if "project" in merged and "forge_project" not in merged and isinstance(merged["project"], str):
        merged["forge_project"] = merged.pop("project")
    if "agent" in merged and "agent_type" not in merged and isinstance(merged["agent"], str):
        merged["agent_type"] = merged.pop("agent")
    if "backend" in merged and "agent_type" not in merged and isinstance(merged["backend"], str):
        merged["agent_type"] = merged.pop("backend")
    if (
        "agent_backend" in merged
        and "agent_type" not in merged
        and isinstance(merged["agent_backend"], str)
    ):
        merged["agent_type"] = merged.pop("agent_backend")

    return merged


@dataclass
class RepoConfig:
    """Configuration for a target repository managed by Orchestack."""

    repo_root: Path
    base_branch: str = "main"
    branch_prefix: str = "orchestack"
    check_cmd: str = "make check"
    forge_type: str = "auto"  # "auto" | "github" | "gitlab" | "local"
    agent_type: str = "gemini"  # "gemini" | "agy" | "claude" | "codex" | "shell"
    require_learnings: bool = True
    require_pr_description: bool = True
    auto_whitelist_collaborators: bool = True
    trusted_authors: list[str] = field(default_factory=list)
    forge_provider: str = ""
    forge_host: str = ""
    forge_project: str = ""
    agent_backend: str = ""

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root).resolve()
        if self.forge_provider and self.forge_type == "auto":
            self.forge_type = self.forge_provider
        elif not self.forge_provider:
            self.forge_provider = self.forge_type
        if self.agent_backend and self.agent_type == "gemini":
            self.agent_type = self.agent_backend
        elif not self.agent_backend:
            self.agent_backend = self.agent_type

    @property
    def queued_dir(self) -> Path:
        return self.repo_root / "prompts" / "queued"

    @property
    def implemented_dir(self) -> Path:
        return self.repo_root / "prompts" / "implemented"

    @property
    def learnings_dir(self) -> Path:
        return self.repo_root / "docs" / "learnings"

    @property
    def pr_description_path(self) -> Path:
        return self.repo_root / "docs" / "PR_DESCRIPTION.md"

    @property
    def daemon_dir(self) -> Path:
        return self.repo_root / ".daemon"

    @property
    def logs_dir(self) -> Path:
        return self.repo_root / "logs"

    @property
    def lock_file(self) -> Path:
        return self.daemon_dir / "daemon.lock"

    @property
    def review_state_file(self) -> Path:
        return self.daemon_dir / "review_state.json"

    @property
    def pr_reviews_file(self) -> Path:
        return self.logs_dir / "pr_reviews.json"

    @property
    def daemon_metrics_file(self) -> Path:
        return self.logs_dir / "daemon_metrics.jsonl"


def load_repo_config(
    repo_root: Path | str | None = None,
    *,
    base_branch: str | None = None,
    branch_prefix: str | None = None,
    check_cmd: str | None = None,
    forge_type: str | None = None,
    forge_provider: str | None = None,
    forge_host: str | None = None,
    forge_project: str | None = None,
    agent_type: str | None = None,
    agent_backend: str | None = None,
    require_learnings: bool | None = None,
    require_pr_description: bool | None = None,
    auto_whitelist_collaborators: bool | None = None,
    trusted_authors: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> RepoConfig:
    """Load `RepoConfig` with precedence: CLI > `ORCHESTACK_*` env > `.orchestack.toml`."""
    environ = os.environ if env is None else env

    # 1. Resolve repo_root (CLI > env > cwd)
    resolved_root_raw: Path | str
    if repo_root is not None:
        resolved_root_raw = repo_root
    elif environ.get("ORCHESTACK_REPO"):
        resolved_root_raw = environ["ORCHESTACK_REPO"]
    elif environ.get("ORCHESTACK_REPO_ROOT"):
        resolved_root_raw = environ["ORCHESTACK_REPO_ROOT"]
    else:
        resolved_root_raw = Path.cwd()
    resolved_root = Path(resolved_root_raw).expanduser().resolve()

    # 2. Load .orchestack.toml if present
    toml_data: dict[str, Any] = {}
    config_file = resolved_root / ".orchestack.toml"
    if config_file.is_file():
        with config_file.open("rb") as f:
            raw_toml = tomllib.load(f)
        toml_data = _flatten_toml_config(raw_toml)

    # 3. Resolve each setting: CLI > env > TOML > auto-detected / default
    final_base_branch = (
        base_branch
        if base_branch is not None
        else environ.get("ORCHESTACK_BASE_BRANCH")
        or (str(toml_data["base_branch"]) if "base_branch" in toml_data else None)
        or detect_base_branch(resolved_root)
    )

    final_branch_prefix = (
        branch_prefix
        if branch_prefix is not None
        else environ.get("ORCHESTACK_BRANCH_PREFIX")
        or (str(toml_data["branch_prefix"]) if "branch_prefix" in toml_data else None)
        or "orchestack"
    )

    final_check_cmd = (
        check_cmd
        if check_cmd is not None
        else environ.get("ORCHESTACK_CHECK_CMD")
        or (str(toml_data["check_cmd"]) if "check_cmd" in toml_data else None)
        or detect_check_cmd(resolved_root)
    )

    explicit_forge = forge_type if forge_type is not None else forge_provider
    final_forge_type = (
        explicit_forge
        if explicit_forge is not None
        else environ.get("ORCHESTACK_FORGE")
        or environ.get("ORCHESTACK_FORGE_TYPE")
        or environ.get("ORCHESTACK_FORGE_PROVIDER")
        or (str(toml_data["forge_type"]) if "forge_type" in toml_data else None)
        or "auto"
    )

    final_forge_host = (
        forge_host
        if forge_host is not None
        else environ.get("ORCHESTACK_FORGE_HOST")
        or (str(toml_data["forge_host"]) if "forge_host" in toml_data else None)
        or ""
    )

    final_forge_project = (
        forge_project
        if forge_project is not None
        else environ.get("ORCHESTACK_FORGE_PROJECT")
        or (str(toml_data["forge_project"]) if "forge_project" in toml_data else None)
        or ""
    )

    explicit_agent = agent_type if agent_type is not None else agent_backend
    final_agent_type = (
        explicit_agent
        if explicit_agent is not None
        else environ.get("ORCHESTACK_AGENT")
        or environ.get("ORCHESTACK_AGENT_TYPE")
        or environ.get("ORCHESTACK_AGENT_BACKEND")
        or (str(toml_data["agent_type"]) if "agent_type" in toml_data else None)
        or "gemini"
    )

    if require_learnings is not None:
        final_require_learnings = require_learnings
    elif "ORCHESTACK_REQUIRE_LEARNINGS" in environ:
        final_require_learnings = _parse_bool_env(
            environ["ORCHESTACK_REQUIRE_LEARNINGS"],
            "ORCHESTACK_REQUIRE_LEARNINGS",
        )
    elif "require_learnings" in toml_data:
        final_require_learnings = bool(toml_data["require_learnings"])
    else:
        final_require_learnings = True

    if require_pr_description is not None:
        final_require_pr_description = require_pr_description
    elif "ORCHESTACK_REQUIRE_PR_DESCRIPTION" in environ:
        final_require_pr_description = _parse_bool_env(
            environ["ORCHESTACK_REQUIRE_PR_DESCRIPTION"],
            "ORCHESTACK_REQUIRE_PR_DESCRIPTION",
        )
    elif "require_pr_description" in toml_data:
        final_require_pr_description = bool(toml_data["require_pr_description"])
    else:
        final_require_pr_description = True

    if auto_whitelist_collaborators is not None:
        final_auto_whitelist = auto_whitelist_collaborators
    elif "ORCHESTACK_AUTO_WHITELIST_COLLABORATORS" in environ:
        final_auto_whitelist = _parse_bool_env(
            environ["ORCHESTACK_AUTO_WHITELIST_COLLABORATORS"],
            "ORCHESTACK_AUTO_WHITELIST_COLLABORATORS",
        )
    elif "auto_whitelist_collaborators" in toml_data:
        final_auto_whitelist = bool(toml_data["auto_whitelist_collaborators"])
    else:
        final_auto_whitelist = True

    if trusted_authors is not None:
        final_trusted_authors = list(trusted_authors)
    elif "ORCHESTACK_TRUSTED_AUTHORS" in environ:
        raw_authors = environ["ORCHESTACK_TRUSTED_AUTHORS"]
        final_trusted_authors = [a.strip() for a in raw_authors.split(",") if a.strip()]
    elif "trusted_authors" in toml_data and isinstance(toml_data["trusted_authors"], list):
        final_trusted_authors = [str(a) for a in toml_data["trusted_authors"]]
    else:
        final_trusted_authors = []

    return RepoConfig(
        repo_root=resolved_root,
        base_branch=final_base_branch,
        branch_prefix=final_branch_prefix,
        check_cmd=final_check_cmd,
        forge_type=final_forge_type,
        forge_provider=final_forge_type,
        forge_host=final_forge_host,
        forge_project=final_forge_project,
        agent_type=final_agent_type,
        agent_backend=final_agent_type,
        require_learnings=final_require_learnings,
        require_pr_description=final_require_pr_description,
        auto_whitelist_collaborators=final_auto_whitelist,
        trusted_authors=final_trusted_authors,
    )


def configure_runtime(config: RepoConfig) -> RepoConfig:
    """Bind a `RepoConfig` to `scripts.daemon_runner` if imported, and return `config`."""
    for mod_name in ("scripts.daemon_runner", "daemon_runner"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "configure_runtime"):
            mod.configure_runtime(config)
    return config

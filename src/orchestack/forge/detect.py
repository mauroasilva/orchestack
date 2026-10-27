# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Git remote URL parsing and automatic forge provider detection."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from orchestack.config import RepoConfig
from orchestack.forge.base import ForgeProvider
from orchestack.forge.github import GitHubForge, RunCmdCallable, _default_run_cmd
from orchestack.forge.gitlab import GitLabForge

_SCP_REMOTE_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(.+)$")


def _clean_project_path(raw_path: str) -> str:
    """Normalize a repository path (`group/subgroup/repo.git` -> `group/subgroup/repo`)."""
    cleaned = raw_path.strip().strip("/")
    if "/-/" in cleaned:
        cleaned = cleaned.split("/-/", 1)[0].strip("/")
    elif cleaned.endswith("/-"):
        cleaned = cleaned[:-2].strip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4].strip("/")
    return cleaned


def _infer_forge_type(host: str, project_path: str) -> str:
    """Infer `'github'` or `'gitlab'` from the remote hostname and project path depth."""
    lowered_host = host.lower()
    if "gitlab" in lowered_host:
        return "gitlab"
    if "github" in lowered_host:
        return "github"
    if project_path.count("/") >= 2:
        return "gitlab"
    return "github"


def parse_git_remote(remote_url: str) -> tuple[str, str, str]:
    """Parse a git remote URL and return `(forge_type, host, project_path)`.

    Supports both HTTPS (`https://gitlab.com/group/subgroup/repo.git`) and SSH
    (`git@gitlab.example.com:group/subgroup/repo.git` or `ssh://git@host/group/repo.git`) URLs.
    """
    cleaned_url = remote_url.strip()
    if not cleaned_url:
        msg = "Empty git remote URL"
        raise ValueError(msg)

    host = ""
    raw_path = ""

    if "://" in cleaned_url:
        parsed = urllib.parse.urlsplit(cleaned_url)
        host = (parsed.hostname or "").strip().lower()
        raw_path = parsed.path or ""
    else:
        scp_match = _SCP_REMOTE_RE.match(cleaned_url)
        if scp_match:
            host = scp_match.group(1).strip().lower()
            raw_path = scp_match.group(2)

    project_path = _clean_project_path(raw_path)
    segments = [seg for seg in project_path.split("/") if seg]
    if not host or len(segments) < 2 or "//" in project_path:
        msg = f"Unsupported or invalid git remote URL: {remote_url!r}"
        raise ValueError(msg)

    normalized_path = "/".join(segments)
    forge_type = _infer_forge_type(host, normalized_path)
    return (forge_type, host, normalized_path)


def get_git_remote_url(
    repo_root: Path | None = None,
    remote_name: str = "origin",
    run_cmd_fn: RunCmdCallable | None = None,
) -> str:
    """Return the configured URL for git remote `remote_name`, or `""` on failure."""
    cmd = ["git", "remote", "get-url", remote_name]
    try:
        if run_cmd_fn is not None:
            res = run_cmd_fn(cmd, check=False)
        else:
            res = _default_run_cmd(cmd, cwd=repo_root, check=False)
        if res.returncode == 0 and res.stdout:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def detect_forge_type(
    repo_root: Path | None = None,
    remote_url: str | None = None,
    run_cmd_fn: RunCmdCallable | None = None,
) -> str:
    """Auto-detect `'github'` or `'gitlab'` from `remote_url` or `git remote get-url origin`."""
    resolved_url = (
        remote_url
        if remote_url is not None
        else get_git_remote_url(repo_root=repo_root, run_cmd_fn=run_cmd_fn)
    )
    if not resolved_url:
        return "github"
    forge_type, _, _ = parse_git_remote(resolved_url)
    return forge_type


def create_forge_provider(
    config: RepoConfig,
    *,
    run_cmd_fn: RunCmdCallable | None = None,
    gh_bin: str = "gh",
    glab_bin: str = "glab",
    remote_url: str | None = None,
) -> ForgeProvider:
    """Instantiate a `ForgeProvider` (`GitHubForge` or `GitLabForge`) from `config`."""
    raw_provider = (
        config.forge_provider
        if config.forge_provider and config.forge_provider != "auto"
        else (config.forge_type or config.forge_provider or "auto")
    )
    provider = raw_provider.strip().lower()
    host = config.forge_host.strip() or None
    project_path = config.forge_project.strip() or None

    resolved_url = (
        remote_url
        if remote_url is not None
        else get_git_remote_url(repo_root=config.repo_root, run_cmd_fn=run_cmd_fn)
    )
    if resolved_url:
        try:
            detected_type, detected_host, detected_project = parse_git_remote(resolved_url)
            if provider == "auto":
                provider = detected_type
            if host is None:
                host = detected_host
            if project_path is None:
                project_path = detected_project
        except ValueError:
            if provider == "auto":
                provider = "github"
    elif provider == "auto":
        provider = "github"

    if provider == "github":
        return GitHubForge(
            repo_root=config.repo_root,
            gh_bin=gh_bin,
            run_cmd_fn=run_cmd_fn,
        )
    if provider == "gitlab":
        return GitLabForge(
            repo_root=config.repo_root,
            glab_bin=glab_bin,
            project_path=project_path,
            host=host,
            run_cmd_fn=run_cmd_fn,
        )

    msg = f"Unsupported forge provider: {provider!r}"
    raise ValueError(msg)

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for `GitLabForge`, `parse_git_remote`, and `create_forge_provider`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestack.config import RepoConfig
from orchestack.forge import (
    GITLAB_CI_QUOTA_PATTERNS,
    GITLAB_QUOTA_FAILURE_REASONS,
    ForgeProvider,
    GitHubForge,
    GitLabForge,
    classify_gitlab_ci_status,
    create_forge_provider,
    detect_forge_type,
    encode_gitlab_project_path,
    is_gitlab_ci_quota_failure,
    normalize_gitlab_mr_state,
    parse_git_remote,
    truncate_pr_comment,
)


def _proc(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args or ["glab"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_gitlab_forge_implements_forge_provider_protocol(tmp_path: Path) -> None:
    forge = GitLabForge(repo_root=tmp_path, project_path="group/subgroup/repo")
    assert isinstance(forge, ForgeProvider)


def test_parse_git_remote_https_and_ssh_variants() -> None:
    # GitLab HTTPS with nested subgroup
    assert parse_git_remote("https://gitlab.com/group/subgroup/repo.git") == (
        "gitlab",
        "gitlab.com",
        "group/subgroup/repo",
    )
    # GitLab SSH SCP-style with nested subgroup and self-hosted domain
    assert parse_git_remote("git@gitlab.example.com:group/subgroup/repo.git") == (
        "gitlab",
        "gitlab.example.com",
        "group/subgroup/repo",
    )
    # GitLab ssh:// URL with custom port and deep subgroup hierarchy
    assert parse_git_remote("ssh://git@gitlab.corp.internal:2222/sec/blue/team/orchestack.git") == (
        "gitlab",
        "gitlab.corp.internal",
        "sec/blue/team/orchestack",
    )
    # GitLab web URL containing /-/ suffix
    assert parse_git_remote("https://gitlab.com/group/subgroup/repo/-/merge_requests") == (
        "gitlab",
        "gitlab.com",
        "group/subgroup/repo",
    )
    # Self-hosted domain without "gitlab" in hostname but nested subgroups -> gitlab
    assert parse_git_remote("git@code.internal.corp:org/division/project.git") == (
        "gitlab",
        "code.internal.corp",
        "org/division/project",
    )
    # GitHub HTTPS and SSH
    assert parse_git_remote("https://github.com/acme-sec/orchestack.git") == (
        "github",
        "github.com",
        "acme-sec/orchestack",
    )
    assert parse_git_remote("git@github.com:acme-sec/orchestack.git") == (
        "github",
        "github.com",
        "acme-sec/orchestack",
    )


def test_parse_git_remote_invalid_urls() -> None:
    for invalid_url in ("", "   ", "not-a-url", "https://gitlab.com/single-segment", "git@host:"):
        with pytest.raises(ValueError, match="git remote URL"):
            parse_git_remote(invalid_url)


def test_detect_forge_type_and_create_forge_provider(tmp_path: Path) -> None:
    # 1. Auto-detect GitLab from git remote
    def run_gitlab_remote(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _proc(0, stdout="git@gitlab.example.com:sec/subgroup/orchestack.git\n")
        return _proc(1)

    assert detect_forge_type(repo_root=tmp_path, run_cmd_fn=run_gitlab_remote) == "gitlab"

    cfg_auto = RepoConfig(repo_root=tmp_path, forge_type="auto")
    provider_gl = create_forge_provider(cfg_auto, run_cmd_fn=run_gitlab_remote)
    assert isinstance(provider_gl, GitLabForge)
    assert provider_gl.host == "gitlab.example.com"
    assert provider_gl.get_repo_slug() == "sec/subgroup/orchestack"
    assert provider_gl.encoded_project_path == "sec%2Fsubgroup%2Forchestack"

    # 2. Auto-detect GitHub from git remote
    def run_github_remote(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _proc(0, stdout="https://github.com/acme/orchestack.git\n")
        return _proc(1)

    provider_gh = create_forge_provider(cfg_auto, run_cmd_fn=run_github_remote)
    assert isinstance(provider_gh, GitHubForge)

    # 3. Explicit forge_provider="gitlab" with configured host and project
    cfg_explicit_gl = RepoConfig(
        repo_root=tmp_path,
        forge_provider="gitlab",
        forge_host="gitlab.custom.org",
        forge_project="platform/security/orchestack",
    )
    provider_explicit = create_forge_provider(
        cfg_explicit_gl,
        run_cmd_fn=lambda cmd, **kw: _proc(1),
    )
    assert isinstance(provider_explicit, GitLabForge)
    assert provider_explicit.host == "gitlab.custom.org"
    assert provider_explicit.encoded_project_path == "platform%2Fsecurity%2Forchestack"

    # 4. Unsupported forge provider raises ValueError
    cfg_invalid = RepoConfig(repo_root=tmp_path, forge_type="bitbucket")
    with pytest.raises(ValueError, match="Unsupported forge provider"):
        create_forge_provider(cfg_invalid, run_cmd_fn=lambda cmd, **kw: _proc(1))


def test_encode_gitlab_project_path_and_get_repo_slug(tmp_path: Path) -> None:
    assert encode_gitlab_project_path("group/subgroup/repo") == "group%2Fsubgroup%2Frepo"
    assert encode_gitlab_project_path("/group/sub/deep/repo.git/") == "group%2Fsub%2Fdeep%2Frepo"

    # Fallback when project_path is not set -> ":id" before slug lookup
    forge_default = GitLabForge(repo_root=tmp_path, run_cmd_fn=lambda cmd, **kw: _proc(1))
    assert forge_default.encoded_project_path == ":id"
    assert forge_default.get_repo_slug() == ""

    # Resolution via `glab repo view -F json` when git remote fails
    def run_glab_repo_view(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "remote", "get-url"]:
            return _proc(1)
        if cmd[:3] == ["glab", "repo", "view"]:
            return _proc(
                0,
                stdout=json.dumps({"path_with_namespace": "sec-group/sub/orchestack"}),
            )
        return _proc(1)

    forge_view = GitLabForge(repo_root=tmp_path, run_cmd_fn=run_glab_repo_view)
    assert forge_view.get_repo_slug() == "sec-group/sub/orchestack"
    assert forge_view.encoded_project_path == "sec-group%2Fsub%2Forchestack"


def test_normalize_gitlab_mr_state() -> None:
    assert normalize_gitlab_mr_state("opened") == "OPEN"
    assert normalize_gitlab_mr_state("OPEN") == "OPEN"
    assert normalize_gitlab_mr_state("merged") == "MERGED"
    assert normalize_gitlab_mr_state("closed") == "CLOSED"
    assert normalize_gitlab_mr_state("locked") == "LOCKED"
    assert normalize_gitlab_mr_state(None) == "UNKNOWN"


def test_list_open_prs_and_api_fallback(tmp_path: Path) -> None:
    raw_mrs = [
        {
            "iid": 101,
            "title": "feat(0003): gitlab forge",
            "state": "opened",
            "source_branch": "gemini/0003-gitlab",
            "target_branch": "main",
            "web_url": "https://gitlab.com/group/subgroup/repo/-/merge_requests/101",
            "draft": False,
            "detailed_merge_status": "mergeable",
        },
        {
            "iid": 102,
            "title": "chore: manual mr",
            "state": "opened",
            "source_branch": "feature/manual",
            "target_branch": "main",
            "web_url": "https://gitlab.com/group/subgroup/repo/-/merge_requests/102",
        },
    ]

    forge = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=lambda cmd, **kw: _proc(0, stdout=json.dumps(raw_mrs)),
    )
    all_open = forge.list_open_prs()
    assert len(all_open) == 2
    assert all_open[0].number == 101
    assert all_open[0].state == "OPEN"
    assert all_open[0].head_ref == "gemini/0003-gitlab"
    assert all_open[0].url == "https://gitlab.com/group/subgroup/repo/-/merge_requests/101"

    filtered = forge.list_open_prs(branch_prefix="gemini/")
    assert len(filtered) == 1
    assert filtered[0].number == 101

    # Fallback to `glab api` when `glab mr list` fails
    recorded_cmds: list[list[str]] = []

    def run_api_fallback(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        if cmd[:3] == ["glab", "mr", "list"]:
            return _proc(1, stderr="glab mr list unsupported flag")
        if cmd[:2] == ["glab", "api"]:
            return _proc(0, stdout=json.dumps(raw_mrs))
        return _proc(1)

    forge_fallback = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=run_api_fallback,
    )
    fallback_mrs = forge_fallback.list_open_prs()
    assert len(fallback_mrs) == 2
    assert recorded_cmds[1] == [
        "glab",
        "api",
        "projects/group%2Fsubgroup%2Frepo/merge_requests?state=opened",
    ]


def test_get_pr_for_branch_and_state_mapping(tmp_path: Path) -> None:
    recorded_cmds: list[list[str]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        return _proc(
            0,
            stdout=json.dumps(
                [
                    {
                        "iid": 42,
                        "title": "feat(0003): gitlab provider",
                        "state": "merged",
                        "source_branch": "gemini/0003-gitlab",
                        "target_branch": "main",
                        "web_url": "https://gitlab.com/group/subgroup/repo/-/merge_requests/42",
                    }
                ]
            ),
        )

    forge = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=mock_run,
    )
    info = forge.get_pr_for_branch("gemini/0003-gitlab")
    assert info is not None
    assert info.number == 42
    assert info.state == "MERGED"
    assert forge.get_pr_state("gemini/0003-gitlab") == ("MERGED", 42)
    assert recorded_cmds[0] == [
        "glab",
        "api",
        "projects/group%2Fsubgroup%2Frepo/merge_requests?source_branch=gemini%2F0003-gitlab",
    ]


def test_create_pr_from_description_file(tmp_path: Path) -> None:
    desc_file = tmp_path / "PR_DESCRIPTION.md"
    desc_file.write_text("## Summary\nImplemented GitLabForge.", encoding="utf-8")

    recorded_cmds: list[list[str]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        if cmd[:3] == ["glab", "mr", "create"]:
            return _proc(
                0,
                stdout=(
                    "Creating merge request for gemini/0003-gitlab into main\n"
                    "https://gitlab.com/group/subgroup/repo/-/merge_requests/88\n"
                ),
            )
        return _proc(0)

    forge = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=mock_run,
    )
    url = forge.create_pr(
        branch="gemini/0003-gitlab",
        base_branch="main",
        title="feat(forge): add GitLabForge (0003)",
        body_file=desc_file,
    )
    assert url == "https://gitlab.com/group/subgroup/repo/-/merge_requests/88"
    assert recorded_cmds[0] == [
        "glab",
        "mr",
        "create",
        "--source-branch",
        "gemini/0003-gitlab",
        "--target-branch",
        "main",
        "--title",
        "feat(forge): add GitLabForge (0003)",
        "--description",
        "## Summary\nImplemented GitLabForge.",
        "--yes",
    ]


def test_fetch_unprocessed_reviews_diff_notes_filtering_and_approvals(tmp_path: Path) -> None:
    discussions_payload = [
        {
            "id": "disc_system",
            "notes": [
                {
                    "id": 1001,
                    "system": True,
                    "author": {"username": "alice"},
                    "body": "added 1 commit",
                    "created_at": "2026-10-27T10:00:00Z",
                }
            ],
        },
        {
            "id": "disc_bot_and_daemon",
            "notes": [
                {
                    "id": 1002,
                    "system": False,
                    "author": {"username": "project_99_bot_a1b2"},
                    "body": "Automated security scan report",
                    "created_at": "2026-10-27T10:01:00Z",
                },
                {
                    "id": 1003,
                    "system": False,
                    "author": {"username": "alice"},
                    "body": "**[Orchestack Daemon]** Addressed 1 review comment(s).",
                    "created_at": "2026-10-27T10:02:00Z",
                },
                {
                    "id": 1004,
                    "system": False,
                    "author": {"username": "alice"},
                    "body": "**[BlueTeamHarness Daemon]** Addressed 1 review comment(s).",
                    "created_at": "2026-10-27T10:03:00Z",
                },
                {
                    "id": 1005,
                    "system": False,
                    "author": {"username": "alice"},
                    "body": "Already processed note",
                    "created_at": "2026-10-27T10:04:00Z",
                },
            ],
        },
        {
            "id": "disc_top_level",
            "notes": [
                {
                    "id": 1006,
                    "type": "DiscussionNote",
                    "system": False,
                    "author": {"username": "bob"},
                    "body": "Please verify nested subgroup URL encoding.",
                    "created_at": "2026-10-27T10:05:00Z",
                }
            ],
        },
        {
            "id": "disc_inline_diff",
            "notes": [
                {
                    "id": 1007,
                    "type": "DiffNote",
                    "system": False,
                    "author": {"username": "carol"},
                    "body": "Use urllib.parse.quote with safe=''.",
                    "position": {
                        "new_path": "src/orchestack/forge/gitlab.py",
                        "old_path": "src/orchestack/forge/gitlab.py",
                        "new_line": 72,
                        "old_line": None,
                    },
                    "created_at": "2026-10-27T10:06:00Z",
                }
            ],
        },
    ]

    approvals_payload = {
        "approved": True,
        "approved_by": [
            {"user": {"id": 50, "username": "dave"}},
            {"user": {"id": 51, "username": "eve"}},
        ],
    }

    recorded_endpoints: list[str] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["glab", "api"]:
            endpoint = cmd[2]
            recorded_endpoints.append(endpoint)
            if endpoint.endswith("/discussions"):
                return _proc(0, stdout=json.dumps(discussions_payload))
            if endpoint.endswith("/approvals"):
                return _proc(0, stdout=json.dumps(approvals_payload))
        return _proc(1)

    forge = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=mock_run,
    )
    items, approved_ids = forge.fetch_unprocessed_reviews(
        pr_number=88,
        processed_ids=["1005", "approval-dave"],
    )

    assert recorded_endpoints == [
        "projects/group%2Fsubgroup%2Frepo/merge_requests/88/discussions",
        "projects/group%2Fsubgroup%2Frepo/merge_requests/88/approvals",
    ]
    assert approved_ids == ["approval-eve"]
    assert [item["id"] for item in items] == ["1006", "1007"]
    assert items[0]["type"] == "comment"
    assert items[0]["author"] == "bob"
    assert items[0]["path"] is None
    assert items[0]["line"] is None

    assert items[1]["type"] == "review_comment"
    assert items[1]["author"] == "carol"
    assert items[1]["path"] == "src/orchestack/forge/gitlab.py"
    assert items[1]["line"] == 72

    review_items = forge.to_review_items(items)
    assert len(review_items) == 2
    assert review_items[0].item_type == "discussion_comment"
    assert review_items[1].item_type == "inline_comment"
    assert review_items[1].path == "src/orchestack/forge/gitlab.py"
    assert review_items[1].line == 72


def test_gitlab_ci_status_and_quota_failure_detection(tmp_path: Path) -> None:
    assert classify_gitlab_ci_status("running") == "pending"
    assert classify_gitlab_ci_status("pending") == "pending"
    assert classify_gitlab_ci_status("created") == "pending"
    assert classify_gitlab_ci_status("success") == "pass"
    assert classify_gitlab_ci_status("skipped") == "pass"
    assert classify_gitlab_ci_status("failed") == "fail"
    assert classify_gitlab_ci_status("canceled") == "fail"

    for reason in GITLAB_QUOTA_FAILURE_REASONS:
        assert is_gitlab_ci_quota_failure({"failure_reason": reason}) is True
    for pattern in GITLAB_CI_QUOTA_PATTERNS:
        assert is_gitlab_ci_quota_failure(f"Job failed: {pattern}") is True
    assert is_gitlab_ci_quota_failure({"failure_reason": "script_failure"}) is False

    # 1. Pipeline PASSED
    forge_pass = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=lambda cmd, **kw: _proc(
            0,
            stdout=json.dumps([{"id": 500, "status": "success", "web_url": "https://gl/p/500"}]),
        ),
    )
    assert forge_pass.get_ci_status(88)[0] == "PASSED"

    # 2. Pipeline PENDING
    forge_pending = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=lambda cmd, **kw: _proc(
            0,
            stdout=json.dumps([{"id": 501, "status": "running", "web_url": "https://gl/p/501"}]),
        ),
    )
    assert forge_pending.get_ci_status(88)[0] == "PENDING"

    # 3. Pipeline FAILED due to normal script_failure
    def run_script_fail(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        endpoint = cmd[2] if len(cmd) > 2 else ""
        if endpoint.endswith("/pipelines"):
            return _proc(
                0,
                stdout=json.dumps([{"id": 502, "status": "failed", "web_url": "https://gl/p/502"}]),
            )
        if endpoint.endswith("/pipelines/502/jobs"):
            return _proc(
                0,
                stdout=json.dumps(
                    [
                        {
                            "id": 901,
                            "name": "pytest",
                            "status": "failed",
                            "failure_reason": "script_failure",
                            "web_url": "https://gl/jobs/901",
                        }
                    ]
                ),
            )
        if endpoint.endswith("/jobs/901/trace"):
            return _proc(0, stdout="E   AssertionError: expected 1 == 2\n")
        return _proc(0)

    forge_fail = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=run_script_fail,
    )
    res_fail = forge_fail.get_ci_check_result(88)
    assert res_fail.status == "FAILED"
    assert "pytest" in res_fail.details
    assert res_fail.failed_urls == ["https://gl/jobs/901"]

    # 4. Pipeline QUOTA_EXCEEDED via job failure_reason (`ci_quota_exceeded`)
    def run_quota_reason(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        endpoint = cmd[2] if len(cmd) > 2 else ""
        if endpoint.endswith("/pipelines"):
            return _proc(
                0,
                stdout=json.dumps([{"id": 503, "status": "failed", "web_url": "https://gl/p/503"}]),
            )
        if endpoint.endswith("/pipelines/503/jobs"):
            return _proc(
                0,
                stdout=json.dumps(
                    [
                        {
                            "id": 902,
                            "name": "pytest",
                            "status": "failed",
                            "failure_reason": "ci_quota_exceeded",
                            "web_url": "https://gl/jobs/902",
                        }
                    ]
                ),
            )
        return _proc(0)

    forge_quota_reason = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=run_quota_reason,
    )
    assert forge_quota_reason.get_ci_status(88)[0] == "QUOTA_EXCEEDED"

    # 5. Pipeline QUOTA_EXCEEDED via `/jobs/:job_id/trace` inspection (`B5`)
    def run_quota_trace(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        endpoint = cmd[2] if len(cmd) > 2 else ""
        if endpoint.endswith("/pipelines"):
            return _proc(
                0,
                stdout=json.dumps([{"id": 504, "status": "failed", "web_url": "https://gl/p/504"}]),
            )
        if endpoint.endswith("/pipelines/504/jobs"):
            return _proc(
                0,
                stdout=json.dumps(
                    [
                        {
                            "id": 903,
                            "name": "pytest",
                            "status": "failed",
                            "failure_reason": "unknown_failure",
                            "web_url": "https://gl/jobs/903",
                        }
                    ]
                ),
            )
        if endpoint.endswith("/jobs/903/trace"):
            return _proc(
                0,
                stdout="ERROR: Job failed: Compute minutes quota exhausted for namespace\n",
            )
        return _proc(0)

    forge_quota_trace = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=run_quota_trace,
    )
    assert forge_quota_trace.get_ci_status(88)[0] == "QUOTA_EXCEEDED"


def test_post_pr_comment_truncation_and_merge_pr(tmp_path: Path) -> None:
    recorded_cmds: list[list[str]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        return _proc(0)

    forge = GitLabForge(
        repo_root=tmp_path,
        project_path="group/subgroup/repo",
        run_cmd_fn=mock_run,
    )

    oversized = "y" * 65000
    assert forge.post_pr_comment(88, oversized) is True
    assert recorded_cmds[-1] == [
        "glab",
        "mr",
        "note",
        "88",
        "-m",
        truncate_pr_comment(oversized),
    ]

    assert forge.merge_pr(88, method="squash", delete_branch=True) is True
    assert recorded_cmds[-1] == [
        "glab",
        "mr",
        "merge",
        "88",
        "--squash",
        "--remove-source-branch",
        "--yes",
    ]

    assert forge.merge_pr(89, method="rebase", delete_branch=False) is True
    assert recorded_cmds[-1] == [
        "glab",
        "mr",
        "merge",
        "89",
        "--rebase",
        "--yes",
    ]

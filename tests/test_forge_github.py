# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for `orchestack.forge` (`ForgeProvider` protocol and `GitHubForge` adapter)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daemon_runner  # type: ignore[import-not-found]  # noqa: E402

from orchestack.forge import (  # noqa: E402
    CI_BILLING_OR_QUOTA_PATTERNS,
    CICheckResult,
    ForgeProvider,
    GitHubForge,
    PullRequestInfo,
    ReviewItem,
    is_ci_billing_or_quota_failure,
    truncate_pr_comment,
)


def _proc(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args or ["gh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_github_forge_implements_forge_provider_protocol(tmp_path: Path) -> None:
    forge = GitHubForge(repo_root=tmp_path)
    assert isinstance(forge, ForgeProvider)


def test_forge_dataclasses() -> None:
    pr = PullRequestInfo(
        number=42,
        title="feat(test): demo",
        url="https://github.com/org/repo/pull/42",
        head_ref="gemini/0002-demo",
        base_ref="main",
        state="OPEN",
    )
    assert pr.number == 42
    assert pr.head_ref == "gemini/0002-demo"
    assert pr.base_ref == "main"
    assert pr.is_draft is False
    assert pr.mergeable == "UNKNOWN"

    ci = CICheckResult(
        status="FAILED",
        details="Failed checks: Check (Python 3.13)",
        failed_urls=["https://github.com/org/repo/actions/runs/1001"],
        raw_checks=[{"name": "Check (Python 3.13)", "state": "FAILURE"}],
    )
    assert ci.status == "FAILED"
    assert ci.failed_urls == ["https://github.com/org/repo/actions/runs/1001"]

    item = ReviewItem(
        id="IC_1",
        author="reviewer",
        body="Please add a docstring",
        created_at="2026-10-27T10:00:00Z",
        item_type="inline_comment",
        state=None,
        path="src/foo.py",
        line=10,
    )
    assert item.id == "IC_1"
    assert item.line == 10
    assert item.item_type == "inline_comment"


def test_get_repo_slug_via_gh_and_git_fallback(tmp_path: Path) -> None:
    # 1. Success via `gh repo view --json nameWithOwner`
    calls: list[list[str]] = []

    def run_gh_ok(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _proc(0, stdout=json.dumps({"nameWithOwner": "owner/orchestack"}))

    forge = GitHubForge(repo_root=tmp_path, run_cmd_fn=run_gh_ok)
    assert forge.get_repo_slug() == "owner/orchestack"
    assert calls[0][:3] == ["gh", "repo", "view"]

    # 2. Fallback to `git remote get-url origin` (SSH URL)
    def run_ssh_fallback(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "gh":
            return _proc(1, stderr="gh error")
        return _proc(0, stdout="git@github.com:acme-sec/orchestack.git\n")

    forge_ssh = GitHubForge(repo_root=tmp_path, run_cmd_fn=run_ssh_fallback)
    assert forge_ssh.get_repo_slug() == "acme-sec/orchestack"

    # 3. Fallback to `git remote get-url origin` (HTTPS URL)
    def run_https_fallback(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "gh":
            return _proc(1, stderr="gh error")
        return _proc(0, stdout="https://github.com/acme-sec/orchestack.git\n")

    forge_https = GitHubForge(repo_root=tmp_path, run_cmd_fn=run_https_fallback)
    assert forge_https.get_repo_slug() == "acme-sec/orchestack"

    # 4. Both fail -> empty string
    forge_fail = GitHubForge(repo_root=tmp_path, run_cmd_fn=lambda cmd, **kw: _proc(1))
    assert forge_fail.get_repo_slug() == ""


def test_list_open_prs_and_branch_prefix_filtering(tmp_path: Path) -> None:
    raw_prs = [
        {
            "number": 10,
            "title": "feat: gemini pr",
            "state": "OPEN",
            "headRefName": "gemini/0002-forge",
            "url": "https://github.com/org/repo/pull/10",
        },
        {
            "number": 11,
            "title": "chore: manual pr",
            "state": "OPEN",
            "headRefName": "feature/manual",
            "url": "https://github.com/org/repo/pull/11",
        },
    ]

    forge = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=lambda cmd, **kw: _proc(0, stdout=json.dumps(raw_prs)),
    )

    all_prs = forge.list_open_prs()
    assert len(all_prs) == 2
    assert all_prs[0].number == 10
    assert all_prs[0].head_ref == "gemini/0002-forge"

    filtered = forge.list_open_prs(branch_prefix="gemini/")
    assert len(filtered) == 1
    assert filtered[0].number == 10

    # Blocking list_open_prs_raw waits for network connectivity when offline then retries
    attempts = 0
    wait_called = False

    def run_with_retry(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _proc(1, stderr="error connecting to api.github.com: network unreachable")
        return _proc(0, stdout=json.dumps(raw_prs))

    def wait_net() -> bool:
        nonlocal wait_called
        wait_called = True
        return True

    forge_retry = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=run_with_retry,
        check_network_fn=lambda: False,
        wait_network_fn=wait_net,
    )
    retried_prs = forge_retry.list_open_prs_raw(non_blocking=False)
    assert wait_called is True
    assert len(retried_prs) == 2


def test_get_pr_for_branch_and_get_pr_state(tmp_path: Path) -> None:
    pr_list_json = json.dumps(
        [
            {
                "number": 55,
                "state": "MERGED",
                "title": "feat(0002): forge provider",
                "url": "https://github.com/org/repo/pull/55",
            }
        ]
    )
    forge = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=lambda cmd, **kw: _proc(0, stdout=pr_list_json),
    )

    pr_info = forge.get_pr_for_branch("gemini/0002-forge")
    assert pr_info is not None
    assert pr_info.number == 55
    assert pr_info.state == "MERGED"
    assert pr_info.head_ref == "gemini/0002-forge"
    assert forge.get_pr_state("gemini/0002-forge") == ("MERGED", 55)

    # Non-existent PR returns None / (None, None)
    forge_empty = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=lambda cmd, **kw: _proc(0, stdout="[]"),
    )
    assert forge_empty.get_pr_for_branch("gemini/none") is None
    assert forge_empty.get_pr_state("gemini/none") == (None, None)


def test_create_pr_from_description_file(tmp_path: Path) -> None:
    desc_file = tmp_path / "PR_DESCRIPTION.md"
    desc_file.write_text("## Summary\nImplemented ForgeProvider.", encoding="utf-8")

    recorded_cmds: list[list[str]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            return _proc(0, stdout="https://github.com/org/repo/pull/77\n")
        return _proc(0)

    forge = GitHubForge(repo_root=tmp_path, run_cmd_fn=mock_run)
    created_url = forge.create_pr(
        branch="gemini/0002-forge",
        base_branch="main",
        title="feat(0002): implement 0002",
        body_file=desc_file,
    )
    assert created_url == "https://github.com/org/repo/pull/77"
    assert recorded_cmds[0] == [
        "gh",
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        "gemini/0002-forge",
        "--title",
        "feat(0002): implement 0002",
        "--body-file",
        str(desc_file),
    ]

    # Failure case returns None
    forge_fail = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=lambda cmd, **kw: _proc(1, stderr="GraphQL error"),
    )
    assert (
        forge_fail.create_pr(
            branch="gemini/0002-forge",
            base_branch="main",
            title="feat(0002): implement 0002",
            body_file=desc_file,
        )
        is None
    )


def test_get_ci_status_and_check_result_states(tmp_path: Path) -> None:
    # 1. PASSED
    checks_pass = [
        {"name": "Check (Python 3.12)", "state": "SUCCESS", "bucket": "pass"},
        {"name": "Check (Python 3.13)", "state": "SUCCESS", "bucket": "pass"},
    ]
    forge_pass = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=lambda cmd, **kw: _proc(0, stdout=json.dumps(checks_pass)),
    )
    status_pass, details_pass = forge_pass.get_ci_status(12)
    assert status_pass == "PASSED"
    assert "2 checks passed" in details_pass
    res_pass = forge_pass.get_ci_check_result(12)
    assert res_pass.status == "PASSED"
    assert res_pass.failed_urls == []

    # 2. PENDING
    checks_pending = [
        {"name": "Check (Python 3.12)", "state": "SUCCESS", "bucket": "pass"},
        {"name": "Check (Python 3.13)", "state": "IN_PROGRESS", "bucket": "pending"},
    ]
    forge_pending = GitHubForge(
        repo_root=tmp_path,
        run_cmd_fn=lambda cmd, **kw: _proc(0, stdout=json.dumps(checks_pending)),
    )
    status_pending, _ = forge_pending.get_ci_status(12)
    assert status_pending == "PENDING"

    # 3. FAILED (normal test failure)
    checks_fail = [
        {
            "name": "Check (Python 3.13)",
            "state": "FAILURE",
            "bucket": "fail",
            "detailsUrl": "https://github.com/org/repo/actions/runs/1001",
        },
    ]

    def run_failed_ci(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "checks"]:
            return _proc(0, stdout=json.dumps(checks_fail))
        if cmd[:3] == ["gh", "run", "view"]:
            return _proc(0, stdout="FAILED tests/test_foo.py::test_bar - AssertionError")
        return _proc(0)

    forge_fail = GitHubForge(repo_root=tmp_path, run_cmd_fn=run_failed_ci)
    status_fail, details_fail = forge_fail.get_ci_status(12)
    assert status_fail == "FAILED"
    assert "Check (Python 3.13)" in details_fail
    res_fail = forge_fail.get_ci_check_result(12)
    assert res_fail.status == "FAILED"
    assert res_fail.failed_urls == ["https://github.com/org/repo/actions/runs/1001"]

    # 4. QUOTA_EXCEEDED (B5 fallback to `gh run list` + `gh run view <run_id>` when no detailsUrl)
    checks_fail_no_url = [
        {"name": "Check (Python 3.13)", "state": "FAILURE", "bucket": "fail"},
    ]

    def run_quota_ci(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "checks"]:
            return _proc(0, stdout=json.dumps(checks_fail_no_url))
        if cmd[:3] == ["gh", "run", "list"]:
            return _proc(
                0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 2002,
                            "status": "completed",
                            "conclusion": "failure",
                            "headSha": "def5678",
                            "workflowName": "CI",
                            "url": "https://github.com/org/repo/actions/runs/2002",
                        }
                    ]
                ),
            )
        if cmd[:3] == ["gh", "run", "view"] and "--log-failed" in cmd:
            return _proc(1, stderr="log not found: job never started")
        if cmd[:3] == ["gh", "run", "view"]:
            return _proc(
                0,
                stdout=(
                    "ANNOTATIONS\n"
                    "X The job was not started because recent account payments have failed "
                    "or your spending limit needs to be increased."
                ),
            )
        return _proc(0)

    forge_quota = GitHubForge(repo_root=tmp_path, run_cmd_fn=run_quota_ci)
    status_quota, _ = forge_quota.get_ci_status(12)
    assert status_quota == "QUOTA_EXCEEDED"
    assert forge_quota.is_ci_quota_failure(12, checks_fail_no_url) is True


def test_is_ci_billing_or_quota_failure_patterns() -> None:
    for pattern in CI_BILLING_OR_QUOTA_PATTERNS:
        assert is_ci_billing_or_quota_failure(f"Error: {pattern} in workflow") is True
    assert is_ci_billing_or_quota_failure({"billing_or_quota_failure": True}) is True
    assert is_ci_billing_or_quota_failure({"failed_logs": "Minutes quota reached"}) is True
    assert is_ci_billing_or_quota_failure(" Normal pytest failure ") is False
    assert is_ci_billing_or_quota_failure(None) is False
    assert is_ci_billing_or_quota_failure("") is False


def test_fetch_unprocessed_reviews_filters_bots_and_daemon_tag_and_tracks_approved(
    tmp_path: Path,
) -> None:
    pr_view_payload = {
        "comments": [
            {
                "id": "IC_processed",
                "author": {"login": "alice"},
                "body": "Already handled",
                "createdAt": "2026-10-27T09:00:00Z",
            },
            {
                "id": "IC_bot",
                "author": {"login": "github-actions[bot]"},
                "body": "CI summary",
                "createdAt": "2026-10-27T09:01:00Z",
            },
            {
                "id": "IC_daemon",
                "author": {"login": "alice"},
                "body": "**[Orchestack Daemon]** Addressed 1 review comment(s).",
                "createdAt": "2026-10-27T09:02:00Z",
            },
            {
                "id": "IC_legacy_daemon",
                "author": {"login": "alice"},
                "body": "**[BlueTeamHarness Daemon]** Addressed 1 review comment(s).",
                "createdAt": "2026-10-27T09:03:00Z",
            },
            {
                "id": "IC_valid",
                "author": {"login": "alice"},
                "body": "Can we also test the fallback?",
                "createdAt": "2026-10-27T09:04:00Z",
            },
        ],
        "reviews": [
            {
                "id": "PRR_approved_new",
                "state": "APPROVED",
                "author": {"login": "bob"},
                "body": "LGTM!",
                "submittedAt": "2026-10-27T09:05:00Z",
                "comments": [],
            },
            {
                "id": "PRR_changes",
                "state": "CHANGES_REQUESTED",
                "author": {"login": "carol"},
                "body": "Please rename this helper.",
                "submittedAt": "2026-10-27T09:06:00Z",
                "comments": [
                    {
                        "id": "PRRC_inline_1",
                        "author": {"login": "carol"},
                        "body": "Use snake_case here.",
                        "path": "src/orchestack/forge/github.py",
                        "line": 42,
                        "diffHunk": "@@ -40,3 +40,3 @@",
                        "createdAt": "2026-10-27T09:06:30Z",
                    }
                ],
            },
        ],
    }

    diff_api_payload = [
        {
            "id": "PRRC_inline_1",  # duplicate of review inline comment above
            "user": {"login": "carol"},
            "body": "Use snake_case here.",
            "path": "src/orchestack/forge/github.py",
            "line": 42,
            "diff_hunk": "@@ -40,3 +40,3 @@",
            "created_at": "2026-10-27T09:06:30Z",
        },
        {
            "id": 9001,
            "user": {"login": "dave"},
            "body": "Check return type.",
            "path": "src/orchestack/forge/base.py",
            "original_line": 18,
            "diff_hunk": "@@ -15,4 +15,4 @@",
            "created_at": "2026-10-27T09:07:00Z",
        },
        {
            "id": 9002,
            "user": {"login": "codecov"},
            "body": "Coverage report",
            "path": "src/orchestack/forge/base.py",
            "line": 1,
        },
    ]

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _proc(0, stdout=json.dumps(pr_view_payload))
        if cmd[:2] == ["gh", "api"]:
            return _proc(0, stdout=json.dumps(diff_api_payload))
        return _proc(0)

    forge = GitHubForge(repo_root=tmp_path, run_cmd_fn=mock_run)
    items, approved_ids = forge.fetch_unprocessed_reviews(
        pr_number=42,
        processed_ids=["IC_processed"],
    )

    assert approved_ids == ["PRR_approved_new"]
    assert [i["id"] for i in items] == [
        "IC_valid",
        "PRR_changes",
        "PRRC_inline_1",
        "9001",
    ]
    assert items[0]["type"] == "comment"
    assert items[1]["type"] == "review_summary"
    assert items[1]["body"] == "[CHANGES_REQUESTED] Please rename this helper."
    assert items[2]["type"] == "review_comment"
    assert items[2]["path"] == "src/orchestack/forge/github.py"
    assert items[2]["line"] == 42
    assert items[3]["type"] == "review_comment"
    assert items[3]["line"] == 18


def test_truncate_pr_comment_and_post_pr_comment(tmp_path: Path) -> None:
    short_msg = "All checks passed."
    assert truncate_pr_comment(short_msg) == short_msg

    oversized_msg = "x" * 65000
    truncated = truncate_pr_comment(oversized_msg)
    assert len(truncated) == 59900 + len("\n\n... (truncated)")
    assert truncated.endswith("\n\n... (truncated)")

    posted_bodies: list[str] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "comment"]:
            body_idx = cmd.index("--body") + 1
            posted_bodies.append(cmd[body_idx])
            return _proc(0)
        return _proc(1)

    forge = GitHubForge(repo_root=tmp_path, run_cmd_fn=mock_run)
    assert forge.post_pr_comment(42, oversized_msg) is True
    assert len(posted_bodies) == 1
    assert posted_bodies[0] == truncated


def test_merge_pr_methods_and_branch_deletion(tmp_path: Path) -> None:
    recorded_cmds: list[list[str]] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        return _proc(0)

    forge = GitHubForge(repo_root=tmp_path, run_cmd_fn=mock_run)
    assert forge.merge_pr(42, method="squash", delete_branch=True) is True
    assert recorded_cmds[-1] == ["gh", "pr", "merge", "42", "--squash", "--delete-branch"]

    assert forge.merge_pr(43, method="rebase", delete_branch=False) is True
    assert recorded_cmds[-1] == ["gh", "pr", "merge", "43", "--rebase"]

    assert forge.merge_pr(44, method="merge", delete_branch=True) is True
    assert recorded_cmds[-1] == ["gh", "pr", "merge", "44", "--merge", "--delete-branch"]


def test_mark_pr_ci_green_updates_dod_checkbox(tmp_path: Path) -> None:
    edited_bodies: list[str | None] = []

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _proc(
                0,
                stdout=json.dumps({"body": "## Definition of Done\n- [ ] CI checks pass\n"}),
            )
        if cmd[:3] == ["gh", "pr", "edit"]:
            edited_bodies.append(kwargs.get("input"))
            return _proc(0)
        return _proc(0)

    forge = GitHubForge(repo_root=tmp_path, run_cmd_fn=mock_run)
    assert forge.mark_pr_ci_green(99) is True
    assert len(edited_bodies) == 1
    assert edited_bodies[0] is not None
    assert "- [x] CI checks pass" in edited_bodies[0]


def test_daemon_runner_fetch_unhandled_pr_feedback_delegates_to_github_forge(
    tmp_path: Path,
) -> None:
    reviews_file = tmp_path / "pr_reviews.json"
    pr_view_payload = {
        "comments": [
            {
                "id": "IC_100",
                "author": {"login": "reviewer"},
                "body": "Please check edge case.",
                "createdAt": "2026-10-27T10:00:00Z",
            }
        ],
        "reviews": [
            {
                "id": "PRR_200",
                "state": "APPROVED",
                "author": {"login": "approver"},
                "body": "Looks great",
                "submittedAt": "2026-10-27T10:01:00Z",
                "comments": [],
            }
        ],
    }

    def mock_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _proc(0, stdout=json.dumps(pr_view_payload))
        if cmd[:2] == ["gh", "api"]:
            return _proc(0, stdout="[]")
        return _proc(0)

    with (
        patch("daemon_runner.PR_REVIEWS_FILE", reviews_file),
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.run_cmd", side_effect=mock_run),
    ):
        feedback = daemon_runner.fetch_unhandled_pr_feedback("gh", 77)
        assert len(feedback) == 1
        assert feedback[0]["id"] == "IC_100"
        assert feedback[0]["type"] == "comment"
        # Approved review ID was persisted as processed so it doesn't re-trigger
        processed = daemon_runner.load_processed_comment_ids(77)
        assert "PRR_200" in processed

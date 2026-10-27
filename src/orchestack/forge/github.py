# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""GitHub forge provider implementation using the `gh` CLI and `gh api`."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestack.forge.base import CICheckResult, PullRequestInfo, ReviewItem

logger = logging.getLogger("orchestack.forge.github")

MAX_PR_COMMENT_LENGTH = 60000
TRUNCATED_PR_COMMENT_PREFIX_LEN = 59900
MAX_FAILED_LOG_CHARS = 12000

DEFAULT_DAEMON_TAG = "🤖 **[BlueTeamHarness Daemon]**"
ORCHESTACK_DAEMON_TAG = "🤖 **[Orchestack Daemon]**"
BOT_LOGINS: frozenset[str] = frozenset({"github-actions", "codecov"})

CI_BILLING_OR_QUOTA_PATTERNS: tuple[str, ...] = (
    "recent account payments have failed",
    "spending limit needs to be increased",
    "spending limit",
    "billing & plans",
    "the job was not started because",
    "minutes quota",
    "storage quota",
    "artifact quota",
    "exceeded your quota",
    "quota has been reached",
    "quota has been exceeded",
    "out of included minutes",
    "account is locked due to billing",
)

_RUN_ID_URL_RE = re.compile(r"/actions/runs/(\d+)")
_GITHUB_REMOTE_SLUG_RE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$")
_DOD_CI_CHECKBOX_RE = re.compile(
    r"^(\s*[-*]?\s*)\[\s*\](\s*CI\b[^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

RunCmdCallable = Callable[..., subprocess.CompletedProcess[str]]


def is_ci_billing_or_quota_failure(run_info_or_logs: dict[str, Any] | str | None) -> bool:
    """Return True if a CI run failed due to GitHub Actions billing, spending limit, or quota."""
    if not run_info_or_logs:
        return False
    if isinstance(run_info_or_logs, dict):
        if run_info_or_logs.get("billing_or_quota_failure") is True:
            return True
        parts = [
            str(run_info_or_logs.get(k) or "")
            for k in ("failed_logs", "description", "details", "output", "summary", "title")
        ]
        text = "\n".join(p for p in parts if p)
    else:
        text = str(run_info_or_logs)

    lowered = text.lower()
    return any(pattern in lowered for pattern in CI_BILLING_OR_QUOTA_PATTERNS)


def truncate_pr_comment(
    body: str,
    max_length: int = MAX_PR_COMMENT_LENGTH,
    keep_length: int = TRUNCATED_PR_COMMENT_PREFIX_LEN,
) -> str:
    """Truncate a PR comment body if it exceeds GitHub's maximum length threshold."""
    if len(body) > max_length:
        return body[:keep_length] + "\n\n... (truncated)"
    return body


def _default_run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
    input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Default subprocess wrapper when no custom `run_cmd_fn` is injected."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=True,
        input=input,
        timeout=timeout,
    )


def _is_bot_author(author: str) -> bool:
    """Return True if `author` is an automated bot account that should be ignored."""
    return author.endswith("[bot]") or author in BOT_LOGINS


class GitHubForge:
    """GitHub forge provider backed by the `gh` CLI and `gh api`."""

    def __init__(
        self,
        repo_root: Path | None = None,
        gh_bin: str = "gh",
        daemon_tag: str = DEFAULT_DAEMON_TAG,
        run_cmd_fn: RunCmdCallable | None = None,
        check_network_fn: Callable[[], bool] | None = None,
        wait_network_fn: Callable[[], bool] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.gh_bin = gh_bin
        self.daemon_tag = daemon_tag
        self._run_cmd_fn: RunCmdCallable = run_cmd_fn or _default_run_cmd
        self._check_network_fn = check_network_fn
        self._wait_network_fn = wait_network_fn

    def _run_cmd(
        self,
        cmd: list[str],
        *,
        check: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if input is not None:
            return self._run_cmd_fn(cmd, input=input, check=check)
        return self._run_cmd_fn(cmd, check=check)

    def _is_daemon_body(self, body: str) -> bool:
        return (
            self.daemon_tag in body
            or DEFAULT_DAEMON_TAG in body
            or ORCHESTACK_DAEMON_TAG in body
            or "[BlueTeamHarness Daemon]" in body
            or "[Orchestack Daemon]" in body
        )

    def get_repo_slug(self) -> str:
        """Return the `owner/repo` slug for the repository."""
        res = self._run_cmd(
            [self.gh_bin, "repo", "view", "--json", "nameWithOwner"],
            check=False,
        )
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            raw = res.stdout.strip()
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("nameWithOwner"):
                    return str(data["nameWithOwner"]).strip()
            except Exception:
                if "/" in raw and not raw.startswith("{"):
                    return raw

        remote_res = self._run_cmd(["git", "remote", "get-url", "origin"], check=False)
        if remote_res.returncode == 0 and remote_res.stdout:
            m = _GITHUB_REMOTE_SLUG_RE.search(remote_res.stdout.strip())
            if m:
                return m.group(1)
        return ""

    def list_open_prs_raw(self, non_blocking: bool = False) -> list[dict[str, Any]]:
        """Return raw open pull request dicts from `gh pr list`."""
        while True:
            try:
                res = self._run_cmd(
                    [
                        self.gh_bin,
                        "pr",
                        "list",
                        "--state",
                        "open",
                        "--json",
                        "number,title,headRefName,url",
                    ],
                    check=False,
                )
                if res.returncode != 0:
                    err_text = (res.stderr or "").lower()
                    network_down = (
                        "network" in err_text
                        or "connection" in err_text
                        or (self._check_network_fn is not None and not self._check_network_fn())
                    )
                    if network_down:
                        if non_blocking or self._wait_network_fn is None:
                            logger.warning(
                                "Network appears offline; skipping open PRs check (non-blocking)."
                            )
                            return []
                        self._wait_network_fn()
                        continue
                    logger.warning("Failed to check open PRs: %s", res.stderr)
                    return []
                data: list[dict[str, Any]] = json.loads(res.stdout)
                return data
            except Exception as e:
                logger.warning("Error checking open PRs: %s", e)
                return []

    def list_open_prs(self, branch_prefix: str | None = None) -> list[PullRequestInfo]:
        """List open pull requests as normalized `PullRequestInfo` objects."""
        raw_prs = self.list_open_prs_raw(non_blocking=True)
        results: list[PullRequestInfo] = []
        for item in raw_prs:
            head_ref = str(item.get("headRefName") or item.get("head_ref") or "")
            if branch_prefix is not None and not head_ref.startswith(branch_prefix):
                continue
            results.append(
                PullRequestInfo(
                    number=int(item["number"]),
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    head_ref=head_ref,
                    base_ref=str(item.get("baseRefName") or item.get("base_ref") or "main"),
                    state=str(item.get("state") or "OPEN").upper(),
                    is_draft=bool(item.get("isDraft", False)),
                    mergeable=str(item.get("mergeable") or "UNKNOWN"),
                )
            )
        return results

    def get_branch_pr_status(self, branch: str) -> dict[str, Any] | None:
        """Return raw PR metadata dict for `branch` across all states (OPEN, MERGED, CLOSED)."""
        try:
            res = self._run_cmd(
                [
                    self.gh_bin,
                    "pr",
                    "list",
                    "--head",
                    branch,
                    "--state",
                    "all",
                    "--json",
                    "number,state,title,url",
                ],
                check=False,
            )
            if res.returncode == 0 and res.stdout and res.stdout.strip():
                prs: list[dict[str, Any]] = json.loads(res.stdout)
                if prs:
                    for pr in prs:
                        if pr.get("state") in ("OPEN", "MERGED"):
                            return pr
                    return prs[0]
        except Exception as e:
            logger.debug("Error checking PR status for branch %s: %s", branch, e)
        return None

    def get_pr_for_branch(self, branch: str) -> PullRequestInfo | None:
        """Return normalized `PullRequestInfo` for `branch`, or None if no PR exists."""
        pr = self.get_branch_pr_status(branch)
        if not pr:
            return None
        return PullRequestInfo(
            number=int(pr["number"]),
            title=str(pr.get("title") or ""),
            url=str(pr.get("url") or ""),
            head_ref=str(pr.get("headRefName") or branch),
            base_ref=str(pr.get("baseRefName") or "main"),
            state=str(pr.get("state") or "UNKNOWN").upper(),
            is_draft=bool(pr.get("isDraft", False)),
            mergeable=str(pr.get("mergeable") or "UNKNOWN"),
        )

    def get_pr_state(self, branch: str) -> tuple[str | None, int | None]:
        """Return `(state, pr_number)` for `branch`, or `(None, None)` if no PR exists."""
        pr = self.get_branch_pr_status(branch)
        if not pr:
            return (None, None)
        state_raw = pr.get("state")
        state = str(state_raw).upper() if state_raw else None
        num_raw = pr.get("number")
        number = int(num_raw) if num_raw is not None else None
        return (state, number)

    def create_pr_with_body(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> subprocess.CompletedProcess[str]:
        """Create a pull request passing `body` on stdin via `--body-file -`."""
        return self._run_cmd(
            [
                self.gh_bin,
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input=body,
            check=False,
        )

    def create_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body_file: Path,
    ) -> str | None:
        """Create a pull request from `body_file` and return the created PR URL or None."""
        res = self._run_cmd(
            [
                self.gh_bin,
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                str(body_file),
            ],
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            url = res.stdout.strip()
            return url or None
        return None

    def get_pr_checks(self, pr_number: int | str) -> list[dict[str, Any]]:
        """Fetch raw GitHub check runs for `pr_number`."""
        try:
            res = self._run_cmd(
                [
                    self.gh_bin,
                    "pr",
                    "checks",
                    str(pr_number),
                    "--json",
                    "bucket,name,state,workflow",
                ],
                check=False,
            )
            if res.returncode == 0 and res.stdout and res.stdout.strip():
                checks: list[dict[str, Any]] = json.loads(res.stdout)
                return checks
        except Exception as e:
            logger.warning("Error fetching checks for PR #%s: %s", pr_number, e)
        return []

    @staticmethod
    def _classify_check(check: dict[str, Any]) -> str:
        """Classify a raw check dict into `'fail'`, `'pending'`, or `'pass'`."""
        bucket = str(check.get("bucket") or "").lower()
        if bucket in ("fail", "pending", "pass"):
            return bucket

        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()

        if conclusion in (
            "FAILURE",
            "FAILED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "CANCELLED",
            "STARTUP_FAILURE",
            "ERROR",
        ) or state in (
            "FAILURE",
            "FAILED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "CANCELLED",
            "STARTUP_FAILURE",
            "ERROR",
        ):
            return "fail"

        if conclusion in ("SUCCESS", "PASSED", "NEUTRAL", "SKIPPED") or state in (
            "SUCCESS",
            "PASSED",
            "NEUTRAL",
            "SKIPPED",
        ):
            return "pass"

        return "pending"

    def _fetch_run_logs_with_fallback(self, run_id: str | int) -> str:
        """Fetch failed logs for `run_id`, falling back to `gh run view <run_id>` per B5."""
        log_res = self._run_cmd(
            [self.gh_bin, "run", "view", str(run_id), "--log-failed"],
            input="",
            check=False,
        )
        failed_logs = log_res.stdout or log_res.stderr or ""
        if (
            log_res.returncode != 0
            or not failed_logs.strip()
            or "log not found" in failed_logs.lower()
        ):
            view_res = self._run_cmd(
                [self.gh_bin, "run", "view", str(run_id)],
                input="",
                check=False,
            )
            view_output = view_res.stdout or view_res.stderr or ""
            if view_output.strip():
                failed_logs = (
                    f"{failed_logs.strip()}\n\n{view_output.strip()}".strip()
                    if failed_logs.strip()
                    else view_output
                )
        if len(failed_logs) > MAX_FAILED_LOG_CHARS:
            failed_logs = "... [truncated output] ...\n" + failed_logs[-MAX_FAILED_LOG_CHARS:]
        return failed_logs

    def fetch_latest_failed_ci_run(self, branch_name: str) -> dict[str, Any] | None:
        """Check if the latest workflow run for `branch_name` failed and fetch its logs."""
        res = self._run_cmd(
            [
                self.gh_bin,
                "run",
                "list",
                "--branch",
                branch_name,
                "--limit",
                "1",
                "--json",
                "databaseId,status,conclusion,headSha,workflowName,url",
            ],
            check=False,
        )
        if res.returncode != 0 or not res.stdout or not res.stdout.strip():
            return None
        try:
            runs = json.loads(res.stdout)
            if not runs:
                return None
            run: dict[str, Any] = runs[0]
            if run.get("status") == "completed" and run.get("conclusion") in (
                "failure",
                "timed_out",
                "action_required",
            ):
                run_id = run.get("databaseId")
                failed_logs = self._fetch_run_logs_with_fallback(str(run_id))
                run["failed_logs"] = failed_logs
                run["billing_or_quota_failure"] = is_ci_billing_or_quota_failure(failed_logs)
                return run
        except Exception as e:
            logger.warning("Error fetching CI run details for branch '%s': %s", branch_name, e)
        return None

    def is_ci_quota_failure(self, pr_number: int, checks: list[dict[str, Any]]) -> bool:
        """Return True if CI checks failed due to GitHub Actions billing/quota exhaustion (B5).

        Includes fallback inspection via `gh run view <run_id>` when a check fails with
        `conclusion == "FAILURE"` (or `state == "FAILURE"` / `bucket == "fail"`) and no
        `detailsUrl` or log output is immediately available on the check item.
        """
        if not checks:
            return False

        failed_checks = [c for c in checks if self._classify_check(c) == "fail"]
        if not failed_checks:
            return False

        # 1. Direct inspection of check metadata/annotations if already present
        for check in failed_checks:
            if is_ci_billing_or_quota_failure(check):
                return True

        # 2. Check if any failed check carries a run ID or run URL
        inspected_run_ids: set[str] = set()
        needs_run_list_lookup = False

        for check in failed_checks:
            run_id: str | None = None
            raw_id = check.get("databaseId") or check.get("run_id")
            if raw_id is not None:
                run_id = str(raw_id)
            else:
                url = str(
                    check.get("detailsUrl") or check.get("url") or check.get("link") or ""
                ).strip()
                if url:
                    m = _RUN_ID_URL_RE.search(url)
                    if m:
                        run_id = m.group(1)

            if run_id:
                if run_id not in inspected_run_ids:
                    inspected_run_ids.add(run_id)
                    logs = self._fetch_run_logs_with_fallback(run_id)
                    if is_ci_billing_or_quota_failure(logs):
                        return True
            else:
                needs_run_list_lookup = True

        # 3. Fallback when a failed check has no detailsUrl or embedded run ID (B5)
        if needs_run_list_lookup:
            res = self._run_cmd(
                [
                    self.gh_bin,
                    "run",
                    "list",
                    "--limit",
                    "5",
                    "--json",
                    "databaseId,status,conclusion,headSha,workflowName,url",
                ],
                check=False,
            )
            if res.returncode == 0 and res.stdout and res.stdout.strip():
                raw_out = res.stdout.strip()
                try:
                    runs = json.loads(raw_out)
                    if isinstance(runs, list):
                        for run in runs:
                            if not isinstance(run, dict):
                                continue
                            conclusion = str(run.get("conclusion") or "").lower()
                            if conclusion in ("failure", "timed_out", "action_required"):
                                rid = str(run.get("databaseId") or run.get("id") or "")
                                if rid and rid not in inspected_run_ids:
                                    inspected_run_ids.add(rid)
                                    logs = self._fetch_run_logs_with_fallback(rid)
                                    if is_ci_billing_or_quota_failure(logs):
                                        return True
                except json.JSONDecodeError:
                    if is_ci_billing_or_quota_failure(raw_out):
                        return True

        return False

    def get_ci_check_result(self, pr_number: int) -> CICheckResult:
        """Evaluate CI checks for `pr_number` and return a normalized `CICheckResult`."""
        checks = self.get_pr_checks(pr_number)
        if not checks:
            return CICheckResult(
                status="PENDING",
                details="No CI checks reported yet",
                failed_urls=[],
                raw_checks=[],
            )

        pending: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        passed: list[dict[str, Any]] = []
        failed_urls: list[str] = []

        for c in checks:
            bucket = self._classify_check(c)
            if bucket == "fail":
                failed.append(c)
                url = str(c.get("detailsUrl") or c.get("url") or c.get("link") or "").strip()
                if url:
                    failed_urls.append(url)
            elif bucket == "pending":
                pending.append(c)
            else:
                passed.append(c)

        if failed and not pending:
            if self.is_ci_quota_failure(pr_number, checks):
                return CICheckResult(
                    status="QUOTA_EXCEEDED",
                    details="CI failed due to GitHub Actions billing/spending limit or quota",
                    failed_urls=failed_urls,
                    raw_checks=checks,
                )
            failed_names = ", ".join(str(c.get("name") or "unknown") for c in failed)
            return CICheckResult(
                status="FAILED",
                details=f"Failed checks: {failed_names}",
                failed_urls=failed_urls,
                raw_checks=checks,
            )

        if pending:
            return CICheckResult(
                status="PENDING",
                details=f"{len(pending)} pending, {len(passed)} passed",
                failed_urls=failed_urls,
                raw_checks=checks,
            )

        if failed:
            failed_names = ", ".join(str(c.get("name") or "unknown") for c in failed)
            return CICheckResult(
                status="FAILED",
                details=f"Failed checks: {failed_names}",
                failed_urls=failed_urls,
                raw_checks=checks,
            )

        return CICheckResult(
            status="PASSED",
            details=f"{len(passed)} checks passed",
            failed_urls=[],
            raw_checks=checks,
        )

    def get_ci_status(self, pr_number: int) -> tuple[str, str]:
        """Return `(status, details)` for CI checks on `pr_number`."""
        result = self.get_ci_check_result(pr_number)
        return (result.status, result.details)

    def mark_pr_ci_green(self, pr_number: int) -> bool:
        """Inspect PR description and ensure CI checkbox in Definition of Done is marked `[x]`."""
        res = self._run_cmd(
            [self.gh_bin, "pr", "view", str(pr_number), "--json", "body"],
            check=False,
        )
        if res.returncode != 0 or not res.stdout:
            return False
        try:
            data = json.loads(res.stdout)
            body = str(data.get("body") or "")
        except Exception as e:
            logger.warning("Could not parse PR #%s body JSON: %s", pr_number, e)
            return False

        if not body or not _DOD_CI_CHECKBOX_RE.search(body):
            return False

        updated_body = _DOD_CI_CHECKBOX_RE.sub(r"\1[x]\2", body)
        if updated_body == body:
            return False

        logger.info("Marking CI as green in Definition of Done for PR #%s...", pr_number)
        edit_res = self._run_cmd(
            [self.gh_bin, "pr", "edit", str(pr_number), "--body-file", "-"],
            input=updated_body,
            check=False,
        )
        if edit_res.returncode == 0:
            logger.info(
                "Updated Definition of Done in PR #%s: marked CI check as passed [x].",
                pr_number,
            )
            return True
        logger.warning("Failed to update PR #%s description: %s", pr_number, edit_res.stderr)
        return False

    def fetch_unprocessed_reviews(
        self,
        pr_number: int,
        processed_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch unprocessed reviews, inline diff comments, and issue comments on `pr_number`.

        Returns:
            `(unhandled_items, newly_seen_approved_ids)` where `newly_seen_approved_ids`
            contains review IDs with `state == "APPROVED"` that should be marked as seen
            without triggering a feedback loop (`B4`).
        """
        processed_set = {str(cid) for cid in processed_ids}
        unhandled: list[dict[str, Any]] = []
        newly_seen_approved_ids: list[str] = []

        # 1. Fetch top-level comments and reviews via gh pr view
        view_res = self._run_cmd(
            [
                self.gh_bin,
                "pr",
                "view",
                str(pr_number),
                "--json",
                "comments,reviews,latestReviews",
            ],
            check=False,
        )
        if view_res.returncode == 0 and view_res.stdout and view_res.stdout.strip():
            try:
                pr_data = json.loads(view_res.stdout)

                # Check conversation comments
                for c in pr_data.get("comments", []):
                    cid = str(c.get("id") or "")
                    if not cid or cid in processed_set:
                        continue
                    author = str((c.get("author") or {}).get("login") or "")
                    if _is_bot_author(author):
                        continue
                    body = str(c.get("body") or "").strip()
                    if not body or self._is_daemon_body(body):
                        continue
                    unhandled.append(
                        {
                            "id": cid,
                            "type": "comment",
                            "author": author,
                            "body": body,
                            "path": None,
                            "line": None,
                            "diff_hunk": None,
                            "created_at": c.get("createdAt", ""),
                        }
                    )

                # Check reviews and review bodies
                for r in pr_data.get("reviews", []):
                    rid = str(r.get("id") or "")
                    state = str(r.get("state") or "").upper()
                    author = str((r.get("author") or {}).get("login") or "")
                    body = str(r.get("body") or "").strip()

                    if state == "APPROVED":
                        # Mark approved review as seen so it doesn't trigger feedback loop (B4)
                        if rid and rid not in processed_set:
                            newly_seen_approved_ids.append(rid)
                        continue

                    if (
                        rid
                        and rid not in processed_set
                        and body
                        and not self._is_daemon_body(body)
                        and not _is_bot_author(author)
                    ):
                        unhandled.append(
                            {
                                "id": rid,
                                "type": "review_summary",
                                "author": author,
                                "body": f"[{state}] {body}",
                                "path": None,
                                "line": None,
                                "diff_hunk": None,
                                "created_at": r.get("submittedAt", ""),
                            }
                        )

                    # Check inline comments attached to review in gh pr view
                    for rc in r.get("comments", []):
                        rcid = str(rc.get("id") or "")
                        if not rcid or rcid in processed_set:
                            continue
                        rc_body = str(rc.get("body") or "").strip()
                        if not rc_body or self._is_daemon_body(rc_body):
                            continue
                        rc_author = str((rc.get("author") or {}).get("login") or "") or author
                        if _is_bot_author(rc_author):
                            continue
                        unhandled.append(
                            {
                                "id": rcid,
                                "type": "review_comment",
                                "author": rc_author,
                                "body": rc_body,
                                "path": rc.get("path"),
                                "line": rc.get("line"),
                                "diff_hunk": rc.get("diffHunk"),
                                "created_at": rc.get("createdAt", ""),
                            }
                        )
            except Exception as e:
                logger.warning("Error parsing PR #%s feedback: %s", pr_number, e)

        # 2. Check inline diff comments via gh api (captures any line comments not in gh pr view)
        api_res = self._run_cmd(
            [self.gh_bin, "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments"],
            check=False,
        )
        if api_res.returncode == 0 and api_res.stdout and api_res.stdout.strip():
            try:
                diff_comments = json.loads(api_res.stdout)
                if isinstance(diff_comments, list):
                    for dc in diff_comments:
                        dcid = str(dc.get("id") or "")
                        if (
                            not dcid
                            or dcid in processed_set
                            or any(item["id"] == dcid for item in unhandled)
                        ):
                            continue
                        author = str((dc.get("user") or {}).get("login") or "")
                        if _is_bot_author(author):
                            continue
                        body = str(dc.get("body") or "").strip()
                        if not body or self._is_daemon_body(body):
                            continue
                        unhandled.append(
                            {
                                "id": dcid,
                                "type": "review_comment",
                                "author": author,
                                "body": body,
                                "path": dc.get("path"),
                                "line": dc.get("line") or dc.get("original_line"),
                                "diff_hunk": dc.get("diff_hunk"),
                                "created_at": dc.get("created_at", ""),
                            }
                        )
            except Exception as e:
                logger.debug("Could not fetch inline review comments via gh api: %s", e)

        return unhandled, newly_seen_approved_ids

    @staticmethod
    def to_review_items(raw_items: list[dict[str, Any]]) -> list[ReviewItem]:
        """Convert raw review feedback dicts into normalized `ReviewItem` dataclasses."""
        items: list[ReviewItem] = []
        for raw in raw_items:
            raw_type = str(raw.get("type") or "comment")
            if raw_type == "review_summary":
                item_type: Any = "review"
            elif raw_type == "review_comment":
                item_type = "inline_comment"
            else:
                item_type = "discussion_comment"
            line_val = raw.get("line")
            items.append(
                ReviewItem(
                    id=str(raw.get("id") or ""),
                    author=str(raw.get("author") or ""),
                    body=str(raw.get("body") or ""),
                    created_at=str(raw.get("created_at") or ""),
                    item_type=item_type,
                    state=str(raw["state"]) if raw.get("state") is not None else None,
                    path=str(raw["path"]) if raw.get("path") is not None else None,
                    line=int(line_val) if isinstance(line_val, int) else None,
                )
            )
        return items

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        """Post a comment to `pr_number`, truncating bodies over 60,000 chars (`B4`)."""
        safe_body = truncate_pr_comment(body)
        res = self._run_cmd(
            [self.gh_bin, "pr", "comment", str(pr_number), "--body", safe_body],
            check=False,
        )
        return res.returncode == 0

    def merge_pr(
        self,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> bool:
        """Merge `pr_number` via `gh pr merge` using `method` (`squash`, `merge`, `rebase`)."""
        normalized_method = method.strip().lower()
        if normalized_method not in ("squash", "merge", "rebase"):
            normalized_method = "squash"
        cmd = [self.gh_bin, "pr", "merge", str(pr_number), f"--{normalized_method}"]
        if delete_branch:
            cmd.append("--delete-branch")
        res = self._run_cmd(cmd, check=False)
        return res.returncode == 0

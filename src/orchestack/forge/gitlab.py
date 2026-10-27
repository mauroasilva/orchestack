# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""GitLab forge provider implementation using the `glab` CLI and GitLab REST API v4."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestack.forge.base import CICheckResult, PullRequestInfo, ReviewItem
from orchestack.forge.github import (
    CI_BILLING_OR_QUOTA_PATTERNS,
    DEFAULT_DAEMON_TAG,
    MAX_FAILED_LOG_CHARS,
    ORCHESTACK_DAEMON_TAG,
    RunCmdCallable,
    _default_run_cmd,
    truncate_pr_comment,
)

logger = logging.getLogger("orchestack.forge.gitlab")

GITLAB_BOT_USERNAMES: frozenset[str] = frozenset(
    {
        "github-actions",
        "codecov",
        "gitlab-bot",
        "support-bot",
        "alert-bot",
        "security-policy-bot",
        "ghost",
    }
)
_GITLAB_TOKEN_BOT_RE = re.compile(r"^(?:project|group)_\d+_bot[a-z0-9_]*$", re.IGNORECASE)
_URL_LINE_RE = re.compile(r"(https?://\S+)")

GITLAB_QUOTA_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "ci_quota_exceeded",
        "runner_system_failure",
        "shared_runners_minutes_limit_exceeded",
    }
)

GITLAB_CI_QUOTA_PATTERNS: tuple[str, ...] = (
    "ci_quota_exceeded",
    "shared_runners_minutes_limit_exceeded",
    "runner_system_failure",
    "no more ci minutes",
    "out of ci minutes",
    "pipeline quota",
    "compute minutes quota",
    "ci/cd minutes",
    "exceeded the quota",
    "shared runner",
    *CI_BILLING_OR_QUOTA_PATTERNS,
)


def encode_gitlab_project_path(project_path: str) -> str:
    """URL-encode a GitLab project path (`group/subgroup/repo` -> `group%2Fsubgroup%2Frepo`)."""
    cleaned = project_path.strip().strip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4].strip("/")
    return urllib.parse.quote(cleaned, safe="")


def normalize_gitlab_mr_state(state: str | None) -> str:
    """Normalize GitLab MR states (`opened`, `merged`, `closed`) to uppercase standard states."""
    if not state:
        return "UNKNOWN"
    lowered = state.strip().lower()
    if lowered in ("opened", "open"):
        return "OPEN"
    if lowered == "merged":
        return "MERGED"
    if lowered == "closed":
        return "CLOSED"
    if lowered == "locked":
        return "LOCKED"
    return state.strip().upper()


def classify_gitlab_ci_status(status: str | None) -> str:
    """Classify a GitLab pipeline or job status into `'pending'`, `'pass'`, or `'fail'`."""
    if not status:
        return "pending"
    lowered = status.strip().lower()
    if lowered in (
        "running",
        "pending",
        "created",
        "preparing",
        "waiting_for_resource",
        "scheduled",
        "manual",
    ):
        return "pending"
    if lowered in ("success", "skipped", "passed", "neutral"):
        return "pass"
    if lowered in ("failed", "failure", "canceled", "cancelled", "timed_out", "error"):
        return "fail"
    return "pending"


def is_gitlab_ci_quota_failure(item_or_logs: dict[str, Any] | str | None) -> bool:
    """Return True if a GitLab job/pipeline dict or trace log indicates CI quota exhaustion."""
    if not item_or_logs:
        return False
    if isinstance(item_or_logs, dict):
        if item_or_logs.get("billing_or_quota_failure") is True:
            return True
        failure_reason = str(item_or_logs.get("failure_reason") or "").strip().lower()
        if failure_reason in GITLAB_QUOTA_FAILURE_REASONS:
            return True
        parts = [
            failure_reason,
            *(
                str(item_or_logs.get(k) or "")
                for k in (
                    "trace",
                    "failed_logs",
                    "description",
                    "details",
                    "output",
                    "summary",
                    "title",
                )
            ),
        ]
        text = "\n".join(p for p in parts if p)
    else:
        text = str(item_or_logs)

    lowered = text.lower()
    return any(pattern in lowered for pattern in GITLAB_CI_QUOTA_PATTERNS)


def _is_gitlab_bot_author(author: str, author_obj: dict[str, Any] | None = None) -> bool:
    """Return True if a GitLab note author is a bot or automated service user."""
    if author_obj and author_obj.get("bot") is True:
        return True
    cleaned = author.strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered.endswith("[bot]") or lowered in GITLAB_BOT_USERNAMES:
        return True
    return bool(_GITLAB_TOKEN_BOT_RE.match(lowered))


class GitLabForge:
    """GitLab forge provider backed by the `glab` CLI and GitLab REST API v4."""

    def __init__(
        self,
        repo_root: Path | None = None,
        glab_bin: str = "glab",
        project_path: str | None = None,
        host: str | None = None,
        daemon_tag: str = DEFAULT_DAEMON_TAG,
        run_cmd_fn: RunCmdCallable | None = None,
        check_network_fn: Callable[[], bool] | None = None,
        wait_network_fn: Callable[[], bool] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.glab_bin = glab_bin
        self._project_path = project_path.strip("/") if project_path else None
        self.host = host
        self.daemon_tag = daemon_tag
        self._run_cmd_fn: RunCmdCallable = run_cmd_fn or _default_run_cmd
        self._check_network_fn = check_network_fn
        self._wait_network_fn = wait_network_fn

    @property
    def encoded_project_path(self) -> str:
        """Return URL-encoded project path (`group%2Fsubgroup%2Frepo`) or `:id` placeholder."""
        if self._project_path:
            return encode_gitlab_project_path(self._project_path)
        return ":id"

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
        """Return the `group/subgroup/repo` project path for the repository."""
        if self._project_path:
            return self._project_path

        from orchestack.forge.detect import parse_git_remote

        remote_res = self._run_cmd(["git", "remote", "get-url", "origin"], check=False)
        if remote_res.returncode == 0 and remote_res.stdout and remote_res.stdout.strip():
            try:
                _, parsed_host, parsed_path = parse_git_remote(remote_res.stdout.strip())
                if parsed_path:
                    self._project_path = parsed_path
                    if not self.host and parsed_host:
                        self.host = parsed_host
                    return parsed_path
            except ValueError:
                pass

        view_res = self._run_cmd(
            [self.glab_bin, "repo", "view", "-F", "json"],
            check=False,
        )
        if view_res.returncode == 0 and view_res.stdout and view_res.stdout.strip():
            try:
                data = json.loads(view_res.stdout.strip())
                if isinstance(data, dict):
                    slug = str(
                        data.get("path_with_namespace") or data.get("fullPath") or ""
                    ).strip()
                    if slug:
                        self._project_path = slug
                        return slug
            except Exception as e:
                logger.debug("Could not parse glab repo view JSON: %s", e)

        return ""

    @staticmethod
    def _mr_dict_to_info(item: dict[str, Any], default_branch: str = "") -> PullRequestInfo:
        """Convert a raw GitLab MR dict into a normalized `PullRequestInfo`."""
        iid_raw = item.get("iid") if item.get("iid") is not None else item.get("number")
        number = int(iid_raw) if iid_raw is not None else 0
        head_ref = str(
            item.get("source_branch")
            or item.get("headRefName")
            or item.get("head_ref")
            or default_branch
        )
        base_ref = str(
            item.get("target_branch") or item.get("baseRefName") or item.get("base_ref") or "main"
        )
        url = str(item.get("web_url") or item.get("url") or "")
        state = normalize_gitlab_mr_state(str(item.get("state") or "opened"))
        is_draft = bool(
            item.get("draft", False)
            or item.get("work_in_progress", False)
            or item.get("isDraft", False)
        )
        mergeable = str(
            item.get("detailed_merge_status")
            or item.get("merge_status")
            or item.get("mergeable")
            or "UNKNOWN"
        )
        return PullRequestInfo(
            number=number,
            title=str(item.get("title") or ""),
            url=url,
            head_ref=head_ref,
            base_ref=base_ref,
            state=state,
            is_draft=is_draft,
            mergeable=mergeable,
        )

    def list_open_prs_raw(self, non_blocking: bool = False) -> list[dict[str, Any]]:
        """Return raw open merge request dicts from `glab mr list` or `glab api`."""
        while True:
            try:
                res = self._run_cmd(
                    [self.glab_bin, "mr", "list", "-F", "json"],
                    check=False,
                )
                if res.returncode != 0:
                    api_res = self._run_cmd(
                        [
                            self.glab_bin,
                            "api",
                            f"projects/{self.encoded_project_path}/merge_requests?state=opened",
                        ],
                        check=False,
                    )
                    if api_res.returncode == 0 and api_res.stdout and api_res.stdout.strip():
                        data: list[dict[str, Any]] = json.loads(api_res.stdout)
                        return data

                    err_text = (res.stderr or api_res.stderr or "").lower()
                    network_down = (
                        "network" in err_text
                        or "connection" in err_text
                        or (self._check_network_fn is not None and not self._check_network_fn())
                    )
                    if network_down:
                        if non_blocking or self._wait_network_fn is None:
                            logger.warning(
                                "Network appears offline; skipping open MRs check (non-blocking)."
                            )
                            return []
                        self._wait_network_fn()
                        continue
                    logger.warning("Failed to check open MRs: %s", res.stderr)
                    return []

                if not res.stdout or not res.stdout.strip():
                    return []
                parsed: list[dict[str, Any]] = json.loads(res.stdout)
                return parsed
            except Exception as e:
                logger.warning("Error checking open MRs: %s", e)
                return []

    def list_open_prs(self, branch_prefix: str | None = None) -> list[PullRequestInfo]:
        """List open merge requests as normalized `PullRequestInfo` objects."""
        raw_mrs = self.list_open_prs_raw(non_blocking=True)
        results: list[PullRequestInfo] = []
        for item in raw_mrs:
            info = self._mr_dict_to_info(item)
            if info.state not in ("OPEN", "UNKNOWN"):
                continue
            if branch_prefix is not None and not info.head_ref.startswith(branch_prefix):
                continue
            results.append(info)
        return results

    def get_branch_pr_status(self, branch: str) -> dict[str, Any] | None:
        """Return raw MR metadata dict for `branch` across all states."""
        encoded_branch = urllib.parse.quote(branch, safe="")
        try:
            res = self._run_cmd(
                [
                    self.glab_bin,
                    "api",
                    f"projects/{self.encoded_project_path}/merge_requests?source_branch={encoded_branch}",
                ],
                check=False,
            )
            if res.returncode != 0 or not res.stdout or not res.stdout.strip():
                res = self._run_cmd(
                    [
                        self.glab_bin,
                        "mr",
                        "list",
                        "--source-branch",
                        branch,
                        "--all",
                        "-F",
                        "json",
                    ],
                    check=False,
                )
            if res.returncode == 0 and res.stdout and res.stdout.strip():
                mrs: list[dict[str, Any]] = json.loads(res.stdout)
                if mrs:
                    for mr in mrs:
                        norm_state = normalize_gitlab_mr_state(str(mr.get("state") or ""))
                        if norm_state in ("OPEN", "MERGED"):
                            normalized = dict(mr)
                            normalized["state"] = norm_state
                            if "number" not in normalized and "iid" in normalized:
                                normalized["number"] = int(normalized["iid"])
                            if "url" not in normalized and "web_url" in normalized:
                                normalized["url"] = normalized["web_url"]
                            return normalized
                    first = dict(mrs[0])
                    first["state"] = normalize_gitlab_mr_state(str(first.get("state") or ""))
                    if "number" not in first and "iid" in first:
                        first["number"] = int(first["iid"])
                    if "url" not in first and "web_url" in first:
                        first["url"] = first["web_url"]
                    return first
        except Exception as e:
            logger.debug("Error checking MR status for branch %s: %s", branch, e)
        return None

    def get_pr_for_branch(self, branch: str) -> PullRequestInfo | None:
        """Return normalized `PullRequestInfo` for `branch`, or None if no MR exists."""
        mr = self.get_branch_pr_status(branch)
        if not mr:
            return None
        return self._mr_dict_to_info(mr, default_branch=branch)

    def get_pr_state(self, branch: str) -> tuple[str | None, int | None]:
        """Return `(state, mr_iid)` for `branch`, or `(None, None)` if no MR exists."""
        info = self.get_pr_for_branch(branch)
        if not info:
            return (None, None)
        return (info.state, info.number)

    def create_pr_with_body(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> subprocess.CompletedProcess[str]:
        """Create a merge request using `glab mr create` with `--description`."""
        return self._run_cmd(
            [
                self.glab_bin,
                "mr",
                "create",
                "--source-branch",
                branch,
                "--target-branch",
                base_branch,
                "--title",
                title,
                "--description",
                body,
                "--yes",
            ],
            check=False,
        )

    def create_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body_file: Path,
    ) -> str | None:
        """Create a merge request from `body_file` and return the created MR URL or None."""
        body_text = ""
        if body_file.is_file():
            try:
                body_text = body_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("Could not read MR description file %s: %s", body_file, e)

        res = self.create_pr_with_body(branch, base_branch, title, body_text)
        if res.returncode == 0 and res.stdout:
            output = res.stdout.strip()
            if not output:
                return None
            url_match = _URL_LINE_RE.search(output)
            if url_match:
                return url_match.group(1)
            return output.splitlines()[-1].strip() or None
        return None

    def get_mr_pipelines(self, pr_number: int | str) -> list[dict[str, Any]]:
        """Fetch pipelines for merge request `pr_number` via `glab api`."""
        try:
            res = self._run_cmd(
                [
                    self.glab_bin,
                    "api",
                    f"projects/{self.encoded_project_path}/merge_requests/{pr_number}/pipelines",
                ],
                check=False,
            )
            if res.returncode == 0 and res.stdout and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning("Error fetching pipelines for MR !%s: %s", pr_number, e)
        return []

    def get_pipeline_jobs(self, pipeline_id: int | str) -> list[dict[str, Any]]:
        """Fetch jobs for `pipeline_id` via `glab api`."""
        try:
            res = self._run_cmd(
                [
                    self.glab_bin,
                    "api",
                    f"projects/{self.encoded_project_path}/pipelines/{pipeline_id}/jobs",
                ],
                check=False,
            )
            if res.returncode == 0 and res.stdout and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning("Error fetching jobs for pipeline #%s: %s", pipeline_id, e)
        return []

    def get_job_trace(self, job_id: int | str) -> str:
        """Fetch raw trace logs for `job_id` via `glab api projects/:id/jobs/:job_id/trace`."""
        try:
            res = self._run_cmd(
                [
                    self.glab_bin,
                    "api",
                    f"projects/{self.encoded_project_path}/jobs/{job_id}/trace",
                ],
                check=False,
            )
            trace = res.stdout or res.stderr or ""
            if len(trace) > MAX_FAILED_LOG_CHARS:
                trace = "... [truncated output] ...\n" + trace[-MAX_FAILED_LOG_CHARS:]
            return trace
        except Exception as e:
            logger.debug("Error fetching trace for job #%s: %s", job_id, e)
            return ""

    @staticmethod
    def _classify_check(check: dict[str, Any]) -> str:
        """Classify a GitLab pipeline or job dict into `'pending'`, `'pass'`, or `'fail'`."""
        bucket = str(check.get("bucket") or "").lower()
        if bucket in ("fail", "pending", "pass"):
            return bucket
        status = str(check.get("status") or check.get("state") or check.get("conclusion") or "")
        return classify_gitlab_ci_status(status)

    def is_ci_quota_failure(self, pr_number: int, checks: list[dict[str, Any]]) -> bool:
        """Return True if GitLab CI failed due to runner quota or billing exhaustion (B5).

        Inspects job `failure_reason` (`ci_quota_exceeded`, `runner_system_failure`,
        `shared_runners_minutes_limit_exceeded`) and `/projects/:id/jobs/:job_id/trace`
        for quota/billing messages.
        """
        if not checks:
            return False

        failed_checks = [c for c in checks if self._classify_check(c) == "fail"]
        if not failed_checks:
            return False

        # 1. Direct inspection of `failure_reason` or embedded logs/metadata
        for item in failed_checks:
            if is_gitlab_ci_quota_failure(item):
                return True

        # 2. Inspect job traces (or pipeline jobs if pipeline dicts were passed)
        inspected_job_ids: set[str] = set()
        for item in failed_checks:
            is_pipeline_item = (
                "sha" in item
                and "stage" not in item
                and "failure_reason" not in item
                and "name" not in item
            )
            if is_pipeline_item and item.get("id") is not None:
                pipeline_jobs = self.get_pipeline_jobs(item["id"])
                for job in pipeline_jobs:
                    if self._classify_check(job) != "fail":
                        continue
                    if is_gitlab_ci_quota_failure(job):
                        return True
                    jid = str(job.get("id") or "")
                    if jid and jid not in inspected_job_ids:
                        inspected_job_ids.add(jid)
                        trace = self.get_job_trace(jid)
                        if is_gitlab_ci_quota_failure(trace):
                            return True
                continue

            job_id = str(item.get("id") or item.get("job_id") or "")
            if job_id and job_id not in inspected_job_ids:
                inspected_job_ids.add(job_id)
                trace = self.get_job_trace(job_id)
                if is_gitlab_ci_quota_failure(trace):
                    return True

        return False

    def get_ci_check_result(self, pr_number: int) -> CICheckResult:
        """Evaluate GitLab CI pipelines/jobs for `pr_number` and return `CICheckResult`."""
        pipelines = self.get_mr_pipelines(pr_number)
        if not pipelines:
            return CICheckResult(
                status="PENDING",
                details="No CI pipelines reported yet",
                failed_urls=[],
                raw_checks=[],
            )

        latest = pipelines[0]
        pipeline_id = latest.get("id")
        pipeline_status = str(latest.get("status") or "")
        bucket = classify_gitlab_ci_status(pipeline_status)
        pipeline_url = str(latest.get("web_url") or latest.get("url") or "").strip()

        if bucket == "pending":
            return CICheckResult(
                status="PENDING",
                details=f"Pipeline #{pipeline_id} is {pipeline_status or 'pending'}",
                failed_urls=[],
                raw_checks=pipelines,
            )

        if bucket == "pass":
            return CICheckResult(
                status="PASSED",
                details=f"Pipeline #{pipeline_id} passed ({pipeline_status or 'success'})",
                failed_urls=[],
                raw_checks=pipelines,
            )

        # Pipeline failed or canceled: inspect jobs for failure details and quota exhaustion
        jobs = self.get_pipeline_jobs(pipeline_id) if pipeline_id is not None else []
        checks_to_inspect = jobs if jobs else [latest]

        failed_urls: list[str] = []
        failed_names: list[str] = []
        for job in jobs:
            if self._classify_check(job) == "fail":
                jname = str(job.get("name") or job.get("id") or "unknown")
                failed_names.append(jname)
                jurl = str(job.get("web_url") or job.get("url") or "").strip()
                if jurl:
                    failed_urls.append(jurl)
        if not failed_urls and pipeline_url:
            failed_urls.append(pipeline_url)

        if self.is_ci_quota_failure(pr_number, checks_to_inspect):
            return CICheckResult(
                status="QUOTA_EXCEEDED",
                details="CI failed due to GitLab CI runner quota or billing exhaustion",
                failed_urls=failed_urls,
                raw_checks=checks_to_inspect,
            )

        details = (
            f"Failed jobs: {', '.join(failed_names)}"
            if failed_names
            else f"Pipeline #{pipeline_id} failed ({pipeline_status})"
        )
        return CICheckResult(
            status="FAILED",
            details=details,
            failed_urls=failed_urls,
            raw_checks=checks_to_inspect,
        )

    def get_ci_status(self, pr_number: int) -> tuple[str, str]:
        """Return `(status, details)` for CI pipelines on `pr_number`."""
        result = self.get_ci_check_result(pr_number)
        return (result.status, result.details)

    def fetch_unprocessed_reviews(
        self,
        pr_number: int,
        processed_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch unprocessed GitLab MR discussions, inline `DiffNote` threads, and approvals.

        Ignores system notes (`"system": true`), bot authors, and comments containing
        `DAEMON_TAG` (`B4`). Records approvals in `newly_seen_approved_ids` without
        triggering fix loops (`B4`).
        """
        processed_set = {str(cid) for cid in processed_ids}
        unhandled: list[dict[str, Any]] = []
        newly_seen_approved_ids: list[str] = []

        # 1. Query MR discussions (includes top-level notes and inline DiffNote threads)
        disc_res = self._run_cmd(
            [
                self.glab_bin,
                "api",
                f"projects/{self.encoded_project_path}/merge_requests/{pr_number}/discussions",
            ],
            check=False,
        )
        if disc_res.returncode == 0 and disc_res.stdout and disc_res.stdout.strip():
            try:
                discussions = json.loads(disc_res.stdout)
                if isinstance(discussions, list):
                    for entry in discussions:
                        if not isinstance(entry, dict):
                            continue
                        notes = entry["notes"] if isinstance(entry.get("notes"), list) else [entry]
                        for note in notes:
                            if not isinstance(note, dict):
                                continue
                            note_id = str(note.get("id") or "")
                            if not note_id or note_id in processed_set:
                                continue
                            if note.get("system") is True:
                                continue

                            author_raw = note.get("author") or note.get("user")
                            author_obj = author_raw if isinstance(author_raw, dict) else None
                            if author_obj is not None:
                                author = str(
                                    author_obj.get("username")
                                    or author_obj.get("login")
                                    or author_obj.get("name")
                                    or ""
                                )
                            else:
                                author = str(author_raw or "")

                            if _is_gitlab_bot_author(author, author_obj):
                                continue

                            body = str(note.get("body") or "").strip()
                            if not body or self._is_daemon_body(body):
                                continue

                            pos_raw = note.get("position")
                            position = pos_raw if isinstance(pos_raw, dict) else {}
                            is_diff_note = str(note.get("type") or "") == "DiffNote" or bool(
                                position
                            )

                            path = (
                                position.get("new_path")
                                or position.get("old_path")
                                or note.get("path")
                            )
                            new_line = position.get("new_line")
                            old_line = position.get("old_line")
                            raw_line = (
                                new_line
                                if new_line is not None
                                else (old_line if old_line is not None else note.get("line"))
                            )
                            line = int(raw_line) if isinstance(raw_line, int) else None

                            unhandled.append(
                                {
                                    "id": note_id,
                                    "type": "review_comment" if is_diff_note else "comment",
                                    "author": author,
                                    "body": body,
                                    "path": str(path) if path is not None else None,
                                    "line": line,
                                    "diff_hunk": note.get("diff_hunk"),
                                    "created_at": str(note.get("created_at") or ""),
                                }
                            )
            except Exception as e:
                logger.warning("Error parsing MR !%s discussions: %s", pr_number, e)

        # 2. Query MR approvals (mark seen without triggering feedback loop per B4)
        app_res = self._run_cmd(
            [
                self.glab_bin,
                "api",
                f"projects/{self.encoded_project_path}/merge_requests/{pr_number}/approvals",
            ],
            check=False,
        )
        if app_res.returncode == 0 and app_res.stdout and app_res.stdout.strip():
            try:
                app_data = json.loads(app_res.stdout)
                approved_by: list[Any] = []
                if isinstance(app_data, dict):
                    raw_approved_by = app_data.get("approved_by")
                    if isinstance(raw_approved_by, list):
                        approved_by = raw_approved_by
                elif isinstance(app_data, list):
                    approved_by = app_data

                for entry in approved_by:
                    if not isinstance(entry, dict):
                        continue
                    user_raw = entry.get("user")
                    user_obj = user_raw if isinstance(user_raw, dict) else entry
                    explicit_id = entry.get("id") if "user" not in entry else None
                    if explicit_id is not None:
                        approval_id = str(explicit_id)
                    else:
                        ident = user_obj.get("username") or user_obj.get("id") or ""
                        approval_id = f"approval-{ident}" if ident else ""
                    if (
                        approval_id
                        and approval_id not in processed_set
                        and approval_id not in newly_seen_approved_ids
                    ):
                        newly_seen_approved_ids.append(approval_id)
            except Exception as e:
                logger.debug("Could not parse MR !%s approvals: %s", pr_number, e)

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
        """Post a note on MR `pr_number` via `glab mr note`, truncating if >60,000 chars (`B4`)."""
        safe_body = truncate_pr_comment(body)
        res = self._run_cmd(
            [self.glab_bin, "mr", "note", str(pr_number), "-m", safe_body],
            check=False,
        )
        return res.returncode == 0

    def merge_pr(
        self,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> bool:
        """Merge MR `pr_number` via `glab mr merge <iid> --squash --remove-source-branch --yes`."""
        normalized_method = method.strip().lower()
        cmd = [self.glab_bin, "mr", "merge", str(pr_number)]
        if normalized_method == "squash":
            cmd.append("--squash")
        elif normalized_method == "rebase":
            cmd.append("--rebase")
        if delete_branch:
            cmd.append("--remove-source-branch")
        cmd.append("--yes")
        res = self._run_cmd(cmd, check=False)
        return res.returncode == 0

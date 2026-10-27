# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Forge provider protocol and normalized data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

CIStatusLiteral = Literal["PENDING", "PASSED", "FAILED", "QUOTA_EXCEEDED"]
ReviewItemTypeLiteral = Literal["review", "inline_comment", "discussion_comment"]


@dataclass
class PullRequestInfo:
    """Normalized pull/merge request metadata across forge providers."""

    number: int
    title: str
    url: str
    head_ref: str
    base_ref: str
    state: str
    is_draft: bool = False
    mergeable: str = "UNKNOWN"


@dataclass
class CICheckResult:
    """Normalized CI check status and failure details for a PR/MR."""

    status: CIStatusLiteral
    details: str
    failed_urls: list[str] = field(default_factory=list)
    raw_checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReviewItem:
    """Normalized review, inline diff comment, or discussion comment on a PR/MR."""

    id: str
    author: str
    body: str
    created_at: str
    item_type: ReviewItemTypeLiteral
    state: str | None = None
    path: str | None = None
    line: int | None = None


@runtime_checkable
class ForgeProvider(Protocol):
    """Protocol implemented by all source code forge providers (GitHub, GitLab, Local)."""

    def get_repo_slug(self) -> str:
        """Return the repository `owner/repo` slug."""
        ...

    def list_open_prs(self, branch_prefix: str | None = None) -> list[PullRequestInfo]:
        """List open pull/merge requests, optionally filtered by head branch prefix."""
        ...

    def get_pr_for_branch(self, branch: str) -> PullRequestInfo | None:
        """Return pull/merge request info for `branch`, or None if none exists."""
        ...

    def get_pr_state(self, branch: str) -> tuple[str | None, int | None]:
        """Return `(state, pr_number)` for `branch`, or `(None, None)` if no PR exists."""
        ...

    def create_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body_file: Path,
    ) -> str | None:
        """Create a pull/merge request and return its URL (or None on failure)."""
        ...

    def get_ci_status(self, pr_number: int) -> tuple[str, str]:
        """Return `(status, details)` for CI checks on `pr_number`."""
        ...

    def is_ci_quota_failure(self, pr_number: int, checks: list[dict[str, Any]]) -> bool:
        """Return True if CI checks failed due to billing/spending limits or quota exhaustion."""
        ...

    def fetch_unprocessed_reviews(
        self,
        pr_number: int,
        processed_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch unprocessed reviews/comments and return `(unhandled_items, newly_seen_ids)`."""
        ...

    def post_pr_comment(self, pr_number: int, body: str) -> bool:
        """Post a comment on `pr_number` (truncating if >60,000 chars) and return True."""
        ...

    def merge_pr(
        self,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> bool:
        """Merge `pr_number` using `method` and return True on success."""
        ...

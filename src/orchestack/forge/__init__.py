# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Forge provider abstractions and implementations for Orchestack."""

from orchestack.forge.base import (
    CICheckResult,
    CIStatusLiteral,
    ForgeProvider,
    PullRequestInfo,
    ReviewItem,
    ReviewItemTypeLiteral,
)
from orchestack.forge.detect import (
    create_forge_provider,
    detect_forge_type,
    get_git_remote_url,
    parse_git_remote,
)
from orchestack.forge.github import (
    CI_BILLING_OR_QUOTA_PATTERNS,
    GitHubForge,
    is_ci_billing_or_quota_failure,
    truncate_pr_comment,
)
from orchestack.forge.gitlab import (
    GITLAB_CI_QUOTA_PATTERNS,
    GITLAB_QUOTA_FAILURE_REASONS,
    GitLabForge,
    classify_gitlab_ci_status,
    encode_gitlab_project_path,
    is_gitlab_ci_quota_failure,
    normalize_gitlab_mr_state,
)

__all__ = [
    "CI_BILLING_OR_QUOTA_PATTERNS",
    "GITLAB_CI_QUOTA_PATTERNS",
    "GITLAB_QUOTA_FAILURE_REASONS",
    "CICheckResult",
    "CIStatusLiteral",
    "ForgeProvider",
    "GitHubForge",
    "GitLabForge",
    "PullRequestInfo",
    "ReviewItem",
    "ReviewItemTypeLiteral",
    "classify_gitlab_ci_status",
    "create_forge_provider",
    "detect_forge_type",
    "encode_gitlab_project_path",
    "get_git_remote_url",
    "is_ci_billing_or_quota_failure",
    "is_gitlab_ci_quota_failure",
    "normalize_gitlab_mr_state",
    "parse_git_remote",
    "truncate_pr_comment",
]

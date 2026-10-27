# 0002 — ForgeProvider protocol and GitHubForge — Learnings

## Summary
- **What shipped**: Defined the `@runtime_checkable` `ForgeProvider` protocol and normalized forge data models (`PullRequestInfo`, `CICheckResult`, `ReviewItem`) in `src/orchestack/forge/base.py`, implemented `GitHubForge` in `src/orchestack/forge/github.py`, wired `scripts/daemon_runner.py` to delegate GitHub operations to `GitHubForge`, and added 14 unit tests in `tests/test_forge_github.py`.
- **Key packages/files**: `src/orchestack/forge/base.py`, `src/orchestack/forge/github.py`, `src/orchestack/forge/__init__.py`, `scripts/daemon_runner.py`, `tests/test_forge_github.py`, `docs/PLAN.md`.
- **Key interfaces/types added or changed**: `ForgeProvider`, `PullRequestInfo`, `CICheckResult`, `ReviewItem`, `GitHubForge`, `is_ci_billing_or_quota_failure()`, `truncate_pr_comment()`, `CI_BILLING_OR_QUOTA_PATTERNS`, and `_make_github_forge()` in `scripts/daemon_runner.py`.
- **Decisions made/affected**: D4, D8.
- **Gotchas / non-obvious constraints**: Existing unit tests in `scripts/tests/test_daemon_runner.py` monkeypatch `daemon_runner.run_cmd`, `daemon_runner.DAEMON_TAG`, `daemon_runner.check_network_connectivity`, and `daemon_runner.wait_for_network_connectivity`. Constructing `GitHubForge` via `_make_github_forge(gh_bin)` with injected callables (`run_cmd_fn=run_cmd`, `check_network_fn=check_network_connectivity`, `wait_network_fn=wait_for_network_connectivity`) ensures all `patch("daemon_runner.*")` mocks intercept `GitHubForge` subprocess calls seamlessly.
- **What the NEXT session must know**: Proceed with `prompts/queued/0003-gitlab-forge-provider.md`. Implement `GitLabForge` in `src/orchestack/forge/gitlab.py` conforming to `ForgeProvider` (`glab mr` + `glab api` for inline `DiffNote` discussions, approvals, and CI job trace quota checks) and add forge factory/auto-detection (`detect_forge_type`, `create_forge_provider`) in `src/orchestack/forge/__init__.py`.

## Details

### Architecture & Backward Compatibility
1. **`ForgeProvider` Protocol & Data Models (`src/orchestack/forge/base.py`)**:
   - `PullRequestInfo(number, title, url, head_ref, base_ref, state, is_draft=False, mergeable="UNKNOWN")` normalizes open/merged/closed PR and MR metadata.
   - `CICheckResult(status, details, failed_urls, raw_checks)` represents normalized CI check status (`"PASSED"`, `"PENDING"`, `"FAILED"`, `"QUOTA_EXCEEDED"`).
   - `ReviewItem(id, author, body, created_at, item_type, state=None, path=None, line=None)` unifies reviews (`"review"`), inline diff comments (`"inline_comment"`), and discussion comments (`"discussion_comment"`).
   - `@runtime_checkable class ForgeProvider(Protocol)` defines the 10 forge methods (`get_repo_slug`, `list_open_prs`, `get_pr_for_branch`, `get_pr_state`, `create_pr`, `get_ci_status`, `is_ci_quota_failure`, `fetch_unprocessed_reviews`, `post_pr_comment`, `merge_pr`).

2. **`GitHubForge` Implementation (`src/orchestack/forge/github.py`)**:
   - Preserves `B4` (Review & Inline Comment Deduplication): filters out `*[bot]`, `github-actions`, `codecov`, `DAEMON_TAG`, `[BlueTeamHarness Daemon]`, and `[Orchestack Daemon]`; returns newly seen `APPROVED` review IDs in the second element of `fetch_unprocessed_reviews(pr_number, processed_ids)` so callers can mark them processed without triggering a feedback loop; truncates PR reply comments >60,000 characters to `59,900` chars + `"\n\n... (truncated)"`.
   - Preserves `B5` (CI Billing/Quota Fallback): falls back to `gh run view <run_id>` (without `--log-failed`) whenever `gh run view <run_id> --log-failed` fails, returns empty output, or reports `"log not found"` so workflow-level billing/spending-limit annotations are always inspected.

3. **`daemon_runner.py` Delegation**:
   - `get_github_repo`, `get_open_prs`, `get_branch_pr_status`, `get_pr_checks`, `mark_pr_ci_green`, `ensure_pr_opened`, `fetch_unhandled_pr_feedback`, `address_pr_feedback`, `is_ci_billing_or_quota_failure`, `fetch_latest_failed_ci_run`, and `address_ci_failure` delegate to `_make_github_forge(gh_bin)` while preserving their exact module-level signatures and return shapes.

# 0002 — `ForgeProvider` Protocol & `GitHubForge` Implementation

## Read first
- `docs/PROTOCOL.md` §1, §3, §3.1 (`B3`, `B4`, `B5`), §4, §5
- `docs/PLAN.md` §2 (`D1`, `D4`, `D9`, `D11`), §3, §5, §8
- `docs/learnings/0000-baseline-import-from-bth-learnings.md`
- `scripts/daemon_runner.py`
- `scripts/tests/test_daemon_runner.py`

## Objective
Define the `ForgeProvider` protocol and normalized data models in `src/orchestack/forge/` and extract all GitHub (`gh` CLI and `gh api`) operations into `GitHubForge` while preserving 100% of the review-loop, CI-billing-quota fallback, and merge-state invariants (`B3`, `B4`, `B5`).

## Deliverables
1. **`src/orchestack/forge/base.py`**:
   - Define normalized dataclasses:
     - `PullRequestInfo(number: int, title: str, url: str, head_ref: str, base_ref: str, state: str, is_draft: bool = False, mergeable: str = "UNKNOWN")`
     - `CICheckResult(status: Literal["PENDING", "PASSED", "FAILED", "QUOTA_EXCEEDED"], details: str, failed_urls: list[str], raw_checks: list[dict[str, Any]])`
     - `ReviewItem(id: str, author: str, body: str, created_at: str, item_type: Literal["review", "inline_comment", "discussion_comment"], state: str | None = None, path: str | None = None, line: int | None = None)`
   - Define `ForgeProvider(Protocol)` with methods:
     - `get_repo_slug() -> str`
     - `list_open_prs(branch_prefix: str | None = None) -> list[PullRequestInfo]`
     - `get_pr_for_branch(branch: str) -> PullRequestInfo | None`
     - `get_pr_state(branch: str) -> tuple[str | None, int | None]`
     - `create_pr(branch: str, base_branch: str, title: str, body_file: Path) -> str | None`
     - `get_ci_status(pr_number: int) -> tuple[str, str]`
     - `is_ci_quota_failure(pr_number: int, checks: list[dict[str, Any]]) -> bool`
     - `fetch_unprocessed_reviews(pr_number: int, processed_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]`
     - `post_pr_comment(pr_number: int, body: str) -> bool`
     - `merge_pr(pr_number: int, method: str = "squash", delete_branch: bool = True) -> bool`
2. **`src/orchestack/forge/github.py` (`GitHubForge`)**:
   - Extract GitHub PR listing, creation, state lookup, CI status checks, billing/quota failure inspection (`is_ci_billing_or_quota_failure` including `gh run view <run_id>` fallback when `conclusion == "FAILURE"` has no URL per `B5`), review/comment fetching (`fetch_unprocessed_reviews` ignoring `[bot]`, `DAEMON_TAG`, and marking `APPROVED` reviews as seen per `B4`), comment truncation (`>60,000` chars per `B4`), and squash-merging.
3. **Wire `GitHubForge` into `scripts/daemon_runner.py`**:
   - Delegate `daemon_runner.py` GitHub helper functions to `GitHubForge` (while keeping the module-level helper functions in `daemon_runner.py` wired so existing unit test monkeypatches on `daemon_runner.run_cmd`, `daemon_runner.get_github_repo`, etc. continue to work seamlessly per `D11`).
4. **Unit Tests (`tests/test_forge_github.py`)**:
   - Add dedicated unit tests for `GitHubForge` covering PR creation, `get_ci_status`, billing/quota fallback (`gh run view`), review filtering (`[bot]`, `DAEMON_TAG`, `APPROVED`), comment truncation, and merging.
   - Verify all 82 baseline tests in `scripts/tests/test_daemon_runner.py` pass.

## Acceptance Criteria
- `make check` passes (`ruff check`, `ruff format --check`, `mypy`, `pytest`).
- All 82 baseline tests in `scripts/tests/test_daemon_runner.py` + new tests in `tests/test_forge_github.py` pass.
- `docs/PLAN.md` §13 status for `0002` is updated to `Implemented`, and `docs/learnings/0002-forge-provider-protocol-and-github-learnings.md` + `docs/PR_DESCRIPTION.md` are created.

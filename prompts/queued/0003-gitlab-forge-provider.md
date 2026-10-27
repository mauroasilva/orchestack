# 0003 — `GitLabForge` Provider & Remote Forge Auto-Detection

## Read first
- `docs/PROTOCOL.md` §1, §3, §3.1 (`B3`, `B4`, `B5`), §4, §5
- `docs/PLAN.md` §2 (`D4`, `D5`, `D9`), §3, §5, §8
- `docs/learnings/0000-baseline-import-from-bth-learnings.md`
- `src/orchestack/forge/`
- `scripts/daemon_runner.py`

## Objective
Implement full **GitLab** Merge Request (`MR`), CI pipeline, inline `DiffNote` discussion, and approval support via `GitLabForge` (`glab` CLI + GitLab REST API v4), along with automatic forge detection from `git remote get-url origin`.

## Deliverables
1. **`src/orchestack/forge/detect.py`**:
   - Implement `parse_git_remote(remote_url: str) -> tuple[str, str, str]` returning `(forge_type, host, project_path)` for both HTTPS (`https://gitlab.com/group/subgroup/repo.git`) and SSH (`git@gitlab.example.com:group/subgroup/repo.git`) URLs.
   - Implement `create_forge_provider(config: RepoConfig) -> ForgeProvider` supporting `config.forge_provider` (`"auto"`, `"github"`, `"gitlab"`).
2. **`src/orchestack/forge/gitlab.py` (`GitLabForge`)**:
   - URL-encode nested GitLab project paths (`group/subgroup/repo` -> `group%2Fsubgroup%2Frepo`) for `glab api projects/<encoded_path>/...`.
   - Implement `list_open_prs()`, `get_pr_for_branch()`, and `get_pr_state()` using `glab mr list` / `glab api projects/:id/merge_requests` (normalizing GitLab states `"opened"` -> `"OPEN"`, `"merged"` -> `"MERGED"`, `"closed"` -> `"CLOSED"`, and `iid` -> `number`).
   - Implement `create_pr()` using `glab mr create --source-branch <branch> --target-branch <base_branch> --title <title> --description <body_text> --yes`.
   - Implement `get_ci_status()` and `is_ci_quota_failure()`:
     - Query `glab api projects/:id/merge_requests/:iid/pipelines` and `/projects/:id/pipelines/:pipeline_id/jobs`.
     - Map GitLab pipeline statuses (`running`, `pending`, `created`, `preparing` -> `"PENDING"`; `success`, `skipped` -> `"PASSED"`; `failed`, `canceled` -> `"FAILED"`).
     - Inspect job `failure_reason` (`ci_quota_exceeded`, `runner_system_failure`, `shared_runners_minutes_limit_exceeded`) and `/projects/:id/jobs/:job_id/trace` for quota/billing messages so `B5` local test fallback works identically on GitLab MRs.
   - Implement `fetch_unprocessed_reviews()`:
     - Query `glab api projects/:id/merge_requests/:iid/discussions` and `/projects/:id/merge_requests/:iid/approvals`.
     - Ignore system notes (`"system": true`), bot authors, and comments containing `DAEMON_TAG` (`B4`).
     - Extract inline diff context (`note["type"] == "DiffNote"`, `position.new_path`, `position.new_line` or `position.old_line`) into the normalized review payload.
     - Handle approval events without triggering unnecessary fix loops (`B4`).
   - Implement `post_pr_comment()` via `glab mr note <iid> -m <body>` (enforcing the 60,000-character truncation guard per `B4`).
   - Implement `merge_pr()` via `glab mr merge <iid> --squash --remove-source-branch --yes`.
3. **Unit Tests (`tests/test_forge_gitlab.py`)**:
   - Test SSH & HTTPS remote URL parsing (including nested GitLab subgroups and self-hosted domains).
   - Test `GitLabForge` MR listing, state mapping (`opened`/`merged`/`closed`), inline `DiffNote` discussion extraction, system/bot/`DAEMON_TAG` filtering, pipeline status mapping, GitLab CI quota fallback detection, and MR merging.
   - Ensure all 82 baseline tests in `scripts/tests/test_daemon_runner.py` remain green.

## Acceptance Criteria
- `make check` passes (`ruff check`, `ruff format --check`, `mypy`, `pytest`).
- All baseline and new unit tests pass.
- `docs/PLAN.md` §13 status for `0003` is updated to `Implemented`, and `docs/learnings/0003-gitlab-forge-provider-learnings.md` + `docs/PR_DESCRIPTION.md` are created.

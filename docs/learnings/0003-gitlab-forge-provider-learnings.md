# 0003 — GitLab Forge Provider and Auto-Detection — Learnings

## Summary
- **What shipped**: Implemented `GitLabForge` in `src/orchestack/forge/gitlab.py` conforming to `ForgeProvider`, implemented git remote URL parsing and forge factory (`parse_git_remote`, `detect_forge_type`, `create_forge_provider`) in `src/orchestack/forge/detect.py`, extended `RepoConfig` with `forge_provider`/`forge_host`/`forge_project`, and added 12 unit tests in `tests/test_forge_gitlab.py`.
- **Key packages/files**: `src/orchestack/forge/gitlab.py`, `src/orchestack/forge/detect.py`, `src/orchestack/forge/__init__.py`, `src/orchestack/config.py`, `tests/test_forge_gitlab.py`, `docs/PLAN.md`.
- **Key interfaces/types added or changed**: `GitLabForge`, `parse_git_remote()`, `detect_forge_type()`, `create_forge_provider()`, `get_git_remote_url()`, `encode_gitlab_project_path()`, `normalize_gitlab_mr_state()`, `classify_gitlab_ci_status()`, `is_gitlab_ci_quota_failure()`, `GITLAB_QUOTA_FAILURE_REASONS`, and `GITLAB_CI_QUOTA_PATTERNS`.
- **Decisions made/affected**: D4, D8.
- **Gotchas / non-obvious constraints**: GitLab REST API v4 endpoints require URL-encoded project paths (`group%2Fsubgroup%2Frepo` via `urllib.parse.quote(path, safe="")`); `GitLabForge.encoded_project_path` returns the encoded path when `project_path` is known and falls back to `":id"` without spawning an extra `git remote` subprocess call so unit tests with mocked command sequences do not have extra calls consumed.
- **What the NEXT session must know**: Proceed with `prompts/queued/0004-agent-backend-protocol-and-antigravity.md`. Define the `AgentBackend` protocol and `PermissionDenial` models in `src/orchestack/agents/` and extract `AntigravityAgent` with all `B1`/`B2`/`B6` invariants preserved.

## Architectural Decisions
1. **Git Remote Parsing & Forge Auto-Detection (`src/orchestack/forge/detect.py`)**:
   - `parse_git_remote(remote_url)` supports HTTPS (`https://gitlab.com/group/subgroup/repo.git`), SCP-style SSH (`git@gitlab.example.com:group/subgroup/repo.git`), and `ssh://` URLs with custom ports or nested subgroups, returning `(forge_type, host, project_path)`.
   - `create_forge_provider(config)` inspects `config.forge_provider` / `config.forge_type` (`"auto"`, `"github"`, `"gitlab"`), auto-detecting from `git remote get-url origin` when set to `"auto"` and populating `host` and `project_path` on `GitLabForge`.
2. **`GitLabForge` Implementation (`src/orchestack/forge/gitlab.py`)**:
   - Uses `glab mr` (`list`, `create`, `note`, `merge`) and `glab api` against `/projects/<encoded_path>/merge_requests/...` and `/projects/<encoded_path>/pipelines/...`.
   - Normalizes GitLab MR states (`opened` -> `OPEN`, `merged` -> `MERGED`, `closed` -> `CLOSED`).
   - Preserves `B4` by filtering out `"system": true` notes, bot accounts (`gitlab-bot`, `project_<id>_bot*`, `author.bot=True`, `*[bot]`), and `DAEMON_TAG` comments, while recording MR approvals in `newly_seen_approved_ids` without triggering fix loops.
   - Preserves `B5` by checking both job `failure_reason` (`ci_quota_exceeded`, `runner_system_failure`, `shared_runners_minutes_limit_exceeded`) and `/projects/:id/jobs/:job_id/trace` output for quota/billing messages.

## Gotchas & Pitfalls
- **Nested GitLab Subgroups**: Standard `/` separators inside `/projects/:id/...` cause 404 responses on GitLab REST API v4 unless every `/` in `group/subgroup/repo` is percent-encoded (`%2F`) via `urllib.parse.quote(project_path, safe="")`.
- **`docs/PR_DESCRIPTION.md` Cleanup in `pytest`**: `test_setup_prompt_branch_avoids_merged_branch` in `scripts/tests/test_daemon_runner.py` invokes `setup_prompt_branch()`, which unlinks `docs/PR_DESCRIPTION.md`. Always write `docs/PR_DESCRIPTION.md` after running `make check`.

## Guidance for the Next Phase
- Phase `0004` (`prompts/queued/0004-agent-backend-protocol-and-antigravity.md`) extracts the `AgentBackend` protocol and `AntigravityAgent` (`src/orchestack/agents/base.py`, `src/orchestack/agents/antigravity.py`). Ensure all `B1`, `B2`, and `B6` regression invariants in `scripts/tests/test_daemon_runner.py` remain untouched and green.

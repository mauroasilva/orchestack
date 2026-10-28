# Learnings: Phase 0008 — Repository State Detection & Phase 0000 Bootstrap Interview Loop

## Summary
- Added `src/orchestack/bootstrap.py` defining `RepoBootstrapState` (`QUEUE_READY`, `BRIEF_ONLY`, `EMPTY_REPO`), `detect_repo_bootstrap_state()`, `ensure_base_branch_initialized()`, `scaffold_phase_0000_prompt()`, and Phase 0000 PR-comment interview helpers (`extract_phase_0000_questions()`, `is_phase_0000_interview_pending()`, `is_phase_0000_complete()`, `format_phase_0000_questions_comment()`, `format_phase_0000_completion_comment()`, and `build_phase_0000_interview_prompt()`).
- Extended `RepoConfig` in `src/orchestack/config.py` with `brief_path` (`docs/BRIEF.md`) and `phase_0000_questions_file` (`.daemon/phase0000_questions.md`), and updated `orchestack status` in `src/orchestack/cli.py` to report `bootstrap_state` in both human-readable and `--json` output.
- Integrated Phase 0000 bootstrap and multi-round PR-comment user interviewing into `scripts/daemon_runner.py` (`get_next_prompt`, `_execute_backend_prompt_session`, `execute_prompt_session`, `build_pr_description`, `ensure_pr_opened`, `build_review_feedback_prompt`, `address_pr_feedback`, `wait_for_pr_merge`, and `main`) while preserving all 82 baseline regression tests in `scripts/tests/test_daemon_runner.py`.

## Key Implementation Details

### 1. Three-State Repository Classification (`RepoBootstrapState`)
- `detect_repo_bootstrap_state(repo_root)` classifies any target repository into:
  - `QUEUE_READY`: `prompts/queued/` contains at least one numbered implementation prompt (`0001-*.md` or higher), or `prompts/implemented/` contains completed phases alongside `docs/PLAN.md`.
  - `BRIEF_ONLY`: `docs/BRIEF.md` exists and is non-empty, with no `0001+` prompts in `prompts/queued/`.
  - `EMPTY_REPO`: Neither `docs/BRIEF.md` nor `0001+` queued prompts exist.
- Note that when `prompts/queued/0000-bootstrap-plan-and-prompts.md` is present on disk during an active Phase 0000 interview (before any `0001+` prompts are generated), `has_queued_implementation_prompts()` ignores `0000-*` so the repository continues to report `brief_only` or `empty_repo` until Phase 0000 completes.

### 2. Phase 0000 Prompt Scaffolding & Empty Git Repo Initialization
- `ensure_base_branch_initialized(repo_root, base_branch)` verifies `git rev-parse --verify HEAD` and, if the repository has no commits yet (or no `.git` directory), initializes the repository and creates an initial commit with `.gitignore` on `<base_branch>` so branch creation (`<prefix>/0000-bootstrap-plan-and-prompts`) succeeds cleanly.
- `scaffold_phase_0000_prompt(repo_root, state)` writes `prompts/queued/0000-bootstrap-plan-and-prompts.md` and `.daemon/bootstrap_state.json`, recording missing skeleton files (`docs/BRIEF.md`, `docs/PLAN.md`, `docs/PROTOCOL.md`, `docs/LICENSE-HEADER.md`, `README.md`, `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`).

### 3. Multi-Round PR Comment Interview Loop & Circuit Breaker Exemption
- During Phase 0000, the agent writes its summary of understanding to `docs/PR_DESCRIPTION.md` and its numbered clarifying questions to `.daemon/phase0000_questions.md` (with fallback extraction from the agent's final response text) while leaving `0000-bootstrap-plan-and-prompts.md` in `prompts/queued/`.
- `_execute_backend_prompt_session()` and `execute_prompt_session()` detect `is_phase_0000_interview_pending()` and return `True` immediately after the interview turn so `ensure_pr_opened()` can open the PR with the summary of understanding as the PR body and post the numbered list of clarifying questions as a PR comment.
- In `wait_for_pr_merge()`, Phase 0000 branches (`"0000-" in branch_name` while `not is_phase_0000_complete(REPO_ROOT)`) are exempted from the `max_auto_heal_attempts` review-feedback circuit breaker and do not trigger `mark_pr_ci_green()` or `"PR Ready for Merge"` notifications until Phase 0000 completes (`prompts/implemented/0000-bootstrap-plan-and-prompts.md`, `docs/PLAN.md`, and `0001+` queued prompts exist).

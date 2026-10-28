# 0008 — Repository State Detection & Phase 0000 Bootstrap Interview Loop

## Read first
- `docs/PROTOCOL.md` (§1, §2, §3, §3.1 `B1`–`B8`, §4, §5, §6, §7, §8)
- `docs/PLAN.md` (§2 `D2`, `D4`, `D6`, `D10`, `D11`, `D13`, §4, §5, §6, §8, §10, §12, §13)
- `docs/learnings/` (Read the `## Summary` section of all existing files before starting)
- `src/orchestack/config.py`, `src/orchestack/cli.py`, `src/orchestack/forge/base.py`, `src/orchestack/forge/github.py`, `src/orchestack/forge/gitlab.py`, and `scripts/daemon_runner.py`

## Objective
Enable Orchestack to operate seamlessly across **three repository states**:
1. **State 1 — Queue-Ready Repository (`QUEUE_READY`)**: A repository that already has a built queue of prompts (`prompts/queued/NNNN-*.md` or `prompts/implemented/NNNN-*.md`), which Orchestack works through and implements automatically (existing behavior).
2. **State 2 — Brief-Only Repository (`BRIEF_ONLY`)**: A repository with just `docs/BRIEF.md` (and no built prompt queue). Orchestack launches an initial **Phase 0000** session (`prompts/queued/0000-bootstrap-plan-and-prompts.md`) that reads `docs/BRIEF.md` and interviews the user iteratively through PR/MR comments. The session assumes nothing and asks as many questions as needed to build `docs/PLAN.md` and all queued prompts (`prompts/queued/0001-*.md`, ...) required to realize the user's vision:
   - The initial PR builds a skeleton repository with the foundational files (`docs/KICKOFF.md`, `docs/PROTOCOL.md`, `docs/PLAN.md`, `docs/LICENSE-HEADER.md`, `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`, `.orchestack.toml`, `.gitignore`, etc.).
   - The PR description (`docs/PR_DESCRIPTION.md`) of that initial PR includes a clear summary of what the session understood it is trying to build.
   - For the initial PR and each subsequent iteration of user answers, the session posts a new PR comment containing a **numbered list of questions** (`1. ...`, `2. ...`).
   - The user answers the questions in their own PR comment; Orchestack detects the reply, resumes the Phase 0000 session, updates the PR/plan, and posts follow-up numbered questions until the agent is satisfied it fully understands the user's intent and generates the complete `docs/PLAN.md` and `prompts/queued/NNNN-*.md` queue.
3. **State 3 — Empty Repository (`EMPTY_REPO`)**: An empty repository (without `docs/BRIEF.md` or queued prompts). Orchestack follows the same Phase 0000 PR-comment interview workflow as State 2, and additionally builds and refines `docs/BRIEF.md` through the PR-comment interviews with the user.

## Deliverables

1. **Repository State Detection & Phase 0000 Bootstrap Module (`src/orchestack/bootstrap.py` & `src/orchestack/config.py`)**
   - Create `src/orchestack/bootstrap.py` (with the SPDX license header from `docs/LICENSE-HEADER.md`) defining:
     - `RepoBootstrapState(StrEnum)` with values:
       - `QUEUE_READY = "queue_ready"`
       - `BRIEF_ONLY = "brief_only"`
       - `EMPTY_REPO = "empty_repo"`
     - `PHASE_0000_FILENAME = "0000-bootstrap-plan-and-prompts.md"` and `PHASE_0000_SLUG = "0000-bootstrap-plan-and-prompts"`.
     - `detect_repo_bootstrap_state(repo_root: Path) -> RepoBootstrapState`:
       - Returns `QUEUE_READY` if `prompts/queued/` contains any `NNNN-*.md` prompt with `NNNN != "0000"` or `prompts/implemented/` contains any `NNNN-*.md` prompt.
       - Returns `BRIEF_ONLY` if not `QUEUE_READY` and `docs/BRIEF.md` exists with non-whitespace content (and WAS NOT created as an empty placeholder by an active `EMPTY_REPO` bootstrap, tracked via `.daemon/bootstrap_state.json`).
       - Returns `EMPTY_REPO` otherwise (e.g., a completely empty repo, or a repo with only initial git files such as `README.md`, `LICENSE`, `.gitignore`, or an in-progress `EMPTY_REPO` Phase 0000 session).
     - `ensure_base_branch_initialized(repo_root: Path, base_branch: str, run_cmd_fn: Callable[..., Any]) -> None`:
       - If the target git repository has no commits on `HEAD` / `origin/<base_branch>` (completely empty git repo), creates an initial root commit on `<base_branch>` and pushes it to `origin/<base_branch>` so that a Phase 0000 branch and PR can be created and diffed cleanly against `<base_branch>`.
     - `scaffold_phase_0000_prompt(repo_root: Path, state: RepoBootstrapState) -> Path`:
       - Records the initial bootstrap mode (`brief_only` or `empty_repo`) in `.daemon/bootstrap_state.json`.
       - Creates `prompts/queued/0000-bootstrap-plan-and-prompts.md` tailored to `BRIEF_ONLY` or `EMPTY_REPO`, instructing the agent to:
         1. Scaffold the skeleton repository (`docs/KICKOFF.md`, `docs/PROTOCOL.md`, `docs/PLAN.md`, `docs/LICENSE-HEADER.md`, `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`, `.orchestack.toml`, `.gitignore`, and `docs/BRIEF.md` if `EMPTY_REPO`).
         2. Read `docs/BRIEF.md` (in `BRIEF_ONLY` state) or initiate discovery for `docs/BRIEF.md` (in `EMPTY_REPO` state), **assume nothing**, and author `docs/PR_DESCRIPTION.md` with a comprehensive summary of what the session understands it is trying to build.
         3. Write a numbered list of clarifying questions (`1. ...`, `2. ...`, ...) to `.daemon/phase0000_questions.md` (and at the end of its response) whenever any requirement, architectural choice, constraint, workflow, or phase scope is ambiguous or unspecified, keeping `0000-bootstrap-plan-and-prompts.md` in `prompts/queued/` while awaiting user answers.
         4. Only after the user has answered via PR comments and no further questions remain: finalize `docs/BRIEF.md`, `docs/PLAN.md`, and all `prompts/queued/0001-*.md` ... `NNNN-*.md` prompts, remove `.daemon/phase0000_questions.md`, move `0000-bootstrap-plan-and-prompts.md` to `prompts/implemented/`, and write `docs/learnings/0000-bootstrap-plan-and-prompts-learnings.md`.
     - Helper functions for extracting/formatting numbered interview questions (`extract_phase_0000_questions(repo_root: Path, agent_summary: str | None) -> str | None`, `is_phase_0000_prompt(prompt_path: Path) -> bool`, `is_phase_0000_interview_pending(repo_root: Path, prompt_path: Path) -> bool`).
   - Add `brief_path` (`self.repo_root / "docs" / "BRIEF.md"`) and `phase_0000_questions_file` (`self.daemon_dir / "phase0000_questions.md"`) properties to `RepoConfig` in `src/orchestack/config.py`.

2. **Phase 0000 Execution & Iterative PR Comment Interview Loop (`scripts/daemon_runner.py` & `src/orchestack/cli.py`)**
   - **Automatic Phase 0000 Kickoff when Queue is Empty**:
     - In `scripts/daemon_runner.py` (`get_next_prompt()` / `main()`), when no `0001+` prompts exist in `prompts/queued/` or `prompts/implemented/`:
       - Detect the repository state via `detect_repo_bootstrap_state(REPO_ROOT)`.
       - If the state is `BRIEF_ONLY` or `EMPTY_REPO` (and `0000-bootstrap-plan-and-prompts.md` is not already in `prompts/implemented/`), call `ensure_base_branch_initialized()` and `scaffold_phase_0000_prompt()` so `prompts/queued/0000-bootstrap-plan-and-prompts.md` is selected and launched as Phase 0000.
   - **Single-Turn Pause for User Interview in `execute_prompt_session()`**:
     - During Phase 0000 (`is_phase_0000_prompt(prompt_path)`), if the agent turn exits with `0000-bootstrap-plan-and-prompts.md` still in `prompts/queued/` AND `is_phase_0000_interview_pending()` is `True` (i.e., skeleton files `docs/KICKOFF.md`, `docs/PROTOCOL.md`, `docs/PLAN.md`, `docs/PR_DESCRIPTION.md` exist, `docs/BRIEF.md` exists for `EMPTY_REPO`, and numbered questions are ready in `.daemon/phase0000_questions.md` or the agent summary):
       - Do **not** auto-continue the session in a tight local loop waiting for `prompts/implemented/0000-...`; instead, return `True` from `execute_prompt_session()` so the daemon commits the skeleton/updates, opens or updates the PR, posts the numbered questions comment, and waits for the user's PR comment reply.
   - **Initial PR Description & First Numbered Questions Comment (`ensure_pr_opened()`)**:
     - When opening the initial Phase 0000 PR, use `docs/PR_DESCRIPTION.md` (containing the summary of what the session understood it is trying to build) as the PR description, and update the PR body on subsequent interview iterations if `docs/PR_DESCRIPTION.md` changed.
     - Immediately after opening the Phase 0000 PR (if `is_phase_0000_interview_pending()`), post a PR comment containing the agent's numbered list of questions so the user is prompted right away on the PR thread.
   - **Multi-Iteration Interview via PR Comments (`wait_for_pr_merge()`, `address_pr_feedback()`, `build_review_feedback_prompt()`)**:
     - Enhance `build_review_feedback_prompt()` (or add `build_phase_0000_interview_prompt()`) so that when `is_phase_0000_prompt(prompt_path)` is `True` and `0000-...` is still in `prompts/queued/`, user PR comments are framed as **Phase 0000 Interview Answers**:
       - Instruct the agent to incorporate the user's answers into `docs/BRIEF.md` (for `EMPTY_REPO` or if refined in `BRIEF_ONLY`), `docs/PLAN.md`, and `docs/PR_DESCRIPTION.md` (updating the summary of understanding).
       - Instruct the agent to **assume nothing**: if further clarification is needed, write the next numbered list of questions (`1. ...`, `2. ...`) to `.daemon/phase0000_questions.md` and keep `0000-...` in `prompts/queued/`; otherwise, if all questions are answered and the user's intent is clear, generate all `prompts/queued/0001-*.md` ... `NNNN-*.md` prompts, complete `docs/PLAN.md`, move `0000-bootstrap-plan-and-prompts.md` to `prompts/implemented/`, and author `docs/learnings/0000-bootstrap-plan-and-prompts-learnings.md`.
     - In `address_pr_feedback()`, when handling a Phase 0000 interview turn:
       - Update the PR description on the forge if `docs/PR_DESCRIPTION.md` was updated.
       - Post the new numbered list of questions (when still interviewing) or the completion summary (when finalized and `0000-...` moved to `prompts/implemented/`) as a new PR comment.
     - In `wait_for_pr_merge()`:
       - **Do not auto-merge** a Phase 0000 PR while the interview is still in progress (`0000-bootstrap-plan-and-prompts.md` is still in `prompts/queued/` or no `0001+` prompts have been queued yet), even if CI checks pass and no new user comments have arrived yet—continue polling until the user replies and the agent completes the interview (`0000-...` moved to `prompts/implemented/` and `0001+` prompts generated).
       - Exempt active Phase 0000 interview turns from tripping the `max_auto_heal_attempts` review feedback circuit breaker so the agent and user can exchange as many question/answer iterations as needed.
   - **Status Reporting (`src/orchestack/cli.py`)**:
     - Include `"bootstrap_state": detect_repo_bootstrap_state(config.repo_root).value` in `collect_repo_status()` output and display it in `orchestack status`.

3. **Documentation Updates (`docs/PLAN.md`)**
   - Update `docs/PLAN.md` (§2 `D13`, §8, §10, §13) to document the 3 repository states (`QUEUE_READY`, `BRIEF_ONLY`, `EMPTY_REPO`) and the Phase 0000 PR-comment interview workflow, and mark Phase `0008` as `Implemented` in §13 when completing this phase.

4. **Unit Tests (`tests/test_bootstrap_phase0000.py`)**
   - Create `tests/test_bootstrap_phase0000.py` (with the SPDX license header from `docs/LICENSE-HEADER.md`) thoroughly testing:
     - `detect_repo_bootstrap_state()` across all 3 states:
       1. Repo with existing `prompts/queued/0001-*.md` or `prompts/implemented/0001-*.md` -> `QUEUE_READY`.
       2. Repo with only `docs/BRIEF.md` -> `BRIEF_ONLY`.
       3. Empty repo (no `docs/BRIEF.md`, no prompts) -> `EMPTY_REPO`.
     - `scaffold_phase_0000_prompt()` for both `BRIEF_ONLY` and `EMPTY_REPO` states (verifying `EMPTY_REPO` requires building `docs/BRIEF.md` in addition to the skeleton repo `docs/KICKOFF.md`, `docs/PLAN.md`, `docs/PROTOCOL.md`, etc.).
     - `execute_prompt_session()` and `ensure_pr_opened()` during Phase 0000:
       - Verifying that when the initial turn scaffolds the skeleton repo, writes `docs/PR_DESCRIPTION.md` (summary of understanding), and produces a numbered list of questions while leaving `0000-...` in `prompts/queued/`, the daemon commits the skeleton, opens the PR with the understanding summary in the PR description, and posts the numbered list of questions as a PR comment.
     - Multi-turn PR comment interview loop (`address_pr_feedback()` / `wait_for_pr_merge()`):
       - Iteration 1: User posts a PR comment answering the numbered questions -> daemon resumes Phase 0000 session -> agent updates `docs/BRIEF.md` / `docs/PLAN.md` / `docs/PR_DESCRIPTION.md` and asks follow-up numbered questions -> daemon posts a new PR comment with the follow-up numbered questions without tripping the review circuit breaker (even beyond `max_auto_heal_attempts` iterations).
       - Final Iteration: User answers the remaining questions -> agent is satisfied, finalizes `docs/BRIEF.md` and `docs/PLAN.md`, creates `prompts/queued/0001-*.md` ..., moves `0000-bootstrap-plan-and-prompts.md` to `prompts/implemented/`, writes `docs/learnings/0000-bootstrap-plan-and-prompts-learnings.md`, and passes `check_definition_of_done()`.
     - All 82 baseline unit tests in `scripts/tests/test_daemon_runner.py` continue passing without modification (`B1`–`B8`).

## Acceptance Criteria
- [ ] Orchestack accurately detects all 3 repository states (`QUEUE_READY`, `BRIEF_ONLY`, `EMPTY_REPO`).
- [ ] In `BRIEF_ONLY` state, Orchestack launches Phase 0000, builds the skeleton repo (`docs/KICKOFF.md`, `docs/PLAN.md`, `docs/PROTOCOL.md`, etc.), opens an initial PR whose description summarizes what it understood it is trying to build, and interviews the user via numbered lists of questions in PR comments until satisfied to produce `docs/PLAN.md` and all `prompts/queued/NNNN-*.md` prompts.
- [ ] In `EMPTY_REPO` state, Orchestack follows the same Phase 0000 PR-comment interview loop while also building `docs/BRIEF.md` through the interviews with the user.
- [ ] All 82 baseline unit tests in `scripts/tests/test_daemon_runner.py` pass without modification (`B1`–`B8`).
- [ ] New unit tests in `tests/test_bootstrap_phase0000.py` pass under `pytest`.
- [ ] `make fmt && make check` passes with zero errors (`ruff check`, `ruff format --check`, `mypy --strict src tests`, `pytest`, and `tests/docs/test_indexes.py`).
- [ ] `docs/PLAN.md` §13 status for `0008` is updated to `Implemented`, `docs/learnings/0008-phase-0000-bootstrap-and-pr-interview-learnings.md` is created, and `docs/PR_DESCRIPTION.md` is written.

# 0001 — Core package and RepoConfig — Learnings

## Summary
- **What shipped**: Created `src/orchestack/config.py` with `RepoConfig`, `.orchestack.toml` loader, base branch and test-runner auto-detection, precedence resolution (`CLI flags > ORHCESTACK_* env vars > .orchestack.toml > auto-detected defaults`), and dynamic runtime configuration in `scripts/daemon_runner.py`.
- **Key packages/files**: `src/orchestack/config.py`, `src/orchestack/__init__.py`, `scripts/daemon_runner.py`, `tests/test_config.py`, `docs/PLAN.md`.
- **Key interfaces/types added or changed**: `RepoConfig`, `load_repo_config()`, `detect_base_branch()`, `detect_check_cmd()`, `configure_runtime()`, updated `check_definition_of_done(prompt_path, config=None)`, `ensure_clean_main()`, and CLI flags on `daemon_runner.main()`.
- **Decisions made/affected**: D1, D2, D3, D10, D11.
- **Gotchas / non-obvious constraints**: Existing unit tests in `scripts/tests/test_daemon_runner.py` monkeypatch module-level globals (`REPO_ROOT`, `PROMPTS_QUEUED`, `PROMPTS_IMPLEMENTED`, `STATE_FILE`, `DOCS_LEARNINGS`, `PLAN_FILE`, `PR_DESCRIPTION_FILE`) directly without calling `configure_runtime()`. Therefore, `check_definition_of_done()` and git/branch helpers must read those module-level path globals when `config` is not explicitly passed, while falling back to `_ACTIVE_CONFIG` for behavioral toggles (`base_branch`, `branch_prefix`, `check_cmd`, `require_learnings`, `require_pr_description`, `require_plan_update`). Also, avoid modifying `pyproject.toml` unless required because `uv run` without `UV_NO_SYNC=1` will attempt to re-lock dependencies over the network.
- **What the NEXT session must know**: Proceed with `prompts/queued/0002-forge-provider-protocol-and-github.md`. Define `ForgeProvider` and its data models (`PullRequestInfo`, `ReviewComment`, `ReviewSummary`, `CICheckStatus`, `FailedCIRun`, `MergeResult`) in `src/orchestack/forge/` and extract `GitHubForge` while keeping `scripts/daemon_runner.py` functions as backward-compatible wrappers.

## Details

### Architecture & Backward Compatibility
1. **Configuration Precedence (`load_repo_config`)**:
   - Resolves `repo_root` first (`--repo` > `ORCHESTACK_REPO` > `Path.cwd()`).
   - Parses optional `<repo_root>/.orchestack.toml` via Python 3.11+ standard library `tomllib` across `[repo]`, `[forge]`, `[agent]`, `[dod]`, `[daemon]`, and `[security]` tables.
   - Applies `ORCHESTACK_*` environment variables (`ORCHESTACK_BASE_BRANCH`, `ORCHESTACK_BRANCH_PREFIX`, `ORCHESTACK_FORGE`, `ORCHESTACK_AGENT`, `ORCHESTACK_CHECK_CMD`, `ORCHESTACK_AUTO_WHITELIST`), then explicit CLI overrides (`cli_overrides`).
   - Auto-detects `base_branch` via `git symbolic-ref refs/remotes/origin/HEAD` (falling back to `"main"`) and `check_cmd` from repository marker files (`Makefile` with `check:` target, `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`) when not explicitly configured.

2. **Dynamic Runtime Globals (`configure_runtime`)**:
   - `scripts/daemon_runner.py` keeps its module-level path globals (`REPO_ROOT`, `PROMPTS_DIR`, `PROMPTS_QUEUED`, `PROMPTS_IMPLEMENTED`, `DOCS_LEARNINGS`, `PLAN_FILE`, `PR_DESCRIPTION_FILE`, `STATE_FILE`) initialized to the repository root at import time so all 82 baseline tests work out of the box.
   - Calling `configure_runtime(config)` updates `_ACTIVE_CONFIG` and re-points all module-level path globals to `config.repo_root.resolve()`.
   - Branch helpers (`update_main`, `is_prompt_merged_into_origin_main`, `setup_prompt_branch`, `ensure_pr_opened`, `find_prompt_path_for_branch`, `count_new_commits`, `get_in_progress_prompt_branches`, `get_unmerged_implemented_prompts`, `reconcile_startup_branch`) dynamically respect `base_branch` and `branch_prefix` while preserving `-impl` suffix handling and `agy/`/`claude/`/`codex/` branch prefix stripping (`B3`).

3. **Configurable Definition of Done (`check_definition_of_done`)**:
   - Honors `require_learnings`, `require_plan_update`, `require_pr_description`, and `check_cmd` (running via `shlex.split(check_cmd)` with a 600-second timeout, or skipping the test command when `check_cmd == ""`).

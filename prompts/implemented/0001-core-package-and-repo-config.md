# 0001 — Core Package & Target Repository Configuration (`RepoConfig`)

## Read first
- `docs/PROTOCOL.md` §1, §3, §3.1 (`B1`–`B8`), §4, §5
- `docs/PLAN.md` §2 (`D1`, `D2`, `D3`, `D10`, `D11`), §3, §4, §8, §11
- `docs/learnings/0000-baseline-import-from-bth-learnings.md`
- `scripts/daemon_runner.py`
- `scripts/tests/test_daemon_runner.py`

## Objective
Decouple Orchestack from hardcoded repository-root globals so it can target **any git repository** passed via `--repo /path/to/repo` (or current working directory) and configured via `.orchestack.toml`, while keeping `scripts/daemon_runner.py` and all 82 baseline unit tests in `scripts/tests/test_daemon_runner.py` 100% green.

## Deliverables
1. **`src/orchestack/config.py`**:
   - Implement `RepoConfig` dataclass encapsulating:
     - `repo_root: Path` (resolved target git repository path)
     - `base_branch: str` (default `"main"`, or auto-detected via `git symbolic-ref refs/remotes/origin/HEAD`)
     - `branch_prefix: str` (default `"prompt"`)
     - `queued_dir: Path`, `implemented_dir: Path`, `learnings_dir: Path`, `pr_description_path: Path`, `daemon_dir: Path`
     - `check_cmd: str` (default `"make check"`, or auto-detected from repository files: `Makefile` with `check:` target -> `"make check"`, `pyproject.toml` -> `"pytest"`, `package.json` -> `"npm test"`, `Cargo.toml` -> `"cargo test"`)
     - `require_learnings: bool` (default `True`)
     - `require_pr_description: bool` (default `True`)
     - `max_auto_heal_attempts: int` (default `3`)
     - `forge_provider: str` (`"auto"`, `"github"`, `"gitlab"`)
     - `agent_backend: str` (`"agy"`, `"claude"`, `"codex"`)
     - `auto_whitelist: bool` (default `False`)
   - Implement `load_repo_config(repo_path: Path | None = None, cli_overrides: dict[str, Any] | None = None) -> RepoConfig`:
     - Reads `<repo_root>/.orchestack.toml` using Python 3.11+ `tomllib` if present.
     - Applies environment variable overrides (`ORCHESTACK_REPO`, `ORCHESTACK_BASE_BRANCH`, `ORCHESTACK_CHECK_CMD`, `ORCHESTACK_FORGE`, `ORCHESTACK_AGENT`).
     - Applies CLI flag overrides (`--repo`, `--base-branch`, `--check-cmd`, `--forge`, `--agent`, `--no-require-learnings`, `--no-require-pr-description`).
2. **Dynamic Path & Branch Binding in `scripts/daemon_runner.py`**:
   - Add `configure_runtime(config: RepoConfig) -> None` in `scripts/daemon_runner.py` (and `src/orchestack/config.py`) that updates module-level path globals (`REPO_ROOT`, `QUEUED_DIR`, `IMPLEMENTED_DIR`, `LEARNINGS_DIR`, `PR_DESC_PATH`, `DAEMON_DIR`, `LOCK_FILE`, `REVIEW_STATE_FILE`) when `--repo` is provided, while preserving direct monkeypatching of `daemon_runner.REPO_ROOT` etc. in existing unit tests (`D11`).
   - Update `check_definition_of_done()` to respect `config.check_cmd`, `config.require_learnings`, and `config.require_pr_description` (`D10`).
   - Update `is_prompt_merged_into_origin_main()`, `ensure_clean_main()`, and git helpers to support configurable `base_branch` and `branch_prefix` while defaulting to `"main"` and `"prompt"`.
   - Add `--repo`, `--base-branch`, `--check-cmd`, `--forge`, and `--agent` CLI arguments to `daemon_runner.py` argument parser.
3. **Unit Tests (`tests/test_config.py`)**:
   - Test `.orchestack.toml` parsing, default auto-detection of `check_cmd` and `base_branch`, environment variable precedence, CLI overrides, and configurable `check_definition_of_done()`.
   - Verify all 82 baseline tests in `scripts/tests/test_daemon_runner.py` continue to pass without modification.

## Acceptance Criteria
- `make check` passes (`ruff check`, `ruff format --check`, `mypy`, `pytest`).
- All 82 baseline tests in `scripts/tests/test_daemon_runner.py` pass.
- Documentation index tests in `tests/docs/test_indexes.py` pass.
- `docs/PLAN.md` §13 status for `0001` is updated to `Implemented`, and `docs/learnings/0001-core-package-and-repo-config-learnings.md` + `docs/PR_DESCRIPTION.md` are created.

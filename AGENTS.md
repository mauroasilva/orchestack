# Orchestack Workspace Rules

## 1. Session & Phase Execution Protocol
- Before starting any phase in `prompts/queued/`, read `docs/PROTOCOL.md`, `docs/PLAN.md`, and the `## Summary` block of every file in `docs/learnings/`.
- Preserve all Mandatory Regression Invariants (`B1`–`B8` in `docs/PROTOCOL.md` §3.1). Never delete, modify, or weaken existing unit tests in `scripts/tests/test_daemon_runner.py`.
- Every new Python source file under `src/` and `tests/` must begin with the SPDX license header from `docs/LICENSE-HEADER.md`.

## 2. Verification & Sandbox Guidelines
- Run `make fmt && make check` (`UV_NO_SYNC=1` is set in `Makefile`) to verify `ruff check`, `ruff format --check`, `mypy --strict src tests`, `pytest`, and `tests/docs/test_indexes.py`.
- Avoid modifying `pyproject.toml` unless explicitly required by a phase prompt, as changing `pyproject.toml` causes `uv run` without `UV_NO_SYNC=1` to attempt network dependency resolution in the sandbox.
- For file moves inside the workspace, prefer `mv` over `git mv` inside the standard sandbox, and run `git add` / `git commit` (using `BypassSandbox: true` if `.git/index.lock` is blocked by the sandbox) as a dedicated step.

## 3. Mandatory Git Commit Before Concluding a Phase
- Before concluding any phase implementation turn, you **MUST** stage and commit all deliverables for that phase:
  - Stage `src/`, `tests/`, `scripts/`, `docs/PLAN.md`, `docs/learnings/NNNN-*-learnings.md`, and the moved prompt in `prompts/implemented/NNNN-*.md` (plus removal from `prompts/queued/NNNN-*.md`).
  - Leave `docs/PR_DESCRIPTION.md` untracked on disk (it is gitignored by design per `B7` for PR body generation).
  - Commit using Conventional Commits format: `feat(<scope>): <summary> (NNNN)`.
  - Verify `git status --short` is clean before reporting completion to the user.

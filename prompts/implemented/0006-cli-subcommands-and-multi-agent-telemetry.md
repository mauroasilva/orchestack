# 0006 — `orchestack` CLI Subcommands & Multi-Agent Telemetry

## Read first
- `docs/PROTOCOL.md` §1, §3, §3.1 (`B1`–`B8`), §4, §5
- `docs/PLAN.md` §2 (`D11`, `D12`), §3, §9, §10, §12
- `docs/learnings/0000-baseline-import-from-bth-learnings.md`
- `src/orchestack/cli.py`
- `scripts/session_telemetry_report.py`
- `scripts/run_daemon.sh`

## Objective
Complete the standalone `orchestack` CLI experience (`run`, `init`, `status`, `doctor`, `telemetry`) and generalize `scripts/session_telemetry_report.py` into a multi-agent telemetry engine (`src/orchestack/telemetry/`) supporting Antigravity, Claude Code, and Codex sessions.

## Deliverables
1. **`src/orchestack/cli.py` Subcommands**:
   - `orchestack run [--repo PATH] [--forge auto|github|gitlab] [--agent agy|claude|codex] [--once] [--interval SEC] [--auto-whitelist] [--dry-run]`:
     - Launches the Orchestack daemon against the target repository.
   - `orchestack init [--repo PATH] [--forge auto|github|gitlab] [--agent agy|claude|codex] [--check-cmd CMD]`:
     - Scaffolds `.orchestack.toml`, `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`, and ensures `.daemon/` is in `.gitignore`.
   - `orchestack status [--repo PATH] [--json]`:
     - Reports target repo path, detected forge & agent backend, active daemon lock state, queued prompt list, implemented prompt count, and open PRs/MRs with CI/review status.
   - `orchestack doctor [--repo PATH]`:
     - Runs diagnostic checks: valid git repository & `origin` remote, forge CLI installed & authenticated (`gh auth status` or `glab auth status`), selected agent CLI installed (`agy`, `claude`, or `codex`), and `.orchestack.toml` syntax validation.
   - `orchestack telemetry [--repo PATH] [--format markdown|json] [--output PATH]`:
     - Generates unified session telemetry across Antigravity, Claude Code, and Codex sessions.
   - Preserve backward compatibility when legacy flags (`orchestack --once`, `python3 scripts/daemon_runner.py --once`) are invoked without an explicit subcommand (`D11`).
2. **Multi-Agent Telemetry (`src/orchestack/telemetry/`)**:
   - Extend `scripts/session_telemetry_report.py` into `src/orchestack/telemetry/reporter.py`:
     - Parse Antigravity telemetry (`state.vscdb`, conversation logs, `.daemon/logs/`).
     - Parse Claude Code telemetry (`~/.claude/projects/**/*.jsonl` and `.daemon/logs/` stream-json `result` events for `total_cost_usd`, `duration_ms`, `num_turns`, `usage.input_tokens`, `usage.cache_read_input_tokens`, `usage.output_tokens`).
     - Parse Codex telemetry (`~/.codex/sessions/**/rollout-*.jsonl` and `.daemon/logs/` `turn.completed` events for `usage.input_tokens`, `usage.cached_input_tokens`, `usage.output_tokens`).
     - Output unified Markdown and JSON reports.
3. **Update `scripts/run_daemon.sh`**:
   - Support `--repo`, `--forge`, and `--agent` passthrough flags in `scripts/run_daemon.sh` while keeping all existing flags (`--once`, `--auto-whitelist`, `--interval`, `--help`) working.
4. **Unit Tests (`tests/test_cli.py`, `tests/test_telemetry.py`)**:
   - Test `orchestack init`, `orchestack status`, `orchestack doctor`, `orchestack run --once`, and `orchestack telemetry` across mocked Antigravity, Claude Code, and Codex session logs.
   - Verify all 82 baseline tests in `scripts/tests/test_daemon_runner.py` pass.

## Acceptance Criteria
- `make check` passes (`ruff check`, `ruff format --check`, `mypy`, `pytest`).
- All 82 baseline tests + all package unit tests + `tests/docs/test_indexes.py` pass.
- `docs/PLAN.md` §13 status for `0006` is updated to `Implemented`, and `docs/learnings/0006-cli-subcommands-and-multi-agent-telemetry-learnings.md` + `docs/PR_DESCRIPTION.md` are created.

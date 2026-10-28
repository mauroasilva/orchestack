# 0006 — CLI Subcommands and Multi-Agent Telemetry Learnings

## Summary

- Implemented the `orchestack` Typer CLI surface in `src/orchestack/cli.py` with subcommands `run`, `init`, `status`, `doctor`, `telemetry`, and `version`, while preserving 100% legacy root-flag compatibility (`orchestack --once`, etc. per `D11`) via a root `@app.callback(invoke_without_command=True)` handler.
- Implemented multi-agent session telemetry in `src/orchestack/telemetry/reporter.py` (and exposed in `src/orchestack/telemetry/__init__.py` and `scripts/session_telemetry_report.py`) to parse Antigravity (`state.vscdb`, `conversation_summaries.db`, `transcript*.jsonl`), Claude Code (`~/.claude/projects/**/*.jsonl` and `.daemon/logs/` stream-json `result` events), and Codex (`~/.codex/sessions/**/rollout-*.jsonl` and `.daemon/logs/` `turn.completed` events) into unified `SessionTelemetry` records with Markdown and JSON formatting.
- Updated `scripts/run_daemon.sh` and `scripts/daemon_runner.py` to support `--repo`, `--forge`, `--agent`, `--once`, `--auto-whitelist`, `--interval` (alias for `--poll-interval`), and `--help` passthrough cleanly across macOS Bash 3.2+.
- Added 12 unit tests across `tests/test_cli.py` and `tests/test_telemetry.py` covering all CLI subcommands, legacy flag invocation, and multi-agent telemetry extraction without network or live subprocess calls.

## Key Decisions

- **Root Callback Forwarding for `D11` Compatibility**: By configuring Typer with `@app.callback(invoke_without_command=True)` and mirroring the `daemon_runner.py` flags (`--repo`, `--forge`, `--agent`, `--once`, `--interval`, `--poll-interval`, `--auto-whitelist`, `--model`, `--effort`, `--resume`, `--fresh`, `--dry-run`) on both the root callback and `orchestack run`, existing invocations like `orchestack --once` route seamlessly to `run_daemon()` when `ctx.invoked_subcommand is None`.
- **Unified `SessionTelemetry` Dataclass with Per-Backend Parsers**: Each agent backend stores session telemetry in a distinct JSONL schema:
  - Antigravity stores step-level objects (`USER_INPUT`, `PLANNER_RESPONSE`, `tool_calls`) in `~/.gemini/antigravity*/brain/<uuid>/.system_generated/logs/transcript*.jsonl` with titles in `conversation_summaries.db` or `state.vscdb` (`ItemTable`).
  - Claude Code stores `stream-json` events in `~/.claude/projects/**/*.jsonl` with `type="result"` carrying `total_cost_usd`, `duration_ms`, `num_turns`, and `usage` (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`).
  - Codex stores JSONL rollout events in `~/.codex/sessions/**/rollout-*.jsonl` with `thread.started`, `item.completed` (`command_execution`), and `turn.completed` carrying `usage` (`input_tokens`, `cached_input_tokens`, `output_tokens`).
  - `.daemon/logs/*.jsonl` files in a target repository are inspected and auto-routed to the matching backend parser (`parse_daemon_log_file`), with deduplication by `(agent, session_id)`.
- **Self-Contained `orchestack init` Scaffolding**: `init_repository()` generates `.orchestack.toml`, `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`, starter `docs/PROTOCOL.md` and `docs/PLAN.md` files, and appends `.daemon/` and `docs/PR_DESCRIPTION.md` to `.gitignore` idempotently.

## Gotchas & Edge Cases

- **macOS Bash 3.2 Empty Array Expansion with `set -u`**: In `scripts/run_daemon.sh`, expanding `"${FORWARD_ARGS[@]}"` when `FORWARD_ARGS=()` is empty raises an unbound variable error under Bash 3.2 with `set -u`. Using `${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}` safely expands to zero arguments when the array is empty.
- **Isolating Home-Directory Discovery in Unit Tests**: `collect_multi_agent_telemetry()` accepts explicit path overrides (`antigravity_brain_dirs`, `antigravity_summaries_dbs`, `antigravity_state_vscdbs`, `claude_projects_dir`, `codex_sessions_dir`, `daemon_logs_dir`) so unit tests can run deterministically in `tmp_path` without reading the developer's live `~/.gemini`, `~/.claude`, or `~/.codex` directories.

## Verification

- Executed `make fmt && make check` (`ruff check`, `ruff format --check`, `mypy --strict src tests`, `pytest`, and `tests/docs/test_indexes.py`).
- All 82 baseline tests in `scripts/tests/test_daemon_runner.py` and 109 package/docs tests in `tests/` (191 total tests) pass.

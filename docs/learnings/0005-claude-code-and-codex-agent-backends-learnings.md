# 0005 — ClaudeCodeAgent (`claude`) & CodexAgent (`codex`) Backends Learnings

## Summary
- **What shipped**: Implemented `ClaudeCodeAgent` (`src/orchestack/agents/claude_code.py`, re-exported in `src/orchestack/agents/claude.py`), `CodexAgent` (`src/orchestack/agents/codex.py`), and `create_agent_backend()` (`src/orchestack/agents/factory.py`), integrated `AgentBackend` into `scripts/daemon_runner.py` (`execute_prompt_session`, `address_pr_feedback`, `address_ci_failure`), added `agent_backend` to `RepoConfig`, and added unit tests in `tests/test_agent_claude.py` and `tests/test_agent_codex.py`.
- **Key packages/files**: `src/orchestack/agents/claude_code.py`, `src/orchestack/agents/claude.py`, `src/orchestack/agents/codex.py`, `src/orchestack/agents/factory.py`, `src/orchestack/agents/__init__.py`, `src/orchestack/config.py`, `scripts/daemon_runner.py`, `tests/test_agent_claude.py`, `tests/test_agent_codex.py`.
- **Key interfaces/types added or changed**: `ClaudeCodeAgent`, `CodexAgent`, `create_agent_backend`, `normalize_agent_backend_name`, `RepoConfig.agent_backend`.
- **Gotchas / non-obvious constraints**: `scripts/tests/test_daemon_runner.py` mocks internal Antigravity helpers (`run_session_process`, `get_latest_conversation_id`, `check_recent_soft_denial_in_cli_log`) directly on `daemon_runner`, so `execute_prompt_session()` keeps the legacy Antigravity loop when `agent_backend` is `None` (or `AntigravityAgent`) and dispatches to `_execute_backend_prompt_session()` when `agent_backend` (or `ACTIVE_CONFIG.agent_backend`) is `ClaudeCodeAgent` or `CodexAgent`.
- **What the NEXT session must know**: Phase `0006` (`cli-subcommands-and-multi-agent-telemetry`) can import `create_agent_backend`, `AntigravityAgent`, `ClaudeCodeAgent`, and `CodexAgent` directly from `orchestack.agents` for `orchestack run`, `orchestack doctor`, and multi-agent telemetry extraction.

## Details

### 1. `ClaudeCodeAgent` (`src/orchestack/agents/claude_code.py`)
- **Kickoff & Resume**:
  - Kickoff: `claude -p <prompt> --output-format stream-json --verbose [--model <model>] [--allowedTools ...]`.
  - Resume: `claude -p <prompt> --resume <session_id> --output-format stream-json --verbose` (falls back to `--continue` if `session_id` is not yet known). Never passes `--dangerously-skip-permissions` (`B6`).
- **Stream & Session Parsing (`B1`, `B2`)**:
  - Parses JSONL events from stdout or `cli_log_path` starting at `cli_log_offset`.
  - Extracts `session_id` from `{"type": "system", "subtype": "init"}` or `{"type": "result"}` events, falling back to `~/.claude/projects/<project-slug>/<session_id>.jsonl` filtered by `since_timestamp` (`B1`).
  - Tracks live heartbeats from `{"type": "assistant"}` tool-use blocks (`Bash`, `Edit`, `Write`, `Read`, `WebFetch`).
  - Extracts final summary text and status from `{"type": "result", "subtype": "success" | "error_max_turns" | "error_during_execution"}` or the project JSONL transcript.
- **Permission Denial & Whitelisting (`B2`, `D8`)**:
  - Detects `permission_denials` arrays in result events and `"Permission to use ... has been denied"` / `"requires approval"` messages scoped to `cli_log_offset` and `turn_start_time`.
  - `whitelist_denial()` updates `<repo_root>/.claude/settings.local.json` (`permissions.allow` adding `"Bash(<prefix>:*)"`, `"WebFetch"`, or `"Edit"`/`"Write"`) and appends rules to `self.allowed_tools` so `--allowedTools` is passed on the resumed turn.

### 2. `CodexAgent` (`src/orchestack/agents/codex.py`)
- **Kickoff & Resume**:
  - Kickoff: `codex exec <prompt> --json -C <repo_root> --full-auto` (or `--sandbox <mode>` and `--add-dir <dir>`).
  - Resume: `codex exec resume <session_id> <prompt> --json`. Never passes `--dangerously-bypass-approvals-and-sandbox` (`B6`).
- **Stream & Session Parsing (`B1`, `B2`)**:
  - Parses JSONL events from stdout or `cli_log_path` starting at `cli_log_offset`.
  - Extracts `thread_id` (`session_id`) from `{"type": "thread.started"}` or `~/.codex/sessions/**/rollout-*.jsonl` filtered by `since_timestamp` (`B1`).
  - Tracks live heartbeats from `item.started` / `item.completed` (`command_execution`, `file_change`, `agent_message`).
  - Extracts the final agent response from the last `agent_message` item before `turn.completed` (or fallback rollout file).
- **Permission Denial & Whitelisting (`B2`, `D8`)**:
  - Detects `sandbox_error`, `approval_required`, and `Operation not permitted` on command executions scoped to `cli_log_offset` and `turn_start_time`.
  - `whitelist_denial()` updates `<repo_root>/.codex/config.toml` and appends `--add-dir` / sandbox settings for the resumed session.

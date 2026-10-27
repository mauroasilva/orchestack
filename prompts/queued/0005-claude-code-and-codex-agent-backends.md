# 0005 — `ClaudeCodeAgent` (`claude`) & `CodexAgent` (`codex`) Backends

## Read first
- `docs/PROTOCOL.md` §1, §3, §3.1 (`B1`, `B2`, `B4`, `B6`), §4, §5
- `docs/PLAN.md` §2 (`D6`, `D7`, `D8`), §3, §6, §7, §8
- `docs/learnings/0000-baseline-import-from-bth-learnings.md`
- `src/orchestack/agents/`
- `scripts/daemon_runner.py`

## Objective
Implement first-class `AgentBackend` drivers for **Anthropic Claude Code** (`claude` CLI) and **OpenAI Codex** (`codex` CLI) with full parity for session kickoff, multi-turn session resume (`--resume` / `exec resume`), streaming JSONL heartbeat monitoring, final response extraction, turn-scoped permission denial detection, and automated tool/sandbox whitelisting.

## Deliverables
1. **`src/orchestack/agents/claude_code.py` (`ClaudeCodeAgent`)**:
   - **Kickoff & Resume**:
     - New session: `claude -p <prompt> --output-format stream-json --verbose` (plus optional `--model` and configured `--allowedTools`).
     - Resume session: `claude -p <prompt> --resume <session_id> --output-format stream-json --verbose`.
   - **Stream & Session Parsing**:
     - Parse JSONL lines from stdout / `cli_log_path` starting at `cli_log_offset` (`B2`).
     - Extract `session_id` from `{"type": "system", "subtype": "init", "session_id": "..."}` or `{"type": "result", "session_id": "..."}`.
     - Track live activity heartbeat from `{"type": "assistant"}` tool-use blocks (`Bash`, `Edit`, `Write`, `Read`, `WebFetch`).
     - Extract final summary text and completion status from `{"type": "result", "subtype": "success" | "error_max_turns" | "error_during_execution", "result": "..."}` or fallback to `< ~/.claude/projects/<project-slug>/<session_id>.jsonl`.
   - **Turn-Scoped Permission Denial & Whitelisting (`B2`, `D8`)**:
     - Detect permission denial events in JSONL tool results / stderr since `cli_log_offset` and `turn_start_time` (`permission_denials` array in result event or `"Permission to use ... has been denied"` / `"requires approval"`).
     - Implement `whitelist_denial()` updating `<repo_root>/.claude/settings.local.json` (`permissions.allow` adding `"Bash(<prefix>:*)"` or `"WebFetch"`), and passing `--allowedTools` on the resumed turn.
2. **`src/orchestack/agents/codex.py` (`CodexAgent`)**:
   - **Kickoff & Resume**:
     - New session: `codex exec <prompt> --json -C <repo_root>` (with `--full-auto` or configured sandbox mode).
     - Resume session: `codex exec resume <session_id> <prompt> --json`.
   - **Stream & Session Parsing**:
     - Parse JSONL events from stdout / `cli_log_path` starting at `cli_log_offset` (`B2`).
     - Extract `session_id` (`thread_id`) from `{"type": "thread.started", "thread_id": "..."}` or `~/.codex/sessions/**/rollout-*.jsonl` filtered by `since_timestamp` (`B1`).
     - Track live heartbeat from `item.started` / `item.completed` (`command_execution`, `file_change`, `agent_message`).
     - Extract final agent response from the last `agent_message` item before `turn.completed`.
   - **Turn-Scoped Permission Denial & Whitelisting (`B2`, `D8`)**:
     - Detect sandbox/approval denials (`sandbox_error`, `approval_required`, `Operation not permitted` on command execution) scoped to `cli_log_offset` and `turn_start_time`.
     - Implement `whitelist_denial()` updating `<repo_root>/.codex/config.toml` or passing appropriate sandbox / `--add-dir` flags on session resume.
3. **Agent Factory (`src/orchestack/agents/factory.py`)**:
   - Implement `create_agent_backend(config: RepoConfig) -> AgentBackend` selecting `AntigravityAgent`, `ClaudeCodeAgent`, or `CodexAgent` based on `config.agent_backend` (`--agent agy|claude|codex`).
   - Integrate `AgentBackend` into `execute_prompt_session()` and the PR/MR review fix loop so any of the three agents can execute queued prompts, DoD auto-heal loops, and PR/MR review fix cycles on both GitHub and GitLab.
4. **Unit Tests (`tests/test_agent_claude.py`, `tests/test_agent_codex.py`)**:
   - Test `ClaudeCodeAgent` kickoff/resume command construction, stream-json parsing, `session_id` extraction, turn-scoped permission denial detection, and `.claude/settings.local.json` whitelisting.
   - Test `CodexAgent` kickoff/resume command construction (`codex exec` / `codex exec resume`), JSONL event parsing, `thread_id` extraction, turn-scoped sandbox denial detection, and whitelisting.
   - Ensure all 82 baseline tests in `scripts/tests/test_daemon_runner.py` pass.

## Acceptance Criteria
- `make check` passes (`ruff check`, `ruff format --check`, `mypy`, `pytest`).
- All baseline and new unit tests pass.
- `docs/PLAN.md` §13 status for `0005` is updated to `Implemented`, and `docs/learnings/0005-claude-code-and-codex-agent-backends-learnings.md` + `docs/PR_DESCRIPTION.md` are created.

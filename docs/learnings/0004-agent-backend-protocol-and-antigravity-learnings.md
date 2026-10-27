# 0004 — AgentBackend Protocol & AntigravityAgent Extraction Learnings

## Summary
- **What shipped**: Defined the `@runtime_checkable` `AgentBackend` protocol and immutable dataclasses (`PermissionDenial`, `AgentSessionResult`, `HeartbeatInfo` / `HeartbeatSnapshot`) in `src/orchestack/agents/base.py`, extracted `AntigravityAgent` and all Antigravity session/transcript/permission helpers into `src/orchestack/agents/antigravity.py` preserving invariants `B1`, `B2`, and `B6`, wired `scripts/daemon_runner.py` to delegate to `orchestack.agents.antigravity`, and added unit tests in `tests/test_agent_antigravity.py`.
- **Key packages/files**: `src/orchestack/agents/base.py`, `src/orchestack/agents/antigravity.py`, `src/orchestack/agents/__init__.py`, `scripts/daemon_runner.py`, `tests/test_agent_antigravity.py`.
- **Key interfaces/types added or changed**: `AgentBackend`, `PermissionDenial`, `AgentSessionResult`, `HeartbeatInfo` (`HeartbeatSnapshot`), `AntigravityAgent`, `extract_command_prefix`, `whitelist_command_in_settings`, `whitelist_url_in_settings`, `extract_last_agent_response`.
- **Gotchas / non-obvious constraints**: `scripts/tests/test_daemon_runner.py` monkeypatches module-level globals on `daemon_runner` (`CLI_BRAIN_DIR`, `CLI_SUMMARIES_DB`, `CLI_LOG_FILE`, `SETTINGS_FILE`, `REPO_ROOT`), so `daemon_runner`'s wrapper functions must pass those current module-level values as explicit keyword arguments into `orchestack.agents.antigravity` helpers on every call. Also, `AntigravityAgent.build_command` uses `agy chat -m agent [-r <session_id>] [--model ...] [--effort ...] <prompt>` without `--project` (`B6`).
- **What the NEXT session must know**: Phase `0005` (`claude-code-and-codex-agent-backends`) should implement `ClaudeCodeAgent` (`src/orchestack/agents/claude.py`) and `CodexAgent` (`src/orchestack/agents/codex.py`) conforming to `AgentBackend` in `src/orchestack/agents/base.py`, and add a factory `create_agent_backend(agent_type: str, ...)` in `src/orchestack/agents/__init__.py`.

## Details

### Architecture & Protocol Design
1. **`AgentBackend` Protocol (`src/orchestack/agents/base.py`)**:
   - `@runtime_checkable class AgentBackend(Protocol)` defines a uniform interface across coding agent CLIs (`antigravity`, `claude`, `codex`):
     - `name: str`
     - `check_available() -> bool`
     - `build_command(prompt, *, session_id=None, model=None, effort=None) -> list[str]`
     - `run_turn(prompt, *, cwd, session_id=None, model=None, effort=None, log_file=None, timeout=None) -> AgentSessionResult`
     - `get_latest_session_id(since_timestamp=0.0) -> str | None`
     - `get_session_status(session_id, since_timestamp=0.0) -> tuple[int | str, str] | None`
     - `get_heartbeat(session_id, since_timestamp=0.0) -> HeartbeatInfo | None`
     - `extract_last_response(session_id=None) -> str | None`
     - `detect_permission_denials(full_output, *, session_id=None, since_timestamp=0.0, cli_log_offset=0) -> list[PermissionDenial]`
     - `whitelist_denial(denial) -> bool`
   - `HeartbeatInfo` is exported alongside its alias `HeartbeatSnapshot` so both `docs/PLAN.md` §7 and prompt `0004` references work interchangeably.

2. **`AntigravityAgent` & Regression Invariants (`src/orchestack/agents/antigravity.py`)**:
   - **B1 (Timestamp Normalization & Turn-Scoped Transcript Filtering)**: `_normalize_db_timestamp()` normalizes seconds, milliseconds (`> 1e11`), microseconds (`> 1e13`), nanoseconds (`> 1e16`), and ISO-8601 strings to POSIX float seconds. `get_latest_conversation_id()` and `get_session_transcript_status()` enforce `since_timestamp - 2.0` filtering and prefer a fresher `brain/` directory over a stale SQLite `conversation_summaries.db` entry.
   - **B2 (Turn-Scoped Permission Denial Detection & Multiline Command Extraction)**: `check_recent_soft_denial_in_cli_log(since_offset)` seeks to `since_offset` (or `0` if the log was truncated), `check_recent_transcript_denial(since_timestamp, conv_id)` inspects only recent steps from the current turn, `parse_denial_from_step()` extracts full multiline command blocks up to `Do not attempt to circumvent`, and `extract_command_prefix()` strips comments and `KEY=VALUE` env assignments before extracting the executable prefix.
   - **B6 (No Global IDE State Mutation)**: `AntigravityAgent.build_command()` constructs `["agy", "chat", "-m", "agent", ...]` using `-r <session_id>` for resumption and never passes `--project`.

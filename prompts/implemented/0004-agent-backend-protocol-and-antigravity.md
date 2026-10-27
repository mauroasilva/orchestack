# 0004 — `AgentBackend` Protocol & `AntigravityAgent` Extraction

## Read first
- `docs/PROTOCOL.md` §1, §3, §3.1 (`B1`, `B2`, `B6`), §4, §5
- `docs/PLAN.md` §2 (`D1`, `D6`, `D7`, `D8`, `D11`), §3, §6, §7
- `docs/learnings/0000-baseline-import-from-bth-learnings.md`
- `scripts/daemon_runner.py`
- `scripts/tests/test_daemon_runner.py`

## Objective
Define the `AgentBackend` protocol and normalized `PermissionDenial` / `AgentSessionResult` models in `src/orchestack/agents/` and extract the Google Antigravity (`agy`) implementation into `AntigravityAgent` while preserving every transcript timestamp, turn-scoped permission denial, multiline command extraction, and headless CLI isolation invariant (`B1`, `B2`, `B6`).

## Deliverables
1. **`src/orchestack/agents/base.py`**:
   - Define dataclasses:
     - `PermissionDenial(tool_name: str, resource: str, command_prefix: str | None = None, raw_reason: str = "")`
     - `AgentSessionResult(session_id: str | None, exit_code: int, completed_normally: bool, error_message: str | None, last_response: str, cli_log_offset: int, turn_start_time: float)`
     - `HeartbeatInfo(active: bool, summary: str, updated_at: float)`
   - Define `AgentBackend(Protocol)` with methods:
     - `name: str`
     - `check_available() -> tuple[bool, str]`
     - `run_turn(prompt: str, repo_root: Path, session_id: str | None = None, timeout: int = 1800, cli_log_path: Path | None = None) -> AgentSessionResult`
     - `get_latest_session_id(repo_root: Path, since_timestamp: float | None = None) -> str | None`
     - `get_session_status(session_id: str, since_timestamp: float | None = None) -> tuple[bool, str | None]`
     - `extract_last_response(session_id: str, cli_log_path: Path | None = None) -> str`
     - `detect_permission_denials(session_id: str | None, cli_log_path: Path | None, cli_log_offset: int = 0, turn_start_time: float | None = None) -> list[PermissionDenial]`
     - `whitelist_denial(denial: PermissionDenial, repo_root: Path) -> bool`
2. **`src/orchestack/agents/antigravity.py` (`AntigravityAgent`)**:
   - Extract Antigravity (`agy`) session execution, SQLite `state.vscdb` and protobuf/JSONL transcript inspection (`get_latest_conversation_id`, `get_session_transcript_status`, `extract_last_agent_response`, `_normalize_db_timestamp`), turn-scoped permission denial detection (`check_recent_transcript_denial`, `extract_command_prefix`, `whitelist_command_in_settings`, `whitelist_url_in_settings`), and heartbeat monitoring.
   - Strictly preserve invariants `B1` (`since_timestamp` filtering and `_normalize_db_timestamp`), `B2` (`cli_log_offset` and `turn_start_time` scoping, multiline command prefix extraction, `read_url` whitelisting), and `B6` (pure headless `agy chat -m agent [-r <id>] <prompt>` without `--project` or IDE GUI mutations).
3. **Wire `AntigravityAgent` into `scripts/daemon_runner.py`**:
   - Keep `daemon_runner.py` helper functions delegating cleanly so all 82 baseline tests in `scripts/tests/test_daemon_runner.py` continue to pass (`D11`).
4. **Unit Tests (`tests/test_agent_antigravity.py`)**:
   - Add unit tests verifying `AntigravityAgent` protocol compliance, turn-scoped denial detection, multiline command prefix extraction, and `since_timestamp` filtering.

## Acceptance Criteria
- `make check` passes (`ruff check`, `ruff format --check`, `mypy`, `pytest`).
- All 82 baseline tests in `scripts/tests/test_daemon_runner.py` + new tests in `tests/test_agent_antigravity.py` pass.
- `docs/PLAN.md` §13 status for `0004` is updated to `Implemented`, and `docs/learnings/0004-agent-backend-protocol-and-antigravity-learnings.md` + `docs/PR_DESCRIPTION.md` are created.

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for the AgentBackend protocol and AntigravityAgent implementation."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestack.agents import (
    AgentBackend,
    AgentSessionResult,
    AntigravityAgent,
    HeartbeatInfo,
    HeartbeatSnapshot,
    PermissionDenial,
    _normalize_db_timestamp,
    add_allowed_rules,
    check_recent_soft_denial_in_cli_log,
    check_recent_transcript_denial,
    extract_command_prefix,
    extract_denied_permission,
    extract_last_agent_response,
    extract_latest_agent_summary,
    extract_session_transcript_metrics,
    get_latest_conversation_id,
    get_session_transcript_status,
    initialize_baseline_permissions,
    is_transient_session_error,
    parse_denial_from_step,
    parse_iso_timestamp,
    whitelist_command_in_settings,
    whitelist_url_in_settings,
)


def _write_transcript(
    brain_dir: Path,
    conv_id: str,
    steps: list[dict[str, object]],
    *,
    filename: str = "transcript.jsonl",
    mtime: float | None = None,
) -> Path:
    logs_dir = brain_dir / conv_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = logs_dir / filename
    lines = [json.dumps(step) for step in steps]
    transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(transcript_file, (mtime, mtime))
        os.utime(brain_dir / conv_id, (mtime, mtime))
    return transcript_file


def test_antigravity_agent_satisfies_protocol() -> None:
    """AntigravityAgent satisfies the runtime-checkable AgentBackend protocol."""
    agent = AntigravityAgent()
    assert isinstance(agent, AgentBackend)
    assert agent.name == "antigravity"
    assert HeartbeatInfo is HeartbeatSnapshot


def test_check_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """check_available locates agy via shutil.which or extra paths."""
    agent = AntigravityAgent(agy_bin="agy", extra_bin_paths=[])
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/agy" if name == "agy" else None,
    )
    ok, path_or_reason = agent.check_available()
    assert ok is True
    assert path_or_reason == "/usr/local/bin/agy"

    monkeypatch.setattr("shutil.which", lambda _name: None)
    fake_bin = tmp_path / "agy"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    agent_with_path = AntigravityAgent(agy_bin="agy", extra_bin_paths=[tmp_path])
    ok2, path2 = agent_with_path.check_available()
    assert ok2 is True
    assert path2 == str(fake_bin)

    agent_missing = AntigravityAgent(
        agy_bin="nonexistent-agy",
        extra_bin_paths=[tmp_path],
    )
    ok3, reason3 = agent_missing.check_available()
    assert ok3 is False
    assert "not found" in reason3


def test_build_command_b6_compliance() -> None:
    """build_command uses `agy chat -m agent [-r <id>] <prompt>` without `--project` (B6)."""
    with patch(
        "orchestack.agents.antigravity.find_binary",
        return_value="/opt/bin/agy",
    ):
        agent = AntigravityAgent(agy_bin="/opt/bin/agy", model="", effort="")
        cmd_new = agent.build_command("Implement feature X")
        assert cmd_new == ["/opt/bin/agy", "chat", "-m", "agent", "Implement feature X"]
        assert "--project" not in cmd_new

        agent_configured = AntigravityAgent(
            agy_bin="/opt/bin/agy",
            model="gemini-3.8-flash-high",
            effort="high",
        )
        cmd_resume = agent_configured.build_command(
            "Continue feature X",
            session_id="conv-abc-123",
        )
        assert cmd_resume == [
            "/opt/bin/agy",
            "chat",
            "-m",
            "agent",
            "-r",
            "conv-abc-123",
            "--model",
            "gemini-3.8-flash-high",
            "--effort",
            "high",
            "Continue feature X",
        ]
        assert "--project" not in cmd_resume


def test_normalize_db_timestamp_all_units() -> None:
    """_normalize_db_timestamp normalizes s, ms, us, ns, and ISO-8601 strings (B1)."""
    target_sec = 1711800000.0
    assert _normalize_db_timestamp(None) is None
    assert _normalize_db_timestamp(-5) is None
    assert _normalize_db_timestamp(0) is None
    assert _normalize_db_timestamp("not-a-timestamp") is None

    # Seconds
    assert _normalize_db_timestamp(target_sec) == pytest.approx(target_sec)
    assert _normalize_db_timestamp(str(target_sec)) == pytest.approx(target_sec)
    # Milliseconds (> 1e11)
    assert _normalize_db_timestamp(int(target_sec * 1_000)) == pytest.approx(target_sec)
    # Microseconds (> 1e13)
    assert _normalize_db_timestamp(int(target_sec * 1_000_000)) == pytest.approx(target_sec)
    # Nanoseconds (> 1e16)
    assert _normalize_db_timestamp(int(target_sec * 1_000_000_000)) == pytest.approx(target_sec)
    # ISO-8601
    iso_val = _normalize_db_timestamp("2026-03-30T12:00:00Z")
    assert iso_val is not None
    assert iso_val == pytest.approx(parse_iso_timestamp("2026-03-30T12:00:00+00:00"))


def test_get_latest_session_id_prefers_newer_brain_dir_and_filters_stale(
    tmp_path: Path,
) -> None:
    """get_latest_session_id filters by since_timestamp and prefers newer brain/ over DB (B1)."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    db_file = tmp_path / "conversation_summaries.db"

    with sqlite3.connect(db_file) as con:
        con.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT, last_modified_time INTEGER)"
        )
        # Stale DB entry at t=1000s (in microseconds)
        con.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)",
            ("conv-stale-db", 1000 * 1_000_000),
        )

    _write_transcript(
        brain_dir,
        "conv-stale-db",
        [{"step_index": 1, "type": "USER_INPUT", "created_at": "1970-01-01T00:16:40Z"}],
        mtime=1000.0,
    )
    _write_transcript(
        brain_dir,
        "conv-fresh-brain",
        [{"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Working..."}],
        mtime=2000.0,
    )

    agent = AntigravityAgent(
        brain_dir=brain_dir,
        summaries_db=db_file,
    )

    # Without since_timestamp, brain_dir is newer than db_conv_brain_mtime
    assert agent.get_latest_session_id(tmp_path) == "conv-fresh-brain"
    assert (
        get_latest_conversation_id(brain_dir=brain_dir, summaries_db=db_file) == "conv-fresh-brain"
    )

    # With since_timestamp=1500.0, conv-stale-db is filtered out
    assert agent.get_latest_session_id(tmp_path, since_timestamp=1500.0) == "conv-fresh-brain"

    # With since_timestamp=2500.0, both are older than since_timestamp - 2.0 -> returns None
    assert agent.get_latest_session_id(tmp_path, since_timestamp=2500.0) is None


def test_get_session_status_and_heartbeat_since_timestamp(tmp_path: Path) -> None:
    """get_session_status and get_heartbeat respect since_timestamp and parse steps (B1)."""
    brain_dir = tmp_path / "brain"
    t_base = 1711800000.0
    iso_old = "2024-03-30T11:50:00Z"  # 1711799400.0
    iso_new = "2024-03-30T12:00:10Z"  # 1711800010.0

    _write_transcript(
        brain_dir,
        "conv-1",
        [
            {"step_index": 1, "type": "USER_INPUT", "created_at": iso_old},
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "created_at": iso_new,
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"CommandLine": "pytest -q", "toolSummary": "Run pytest"},
                    }
                ],
            },
        ],
        mtime=t_base + 15.0,
    )

    agent = AntigravityAgent(brain_dir=brain_dir)
    active, summary = agent.get_session_status("conv-1", since_timestamp=t_base)
    assert active is True
    assert summary == "Step 4: Tool run_command: Run pytest"

    hb = agent.get_heartbeat("conv-1", since_timestamp=t_base)
    assert hb.active is True
    assert hb.summary == "Step 4: Tool run_command: Run pytest"
    assert hb.latest_activity == "Step 4: Tool run_command: Run pytest"

    # If since_timestamp is after the latest step's created_at + 2s tolerance, inactive
    assert agent.get_session_status("conv-1", since_timestamp=t_base + 100.0) == (False, None)
    hb_stale = agent.get_heartbeat("conv-1", since_timestamp=t_base + 100.0)
    assert hb_stale.active is False


def test_get_session_transcript_status_step_variants(tmp_path: Path) -> None:
    """get_session_transcript_status formats command, file, and planner steps."""
    brain_dir = tmp_path / "brain"

    # Command without toolSummary
    _write_transcript(
        brain_dir,
        "conv-cmd",
        [
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "git status --short"}}
                ],
            }
        ],
    )
    assert get_session_transcript_status("conv-cmd", brain_dir=brain_dir) == (
        2,
        "Tool run_command: `git status --short`",
    )

    # File tool call
    _write_transcript(
        brain_dir,
        "conv-file",
        [
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "read_file", "args": {"TargetFile": "/repo/src/main.py"}}],
            }
        ],
    )
    assert get_session_transcript_status("conv-file", brain_dir=brain_dir) == (
        3,
        "Tool read_file: main.py",
    )

    # Planner response with text
    _write_transcript(
        brain_dir,
        "conv-plan",
        [{"step_index": 5, "type": "PLANNER_RESPONSE", "content": "Implemented the protocol."}],
    )
    assert get_session_transcript_status("conv-plan", brain_dir=brain_dir) == (
        5,
        "Gemini: Implemented the protocol.",
    )


def test_extract_command_prefix_multiline_and_env() -> None:
    """extract_command_prefix handles single-line, multiline python -c, comments, and env (B2)."""
    assert extract_command_prefix("") == ""
    assert extract_command_prefix("git status --short") == "git"
    assert extract_command_prefix("ls") == "ls"

    multiline_py = (
        "# comment line\n"
        'PYTHONPATH=src UV_NO_SYNC=1 python3 -c "\n'
        "import sys\n"
        "print(sys.version)\n"
        '"'
    )
    assert extract_command_prefix(multiline_py) == "python3"


def test_parse_denial_from_step_and_turn_scoped_detection(tmp_path: Path) -> None:
    """Turn-scoped permission denial detection handles CLI log offset and transcripts (B2)."""
    brain_dir = tmp_path / "brain"
    cli_log = tmp_path / "cli.log"
    settings_file = tmp_path / "settings.json"

    # 1. Write initial CLI log lines before the turn starts
    cli_log.write_text(
        "2026-03-30T10:00:00Z INFO old log line\n"
        '2026-03-30T10:00:01Z WARN soft-denying tool confirmation "OldTool"\n',
        encoding="utf-8",
    )
    offset_before_turn = cli_log.stat().st_size

    # No new soft denial after offset_before_turn
    assert (
        check_recent_soft_denial_in_cli_log(since_offset=offset_before_turn, cli_log_file=cli_log)
        is None
    )

    # Append a new soft denial during the turn
    with cli_log.open("a", encoding="utf-8") as f:
        f.write('2026-03-30T12:00:05Z WARN soft-denying tool confirmation "ReadUrlContent"\n')

    assert (
        check_recent_soft_denial_in_cli_log(since_offset=offset_before_turn, cli_log_file=cli_log)
        == "ReadUrlContent"
    )

    # Test log truncation recovery (since_offset > current file_size reads from start)
    cli_log.write_text(
        '2026-03-30T12:01:00Z WARN soft-denying tool confirmation "run_command"\n',
        encoding="utf-8",
    )
    assert (
        check_recent_soft_denial_in_cli_log(since_offset=999_999, cli_log_file=cli_log)
        == "run_command"
    )

    # 2. Test multiline denied command block in transcript step
    multiline_err = (
        "permission check failed for unsandboxed\n"
        "user denied permission to run command:\n"
        "python3 -c \"\nimport urllib.request\nprint('hi')\n\"\n"
        "Do not attempt to circumvent"
    )
    step_data = {
        "step_index": 7,
        "type": "GENERIC",
        "created_at": "2026-03-30T12:00:10Z",
        "error": multiline_err,
    }
    parsed = parse_denial_from_step(step_data)
    assert parsed == ("unsandboxed", "python3 -c \"\nimport urllib.request\nprint('hi')\n\"")

    turn_ts = parse_iso_timestamp("2026-03-30T12:00:00Z")
    assert turn_ts is not None
    _write_transcript(brain_dir, "conv-denial", [step_data], mtime=turn_ts + 10.0)

    assert check_recent_transcript_denial(
        since_timestamp=turn_ts,
        conv_id="conv-denial",
        brain_dir=brain_dir,
    ) == ("unsandboxed", "python3 -c \"\nimport urllib.request\nprint('hi')\n\"")

    # Stale transcript denial before turn_ts is ignored
    assert (
        check_recent_transcript_denial(
            since_timestamp=turn_ts + 60.0,
            conv_id="conv-denial",
            brain_dir=brain_dir,
        )
        is None
    )

    # 3. Test AntigravityAgent.detect_permission_denials
    agent = AntigravityAgent(
        brain_dir=brain_dir,
        cli_log_file=cli_log,
        settings_file=settings_file,
    )
    denials = agent.detect_permission_denials(
        session_id="conv-denial",
        cli_log_path=cli_log,
        cli_log_offset=999_999,
        turn_start_time=turn_ts,
    )
    assert len(denials) == 1
    assert denials[0].tool_name == "unsandboxed"
    assert denials[0].resource == "python3 -c \"\nimport urllib.request\nprint('hi')\n\""
    assert denials[0].command_prefix == "python3"


def test_extract_denied_permission_url_normalization(tmp_path: Path) -> None:
    """extract_denied_permission normalizes ReadUrlContent tool calls to read_url."""
    brain_dir = tmp_path / "brain"
    cli_log = tmp_path / "cli.log"
    turn_ts = parse_iso_timestamp("2026-03-30T12:00:00Z")
    assert turn_ts is not None

    _write_transcript(
        brain_dir,
        "conv-url",
        [
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-03-30T12:00:05Z",
                "tool_calls": [
                    {
                        "name": "ReadUrlContent",
                        "args": {"Url": "https://docs.python.org/3/library/typing.html"},
                    }
                ],
            }
        ],
        mtime=turn_ts + 5.0,
    )

    perm_kind, target = extract_denied_permission(
        "jetski: no output produced",
        perm_hint="ReadUrlContent",
        conv_id="conv-url",
        since_timestamp=turn_ts,
        brain_dir=brain_dir,
        cli_log_file=cli_log,
    )
    assert perm_kind == "read_url"
    assert target == "https://docs.python.org/3/library/typing.html"


def test_whitelist_helpers_and_agent_whitelist_denial(tmp_path: Path) -> None:
    """whitelist_command_in_settings, whitelist_url_in_settings, and agent.whitelist_denial."""
    settings_file = tmp_path / "settings.json"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    initialize_baseline_permissions(
        settings_file=settings_file,
        repo_root=repo_root,
        safe_baseline_allow=["command(git)", "command(git *)"],
        default_deny=["command(sudo)"],
    )
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["toolPermission"] == "proceed-in-sandbox"
    assert "command(git *)" in data["permissions"]["allow"]
    assert "command(sudo)" in data["permissions"]["deny"]

    # Command prefix whitelisting
    cmd_rules = whitelist_command_in_settings(
        "pytest tests/test_foo.py -k bar",
        prefix_only=True,
        settings_file=settings_file,
    )
    assert cmd_rules == ["command(pytest)", "command(pytest *)"]

    # URL domain whitelisting
    url_rule = whitelist_url_in_settings(
        "https://docs.python.org/3/library/asyncio.html",
        domain_wide=True,
        settings_file=settings_file,
    )
    assert url_rule == "read_url(docs.python.org/*)"

    # Agent.whitelist_denial
    agent = AntigravityAgent(settings_file=settings_file)
    denial = PermissionDenial(
        tool_name="unsandboxed",
        resource="uv run pytest",
        command_prefix="uv",
    )
    assert agent.whitelist_denial(denial, repo_root=repo_root) is True

    updated = json.loads(settings_file.read_text(encoding="utf-8"))
    allow_list = updated["permissions"]["allow"]
    assert "command(pytest *)" in allow_list
    assert "read_url(docs.python.org/*)" in allow_list
    assert "unsandboxed(uv run pytest)" in allow_list

    add_allowed_rules("write_file(/tmp/test)", settings_file=settings_file)
    updated2 = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "write_file(/tmp/test)" in updated2["permissions"]["allow"]


def test_extract_last_response_and_metrics(tmp_path: Path) -> None:
    """extract_last_response and extract_session_transcript_metrics parse transcripts."""
    brain_dir = tmp_path / "brain"
    _write_transcript(
        brain_dir,
        "conv-metrics",
        [
            {
                "step_index": 1,
                "source": "USER",
                "type": "USER_INPUT",
                "content": "Please implement feature Y.",
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "Feature Y is implemented and verified.",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "make check"}}],
                "usage": {"input_tokens": 200, "output_tokens": 50},
            },
        ],
    )

    agent = AntigravityAgent(brain_dir=brain_dir)
    assert agent.extract_last_response("conv-metrics") == "Feature Y is implemented and verified."
    assert (
        extract_last_agent_response("conv-metrics", brain_dir=brain_dir)
        == "Feature Y is implemented and verified."
    )
    assert (
        extract_latest_agent_summary("conv-metrics", brain_dir=brain_dir)
        == "Feature Y is implemented and verified."
    )

    metrics = extract_session_transcript_metrics("conv-metrics", brain_dir=brain_dir)
    assert metrics["total_steps"] == 2
    assert metrics["output_tokens_estimate"] == 50
    assert metrics["tool_calls_breakdown"] == {"run_command": 1}


def test_is_transient_session_error() -> None:
    """is_transient_session_error detects AGY_ERROR payloads and transient patterns."""
    err_payload = (
        'AGY_ERROR: {"error_code": 503, "status": "UNAVAILABLE", '
        '"short_error": "Service overloaded"}'
    )
    ok, reason = is_transient_session_error(err_payload, 1)
    assert ok is True
    assert reason == "Service overloaded"

    ok2, reason2 = is_transient_session_error("Error: connection reset by peer", 1)
    assert ok2 is True
    assert reason2 == "Connection reset"

    ok3, reason3 = is_transient_session_error("Normal completion", 0)
    assert ok3 is False
    assert reason3 == ""


def test_run_turn_success_and_timeout(tmp_path: Path) -> None:
    """AntigravityAgent.run_turn returns AgentSessionResult for normal and timeout runs."""
    brain_dir = tmp_path / "brain"
    cli_log = tmp_path / "cli.log"
    settings_file = tmp_path / "settings.json"

    agent = AntigravityAgent(
        agy_bin="agy",
        brain_dir=brain_dir,
        cli_log_file=cli_log,
        settings_file=settings_file,
    )

    mock_completed = subprocess.CompletedProcess(
        args=["agy", "chat", "-m", "agent", "Hello"],
        returncode=0,
        stdout="Done!\n",
        stderr="",
    )
    with (
        patch("subprocess.run", return_value=mock_completed),
        patch.object(agent, "get_latest_session_id", return_value="conv-run-1"),
    ):
        result = agent.run_turn("Hello", repo_root=tmp_path, cli_log_path=cli_log)

    assert isinstance(result, AgentSessionResult)
    assert result.exit_code == 0
    assert result.completed_normally is True
    assert result.error_message is None
    assert result.last_response == "Done!"
    assert result.session_id == "conv-run-1"

    # Timeout case
    timeout_exc = subprocess.TimeoutExpired(
        cmd=["agy", "chat", "-m", "agent", "Hang"],
        timeout=5,
        output="Partial output\n",
    )
    with (
        patch("subprocess.run", side_effect=timeout_exc),
        patch.object(agent, "get_latest_session_id", return_value="conv-timeout"),
    ):
        timeout_res = agent.run_turn("Hang", repo_root=tmp_path, timeout=5)

    assert timeout_res.exit_code == 124
    assert timeout_res.completed_normally is False
    assert timeout_res.error_message is not None
    assert "timed out" in timeout_res.error_message
    assert "Partial output" in timeout_res.last_response

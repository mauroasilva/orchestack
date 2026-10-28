# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for the CodexAgent backend and daemon runner integration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orchestack.agents import (
    AgentBackend,
    AgentSessionResult,
    CodexAgent,
    PermissionDenial,
    extract_codex_heartbeat_from_events,
    extract_codex_last_response_from_events,
    extract_thread_id_from_events,
    parse_codex_jsonl,
    parse_codex_permission_denials,
    read_codex_log_slice,
    whitelist_codex_denial_in_config,
)


def _write_codex_rollout(
    sessions_dir: Path,
    rel_dir: str,
    rollout_name: str,
    events: list[dict[str, object]],
    *,
    mtime: float | None = None,
) -> Path:
    sub = sessions_dir / rel_dir
    sub.mkdir(parents=True, exist_ok=True)
    rollout = sub / rollout_name
    lines = [json.dumps(ev) for ev in events]
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(rollout, (mtime, mtime))
    return rollout


def test_codex_agent_satisfies_protocol() -> None:
    """CodexAgent satisfies the runtime-checkable AgentBackend protocol."""
    agent = CodexAgent()
    assert isinstance(agent, AgentBackend)
    assert agent.name == "codex"


def test_codex_check_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """check_available locates codex via shutil.which or absolute path."""
    agent = CodexAgent(codex_bin="codex")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    fake_ver = subprocess.CompletedProcess(
        args=["/usr/local/bin/codex", "--version"],
        returncode=0,
        stdout="codex 0.42.0\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_ver):
        ok, version = agent.check_available()
    assert ok is True
    assert "0.42.0" in version

    monkeypatch.setattr("shutil.which", lambda _name: None)
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    agent_with_path = CodexAgent(codex_bin=str(fake_bin))
    with patch("subprocess.run", side_effect=OSError("no exec")):
        ok2, path2 = agent_with_path.check_available()
    assert ok2 is True
    assert path2 == str(fake_bin)

    agent_missing = CodexAgent(codex_bin="nonexistent-codex")
    ok3, reason3 = agent_missing.check_available()
    assert ok3 is False
    assert "not found" in reason3


def test_codex_build_command_kickoff_and_resume(tmp_path: Path) -> None:
    """build_command constructs codex exec and codex exec resume commands."""
    agent = CodexAgent(
        codex_bin="/usr/local/bin/codex",
        model="gpt-5-codex",
        repo_root=tmp_path,
        full_auto=True,
    )

    cmd_new = agent.build_command("Implement 0005", repo_root=tmp_path)
    assert cmd_new == [
        "/usr/local/bin/codex",
        "exec",
        "Implement 0005",
        "--json",
        "-C",
        str(tmp_path),
        "--full-auto",
        "--model",
        "gpt-5-codex",
    ]
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd_new

    cmd_resume = agent.build_command(
        "Address review comments",
        session_id="thread-abc-999",
        repo_root=tmp_path,
    )
    assert cmd_resume == [
        "/usr/local/bin/codex",
        "exec",
        "resume",
        "thread-abc-999",
        "Address review comments",
        "--json",
        "--full-auto",
        "--model",
        "gpt-5-codex",
    ]

    # Configured sandbox mode + extra add-dirs
    agent_sandbox = CodexAgent(
        codex_bin="codex",
        repo_root=tmp_path,
        full_auto=False,
        sandbox_mode="workspace-write",
        add_dirs=["/tmp/shared"],
    )
    cmd_sb = agent_sandbox.build_command("Run task", repo_root=tmp_path)
    assert "--sandbox" in cmd_sb
    assert "workspace-write" in cmd_sb
    assert "--add-dir" in cmd_sb
    assert "/tmp/shared" in cmd_sb


def test_parse_codex_stream_events_and_thread_id() -> None:
    """JSONL events parse cleanly and extract thread_id from thread.started."""
    raw = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-0005-xyz"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": "uv run pytest",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_2",
                        "type": "agent_message",
                        "text": "All Codex deliverables implemented and verified.",
                    },
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    events = parse_codex_jsonl(raw)
    assert len(events) == 5
    assert extract_thread_id_from_events(events) == "thread-0005-xyz"


def test_codex_session_id_fallback_to_rollout_files_b1(tmp_path: Path) -> None:
    """Fallback thread_id discovery scans ~/.codex/sessions/ with since_timestamp."""
    sessions_dir = tmp_path / "sessions"
    _write_codex_rollout(
        sessions_dir,
        "2026/10/26",
        "rollout-2026-10-26-old-thread-111.jsonl",
        [{"type": "thread.started", "thread_id": "old-thread-111"}],
        mtime=1000.0,
    )
    _write_codex_rollout(
        sessions_dir,
        "2026/10/27",
        "rollout-2026-10-27-new-thread-222.jsonl",
        [{"type": "thread.started", "thread_id": "new-thread-222"}],
        mtime=2000.0,
    )

    agent = CodexAgent(sessions_dir=sessions_dir, repo_root=tmp_path)
    assert agent.get_latest_session_id(tmp_path, since_timestamp=1500.0) == "new-thread-222"
    assert agent.get_latest_session_id(tmp_path, since_timestamp=2500.0) is None


def test_codex_heartbeat_from_item_events(tmp_path: Path) -> None:
    """Heartbeat tracks command_execution, file_change, and agent_message from cli_log_offset."""
    log_file = tmp_path / "codex.log"
    stale_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "echo stale"},
        }
    )
    log_file.write_text(stale_line + "\n", encoding="utf-8")
    offset = log_file.stat().st_size

    events = [
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "ruff check src tests"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"path": "src/orchestack/agents/codex.py"}],
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Finished updating codex.py and running checks.",
            },
        },
    ]
    with log_file.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    sliced = read_codex_log_slice(log_file, cli_log_offset=offset)
    parsed = parse_codex_jsonl(sliced)
    hb = extract_codex_heartbeat_from_events(parsed)
    assert hb == "agent_message: Finished updating codex.py and running checks."

    agent = CodexAgent(cli_log_path=log_file, sessions_dir=tmp_path / "sessions")
    snap = agent.get_heartbeat(session_id=None, cli_log_path=log_file)
    assert snap.active is True
    assert "Finished updating codex.py" in snap.summary


def test_codex_extract_last_agent_message_before_turn_completed(tmp_path: Path) -> None:
    """Extract final agent response from the last agent_message item before turn.completed."""
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Intermediate status message."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Final summary before turn.completed.",
                    },
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    summary = extract_codex_last_response_from_events(parse_codex_jsonl(stream))
    assert summary == "Final summary before turn.completed."

    # Fallback to rollout file when stdout wasn't cached
    sessions_dir = tmp_path / "sessions"
    _write_codex_rollout(
        sessions_dir,
        "2026/10/27",
        "rollout-2026-10-27-th-fallback.jsonl",
        [
            {"type": "thread.started", "thread_id": "th-fallback"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Summary recovered from rollout file.",
                },
            },
            {"type": "turn.completed"},
        ],
        mtime=3000.0,
    )
    agent = CodexAgent(sessions_dir=sessions_dir, repo_root=tmp_path)
    assert agent.extract_last_response("th-fallback") == "Summary recovered from rollout file."


def test_codex_turn_scoped_sandbox_and_approval_denial_b2(tmp_path: Path) -> None:
    """Detect sandbox_error, approval_required, and Operation not permitted from cli_log_offset."""
    cli_log = tmp_path / "codex_cli.log"
    stale_line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "git push origin old",
                "exit_code": 1,
                "aggregated_output": "sandbox_error: Operation not permitted",
            },
        }
    )
    cli_log.write_text(stale_line + "\n", encoding="utf-8")
    offset = cli_log.stat().st_size

    agent = CodexAgent(cli_log_path=cli_log, sessions_dir=tmp_path / "sessions")
    assert (
        agent.detect_permission_denials(
            session_id="th-1",
            cli_log_path=cli_log,
            cli_log_offset=offset,
            turn_start_time=1000.0,
        )
        == []
    )

    new_line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "git commit -m 'test'",
                "exit_code": 1,
                "aggregated_output": "error: Operation not permitted (.git/index.lock)",
            },
        }
    )
    with cli_log.open("a", encoding="utf-8") as f:
        f.write(new_line + "\n")

    denials = agent.detect_permission_denials(
        session_id="th-1",
        cli_log_path=cli_log,
        cli_log_offset=offset,
        turn_start_time=1000.0,
    )
    assert len(denials) == 1
    assert denials[0].resource == "git commit -m 'test'"
    assert denials[0].command_prefix == "git"

    # Also test approval_required event
    approval_stream = json.dumps(
        {
            "type": "approval_required",
            "command": "curl https://example.com",
        }
    )
    parsed = parse_codex_permission_denials(approval_stream)
    assert len(parsed) == 1
    assert parsed[0].tool_name == "approval_required"
    assert parsed[0].resource == "curl https://example.com"


def test_codex_whitelist_denial_updates_config_toml_and_flags(tmp_path: Path) -> None:
    """whitelist_denial writes <repo_root>/.codex/config.toml and updates resume flags."""
    agent = CodexAgent(repo_root=tmp_path)

    cmd_denial = PermissionDenial(
        tool_name="command_execution",
        resource="git commit -m 'fix'",
        command_prefix="git commit",
        raw_reason="Operation not permitted",
    )
    ok = agent.whitelist_denial(cmd_denial, tmp_path)
    assert ok is True

    config_toml = tmp_path / ".codex" / "config.toml"
    assert config_toml.is_file()
    content = config_toml.read_text(encoding="utf-8")
    assert 'approval_policy = "never"' in content
    assert 'sandbox_mode = "workspace-write"' in content
    assert '"git commit"' in content

    outside_dir = tmp_path / "external_dir"
    outside_dir.mkdir()
    write_denial = PermissionDenial(
        tool_name="sandbox_error",
        resource=str(outside_dir / "file.txt"),
        raw_reason=f"Operation not permitted: '{outside_dir / 'file.txt'}'",
    )
    ok_write = agent.whitelist_denial(write_denial, tmp_path)
    assert ok_write is True
    assert str(outside_dir) in agent.get_add_dirs()

    # Resumed command passes --add-dir for the newly whitelisted directory
    resume_cmd = agent.build_command("Continue", session_id="th-1", repo_root=tmp_path)
    assert "--add-dir" in resume_cmd
    assert str(outside_dir) in resume_cmd

    ok_cfg, added = whitelist_codex_denial_in_config(
        PermissionDenial(
            tool_name="sandbox_error",
            resource=str(outside_dir),
            raw_reason="sandbox_error",
        ),
        tmp_path,
    )
    assert ok_cfg is True
    assert added == str(outside_dir)


def test_codex_run_turn_mocked_subprocess(tmp_path: Path) -> None:
    """run_turn executes codex subprocess and returns structured AgentSessionResult."""
    agent = CodexAgent(
        codex_bin="/usr/local/bin/codex",
        repo_root=tmp_path,
        sessions_dir=tmp_path / "sessions",
    )
    stdout_lines = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-run-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Codex completed prompt 0005.",
                    },
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    fake_completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/codex"],
        returncode=0,
        stdout=stdout_lines,
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed):
        result = agent.run_turn("Implement 0005", repo_root=tmp_path)

    assert result.exit_code == 0
    assert result.completed_normally is True
    assert result.session_id == "thread-run-1"
    assert result.last_response == "Codex completed prompt 0005."
    assert result.error_message is None


def test_daemon_runner_pr_review_and_dod_loop_with_codex_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon_runner executes DoD auto-heal and PR review fix loop using CodexAgent."""
    import importlib
    import sys

    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    dr: Any = importlib.import_module("daemon_runner")

    queued_dir = tmp_path / "prompts" / "queued"
    impl_dir = tmp_path / "prompts" / "implemented"
    learnings_dir = tmp_path / "docs" / "learnings"
    logs_dir = tmp_path / ".gemini" / "logs"
    queued_dir.mkdir(parents=True)
    impl_dir.mkdir(parents=True)
    learnings_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    prompt_path = impl_dir / "0005-test-codex.md"
    prompt_path.write_text("# 0005 — Test Codex\n", encoding="utf-8")
    (learnings_dir / "0005-test-codex-learnings.md").write_text(
        "## Summary\n- Initial\n", encoding="utf-8"
    )

    monkeypatch.setattr(dr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dr, "QUEUED_DIR", queued_dir)
    monkeypatch.setattr(dr, "IMPLEMENTED_DIR", impl_dir)
    monkeypatch.setattr(dr, "LEARNINGS_DIR", learnings_dir)
    monkeypatch.setattr(dr, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(dr, "PR_REVIEWS_FILE", logs_dir / "pr_reviews.json")

    backend = CodexAgent(
        codex_bin="/usr/local/bin/codex",
        repo_root=tmp_path,
        sessions_dir=tmp_path / "sessions",
    )

    dod_checks = [
        (False, "'make check' failed: ruff format error"),
        (True, ""),
    ]
    monkeypatch.setattr(dr, "check_definition_of_done", lambda _p: dod_checks.pop(0))

    turns: list[tuple[str, str | None]] = []

    def fake_run_turn(
        prompt: str,
        repo_root: Path,
        session_id: str | None = None,
        timeout: int = 1800,
        cli_log_path: Path | None = None,
    ) -> AgentSessionResult:
        turns.append((prompt, session_id))
        return AgentSessionResult(
            session_id="codex-thread-77",
            exit_code=0,
            completed_normally=True,
            error_message=None,
            last_response="Fixed formatting and verified make check.",
            cli_log_offset=0,
            turn_start_time=1000.0,
        )

    monkeypatch.setattr(backend, "run_turn", fake_run_turn)

    # 1. Execute prompt session with DoD failure on turn 1 -> auto-heal on turn 2
    ok = dr.execute_prompt_session(
        agy_bin="codex",
        prompt_path=prompt_path,
        agent_backend=backend,
    )
    assert ok is True
    assert len(turns) == 2
    assert turns[0][1] is None
    assert turns[1][1] == "codex-thread-77"
    assert "Definition of Done verification failed" in turns[1][0]

    # 2. Address PR review feedback with CodexAgent
    monkeypatch.setattr(dr, "check_definition_of_done", lambda _p: (True, ""))
    monkeypatch.setattr(dr, "get_current_branch", lambda: "orchestack/0005-test-codex")
    monkeypatch.setattr(
        dr,
        "run_cmd",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    posted_comments: list[tuple[int, str]] = []

    class _FakeForge:
        def post_pr_comment(self, pr_num: int, body: str) -> bool:
            posted_comments.append((pr_num, body))
            return True

    monkeypatch.setattr(dr, "_make_github_forge", lambda _gh: _FakeForge())

    review_ok = dr.address_pr_feedback(
        gh_bin="gh",
        agy_bin="codex",
        pr_number=12,
        pr_title="0005 Codex Backend",
        branch_name="orchestack/0005-test-codex",
        prompt_path=prompt_path,
        new_comments=[{"id": "c1", "author": "reviewer", "body": "Please add a docstring."}],
        agent_backend=backend,
    )
    assert review_ok is True
    assert len(posted_comments) == 1
    assert "Fixed formatting and verified make check." in posted_comments[0][1]

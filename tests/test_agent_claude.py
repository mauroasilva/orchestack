# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for the ClaudeCodeAgent backend, factory, and daemon runner integration."""

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
    AntigravityAgent,
    ClaudeCodeAgent,
    CodexAgent,
    PermissionDenial,
    create_agent_backend,
    extract_heartbeat_from_events,
    extract_result_from_events,
    extract_session_id_from_events,
    format_claude_allow_rule,
    normalize_agent_backend_name,
    parse_claude_permission_denials,
    parse_claude_stream_jsonl,
    project_slug_for_repo,
    read_claude_log_slice,
    whitelist_claude_denial_in_settings,
)
from orchestack.config import RepoConfig


def _write_claude_project_transcript(
    projects_dir: Path,
    project_slug: str,
    session_id: str,
    events: list[dict[str, object]],
    *,
    mtime: float | None = None,
) -> Path:
    proj_dir = projects_dir / project_slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    transcript = proj_dir / f"{session_id}.jsonl"
    lines = [json.dumps(ev) for ev in events]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(transcript, (mtime, mtime))
    return transcript


def test_claude_code_agent_satisfies_protocol() -> None:
    """ClaudeCodeAgent satisfies the runtime-checkable AgentBackend protocol."""
    agent = ClaudeCodeAgent()
    assert isinstance(agent, AgentBackend)
    assert agent.name == "claude"


def test_claude_check_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """check_available locates claude via shutil.which or absolute path."""
    agent = ClaudeCodeAgent(claude_bin="claude")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    fake_ver = subprocess.CompletedProcess(
        args=["/usr/local/bin/claude", "--version"],
        returncode=0,
        stdout="1.0.24 (Claude Code)\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_ver):
        ok, version = agent.check_available()
    assert ok is True
    assert "1.0.24" in version

    monkeypatch.setattr("shutil.which", lambda _name: None)
    fake_bin = tmp_path / "claude"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    agent_with_path = ClaudeCodeAgent(claude_bin=str(fake_bin))
    with patch("subprocess.run", side_effect=OSError("no exec")):
        ok2, path2 = agent_with_path.check_available()
    assert ok2 is True
    assert path2 == str(fake_bin)

    agent_missing = ClaudeCodeAgent(claude_bin="nonexistent-claude")
    ok3, reason3 = agent_missing.check_available()
    assert ok3 is False
    assert "not found" in reason3


def test_claude_build_command_kickoff_and_resume() -> None:
    """build_command constructs proper kickoff and --resume commands and never skips permissions."""
    agent = ClaudeCodeAgent(
        claude_bin="/usr/local/bin/claude",
        model="claude-sonnet-4-5",
        allowed_tools=["Bash(make:*)", "Read"],
    )

    cmd_new = agent.build_command("Implement 0005")
    assert cmd_new == [
        "/usr/local/bin/claude",
        "-p",
        "Implement 0005",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-sonnet-4-5",
        "--allowedTools",
        "Bash(make:*)",
        "Read",
    ]
    assert "--dangerously-skip-permissions" not in cmd_new

    cmd_resume = agent.build_command(
        "Continue 0005",
        session_id="sess-abc-123",
    )
    assert cmd_resume == [
        "/usr/local/bin/claude",
        "-p",
        "Continue 0005",
        "--resume",
        "sess-abc-123",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-sonnet-4-5",
        "--allowedTools",
        "Bash(make:*)",
        "Read",
    ]
    assert "--dangerously-skip-permissions" not in cmd_resume


def test_parse_claude_stream_events_and_session_id() -> None:
    """Stream-json lines parse cleanly and extract session_id from init or result events."""
    raw = "\n".join(
        [
            "Non-json banner line",
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "claude-sess-001",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "pytest -q"},
                            }
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "claude-sess-001",
                    "result": "All tests pass and deliverables are complete.",
                }
            ),
        ]
    )
    events = parse_claude_stream_jsonl(raw)
    assert len(events) == 3
    assert extract_session_id_from_events(events) == "claude-sess-001"


def test_claude_session_id_fallback_to_projects_dir_b1(tmp_path: Path) -> None:
    """Fallback session ID discovery filters ~/.claude/projects transcripts by since_timestamp."""
    projects_dir = tmp_path / "projects"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    slug = project_slug_for_repo(repo_root)

    _write_claude_project_transcript(
        projects_dir,
        slug,
        "old-session-111",
        [{"type": "system", "subtype": "init", "session_id": "old-session-111"}],
        mtime=1000.0,
    )
    _write_claude_project_transcript(
        projects_dir,
        slug,
        "new-session-222",
        [{"type": "system", "subtype": "init", "session_id": "new-session-222"}],
        mtime=2000.0,
    )

    agent = ClaudeCodeAgent(projects_dir=projects_dir)
    assert agent.get_latest_session_id(repo_root, since_timestamp=1500.0) == "new-session-222"
    assert agent.get_latest_session_id(repo_root, since_timestamp=2500.0) is None


def test_claude_heartbeat_from_assistant_tool_use(tmp_path: Path) -> None:
    """Heartbeat extracts live tool-use status for Bash, Edit, Write, Read, and WebFetch."""
    log_file = tmp_path / "claude.log"
    stale_line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "old.py"}}]
            },
        }
    )
    log_file.write_text(stale_line + "\n", encoding="utf-8")
    offset = log_file.stat().st_size

    turn_events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "src/a.py"}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "src/b.py"}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "WebFetch",
                        "input": {"url": "https://docs.anthropic.com/en/docs"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "uv run pytest -q"},
                    }
                ]
            },
        },
    ]
    with log_file.open("a", encoding="utf-8") as f:
        for ev in turn_events:
            f.write(json.dumps(ev) + "\n")

    sliced = read_claude_log_slice(log_file, cli_log_offset=offset)
    events = parse_claude_stream_jsonl(sliced)
    hb = extract_heartbeat_from_events(events)
    assert hb == "Tool: Bash (uv run pytest -q)"

    agent = ClaudeCodeAgent(cli_log_path=log_file, projects_dir=tmp_path / "projects")
    snap = agent.get_heartbeat(session_id=None, cli_log_path=log_file)
    assert snap.active is True
    assert snap.summary == "Tool: Bash (uv run pytest -q)"


def test_claude_extract_last_response_and_status(tmp_path: Path) -> None:
    """Extract final summary text from result events (success/error subtypes) or project JSONL."""
    for subtype, expected_err in (
        ("success", False),
        ("error_max_turns", True),
        ("error_during_execution", True),
    ):
        stream = json.dumps(
            {
                "type": "result",
                "subtype": subtype,
                "session_id": "sess-xyz",
                "result": f"Summary for {subtype}: verified all changes.",
            }
        )
        summary, status, is_err = extract_result_from_events(parse_claude_stream_jsonl(stream))
        assert status == subtype
        assert is_err is expected_err
        assert summary == f"Summary for {subtype}: verified all changes."

    # Fallback to ~/.claude/projects/<project-slug>/<session_id>.jsonl
    projects_dir = tmp_path / "projects"
    _write_claude_project_transcript(
        projects_dir,
        "-Users-dev-repo",
        "sess-fallback-99",
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Fallback assistant summary from project jsonl file.",
                        }
                    ]
                },
            }
        ],
        mtime=3000.0,
    )
    agent = ClaudeCodeAgent(projects_dir=projects_dir)
    resp = agent.extract_last_response("sess-fallback-99")
    assert resp == "Fallback assistant summary from project jsonl file."


def test_claude_turn_scoped_permission_denial_b2(tmp_path: Path) -> None:
    """Permission denial detection ignores stale events before cli_log_offset / turn_start_time."""
    cli_log = tmp_path / "claude_cli.log"
    stale_event = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "permission_denials": [
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "rm -rf /stale"},
                }
            ],
        }
    )
    cli_log.write_text(stale_event + "\n", encoding="utf-8")
    offset = cli_log.stat().st_size

    agent = ClaudeCodeAgent(cli_log_path=cli_log, projects_dir=tmp_path / "projects")
    # No new denial since offset
    assert (
        agent.detect_permission_denials(
            session_id="sess-denial-1",
            cli_log_path=cli_log,
            cli_log_offset=offset,
            turn_start_time=1000.0,
        )
        == []
    )

    # Append a new denial after offset
    new_event = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "sess-denial-1",
            "permission_denials": [
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr view 42"},
                }
            ],
        }
    )
    with cli_log.open("a", encoding="utf-8") as f:
        f.write(new_event + "\n")

    denials = agent.detect_permission_denials(
        session_id="sess-denial-1",
        cli_log_path=cli_log,
        cli_log_offset=offset,
        turn_start_time=1000.0,
    )
    assert len(denials) == 1
    assert denials[0].tool_name == "Bash"
    assert denials[0].resource == "gh pr view 42"
    assert denials[0].command_prefix == "gh"

    # Also test text-based denial detection ("Permission to use ... has been denied")
    text_output = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "WebFetch",
                            "input": {"url": "https://api.github.com/repos/org/repo"},
                        }
                    ]
                },
            }
        )
        + "\nError: Permission to use WebFetch has been denied because it requires approval.\n"
    )
    parsed = parse_claude_permission_denials(text_output)
    assert len(parsed) == 1
    assert parsed[0].tool_name == "WebFetch"


def test_claude_whitelist_denial_settings_local_json(tmp_path: Path) -> None:
    """whitelist_denial updates <repo_root>/.claude/settings.local.json and allowed_tools."""
    agent = ClaudeCodeAgent()

    cmd_denial = PermissionDenial(
        tool_name="Bash",
        resource="uv run pytest -k test_claude",
        command_prefix="uv run",
        raw_reason="Permission to use Bash has been denied",
    )
    assert format_claude_allow_rule(cmd_denial) == "Bash(uv run:*)"
    ok = agent.whitelist_denial(cmd_denial, tmp_path)
    assert ok is True
    assert "Bash(uv run:*)" in agent.get_allowed_tools()

    settings_file = tmp_path / ".claude" / "settings.local.json"
    assert settings_file.is_file()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "Bash(uv run:*)" in data["permissions"]["allow"]

    url_denial = PermissionDenial(
        tool_name="WebFetch",
        resource="https://docs.python.org/3/",
        raw_reason="Permission to use WebFetch has been denied",
    )
    ok_url = agent.whitelist_denial(url_denial, tmp_path)
    assert ok_url is True
    assert "WebFetch" in agent.get_allowed_tools()
    data2 = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "WebFetch" in data2["permissions"]["allow"]

    ok_write, write_rule = whitelist_claude_denial_in_settings(
        PermissionDenial(tool_name="Edit", resource="src/foo.py"),
        tmp_path,
    )
    assert ok_write is True
    assert write_rule == "Edit(src/foo.py)"

    # Subsequent resume command includes updated --allowedTools
    resume_cmd = agent.build_command("Resume after whitelist", session_id="s-1")
    assert "--allowedTools" in resume_cmd
    assert "Bash(uv run:*)" in resume_cmd
    assert "WebFetch" in resume_cmd


def test_claude_run_turn_mocked_subprocess(tmp_path: Path) -> None:
    """run_turn executes claude subprocess and returns structured AgentSessionResult."""
    agent = ClaudeCodeAgent(
        claude_bin="/usr/local/bin/claude",
        projects_dir=tmp_path / "projects",
    )
    stdout_lines = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-run-1"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "sess-run-1",
                    "result": "Completed implementation of 0005.",
                }
            ),
        ]
    )
    fake_completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/claude"],
        returncode=0,
        stdout=stdout_lines,
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed):
        result = agent.run_turn("Implement 0005", repo_root=tmp_path)

    assert result.exit_code == 0
    assert result.completed_normally is True
    assert result.session_id == "sess-run-1"
    assert result.last_response == "Completed implementation of 0005."
    assert result.error_message is None


def test_agent_factory_selects_backends(tmp_path: Path) -> None:
    """create_agent_backend selects AntigravityAgent, ClaudeCodeAgent, or CodexAgent."""
    cfg_agy = RepoConfig(repo_root=tmp_path, agent_backend="agy")
    assert isinstance(create_agent_backend(cfg_agy), AntigravityAgent)

    cfg_claude = RepoConfig(repo_root=tmp_path, agent_backend="claude")
    claude_backend = create_agent_backend(cfg_claude, model="claude-sonnet-4-5")
    assert isinstance(claude_backend, ClaudeCodeAgent)
    assert claude_backend.model == "claude-sonnet-4-5"

    cfg_codex = RepoConfig(repo_root=tmp_path, agent_backend="codex")
    codex_backend = create_agent_backend(cfg_codex)
    assert isinstance(codex_backend, CodexAgent)

    assert normalize_agent_backend_name("claude-code") == "claude"
    assert normalize_agent_backend_name("openai-codex") == "codex"
    assert normalize_agent_backend_name("gemini") == "agy"

    with pytest.raises(ValueError, match="Unsupported agent backend"):
        create_agent_backend("unknown-backend")


def test_daemon_runner_execute_prompt_session_with_claude_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon_runner.execute_prompt_session drives ClaudeCodeAgent across denial + completion."""
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

    prompt_path = queued_dir / "0005-test-claude.md"
    prompt_path.write_text("# 0005 — Test Claude\n", encoding="utf-8")

    monkeypatch.setattr(dr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dr, "QUEUED_DIR", queued_dir)
    monkeypatch.setattr(dr, "IMPLEMENTED_DIR", impl_dir)
    monkeypatch.setattr(dr, "LEARNINGS_DIR", learnings_dir)
    monkeypatch.setattr(dr, "LOGS_DIR", logs_dir)

    backend = ClaudeCodeAgent(
        claude_bin="/usr/local/bin/claude",
        projects_dir=tmp_path / "projects",
    )

    turn_calls: list[tuple[str, str | None]] = []

    def fake_run_turn(
        prompt: str,
        repo_root: Path,
        session_id: str | None = None,
        timeout: int = 1800,
        cli_log_path: Path | None = None,
    ) -> AgentSessionResult:
        offset = cli_log_path.stat().st_size if cli_log_path and cli_log_path.exists() else 0
        turn_calls.append((prompt, session_id))
        if len(turn_calls) == 1:
            if cli_log_path is not None:
                denial_event = json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "claude-sess-42",
                        "permission_denials": [
                            {
                                "tool_name": "Bash",
                                "tool_input": {"command": "make check"},
                            }
                        ],
                    }
                )
                with cli_log_path.open("a", encoding="utf-8") as f:
                    f.write(denial_event + "\n")
            return AgentSessionResult(
                session_id="claude-sess-42",
                exit_code=0,
                completed_normally=True,
                error_message=None,
                last_response="",
                cli_log_offset=offset,
                turn_start_time=1000.0,
            )
        # Second turn: move prompt to implemented and write learnings
        impl_prompt = impl_dir / prompt_path.name
        impl_prompt.write_text("# 0005 — Test Claude\n", encoding="utf-8")
        (learnings_dir / f"{prompt_path.stem}-learnings.md").write_text(
            "## Summary\n- Done\n", encoding="utf-8"
        )
        return AgentSessionResult(
            session_id="claude-sess-42",
            exit_code=0,
            completed_normally=True,
            error_message=None,
            last_response="Implemented 0005 and verified make check.",
            cli_log_offset=offset,
            turn_start_time=2000.0,
        )

    monkeypatch.setattr(backend, "run_turn", fake_run_turn)
    monkeypatch.setattr(dr, "check_definition_of_done", lambda _p: (True, ""))

    ok = dr.execute_prompt_session(
        agy_bin="claude",
        prompt_path=prompt_path,
        agent_backend=backend,
    )
    assert ok is True
    assert len(turn_calls) == 2
    assert turn_calls[0][1] is None
    assert turn_calls[1][1] == "claude-sess-42"
    assert "Bash(make:*)" in backend.get_allowed_tools()
    assert dr.extract_latest_agent_summary() == "Implemented 0005 and verified make check."

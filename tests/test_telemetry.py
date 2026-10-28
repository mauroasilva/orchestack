# Copyright 2026 The Orchestack Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for multi-agent telemetry extraction and reporting."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from orchestack.telemetry.reporter import (
    SessionTelemetry,
    build_canonical_slug_map,
    collect_multi_agent_telemetry,
    collect_telemetry_dataset,
    discover_antigravity_sessions,
    discover_claude_sessions,
    discover_codex_sessions,
    export_markdown_table,
    format_telemetry_json,
    format_telemetry_markdown,
    load_antigravity_titles,
    parse_antigravity_transcript,
    parse_claude_events_telemetry,
    parse_claude_session_file,
    parse_codex_events_telemetry,
    parse_codex_session_file,
    parse_daemon_log_file,
)


def _setup_mock_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "prompts" / "implemented").mkdir(parents=True)
    (repo / "prompts" / "queued").mkdir(parents=True)
    (repo / "prompts" / "implemented" / "0001-bootstrap.md").write_text(
        "# 0001 Bootstrap\n", encoding="utf-8"
    )
    (repo / "prompts" / "implemented" / "0002-forge.md").write_text(
        "# 0002 Forge\n", encoding="utf-8"
    )
    (repo / "prompts" / "queued" / "0003-agent.md").write_text("# 0003 Agent\n", encoding="utf-8")
    return repo


def test_build_canonical_slug_map(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    slug_map = build_canonical_slug_map(repo)
    assert slug_map == {
        "0001": "0001-bootstrap",
        "0002": "0002-forge",
        "0003": "0003-agent",
    }


def test_load_antigravity_titles_sqlite_and_vscdb(tmp_path: Path) -> None:
    db_summaries = tmp_path / "conversation_summaries.db"
    with sqlite3.connect(db_summaries) as conn:
        conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT)")
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)",
            ("conv-sqlite-1", "Orchestack - Prompt 0001"),
        )

    db_vsc = tmp_path / "state.vscdb"
    with sqlite3.connect(db_vsc) as conn:
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                "antigravity.conversationSummaries",
                json.dumps({"conv-vsc-2": {"title": "Prompt 0002 Implementation"}}),
            ),
        )

    titles = load_antigravity_titles(db_paths=(db_summaries, db_vsc))
    assert titles["conv-sqlite-1"] == "Orchestack - Prompt 0001"
    assert titles["conv-vsc-2"] == "Prompt 0002 Implementation"


def test_parse_antigravity_transcript(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    slug_map = build_canonical_slug_map(repo)

    brain_dir = tmp_path / "brain"
    empty_dir = tmp_path / "empty_brain"
    logs_dir = brain_dir / "conv-agy-100" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript_full.jsonl"
    events = [
        {
            "step_index": 0,
            "source": "USER",
            "type": "USER_INPUT",
            "created_at": "2026-10-28T08:00:00Z",
            "content": "Execute prompts/implemented/0001-bootstrap.md",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-28T08:02:00Z",
            "content": "Implemented bootstrap.",
            "thinking": "Checking repository layout.",
            "usage": {"input_tokens": 1200, "output_tokens": 300},
            "tool_calls": [
                {"function": {"name": "run_command", "arguments": '{"cmd": "pytest"}'}},
                {"function": {"name": "write_file", "arguments": '{"path": "a.py"}'}},
            ],
        },
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    raw = discover_antigravity_sessions(cli_brain_dir=brain_dir, ide_brain_dir=empty_dir)
    assert "conv-agy-100" in raw

    parsed = parse_antigravity_transcript(
        "conv-agy-100",
        raw["conv-agy-100"],
        slug_map,
        {"conv-agy-100": "Bootstrap Session"},
    )
    assert parsed is not None
    assert parsed.backend == "agy"
    assert parsed.prompt_num == "0001"
    assert parsed.prompt_slug == "0001-bootstrap"
    assert parsed.total_steps == 2
    assert parsed.input_tokens >= 1200
    assert parsed.output_tokens == 300
    assert parsed.duration_seconds == 120.0
    assert parsed.tool_calls_breakdown == {"run_command": 1, "write_file": 1}


def test_parse_claude_session_and_events(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    slug_map = build_canonical_slug_map(repo)

    claude_dir = tmp_path / "claude_projects" / "my-project"
    claude_dir.mkdir(parents=True)
    session_file = claude_dir / "session-claude-0002.jsonl"

    events: list[dict[str, Any]] = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "claude-sess-2",
            "timestamp": "2026-10-28T09:00:00Z",
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "Please implement prompts/implemented/0002-forge.md "
                    "on branch orchestack/0002-forge"
                )
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Creating forge protocol..."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file": "forge.py"}},
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "claude-sess-2",
            "is_error": False,
            "duration_ms": 45000,
            "num_turns": 6,
            "total_cost_usd": 0.1425,
            "timestamp": "2026-10-28T09:00:45Z",
            "usage": {
                "input_tokens": 4000,
                "cache_read_input_tokens": 1000,
                "output_tokens": 1500,
            },
        },
    ]
    session_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    parsed = parse_claude_session_file(session_file, slug_map=slug_map)
    assert parsed is not None
    assert parsed.backend == "claude"
    assert parsed.session_id == "claude-sess-2"
    assert parsed.prompt_num == "0002"
    assert parsed.prompt_slug == "0002-forge"
    assert parsed.num_turns == 6
    assert parsed.input_tokens == 4000
    assert parsed.cached_input_tokens == 1000
    assert parsed.output_tokens == 1500
    assert parsed.total_tokens == 6500
    assert parsed.estimated_cost_usd == 0.1425
    assert parsed.duration_seconds == 45.0
    assert parsed.tool_calls_breakdown == {"Bash": 1, "Edit": 1}

    discovered = discover_claude_sessions(tmp_path / "claude_projects", slug_map=slug_map)
    assert len(discovered) == 1
    assert discovered[0].session_id == "claude-sess-2"

    direct = parse_claude_events_telemetry(events, default_session_id="fallback", slug_map=slug_map)
    assert direct is not None
    assert direct.session_id == "claude-sess-2"


def test_parse_codex_session_and_events(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    slug_map = build_canonical_slug_map(repo)

    codex_dir = tmp_path / "codex_sessions" / "2026" / "10" / "28"
    codex_dir.mkdir(parents=True)
    rollout_file = codex_dir / "rollout-2026-10-28-codex3.jsonl"

    events: list[dict[str, Any]] = [
        {
            "type": "thread.started",
            "thread_id": "thread-codex-3",
            "timestamp": "2026-10-28T10:00:00Z",
        },
        {
            "type": "user",
            "prompt": "Implement prompts/queued/0003-agent.md",
            "timestamp": "2026-10-28T10:00:05Z",
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "pytest",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "agent_message",
                "text": "Completed 0003-agent.md implementation.",
            },
        },
        {
            "type": "turn.completed",
            "timestamp": "2026-10-28T10:01:35Z",
            "usage": {
                "input_tokens": 3000,
                "cached_input_tokens": 500,
                "output_tokens": 800,
            },
        },
    ]
    rollout_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    parsed = parse_codex_session_file(rollout_file, slug_map=slug_map)
    assert parsed is not None
    assert parsed.backend == "codex"
    assert parsed.session_id == "thread-codex-3"
    assert parsed.prompt_num == "0003"
    assert parsed.prompt_slug == "0003-agent"
    assert parsed.input_tokens == 3000
    assert parsed.cached_input_tokens == 500
    assert parsed.output_tokens == 800
    assert parsed.total_tokens == 4300
    assert parsed.duration_seconds == 95.0
    assert parsed.tool_calls_breakdown == {"command_execution": 1}

    discovered = discover_codex_sessions(tmp_path / "codex_sessions", slug_map=slug_map)
    assert len(discovered) == 1
    assert discovered[0].session_id == "thread-codex-3"

    direct = parse_codex_events_telemetry(events, default_session_id="fallback", slug_map=slug_map)
    assert direct is not None
    assert direct.session_id == "thread-codex-3"


def test_parse_daemon_log_file_and_multi_agent_aggregation(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    slug_map = build_canonical_slug_map(repo)
    daemon_logs = repo / ".daemon" / "logs"
    daemon_logs.mkdir(parents=True)

    # Create a Claude log in .daemon/logs/
    claude_log = daemon_logs / "session-0001-bootstrap-claude.jsonl"
    claude_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": "daemon-claude-1",
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "session_id": "daemon-claude-1",
                        "duration_ms": 30000,
                        "num_turns": 3,
                        "total_cost_usd": 0.05,
                        "usage": {"input_tokens": 1000, "output_tokens": 400},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Create a Codex log in .daemon/logs/
    codex_log = daemon_logs / "session-0002-forge-codex.jsonl"
    codex_log.write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "daemon-codex-2"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 2000, "output_tokens": 600},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    parsed_claude = parse_daemon_log_file(claude_log, slug_map)
    assert len(parsed_claude) == 1
    assert parsed_claude[0].backend == "claude"
    assert parsed_claude[0].prompt_num == "0001"

    parsed_codex = parse_daemon_log_file(codex_log, slug_map)
    assert len(parsed_codex) == 1
    assert parsed_codex[0].backend == "codex"
    assert parsed_codex[0].prompt_num == "0002"

    # Now run collect_multi_agent_telemetry with isolated empty home dirs
    empty_dir = tmp_path / "empty"
    sessions, by_prompt = collect_multi_agent_telemetry(
        repo_root=repo,
        agy_cli_brain_dir=empty_dir,
        agy_ide_brain_dir=empty_dir,
        agy_summaries_dbs=(),
        claude_projects_dir=empty_dir,
        codex_sessions_dir=empty_dir,
        daemon_logs_dirs=(daemon_logs,),
    )
    assert len(sessions) == 2
    assert "0001" in by_prompt
    assert "0002" in by_prompt

    md_report = format_telemetry_markdown(sessions, by_prompt, slug_map)
    assert "# Orchestack Multi-Agent Telemetry Report" in md_report
    assert "`0001-bootstrap`" in md_report
    assert "`0002-forge`" in md_report
    assert "`claude`" in md_report
    assert "`codex`" in md_report

    json_report = json.loads(format_telemetry_json(sessions, by_prompt, slug_map))
    assert json_report["summary"]["total_sessions"] == 2
    assert "claude" in json_report["summary"]["by_backend"]
    assert "codex" in json_report["summary"]["by_backend"]
    assert "0001" in json_report["summary_by_prompt"]
    assert "0002" in json_report["summary_by_prompt"]


def test_legacy_telemetry_helpers(tmp_path: Path) -> None:
    repo = _setup_mock_repo(tmp_path)
    s1 = SessionTelemetry(
        session_id="sess-1",
        backend="agy",
        prompt_num="0001",
        prompt_slug="0001-bootstrap",
        total_steps=4,
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        estimated_cost_usd=0.0038,
        duration_seconds=60.0,
    )
    slug_map = build_canonical_slug_map(repo)
    md = export_markdown_table({"0001": [s1.to_dict()]}, slug_map)
    assert "| `0001` | `0001-bootstrap` |" in md

    all_dicts, by_prompt_dicts = collect_telemetry_dataset(repo_root=repo)
    assert isinstance(all_dicts, list)
    assert isinstance(by_prompt_dicts, dict)

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
"""Unit tests for Orchestack CLI subcommands and legacy flag compatibility."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestack import __version__
from orchestack.cli import app
from orchestack.telemetry.reporter import SessionTelemetry

runner = CliRunner()


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"orchestack {__version__}" in result.stdout

    result_flag = runner.invoke(app, ["--version"])
    assert result_flag.exit_code == 0
    assert f"orchestack {__version__}" in result_flag.stdout


def test_cli_init_scaffolds_project_and_updates_gitignore(tmp_path: Path) -> None:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "init",
            "--repo",
            str(repo),
            "--forge",
            "gitlab",
            "--agent",
            "claude",
            "--base-branch",
            "develop",
            "--check-cmd",
            "pytest -q",
        ],
    )
    assert result.exit_code == 0
    assert (repo / ".orchestack.toml").is_file()
    assert (repo / "prompts" / "queued").is_dir()
    assert (repo / "prompts" / "implemented").is_dir()
    assert (repo / "docs" / "learnings").is_dir()

    toml_text = (repo / ".orchestack.toml").read_text(encoding="utf-8")
    assert 'base_branch = "develop"' in toml_text
    assert 'check_cmd = "pytest -q"' in toml_text
    assert 'provider = "gitlab"' in toml_text
    assert 'backend = "claude"' in toml_text

    gitignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".daemon/" in gitignore_text
    assert "docs/PR_DESCRIPTION.md" in gitignore_text


def test_cli_status_human_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "status_repo"
    runner.invoke(
        app,
        ["init", "--repo", str(repo), "--forge", "github", "--agent", "claude"],
    )
    (repo / "prompts" / "implemented" / "0001-first.md").write_text("# 0001\n", encoding="utf-8")
    (repo / "prompts" / "queued" / "0002-second.md").write_text("# 0002\n", encoding="utf-8")
    (repo / "prompts" / "queued" / "0003-third.md").write_text("# 0003\n", encoding="utf-8")

    daemon_dir = repo / ".daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "daemon.lock").write_text("pid=1234\n", encoding="utf-8")

    class FakeForge:
        def list_open_prs(self) -> list[Any]:
            return [
                SimpleNamespace(
                    number=42,
                    title="0002-second",
                    url="https://github.com/org/repo/pull/42",
                    head_ref="orchestack/0002-second",
                    base_ref="main",
                    state="OPEN",
                )
            ]

        def get_ci_status(self, pr_num: int) -> Any:
            return SimpleNamespace(status="PASSED")

        def fetch_unprocessed_reviews(self, pr_num: int, processed: set[str]) -> Any:
            return (["rev1"], {"id1"})

    monkeypatch.setattr("orchestack.cli.create_forge_provider", lambda cfg: FakeForge())

    res_human = runner.invoke(app, ["status", "--repo", str(repo)])
    assert res_human.exit_code == 0
    assert "0002-second.md" in res_human.stdout
    assert "Queued          : 2 prompt(s)" in res_human.stdout
    assert "Implemented     : 1 prompt(s)" in res_human.stdout
    assert "LOCKED" in res_human.stdout
    assert "#42 [PASSED]" in res_human.stdout

    res_json = runner.invoke(app, ["status", "--repo", str(repo), "--json"])
    assert res_json.exit_code == 0
    payload = json.loads(res_json.stdout)
    assert payload["forge"] == "github"
    assert payload["agent"] == "claude"
    assert payload["queued_count"] == 2
    assert payload["implemented_count"] == 1
    assert payload["daemon_locked"] is True
    assert payload["open_prs"][0]["number"] == 42
    assert payload["open_prs"][0]["ci_status"] == "PASSED"


def test_cli_doctor_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "doctor_repo"
    runner.invoke(app, ["init", "--repo", str(repo), "--forge", "github", "--agent", "claude"])

    def fake_run_ok(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if "auth" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Logged in to github.com\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("orchestack.cli.subprocess.run", fake_run_ok)
    monkeypatch.setattr(
        "orchestack.cli.get_git_remote_url",
        lambda root: "https://github.com/org/repo.git",
    )
    monkeypatch.setattr("orchestack.cli.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "orchestack.agents.claude_code.ClaudeCodeAgent.check_available",
        lambda self: (True, "claude 1.0.0 (/usr/bin/claude)"),
    )

    res_ok = runner.invoke(app, ["doctor", "--repo", str(repo)])
    assert res_ok.exit_code == 0
    assert "[OK]" in res_ok.stdout

    res_json = runner.invoke(app, ["doctor", "--repo", str(repo), "--json"])
    assert res_json.exit_code == 0
    doc_payload = json.loads(res_json.stdout)
    assert doc_payload["ok"] is True

    # Simulate missing agent CLI binary -> exit code 1
    monkeypatch.setattr(
        "orchestack.agents.claude_code.ClaudeCodeAgent.check_available",
        lambda self: (False, "Claude Code CLI ('claude') not found on PATH."),
    )
    res_fail = runner.invoke(app, ["doctor", "--repo", str(repo)])
    assert res_fail.exit_code == 1
    assert "[FAIL]" in res_fail.stdout


def test_cli_run_subcommand_and_legacy_root_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_argv: list[list[str]] = []

    class FakeDaemonModule:
        @staticmethod
        def main() -> None:
            captured_argv.append(list(sys.argv))

    monkeypatch.setattr("orchestack.cli._load_daemon_runner_module", lambda: FakeDaemonModule)
    monkeypatch.setattr("orchestack.cli.configure_runtime", lambda cfg: None)

    res_sub = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(tmp_path),
            "--forge",
            "gitlab",
            "--agent",
            "codex",
            "--once",
            "--interval",
            "45",
            "--auto-whitelist",
            "--dry-run",
        ],
    )
    assert res_sub.exit_code == 0
    assert len(captured_argv) == 1
    cmd = captured_argv[0]
    assert "--repo" in cmd
    assert str(tmp_path.resolve()) in cmd
    assert "--forge" in cmd
    assert "gitlab" in cmd
    assert "--agent" in cmd
    assert "codex" in cmd
    assert "--once" in cmd
    assert "--poll-interval" in cmd
    assert "45" in cmd
    assert "--auto-whitelist" in cmd
    assert "--dry-run" in cmd

    # Verify legacy flag compatibility on root command (orchestack --once --dry-run)
    captured_argv.clear()
    res_legacy = runner.invoke(
        app,
        [
            "--repo",
            str(tmp_path),
            "--forge",
            "github",
            "--agent",
            "claude",
            "--once",
            "--dry-run",
        ],
    )
    assert res_legacy.exit_code == 0
    assert len(captured_argv) == 1
    legacy_cmd = captured_argv[0]
    assert "--once" in legacy_cmd
    assert "--dry-run" in legacy_cmd
    assert "claude" in legacy_cmd


def test_cli_telemetry_markdown_json_and_output_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_sessions = [
        SessionTelemetry(
            session_id="agy-001",
            backend="agy",
            model="gemini-2.5-pro",
            prompt_num="0001",
            prompt_slug="0001-bootstrap",
            total_steps=10,
            input_tokens=2000,
            output_tokens=500,
            total_tokens=2500,
            estimated_cost_usd=0.005,
            duration_seconds=90.0,
            tool_calls_breakdown={"run_command": 3},
        ),
        SessionTelemetry(
            session_id="claude-002",
            backend="claude",
            model="claude-sonnet-4",
            prompt_num="0002",
            prompt_slug="0002-forge",
            total_steps=8,
            input_tokens=4000,
            output_tokens=1000,
            total_tokens=5000,
            estimated_cost_usd=0.085,
            duration_seconds=60.0,
            tool_calls_breakdown={"Bash": 2},
        ),
        SessionTelemetry(
            session_id="codex-003",
            backend="codex",
            model="gpt-5-codex",
            prompt_num="0003",
            prompt_slug="0003-agent",
            total_steps=5,
            input_tokens=3000,
            output_tokens=700,
            total_tokens=3700,
            estimated_cost_usd=0.0073,
            duration_seconds=45.0,
            tool_calls_breakdown={"command_execution": 2},
        ),
    ]
    by_prompt = {
        "0001": [mock_sessions[0]],
        "0002": [mock_sessions[1]],
        "0003": [mock_sessions[2]],
    }
    slug_map = {
        "0001": "0001-bootstrap",
        "0002": "0002-forge",
        "0003": "0003-agent",
    }

    monkeypatch.setattr(
        "orchestack.cli.build_canonical_slug_map",
        lambda root: slug_map,
    )
    monkeypatch.setattr(
        "orchestack.cli.collect_multi_agent_telemetry",
        lambda repo_root: (mock_sessions, by_prompt),
    )

    res_md = runner.invoke(app, ["telemetry", "--repo", str(tmp_path), "--format", "markdown"])
    assert res_md.exit_code == 0
    assert "# Orchestack Multi-Agent Telemetry Report" in res_md.stdout
    assert "`agy`" in res_md.stdout
    assert "`claude`" in res_md.stdout
    assert "`codex`" in res_md.stdout

    out_file = tmp_path / "reports" / "telemetry.json"
    res_json = runner.invoke(
        app,
        [
            "telemetry",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
            "--output",
            str(out_file),
        ],
    )
    assert res_json.exit_code == 0
    assert out_file.is_file()
    parsed = json.loads(out_file.read_text(encoding="utf-8"))
    assert parsed["summary"]["total_sessions"] == 3
    assert set(parsed["summary"]["by_backend"].keys()) == {"agy", "claude", "codex"}

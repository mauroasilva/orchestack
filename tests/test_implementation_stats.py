# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for phase implementation statistics tracking (docs/IMPLEMENTATION_STATS.md)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestack.cli import app
from orchestack.config import load_repo_config
from orchestack.telemetry import (
    ModelPricing,
    PhaseImplementationStats,
    SessionTelemetry,
    TelemetryReport,
    build_canonical_slug_map,
    calculate_token_cost,
    format_duration,
    get_model_pricing,
    parse_duration_str,
    parse_existing_implementation_stats,
    render_implementation_stats_markdown,
    update_implementation_stats,
)

runner = CliRunner()


def _setup_repo_with_phases(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "prompts" / "implemented").mkdir(parents=True)
    (repo / "prompts" / "queued").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)

    (repo / "prompts" / "implemented" / "0001-core-package-and-repo-config.md").write_text(
        "# 0001\n", encoding="utf-8"
    )
    (repo / "prompts" / "implemented" / "0002-forge-provider-protocol-and-github.md").write_text(
        "# 0002\n", encoding="utf-8"
    )
    (repo / "prompts" / "queued" / "0003-gitlab-forge-provider.md").write_text(
        "# 0003\n", encoding="utf-8"
    )
    (repo / "docs" / "PLAN.md").write_text(
        "# Plan\n\n## 13. Phased delivery\n\n"
        "| Phase | Slug | Scope | Status |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| 0001 | `core-package-and-repo-config` | Core | Implemented |\n"
        "| 0002 | `forge-provider-protocol-and-github` | GitHub | Implemented |\n"
        "| 0003 | `gitlab-forge-provider` | GitLab | Queued |\n"
        "| 0004 | `agent-backend-protocol-and-antigravity` | Agent | Queued |\n",
        encoding="utf-8",
    )
    return repo


def test_repo_config_implementation_stats_path(tmp_path: Path) -> None:
    cfg = load_repo_config(tmp_path)
    assert cfg.implementation_stats_path == tmp_path.resolve() / "docs" / "IMPLEMENTATION_STATS.md"


def test_format_and_parse_duration() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(45.2) == "45s"
    assert format_duration(125.0) == "2m 5s"
    assert format_duration(3665.0) == "1h 1m 5s"

    assert parse_duration_str("45s") == 45.0
    assert parse_duration_str("2m 5s") == 125.0
    assert parse_duration_str("1h 1m 5s") == 3665.0
    assert parse_duration_str("**1h 1m 5s**") == 3665.0


def test_build_canonical_slug_map_includes_plan_section_13(tmp_path: Path) -> None:
    repo = _setup_repo_with_phases(tmp_path)
    slug_map = build_canonical_slug_map(repo)
    assert slug_map["0001"] == "0001-core-package-and-repo-config"
    assert slug_map["0002"] == "0002-forge-provider-protocol-and-github"
    assert slug_map["0003"] == "0003-gitlab-forge-provider"
    assert slug_map["0004"] == "0004-agent-backend-protocol-and-antigravity"


def test_render_implementation_stats_markdown_formatting_and_totals(tmp_path: Path) -> None:
    repo = _setup_repo_with_phases(tmp_path)
    slug_map = build_canonical_slug_map(repo)

    sessions = [
        SessionTelemetry(
            session_id="s1",
            backend="agy",
            model="gemini",
            prompt_num="0001",
            prompt_slug="0001-core-package-and-repo-config",
            input_tokens=100_000,
            cache_write_tokens=0,
            cache_read_tokens=10_000,
            output_tokens=25_000,
            total_tokens=135_000,
            estimated_cost_usd=1.25,
            duration_seconds=310.0,
        ),
        SessionTelemetry(
            session_id="s1-review",
            backend="agy",
            model="gemini",
            prompt_num="0001",
            prompt_slug="0001-core-package-and-repo-config",
            input_tokens=20_000,
            cache_write_tokens=0,
            cache_read_tokens=0,
            output_tokens=5_000,
            total_tokens=25_000,
            estimated_cost_usd=0.35,
            duration_seconds=55.0,
        ),
        SessionTelemetry(
            session_id="s2",
            backend="claude",
            model="claude-sonnet-4-5",
            prompt_num="0002",
            prompt_slug="0002-forge-provider-protocol-and-github",
            input_tokens=150_000,
            cache_write_tokens=20_000,
            cache_read_tokens=30_000,
            output_tokens=50_000,
            total_tokens=250_000,
            estimated_cost_usd=2.40,
            duration_seconds=3600.0,
        ),
    ]
    report = TelemetryReport(sessions=sessions, slug_map=slug_map)
    markdown = render_implementation_stats_markdown(report, repo_root=repo)

    assert "# Implementation Statistics" in markdown
    assert "pricing-grouped token breakdown" in markdown
    assert (
        "| Phase | Model | Implementation Time | `input_tokens` | `cache_write_tokens` "
        "| `cache_read_tokens` | `output_tokens` | Total Tokens | Total Cost |"
    ) in markdown
    assert (
        "| `0001-core-package-and-repo-config` | `gemini` | 6m 5s | 120,000 | 0 | 10,000 "
        "| 30,000 | 160,000 | $1.60 |"
    ) in markdown
    assert (
        "| `0002-forge-provider-protocol-and-github` | `claude-sonnet-4-5` | 1h 0m 0s "
        "| 150,000 | 20,000 | 30,000 | 50,000 | 250,000 | $2.40 |"
    ) in markdown
    assert (
        "| **Total** | **—** | **1h 6m 5s** | **270,000** | **20,000** | **40,000** "
        "| **80,000** | **410,000** | **$4.00** |"
    ) in markdown


def test_preserve_existing_stats_and_update_newer_phases(tmp_path: Path) -> None:
    repo = _setup_repo_with_phases(tmp_path)
    stats_path = repo / "docs" / "IMPLEMENTATION_STATS.md"

    # Seed historical stats where 0001 and 0002 were recorded on another machine
    initial_sessions = [
        SessionTelemetry(
            session_id="hist-1",
            backend="agy",
            model="gemini",
            prompt_num="0001",
            prompt_slug="0001-core-package-and-repo-config",
            input_tokens=400_000,
            output_tokens=100_000,
            total_tokens=500_000,
            estimated_cost_usd=3.50,
            duration_seconds=900.0,
        ),
        SessionTelemetry(
            session_id="hist-2",
            backend="agy",
            model="gemini",
            prompt_num="0002",
            prompt_slug="0002-forge-provider-protocol-and-github",
            input_tokens=240_000,
            output_tokens=60_000,
            total_tokens=300_000,
            estimated_cost_usd=2.00,
            duration_seconds=600.0,
        ),
    ]
    update_implementation_stats(repo_root=repo, output_path=stats_path, report=initial_sessions)
    assert stats_path.is_file()

    parsed = parse_existing_implementation_stats(stats_path)
    assert "0001" in parsed
    assert isinstance(parsed["0001"], PhaseImplementationStats)
    assert parsed["0001"].duration_seconds == 900.0
    assert parsed["0001"].input_tokens == 400_000
    assert parsed["0001"].output_tokens == 100_000
    assert parsed["0001"].total_tokens == 500_000
    assert parsed["0001"].estimated_cost_usd == 3.50

    # Now simulate a machine where 0001 logs are absent, 0002 has a new review turn (higher tokens),
    # and 0003 was just implemented.
    newer_sessions = [
        SessionTelemetry(
            session_id="new-2",
            backend="claude",
            model="claude-sonnet-4-5",
            prompt_num="0002",
            prompt_slug="0002-forge-provider-protocol-and-github",
            input_tokens=300_000,
            cache_write_tokens=20_000,
            cache_read_tokens=20_000,
            output_tokens=80_000,
            total_tokens=420_000,
            estimated_cost_usd=2.85,
            duration_seconds=780.0,
        ),
        SessionTelemetry(
            session_id="new-3",
            backend="codex",
            model="gpt-5-codex",
            prompt_num="0003",
            prompt_slug="0003-gitlab-forge-provider",
            input_tokens=120_000,
            cache_write_tokens=0,
            cache_read_tokens=30_000,
            output_tokens=30_000,
            total_tokens=180_000,
            estimated_cost_usd=1.15,
            duration_seconds=420.0,
        ),
    ]
    update_implementation_stats(repo_root=repo, output_path=stats_path, report=newer_sessions)
    updated_md = stats_path.read_text(encoding="utf-8")

    # 0001 preserved
    assert (
        "| `0001-core-package-and-repo-config` | `gemini` | 15m 0s | 400,000 | 0 | 0 "
        "| 100,000 | 500,000 | $3.50 |"
    ) in updated_md
    # 0002 updated to newer higher values
    assert (
        "| `0002-forge-provider-protocol-and-github` | `claude-sonnet-4-5` | 13m 0s "
        "| 300,000 | 20,000 | 20,000 | 80,000 | 420,000 | $2.85 |"
    ) in updated_md
    # 0003 added
    assert (
        "| `0003-gitlab-forge-provider` | `gpt-5-codex` | 7m 0s | 120,000 | 0 | 30,000 "
        "| 30,000 | 180,000 | $1.15 |"
    ) in updated_md
    # Total reflects 900 + 780 + 420 = 2100s (35m 0s), $7.50, 1,100,000 tokens
    assert (
        "| **Total** | **—** | **35m 0s** | **820,000** | **20,000** | **50,000** "
        "| **210,000** | **1,100,000** | **$7.50** |"
    ) in updated_md


def test_parse_legacy_three_column_implementation_stats() -> None:
    legacy_md = (
        "# Implementation Statistics\n\n"
        "| Phase | Implementation Time | Total Token Cost |\n"
        "| :--- | :---: | :---: |\n"
        "| `0001-core-package-and-repo-config` | 24m 18s | $0.42 (189,930 tokens) |\n"
        "| **Total** | **24m 18s** | **$0.42 (189,930 tokens)** |\n"
    )
    parsed = parse_existing_implementation_stats(legacy_md)
    assert "0001" in parsed
    assert parsed["0001"].phase_slug == "0001-core-package-and-repo-config"
    assert parsed["0001"].duration_seconds == 1458.0
    assert parsed["0001"].total_tokens == 189_930
    assert parsed["0001"].estimated_cost_usd == 0.42


def test_model_pricing_and_cost_calculation() -> None:
    claude_pricing = get_model_pricing("claude-sonnet-4-5", backend="claude")
    assert isinstance(claude_pricing, ModelPricing)
    assert claude_pricing.input_per_million == 3.00
    assert claude_pricing.cache_write_per_million == 3.75
    assert claude_pricing.cache_read_per_million == 0.30
    assert claude_pricing.output_per_million == 15.00

    # 1M input ($3.00) + 1M cache_write ($3.75) + 1M cache_read ($0.30) + 100k out ($1.50) = $8.55
    cost = calculate_token_cost(
        "claude-sonnet-4-5",
        backend="claude",
        input_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        output_tokens=100_000,
    )
    assert cost == pytest.approx(8.55)

    # Auto-computed cost on SessionTelemetry when estimated_cost_usd is omitted
    sess = SessionTelemetry(
        session_id="auto-cost",
        backend="claude",
        model="claude-sonnet-4-5",
        input_tokens=100_000,
        cache_write_tokens=50_000,
        cache_read_tokens=200_000,
        output_tokens=10_000,
    )
    assert sess.total_tokens == 360_000
    assert sess.cached_input_tokens == 200_000
    # 0.1 * 3.00 + 0.05 * 3.75 + 0.2 * 0.30 + 0.01 * 15.00 = 0.30 + 0.1875 + 0.06 + 0.15 = 0.6975
    assert sess.estimated_cost_usd == pytest.approx(0.6975)


def test_cli_telemetry_update_stats_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _setup_repo_with_phases(tmp_path)
    mock_sessions = [
        SessionTelemetry(
            session_id="cli-s1",
            backend="agy",
            model="gemini",
            prompt_num="0001",
            prompt_slug="0001-core-package-and-repo-config",
            input_tokens=60_000,
            output_tokens=20_000,
            total_tokens=80_000,
            estimated_cost_usd=0.95,
            duration_seconds=185.0,
        ),
        SessionTelemetry(
            session_id="cli-s2",
            backend="claude",
            model="claude-sonnet-4-5",
            prompt_num="0002",
            prompt_slug="0002-forge-provider-protocol-and-github",
            input_tokens=90_000,
            output_tokens=30_000,
            total_tokens=120_000,
            estimated_cost_usd=1.55,
            duration_seconds=245.0,
        ),
    ]
    by_prompt = {"0001": [mock_sessions[0]], "0002": [mock_sessions[1]]}
    monkeypatch.setattr(
        "orchestack.cli.collect_multi_agent_telemetry",
        lambda repo_root=None: (mock_sessions, by_prompt),
    )

    res = runner.invoke(app, ["telemetry", "--repo", str(repo), "--update-stats"])
    assert res.exit_code == 0
    stats_file = repo / "docs" / "IMPLEMENTATION_STATS.md"
    assert stats_file.is_file()
    content = stats_file.read_text(encoding="utf-8")
    assert "# Implementation Statistics" in content
    assert (
        "| `0001-core-package-and-repo-config` | `gemini` | 3m 5s | 60,000 | 0 | 0 "
        "| 20,000 | 80,000 | $0.95 |"
    ) in content
    assert (
        "| `0002-forge-provider-protocol-and-github` | `claude-sonnet-4-5` | 4m 5s "
        "| 90,000 | 0 | 0 | 30,000 | 120,000 | $1.55 |"
    ) in content
    assert (
        "| **Total** | **—** | **7m 10s** | **150,000** | **0** | **0** "
        "| **50,000** | **200,000** | **$2.50** |"
    ) in content


def test_daemon_runner_record_metrics_updates_implementation_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _setup_repo_with_phases(tmp_path)
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    daemon_runner: Any = importlib.import_module("daemon_runner")

    logs_dir = repo / "logs"
    metrics_file = logs_dir / "daemon_metrics.jsonl"
    stats_file = repo / "docs" / "IMPLEMENTATION_STATS.md"

    monkeypatch.setattr(daemon_runner, "REPO_ROOT", repo)
    monkeypatch.setattr(daemon_runner, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(daemon_runner, "DAEMON_METRICS_FILE", metrics_file)
    monkeypatch.setattr(daemon_runner, "IMPLEMENTATION_STATS_PATH", stats_file)

    daemon_runner.record_daemon_metrics(
        {
            "prompt_name": "0001-core-package-and-repo-config.md",
            "prompt_slug": "0001-core-package-and-repo-config",
            "branch": "gemini/0001-core-package-and-repo-config",
            "status": "pr_opened",
            "session_duration_seconds": 300.0,
            "total_steps": 15,
            "input_tokens_estimate": 60_000,
            "output_tokens_estimate": 15_000,
            "total_tokens_estimate": 75_000,
            "estimated_cost_usd": 0.85,
        }
    )

    assert metrics_file.is_file()
    assert stats_file.is_file()
    stats_md = stats_file.read_text(encoding="utf-8")
    assert (
        "| `0001-core-package-and-repo-config` | `gemini` | 5m 0s | 60,000 | 0 | 0 "
        "| 15,000 | 75,000 | $0.85 |"
    ) in stats_md


def test_repository_implementation_stats_doc_exists_and_has_phases_0001_to_0007() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    stats_path = repo_root / "docs" / "IMPLEMENTATION_STATS.md"
    assert stats_path.is_file(), "docs/IMPLEMENTATION_STATS.md must exist in the repository"
    content = stats_path.read_text(encoding="utf-8")
    assert content.startswith("# Implementation Statistics")
    assert (
        "| Phase | Model | Implementation Time | `input_tokens` | `cache_write_tokens` "
        "| `cache_read_tokens` | `output_tokens` | Total Tokens | Total Cost |"
    ) in content
    assert "| **Total** |" in content

    parsed = parse_existing_implementation_stats(stats_path)
    for phase_num in ("0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"):
        assert phase_num in parsed, f"Phase {phase_num} missing from docs/IMPLEMENTATION_STATS.md"
        assert parsed[phase_num].duration_seconds > 0
        assert parsed[phase_num].input_tokens > 0
        assert parsed[phase_num].output_tokens > 0
        assert parsed[phase_num].total_tokens > 0
        assert parsed[phase_num].estimated_cost_usd > 0

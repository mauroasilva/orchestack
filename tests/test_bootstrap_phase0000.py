# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for Phase 0008: Repository State Detection & Phase 0000 Bootstrap Interview Loop."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daemon_runner  # type: ignore[import-not-found]  # noqa: E402

from orchestack.agents.base import AgentSessionResult  # noqa: E402
from orchestack.bootstrap import (  # noqa: E402
    BOOTSTRAP_STATE_FILENAME,
    PHASE_0000_FILENAME,
    PHASE_0000_QUESTIONS_FILENAME,
    PHASE_0000_SLUG,
    RepoBootstrapState,
    build_phase_0000_interview_prompt,
    check_phase_0000_skeleton_files,
    detect_repo_bootstrap_state,
    ensure_base_branch_initialized,
    extract_phase_0000_questions,
    format_phase_0000_completion_comment,
    format_phase_0000_questions_comment,
    has_queued_implementation_prompts,
    is_phase_0000_complete,
    is_phase_0000_interview_pending,
    is_phase_0000_prompt,
    scaffold_phase_0000_prompt,
)
from orchestack.cli import app  # noqa: E402
from orchestack.config import RepoConfig, configure_runtime  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def restore_daemon_runner_globals() -> Iterator[None]:
    """Ensure `daemon_runner` module globals are restored after each test."""
    orig = {
        "REPO_ROOT": daemon_runner.REPO_ROOT,
        "QUEUED_DIR": daemon_runner.QUEUED_DIR,
        "IMPLEMENTED_DIR": daemon_runner.IMPLEMENTED_DIR,
        "LEARNINGS_DIR": daemon_runner.LEARNINGS_DIR,
        "PR_DESC_PATH": daemon_runner.PR_DESC_PATH,
        "BRIEF_PATH": daemon_runner.BRIEF_PATH,
        "PHASE_0000_QUESTIONS_FILE": daemon_runner.PHASE_0000_QUESTIONS_FILE,
        "DAEMON_DIR": daemon_runner.DAEMON_DIR,
        "LOCK_FILE": daemon_runner.LOCK_FILE,
        "REVIEW_STATE_FILE": daemon_runner.REVIEW_STATE_FILE,
        "LOGS_DIR": daemon_runner.LOGS_DIR,
        "PR_REVIEWS_FILE": daemon_runner.PR_REVIEWS_FILE,
        "DAEMON_METRICS_FILE": daemon_runner.DAEMON_METRICS_FILE,
        "BASE_BRANCH": daemon_runner.BASE_BRANCH,
        "BRANCH_PREFIX": daemon_runner.BRANCH_PREFIX,
        "CHECK_CMD": daemon_runner.CHECK_CMD,
        "REQUIRE_LEARNINGS": daemon_runner.REQUIRE_LEARNINGS,
        "REQUIRE_PR_DESCRIPTION": daemon_runner.REQUIRE_PR_DESCRIPTION,
        "ACTIVE_CONFIG": daemon_runner.ACTIVE_CONFIG,
        "SAFE_BASELINE_ALLOW": list(daemon_runner.SAFE_BASELINE_ALLOW),
    }
    try:
        yield
    finally:
        for key, val in orig.items():
            setattr(daemon_runner, key, val)


# ==============================================================================
# 1. Repository Bootstrap State Detection Tests
# ==============================================================================


def test_detect_repo_bootstrap_state_empty_repo(tmp_path: Path) -> None:
    """Empty directory or freshly initialized layout without BRIEF/PLAN/prompts is EMPTY_REPO."""
    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.EMPTY_REPO

    # Even with empty prompts/queued and prompts/implemented directories
    (tmp_path / "prompts" / "queued").mkdir(parents=True)
    (tmp_path / "prompts" / "implemented").mkdir(parents=True)
    (tmp_path / "docs").mkdir(parents=True)
    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.EMPTY_REPO

    # Empty docs/BRIEF.md (whitespace only) is still EMPTY_REPO
    (tmp_path / "docs" / "BRIEF.md").write_text("   \n", encoding="utf-8")
    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.EMPTY_REPO


def test_detect_repo_bootstrap_state_brief_only(tmp_path: Path) -> None:
    """Repository with non-empty docs/BRIEF.md and no 0001+ queued prompts is BRIEF_ONLY."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "BRIEF.md").write_text("# Project Brief\nBuild a CLI tool.\n", encoding="utf-8")

    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.BRIEF_ONLY

    # Even if 0000-bootstrap-plan-and-prompts.md is in prompts/queued/, still BRIEF_ONLY
    queued_dir = tmp_path / "prompts" / "queued"
    queued_dir.mkdir(parents=True)
    (queued_dir / PHASE_0000_FILENAME).write_text("# Phase 0000\n", encoding="utf-8")
    assert has_queued_implementation_prompts(tmp_path) is False
    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.BRIEF_ONLY


def test_detect_repo_bootstrap_state_queue_ready(tmp_path: Path) -> None:
    """Repository with 0001+ queued prompts or completed bootstrap is QUEUE_READY."""
    queued_dir = tmp_path / "prompts" / "queued"
    queued_dir.mkdir(parents=True)
    (queued_dir / "0001-initial-architecture.md").write_text("# 0001\n", encoding="utf-8")

    assert has_queued_implementation_prompts(tmp_path) is True
    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.QUEUE_READY


def test_detect_repo_bootstrap_state_queue_ready_when_all_implemented(tmp_path: Path) -> None:
    """Repository where all prompts have been moved to prompts/implemented/ is QUEUE_READY."""
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True)
    (impl_dir / "0001-initial.md").write_text("# 0001\n", encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "PLAN.md").write_text("# Plan\n", encoding="utf-8")

    assert detect_repo_bootstrap_state(tmp_path) == RepoBootstrapState.QUEUE_READY


def test_cli_status_reports_bootstrap_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`orchestack status` reports bootstrap_state in both human and JSON output."""
    repo = tmp_path / "brief_repo"
    runner.invoke(app, ["init", "--repo", str(repo), "--forge", "local", "--agent", "claude"])
    (repo / "docs" / "BRIEF.md").write_text("# Brief\nMy new service.\n", encoding="utf-8")

    class FakeForge:
        def list_open_prs(self) -> list[Any]:
            return []

    monkeypatch.setattr("orchestack.cli.create_forge_provider", lambda cfg: FakeForge())

    res_human = runner.invoke(app, ["status", "--repo", str(repo)])
    assert res_human.exit_code == 0
    assert "Bootstrap state : brief_only" in res_human.stdout

    res_json = runner.invoke(app, ["status", "--repo", str(repo), "--json"])
    assert res_json.exit_code == 0
    payload = json.loads(res_json.stdout)
    assert payload["bootstrap_state"] == "brief_only"


# ==============================================================================
# 2. Phase 0000 Prompt Scaffolding & Base Branch Initialization Tests
# ==============================================================================


def test_scaffold_phase_0000_prompt_brief_only_and_empty_repo(tmp_path: Path) -> None:
    """scaffold_phase_0000_prompt generates 0000-bootstrap-plan-and-prompts.md and state file."""
    # Case A: EMPTY_REPO
    prompt_path = scaffold_phase_0000_prompt(tmp_path, RepoBootstrapState.EMPTY_REPO)
    assert prompt_path == tmp_path / "prompts" / "queued" / PHASE_0000_FILENAME
    assert prompt_path.is_file()
    assert is_phase_0000_prompt(prompt_path) is True

    text_empty = prompt_path.read_text(encoding="utf-8")
    assert "`empty_repo`" in text_empty
    assert "No `docs/BRIEF.md` was found" in text_empty
    assert ".daemon/phase0000_questions.md" in text_empty
    assert "docs/PR_DESCRIPTION.md" in text_empty

    state_file = tmp_path / ".daemon" / BOOTSTRAP_STATE_FILENAME
    assert state_file.is_file()
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_data["bootstrap_state"] == "empty_repo"
    assert state_data["phase_0000_prompt"] == f"prompts/queued/{PHASE_0000_FILENAME}"
    assert "docs/PLAN.md" in state_data["missing_skeleton_files"]

    # Case B: BRIEF_ONLY (remove prompt first to re-scaffold for BRIEF_ONLY)
    prompt_path.unlink()
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "BRIEF.md").write_text("# Brief\nBuild X.\n", encoding="utf-8")

    prompt_brief = scaffold_phase_0000_prompt(tmp_path)
    text_brief = prompt_brief.read_text(encoding="utf-8")
    assert "`brief_only`" in text_brief
    assert "`docs/BRIEF.md` exists" in text_brief

    all_present, missing = check_phase_0000_skeleton_files(tmp_path, RepoBootstrapState.BRIEF_ONLY)
    assert all_present is False
    assert "docs/BRIEF.md" not in missing
    assert "docs/PLAN.md" in missing


def test_ensure_base_branch_initialized_creates_initial_commit_when_head_missing(
    tmp_path: Path,
) -> None:
    """ensure_base_branch_initialized initializes git and commits when HEAD is missing."""
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="fatal")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    ensure_base_branch_initialized(tmp_path, "main", run_cmd_fn=fake_run_cmd)
    assert (tmp_path / ".gitignore").is_file()
    assert ["git", "init", "-b", "main"] in calls
    assert ["git", "add", ".gitignore"] in calls
    assert any(cmd[:3] == ["git", "commit", "-m"] for cmd in calls)


def test_ensure_base_branch_initialized_noop_when_head_exists(tmp_path: Path) -> None:
    """ensure_base_branch_initialized is a no-op when git rev-parse --verify HEAD succeeds."""
    (tmp_path / ".git").mkdir()
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="abc1234\n", stderr="")

    ensure_base_branch_initialized(tmp_path, "main", run_cmd_fn=fake_run_cmd)
    assert calls == [["git", "rev-parse", "--verify", "HEAD"]]


def test_daemon_get_next_prompt_bootstraps_when_enabled(tmp_path: Path) -> None:
    """daemon_runner.get_next_prompt(bootstrap_if_needed=True) scaffolds Phase 0000."""
    cfg = RepoConfig(repo_root=tmp_path)
    configure_runtime(cfg)
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs" / "BRIEF.md").write_text("# Brief\n", encoding="utf-8")

    # Without bootstrap_if_needed, returns None when queue is empty
    assert daemon_runner.get_next_prompt() is None

    with patch("daemon_runner.ensure_base_branch_initialized") as mock_init:
        prompt = daemon_runner.get_next_prompt(bootstrap_if_needed=True)
        assert prompt is not None
        assert prompt.name == PHASE_0000_FILENAME
        assert prompt.is_file()
        mock_init.assert_called_once()


# ==============================================================================
# 3. Initial Phase 0000 PR Creation & Questions Comment Tests
# ==============================================================================


def test_execute_backend_prompt_session_pauses_for_phase_0000_interview(
    tmp_path: Path,
) -> None:
    """Backend session returns True after turn 1 when Phase 0000 questions are pending."""
    cfg = RepoConfig(repo_root=tmp_path)
    configure_runtime(cfg)
    daemon_runner.configure_runtime(cfg)
    prompt_path = scaffold_phase_0000_prompt(tmp_path, RepoBootstrapState.EMPTY_REPO)

    fake_backend = MagicMock()
    fake_backend.name = "claude"
    fake_backend.detect_permission_denials.return_value = []

    def side_effect_run(**kwargs: Any) -> AgentSessionResult:
        # Simulate agent writing summary of understanding + questions during turn 1
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "PR_DESCRIPTION.md").write_text(
            "## Summary of Understanding\nBuilding a distributed job runner.\n",
            encoding="utf-8",
        )
        (tmp_path / ".daemon").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".daemon" / PHASE_0000_QUESTIONS_FILENAME).write_text(
            "1. Which language and runtime should we target?\n"
            "2. Which storage backend is preferred?\n",
            encoding="utf-8",
        )
        return AgentSessionResult(
            session_id="sess-0000",
            exit_code=0,
            completed_normally=True,
            error_message=None,
            last_response="Created initial summary and questions.",
        )

    fake_backend.run_turn.side_effect = side_effect_run

    log_file = tmp_path / ".daemon" / "logs" / "session-0000.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    ok = daemon_runner._execute_backend_prompt_session(
        backend=fake_backend,
        prompt_path=prompt_path,
        log_file=log_file,
        title="Orchestack - Phase 0000 Bootstrap",
        kickoff_prompt="Bootstrap Phase 0000",
        continuation_prompt="Continue Phase 0000",
        resume=False,
        custom_prompt=None,
    )
    assert ok is True
    # Must NOT loop for a second turn since Phase 0000 interview is pending
    assert fake_backend.run_turn.call_count == 1


def test_ensure_pr_opened_posts_summary_body_and_numbered_questions_comment(
    tmp_path: Path,
) -> None:
    """ensure_pr_opened sets PR body from PR_DESCRIPTION.md and posts numbered questions comment."""
    cfg = RepoConfig(repo_root=tmp_path)
    configure_runtime(cfg)
    prompt_path = scaffold_phase_0000_prompt(tmp_path, RepoBootstrapState.BRIEF_ONLY)

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    summary_body = "## Summary of Understanding\nWe are building an event-driven webhook proxy."
    (tmp_path / "docs" / "PR_DESCRIPTION.md").write_text(summary_body, encoding="utf-8")

    questions_text = (
        "1. What authentication scheme should incoming webhooks use?\n"
        "2. Should retry state be persisted in SQLite or PostgreSQL?"
    )
    (tmp_path / ".daemon" / PHASE_0000_QUESTIONS_FILENAME).write_text(
        questions_text, encoding="utf-8"
    )

    fake_forge = MagicMock()
    fake_forge.create_pr_with_body.return_value = subprocess.CompletedProcess(
        ["gh", "pr", "create"],
        returncode=0,
        stdout="https://github.com/org/repo/pull/10\n",
        stderr="",
    )

    with (
        patch("daemon_runner.get_branch_pr_status", return_value=None),
        patch(
            "daemon_runner.run_cmd",
            return_value=subprocess.CompletedProcess(["git", "push"], 0, stdout="", stderr=""),
        ),
        patch("daemon_runner._make_github_forge", return_value=fake_forge),
        patch("daemon_runner.send_notification"),
    ):
        ok = daemon_runner.ensure_pr_opened("gh", f"orchestack/{PHASE_0000_SLUG}", prompt_path)
        assert ok is True

    fake_forge.create_pr_with_body.assert_called_once()
    create_kwargs = fake_forge.create_pr_with_body.call_args.kwargs
    assert create_kwargs["body"] == summary_body

    fake_forge.post_pr_comment.assert_called_once()
    posted_pr_num, posted_comment = fake_forge.post_pr_comment.call_args.args
    assert posted_pr_num == 10
    assert "Phase 0000 Bootstrap — Clarifying Questions" in posted_comment
    assert "1. What authentication scheme should incoming webhooks use?" in posted_comment
    assert "2. Should retry state be persisted in SQLite or PostgreSQL?" in posted_comment


# ==============================================================================
# 4. Multi-Round PR Comment Interview Loop Tests
# ==============================================================================


def test_multi_round_phase_0000_interview_loop_and_finalization(tmp_path: Path) -> None:
    """Test Round 1 (follow-up questions) and Round 2 (finalization into PLAN + 0001..000N)."""
    cfg = RepoConfig(repo_root=tmp_path)
    configure_runtime(cfg)
    prompt_path = scaffold_phase_0000_prompt(tmp_path, RepoBootstrapState.BRIEF_ONLY)
    branch_name = f"orchestack/{PHASE_0000_SLUG}"

    # Verify build_review_feedback_prompt produces Phase 0000 interview prompt while in queued/
    user_comments_round_1 = [
        {
            "id": "c-101",
            "author": "owner",
            "body": "1. Use HMAC-SHA256 signatures.\n2. Use SQLite for local persistence.",
        }
    ]
    interview_prompt = daemon_runner.build_review_feedback_prompt(
        10,
        f"feat({PHASE_0000_SLUG}): implement {PHASE_0000_SLUG}",
        user_comments_round_1,
        prompt_path=prompt_path,
    )
    assert "Phase 0000 Bootstrap Interview response(s)" in interview_prompt
    assert "HMAC-SHA256" in interview_prompt

    fake_forge = MagicMock()
    cmd_calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd_calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--short"]:
            # Return different SHA before and after session to simulate new commit
            sha = (
                "1111111"
                if len([c for c in cmd_calls if c[:2] == ["git", "rev-parse"]]) % 2 == 1
                else "2222222"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{sha}\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # --- Round 1: Agent updates PR_DESCRIPTION.md and asks 1 follow-up question ---
    def execute_round_1(**kwargs: Any) -> bool:
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "PR_DESCRIPTION.md").write_text(
            "## Summary of Understanding (Updated)\n- Auth: HMAC-SHA256\n- Storage: SQLite\n",
            encoding="utf-8",
        )
        (tmp_path / ".daemon" / PHASE_0000_QUESTIONS_FILENAME).write_text(
            "1. Should dead-letter events be exposed via a CLI subcommand?\n",
            encoding="utf-8",
        )
        return True

    with (
        patch("daemon_runner.get_current_branch", return_value=branch_name),
        patch("daemon_runner.run_cmd", side_effect=fake_run_cmd),
        patch("daemon_runner.execute_prompt_session", side_effect=execute_round_1),
        patch("daemon_runner.extract_latest_agent_summary", return_value=None),
        patch("daemon_runner._make_github_forge", return_value=fake_forge),
        patch("daemon_runner.save_processed_comment_ids"),
        patch("daemon_runner.save_pr_tracking_data"),
    ):
        ok1 = daemon_runner.address_pr_feedback(
            gh_bin="gh",
            agy_bin="agy",
            pr_number=10,
            pr_title="Phase 0000",
            branch_name=branch_name,
            prompt_path=prompt_path,
            new_comments=user_comments_round_1,
        )
        assert ok1 is True

    # Verify PR description was updated via `gh pr edit 10 --body ...`
    assert any(cmd[:4] == ["gh", "pr", "edit", "10"] for cmd in cmd_calls)
    # Verify follow-up questions comment was posted and Phase 0000 is not yet complete
    assert is_phase_0000_complete(tmp_path, prompt_path) is False
    fake_forge.post_pr_comment.assert_called_once()
    _, round1_comment = fake_forge.post_pr_comment.call_args.args
    assert "Phase 0000 Bootstrap — Clarifying Questions" in round1_comment
    assert "1. Should dead-letter events be exposed via a CLI subcommand?" in round1_comment

    # --- Round 2: User answers follow-up; agent finalizes PLAN.md, 0001..0002, and moves 0000 ---
    fake_forge.reset_mock()
    cmd_calls.clear()
    user_comments_round_2 = [
        {
            "id": "c-102",
            "author": "owner",
            "body": "1. Yes, add `webhook dlq list` and `webhook dlq replay`.",
        }
    ]

    def execute_round_2(**kwargs: Any) -> bool:
        (tmp_path / "docs" / "PLAN.md").write_text(
            "# Master Plan\n\n## §13 Prompt Index\n- 0001\n- 0002\n", encoding="utf-8"
        )
        (tmp_path / "docs" / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
        (tmp_path / "docs" / "LICENSE-HEADER.md").write_text(
            "# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8"
        )
        (tmp_path / "README.md").write_text("# Webhook Proxy\n", encoding="utf-8")
        (tmp_path / "prompts" / "queued" / "0001-hmac-receiver.md").write_text(
            "# Phase 0001\n", encoding="utf-8"
        )
        (tmp_path / "prompts" / "queued" / "0002-sqlite-dlq-cli.md").write_text(
            "# Phase 0002\n", encoding="utf-8"
        )
        impl_dir = tmp_path / "prompts" / "implemented"
        impl_dir.mkdir(parents=True, exist_ok=True)
        prompt_path.rename(impl_dir / PHASE_0000_FILENAME)
        learnings_dir = tmp_path / "docs" / "learnings"
        learnings_dir.mkdir(parents=True, exist_ok=True)
        (learnings_dir / f"{PHASE_0000_SLUG}-learnings.md").write_text(
            "## Summary\nBootstrapped plan and prompts.\n", encoding="utf-8"
        )
        (tmp_path / "docs" / "PR_DESCRIPTION.md").write_text(
            "## Summary\nFinalized master plan and queued phases 0001-0002.\n",
            encoding="utf-8",
        )
        q_file = tmp_path / ".daemon" / PHASE_0000_QUESTIONS_FILENAME
        if q_file.exists():
            q_file.unlink()
        return True

    with (
        patch("daemon_runner.get_current_branch", return_value=branch_name),
        patch("daemon_runner.run_cmd", side_effect=fake_run_cmd),
        patch("daemon_runner.execute_prompt_session", side_effect=execute_round_2),
        patch("daemon_runner.extract_latest_agent_summary", return_value="Done"),
        patch("daemon_runner._make_github_forge", return_value=fake_forge),
        patch("daemon_runner.save_processed_comment_ids"),
        patch("daemon_runner.save_pr_tracking_data"),
    ):
        ok2 = daemon_runner.address_pr_feedback(
            gh_bin="gh",
            agy_bin="agy",
            pr_number=10,
            pr_title="Phase 0000",
            branch_name=branch_name,
            prompt_path=prompt_path,
            new_comments=user_comments_round_2,
        )
        assert ok2 is True

    assert is_phase_0000_complete(tmp_path) is True
    assert is_phase_0000_interview_pending(tmp_path) is False
    fake_forge.post_pr_comment.assert_called_once()
    _, completion_comment = fake_forge.post_pr_comment.call_args.args
    assert "Phase 0000 Bootstrap Complete" in completion_comment
    assert "`prompts/queued/0001-hmac-receiver.md`" in completion_comment
    assert "`prompts/queued/0002-sqlite-dlq-cli.md`" in completion_comment
    assert "ready for review and merge" in completion_comment


def test_wait_for_pr_merge_exempts_phase_0000_from_circuit_breaker_and_skips_ready_until_complete(
    tmp_path: Path,
) -> None:
    """wait_for_pr_merge does not trip feedback circuit breaker or mark ready during interview."""
    cfg = RepoConfig(repo_root=tmp_path)
    configure_runtime(cfg)
    prompt_path = scaffold_phase_0000_prompt(tmp_path, RepoBootstrapState.BRIEF_ONLY)
    branch_name = f"orchestack/{PHASE_0000_SLUG}"

    open_prs_seq = [
        # Poll 1: open PR with unhandled comment while attempts (5) >= max_auto_heal_attempts (3)
        [{"number": 10, "title": "Phase 0000", "headRefName": branch_name}],
        # Poll 2: open PR with passing CI checks while Phase 0000 is still in queued/
        [{"number": 10, "title": "Phase 0000", "headRefName": branch_name}],
        # Poll 3: PR merged (no open PRs)
        [],
    ]
    feedback_seq = [
        [{"id": "c-1", "author": "owner", "body": "Answer 1"}],
        [],
    ]

    with (
        patch("daemon_runner.get_open_prs", side_effect=open_prs_seq),
        patch("daemon_runner.fetch_unhandled_pr_feedback", side_effect=feedback_seq),
        # Even though review_feedback_attempts is 5 (>= max 3), circuit breaker must NOT trip
        patch("daemon_runner.get_review_feedback_attempts", return_value=5),
        patch("daemon_runner.find_prompt_path_for_branch", return_value=prompt_path),
        patch("daemon_runner.address_pr_feedback", return_value=True) as mock_address,
        patch(
            "daemon_runner.get_pr_checks",
            return_value=[{"name": "ci", "bucket": "pass"}],
        ),
        patch("daemon_runner.mark_pr_ci_green") as mock_mark_green,
        patch("daemon_runner.send_notification") as mock_notify,
        patch("daemon_runner._interruptible_sleep", return_value=False),
    ):
        elapsed = daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=1,
            max_interval=5,
            max_auto_heal_attempts=3,
            agy_bin="agy",
        )
        assert elapsed >= 0.0
        mock_address.assert_called_once()
        # Because Phase 0000 is still in progress (not yet moved to implemented/),
        # mark_pr_ci_green and "PR Ready for Merge" notification must NOT be called!
        mock_mark_green.assert_not_called()
        mock_notify.assert_not_called()


def test_helper_formatting_and_fallback_question_extraction(tmp_path: Path) -> None:
    """Verify fallback extraction of numbered questions from agent response text."""
    summary_with_questions = (
        "I have reviewed the repository.\n\n"
        "### Clarifying Questions\n"
        "1. What is the target Python version?\n"
        "2. Should we include Docker packaging?\n"
    )
    extracted = extract_phase_0000_questions(tmp_path, summary_with_questions)
    assert extracted is not None
    assert "1. What is the target Python version?" in extracted
    assert "2. Should we include Docker packaging?" in extracted

    formatted = format_phase_0000_questions_comment(extracted, iteration=2)
    assert "(Round 2)" in formatted
    assert "1. What is the target Python version?" in formatted

    prompt_text = build_phase_0000_interview_prompt(
        tmp_path,
        tmp_path / "prompts" / "queued" / PHASE_0000_FILENAME,
        42,
        ["#### 1. General comment from @alice:\n> Use Python 3.12.\n"],
    )
    assert "Pull Request #42" in prompt_text
    assert "Use Python 3.12." in prompt_text

    completion = format_phase_0000_completion_comment(tmp_path)
    assert "Phase 0000 Bootstrap Complete" in completion

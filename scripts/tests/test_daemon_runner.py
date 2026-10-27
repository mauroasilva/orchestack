"""Standalone regression unit test suite for BlueTeamHarness autonomous daemon runner.

Tests cover:
- macOS desktop notifications and sounds
- Git staging & untracked file safety pre-checks
- Auto-healing retry budget & circuit breaker attempt counters
- CI failure detection & log fetching
- PR feedback extraction & filtering
- Session telemetry extraction & structured metrics logging
- Network connectivity watchdog
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts directory is on sys.path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daemon_runner  # noqa: E402


class MockCompletedProcess:
    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ==============================================================================
# 1. macOS Notification Tests
# ==============================================================================


def test_send_notification_invokes_osascript() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MockCompletedProcess(returncode=0)
        success = daemon_runner.send_notification(
            title="CI Green",
            message="Ready for merge",
            sound="Hero",
            subtitle="PR #42",
        )
        assert success is True
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "osascript"
        assert "-e" in cmd
        script = cmd[cmd.index("-e") + 1]
        assert 'display notification "Ready for merge"' in script
        assert 'with title "CI Green"' in script
        assert 'subtitle "PR #42"' in script
        assert 'sound name "Hero"' in script


def test_send_notification_handles_failure_gracefully() -> None:
    with patch("subprocess.run", side_effect=Exception("osascript error")):
        success = daemon_runner.send_notification(
            title="Error Alert",
            message="Something went wrong",
        )
        assert success is False


# ==============================================================================
# 2. Git Staging & Untracked File Safety Pre-check Tests
# ==============================================================================


def test_verify_git_staging_safety_clean(tmp_path: Path) -> None:
    with patch("daemon_runner.run_cmd") as mock_run_cmd:
        mock_run_cmd.side_effect = [
            MockCompletedProcess(returncode=0, stdout=""),  # git ls-files --others --ignored
            MockCompletedProcess(returncode=0, stdout=""),  # git status --porcelain
        ]
        safe, issues = daemon_runner.verify_git_staging_safety(repo_root=tmp_path)
        assert safe is True
        assert issues == []


def test_verify_git_staging_safety_catches_ignored_fixture(tmp_path: Path) -> None:
    with patch("daemon_runner.run_cmd") as mock_run_cmd:
        mock_run_cmd.side_effect = [
            MockCompletedProcess(
                returncode=0,
                stdout="tests/fixtures/samples/sample.pcap\n.DS_Store\n",
            ),
            MockCompletedProcess(
                returncode=0,
                stdout=".gitignore:12:*.pcap\ttests/fixtures/samples/sample.pcap\n",
            ),
            MockCompletedProcess(returncode=0, stdout=""),  # git status --porcelain
        ]
        safe, issues = daemon_runner.verify_git_staging_safety(repo_root=tmp_path)
        assert safe is False
        assert len(issues) == 1
        assert "tests/fixtures/samples/sample.pcap" in issues[0]
        assert "*.pcap" in issues[0]


def test_verify_git_staging_safety_catches_untracked_source(tmp_path: Path) -> None:
    with patch("daemon_runner.run_cmd") as mock_run_cmd:
        mock_run_cmd.side_effect = [
            MockCompletedProcess(returncode=0, stdout=""),  # git ls-files
            MockCompletedProcess(
                returncode=0,
                stdout="?? src/evals/new_heuristic.py\n?? scratch/temp.txt\n",
            ),
        ]
        safe, issues = daemon_runner.verify_git_staging_safety(repo_root=tmp_path)
        assert safe is False
        assert len(issues) == 1
        assert "src/evals/new_heuristic.py" in issues[0]


# ==============================================================================
# 3. Retry Budget & Circuit Breaker Tracking Tests
# ==============================================================================


def test_pr_tracking_data_and_circuit_breaker(tmp_path: Path) -> None:
    test_reviews_file = tmp_path / "pr_reviews.json"
    with patch("daemon_runner.PR_REVIEWS_FILE", test_reviews_file):
        # Initial state: 0 attempts
        assert daemon_runner.get_ci_heal_attempts(99) == 0
        assert daemon_runner.get_review_feedback_attempts(99) == 0
        assert daemon_runner.load_processed_comment_ids(99) == set()

        # Increment CI heal attempts
        attempt1 = daemon_runner.increment_ci_heal_attempts(99)
        attempt2 = daemon_runner.increment_ci_heal_attempts(99)
        assert attempt1 == 1
        assert attempt2 == 2
        assert daemon_runner.get_ci_heal_attempts(99) == 2

        # Increment review feedback attempts
        rf_attempt = daemon_runner.increment_review_feedback_attempts(99)
        assert rf_attempt == 1
        assert daemon_runner.get_review_feedback_attempts(99) == 1

        # Processed comments preservation
        daemon_runner.save_processed_comment_ids(99, ["comment-101", "comment-102"])
        processed = daemon_runner.load_processed_comment_ids(99)
        assert "comment-101" in processed
        assert "comment-102" in processed

        # Re-check that attempt counters survived saving comment IDs
        assert daemon_runner.get_ci_heal_attempts(99) == 2
        assert daemon_runner.get_review_feedback_attempts(99) == 1


# ==============================================================================
# 4. CI Failure Detection Tests
# ==============================================================================


def test_fetch_latest_failed_ci_run_success() -> None:
    fake_runs = [
        {
            "databaseId": 98765,
            "status": "completed",
            "conclusion": "failure",
            "headSha": "abcdef1",
            "workflowName": "CI",
            "url": "https://github.com/org/repo/actions/runs/98765",
        }
    ]
    with patch("daemon_runner.run_cmd") as mock_run_cmd:
        mock_run_cmd.side_effect = [
            MockCompletedProcess(returncode=0, stdout=json.dumps(fake_runs)),
            MockCompletedProcess(returncode=0, stdout="Error: tests failed at line 42"),
        ]
        result = daemon_runner.fetch_latest_failed_ci_run("gh", "gemini/test-branch")
        assert result is not None
        assert result["databaseId"] == 98765
        assert result["conclusion"] == "failure"
        assert "Error: tests failed at line 42" in result["failed_logs"]


def test_fetch_latest_failed_ci_run_ignores_passing_run() -> None:
    fake_runs = [
        {
            "databaseId": 12345,
            "status": "completed",
            "conclusion": "success",
            "headSha": "abcdef1",
            "workflowName": "CI",
            "url": "https://github.com/org/repo/actions/runs/12345",
        }
    ]
    with patch("daemon_runner.run_cmd") as mock_run_cmd:
        mock_run_cmd.return_value = MockCompletedProcess(returncode=0, stdout=json.dumps(fake_runs))
        result = daemon_runner.fetch_latest_failed_ci_run("gh", "gemini/test-branch")
        assert result is None


# ==============================================================================
# 5. PR Feedback Extraction Tests
# ==============================================================================


def test_fetch_unhandled_pr_feedback(tmp_path: Path) -> None:
    test_reviews_file = tmp_path / "pr_reviews.json"
    fake_pr_view = {
        "comments": [
            {"id": "c1", "author": {"login": "github-actions[bot]"}, "body": "Bot notification"},
            {
                "id": "c2",
                "author": {"login": "daemon-bot"},
                "body": f"{daemon_runner.DAEMON_TAG} Fix applied",
            },
            {
                "id": "c3",
                "author": {"login": "reviewer"},
                "body": "Please add unit test for edge cases.",
            },
        ],
        "reviews": [
            {
                "id": "r1",
                "state": "APPROVED",
                "author": {"login": "reviewer"},
                "body": "LGTM",
                "comments": [],
            },
            {
                "id": "r2",
                "state": "CHANGES_REQUESTED",
                "author": {"login": "lead-eng"},
                "body": "Refactor the parser to avoid exceptions.",
                "comments": [
                    {
                        "id": "diff_c1",
                        "author": {"login": "lead-eng"},
                        "body": "Use match statement here",
                        "path": "src/parser.py",
                        "line": 45,
                    }
                ],
            },
        ],
        "latestReviews": [],
    }

    with (
        patch("daemon_runner.PR_REVIEWS_FILE", test_reviews_file),
        patch("daemon_runner.run_cmd") as mock_run_cmd,
    ):
        mock_run_cmd.return_value = MockCompletedProcess(
            returncode=0, stdout=json.dumps(fake_pr_view)
        )
        feedback = daemon_runner.fetch_unhandled_pr_feedback("gh", 42)

        # Expected items:
        # 1. c3 (reviewer comment)
        # 2. r2 summary (changes requested)
        # 3. diff_c1 (inline diff comment)
        assert len(feedback) == 3
        ids = [item["id"] for item in feedback]
        assert "c3" in ids
        assert "r2" in ids
        assert "diff_c1" in ids


# ==============================================================================
# 6. Telemetry & Structured Metrics Logging Tests
# ==============================================================================


def test_extract_and_record_daemon_metrics(tmp_path: Path) -> None:
    conv_dir = tmp_path / "conv_123" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript_full.jsonl"

    steps = [
        {
            "step_index": 1,
            "type": "USER_INPUT",
            "content": "Implement feature X per protocol.",
        },
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "content": "I will examine the codebase first.",
            "thinking": "Need to check existing files.",
            "tool_calls": [
                {"name": "view_file", "args": {"path": "src/app.py"}},
                {"name": "run_command", "args": {"cmd": "pytest"}},
            ],
        },
    ]
    with transcript_file.open("w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")

    with patch("daemon_runner.CLI_BRAIN_DIR", tmp_path):
        metrics = daemon_runner.extract_session_transcript_metrics("conv_123")
        assert metrics["total_steps"] == 2
        assert metrics["total_tokens_estimate"] > 0
        assert metrics["tool_calls_breakdown"].get("view_file") == 1
        assert metrics["tool_calls_breakdown"].get("run_command") == 1

        # Test writing metrics
        metrics_file = tmp_path / "daemon_metrics.jsonl"
        with (
            patch("daemon_runner.DAEMON_METRICS_FILE", metrics_file),
            patch("daemon_runner.LOGS_DIR", tmp_path),
        ):
            record: dict[str, Any] = {
                "prompt_name": "0024-test.md",
                "status": "pr_opened",
                "session_duration_seconds": 120.5,
                **metrics,
            }
            daemon_runner.record_daemon_metrics(record)

            assert metrics_file.exists()
            lines = metrics_file.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["prompt_name"] == "0024-test.md"
            assert data["total_steps"] == 2
            assert "timestamp" in data


# ==============================================================================
# 7. Network Connectivity Watchdog Tests
# ==============================================================================


def test_wait_for_network_connectivity_retry() -> None:
    with (
        patch(
            "daemon_runner.check_network_connectivity",
            side_effect=[False, False, True],
        ) as mock_check,
        patch("daemon_runner.time.sleep") as mock_sleep,
    ):
        result = daemon_runner.wait_for_network_connectivity(
            initial_interval=0.01, max_interval=0.05, heartbeat_interval=0.1
        )
        assert result is True
        assert mock_check.call_count == 3
        assert mock_sleep.call_count >= 2


def test_verify_git_staging_safety_ignores_benign_caches(tmp_path: Path) -> None:
    with patch("daemon_runner.run_cmd") as mock_run_cmd:
        mock_run_cmd.side_effect = [
            MockCompletedProcess(
                returncode=0,
                stdout=(
                    "src/__pycache__/core.cpython-313.pyc\n"
                    ".pytest_cache/v/cache/nodeids\n"
                    "tests/fixtures/.DS_Store\n"
                ),
            ),
            MockCompletedProcess(returncode=0, stdout=""),
        ]
        safe, issues = daemon_runner.verify_git_staging_safety(repo_root=tmp_path)
        assert safe is True
        assert issues == []


def test_circuit_breaker_halts_on_max_ci_heals() -> None:
    fake_pr = [{"number": 42, "title": "Test PR", "headRefName": "gemini/test"}]
    fake_checks = [{"name": "CI", "bucket": "fail"}]
    fake_run = {
        "databaseId": 555,
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "traceback",
    }

    with (
        patch("daemon_runner.get_open_prs", return_value=fake_pr),
        patch("daemon_runner.get_pr_checks", return_value=fake_checks),
        patch("daemon_runner.fetch_latest_failed_ci_run", return_value=fake_run),
        patch("daemon_runner.load_processed_comment_ids", return_value=set()),
        patch("daemon_runner.get_ci_heal_attempts", return_value=3),
        patch("daemon_runner.send_notification") as mock_notify,
        patch("daemon_runner.run_cmd") as mock_run_cmd,
        pytest.raises(SystemExit) as excinfo,
    ):
        daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=1,
            max_interval=2,
            max_auto_heal_attempts=3,
            agy_bin="/bin/true",
        )

    assert excinfo.value.code == 1
    mock_notify.assert_called_once()
    assert "Circuit Breaker Tripped" in mock_notify.call_args[0][0]
    # Check that a PR comment was posted
    posted_comments = [
        call[0][0] for call in mock_run_cmd.call_args_list if call[0][0][1:3] == ["pr", "comment"]
    ]
    assert len(posted_comments) == 1
    assert "Circuit Breaker Tripped" in posted_comments[0][5]


def test_circuit_breaker_halts_on_max_review_feedback() -> None:
    fake_pr = [{"number": 42, "title": "Test PR", "headRefName": "gemini/test"}]
    fake_feedback = [{"id": "comment_99", "body": "fix this"}]

    with (
        patch("daemon_runner.get_open_prs", return_value=fake_pr),
        patch("daemon_runner.fetch_unhandled_pr_feedback", return_value=fake_feedback),
        patch("daemon_runner.get_review_feedback_attempts", return_value=3),
        patch("daemon_runner.send_notification") as mock_notify,
        patch("daemon_runner.run_cmd") as mock_run_cmd,
        pytest.raises(SystemExit) as excinfo,
    ):
        daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=1,
            max_interval=2,
            max_auto_heal_attempts=3,
            agy_bin="/bin/true",
        )

    assert excinfo.value.code == 1
    mock_notify.assert_called_once()
    assert "Circuit Breaker Tripped" in mock_notify.call_args[0][0]
    posted_comments = [
        call[0][0] for call in mock_run_cmd.call_args_list if call[0][0][1:3] == ["pr", "comment"]
    ]
    assert len(posted_comments) == 1
    assert "Circuit Breaker Tripped" in posted_comments[0][5]


# ==============================================================================
# 8. Agent Summary Extraction & Meaningful PR Response Tests
# ==============================================================================


def test_extract_latest_agent_summary(tmp_path: Path) -> None:
    conv_dir = tmp_path / "conv_summary_test" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript_full.jsonl"

    steps = [
        {
            "step_index": 1,
            "source": "USER",
            "type": "USER_INPUT",
            "content": "Have you tested the corpus?",
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "Initial thoughts.",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": (
                "Yes, I tested with the 2025 corpus. Both test_csirt_2025_kind_detection "
                "and test_csirt_2025_event_log_forensics passed cleanly."
            ),
        },
    ]
    with transcript_file.open("w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")

    with patch("daemon_runner.CLI_BRAIN_DIR", tmp_path):
        summary = daemon_runner.extract_latest_agent_summary("conv_summary_test")
        assert summary is not None
        assert "Yes, I tested with the 2025 corpus" in summary
        assert "test_csirt_2025_kind_detection" in summary


def test_address_pr_feedback_includes_agent_summary_and_commit_note(tmp_path: Path) -> None:
    comments = [{"id": "c1", "author": "reviewer", "body": "Did you test the corpus?"}]
    prompt_file = tmp_path / "0023-test.md"
    prompt_file.write_text("Test prompt", encoding="utf-8")

    with (
        patch("daemon_runner.get_current_branch", return_value="gemini/0023-test"),
        patch("daemon_runner.execute_prompt_session", return_value=True),
        patch(
            "daemon_runner.extract_latest_agent_summary",
            return_value="Corpus verified: 2 passed in 10.37s.",
        ),
        patch("daemon_runner.save_processed_comment_ids"),
        patch("daemon_runner.run_cmd") as mock_run_cmd,
    ):
        # Case 1: No new commit (inquiry response)
        mock_run_cmd.side_effect = [
            MockCompletedProcess(returncode=0, stdout="a18a1ff\n"),  # initial rev-parse
            MockCompletedProcess(returncode=0, stdout=""),  # git status --porcelain
            MockCompletedProcess(returncode=0, stdout="a18a1ff\n"),  # current rev-parse
            MockCompletedProcess(returncode=0, stdout=""),  # gh pr comment
        ]

        success = daemon_runner.address_pr_feedback(
            gh_bin="gh",
            agy_bin="agy",
            pr_number=26,
            pr_title="PR Title",
            branch_name="gemini/0023-test",
            prompt_path=prompt_file,
            new_comments=comments,
        )
        assert success is True

        pr_comment_call = next(
            call[0][0]
            for call in mock_run_cmd.call_args_list
            if call[0][0][1:3] == ["pr", "comment"]
        )
        body = pr_comment_call[5]
        assert "(findings verified without code changes)" in body
        assert "Corpus verified: 2 passed in 10.37s." in body
        assert "### Findings & Response:" in body


# ==============================================================================
# 9. Model and Reasoning Effort Configuration Tests
# ==============================================================================


def test_default_model_and_effort_constants() -> None:
    assert daemon_runner.DEFAULT_MODEL == "gemini-3.8-flash-high"
    assert daemon_runner.DEFAULT_EFFORT == "high"


def test_execute_prompt_session_passes_model_and_effort(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        (impl_dir / prompt_file.name).write_text("done")
        (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")
        return (0, "Completed successfully")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch("daemon_runner.run_session_process", side_effect=fake_run) as mock_run_process,
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
            model="gemini-3.8-flash-high",
            effort="high",
        )
        assert success is True
        mock_run_process.assert_called_once()
        cmd = mock_run_process.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "gemini-3.8-flash-high"
        assert "--effort" in cmd
        effort_idx = cmd.index("--effort")
        assert cmd[effort_idx + 1] == "high"


def test_execute_prompt_session_omits_effort_when_none(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        (impl_dir / prompt_file.name).write_text("done")
        (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")
        return (0, "Completed successfully")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch("daemon_runner.run_session_process", side_effect=fake_run) as mock_run_process,
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
            model="gemini-2.5-pro",
            effort=None,
        )
        assert success is True
        cmd = mock_run_process.call_args[0][0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gemini-2.5-pro"
        assert "--effort" not in cmd


# ==============================================================================
# 10. Multi-Turn Session and Soft-Denial Handling Tests
# ==============================================================================


def test_check_recent_soft_denial_in_cli_log(tmp_path: Path) -> None:
    cli_log = tmp_path / "cli.log"
    cli_log.write_text(
        "tool_confirmation_manager.go:211] Print mode: soft-denying tool confirmation "
        '"ReadUrlContent" at step 165\n',
        encoding="utf-8",
    )
    with patch("daemon_runner.CLI_LOG_FILE", cli_log):
        tool = daemon_runner.check_recent_soft_denial_in_cli_log()
        assert tool == "ReadUrlContent"


def test_prompt_user_permission_action_read_url_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "1")
    with patch("daemon_runner.add_allowed_rules") as mock_add_rules:
        action, detail = daemon_runner.prompt_user_permission_action(
            ("read_url", "https://pypi.org/project/prefetch-parser/")
        )
        assert action == "allow"
        assert detail == "read_url(https://pypi.org/project/prefetch-parser/)"
        mock_add_rules.assert_called_once_with(
            "read_url(https://pypi.org/project/prefetch-parser/)"
        )


def test_prompt_user_permission_action_read_url_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = iter(
        [
            "4",
            "Use standard library struct module instead of external prefetch packages.",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    with patch("daemon_runner.add_allowed_rules") as mock_add_rules:
        action, detail = daemon_runner.prompt_user_permission_action(
            ("read_url", "https://pypi.org/project/prefetch-parser/")
        )
        assert action == "redirect"
        assert detail == "Use standard library struct module instead of external prefetch packages."
        mock_add_rules.assert_not_called()


def test_execute_prompt_session_soft_denial_user_redirects(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    cli_log = tmp_path / "cli.log"
    cli_log.write_text("", encoding="utf-8")

    call_count = 0
    passed_cmd = None

    def fake_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        nonlocal call_count, passed_cmd
        call_count += 1
        passed_cmd = cmd
        if call_count == 1:
            # Turn 1 triggers ReadUrlContent soft-denial in cli.log
            with cli_log.open("a", encoding="utf-8") as f:
                f.write('Print mode: soft-denying tool confirmation "ReadUrlContent" at step 165\n')
            return (0, "")
        else:
            # Turn 2 completes the task after receiving redirect
            (impl_dir / prompt_file.name).write_text("done")
            (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")
            return (0, "All done")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.CLI_LOG_FILE", cli_log),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch(
            "daemon_runner.prompt_user_permission_action",
            return_value=("redirect", "Do not fetch URLs. Use local parser."),
        ),
        patch("daemon_runner.add_allowed_rules") as mock_add_rules,
        patch("daemon_runner.run_session_process", side_effect=fake_run),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is True
        assert call_count == 2
        # Ensure no permissions were added
        mock_add_rules.assert_not_called()
        # Verify redirect prompt was passed into the resumed session
        assert passed_cmd is not None
        p_idx = passed_cmd.index("-p")
        assert "Do not fetch URLs. Use local parser." in passed_cmd[p_idx + 1]


def test_execute_prompt_session_soft_denial_user_aborts(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    cli_log = tmp_path / "cli.log"
    cli_log.write_text("", encoding="utf-8")

    def fake_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        with cli_log.open("a", encoding="utf-8") as f:
            f.write('Print mode: soft-denying tool confirmation "ReadUrlContent" at step 165\n')
        return (0, "")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.CLI_LOG_FILE", cli_log),
        patch(
            "daemon_runner.prompt_user_permission_action",
            return_value=("abort", None),
        ),
        patch("daemon_runner.run_session_process", side_effect=fake_run),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is False


def test_execute_prompt_session_multi_turn_continuation(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    turns = 0

    def multi_turn_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        nonlocal turns
        turns += 1
        if turns == 1:
            # Turn 1: partially completed, not yet moved to implemented
            return (0, "Partial work done")
        else:
            # Turn 2: completes and moves to implemented
            (impl_dir / prompt_file.name).write_text("done")
            (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")
            return (0, "All requirements met")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch("daemon_runner.run_session_process", side_effect=multi_turn_run),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is True
        assert turns == 2


# ==============================================================================
# Dual Timer Tests (Cumulative Prompt Timer & agy Session Timer)
# ==============================================================================


def test_extract_previous_cumulative_time(tmp_path: Path) -> None:
    non_existent = tmp_path / "nonexistent.log"
    assert daemon_runner.extract_previous_cumulative_time(non_existent) == 0.0

    empty_log = tmp_path / "empty.log"
    empty_log.write_text("Session started\nNo status line\n", encoding="utf-8")
    assert daemon_runner.extract_previous_cumulative_time(empty_log) == 0.0

    legacy_log = tmp_path / "legacy.log"
    legacy_log.write_text(
        "[STATUS 14:16:33] ⏳ [31m 17s | Step 802] Gemini: Done\n",
        encoding="utf-8",
    )
    assert daemon_runner.extract_previous_cumulative_time(legacy_log) == 31 * 60 + 17

    dual_timer_log = tmp_path / "dual.log"
    dual_timer_log.write_text(
        "[STATUS 13:24:19] ⏳ [total: 10m 56s | agy: 4s | Step 262] Tool running...\n"
        "[STATUS 13:24:25] ⏳ [total: 11m 02s | agy: 10s | Step 263] Tool done\n",
        encoding="utf-8",
    )
    assert daemon_runner.extract_previous_cumulative_time(dual_timer_log) == 11 * 60 + 2

    hours_log = tmp_path / "hours.log"
    hours_log.write_text(
        "[STATUS 15:00:00] ⏳ [total: 1h 2m 3s | agy: 50s | Step 500] Working\n",
        encoding="utf-8",
    )
    assert daemon_runner.extract_previous_cumulative_time(hours_log) == 3600 + 120 + 3


def test_execute_prompt_session_accumulates_timer_across_turns(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    offsets_received: list[float] = []

    def tracking_run(cmd, log_file, prompt_name, prompt_num, active_time_offset=0.0):
        offsets_received.append(active_time_offset)
        if len(offsets_received) == 1:
            # Turn 1: simulate 0.05s runtime
            time.sleep(0.05)
            return (0, "Turn 1 finished without completing prompt")
        else:
            # Turn 2: complete prompt
            (impl_dir / prompt_file.name).write_text("done")
            (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")
            return (0, "Turn 2 complete")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch("daemon_runner.run_session_process", side_effect=tracking_run),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is True
        assert len(offsets_received) == 2
        # Turn 1 should start with 0.0 offset
        assert offsets_received[0] == 0.0
        # Turn 2 should have an accumulated positive offset from Turn 1 duration
        assert offsets_received[1] >= 0.04


def test_run_session_process_logs_both_timers(tmp_path: Path) -> None:
    log_file = tmp_path / "session-test.log"
    log_file.write_text("", encoding="utf-8")

    # Command that outputs a line and sleeps long enough to enter status polling loop
    cmd = [
        sys.executable,
        "-c",
        "import time; print('turn line'); time.sleep(1.8)",
    ]

    with (
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.get_latest_conversation_id", return_value="conv-123"),
        patch(
            "daemon_runner.get_session_transcript_status",
            side_effect=[
                (10, "Processing step"),
                (10, "Processing step"),
                (10, "Processing step"),
            ],
        ),
    ):
        ret, output = daemon_runner.run_session_process(
            cmd,
            log_file,
            prompt_name="0042-test.md",
            prompt_num="0042",
            active_time_offset=600.0,  # 10 minutes prior
        )
        assert ret == 0
        assert "turn line" in output
        content = log_file.read_text(encoding="utf-8")
        assert "total: 10m" in content
        assert "agy:" in content
        assert "Step 10" in content


# ==============================================================================
# Definition of Done Verification & Feedback Tests
# ==============================================================================


def test_check_definition_of_done_success(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test.md"
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / prompt_file.name).write_text("prompt content", encoding="utf-8")

    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings", encoding="utf-8")

    mock_res = subprocess.CompletedProcess(
        ["make", "check"],
        returncode=0,
        stdout="All 100 tests passed",
        stderr="",
    )

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.run_cmd", return_value=mock_res),
    ):
        ok, msg = daemon_runner.check_definition_of_done(prompt_file)
        assert ok is True
        assert msg == ""
        assert daemon_runner.verify_definition_of_done(prompt_file) is True


def test_check_definition_of_done_timeout(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test.md"
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / prompt_file.name).write_text("prompt content", encoding="utf-8")

    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings", encoding="utf-8")

    mock_res = subprocess.CompletedProcess(
        ["make", "check"],
        returncode=124,
        stdout="tests/unit/test_hang.py::test_hanging_network_call ...",
        stderr="Command timed out after 600.0s",
    )

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.run_cmd", return_value=mock_res),
    ):
        ok, msg = daemon_runner.check_definition_of_done(prompt_file)
        assert ok is False
        assert "timed out after 10 minutes (600s)" in msg
        assert "Possible hang, infinite loop, or test deadlock" in msg
        assert "test_hanging_network_call" in msg
        assert daemon_runner.verify_definition_of_done(prompt_file) is False


def test_check_definition_of_done_failure(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test.md"
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / prompt_file.name).write_text("prompt content", encoding="utf-8")

    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings", encoding="utf-8")

    mock_res = subprocess.CompletedProcess(
        ["make", "check"],
        returncode=1,
        stdout="FAILED tests/unit/test_evtx.py::test_parse - AssertionError",
        stderr="",
    )

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.run_cmd", return_value=mock_res),
    ):
        ok, msg = daemon_runner.check_definition_of_done(prompt_file)
        assert ok is False
        assert "'make check' failed with exit status 1" in msg
        assert "FAILED tests/unit/test_evtx.py" in msg


def test_check_definition_of_done_missing_files(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test.md"
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
    ):
        # 1. Prompt not moved to implemented
        ok, msg = daemon_runner.check_definition_of_done(prompt_file)
        assert ok is False
        assert "was not moved to prompts/implemented/" in msg

        # 2. Prompt moved, but learnings missing
        (impl_dir / prompt_file.name).write_text("prompt", encoding="utf-8")
        ok, msg = daemon_runner.check_definition_of_done(prompt_file)
        assert ok is False
        assert "was not found" in msg


def test_execute_prompt_session_feeds_dod_timeout_back_to_session(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0042-test-feature.md"
    prompt_file.write_text("# Test Feature", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    (impl_dir / prompt_file.name).write_text("done")
    (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")

    turn_cmds: list[list[str]] = []

    def mock_session_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        turn_cmds.append(list(cmd))
        return (0, "Completed turn")

    # Turn 1: DoD fails due to timeout
    # Turn 2: DoD passes
    dod_side_effects = [
        (
            False,
            "Definition of Done check 'make check' timed out after 10 minutes (600s).\n"
            "Possible hang, infinite loop, or test deadlock.",
        ),
        (True, ""),
    ]

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", side_effect=dod_side_effects),
        patch("daemon_runner.run_session_process", side_effect=mock_session_run),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is True
        assert len(turn_cmds) == 2

        # Turn 1: initial kickoff
        assert "-c" not in turn_cmds[0]

        # Turn 2: resumed turn with timeout feedback!
        assert "-c" in turn_cmds[1]
        assert "-p" in turn_cmds[1]
        p_idx = turn_cmds[1].index("-p")
        resumed_prompt = turn_cmds[1][p_idx + 1]
        assert "Definition of Done verification failed" in resumed_prompt
        assert "timed out after 10 minutes (600s)" in resumed_prompt
        assert "possible hang, infinite loop, or test deadlock" in resumed_prompt.lower()


# ==============================================================================
# Permission Denial Extraction Tests
# ==============================================================================


def test_extract_denied_permission_multiline_unsandboxed_command(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    conv_dir = brain_dir / "test-conv" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript.jsonl"

    multiline_cmd = (
        'python3 -c "\nimport os\nfrom pathlib import Path\n'
        "p = Path('./corpus/2025')\nprint('exists:', p.exists())\n\""
    )
    err_text = (
        f"permission check failed for unsandboxed {json.dumps(multiline_cmd)}: "
        f"user denied permission to run command:\n{multiline_cmd}\n"
        "Do not attempt to circumvent this denial by rephrasing the command, using "
        "alternative tools/scripts (e.g. python, sh, curl), or accessing the same "
        "target resource. Proceed without performing this action."
    )
    transcript_file.write_text(
        json.dumps({"source": "MODEL", "type": "GENERIC", "error": err_text}) + "\n",
        encoding="utf-8",
    )

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        perm_kind, target = daemon_runner.extract_denied_permission("")
        assert perm_kind == "unsandboxed"
        assert target == multiline_cmd
        assert target.startswith('python3 -c "')
        assert target.endswith('"')
        assert "print('exists:', p.exists())" in target


def test_extract_denied_permission_multiline_user_denied_block(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    conv_dir = brain_dir / "test-conv" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript.jsonl"

    multiline_cmd = 'python3 -c "\nimport urllib.request\nprint(123)\n"'
    err_text = (
        f"user denied permission to run command:\n{multiline_cmd}\n"
        "Do not attempt to circumvent this denial by rephrasing the command."
    )
    transcript_file.write_text(
        json.dumps({"source": "MODEL", "type": "GENERIC", "error": err_text}) + "\n",
        encoding="utf-8",
    )

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        perm_kind, target = daemon_runner.extract_denied_permission("")
        assert perm_kind == "command"
        assert target == multiline_cmd


def test_extract_denied_permission_from_tool_call_fallback(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    conv_dir = brain_dir / "test-conv" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = conv_dir / "transcript.jsonl"

    multiline_cmd = 'python3 -c "\nimport os\nprint(os.getcwd())\n"'
    step = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "tool_calls": [
            {
                "name": "run_command",
                "args": {
                    "CommandLine": json.dumps(multiline_cmd),
                },
            }
        ],
    }
    transcript_file.write_text(json.dumps(step) + "\n", encoding="utf-8")

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        perm_kind, target = daemon_runner.extract_denied_permission("", perm_hint="unsandboxed")
        assert perm_kind == "unsandboxed"
        assert target == multiline_cmd


def test_initialize_baseline_permissions_preserves_unsandboxed_git_and_custom_rules(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    initial_settings = {
        "permissions": {
            "allow": [
                "read_file(*)",
                "unsandboxed(git)",
                "unsandboxed(git *)",
                "unsandboxed(grep *)",
                "command(*)",
                "write_file(*)",
                "write_file(relative/path)",
                "write_file(/absolute/path/*)",
                "write_file(/safe/absolute/file.py)",
            ],
            "deny": ["write_file(/repo/.git/*)"],
            "ask": ["command(*)", "unsandboxed(*)"],
        }
    }
    settings_file.write_text(json.dumps(initial_settings), encoding="utf-8")

    with patch("daemon_runner.SETTINGS_FILE", settings_file):
        daemon_runner.initialize_baseline_permissions()

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    allow_rules = saved["permissions"]["allow"]
    assert "unsandboxed(git)" in allow_rules
    assert "unsandboxed(git *)" in allow_rules
    assert "unsandboxed(grep *)" in allow_rules
    assert "write_file(/safe/absolute/file.py)" in allow_rules
    assert "command(*)" not in allow_rules
    assert "write_file(*)" not in allow_rules
    assert "write_file(relative/path)" not in allow_rules


def test_parse_iso_timestamp() -> None:
    # UTC Z timestamp
    ts_z = daemon_runner.parse_iso_timestamp("2026-10-10T21:01:48Z")
    assert ts_z is not None
    assert ts_z > 0

    # Offset timestamp
    ts_offset = daemon_runner.parse_iso_timestamp("2026-10-10T21:01:48+00:00")
    assert ts_offset == ts_z

    # Invalid / empty
    assert daemon_runner.parse_iso_timestamp("") is None
    assert daemon_runner.parse_iso_timestamp(None) is None
    assert daemon_runner.parse_iso_timestamp("not-a-timestamp") is None


def test_parse_denial_from_step() -> None:
    # Quoted target
    step_fail = {
        "error": (
            'permission check failed for unsandboxed "git push origin feat": user denied permission'
        )
    }
    perm, target = daemon_runner.parse_denial_from_step(step_fail) or ("", None)
    assert perm == "unsandboxed"
    assert target == "git push origin feat"

    # Multiline command
    step_cmd = {
        "error": (
            "permission check failed for command\n"
            "user denied permission to run command:\n"
            "git push origin test\n"
            "Do not attempt to circumvent"
        )
    }
    perm, target = daemon_runner.parse_denial_from_step(step_cmd) or ("", None)
    assert perm == "command"
    assert target == "git push origin test"

    # Action(target)
    step_perm = {"error": "user denied permission for write_file(/path/to/file.py)"}
    perm, target = daemon_runner.parse_denial_from_step(step_perm) or ("", None)
    assert perm == "write_file"
    assert target == "/path/to/file.py"

    # Normal step without error
    assert daemon_runner.parse_denial_from_step({"status": "DONE"}) is None


def test_check_recent_transcript_denial_respects_since_timestamp(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    conv_dir = brain_dir / "test-conv" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    t_file = conv_dir / "transcript.jsonl"

    old_step = {
        "step_index": 1,
        "created_at": "2026-10-10T21:00:00Z",
        "error": (
            'permission check failed for unsandboxed "git push origin branch": '
            "user denied permission"
        ),
    }
    new_step = {
        "step_index": 2,
        "created_at": "2026-10-10T21:10:00Z",
        "status": "DONE",
    }
    t_file.write_text(f"{json.dumps(old_step)}\n{json.dumps(new_step)}\n", encoding="utf-8")

    turn_start_time = daemon_runner.parse_iso_timestamp("2026-10-10T21:05:00Z") or 0.0

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        # Without since_timestamp, old denial would be returned
        result_without_since = daemon_runner.check_recent_transcript_denial(
            since_timestamp=0.0, conv_id="test-conv"
        )
        assert result_without_since is not None
        assert result_without_since[0] == "unsandboxed"
        assert result_without_since[1] == "git push origin branch"

        # With since_timestamp after old_step, old denial MUST be ignored!
        result_with_since = daemon_runner.check_recent_transcript_denial(
            since_timestamp=turn_start_time, conv_id="test-conv"
        )
        assert result_with_since is None


def test_check_recent_transcript_denial_scopes_to_conv_id(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    old_conv = brain_dir / "conv-old" / ".system_generated" / "logs"
    old_conv.mkdir(parents=True, exist_ok=True)
    (old_conv / "transcript.jsonl").write_text(
        json.dumps(
            {
                "created_at": "2026-10-10T21:10:00Z",
                "error": (
                    'permission check failed for unsandboxed "git push origin old": '
                    "user denied permission"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    new_conv = brain_dir / "conv-new" / ".system_generated" / "logs"
    new_conv.mkdir(parents=True, exist_ok=True)
    (new_conv / "transcript.jsonl").write_text(
        json.dumps({"created_at": "2026-10-10T21:10:00Z", "status": "DONE"}) + "\n",
        encoding="utf-8",
    )

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        res = daemon_runner.check_recent_transcript_denial(conv_id="conv-new")
        assert res is None


def test_extract_denied_permission_respects_since_timestamp(tmp_path: Path) -> None:
    brain_dir = tmp_path / "brain"
    conv_dir = brain_dir / "test-conv" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    t_file = conv_dir / "transcript.jsonl"

    old_denial = {
        "step_index": 1,
        "created_at": "2026-10-10T21:00:00Z",
        "error": (
            'permission check failed for unsandboxed "git push origin old": user denied permission'
        ),
    }
    t_file.write_text(f"{json.dumps(old_denial)}\n", encoding="utf-8")

    turn_start_time = daemon_runner.parse_iso_timestamp("2026-10-10T21:05:00Z") or 0.0

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        # Setting since_timestamp prevents resurrecting the stale denial
        _perm, target = daemon_runner.extract_denied_permission(
            "", conv_id="test-conv", since_timestamp=turn_start_time
        )
        assert target is None


def test_extract_denied_permission_from_full_output() -> None:
    output = (
        'Some output\npermission check failed for unsandboxed "git push origin test": '
        "user denied permission\n"
    )
    perm, target = daemon_runner.extract_denied_permission(output)
    assert perm == "unsandboxed"
    assert target == "git push origin test"


def test_get_branch_pr_status_open() -> None:
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = json.dumps(
        [
            {
                "number": 30,
                "state": "OPEN",
                "title": "feat(test): test prompt",
                "url": "https://github.com/test/pr/30",
            }
        ]
    )
    with patch("daemon_runner.run_cmd", return_value=mock_res) as mock_run:
        status = daemon_runner.get_branch_pr_status("gh", "gemini/test")
        assert status is not None
        assert status["number"] == 30
        assert status["state"] == "OPEN"
        mock_run.assert_called_once_with(
            [
                "gh",
                "pr",
                "list",
                "--head",
                "gemini/test",
                "--state",
                "all",
                "--json",
                "number,state,title,url",
            ],
            check=False,
        )


def test_get_branch_pr_status_merged() -> None:
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = json.dumps(
        [
            {
                "number": 30,
                "state": "MERGED",
                "title": "feat(test): test prompt",
                "url": "https://github.com/test/pr/30",
            }
        ]
    )
    with patch("daemon_runner.run_cmd", return_value=mock_res):
        status = daemon_runner.get_branch_pr_status("gh", "gemini/test")
        assert status is not None
        assert status["number"] == 30
        assert status["state"] == "MERGED"


def test_get_branch_pr_status_none() -> None:
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "[]"
    with patch("daemon_runner.run_cmd", return_value=mock_res):
        status = daemon_runner.get_branch_pr_status("gh", "gemini/test")
        assert status is None


def test_ensure_pr_opened_already_merged() -> None:
    merged_info = {
        "number": 30,
        "state": "MERGED",
        "title": "feat(test): test prompt",
        "url": "https://github.com/test/pr/30",
    }
    rev_res = MagicMock()
    rev_res.returncode = 0
    rev_res.stdout = "0\n"

    with (
        patch("daemon_runner.get_branch_pr_status", return_value=merged_info),
        patch("daemon_runner.is_prompt_merged_into_origin_main", return_value=True),
        patch("daemon_runner.run_cmd", return_value=rev_res) as mock_run,
    ):
        ok = daemon_runner.ensure_pr_opened("gh", "gemini/test", Path("prompts/0027-test.md"))
        assert ok is True
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/main..gemini/test"], check=False
        )


def test_ensure_pr_opened_stale_merged_pr_renames_branch() -> None:
    merged_info = {
        "number": 30,
        "state": "MERGED",
        "title": "feat(test): test prompt",
        "url": "https://github.com/test/pr/30",
    }
    push_res = MagicMock(returncode=0)
    create_res = MagicMock(returncode=0, stdout="https://github.com/test/pr/31\n")
    rev_res = MagicMock(returncode=0, stdout="1\n")

    def mock_run_cmd(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        if "rev-list" in cmd:
            return rev_res
        if "branch" in cmd and "-m" in cmd:
            return MagicMock(returncode=0)
        if "push" in cmd:
            return push_res
        if "create" in cmd:
            return create_res
        return MagicMock(returncode=0)

    with (
        patch("daemon_runner.get_branch_pr_status", return_value=merged_info),
        patch("daemon_runner.is_prompt_merged_into_origin_main", return_value=False),
        patch("daemon_runner.build_pr_description", return_value="PR body"),
        patch("daemon_runner.send_notification"),
        patch("daemon_runner.run_cmd", side_effect=mock_run_cmd) as mock_run,
    ):
        ok = daemon_runner.ensure_pr_opened("gh", "gemini/test", Path("prompts/0027-test.md"))
        assert ok is True
        assert any(
            call.args[0] == ["git", "branch", "-m", "gemini/test", "gemini/test-impl"]
            for call in mock_run.call_args_list
        )


def test_setup_prompt_branch_avoids_merged_branch() -> None:
    merged_info = {
        "number": 30,
        "state": "MERGED",
        "title": "feat(test): test prompt",
        "url": "https://github.com/test/pr/30",
    }
    with (
        patch("daemon_runner.get_current_branch", return_value="main"),
        patch("daemon_runner.get_branch_pr_status", return_value=merged_info),
        patch("daemon_runner.is_prompt_merged_into_origin_main", return_value=False),
        patch("daemon_runner.run_cmd", return_value=MagicMock(returncode=1)),
    ):
        branch = daemon_runner.setup_prompt_branch(
            Path("prompts/0027-test.md"), resume=False, fresh=False, gh_bin="gh"
        )
        assert branch == "gemini/0027-test-impl"


def test_ensure_pr_opened_already_open() -> None:
    open_info = {
        "number": 30,
        "state": "OPEN",
        "title": "feat(test): test prompt",
        "url": "https://github.com/test/pr/30",
    }
    with (
        patch("daemon_runner.get_branch_pr_status", return_value=open_info),
        patch("daemon_runner.run_cmd") as mock_run,
    ):
        ok = daemon_runner.ensure_pr_opened("gh", "gemini/test", Path("prompts/0027-test.md"))
        assert ok is True
        mock_run.assert_not_called()


def test_ensure_pr_opened_no_commits_between_main_and_branch() -> None:
    push_res = MagicMock()
    push_res.returncode = 0

    create_res = MagicMock()
    create_res.returncode = 1
    create_res.stderr = (
        "pull request create failed: GraphQL: No commits between main and "
        "gemini/0027-test (createPullRequest)"
    )
    create_res.stdout = ""

    def side_effect(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        if "push" in cmd:
            return push_res
        if "create" in cmd:
            return create_res
        res = MagicMock()
        res.returncode = 0
        res.stdout = ""
        return res

    with (
        patch("daemon_runner.get_branch_pr_status", return_value=None),
        patch("daemon_runner.build_pr_description", return_value="PR body"),
        patch("daemon_runner.run_cmd", side_effect=side_effect),
    ):
        ok = daemon_runner.ensure_pr_opened("gh", "gemini/0027-test", Path("prompts/0027-test.md"))
        assert ok is True


def test_reconcile_startup_branch_merged_switches_to_main(tmp_path: Path) -> None:
    merged_info = {
        "number": 30,
        "state": "MERGED",
        "title": "feat(test): test prompt",
        "url": "https://github.com/test/pr/30",
    }
    candidate = tmp_path / "0027-test.md"
    candidate.write_text("dummy prompt", encoding="utf-8")

    commands_executed: list[list[str]] = []

    def mock_run_cmd(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        commands_executed.append(cmd)
        res = MagicMock()
        res.returncode = 0
        res.stdout = ""
        res.stderr = ""
        return res

    with (
        patch("daemon_runner.get_current_branch", return_value="gemini/0027-test"),
        patch("daemon_runner.get_branch_pr_status", return_value=merged_info),
        patch("daemon_runner.run_cmd", side_effect=mock_run_cmd),
        patch("daemon_runner.check_definition_of_done") as mock_dod,
        patch("daemon_runner.ensure_pr_opened") as mock_ensure,
    ):
        branch, act_cand, act_fb = daemon_runner.reconcile_startup_branch("gh")
        assert branch == "main"
        assert act_cand is None
        assert act_fb is None
        mock_dod.assert_not_called()
        mock_ensure.assert_not_called()
        assert ["git", "checkout", "main"] in commands_executed


def test_reconcile_startup_branch_not_merged_commits_and_opens_pr(tmp_path: Path) -> None:
    candidate = tmp_path / "0034-test.md"
    candidate.write_text("dummy prompt", encoding="utf-8")

    commands_executed: list[list[str]] = []

    def mock_run_cmd(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        commands_executed.append(cmd)
        res = MagicMock()
        res.returncode = 0
        if cmd == ["git", "status", "--porcelain"]:
            res.stdout = " M src/bth/test.py\n"
        elif cmd[:3] == ["git", "cat-file", "-e"]:
            res.returncode = 1  # Not merged in origin/main
        else:
            res.stdout = ""
        res.stderr = ""
        return res

    with (
        patch("daemon_runner.get_current_branch", return_value="gemini/0034-test"),
        patch("daemon_runner.get_branch_pr_status", return_value=None),
        patch("daemon_runner.IMPLEMENTED_DIR", tmp_path),
        patch("daemon_runner.run_cmd", side_effect=mock_run_cmd),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")) as mock_dod,
        patch("daemon_runner.verify_git_staging_safety", return_value=(True, [])) as mock_safe,
        patch("daemon_runner.ensure_pr_opened", return_value=True) as mock_ensure,
    ):
        branch, act_cand, act_fb = daemon_runner.reconcile_startup_branch("gh")
        assert branch == "gemini/0034-test"
        assert act_cand is None
        assert act_fb is None
        mock_dod.assert_called_once()
        mock_safe.assert_called_once()
        mock_ensure.assert_called_once()
        assert ["git", "commit", "-m", "feat(0034-test): implement 0034-test"] in commands_executed


def test_reconcile_startup_branch_dod_fails_returns_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "0034-test.md"
    candidate.write_text("dummy prompt", encoding="utf-8")

    def mock_run_cmd(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        res = MagicMock()
        res.returncode = 0
        if cmd[:3] == ["git", "cat-file", "-e"]:
            res.returncode = 1
        res.stdout = ""
        res.stderr = ""
        return res

    with (
        patch("daemon_runner.get_current_branch", return_value="gemini/0034-test"),
        patch("daemon_runner.get_branch_pr_status", return_value=None),
        patch("daemon_runner.IMPLEMENTED_DIR", tmp_path),
        patch("daemon_runner.run_cmd", side_effect=mock_run_cmd),
        patch(
            "daemon_runner.check_definition_of_done",
            return_value=(False, "make check timed out"),
        ),
        patch("daemon_runner.ensure_pr_opened") as mock_ensure,
    ):
        branch, act_cand, act_fb = daemon_runner.reconcile_startup_branch("gh")
        assert branch == "gemini/0034-test"
        assert act_cand == candidate
        assert act_fb is not None
        assert "make check timed out" in act_fb
        mock_ensure.assert_not_called()


def test_reconcile_startup_branch_from_main_with_dirty_work_switches_branch() -> None:
    commands_executed: list[list[str]] = []

    def mock_run_cmd(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        commands_executed.append(cmd)
        res = MagicMock()
        res.returncode = 0
        if cmd == ["git", "status", "--porcelain"]:
            res.stdout = " R prompts/queued/0034-test.md -> prompts/implemented/0034-test.md\n"
        elif cmd[:2] == ["git", "show-ref"]:
            res.returncode = 0  # Branch exists
        elif cmd[:3] == ["git", "cat-file", "-e"]:
            res.returncode = 1  # Not merged
        else:
            res.stdout = ""
        res.stderr = ""
        return res

    with (
        patch("daemon_runner.get_current_branch", side_effect=["main", "gemini/0034-test"]),
        patch("daemon_runner.get_branch_pr_status", return_value=None),
        patch("daemon_runner.run_cmd", side_effect=mock_run_cmd),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch("daemon_runner.verify_git_staging_safety", return_value=(True, [])),
        patch("daemon_runner.ensure_pr_opened", return_value=True),
    ):
        branch, _, _ = daemon_runner.reconcile_startup_branch("gh")
        assert branch == "gemini/0034-test"
        assert ["git", "checkout", "gemini/0034-test"] in commands_executed


# ==============================================================================
# 12. Soft Denial Detection, Format Error Recovery & PR Feedback Retry Tests
# ==============================================================================


def test_check_recent_soft_denial_in_cli_log_with_truncation(tmp_path: Path) -> None:
    test_cli_log = tmp_path / "cli.log"
    # Write initial content
    test_cli_log.write_text("Old log entry\n" * 50, encoding="utf-8")
    old_size = test_cli_log.stat().st_size

    # Simulate AGY truncating cli.log on launch with fresh content
    test_cli_log.write_text(
        "I1003 15:23:34.776156 345 tool_confirmation_manager.go:212] "
        'Print mode: soft-denying tool confirmation "RunCommand" at step 2455\n',
        encoding="utf-8",
    )
    assert test_cli_log.stat().st_size < old_size

    with patch("daemon_runner.CLI_LOG_FILE", test_cli_log):
        # Even with old_size > current_size, it should seek correctly and find RunCommand
        denied_tool = daemon_runner.check_recent_soft_denial_in_cli_log(since_offset=old_size)
        assert denied_tool == "RunCommand"


def test_check_recent_transcript_denial(tmp_path: Path) -> None:
    conv_dir = tmp_path / "test-conv-123"
    logs_dir = conv_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript_file = logs_dir / "transcript.jsonl"

    error_step = {
        "step_index": 2455,
        "source": "MODEL",
        "type": "GENERIC",
        "status": "ERROR",
        "error": (
            'permission check failed for unsandboxed "cat \\"corpus/2025/Evidences/case.yaml\\"": '
            'user denied permission to run command:\ncat "corpus/2025/Evidences/case.yaml"\n'
            "Do not attempt to circumvent this denial"
        ),
    }
    transcript_file.write_text(json.dumps(error_step) + "\n", encoding="utf-8")

    with patch("daemon_runner.CLI_BRAIN_DIR", tmp_path):
        res = daemon_runner.check_recent_transcript_denial()
        assert res is not None
        perm_k, target = res
        assert perm_k == "unsandboxed"
        assert 'cat "corpus/2025/Evidences/case.yaml"' in (target or "")


def test_is_transient_session_error_formatting_error() -> None:
    err_json = json.dumps(
        {
            "short_error": (
                "Your previous response contained an improperly formatted function call\n"
                "Please retry with a properly formatted function call\n"
                "Retries remaining: 3"
            ),
            "retryable": False,
            "error_id": "56888a1b-3b93-4bfd-bcf9-388c8f5ba863-1679",
        }
    )
    raw_output = (
        "error: Your previous response contained an improperly formatted function call\n"
        "Please retry with a properly formatted function call\n"
        "Retries remaining: 3\n"
        f"AGY_ERROR: {err_json}\n"
    )
    is_transient, reason = daemon_runner.is_transient_session_error(raw_output, returncode=3)
    assert is_transient is True
    assert "formatted" in reason.lower() or "tool" in reason.lower()


def test_wait_for_pr_merge_retries_review_feedback_with_backoff() -> None:
    fake_pr = [{"number": 42, "title": "Test PR", "headRefName": "gemini/test"}]
    fake_feedback = [{"id": "comment_99", "body": "fix this"}]

    with (
        patch("daemon_runner.get_open_prs") as mock_open_prs,
        patch("daemon_runner.fetch_unhandled_pr_feedback", return_value=fake_feedback),
        patch("daemon_runner.get_review_feedback_attempts", return_value=0),
        patch("daemon_runner.increment_review_feedback_attempts", return_value=1),
        patch("daemon_runner.address_pr_feedback", return_value=False) as mock_address,
        patch("daemon_runner.time.sleep") as mock_sleep,
        patch("daemon_runner.IMPLEMENTED_DIR") as mock_impl_dir,
    ):
        mock_prompt = MagicMock()
        mock_prompt.exists.return_value = True
        mock_impl_dir.__truediv__.return_value = mock_prompt

        # First call has 1 PR, second call has 0 PRs (simulating merge/close)
        mock_open_prs.side_effect = [fake_pr, []]

        duration = daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=1,
            max_interval=2,
            max_auto_heal_attempts=3,
            agy_bin="/bin/true",
        )
        assert duration >= 0
        mock_address.assert_called_once()
        mock_sleep.assert_called_once_with(1)


# ==============================================================================
# 20. Instant Recheck on Enter Tests
# ==============================================================================


def test_is_stdin_interactive() -> None:
    with patch("sys.stdin", None):
        assert daemon_runner._is_stdin_interactive() is False

    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    with patch("sys.stdin", mock_stdin):
        assert daemon_runner._is_stdin_interactive() is False

    mock_stdin.isatty.return_value = True
    with patch("sys.stdin", mock_stdin):
        assert daemon_runner._is_stdin_interactive() is True


def test_interruptible_sleep_non_interactive() -> None:
    with (
        patch("daemon_runner._is_stdin_interactive", return_value=False),
        patch("daemon_runner.time.sleep") as mock_sleep,
    ):
        result = daemon_runner._interruptible_sleep(1.0)
        assert result is False
        assert mock_sleep.call_count == 2


def test_interruptible_sleep_user_presses_enter() -> None:
    mock_stdin = MagicMock()
    mock_stdin.readline.return_value = "\n"

    with (
        patch("daemon_runner._is_stdin_interactive", return_value=True),
        patch("sys.stdin", mock_stdin),
        patch("select.select", return_value=([mock_stdin], [], [])),
    ):
        result = daemon_runner._interruptible_sleep(10.0)
        assert result is True
        mock_stdin.readline.assert_called_once()


def test_interruptible_sleep_timeout_no_input() -> None:
    mock_stdin = MagicMock()

    with (
        patch("daemon_runner._is_stdin_interactive", return_value=True),
        patch("sys.stdin", mock_stdin),
        patch("select.select", return_value=([], [], [])),
        patch("daemon_runner.time.time", side_effect=[0.0, 0.2, 0.4, 0.6]),
    ):
        result = daemon_runner._interruptible_sleep(0.5)
        assert result is False
        mock_stdin.readline.assert_not_called()


def test_wait_for_pr_merge_instant_recheck_on_enter() -> None:
    fake_pr = [{"number": 42, "title": "Test PR", "headRefName": "gemini/test"}]

    with (
        patch("daemon_runner.get_open_prs") as mock_open_prs,
        patch("daemon_runner.fetch_unhandled_pr_feedback", return_value=[]),
        patch("daemon_runner.get_pr_checks", return_value=[]),
        patch("daemon_runner._interruptible_sleep") as mock_int_sleep,
    ):
        mock_open_prs.side_effect = [fake_pr, fake_pr, []]
        mock_int_sleep.side_effect = [True, False]

        duration = daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=60,
            max_interval=1800,
        )
        assert duration >= 0
        assert mock_int_sleep.call_count == 2


# ==============================================================================
# 21. Prompt Path Resolution and Review Feedback Loop Prevention
# ==============================================================================


def test_find_prompt_path_for_branch_exact(tmp_path: Path) -> None:
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True)
    prompt_file = impl_dir / "0056-test-prompt.md"
    prompt_file.write_text("# 0056 Test Prompt")

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.QUEUED_DIR", tmp_path / "prompts" / "queued"),
    ):
        result = daemon_runner.find_prompt_path_for_branch("gemini/0056-test-prompt")
        assert result == prompt_file


def test_find_prompt_path_for_branch_impl_suffix(tmp_path: Path) -> None:
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True)
    prompt_file = impl_dir / "0056-test-prompt.md"
    prompt_file.write_text("# 0056 Test Prompt")

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.QUEUED_DIR", tmp_path / "prompts" / "queued"),
    ):
        # Branch has -impl suffix, but prompt file does not
        result = daemon_runner.find_prompt_path_for_branch("gemini/0056-test-prompt-impl")
        assert result == prompt_file


def test_find_prompt_path_for_branch_prefix_fallback(tmp_path: Path) -> None:
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True)
    prompt_file = impl_dir / "0056-evaluation-scoring-parser-fixes.md"
    prompt_file.write_text("# 0056 Prompt")

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.QUEUED_DIR", tmp_path / "prompts" / "queued"),
    ):
        # Branch has slightly different slug or extra suffix
        result = daemon_runner.find_prompt_path_for_branch("gemini/0056-short-slug-different-impl")
        assert result == prompt_file


def test_find_prompt_path_for_branch_queued_dir(tmp_path: Path) -> None:
    queued_dir = tmp_path / "prompts" / "queued"
    queued_dir.mkdir(parents=True)
    prompt_file = queued_dir / "0057-next-feature.md"
    prompt_file.write_text("# 0057 Next Feature")

    with (
        patch("daemon_runner.IMPLEMENTED_DIR", tmp_path / "prompts" / "implemented"),
        patch("daemon_runner.QUEUED_DIR", queued_dir),
    ):
        result = daemon_runner.find_prompt_path_for_branch("gemini/0057-next-feature")
        assert result == prompt_file


def test_find_prompt_path_for_branch_none_when_empty_or_missing(tmp_path: Path) -> None:
    with (
        patch("daemon_runner.IMPLEMENTED_DIR", tmp_path / "prompts" / "implemented"),
        patch("daemon_runner.QUEUED_DIR", tmp_path / "prompts" / "queued"),
    ):
        assert daemon_runner.find_prompt_path_for_branch("") is None
        assert daemon_runner.find_prompt_path_for_branch("gemini/9999-nonexistent") is None


def test_is_prompt_merged_into_origin_main_handles_impl_suffix() -> None:
    with patch("daemon_runner.run_cmd") as mock_run:
        # First call with stripped slug succeeds
        mock_run.return_value = MockCompletedProcess(returncode=0)
        assert daemon_runner.is_prompt_merged_into_origin_main("0056-test-impl") is True
        mock_run.assert_called_with(
            ["git", "cat-file", "-e", "origin/main:prompts/implemented/0056-test.md"],
            check=False,
        )


def test_wait_for_pr_merge_resolves_impl_branch_for_review_feedback(tmp_path: Path) -> None:
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True)
    prompt_file = impl_dir / "0056-test-feature.md"
    prompt_file.write_text("# 0056 Prompt")

    fake_pr = [
        {
            "number": 81,
            "title": "feat: test feature",
            "headRefName": "gemini/0056-test-feature-impl",
        }
    ]
    fake_feedback = [{"id": "comment_123", "body": "Please explain this logic"}]

    with (
        patch("daemon_runner.get_open_prs") as mock_open_prs,
        patch("daemon_runner.fetch_unhandled_pr_feedback", return_value=fake_feedback),
        patch("daemon_runner.get_review_feedback_attempts", return_value=0),
        patch("daemon_runner.address_pr_feedback", return_value=True) as mock_address,
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.QUEUED_DIR", tmp_path / "prompts" / "queued"),
    ):
        mock_open_prs.side_effect = [fake_pr, []]

        duration = daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=1,
            max_interval=2,
            agy_bin="/bin/true",
        )
        assert duration >= 0
        mock_address.assert_called_once()
        # Verify it passed the resolved prompt_file without -impl
        assert mock_address.call_args.kwargs["prompt_path"] == prompt_file


def test_wait_for_pr_merge_saves_comment_ids_when_prompt_unresolvable(tmp_path: Path) -> None:
    fake_pr = [
        {
            "number": 99,
            "title": "feat: unknown feature",
            "headRefName": "gemini/unknown-branch",
        }
    ]
    fake_feedback = [{"id": "comment_unresolvable_1", "body": "Where is the prompt?"}]

    with (
        patch("daemon_runner.get_open_prs") as mock_open_prs,
        patch("daemon_runner.fetch_unhandled_pr_feedback", return_value=fake_feedback),
        patch("daemon_runner.get_review_feedback_attempts", return_value=0),
        patch("daemon_runner.address_pr_feedback") as mock_address,
        patch("daemon_runner.save_processed_comment_ids") as mock_save_processed,
        patch("daemon_runner.get_pr_checks", return_value=[]),
        patch("daemon_runner._interruptible_sleep", return_value=False),
        patch("daemon_runner.IMPLEMENTED_DIR", tmp_path / "prompts" / "implemented"),
        patch("daemon_runner.QUEUED_DIR", tmp_path / "prompts" / "queued"),
    ):
        # 1st iteration: unresolvable prompt, saves processed IDs. 2nd iteration: PR merged.
        mock_open_prs.side_effect = [fake_pr, []]

        duration = daemon_runner.wait_for_pr_merge(
            gh_bin="gh",
            poll_interval=1,
            max_interval=2,
            agy_bin="/bin/true",
        )
        assert duration >= 0
        mock_address.assert_not_called()
        mock_save_processed.assert_called_once_with(99, ["comment_unresolvable_1"])


# ==============================================================================
# Prompt 0065: IDE GUI Session Sync Removal & Single-Run Definition of Done Tests
# ==============================================================================


def test_get_latest_conversation_id_from_summaries_db_and_brain_fallback(tmp_path: Path) -> None:
    # 1. When summaries db exists and has records
    db_file = tmp_path / "conversation_summaries.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, "
            "last_modified_time INTEGER"
            ")"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)",
            ("conv-older", 1000),
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)",
            ("conv-newer", 2000),
        )

    brain_dir = tmp_path / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("daemon_runner.CLI_SUMMARIES_DB", db_file),
        patch("daemon_runner.CLI_BRAIN_DIR", brain_dir),
    ):
        latest = daemon_runner.get_latest_conversation_id()
        assert latest == "conv-newer"

    # 2. When summaries db is absent, fall back to newest directory in brain
    nonexistent_db = tmp_path / "absent.db"
    dir_old = brain_dir / "conv-brain-old"
    dir_old.mkdir(parents=True, exist_ok=True)
    dir_new = brain_dir / "conv-brain-new"
    dir_new.mkdir(parents=True, exist_ok=True)
    hidden_dir = brain_dir / ".hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)

    # Set distinct modification times
    os.utime(dir_old, (1000.0, 1000.0))
    os.utime(dir_new, (2000.0, 2000.0))
    os.utime(hidden_dir, (3000.0, 3000.0))  # Should be ignored (.hidden)

    with (
        patch("daemon_runner.CLI_SUMMARIES_DB", nonexistent_db),
        patch("daemon_runner.CLI_BRAIN_DIR", brain_dir),
    ):
        latest_fallback = daemon_runner.get_latest_conversation_id()
        assert latest_fallback == "conv-brain-new"

    # 3. When both db is empty and brain dir has no directories
    empty_db = tmp_path / "empty.db"
    with sqlite3.connect(empty_db) as conn:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, "
            "last_modified_time INTEGER"
            ")"
        )
    empty_brain = tmp_path / "empty_brain"
    empty_brain.mkdir(parents=True, exist_ok=True)

    with (
        patch("daemon_runner.CLI_SUMMARIES_DB", empty_db),
        patch("daemon_runner.CLI_BRAIN_DIR", empty_brain),
    ):
        assert daemon_runner.get_latest_conversation_id() is None


def test_ide_gui_sync_symbols_removed() -> None:
    removed_symbols = [
        "IDE_DIR",
        "IDE_BRAIN_DIR",
        "IDE_CONV_DIR",
        "IDE_SUMMARIES_DB",
        "get_ide_project_id",
        "sync_conversation_to_ide",
        "link_latest_conversation_to_ide",
        "sync_all_cli_sessions_to_ide",
    ]
    for sym in removed_symbols:
        assert not hasattr(daemon_runner, sym), f"Expected {sym} to be removed from daemon_runner"


def test_execute_prompt_session_omits_project_flag(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0065-remove-daemon-ide-gui-session-sync.md"
    prompt_file.write_text("# Test Prompt", encoding="utf-8")
    impl_dir = tmp_path / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)

    turn_cmds: list[list[str]] = []

    def fake_multiturn_run(cmd, log_file, prompt_name, prompt_num, *args, **kwargs):
        turn_cmds.append(list(cmd))
        if len(turn_cmds) == 1:
            # Turn 1: WIP
            return (0, "Turn 1 output")
        else:
            # Turn 2: Finish
            (impl_dir / prompt_file.name).write_text("done")
            (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings")
            return (0, "Turn 2 output")

    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")),
        patch("daemon_runner.run_session_process", side_effect=fake_multiturn_run),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is True
        assert len(turn_cmds) == 2

        # Turn 1: kickoff command
        assert "--project" not in turn_cmds[0]
        # Turn 2: continuation command (-c)
        assert "--project" not in turn_cmds[1]
        assert "-c" in turn_cmds[1]


def test_dod_runs_once_during_session_and_not_duplicated_by_callers(tmp_path: Path) -> None:
    prompt_file = tmp_path / "0065-test.md"
    prompt_file.write_text("# Test", encoding="utf-8")
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    learnings_dir = tmp_path / "docs" / "learnings"
    learnings_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / prompt_file.name).write_text("done", encoding="utf-8")
    (learnings_dir / f"{prompt_file.stem}-learnings.md").write_text("learnings", encoding="utf-8")

    # 1. execute_prompt_session calls check_definition_of_done exactly once
    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.IMPLEMENTED_DIR", impl_dir),
        patch("daemon_runner.REPO_ROOT", tmp_path),
        patch("daemon_runner.check_definition_of_done", return_value=(True, "")) as mock_dod,
        patch("daemon_runner.run_session_process", return_value=(0, "Success")),
    ):
        success = daemon_runner.execute_prompt_session(
            agy_bin="agy",
            prompt_path=prompt_file,
        )
        assert success is True
        mock_dod.assert_called_once_with(prompt_file)

    # 2. address_pr_feedback does not invoke verify_definition_of_done
    with (
        patch("daemon_runner.get_current_branch", return_value="gemini/0065-test"),
        patch("daemon_runner.execute_prompt_session", return_value=True),
        patch("daemon_runner.verify_definition_of_done") as mock_verify_feedback,
        patch("daemon_runner.extract_latest_agent_summary", return_value="Summary"),
        patch("daemon_runner.save_processed_comment_ids"),
        patch("daemon_runner.run_cmd") as mock_cmd,
    ):
        mock_cmd.side_effect = [
            MockCompletedProcess(returncode=0, stdout="sha1\n"),  # git rev-parse HEAD
            MockCompletedProcess(returncode=0, stdout=""),  # git status --porcelain
            MockCompletedProcess(returncode=0, stdout="sha1\n"),  # git rev-parse HEAD
            MockCompletedProcess(returncode=0, stdout=""),  # gh pr comment
        ]
        res = daemon_runner.address_pr_feedback(
            gh_bin="gh",
            agy_bin="agy",
            pr_number=65,
            pr_title="PR Title",
            branch_name="gemini/0065-test",
            prompt_path=prompt_file,
            new_comments=[{"id": "c1", "author": "rev", "body": "Look good?"}],
        )
        assert res is True
        mock_verify_feedback.assert_not_called()

    # 3. address_ci_failure does not invoke verify_definition_of_done
    with (
        patch("daemon_runner.LOGS_DIR", tmp_path),
        patch("daemon_runner.get_current_branch", return_value="gemini/0065-test"),
        patch("daemon_runner.execute_prompt_session", return_value=True),
        patch("daemon_runner.verify_definition_of_done") as mock_verify_ci,
        patch("daemon_runner.extract_latest_agent_summary", return_value="Summary"),
        patch("daemon_runner.run_cmd") as mock_cmd,
    ):
        mock_cmd.side_effect = [
            MockCompletedProcess(returncode=0, stdout=""),  # git status --porcelain
            MockCompletedProcess(returncode=0, stdout=""),  # git push
            MockCompletedProcess(returncode=0, stdout="sha1\n"),  # git rev-parse HEAD
            MockCompletedProcess(returncode=0, stdout=""),  # gh pr comment
        ]
        res = daemon_runner.address_ci_failure(
            gh_bin="gh",
            agy_bin="agy",
            pr_number=65,
            pr_title="PR Title",
            branch_name="gemini/0065-test",
            prompt_path=prompt_file,
            run_info={"databaseId": 12345, "name": "CI"},
        )
        assert res is True
        mock_verify_ci.assert_not_called()

    # 4. main() does not invoke verify_definition_of_done after execute_prompt_session returns True
    with (
        patch("sys.argv", ["daemon_runner.py", "--once"]),
        patch("daemon_runner.get_gh_bin", return_value="gh"),
        patch("daemon_runner.get_agy_bin", return_value="agy"),
        patch("daemon_runner.initialize_baseline_permissions"),
        patch("daemon_runner.reconcile_startup_branch", return_value=("main", None, None)),
        patch("daemon_runner.wait_for_pr_merge", return_value=0.0),
        patch("daemon_runner.update_main"),
        patch("daemon_runner.get_next_prompt", return_value=prompt_file),
        patch("daemon_runner.setup_prompt_branch", return_value="gemini/0065-test"),
        patch("daemon_runner.execute_prompt_session", return_value=True),
        patch("daemon_runner.count_new_commits", return_value=1),
        patch("daemon_runner.ensure_pr_opened", return_value=True),
        patch("daemon_runner.get_current_branch", return_value="gemini/0065-test"),
        patch("daemon_runner.get_branch_pr_status", return_value={"number": 65}),
        patch("daemon_runner.record_daemon_metrics"),
        patch("daemon_runner.verify_definition_of_done") as mock_verify_main,
    ):
        daemon_runner.main()
        mock_verify_main.assert_not_called()


def test_get_latest_conversation_id_ignores_stale_sessions_and_prefers_newer_brain_dir(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "conversation_summaries.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, "
            "last_modified_time INTEGER"
            ")"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?)",
            ("conv-stale-0071", 1000),
        )

    brain_dir = tmp_path / "brain"
    stale_dir = brain_dir / "conv-stale-0071"
    stale_logs = stale_dir / ".system_generated" / "logs"
    stale_logs.mkdir(parents=True, exist_ok=True)
    stale_transcript = stale_logs / "transcript.jsonl"
    stale_transcript.write_text('{"step_index": 942}\n', encoding="utf-8")
    os.utime(stale_transcript, (1000.0, 1000.0))
    os.utime(stale_dir, (1000.0, 1000.0))

    with (
        patch("daemon_runner.CLI_SUMMARIES_DB", db_file),
        patch("daemon_runner.CLI_BRAIN_DIR", brain_dir),
    ):
        # 1. When a new session starts at t=2000.0 and no new conversation exists yet, return None
        assert daemon_runner.get_latest_conversation_id(since_timestamp=2000.0) is None

        # 2. When a new conversation directory appears in brain_dir at t=2005.0 (before DB updates)
        active_dir = brain_dir / "conv-active-0075"
        active_logs = active_dir / ".system_generated" / "logs"
        active_logs.mkdir(parents=True, exist_ok=True)
        active_transcript = active_logs / "transcript.jsonl"
        active_transcript.write_text('{"step_index": 1}\n', encoding="utf-8")
        os.utime(active_transcript, (2005.0, 2005.0))
        os.utime(active_dir, (2005.0, 2005.0))

        assert (
            daemon_runner.get_latest_conversation_id(since_timestamp=2000.0) == "conv-active-0075"
        )
        # Even without since_timestamp, the newer brain transcript wins over stale DB entry
        assert daemon_runner.get_latest_conversation_id() == "conv-active-0075"


def test_get_session_transcript_status_ignores_stale_transcripts_and_steps(
    tmp_path: Path,
) -> None:
    brain_dir = tmp_path / "brain"
    stale_conv = brain_dir / "conv-0071" / ".system_generated" / "logs"
    stale_conv.mkdir(parents=True, exist_ok=True)
    stale_file = stale_conv / "transcript.jsonl"
    stale_file.write_text(
        json.dumps(
            {
                "step_index": 942,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-10-16T13:00:00Z",
                "content": "Prompt 0071 complete",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stale_ts = daemon_runner.parse_iso_timestamp("2026-10-16T13:00:00Z")
    assert stale_ts is not None
    os.utime(stale_file, (stale_ts, stale_ts))

    session_start_ts = stale_ts + 600.0  # 10 minutes later

    with patch("daemon_runner.CLI_BRAIN_DIR", brain_dir):
        # Stale transcript file should be ignored when since_timestamp is newer
        assert (
            daemon_runner.get_session_transcript_status(
                "conv-0071", since_timestamp=session_start_ts
            )
            is None
        )
        assert (
            daemon_runner.get_session_transcript_status(None, since_timestamp=session_start_ts)
            is None
        )

        # If the file mtime is touched at session start, but its only step has an older created_at,
        # get_session_transcript_status still ignores the stale step
        os.utime(stale_file, (session_start_ts + 1.0, session_start_ts + 1.0))
        assert (
            daemon_runner.get_session_transcript_status(
                "conv-0071", since_timestamp=session_start_ts
            )
            is None
        )


def test_is_ci_billing_or_quota_failure() -> None:
    billing_annotation = (
        "ANNOTATIONS\n"
        "X The job was not started because recent account payments have failed "
        "or your spending limit needs to be increased. Please check the "
        "'Billing & plans' section in your settings\n"
        "Check (Python 3.13): .github#1\n"
    )
    assert daemon_runner.is_ci_billing_or_quota_failure(billing_annotation) is True
    assert (
        daemon_runner.is_ci_billing_or_quota_failure(
            {"databaseId": 39245125912, "failed_logs": billing_annotation}
        )
        is True
    )
    assert (
        daemon_runner.is_ci_billing_or_quota_failure(
            {"databaseId": 39245125912, "billing_or_quota_failure": True, "failed_logs": ""}
        )
        is True
    )
    assert (
        daemon_runner.is_ci_billing_or_quota_failure("FAILED tests/unit/test_foo.py::test_bar")
        is False
    )
    assert daemon_runner.is_ci_billing_or_quota_failure(None) is False


@patch("daemon_runner.run_cmd")
def test_fetch_latest_failed_ci_run_falls_back_to_run_view_for_billing_annotations(
    mock_run_cmd: MagicMock,
) -> None:
    runs_json = json.dumps(
        [
            {
                "databaseId": 39245125912,
                "status": "completed",
                "conclusion": "failure",
                "name": "CI",
                "workflowName": "CI",
                "url": "https://github.com/org/repo/actions/runs/39245125912",
            }
        ]
    )
    run_summary_output = (
        "JOBS\n"
        "X Check (Python 3.13) in 3s (ID 118035075900)\n"
        "X Check (Python 3.12) in 3s (ID 118035076723)\n\n"
        "ANNOTATIONS\n"
        "X The job was not started because recent account payments have failed or your "
        "spending limit needs to be increased. Please check the 'Billing & plans' section "
        "in your settings\n"
    )
    mock_run_cmd.side_effect = [
        MagicMock(returncode=0, stdout=runs_json, stderr=""),
        MagicMock(returncode=1, stdout="", stderr="log not found: 118035075900"),
        MagicMock(returncode=0, stdout=run_summary_output, stderr=""),
    ]

    result = daemon_runner.fetch_latest_failed_ci_run("gh", "gemini/0084-test")
    assert result is not None
    assert result["databaseId"] == 39245125912
    assert result["billing_or_quota_failure"] is True
    assert "spending limit needs to be increased" in result["failed_logs"]


@patch("daemon_runner.send_notification")
@patch("daemon_runner.mark_pr_ci_green")
@patch("daemon_runner.address_ci_failure")
@patch("daemon_runner.verify_local_tests_for_pr")
@patch("daemon_runner.find_prompt_path_for_branch")
@patch("daemon_runner.fetch_latest_failed_ci_run")
@patch("daemon_runner.get_pr_checks")
@patch("daemon_runner.fetch_unhandled_pr_feedback")
@patch("daemon_runner.get_open_prs")
@patch("daemon_runner._interruptible_sleep", return_value=False)
def test_wait_for_pr_merge_treats_ci_billing_failure_as_green_when_local_tests_pass(
    mock_wait: MagicMock,
    mock_open_prs: MagicMock,
    mock_feedback: MagicMock,
    mock_checks: MagicMock,
    mock_fetch_run: MagicMock,
    mock_find_prompt: MagicMock,
    mock_verify_local: MagicMock,
    mock_address_ci: MagicMock,
    mock_mark_green: MagicMock,
    mock_notify: MagicMock,
    tmp_path: Path,
) -> None:
    reviews_file = tmp_path / "pr_reviews.json"
    pr_obj = {
        "number": 128,
        "title": "feat: scoring judge retry",
        "headRefName": "gemini/0084-scoring-judge",
    }
    mock_open_prs.side_effect = [[pr_obj], [pr_obj], []]
    mock_feedback.return_value = []
    mock_checks.return_value = [
        {"name": "Check (Python 3.12)", "state": "FAILURE", "bucket": "fail"},
        {"name": "Check (Python 3.13)", "state": "FAILURE", "bucket": "fail"},
    ]
    mock_fetch_run.return_value = {
        "databaseId": 39245125912,
        "workflowName": "CI",
        "url": "https://github.com/org/repo/actions/runs/39245125912",
        "failed_logs": (
            "The job was not started because recent account payments have failed "
            "or your spending limit needs to be increased."
        ),
        "billing_or_quota_failure": True,
    }
    mock_find_prompt.return_value = tmp_path / "0084_test.md"
    mock_verify_local.return_value = (True, "")

    with patch("daemon_runner.PR_REVIEWS_FILE", reviews_file):
        daemon_runner.wait_for_pr_merge(
            "gh",
            poll_interval=10,
            agy_bin="/usr/local/bin/agy",
        )
        processed = daemon_runner.load_processed_comment_ids(128)

    # Local verification runs only once for the billing-failed run across multiple polls
    mock_verify_local.assert_called_once_with(
        "gemini/0084-scoring-judge", tmp_path / "0084_test.md"
    )
    # agy conversation is NOT resumed when billing/quota fails and local tests pass
    mock_address_ci.assert_not_called()
    # Treated as CI green while waiting for merge
    assert mock_mark_green.call_count == 2
    mock_notify.assert_called_once()
    assert "ci-run-39245125912" in processed
    assert "ci-billing-local-green-39245125912" in processed


@patch("daemon_runner.mark_pr_ci_green")
@patch("daemon_runner.address_ci_failure")
@patch("daemon_runner.verify_local_tests_for_pr")
@patch("daemon_runner.find_prompt_path_for_branch")
@patch("daemon_runner.fetch_latest_failed_ci_run")
@patch("daemon_runner.get_pr_checks")
@patch("daemon_runner.fetch_unhandled_pr_feedback")
@patch("daemon_runner.get_open_prs")
@patch("daemon_runner._interruptible_sleep", return_value=False)
def test_wait_for_pr_merge_heals_local_failure_when_ci_fails_due_to_billing(
    mock_wait: MagicMock,
    mock_open_prs: MagicMock,
    mock_feedback: MagicMock,
    mock_checks: MagicMock,
    mock_fetch_run: MagicMock,
    mock_find_prompt: MagicMock,
    mock_verify_local: MagicMock,
    mock_address_ci: MagicMock,
    mock_mark_green: MagicMock,
    tmp_path: Path,
) -> None:
    reviews_file = tmp_path / "pr_reviews.json"
    pr_obj = {
        "number": 128,
        "title": "feat: scoring judge retry",
        "headRefName": "gemini/0084-scoring-judge",
    }
    mock_open_prs.side_effect = [[pr_obj], []]
    mock_feedback.return_value = []
    mock_checks.return_value = [
        {"name": "Check (Python 3.12)", "state": "FAILURE", "bucket": "fail"}
    ]
    mock_fetch_run.return_value = {
        "databaseId": 39245125912,
        "workflowName": "CI",
        "url": "https://github.com/org/repo/actions/runs/39245125912",
        "failed_logs": (
            "The job was not started because recent account payments have failed "
            "or your spending limit needs to be increased."
        ),
        "billing_or_quota_failure": True,
    }
    prompt_file = tmp_path / "0084_test.md"
    mock_find_prompt.return_value = prompt_file
    mock_verify_local.return_value = (False, "FAILED tests/unit/test_broken.py")
    mock_address_ci.return_value = True

    with patch("daemon_runner.PR_REVIEWS_FILE", reviews_file):
        daemon_runner.wait_for_pr_merge(
            "gh",
            poll_interval=10,
            agy_bin="/usr/local/bin/agy",
        )

    mock_verify_local.assert_called_once_with("gemini/0084-scoring-judge", prompt_file)
    mock_address_ci.assert_called_once()
    run_info_passed = mock_address_ci.call_args.kwargs["run_info"]
    assert run_info_passed["failed_logs"] == "FAILED tests/unit/test_broken.py"
    mock_mark_green.assert_not_called()


def test_verify_git_staging_safety_allows_ignored_pr_description(tmp_path: Path) -> None:
    def mock_run_cmd(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        res = MagicMock()
        res.returncode = 0
        if cmd[:3] == ["git", "ls-files", "--others"]:
            res.stdout = "docs/PR_DESCRIPTION.md\n"
        elif cmd == ["git", "status", "--porcelain"]:
            res.stdout = ""
        else:
            res.stdout = ""
        res.stderr = ""
        return res

    with patch("daemon_runner.run_cmd", side_effect=mock_run_cmd):
        safe, issues = daemon_runner.verify_git_staging_safety(repo_root=tmp_path)
        assert safe is True
        assert issues == []

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Unit tests for `orchestack.config` and configurable runtime behavior in `daemon_runner`."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daemon_runner  # type: ignore[import-not-found]  # noqa: E402

from orchestack.config import (  # noqa: E402
    RepoConfig,
    configure_runtime,
    detect_base_branch,
    detect_check_cmd,
    load_repo_config,
)


@pytest.fixture(autouse=True)
def restore_daemon_runner_globals() -> Iterator[None]:
    """Ensure `daemon_runner` module globals are restored after each test."""
    orig = {
        "REPO_ROOT": daemon_runner.REPO_ROOT,
        "QUEUED_DIR": daemon_runner.QUEUED_DIR,
        "IMPLEMENTED_DIR": daemon_runner.IMPLEMENTED_DIR,
        "LEARNINGS_DIR": daemon_runner.LEARNINGS_DIR,
        "PR_DESC_PATH": daemon_runner.PR_DESC_PATH,
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


def test_repo_config_defaults_and_derived_paths(tmp_path: Path) -> None:
    cfg = RepoConfig(repo_root=tmp_path)
    resolved = tmp_path.resolve()

    assert cfg.repo_root == resolved
    assert cfg.base_branch == "main"
    assert cfg.branch_prefix == "orchestack"
    assert cfg.check_cmd == "make check"
    assert cfg.forge_type == "auto"
    assert cfg.agent_type == "gemini"
    assert cfg.require_learnings is True
    assert cfg.require_pr_description is True
    assert cfg.auto_whitelist_collaborators is True
    assert cfg.trusted_authors == []

    assert cfg.queued_dir == resolved / "prompts" / "queued"
    assert cfg.implemented_dir == resolved / "prompts" / "implemented"
    assert cfg.learnings_dir == resolved / "docs" / "learnings"
    assert cfg.pr_description_path == resolved / "docs" / "PR_DESCRIPTION.md"
    assert cfg.daemon_dir == resolved / ".daemon"
    assert cfg.logs_dir == resolved / "logs"
    assert cfg.lock_file == resolved / ".daemon" / "daemon.lock"
    assert cfg.review_state_file == resolved / ".daemon" / "review_state.json"
    assert cfg.pr_reviews_file == resolved / "logs" / "pr_reviews.json"
    assert cfg.daemon_metrics_file == resolved / "logs" / "daemon_metrics.jsonl"


def test_detect_base_branch_symbolic_ref(tmp_path: Path) -> None:
    mock_res = subprocess.CompletedProcess(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        returncode=0,
        stdout="refs/remotes/origin/develop\n",
        stderr="",
    )
    with patch("orchestack.config.subprocess.run", return_value=mock_res) as mock_run:
        assert detect_base_branch(tmp_path) == "develop"
        mock_run.assert_called_once()


def test_detect_base_branch_fallback_to_master(tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "symbolic-ref" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr="fatal")
        if "refs/remotes/origin/main" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        if "refs/remotes/origin/master" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="abc123\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("orchestack.config.subprocess.run", side_effect=fake_run):
        assert detect_base_branch(tmp_path) == "master"


def test_detect_base_branch_fallback_on_oserror(tmp_path: Path) -> None:
    with patch("orchestack.config.subprocess.run", side_effect=OSError("git not found")):
        assert detect_base_branch(tmp_path) == "main"


def test_detect_check_cmd_precedence(tmp_path: Path) -> None:
    # Empty directory -> "true"
    assert detect_check_cmd(tmp_path) == "true"

    # go.mod -> "go test ./..."
    (tmp_path / "go.mod").write_text("module example.com/mod\n", encoding="utf-8")
    assert detect_check_cmd(tmp_path) == "go test ./..."

    # Cargo.toml -> "cargo test"
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    assert detect_check_cmd(tmp_path) == "cargo test"

    # package.json -> "npm test"
    (tmp_path / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")
    assert detect_check_cmd(tmp_path) == "npm test"

    # pyproject.toml -> "pytest"
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    assert detect_check_cmd(tmp_path) == "pytest"

    # Makefile without check target -> still "pytest"
    (tmp_path / "Makefile").write_text("build:\n\techo building\n", encoding="utf-8")
    assert detect_check_cmd(tmp_path) == "pytest"

    # Makefile with check target -> "make check"
    (tmp_path / "Makefile").write_text("check:\n\tpytest\n", encoding="utf-8")
    assert detect_check_cmd(tmp_path) == "make check"


def test_load_repo_config_from_orchestack_toml(tmp_path: Path) -> None:
    toml_content = """
base_branch = "trunk"
branch_prefix = "bot"
check_cmd = "pytest -q"
forge = "gitlab"
agent = "claude"
require_learnings = false
require_pr_description = false
auto_whitelist_collaborators = false
trusted_authors = ["alice", "bob"]
"""
    (tmp_path / ".orchestack.toml").write_text(toml_content.strip(), encoding="utf-8")

    cfg = load_repo_config(repo_root=tmp_path, env={})
    assert cfg.repo_root == tmp_path.resolve()
    assert cfg.base_branch == "trunk"
    assert cfg.branch_prefix == "bot"
    assert cfg.check_cmd == "pytest -q"
    assert cfg.forge_type == "gitlab"
    assert cfg.agent_type == "claude"
    assert cfg.require_learnings is False
    assert cfg.require_pr_description is False
    assert cfg.auto_whitelist_collaborators is False
    assert cfg.trusted_authors == ["alice", "bob"]


def test_load_repo_config_sectioned_toml(tmp_path: Path) -> None:
    toml_content = """
[orchestack]
base_branch = "develop"
branch_prefix = "custom"
forge_type = "github"
agent_type = "codex"

[dod]
check_cmd = "cargo test --all"
require_learnings = false
"""
    (tmp_path / ".orchestack.toml").write_text(toml_content.strip(), encoding="utf-8")

    cfg = load_repo_config(repo_root=tmp_path, env={})
    assert cfg.base_branch == "develop"
    assert cfg.branch_prefix == "custom"
    assert cfg.forge_type == "github"
    assert cfg.agent_type == "codex"
    assert cfg.check_cmd == "cargo test --all"
    assert cfg.require_learnings is False
    assert cfg.require_pr_description is True


def test_load_repo_config_precedence_cli_over_env_over_toml(tmp_path: Path) -> None:
    (tmp_path / ".orchestack.toml").write_text(
        'base_branch = "toml-branch"\n'
        'branch_prefix = "toml-prefix"\n'
        'check_cmd = "toml-check"\n'
        'forge_type = "github"\n'
        'agent_type = "gemini"\n'
        "require_learnings = true\n"
        "require_pr_description = true\n",
        encoding="utf-8",
    )

    env = {
        "ORCHESTACK_REPO": str(tmp_path),
        "ORCHESTACK_BASE_BRANCH": "env-branch",
        "ORCHESTACK_BRANCH_PREFIX": "env-prefix",
        "ORCHESTACK_CHECK_CMD": "env-check",
        "ORCHESTACK_FORGE": "gitlab",
        "ORCHESTACK_AGENT": "claude",
        "ORCHESTACK_REQUIRE_LEARNINGS": "0",
        "ORCHESTACK_REQUIRE_PR_DESCRIPTION": "false",
        "ORCHESTACK_AUTO_WHITELIST_COLLABORATORS": "no",
        "ORCHESTACK_TRUSTED_AUTHORS": "carol, dave ",
    }

    # 1. Env overrides TOML
    env_cfg = load_repo_config(env=env)
    assert env_cfg.repo_root == tmp_path.resolve()
    assert env_cfg.base_branch == "env-branch"
    assert env_cfg.branch_prefix == "env-prefix"
    assert env_cfg.check_cmd == "env-check"
    assert env_cfg.forge_type == "gitlab"
    assert env_cfg.agent_type == "claude"
    assert env_cfg.require_learnings is False
    assert env_cfg.require_pr_description is False
    assert env_cfg.auto_whitelist_collaborators is False
    assert env_cfg.trusted_authors == ["carol", "dave"]

    # 2. CLI overrides Env and TOML
    cli_cfg = load_repo_config(
        repo_root=tmp_path,
        base_branch="cli-branch",
        branch_prefix="cli-prefix",
        check_cmd="cli-check",
        forge_type="local",
        agent_type="codex",
        require_learnings=True,
        require_pr_description=True,
        auto_whitelist_collaborators=True,
        trusted_authors=["eve"],
        env=env,
    )
    assert cli_cfg.base_branch == "cli-branch"
    assert cli_cfg.branch_prefix == "cli-prefix"
    assert cli_cfg.check_cmd == "cli-check"
    assert cli_cfg.forge_type == "local"
    assert cli_cfg.agent_type == "codex"
    assert cli_cfg.require_learnings is True
    assert cli_cfg.require_pr_description is True
    assert cli_cfg.auto_whitelist_collaborators is True
    assert cli_cfg.trusted_authors == ["eve"]


def test_load_repo_config_invalid_bool_env(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ORCHESTACK_REQUIRE_LEARNINGS"):
        load_repo_config(
            repo_root=tmp_path,
            env={"ORCHESTACK_REQUIRE_LEARNINGS": "not-a-bool"},
        )


def test_configure_runtime_rebinds_daemon_runner_globals(tmp_path: Path) -> None:
    cfg = RepoConfig(
        repo_root=tmp_path,
        base_branch="develop",
        branch_prefix="orchestack",
        check_cmd="pytest -v",
        require_learnings=False,
        require_pr_description=False,
    )
    returned = configure_runtime(cfg)
    assert returned is cfg
    assert tmp_path.resolve() == daemon_runner.REPO_ROOT
    assert cfg.queued_dir == daemon_runner.QUEUED_DIR
    assert cfg.implemented_dir == daemon_runner.IMPLEMENTED_DIR
    assert cfg.learnings_dir == daemon_runner.LEARNINGS_DIR
    assert cfg.pr_description_path == daemon_runner.PR_DESC_PATH
    assert cfg.daemon_dir == daemon_runner.DAEMON_DIR
    assert cfg.lock_file == daemon_runner.LOCK_FILE
    assert cfg.review_state_file == daemon_runner.REVIEW_STATE_FILE
    assert cfg.logs_dir == daemon_runner.LOGS_DIR
    assert cfg.pr_reviews_file == daemon_runner.PR_REVIEWS_FILE
    assert cfg.daemon_metrics_file == daemon_runner.DAEMON_METRICS_FILE
    assert daemon_runner.BASE_BRANCH == "develop"
    assert daemon_runner.BRANCH_PREFIX == "orchestack"
    assert daemon_runner.CHECK_CMD == "pytest -v"
    assert daemon_runner.REQUIRE_LEARNINGS is False
    assert daemon_runner.REQUIRE_PR_DESCRIPTION is False
    assert daemon_runner.ACTIVE_CONFIG is cfg


def test_check_definition_of_done_configurable_options(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompts" / "queued" / "0007-custom-repo.md"
    impl_dir = tmp_path / "prompts" / "implemented"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / prompt_file.name).write_text("# Prompt 0007\n", encoding="utf-8")

    # 1. require_learnings=False, require_pr_description=False, custom check_cmd="pytest -q"
    cfg_no_docs = RepoConfig(
        repo_root=tmp_path,
        check_cmd="pytest -q",
        require_learnings=False,
        require_pr_description=False,
    )
    mock_ok = subprocess.CompletedProcess(["pytest", "-q"], returncode=0, stdout="ok", stderr="")
    with patch("daemon_runner.run_cmd", return_value=mock_ok) as mock_run:
        ok, err = daemon_runner.check_definition_of_done(prompt_file, config=cfg_no_docs)
        assert ok is True
        assert err == ""
        assert mock_run.call_args[0][0] == ["pytest", "-q"]

    # 2. require_pr_description=True fails when PR_DESCRIPTION.md is missing
    cfg_req_pr = RepoConfig(
        repo_root=tmp_path,
        check_cmd="true",
        require_learnings=False,
        require_pr_description=True,
    )
    ok, err = daemon_runner.check_definition_of_done(prompt_file, config=cfg_req_pr)
    assert ok is False
    assert "docs/PR_DESCRIPTION.md" in err

    # Create non-empty PR_DESCRIPTION.md -> passes with check_cmd="true"
    cfg_req_pr.pr_description_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_req_pr.pr_description_path.write_text("# PR Description\nDetails here.\n", encoding="utf-8")
    ok, err = daemon_runner.check_definition_of_done(prompt_file, config=cfg_req_pr)
    assert ok is True
    assert err == ""


def test_git_and_branch_helpers_respect_configured_base_branch_and_prefix(tmp_path: Path) -> None:
    cfg = RepoConfig(
        repo_root=tmp_path,
        base_branch="master",
        branch_prefix="orchestack",
    )
    daemon_runner.configure_runtime(cfg)

    impl_dir = cfg.implemented_dir
    impl_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = impl_dir / "0012-custom-branch.md"
    prompt_file.write_text("# 0012\n", encoding="utf-8")

    # find_prompt_path_for_branch resolves both orchestack/ and gemini/ prefixes
    assert daemon_runner.find_prompt_path_for_branch("orchestack/0012-custom-branch") == prompt_file
    assert (
        daemon_runner.find_prompt_path_for_branch("orchestack/0012-custom-branch-impl")
        == prompt_file
    )
    assert daemon_runner.find_prompt_path_for_branch("gemini/0012-custom-branch") == prompt_file

    # count_new_commits uses origin/master
    with patch(
        "daemon_runner.run_cmd",
        return_value=subprocess.CompletedProcess([], returncode=0, stdout="3\n", stderr=""),
    ) as mock_run:
        assert daemon_runner.count_new_commits("orchestack/0012-custom-branch") == 3
        mock_run.assert_called_once_with(
            ["git", "rev-list", "--count", "origin/master..orchestack/0012-custom-branch"],
            check=False,
        )

    # ensure_clean_main / update_main uses configured base_branch ("master")
    with (
        patch("daemon_runner.check_network_connectivity", return_value=True),
        patch(
            "daemon_runner.run_cmd",
            return_value=subprocess.CompletedProcess([], returncode=0, stdout="", stderr=""),
        ) as mock_run,
    ):
        daemon_runner.ensure_clean_main()
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "fetch", "origin", "master"] in called_cmds
        assert ["git", "checkout", "master"] in called_cmds
        assert ["git", "pull", "--ff-only", "origin", "master"] in called_cmds

    # setup_prompt_branch creates branch with configured prefix off origin/master
    def fake_setup_cmd(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="master\n", stderr="")
        if cmd[:3] == ["git", "show-ref", "--verify"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch("daemon_runner.run_cmd", side_effect=fake_setup_cmd) as mock_run:
        branch = daemon_runner.setup_prompt_branch(prompt_file, fresh=True)
        assert branch == "orchestack/0012-custom-branch"
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert [
            "git",
            "checkout",
            "-B",
            "orchestack/0012-custom-branch",
            "origin/master",
        ] in called_cmds

    # ensure_pr_opened targets --base master
    with (
        patch("daemon_runner.get_branch_pr_status", return_value=None),
        patch("daemon_runner.build_pr_description", return_value="PR body"),
        patch("daemon_runner.send_notification", MagicMock()),
        patch(
            "daemon_runner.run_cmd",
            return_value=subprocess.CompletedProcess(
                [], returncode=0, stdout="https://github.com/org/repo/pull/1\n", stderr=""
            ),
        ) as mock_run,
    ):
        assert daemon_runner.ensure_pr_opened("gh", "orchestack/0012-custom-branch", prompt_file)
        pr_create_cmd = mock_run.call_args_list[-1].args[0]
        assert "--base" in pr_create_cmd
        base_idx = pr_create_cmd.index("--base")
        assert pr_create_cmd[base_idx + 1] == "master"

    # is_prompt_merged_into_origin_main checks origin/master
    with patch(
        "daemon_runner.run_cmd",
        return_value=subprocess.CompletedProcess([], returncode=0, stdout="", stderr=""),
    ) as mock_run:
        assert daemon_runner.is_prompt_merged_into_origin_main("0012-custom-branch") is True
        mock_run.assert_called_once_with(
            ["git", "cat-file", "-e", "origin/master:prompts/implemented/0012-custom-branch.md"],
            check=False,
        )

    # get_in_progress_prompt_branches queries refs/heads/orchestack/
    with patch(
        "daemon_runner.run_cmd",
        return_value=subprocess.CompletedProcess(
            [], returncode=0, stdout="orchestack/0012-custom-branch\n", stderr=""
        ),
    ) as mock_run:
        branches = daemon_runner.get_in_progress_prompt_branches()
        assert branches == [(12, "0012-custom-branch", "orchestack/0012-custom-branch")]
        mock_run.assert_called_once_with(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/orchestack/"],
            check=False,
        )


def test_main_cli_flags_configure_runtime(tmp_path: Path) -> None:
    queued = tmp_path / "prompts" / "queued"
    queued.mkdir(parents=True, exist_ok=True)
    (queued / "0001-demo.md").write_text("# 0001\n", encoding="utf-8")

    argv = [
        "daemon_runner.py",
        "--repo",
        str(tmp_path),
        "--base-branch",
        "develop",
        "--branch-prefix",
        "custom",
        "--check-cmd",
        "pytest -q",
        "--forge",
        "gitlab",
        "--agent",
        "claude",
        "--no-require-learnings",
        "--no-require-pr-description",
        "--dry-run",
    ]
    with (
        patch.object(sys, "argv", argv),
        patch("daemon_runner.get_gh_bin", return_value="gh"),
        patch("daemon_runner.get_open_prs", return_value=[]),
        patch("daemon_runner.get_current_branch", return_value="develop"),
    ):
        daemon_runner.main()

    assert tmp_path.resolve() == daemon_runner.REPO_ROOT
    assert daemon_runner.BASE_BRANCH == "develop"
    assert daemon_runner.BRANCH_PREFIX == "custom"
    assert daemon_runner.CHECK_CMD == "pytest -q"
    assert daemon_runner.REQUIRE_LEARNINGS is False
    assert daemon_runner.REQUIRE_PR_DESCRIPTION is False
    assert daemon_runner.ACTIVE_CONFIG is not None
    assert daemon_runner.ACTIVE_CONFIG.forge_type == "gitlab"
    assert daemon_runner.ACTIVE_CONFIG.agent_type == "claude"

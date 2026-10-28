# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Blue Team Harness Authors

"""Autonomous daemon runner for queued Blue Team Harness prompts.

Monitors the repository for merged pull requests, pulls latest main,
picks the lowest-numbered prompt in prompts/queued/, creates a fresh
branch, and spawns a clean coding agent session via the Antigravity CLI (agy).

Implements Option 1: Least-Privilege Sandboxed Execution with an interactive
security gate. Commands not in the granular allowlist are caught, and the user
is prompted interactively whether to whitelist them.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import queue
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path and _SRC_DIR.is_dir():
    sys.path.insert(0, str(_SRC_DIR))

from orchestack.agents.antigravity import (  # noqa: E402
    NETWORK_AND_TRANSIENT_PATTERNS as _AGY_NETWORK_AND_TRANSIENT_PATTERNS,
)
from orchestack.agents.antigravity import (  # noqa: E402
    AntigravityAgent,
)
from orchestack.agents.antigravity import (  # noqa: E402
    _get_conversation_mtime as _agy_get_conversation_mtime,
)
from orchestack.agents.antigravity import (  # noqa: E402
    _normalize_db_timestamp as _agy_normalize_db_timestamp,
)
from orchestack.agents.antigravity import (  # noqa: E402
    add_allowed_rules as _agy_add_allowed_rules,
)
from orchestack.agents.antigravity import (  # noqa: E402
    check_recent_soft_denial_in_cli_log as _agy_check_recent_soft_denial_in_cli_log,
)
from orchestack.agents.antigravity import (  # noqa: E402
    check_recent_transcript_denial as _agy_check_recent_transcript_denial,
)
from orchestack.agents.antigravity import (  # noqa: E402
    extract_denied_permission as _agy_extract_denied_permission,
)
from orchestack.agents.antigravity import (  # noqa: E402
    extract_latest_agent_summary as _agy_extract_latest_agent_summary,
)
from orchestack.agents.antigravity import (  # noqa: E402
    extract_session_transcript_metrics as _agy_extract_session_transcript_metrics,
)
from orchestack.agents.antigravity import (  # noqa: E402
    get_latest_conversation_id as _agy_get_latest_conversation_id,
)
from orchestack.agents.antigravity import (  # noqa: E402
    get_session_transcript_status as _agy_get_session_transcript_status,
)
from orchestack.agents.antigravity import (  # noqa: E402
    initialize_baseline_permissions as _agy_initialize_baseline_permissions,
)
from orchestack.agents.antigravity import (  # noqa: E402
    is_transient_session_error as _agy_is_transient_session_error,
)
from orchestack.agents.antigravity import (  # noqa: E402
    parse_denial_from_step as _agy_parse_denial_from_step,
)
from orchestack.agents.antigravity import (  # noqa: E402
    parse_iso_timestamp as _agy_parse_iso_timestamp,
)
from orchestack.agents.base import AgentBackend  # noqa: E402
from orchestack.agents.claude_code import ClaudeCodeAgent  # noqa: E402
from orchestack.agents.codex import CodexAgent  # noqa: E402
from orchestack.agents.factory import create_agent_backend  # noqa: E402
from orchestack.config import RepoConfig, load_repo_config  # noqa: E402
from orchestack.forge.github import (  # noqa: E402
    CI_BILLING_OR_QUOTA_PATTERNS as _FORGE_CI_BILLING_OR_QUOTA_PATTERNS,
)
from orchestack.forge.github import (  # noqa: E402
    GitHubForge,
    truncate_pr_comment,
)
from orchestack.forge.github import (  # noqa: E402
    is_ci_billing_or_quota_failure as _forge_is_ci_billing_or_quota_failure,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_REPO_ROOT = REPO_ROOT
QUEUED_DIR = REPO_ROOT / "prompts" / "queued"
IMPLEMENTED_DIR = REPO_ROOT / "prompts" / "implemented"
LEARNINGS_DIR = REPO_ROOT / "docs" / "learnings"
_DEFAULT_LEARNINGS_DIR = LEARNINGS_DIR
PR_DESC_PATH = REPO_ROOT / "docs" / "PR_DESCRIPTION.md"
_DEFAULT_PR_DESC_PATH = PR_DESC_PATH
DAEMON_DIR = REPO_ROOT / ".daemon"
LOCK_FILE = DAEMON_DIR / "daemon.lock"
REVIEW_STATE_FILE = DAEMON_DIR / "review_state.json"
LOGS_DIR = REPO_ROOT / "logs"
PR_REVIEWS_FILE = LOGS_DIR / "pr_reviews.json"
DAEMON_METRICS_FILE = LOGS_DIR / "daemon_metrics.jsonl"
BASE_BRANCH = "main"
BRANCH_PREFIX = "gemini"
CHECK_CMD = "make check"
REQUIRE_LEARNINGS = True
REQUIRE_PR_DESCRIPTION = False
ACTIVE_CONFIG: RepoConfig | None = None
_LAST_BACKEND_RESPONSE: str | None = None
_LAST_BACKEND_SESSION_ID: str | None = None
DAEMON_TAG = "🤖 **[BlueTeamHarness Daemon]**"
DEFAULT_MAX_AUTO_HEAL_ATTEMPTS = 3
DEFAULT_MODEL = "gemini-3.8-flash-high"
DEFAULT_EFFORT = "high"
PROTECTED_PRECHECK_DIRS = ("tests/fixtures", "src", "docs")
BENIGN_IGNORED_PATTERNS = (
    "__pycache__",
    ".pyc",
    ".pyo",
    ".pyd",
    ".DS_Store",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "docs/PR_DESCRIPTION.md",
    "PR_DESCRIPTION.md",
)

CLI_DIR = Path.home() / ".gemini" / "antigravity-cli"

SETTINGS_FILE = CLI_DIR / "settings.json"
CLI_BRAIN_DIR = CLI_DIR / "brain"
CLI_CONV_DIR = CLI_DIR / "conversations"
CLI_SUMMARIES_DB = CLI_DIR / "conversation_summaries.db"
CLI_LOG_FILE = CLI_DIR / "cli.log"

ALT_REPO_ROOT = Path.home() / "OneDrive - Virgin Media O2" / "Code" / "BlueTeamHarness"

# Granular, least-privilege baseline allowlist
# In Antigravity CLI, command(target) matches exact command, while
# command(target *) matches command with any arguments or flags.
# write_file permissions are strictly scoped to the project repository.
SAFE_BASELINE_ALLOW = [
    # File authoring strictly scoped to the project folder (directory paths without globs)
    f"write_file({REPO_ROOT})",
    f"write_file({REPO_ROOT}/)",
    f"write_file({REPO_ROOT.resolve()})",
    f"write_file({REPO_ROOT.resolve()}/)",
    f"write_file({ALT_REPO_ROOT})",
    f"write_file({ALT_REPO_ROOT}/)",
    # Git directory permissions for internal repository operations (staging, committing, locking)
    f"write_file({REPO_ROOT}/.git)",
    f"write_file({REPO_ROOT}/.git/)",
    f"write_file({REPO_ROOT.resolve()}/.git)",
    f"write_file({REPO_ROOT.resolve()}/.git/)",
    f"write_file({ALT_REPO_ROOT}/.git)",
    f"write_file({ALT_REPO_ROOT}/.git/)",
    # Development commands in sandbox
    "command(git)",
    "command(git *)",
    "command(uv)",
    "command(uv *)",
    "command(make)",
    "command(make *)",
    "command(bth)",
    "command(bth *)",
    "command(grep)",
    "command(grep *)",
    # Network access for python package resolution (e.g. uv add duckdb)
    "unsandboxed(uv)",
    "unsandboxed(uv *)",
]

DEFAULT_DENY = [
    # Protect sensitive host system paths (must be absolute paths starting with /)
    f"write_file({Path.home()}/.ssh)",
    f"write_file({Path.home()}/.ssh/)",
    "write_file(/etc)",
    "write_file(/etc/)",
    # Dangerous system commands
    "command(sudo)",
    "command(sudo *)",
    "command(rm -rf)",
    "command(rm -rf *)",
    "command(curl)",
    "command(curl *)",
    "command(wget)",
    "command(wget *)",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bth-daemon")


def configure_runtime(config: RepoConfig) -> RepoConfig:
    """Rebind module-level repository paths and settings from a `RepoConfig`."""
    global REPO_ROOT, QUEUED_DIR, IMPLEMENTED_DIR, LEARNINGS_DIR, PR_DESC_PATH
    global DAEMON_DIR, LOCK_FILE, REVIEW_STATE_FILE, LOGS_DIR, PR_REVIEWS_FILE
    global DAEMON_METRICS_FILE, BASE_BRANCH, BRANCH_PREFIX, CHECK_CMD
    global REQUIRE_LEARNINGS, REQUIRE_PR_DESCRIPTION, ACTIVE_CONFIG

    REPO_ROOT = config.repo_root
    QUEUED_DIR = config.queued_dir
    IMPLEMENTED_DIR = config.implemented_dir
    LEARNINGS_DIR = config.learnings_dir
    PR_DESC_PATH = config.pr_description_path
    DAEMON_DIR = config.daemon_dir
    LOCK_FILE = config.lock_file
    REVIEW_STATE_FILE = config.review_state_file
    LOGS_DIR = config.logs_dir
    PR_REVIEWS_FILE = config.pr_reviews_file
    DAEMON_METRICS_FILE = config.daemon_metrics_file
    BASE_BRANCH = config.base_branch
    BRANCH_PREFIX = config.branch_prefix
    CHECK_CMD = config.check_cmd
    REQUIRE_LEARNINGS = config.require_learnings
    REQUIRE_PR_DESCRIPTION = config.require_pr_description
    ACTIVE_CONFIG = config

    for rule in (
        f"write_file({REPO_ROOT})",
        f"write_file({REPO_ROOT}/)",
        f"write_file({REPO_ROOT.resolve()})",
        f"write_file({REPO_ROOT.resolve()}/)",
        f"write_file({REPO_ROOT}/.git)",
        f"write_file({REPO_ROOT}/.git/)",
        f"write_file({REPO_ROOT.resolve()}/.git)",
        f"write_file({REPO_ROOT.resolve()}/.git/)",
    ):
        if rule not in SAFE_BASELINE_ALLOW:
            SAFE_BASELINE_ALLOW.append(rule)

    return config


def find_binary(name: str, extra_paths: list[Path] | None = None) -> str | None:
    """Find executable in PATH or extra specified locations."""
    found = shutil.which(name)
    if found:
        return found
    if extra_paths:
        for p in extra_paths:
            candidate = p / name if p.is_dir() else p
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def get_gh_bin() -> str:
    """Locate the GitHub CLI executable."""
    extra = [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path.home() / "bin",
    ]
    gh = find_binary("gh", extra)
    if not gh:
        logger.error("GitHub CLI ('gh') not found. Please install it with: brew install gh")
        sys.exit(1)
    return gh


def get_agy_bin() -> str:
    """Locate the Antigravity CLI executable."""
    extra = [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    agy = find_binary("agy", extra)
    if not agy:
        logger.error(
            "Antigravity CLI ('agy') not found in PATH or ~/.local/bin/agy.\n"
            "To install it, run:\n"
            "  curl -fsSL https://antigravity.google/cli/install.sh | bash\n"
            "Then re-run this script."
        )
        sys.exit(1)
    return agy


def send_notification(
    title: str,
    message: str,
    sound: str | None = "Glass",
    subtitle: str | None = None,
) -> bool:
    """Send a lightweight desktop notification (macOS osascript) with optional sound alert."""
    if sys.platform != "darwin":
        return False
    try:
        clean_title = title.replace("\\", "\\\\").replace('"', '\\"')
        clean_msg = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{clean_msg}" with title "{clean_title}"'
        if subtitle:
            clean_sub = subtitle.replace("\\", "\\\\").replace('"', '\\"')
            script += f' subtitle "{clean_sub}"'
        if sound:
            clean_sound = sound.replace("\\", "\\\\").replace('"', '\\"')
            script += f' sound name "{clean_sound}"'
        res = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return res.returncode == 0
    except Exception as e:
        logger.debug("Failed to dispatch desktop notification: %s", e)
        return False


def _make_antigravity_agent(agy_bin: str = "agy") -> AntigravityAgent:
    """Create an AntigravityAgent bound to current daemon_runner path globals."""
    return AntigravityAgent(
        agy_bin=agy_bin,
        cli_dir=CLI_DIR,
        brain_dir=CLI_BRAIN_DIR,
        summaries_db=CLI_SUMMARIES_DB,
        cli_log_file=CLI_LOG_FILE,
        settings_file=SETTINGS_FILE,
    )


def initialize_baseline_permissions() -> None:
    """Configure least-privilege permissions in settings.json."""
    _agy_initialize_baseline_permissions(
        settings_file=SETTINGS_FILE,
        repo_root=REPO_ROOT,
        safe_baseline_allow=SAFE_BASELINE_ALLOW,
        default_deny=DEFAULT_DENY,
    )


def add_allowed_rules(rules: list[str] | str) -> None:
    """Add verified rule(s) to permissions.allow in settings.json."""
    _agy_add_allowed_rules(rules, settings_file=SETTINGS_FILE)


add_allowed_rule = add_allowed_rules


def parse_iso_timestamp(ts_str: Any) -> float | None:
    """Parse an ISO 8601 timestamp string from a transcript step into a POSIX timestamp."""
    return _agy_parse_iso_timestamp(ts_str)


def parse_denial_from_step(data: dict[str, Any]) -> tuple[str, str | None] | None:
    """Extract perm_kind and target from a transcript step if it recorded a denial."""
    return _agy_parse_denial_from_step(data)


def extract_denied_permission(
    full_output: str,
    perm_hint: str | None = None,
    conv_id: str | None = None,
    since_timestamp: float = 0.0,
) -> tuple[str, str | None]:
    """Inspect CLI output, recent transcripts, or CLI logs to find denied permission and target."""
    return _agy_extract_denied_permission(
        full_output,
        perm_hint=perm_hint,
        conv_id=conv_id,
        since_timestamp=since_timestamp,
        brain_dir=CLI_BRAIN_DIR,
        cli_log_file=CLI_LOG_FILE,
    )


def check_recent_soft_denial_in_cli_log(since_offset: int = 0) -> str | None:
    """Inspect recent CLI log lines for soft-denied tool confirmations in print mode."""
    return _agy_check_recent_soft_denial_in_cli_log(
        since_offset=since_offset,
        cli_log_file=CLI_LOG_FILE,
    )


def check_recent_transcript_denial(
    since_timestamp: float = 0.0,
    conv_id: str | None = None,
) -> tuple[str, str | None] | None:
    """Check if the latest step in active conversation transcript recorded a denial."""
    return _agy_check_recent_transcript_denial(
        since_timestamp=since_timestamp,
        conv_id=conv_id,
        brain_dir=CLI_BRAIN_DIR,
    )


def extract_denied_command() -> str | None:
    """Legacy helper returning denied command target if any."""
    return extract_denied_permission("")[1]


def _get_conversation_mtime(conv_dir: Path) -> float:
    """Return the most recent modification time for a conversation directory and its transcripts."""
    return _agy_get_conversation_mtime(conv_dir)


def _normalize_db_timestamp(raw_ts: Any) -> float | None:
    """Normalize a database last_modified_time value (s/ms/us/ns or ISO string) to POSIX seconds."""
    return _agy_normalize_db_timestamp(raw_ts)


def get_latest_conversation_id(since_timestamp: float = 0.0) -> str | None:
    """Find the latest CLI conversation ID from CLI_SUMMARIES_DB or CLI_BRAIN_DIR."""
    return _agy_get_latest_conversation_id(
        since_timestamp=since_timestamp,
        brain_dir=CLI_BRAIN_DIR,
        summaries_db=CLI_SUMMARIES_DB,
    )


def extract_session_transcript_metrics(conv_id: str | None = None) -> dict[str, Any]:
    """Parse session transcript to extract steps, tool counts, and estimated token/cost metrics."""
    return _agy_extract_session_transcript_metrics(
        conv_id=conv_id,
        brain_dir=CLI_BRAIN_DIR,
    )


def record_daemon_metrics(record: dict[str, Any]) -> None:
    """Append a structured metrics event to logs/daemon_metrics.jsonl."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        if "timestamp" not in record:
            record["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with DAEMON_METRICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        logger.info(
            "📊 Recorded session telemetry metrics to %s (steps: %d, tokens: ~%d, est cost: $%.4f)",
            DAEMON_METRICS_FILE.name,
            record.get("total_steps", 0),
            record.get("total_tokens_estimate", 0),
            record.get("estimated_cost_usd", 0.0),
        )
    except Exception as e:
        logger.warning("Could not append to %s: %s", DAEMON_METRICS_FILE, e)


def extract_latest_agent_summary(conv_id: str | None = None) -> str | None:
    """Extract the concluding planner response from the latest session transcript."""
    summary = _agy_extract_latest_agent_summary(
        conv_id=conv_id,
        brain_dir=CLI_BRAIN_DIR,
    )
    if summary:
        return summary
    if _LAST_BACKEND_RESPONSE:
        return _LAST_BACKEND_RESPONSE
    return None


def prompt_user_permission_action(
    perm_info: tuple[str, str | None],
) -> tuple[str, str | None]:
    """Ask user interactively whether to approve a denied tool or redirect the session.

    Returns:
        ("allow", rule_string) -> Rule was added to settings.json, continue session.
        ("redirect", guidance_string) -> User denied permission and provided redirection guidance.
        ("abort", None) -> User chose to halt daemon execution.
    """
    perm_kind, target = perm_info

    print("\n" + "=" * 80)
    if perm_kind == "read_url":
        domain = ""
        if target:
            try:
                domain = urllib.parse.urlparse(target).netloc
            except Exception:
                domain = ""
        print("🔒 [SECURITY GATE] External URL access was denied by the sandbox policy:")
        print(f"   URL   : {target or '(unknown URL)'}")
        if domain:
            print(f"   Domain: {domain}")
    elif perm_kind == "write_file":
        print("🔒 [SECURITY GATE] File write was denied by the sandbox policy:")
        print(f"   Target: {target or 'Workspace file write'}")
    elif perm_kind == "unsandboxed":
        print("🔒 [SECURITY GATE] Unsandboxed execution (network/host) was denied:")
        print(f"   Command: {target or 'External process'}")
    else:
        print("🔒 [SECURITY GATE] A command was denied by the sandbox policy:")
        print(f"   Command: {target or 'Sandbox command'}")
    print("=" * 80)

    if perm_kind == "read_url":
        domain = ""
        if target:
            try:
                domain = urllib.parse.urlparse(target).netloc
            except Exception:
                domain = ""
        rule_specific = f"read_url({target})" if target else "read_url(*)"
        rule_domain = f"read_url({domain}/*)" if domain else f"read_url({target or '*'})"

        print("\nHow would you like to handle this URL access request?")
        if target:
            print(f"  [1] Allow this specific URL only:\n      {rule_specific}")
        else:
            print("  [1] Enter URL to allow")
        if domain:
            print(f"  [2] Allow this domain:\n      {rule_domain}")
        else:
            print("  [2] Enter domain to allow")
        print("  [3] Enter custom pattern (e.g. read_url(docs.python.org/*))")
        print("  [4] Send redirect message to session (Deny access and instruct agent to pivot)")
        print("  [5] Deny and abort (keep branch for inspection)")

        while True:
            choice = input("\nSelect an option [1-5]: ").strip()
            if choice == "1":
                rule = rule_specific if target else input("Enter URL rule: ").strip()
                if rule and not rule.startswith("read_url("):
                    rule = f"read_url({rule})"
                add_allowed_rules(rule)
                return "allow", rule
            if choice == "2":
                rule = rule_domain if domain else input("Enter domain rule: ").strip()
                if rule and not rule.startswith("read_url("):
                    rule = f"read_url({rule})"
                add_allowed_rules(rule)
                return "allow", rule
            if choice == "3":
                custom = input("Enter rule pattern: ").strip()
                if custom:
                    if not custom.startswith("read_url("):
                        custom = f"read_url({custom})"
                    add_allowed_rules(custom)
                    return "allow", custom
            if choice == "4":
                print("\nEnter guidance message to redirect session (press Enter for default):")
                default_msg = (
                    "Do not fetch external URLs. Rely only on existing packages and standard "
                    "library."
                )
                print(f'Default: "{default_msg}"')
                guidance = input("> ").strip()
                if not guidance:
                    guidance = default_msg
                return "redirect", guidance
            if choice == "5":
                print("Permission denied. Halting execution.")
                return "abort", None
            print("Invalid choice. Please enter 1, 2, 3, 4, or 5.")

    elif perm_kind == "write_file":
        rules_project = [
            f"write_file({REPO_ROOT})",
            f"write_file({REPO_ROOT}/)",
            f"write_file({REPO_ROOT.resolve()})",
            f"write_file({REPO_ROOT.resolve()}/)",
            f"write_file({ALT_REPO_ROOT})",
            f"write_file({ALT_REPO_ROOT}/)",
        ]
        rule_specific = [f"write_file({target})"] if target else rules_project

        print("\nHow would you like to handle this file write permission?")
        print(f"  [1] Allow writing to all files in project folder:\n      write_file({REPO_ROOT})")
        if target:
            print(f"  [2] Allow writing to this specific file:\n      {rule_specific[0]}")
        print("  [3] Enter custom pattern (e.g. write_file(src/))")
        print("  [4] Send redirect message to session (Deny write and instruct agent to pivot)")
        print("  [5] Deny and abort (keep branch for inspection)")

        while True:
            choice = input("\nSelect an option [1-5]: ").strip()
            if choice == "1":
                add_allowed_rules(rules_project)
                return "allow", f"write_file({REPO_ROOT})"
            if choice == "2" and target:
                add_allowed_rules(rule_specific)
                return "allow", rule_specific[0]
            if choice == "3":
                custom = input("Enter rule pattern: ").strip()
                if custom:
                    if not custom.startswith("write_file("):
                        custom = f"write_file({custom})"
                    add_allowed_rules(custom)
                    return "allow", custom
            if choice == "4":
                print("\nEnter guidance message to redirect session (press Enter for default):")
                default_msg = (
                    "Do not write files outside repository root. Confine operations to workspace."
                )
                print(f'Default: "{default_msg}"')
                guidance = input("> ").strip()
                if not guidance:
                    guidance = default_msg
                return "redirect", guidance
            if choice == "5":
                print("Permission denied. Halting execution.")
                return "abort", None
            print("Invalid choice. Please enter 1, 2, 3, 4, or 5.")

    elif perm_kind == "unsandboxed":
        cmd_str = target or ""
        binary = cmd_str.split()[0] if cmd_str.split() else cmd_str
        rules_exact = [f"unsandboxed({cmd_str})"]
        if f"unsandboxed({cmd_str} *)" not in rules_exact:
            rules_exact.append(f"unsandboxed({cmd_str} *)")
        rules_prefix = [f"unsandboxed({binary})", f"unsandboxed({binary} *)"]

        print("\nHow would you like to handle this unsandboxed command?")
        print(f"  [1] Allow unsandboxed command and sub-arguments:\n      {', '.join(rules_exact)}")
        print(f"  [2] Allow unsandboxed executable prefix:\n      {', '.join(rules_prefix)}")
        print("  [3] Enter custom rule pattern (e.g. unsandboxed(uv *))")
        print("  [4] Send redirect message to session (Deny unsandboxed, instruct agent to pivot)")
        print("  [5] Deny and abort (keep branch for inspection)")

        while True:
            choice = input("\nSelect an option [1-5]: ").strip()
            if choice == "1":
                add_allowed_rules(rules_exact)
                return "allow", rules_exact[0]
            if choice == "2":
                add_allowed_rules(rules_prefix)
                return "allow", rules_prefix[0]
            if choice == "3":
                custom = input("Enter rule pattern: ").strip()
                if custom:
                    if not custom.startswith("unsandboxed("):
                        custom = f"unsandboxed({custom})"
                    add_allowed_rules(custom)
                    return "allow", custom
            if choice == "4":
                print("\nEnter guidance message to redirect session (press Enter for default):")
                default_msg = (
                    "Do not run unsandboxed host commands. Implement using only standard "
                    "sandbox tools."
                )
                print(f'Default: "{default_msg}"')
                guidance = input("> ").strip()
                if not guidance:
                    guidance = default_msg
                return "redirect", guidance
            if choice == "5":
                print("Permission denied. Halting execution.")
                return "abort", None
            print("Invalid choice. Please enter 1, 2, 3, 4, or 5.")

    else:
        # Default: command
        cmd_str = target or ""
        binary = cmd_str.split()[0] if cmd_str.split() else cmd_str
        rule_exact = [f"command({cmd_str})"] if cmd_str else []
        if cmd_str and f"command({cmd_str} *)" not in rule_exact:
            rule_exact.append(f"command({cmd_str} *)")
        rule_prefix = [f"command({binary})", f"command({binary} *)"] if binary else []

        print("\nHow would you like to handle this command?")
        if rule_exact:
            print(f"  [1] Allow command and sub-arguments:\n      {', '.join(rule_exact)}")
            print(f"  [2] Allow executable prefix:\n      {', '.join(rule_prefix)}")
            print("  [3] Enter custom rule pattern (e.g. command(regex:uv run .*))")
        else:
            print("  [1] Enter command pattern to allow (e.g. command(make *))")
        print("  [4] Send redirect message to session (Deny command and instruct agent to pivot)")
        print("  [5] Deny and abort (keep branch for inspection)")

        while True:
            choice = input("\nSelect an option [1-5]: ").strip()
            if choice == "1":
                if rule_exact:
                    add_allowed_rules(rule_exact)
                    return "allow", rule_exact[0]
                else:
                    custom = input("Enter rule pattern: ").strip()
                    if custom:
                        if not custom.startswith("command("):
                            custom = f"command({custom})"
                        add_allowed_rules(custom)
                        return "allow", custom
            if choice == "2" and rule_prefix:
                add_allowed_rules(rule_prefix)
                return "allow", rule_prefix[0]
            if choice == "3":
                custom = input("Enter rule pattern: ").strip()
                if custom:
                    if not custom.startswith("command("):
                        custom = f"command({custom})"
                    add_allowed_rules(custom)
                    return "allow", custom
            if choice == "4":
                print("\nEnter guidance message to redirect session (press Enter for default):")
                default_msg = (
                    f"Do not execute '{cmd_str}'. Use an alternative approach or sandbox commands."
                    if cmd_str
                    else "Do not run requested command. Use standard sandbox commands or libraries."
                )
                print(f'Default: "{default_msg}"')
                guidance = input("> ").strip()
                if not guidance:
                    guidance = default_msg
                return "redirect", guidance
            if choice == "5":
                print("Command denied. Halting execution.")
                return "abort", None
            print("Invalid choice. Please enter 1, 2, 3, 4, or 5.")


def prompt_user_to_whitelist(perm_info: tuple[str, str | None]) -> bool:
    """Legacy helper returning True if permission was approved, False otherwise."""
    action, _ = prompt_user_permission_action(perm_info)
    return action == "allow"


def get_next_prompt() -> Path | None:
    """Find the lowest-numbered prompt in prompts/queued/."""
    if not QUEUED_DIR.is_dir():
        return None

    prompt_files: list[tuple[int, Path]] = []
    for path in QUEUED_DIR.glob("*.md"):
        match = re.match(r"^(\d{4})-", path.name)
        if match:
            prompt_files.append((int(match.group(1)), path))

    if not prompt_files:
        return None

    prompt_files.sort(key=lambda x: x[0])
    return prompt_files[0][1]


def find_prompt_path_for_branch(
    branch_name: str,
    branch_prefix: str | None = None,
) -> Path | None:
    """Resolve prompt markdown path from branch name, stripping suffixes like -impl.

    Searches both prompts/implemented/ and prompts/queued/.
    Handles:
    - Direct branch name or slug: gemini/0056-... or orchestack/0056-... -> 0056-...
    - Suffixes appended to branch names (e.g., -impl)
    - Fallback matching on 4-digit prompt ID prefix (e.g., 0056)
    """
    if not branch_name:
        return None

    slug = branch_name
    for prefix_candidate in (branch_prefix, BRANCH_PREFIX, "gemini", "orchestack"):
        if prefix_candidate and slug.startswith(f"{prefix_candidate}/"):
            slug = slug.removeprefix(f"{prefix_candidate}/")
            break
    else:
        slug = re.sub(r"^[a-zA-Z0-9_.-]+/(?=\d{4}-)", "", slug)

    candidates_to_try = [slug]
    if slug.endswith("-impl"):
        candidates_to_try.append(slug.removesuffix("-impl"))

    for s in candidates_to_try:
        for directory in (IMPLEMENTED_DIR, QUEUED_DIR):
            candidate = directory / f"{s}.md"
            if candidate.exists():
                return candidate

    # Fallback to 4-digit prompt prefix if available (e.g. 0056-)
    match = re.match(r"^(\d{4})-", slug)
    if match:
        prefix = match.group(1)
        for directory in (IMPLEMENTED_DIR, QUEUED_DIR):
            if directory.is_dir():
                matches = sorted(directory.glob(f"{prefix}-*.md"))
                if matches:
                    return matches[0]

    return None


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a shell command with repo context and optional timeout safety check."""
    target_cwd = REPO_ROOT if cwd is None else cwd
    logger.debug("Executing: %s", " ".join(cmd))
    try:
        res = subprocess.run(
            cmd,
            cwd=target_cwd,
            check=check,
            capture_output=capture_output,
            text=text,
            input=input,
            timeout=timeout,
        )
        return res
    except subprocess.TimeoutExpired as e:
        logger.error(
            "Command '%s' timed out after %.1fs.",
            " ".join(cmd),
            timeout or 0.0,
        )
        if check:
            raise
        stdout_str = (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, bytes)
            else str(e.stdout or "")
        )
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=stdout_str,
            stderr=f"Command timed out after {timeout}s",
        )
    except subprocess.CalledProcessError as e:
        logger.error(
            "Command '%s' failed with exit status %d.\nStderr: %s\nStdout: %s",
            " ".join(cmd),
            e.returncode,
            (e.stderr or "").strip(),
            (e.stdout or "").strip(),
        )
        raise


def count_new_commits(branch_name: str, base_branch: str | None = None) -> int:
    """Count commits on branch_name that are not on origin/<base_branch>."""
    base = base_branch or BASE_BRANCH
    res = run_cmd(["git", "rev-list", "--count", f"origin/{base}..{branch_name}"], check=False)
    if res.returncode == 0 and res.stdout.strip().isdigit():
        return int(res.stdout.strip())
    return 0


def verify_git_staging_safety(repo_root: Path | None = None) -> tuple[bool, list[str]]:
    """Verify that newly created files in critical directories are not accidentally ignored.

    Prevents scenarios where new test fixtures, code files, or documentation are ignored by
    overbroad .gitignore rules or .git/info/exclude, which causes missing fixture errors in CI.
    """
    if repo_root is None:
        repo_root = REPO_ROOT
    issues: list[str] = []

    # 1. Check for files in protected dirs that are ignored by git rules
    try:
        cmd = [
            "git",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            *PROTECTED_PRECHECK_DIRS,
        ]
        res = run_cmd(cmd, cwd=repo_root, check=False)
        if res.returncode == 0 and res.stdout.strip():
            ignored_lines = res.stdout.splitlines()
            for raw_line in ignored_lines:
                rel_path = raw_line.strip()
                if not rel_path:
                    continue
                # Skip known compiler and tool cache files
                if any(benign in rel_path for benign in BENIGN_IGNORED_PATTERNS):
                    continue

                # Identify which rule ignored this file
                check_res = run_cmd(
                    ["git", "check-ignore", "-v", rel_path], cwd=repo_root, check=False
                )
                rule_desc = (
                    check_res.stdout.strip() if check_res.returncode == 0 else "unknown rule"
                )
                issues.append(f"File '{rel_path}' is accidentally ignored by git rule: {rule_desc}")
    except Exception as e:
        logger.warning("Error running git ignored files check: %s", e)

    # 2. Check for untracked files in protected dirs
    try:
        status_res = run_cmd(["git", "status", "--porcelain"], cwd=repo_root, check=False)
        if status_res.returncode == 0 and status_res.stdout.strip():
            for line in status_res.stdout.splitlines():
                if line.startswith("?? "):
                    untracked_path = line[3:].strip()
                    if any(
                        untracked_path == d or untracked_path.startswith(f"{d}/")
                        for d in (*PROTECTED_PRECHECK_DIRS, "prompts")
                    ):
                        if any(benign in untracked_path for benign in BENIGN_IGNORED_PATTERNS):
                            continue
                        issues.append(f"Untracked file in protected directory: '{untracked_path}'")
    except Exception as e:
        logger.warning("Error checking untracked files: %s", e)

    if issues:
        for issue in issues:
            logger.error("❌ [GIT SAFETY PRE-CHECK] %s", issue)
        return False, issues

    logger.info("✅ Git staging safety pre-check passed: no critical files accidentally ignored.")
    return True, []


def _make_github_forge(gh_bin: str = "gh") -> GitHubForge:
    """Create a `GitHubForge` bound to active runtime callables for monkeypatch compatibility."""
    return GitHubForge(
        repo_root=REPO_ROOT,
        gh_bin=gh_bin,
        daemon_tag=DAEMON_TAG,
        run_cmd_fn=run_cmd,
        check_network_fn=check_network_connectivity,
        wait_network_fn=wait_for_network_connectivity,
    )


def get_github_repo(gh_bin: str = "gh") -> str:
    """Return the GitHub `owner/repo` slug for the repository."""
    return _make_github_forge(gh_bin).get_repo_slug()


def get_open_prs(gh_bin: str, non_blocking: bool = False) -> list[dict[str, str]]:
    """Return active open pull requests for the repository."""
    return _make_github_forge(gh_bin).list_open_prs_raw(non_blocking=non_blocking)


def get_branch_pr_status(gh_bin: str, branch_name: str) -> dict[str, Any] | None:
    """Return PR metadata for a branch across all states (OPEN, MERGED, CLOSED).

    Returns a dict with 'number', 'state', 'title', and 'url', or None if no PR exists.
    """
    return _make_github_forge(gh_bin).get_branch_pr_status(branch_name)


def get_pr_checks(gh_bin: str, pr_number: int | str) -> list[dict[str, Any]]:
    """Fetch GitHub check runs for a PR."""
    return _make_github_forge(gh_bin).get_pr_checks(pr_number)


def format_interval(seconds: int) -> str:
    """Format duration in seconds into human-readable string (e.g. 30s, 1m, 2m, 30m)."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    rem = seconds % 60
    if rem == 0:
        return f"{minutes}m"
    return f"{minutes}m {rem}s"


def mark_pr_ci_green(gh_bin: str, pr_number: int) -> bool:
    """Inspect PR description and ensure CI checkbox in Definition of Done is marked [x]."""
    return _make_github_forge(gh_bin).mark_pr_ci_green(pr_number)


def get_current_branch() -> str:
    """Get active git branch name."""
    res = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def update_main(base_branch: str | None = None) -> None:
    """Fetch latest changes from origin and fast-forward base branch with retries."""
    base = base_branch or BASE_BRANCH
    logger.info("Syncing latest %s from origin...", base)

    # Clean up any stale git lock files left over from crashes or OneDrive sync
    git_dir = REPO_ROOT / ".git"
    if git_dir.is_dir():
        for lock_file in git_dir.rglob("*.lock"):
            try:
                lock_file.unlink(missing_ok=True)
                logger.warning("Removed stale git lock file: %s", lock_file)
            except Exception as e:
                logger.debug("Could not remove lock file %s: %s", lock_file, e)

    # Retry git fetch up to 4 times with exponential backoff for network/cloud resilience
    max_retries = 4
    for attempt in range(1, max_retries + 1):
        if not check_network_connectivity():
            wait_for_network_connectivity()
        res = run_cmd(["git", "fetch", "origin", base], check=False)
        if res.returncode == 0:
            break
        err_msg = (res.stderr or res.stdout or "").strip()
        logger.warning(
            "git fetch origin %s failed (attempt %d/%d): %s",
            base,
            attempt,
            max_retries,
            err_msg,
        )
        if attempt < max_retries:
            time.sleep(3 * attempt)
        else:
            res.check_returncode()

    run_cmd(["git", "checkout", base])
    pull_res = run_cmd(["git", "pull", "--ff-only", "origin", base], check=False)
    if pull_res.returncode != 0:
        logger.warning(
            "git pull --ff-only failed (%s). Resetting %s hard to origin/%s...",
            (pull_res.stderr or pull_res.stdout or "").strip(),
            base,
            base,
        )
        run_cmd(["git", "reset", "--hard", f"origin/{base}"])


def ensure_clean_main(base_branch: str | None = None) -> None:
    """Ensure the repository is checked out to the clean base branch and synced with origin."""
    update_main(base_branch=base_branch)


def is_prompt_merged_into_origin_main(slug: str, base_branch: str | None = None) -> bool:
    """Check if the prompt has already been merged into origin/<base_branch>."""
    base = base_branch or BASE_BRANCH
    clean_slug = slug.removesuffix("-impl")
    slugs_to_check = [clean_slug] if clean_slug == slug else [clean_slug, slug]
    for s in slugs_to_check:
        res = run_cmd(
            ["git", "cat-file", "-e", f"origin/{base}:prompts/implemented/{s}.md"],
            check=False,
        )
        if res.returncode == 0:
            return True

    # Fallback to checking by 4-digit prefix in origin/<base_branch>:prompts/implemented/
    match = re.match(r"^(\d{4})-", slug)
    if match:
        prefix = match.group(1)
        tree_res = run_cmd(
            ["git", "ls-tree", "--name-only", f"origin/{base}:prompts/implemented/"],
            check=False,
        )
        if tree_res.returncode == 0 and tree_res.stdout:
            for fname in tree_res.stdout.splitlines():
                if fname.startswith(f"{prefix}-") and fname.endswith(".md"):
                    return True

    return False


def setup_prompt_branch(
    prompt_path: Path,
    resume: bool = False,
    fresh: bool = False,
    gh_bin: str | None = None,
    base_branch: str | None = None,
    branch_prefix: str | None = None,
) -> str:
    """Create a clean branch for the target prompt per PROTOCOL.md §2."""
    base = base_branch or BASE_BRANCH
    prefix = branch_prefix or BRANCH_PREFIX
    slug = prompt_path.stem
    branch_name = f"{prefix}/{slug}"
    if (
        gh_bin
        and not resume
        and (branch_pr := get_branch_pr_status(gh_bin, branch_name))
        and str(branch_pr.get("state", "")).upper() == "MERGED"
        and not is_prompt_merged_into_origin_main(slug, base_branch=base)
    ):
        logger.warning(
            "Branch '%s' was already merged on GitHub in PR #%s, but prompt %s is not yet "
            "in origin/%s. Using branch '%s-impl' to avoid branch reuse collision.",
            branch_name,
            branch_pr.get("number"),
            slug,
            base,
            branch_name,
        )
        branch_name = f"{branch_name}-impl"

    current_branch = get_current_branch()
    if resume and current_branch == branch_name:
        logger.info("Resuming on existing branch '%s'...", branch_name)
        return branch_name

    ref_check = run_cmd(["git", "show-ref", "--verify", f"refs/heads/{branch_name}"], check=False)
    branch_exists = ref_check.returncode == 0

    if branch_exists and not fresh:
        logger.info("Switching to existing branch '%s'...", branch_name)
        run_cmd(["git", "checkout", branch_name])
        return branch_name

    logger.info("Creating clean branch '%s' off origin/%s...", branch_name, base)
    run_cmd(["git", "checkout", "-B", branch_name, f"origin/{base}"])
    for pr_desc_file in {PR_DESC_PATH, REPO_ROOT / "docs" / "PR_DESCRIPTION.md"}:
        if pr_desc_file.is_file():
            with contextlib.suppress(OSError):
                pr_desc_file.unlink()
    return branch_name


def get_session_transcript_status(
    conv_id: str | None,
    since_timestamp: float = 0.0,
) -> tuple[int | str, str] | None:
    """Read the latest step from transcript.jsonl for real-time progress reporting."""
    return _agy_get_session_transcript_status(
        conv_id,
        since_timestamp=since_timestamp,
        brain_dir=CLI_BRAIN_DIR,
    )


def extract_previous_cumulative_time(log_file: Path) -> float:
    """Extract cumulative runtime in seconds from the most recent status entry in log_file."""
    if not log_file.exists():
        return 0.0
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if "⏳ [" not in line:
                continue
            # Look for "total: <interval>" or standalone "<interval>"
            m_total = re.search(r"⏳ \[(?:total:\s*)?((?:(?:\d+h\s*)?\d+m\s*)?\d+s)", line)
            if m_total:
                time_str = m_total.group(1).strip()
                seconds = 0
                m_h = re.search(r"(\d+)h", time_str)
                m_m = re.search(r"(\d+)m", time_str)
                m_s = re.search(r"(\d+)s", time_str)
                if m_h:
                    seconds += int(m_h.group(1)) * 3600
                if m_m:
                    seconds += int(m_m.group(1)) * 60
                if m_s:
                    seconds += int(m_s.group(1))
                if seconds > 0:
                    return float(seconds)
    except Exception as e:
        logger.debug("Failed extracting previous cumulative time from %s: %s", log_file, e)
    return 0.0


def run_session_process(
    cmd: list[str],
    log_file: Path,
    prompt_name: str,
    prompt_num: str,
    active_time_offset: float = 0.0,
) -> tuple[int, str]:
    """Execute agy process and stream output to terminal with live activity feedback."""
    session_output: list[str] = []
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write(f"\n--- Invocation: {' '.join(cmd)} ---\n")
        proc_start_time = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        out_queue: queue.Queue[str | None] = queue.Queue()

        def stream_reader() -> None:
            if proc.stdout:
                for line in proc.stdout:
                    out_queue.put(line)
            out_queue.put(None)

        reader_thread = threading.Thread(target=stream_reader, daemon=True)
        reader_thread.start()

        time.sleep(1.0)
        conv_id = get_latest_conversation_id(since_timestamp=proc_start_time)

        start_time = time.time()
        last_sync = time.time()
        last_output_time = time.time()
        last_heartbeat_time = time.time()
        last_reported_step: tuple[int | str, str] | None = None
        step_start_time = time.time()
        last_step_warning_time = time.time()

        try:
            while True:
                # 1. Drain any available lines from stdout queue
                while True:
                    try:
                        line = out_queue.get_nowait()
                        if line is None:
                            break
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        lf.write(line)
                        session_output.append(line)
                        last_output_time = time.time()
                    except queue.Empty:
                        break

                now = time.time()

                # Periodically refresh conversation ID for live progress reporting
                sync_interval = 1.0 if conv_id is None else 15.0
                if now - last_sync > sync_interval:
                    conv_id = get_latest_conversation_id(since_timestamp=proc_start_time)
                    last_sync = now

                # Check if process has terminated
                if proc.poll() is not None:
                    # Drain remaining queue lines
                    while True:
                        try:
                            line = out_queue.get(timeout=0.2)
                            if line is None:
                                break
                            sys.stdout.write(line)
                            sys.stdout.flush()
                            lf.write(line)
                            session_output.append(line)
                        except queue.Empty:
                            break
                    break

                # 2. Status feedback when Gemini is silent (or whenever step changes)
                status_info = get_session_transcript_status(
                    conv_id, since_timestamp=proc_start_time
                )
                agy_elapsed = int(now - start_time)
                total_elapsed = int(active_time_offset + agy_elapsed)
                agy_elapsed_str = format_interval(agy_elapsed)
                total_elapsed_str = format_interval(total_elapsed)
                timer_label = f"total: {total_elapsed_str} | agy: {agy_elapsed_str}"

                # Step changed?
                if status_info and status_info != last_reported_step:
                    if now - last_heartbeat_time >= 2.0 or last_reported_step is None:
                        step_idx, desc = status_info
                        msg = f"⏳ [{timer_label} | Step {step_idx}] {desc}"
                        logger.info(msg)
                        lf.write(f"\n[STATUS {time.strftime('%H:%M:%S')}] {msg}\n")
                        lf.flush()
                        last_reported_step = status_info
                        last_heartbeat_time = now
                        step_start_time = now
                        last_step_warning_time = now
                elif now - last_output_time >= 12.0 and now - last_heartbeat_time >= 15.0:
                    # Heartbeat during quiet periods (e.g. LLM thinking or long test command)
                    if status_info:
                        step_idx, desc = status_info
                        step_duration_sec = now - step_start_time
                        step_duration = format_interval(int(step_duration_sec))
                        msg = (
                            f"⏳ [{timer_label} | Step {step_idx}] {desc} "
                            f"(in progress for {step_duration})"
                        )
                        # Safety check: detect tests/steps running longer than intended (> 10m)
                        if step_duration_sec >= 600.0 and (now - last_step_warning_time >= 120.0):
                            warn_msg = (
                                f"⚠️ [WATCHDOG] Step {step_idx} taking {step_duration} (> 10m). "
                                "Commands typically complete in < 1m. Check for hangs or deadlocks."
                            )
                            logger.warning(warn_msg)
                            lf.write(f"\n[WATCHDOG {time.strftime('%H:%M:%S')}] {warn_msg}\n")
                            lf.flush()
                            last_step_warning_time = now
                    else:
                        msg = f"⏳ [{timer_label}] Gemini working in background (PID {proc.pid})..."
                    logger.info(msg)
                    lf.write(f"\n[HEARTBEAT {time.strftime('%H:%M:%S')}] {msg}\n")
                    lf.flush()
                    last_heartbeat_time = now

                time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("\n🛑 Interrupted by user (Ctrl+C). Terminating Antigravity session...")
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=3.0)
            if proc.poll() is None:
                proc.kill()
            sys.exit(0)

        proc.wait()

    return proc.returncode, "".join(session_output)


NETWORK_AND_TRANSIENT_PATTERNS: list[tuple[str, str]] = list(_AGY_NETWORK_AND_TRANSIENT_PATTERNS)


def check_network_connectivity(timeout: float = 3.0) -> bool:
    """Check if internet connectivity is available."""
    targets = [("googleapis.com", 443), ("1.1.1.1", 53), ("8.8.8.8", 53)]
    for target, port in targets:
        try:
            with socket.create_connection((target, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def wait_for_network_connectivity(
    initial_interval: float = 2.0,
    max_interval: float = 30.0,
    heartbeat_interval: float = 60.0,
) -> bool:
    """Wait with exponential backoff until internet connectivity is restored (until Ctrl+C)."""
    if check_network_connectivity():
        return True

    logger.warning(
        "⚠️  Network connection lost (laptop lid closed, Wi-Fi dropped, or offline). "
        "Waiting for internet connectivity to be restored (press Ctrl+C to abort)..."
    )

    start_time = time.time()
    last_log_time = start_time
    interval = initial_interval

    try:
        while True:
            # Responsive sleep in 0.5s chunks to catch Ctrl+C immediately
            sleep_end = time.time() + interval
            while time.time() < sleep_end:
                time.sleep(0.5)

            if check_network_connectivity():
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                duration_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                logger.info(
                    "🌐 Network connectivity restored after %s! Resuming...",
                    duration_str,
                )
                return True

            interval = min(interval * 1.5, max_interval)
            now = time.time()
            if now - last_log_time >= heartbeat_interval:
                elapsed = int(now - start_time)
                mins, secs = divmod(elapsed, 60)
                logger.info(
                    "⏳ [Network offline] Waiting for connection "
                    "(%dm %ds elapsed, checking every %ds)...",
                    mins,
                    secs,
                    int(interval),
                )
                last_log_time = now
    except KeyboardInterrupt:
        logger.info("\n🛑 Interrupted by user while waiting for network. Halting daemon.")
        sys.exit(0)


def is_transient_session_error(full_output: str, returncode: int) -> tuple[bool, str]:
    """Check if session failed due to a transient API, HTTP, or network error."""
    return _agy_is_transient_session_error(full_output, returncode)


def _execute_backend_prompt_session(
    backend: AgentBackend,
    prompt_path: Path,
    log_file: Path,
    title: str,
    kickoff_prompt: str,
    continuation_prompt: str,
    resume: bool = False,
    custom_prompt: str | None = None,
) -> bool:
    """Execute a multi-turn prompt session using a generic `AgentBackend`."""
    global _LAST_BACKEND_RESPONSE, _LAST_BACKEND_SESSION_ID

    is_first_turn = not resume
    retry_count = 0
    max_retries = 5
    turn_count = 1
    max_turns = 10
    session_id: str | None = backend.get_latest_session_id(REPO_ROOT) if resume else None

    while True:
        current_prompt = kickoff_prompt if is_first_turn else continuation_prompt
        is_first_turn = False

        result = backend.run_turn(
            prompt=current_prompt,
            repo_root=REPO_ROOT,
            session_id=session_id,
            cli_log_path=log_file,
        )
        if result.session_id:
            session_id = result.session_id
            _LAST_BACKEND_SESSION_ID = session_id
        if result.last_response:
            _LAST_BACKEND_RESPONSE = result.last_response

        # 1. Check for turn-scoped permission denials (B2)
        denials = backend.detect_permission_denials(
            session_id=session_id,
            cli_log_path=log_file,
            cli_log_offset=result.cli_log_offset,
            turn_start_time=result.turn_start_time,
        )
        if denials:
            denial = denials[0]
            logger.warning(
                "Tool permission denied in %s backend (%s: %s).",
                backend.name,
                denial.tool_name,
                denial.resource,
            )
            whitelisted = backend.whitelist_denial(denial, REPO_ROOT)
            if whitelisted:
                detail = denial.command_prefix or denial.resource
                logger.info("Whitelisted %s (%s). Resuming session...", denial.tool_name, detail)
                if custom_prompt:
                    continuation_prompt = (
                        f"Tool permission for '{detail}' has been granted. "
                        f"Please proceed with addressing the reviewer feedback for "
                        f"{prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        f"Tool permission for '{detail}' has been granted. "
                        "Please proceed with implementation per PROTOCOL.md."
                    )
                retry_count = 0
                continue
            logger.error("Could not whitelist permission denial for %s.", denial)
            return False

        # 2. Check for transient errors
        err_text = (result.error_message or "") + "\n" + (result.last_response or "")
        is_transient, reason = is_transient_session_error(err_text, result.exit_code)
        if result.exit_code != 0 and is_transient:
            if retry_count < max_retries:
                retry_count += 1
                backoff_seconds = min(10 * (2 ** (retry_count - 1)), 120)
                logger.warning(
                    "⚠️  Transient error in %s session for %s: %s (exit code %d). "
                    "Auto-resuming in %ds (retry %d/%d)...",
                    backend.name,
                    prompt_path.name,
                    reason,
                    result.exit_code,
                    backoff_seconds,
                    retry_count,
                    max_retries,
                )
                time.sleep(backoff_seconds)
                if custom_prompt:
                    continuation_prompt = (
                        f"A transient error ({reason}) interrupted the previous turn. "
                        f"Please continue addressing the reviewer feedback for "
                        f"{prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        f"A transient error ({reason}) interrupted the previous turn. "
                        f"Please review the repository state and continue implementing "
                        f"prompts/queued/{prompt_path.name} per PROTOCOL.md."
                    )
                continue
            logger.error(
                "Session for %s exceeded maximum transient error retries (%d). Halting.",
                prompt_path.name,
                max_retries,
            )
            return False

        if not result.completed_normally or result.exit_code != 0:
            logger.error(
                "Session (%s) exited with failure code %d (%s). See %s.",
                backend.name,
                result.exit_code,
                result.error_message,
                log_file,
            )
            return False

        # 3. Check Definition of Done
        impl_path = IMPLEMENTED_DIR / prompt_path.name
        if impl_path.exists():
            learnings_dir = (
                LEARNINGS_DIR
                if LEARNINGS_DIR != _DEFAULT_LEARNINGS_DIR
                else REPO_ROOT / "docs" / "learnings"
            )
            learnings_file = learnings_dir / f"{prompt_path.stem}-learnings.md"
            if REQUIRE_LEARNINGS and not learnings_file.exists() and turn_count < max_turns:
                turn_count += 1
                continuation_prompt = (
                    f"The prompt file was moved to prompts/implemented/{prompt_path.name}, but "
                    f"docs/learnings/{learnings_file.name} is missing. "
                    "Per PROTOCOL.md §5, author this learnings file and docs/PR_DESCRIPTION.md, "
                    f"and ensure `{CHECK_CMD}` passes."
                )
                continue

            dod_ok, dod_error = check_definition_of_done(prompt_path)
            if dod_ok:
                logger.info(
                    "Session (%s) completed and Definition of Done verified for prompt %s.",
                    backend.name,
                    prompt_path.name,
                )
                return True

            if turn_count < max_turns:
                turn_count += 1
                continuation_prompt = (
                    f"Definition of Done verification failed for "
                    f"prompts/implemented/{prompt_path.name}.\n\n"
                    f"{dod_error}\n\n"
                    f"Please diagnose and resolve this issue and ensure `{CHECK_CMD}` "
                    "passes locally."
                )
                continue
            return False

        if turn_count < max_turns:
            turn_count += 1
            continuation_prompt = (
                f"Please continue implementing prompts/queued/{prompt_path.name} per PROTOCOL.md. "
                "The implementation is still in progress. Follow all steps until the Definition "
                "of Done is fully satisfied "
                f"(move prompt to prompts/implemented/{prompt_path.name}, "
                f"author docs/learnings/{prompt_path.stem}-learnings.md, "
                f"author docs/PR_DESCRIPTION.md, and verify that `{CHECK_CMD}` passes locally)."
            )
            continue
        return False


def execute_prompt_session(
    agy_bin: str,
    prompt_path: Path,
    model: str | None = None,
    effort: str | None = None,
    resume: bool = False,
    custom_prompt: str | None = None,
    agent_backend: AgentBackend | None = None,
) -> bool:
    """Run an agent session with sandbox isolation and an interactive/automated approval loop."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"session-{prompt_path.stem}.log"

    match = re.match(r"^(\d{4})", prompt_path.name)
    prompt_num = match.group(1) if match else prompt_path.stem
    title = f"BlueTeamHarness - Prompt {prompt_num}"

    if custom_prompt:
        kickoff_prompt = custom_prompt
        continuation_prompt = custom_prompt
        resume = True
    else:
        kickoff_prompt = (
            f"{title}: Read docs/PROTOCOL.md and follow it strictly. "
            f"Implement prompts/queued/{prompt_path.name}. "
            "Do not start any other prompt in this session.\n\n"
            "MANDATORY PR DESCRIPTION REQUIREMENT (docs/PROTOCOL.md §8):\n"
            "Before concluding your session, author a comprehensive PR description in "
            "`docs/PR_DESCRIPTION.md`. It must detail:\n"
            "1. Summary of what was implemented\n"
            "2. Key deliverables, packages, and interfaces added or updated\n"
            "3. Issues, obstacles, and edge cases encountered and how they were resolved\n"
            "4. Testing and verification performed, confirming `make check` passes locally\n"
            "5. Complete Definition of Done checklist"
        )
        continuation_prompt = (
            f"Please resume implementation of prompts/queued/{prompt_path.name} per PROTOCOL.md. "
            "Inspect the current workspace and repository state and continue."
        )

    effective_backend = agent_backend
    if effective_backend is None and ACTIVE_CONFIG is not None:
        cfg_agent = (ACTIVE_CONFIG.agent_backend or ACTIVE_CONFIG.agent_type or "").lower()
        if cfg_agent in {"claude", "claude-code", "claude_code", "codex", "openai-codex"}:
            effective_backend = create_agent_backend(
                ACTIVE_CONFIG,
                model=model,
                effort=effort,
                agent_bin=agy_bin if Path(agy_bin).name in {"claude", "codex"} else None,
                cli_log_path=log_file,
                repo_root=REPO_ROOT,
            )

    if effective_backend is not None and isinstance(
        effective_backend, (ClaudeCodeAgent, CodexAgent)
    ):
        if resume and log_file.exists():
            with log_file.open("a", encoding="utf-8") as lf:
                suffix = "Review Feedback" if custom_prompt else "Continued"
                lf.write(f"\n=== Resumed Session ({suffix}): {title} ===\n")
        else:
            with log_file.open("w", encoding="utf-8") as lf:
                lf.write(f"=== {title}: {prompt_path.name} ===\n")
        return _execute_backend_prompt_session(
            backend=effective_backend,
            prompt_path=prompt_path,
            log_file=log_file,
            title=title,
            kickoff_prompt=kickoff_prompt,
            continuation_prompt=continuation_prompt,
            resume=resume,
            custom_prompt=custom_prompt,
        )

    # Session launch using --sandbox (never --dangerously-skip-permissions)
    cmd = [
        agy_bin,
        "--sandbox",
    ]
    if resume:
        cmd.append("-c")
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    cmd.extend(["-p", kickoff_prompt])

    action_label = (
        "Resuming (review feedback)" if custom_prompt else ("Resuming" if resume else "Starting")
    )
    logger.info("%s sandboxed session for prompt %s (%s)...", action_label, prompt_path.name, title)
    logger.info("Logging session transcript to: %s", log_file)

    if resume and log_file.exists():
        with log_file.open("a", encoding="utf-8") as lf:
            suffix = "Review Feedback" if custom_prompt else "Continued"
            lf.write(f"\n=== Resumed Session ({suffix}): {title} ===\n")
    else:
        with log_file.open("w", encoding="utf-8") as lf:
            lf.write(f"=== {title}: {prompt_path.name} ===\n")

    is_first_turn = not resume
    retry_count = 0
    max_retries = 5
    turn_count = 1
    max_turns = 10
    cumulative_active_seconds = extract_previous_cumulative_time(log_file) if resume else 0.0

    while True:
        if not is_first_turn:
            # Continue the existing conversation session
            cmd = [
                agy_bin,
                "--sandbox",
                "-c",
            ]
            if model:
                cmd.extend(["--model", model])
            if effort:
                cmd.extend(["--effort", effort])
            cmd.extend(["-p", continuation_prompt])

        cli_log_offset = CLI_LOG_FILE.stat().st_size if CLI_LOG_FILE.exists() else 0
        turn_start_time = time.time()
        returncode, full_output = run_session_process(
            cmd,
            log_file,
            prompt_path.name,
            prompt_num,
            active_time_offset=cumulative_active_seconds,
        )
        turn_duration = time.time() - turn_start_time
        cumulative_active_seconds += turn_duration
        is_first_turn = False
        active_conv_id = get_latest_conversation_id(since_timestamp=turn_start_time)

        # 1. Check if a tool was auto-denied or soft-denied
        soft_denied_tool = check_recent_soft_denial_in_cli_log(since_offset=cli_log_offset)
        transcript_denial = None
        has_denial = (
            "jetski: no output produced" in full_output
            or "auto-denied" in full_output
            or "permission check failed" in full_output
            or "user denied permission" in full_output
            or bool(soft_denied_tool)
        )
        if not has_denial:
            transcript_denial = check_recent_transcript_denial(
                since_timestamp=turn_start_time,
                conv_id=active_conv_id,
            )
            if transcript_denial and (transcript_denial[0] or transcript_denial[1]):
                has_denial = True

        if has_denial:
            logger.warning("Tool permission was denied or soft-denied by the sandbox policy.")
            if transcript_denial and (transcript_denial[0] or transcript_denial[1]):
                perm_info = transcript_denial
            else:
                perm_info = extract_denied_permission(
                    full_output,
                    perm_hint=soft_denied_tool,
                    conv_id=active_conv_id,
                    since_timestamp=turn_start_time,
                )
            action, detail = prompt_user_permission_action(perm_info)
            if action == "allow":
                logger.info("Retrying session with updated whitelist rule (%s)...", detail)
                if custom_prompt:
                    continuation_prompt = (
                        f"Tool permission for '{detail}' has been granted. "
                        f"Please proceed with addressing the reviewer feedback for "
                        f"{prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        f"Tool permission for '{detail}' has been granted. "
                        "Please proceed with implementation per PROTOCOL.md."
                    )
                retry_count = 0
                continue
            elif action == "redirect":
                logger.info("Retrying session with user redirect directive...")
                target_desc = perm_info[1] or perm_info[0]
                notice_header = (
                    f"Security Notice: The attempted action ({perm_info[0]}: {target_desc}) "
                    "was denied by user.\n"
                    f"User Redirect Directive:\n{detail}\n\n"
                )
                if custom_prompt:
                    continuation_prompt = (
                        f"{notice_header}Please adjust your approach accordingly and continue "
                        f"addressing the reviewer feedback for {prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        f"{notice_header}Please adjust your implementation approach accordingly "
                        f"and continue implementing prompts/queued/{prompt_path.name} "
                        "per PROTOCOL.md."
                    )
                retry_count = 0
                continue
            else:
                logger.error("Execution aborted by user after permission denial.")
                return False

        # 2. Check if a transient API, HTTP, or network error occurred
        is_transient, reason = is_transient_session_error(full_output, returncode)
        if returncode != 0 and is_transient:
            is_network = any(
                term in reason.lower()
                for term in (
                    "network",
                    "socket",
                    "address",
                    "tcp",
                    "dns",
                    "connection",
                    "pipe",
                    "host",
                    "unassigned",
                    "unreachable",
                )
            )
            # If network connection was lost or offline, wait indefinitely until restored
            if is_network or not check_network_connectivity():
                logger.warning(
                    "⚠️  Network disconnect in session for %s: %s (exit code %d).",
                    prompt_path.name,
                    reason,
                    returncode,
                )
                wait_for_network_connectivity()
                logger.info("Resuming session via 'agy -c' now that network is online...")
                if custom_prompt:
                    continuation_prompt = (
                        f"A temporary network disconnection ({reason}) occurred while offline. "
                        "Network connectivity is now restored. "
                        f"Please continue addressing the reviewer feedback for "
                        f"{prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        f"A temporary network disconnection ({reason}) occurred while offline. "
                        "Network connectivity is now restored. "
                        f"Please inspect repository state and continue implementing "
                        f"prompts/queued/{prompt_path.name} per PROTOCOL.md."
                    )
                continue

            if retry_count < max_retries:
                retry_count += 1
                backoff_seconds = min(10 * (2 ** (retry_count - 1)), 120)
                logger.warning(
                    "⚠️  Transient error in session for %s: %s (exit code %d). "
                    "Auto-resuming session via 'agy -c' in %ds (retry %d/%d)...",
                    prompt_path.name,
                    reason,
                    returncode,
                    backoff_seconds,
                    retry_count,
                    max_retries,
                )
                time.sleep(backoff_seconds)
                if custom_prompt:
                    continuation_prompt = (
                        f"A transient error ({reason}) interrupted the previous turn. "
                        f"Please continue addressing the reviewer feedback for "
                        f"{prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        f"A transient error ({reason}) interrupted the previous turn. "
                        f"Please review the repository state and continue implementing "
                        f"prompts/queued/{prompt_path.name} per PROTOCOL.md."
                    )
                continue
            else:
                logger.error(
                    "Session for %s exceeded maximum transient error retries (%d). Halting.",
                    prompt_path.name,
                    max_retries,
                )
                return False

        if returncode != 0:
            if returncode == 3 and retry_count < 2:
                retry_count += 1
                backoff_seconds = 5 * retry_count
                logger.warning(
                    "⚠️  Session exited with runtime error code 3. "
                    "Auto-resuming session via 'agy -c' in %ds (retry %d/2)...",
                    backoff_seconds,
                    retry_count,
                )
                time.sleep(backoff_seconds)
                if custom_prompt:
                    continuation_prompt = (
                        "A runtime error interrupted the previous turn. "
                        f"Please continue addressing the reviewer feedback for "
                        f"{prompt_path.name} per PROTOCOL.md."
                    )
                else:
                    continuation_prompt = (
                        "A runtime error interrupted the previous turn. "
                        f"Please review repository state and continue implementing "
                        f"prompts/queued/{prompt_path.name} per PROTOCOL.md."
                    )
                continue
            logger.error("Session exited with failure code %d. See %s.", returncode, log_file)
            return False

        # Check whether prompt has completed the implementation phase or is under verification
        impl_path = IMPLEMENTED_DIR / prompt_path.name
        if impl_path.exists():
            learnings_dir = (
                LEARNINGS_DIR
                if LEARNINGS_DIR != _DEFAULT_LEARNINGS_DIR
                else REPO_ROOT / "docs" / "learnings"
            )
            learnings_file = learnings_dir / f"{prompt_path.stem}-learnings.md"
            if REQUIRE_LEARNINGS and not learnings_file.exists() and turn_count < max_turns:
                turn_count += 1
                logger.warning(
                    "Prompt %s was moved to implemented/, but %s is missing. "
                    "Auto-continuing session (turn %d/%d)...",
                    prompt_path.name,
                    learnings_file.name,
                    turn_count,
                    max_turns,
                )
                continuation_prompt = (
                    f"The prompt file was moved to prompts/implemented/{prompt_path.name}, but "
                    f"docs/learnings/{learnings_file.name} is missing. "
                    "Per PROTOCOL.md §5, author this learnings file and docs/PR_DESCRIPTION.md, "
                    f"and ensure `{CHECK_CMD}` passes."
                )
                continue

            # Verify Definition of Done before declaring completion
            dod_ok, dod_error = check_definition_of_done(prompt_path)
            if dod_ok:
                logger.info(
                    "Session process completed and Definition of Done verified for prompt %s.",
                    prompt_path.name,
                )
                return True

            # Definition of Done check failed (e.g. make check timed out or failed)
            if turn_count < max_turns:
                turn_count += 1
                first_error_line = dod_error.strip().split("\n")[0]
                logger.warning(
                    "Definition of Done verification failed for prompt %s (%s). "
                    "Feeding failure back to session (turn %d/%d)...",
                    prompt_path.name,
                    first_error_line,
                    turn_count,
                    max_turns,
                )
                continuation_prompt = (
                    f"Definition of Done verification failed for "
                    f"prompts/implemented/{prompt_path.name}.\n\n"
                    f"{dod_error}\n\n"
                    f"Please diagnose and resolve this issue. If '{CHECK_CMD}' timed out, look for "
                    "infinite loops, hanging network requests, test deadlocks, or long-running "
                    f"commands. Ensure all tests and checks complete promptly and `{CHECK_CMD}` "
                    "passes locally."
                )
                continue
            else:
                logger.error(
                    "Definition of Done verification failed for %s and turn budget (%d turns) "
                    "exhausted: %s",
                    prompt_path.name,
                    max_turns,
                    dod_error.strip().split("\n")[0],
                )
                return False

        # If prompt is still in queued/, the agent's turn ended before finishing the implementation!
        if turn_count < max_turns:
            turn_count += 1
            logger.info(
                "Prompt %s not yet moved to implemented/ (work in progress). "
                "Auto-continuing session (turn %d/%d)...",
                prompt_path.name,
                turn_count,
                max_turns,
            )
            continuation_prompt = (
                f"Please continue implementing prompts/queued/{prompt_path.name} per PROTOCOL.md. "
                "The implementation is still in progress. Follow all steps until the Definition "
                "of Done is fully satisfied "
                f"(move prompt to prompts/implemented/{prompt_path.name}, "
                f"author docs/learnings/{prompt_path.stem}-learnings.md, "
                f"author docs/PR_DESCRIPTION.md, and verify that `{CHECK_CMD}` passes locally)."
            )
            continue
        else:
            logger.error(
                "Session for %s reached maximum turn budget (%d turns) without completion. "
                "Halting daemon for manual inspection.",
                prompt_path.name,
                max_turns,
            )
            return False


def check_definition_of_done(
    prompt_path: Path,
    config: RepoConfig | None = None,
) -> tuple[bool, str]:
    """Verify local Definition of Done checks (check_cmd, prompt moved, learnings, PR desc).

    Returns:
        tuple[bool, str]: (is_successful, failure_details)
    """
    if config is not None:
        impl_dir = config.implemented_dir
        learnings_dir = config.learnings_dir
        pr_desc_path = config.pr_description_path
        check_cmd = config.check_cmd
        req_learnings = config.require_learnings
        req_pr_desc = config.require_pr_description
    elif ACTIVE_CONFIG is not None and ACTIVE_CONFIG.repo_root == REPO_ROOT:
        impl_dir = IMPLEMENTED_DIR
        learnings_dir = LEARNINGS_DIR
        pr_desc_path = PR_DESC_PATH
        check_cmd = CHECK_CMD
        req_learnings = REQUIRE_LEARNINGS
        req_pr_desc = REQUIRE_PR_DESCRIPTION
    else:
        impl_dir = IMPLEMENTED_DIR
        learnings_dir = (
            LEARNINGS_DIR
            if LEARNINGS_DIR != _DEFAULT_LEARNINGS_DIR
            else REPO_ROOT / "docs" / "learnings"
        )
        pr_desc_path = (
            PR_DESC_PATH
            if PR_DESC_PATH != _DEFAULT_PR_DESC_PATH
            else REPO_ROOT / "docs" / "PR_DESCRIPTION.md"
        )
        check_cmd = CHECK_CMD
        req_learnings = REQUIRE_LEARNINGS
        req_pr_desc = REQUIRE_PR_DESCRIPTION

    # 1. Check prompt moved to implemented
    impl_path = impl_dir / prompt_path.name
    if not impl_path.exists():
        msg = f"Prompt file {prompt_path.name} was not moved to prompts/implemented/."
        logger.error(msg)
        return False, msg

    # 2. Check learnings file (when required)
    if req_learnings:
        learnings_slug = f"{prompt_path.stem}-learnings.md"
        learnings_file = learnings_dir / learnings_slug
        if not learnings_file.exists():
            msg = f"Learnings file docs/learnings/{learnings_slug} was not found."
            logger.error("Learnings file %s was not found in docs/learnings/.", learnings_slug)
            return False, msg

    # 3. Check PR description file (when required)
    if req_pr_desc and (
        not pr_desc_path.is_file()
        or not pr_desc_path.read_text(encoding="utf-8", errors="replace").strip()
    ):
        msg = "PR description file docs/PR_DESCRIPTION.md was not found or is empty."
        logger.error(msg)
        return False, msg

    # 4. Run check_cmd (10m safety timeout with generous margin for loaded boxes)
    cmd_str = check_cmd.strip()
    if not cmd_str or cmd_str == "true":
        logger.info("Skipping verification command (check_cmd=%r).", check_cmd)
        return True, ""

    cmd_parts = shlex.split(cmd_str)
    logger.info("Running Definition of Done checks: '%s' (safety timeout: 10m)...", cmd_str)
    if config is not None and config.repo_root != REPO_ROOT:
        res = run_cmd(cmd_parts, cwd=config.repo_root, check=False, timeout=600.0)
    else:
        res = run_cmd(cmd_parts, check=False, timeout=600.0)
    if res.returncode != 0:
        if res.returncode == 124:
            out_snippet = res.stdout.strip() if res.stdout else ""
            if len(out_snippet) > 2000:
                out_snippet = out_snippet[-2000:]
            msg = (
                f"Definition of Done check '{cmd_str}' timed out after 10 minutes (600s).\n"
                "Possible hang, infinite loop, or test deadlock.\n"
            )
            if out_snippet:
                msg += f"\nPartial output before timeout:\n```\n{out_snippet}\n```\n"
            msg += (
                "\nPlease inspect your tests and implementation to identify what is hanging or "
                "taking excessive time. Ensure all tests and checks complete promptly and "
                f"`{cmd_str}` passes locally."
            )
            logger.error("'%s' timed out after 10m (possible hang or test deadlock).", cmd_str)
        else:
            output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
            if len(output) > 4000:
                output = output[-4000:]
            msg = (
                f"'{cmd_str}' failed with exit status {res.returncode}:\n\n"
                f"```\n{output.strip()}\n```\n"
            )
            logger.error("'%s' failed:\n%s\n%s", cmd_str, res.stdout, res.stderr)
        return False, msg

    logger.info("'%s' passed successfully.", cmd_str)
    return True, ""


def verify_definition_of_done(prompt_path: Path, config: RepoConfig | None = None) -> bool:
    """Verify local Definition of Done checks (check_cmd, prompt moved, learnings, PR desc)."""
    ok, _ = check_definition_of_done(prompt_path, config=config)
    return ok


def _format_pr_from_learnings(text: str, prompt_name: str, slug: str) -> str:
    """Extract structured sections from a learnings file to generate a rich PR description."""
    what_shipped = ""
    key_packages = ""
    key_interfaces = ""
    gotchas = ""
    next_session = ""

    m_shipped = re.search(r"- \*\*What shipped\*\*:\s*(.*?)(?=\n- \*\*|\n## |\Z)", text, re.DOTALL)
    if m_shipped:
        what_shipped = m_shipped.group(1).strip()

    m_pkg = re.search(
        r"- \*\*Key packages/files\*\*:\s*(.*?)(?=\n- \*\*|\n## |\Z)", text, re.DOTALL
    )
    if m_pkg:
        key_packages = m_pkg.group(1).strip()

    m_types = re.search(
        r"- \*\*Key interfaces/types added or changed\*\*:\s*(.*?)(?=\n- \*\*|\n## |\Z)",
        text,
        re.DOTALL,
    )
    if m_types:
        key_interfaces = m_types.group(1).strip()

    m_gotchas = re.search(
        r"- \*\*Gotchas / non-obvious constraints\*\*:\s*(.*?)(?=\n- \*\*|\n## |\Z)",
        text,
        re.DOTALL,
    )
    if m_gotchas:
        gotchas = m_gotchas.group(1).strip()

    m_next = re.search(
        r"- \*\*What the NEXT session must know\*\*:\s*(.*?)(?=\n- \*\*|\n## |\Z)",
        text,
        re.DOTALL,
    )
    if m_next:
        next_session = m_next.group(1).strip()

    details_part = ""
    m_details = re.search(r"## Details\s*(.*)", text, re.DOTALL)
    if m_details:
        details_text = m_details.group(1).strip()
        details_part = details_text[:2500].strip()
        if len(details_text) > 2500:
            details_part += f"\n\n*(Full details in `docs/learnings/{slug}-learnings.md`)*"

    sections = [
        f"Implements prompt `{prompt_name}` per `docs/PROTOCOL.md` and `docs/PLAN.md`.\n",
        "### Summary of Implementation",
        what_shipped or "Implemented all acceptance criteria and deliverables.",
    ]

    if key_packages:
        sections.extend(["", "### Key Packages & Deliverables", key_packages])

    if key_interfaces:
        sections.extend(["", "### Key Interfaces & Types", key_interfaces])

    if gotchas:
        sections.extend(["", "### Issues, Gotchas & Constraints Identified", gotchas])

    if next_session:
        sections.extend(["", "### Notes for Subsequent Sessions", next_session])

    if details_part:
        sections.extend(["", "### Implementation Details", details_part])

    sections.extend(
        [
            "",
            "---",
            "",
            "### Definition of Done",
            "- [x] Prompt moved to `prompts/implemented/`",
            f"- [x] Learnings file documented at `docs/learnings/{slug}-learnings.md`",
            "- [x] `make check` passes locally (format, lint, types, unit tests)",
            "- [ ] CI is green on the PR (checked automatically by daemon runner)",
        ]
    )

    return "\n".join(sections)


def build_pr_description(
    prompt_path: Path,
    branch_name: str,
    base_branch: str | None = None,
) -> str:
    """Build an informative PR description from docs/PR_DESCRIPTION.md or learnings file."""
    base = base_branch or BASE_BRANCH
    slug = prompt_path.stem

    # 1. Check for docs/PR_DESCRIPTION.md (written in workspace)
    pr_desc_file = (
        PR_DESC_PATH
        if PR_DESC_PATH != _DEFAULT_PR_DESC_PATH
        else REPO_ROOT / "docs" / "PR_DESCRIPTION.md"
    )
    if pr_desc_file.is_file():
        try:
            content = pr_desc_file.read_text(encoding="utf-8").strip()
            if content and len(content) > 80:
                logger.info("Using agent-authored docs/PR_DESCRIPTION.md for PR description.")
                if "### Definition of Done" not in content:
                    content += (
                        "\n\n---\n\n### Definition of Done\n"
                        "- [x] Prompt moved to `prompts/implemented/`\n"
                        f"- [x] Learnings file documented at `docs/learnings/{slug}-learnings.md`\n"
                        f"- [x] `{CHECK_CMD}` passes locally (format, lint, types, unit tests)\n"
                        "- [ ] CI is green on the PR (checked automatically by daemon runner)\n"
                    )
                return content
        except Exception as e:
            logger.debug("Failed reading docs/PR_DESCRIPTION.md: %s", e)

    # 2. Check for learnings file on disk or branch
    learnings_dir = (
        LEARNINGS_DIR
        if LEARNINGS_DIR != _DEFAULT_LEARNINGS_DIR
        else REPO_ROOT / "docs" / "learnings"
    )
    learnings_file = learnings_dir / f"{slug}-learnings.md"
    learnings_text = ""
    if learnings_file.is_file():
        with contextlib.suppress(Exception):
            learnings_text = learnings_file.read_text(encoding="utf-8")
    if not learnings_text:
        res = run_cmd(
            ["git", "show", f"{branch_name}:docs/learnings/{slug}-learnings.md"],
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            learnings_text = res.stdout

    if learnings_text:
        logger.info("Generating comprehensive PR description from %s-learnings.md.", slug)
        return _format_pr_from_learnings(learnings_text, prompt_path.name, slug)

    # 3. Fallback to git commit messages
    commits_res = run_cmd(
        ["git", "log", f"origin/{base}..{branch_name}", "--pretty=format:- %s (%h)"],
        check=False,
    )
    commit_list = (commits_res.stdout or "").strip()
    return (
        f"Implements prompt `{prompt_path.name}` per `docs/PROTOCOL.md`.\n\n"
        f"### Commits\n{commit_list or '- Implementation commit'}\n\n"
        "### Definition of Done\n"
        "- [x] Prompt moved to `prompts/implemented/`\n"
        "- [x] Learnings file added\n"
        f"- [x] `{CHECK_CMD}` passes locally\n"
        "- [ ] CI is green on the PR (checked automatically by daemon runner)\n"
    )


def ensure_pr_opened(
    gh_bin: str,
    branch_name: str,
    prompt_path: Path,
    base_branch: str | None = None,
) -> bool:
    """Ensure branch is pushed and PR is open with an informative description."""
    base = base_branch or BASE_BRANCH
    existing_pr = get_branch_pr_status(gh_bin, branch_name)
    if existing_pr:
        state = str(existing_pr.get("state", "")).upper()
        if state == "MERGED":
            slug = prompt_path.stem
            prompt_merged = is_prompt_merged_into_origin_main(slug, base_branch=base)
            ahead_res = run_cmd(
                ["git", "rev-list", "--count", f"origin/{base}..{branch_name}"],
                check=False,
            )
            ahead_count = (
                int(ahead_res.stdout.strip())
                if ahead_res.returncode == 0 and ahead_res.stdout.strip().isdigit()
                else 0
            )
            if ahead_count == 0 and prompt_merged:
                logger.info(
                    "PR #%s for branch '%s' is already merged into %s: %s",
                    existing_pr.get("number"),
                    branch_name,
                    base,
                    existing_pr.get("url"),
                )
                return True
            else:
                logger.warning(
                    "PR #%s for branch '%s' is marked MERGED on GitHub, but branch has "
                    "%d unmerged commit(s) ahead of origin/%s (prompt in origin/%s: %s). "
                    "Renaming branch to '%s-impl' to allow opening a new PR...",
                    existing_pr.get("number"),
                    branch_name,
                    ahead_count,
                    base,
                    base,
                    prompt_merged,
                    branch_name,
                )
                new_branch_name = f"{branch_name}-impl"
                run_cmd(["git", "branch", "-m", branch_name, new_branch_name], check=True)
                branch_name = new_branch_name
        elif state == "OPEN":
            logger.info(
                "PR #%s is already open for branch '%s': %s",
                existing_pr.get("number"),
                branch_name,
                existing_pr.get("url"),
            )
            return True

    logger.info("Pushing branch '%s' to origin...", branch_name)
    push_res = run_cmd(["git", "push", "-u", "origin", branch_name], check=False)
    if push_res.returncode != 0:
        logger.error("Failed to push branch '%s': %s", branch_name, push_res.stderr)
        if "without `workflow` scope" in (push_res.stderr or ""):
            logger.error(
                "GitHub rejected the push because the branch modifies .github/workflows/ "
                "and your GitHub CLI / OAuth token lacks the 'workflow' scope.\n"
                "To resolve, run:\n"
                "    gh auth refresh -s workflow\n"
                "Or revert any changes to .github/workflows/ if unintended."
            )
        return False

    logger.info("Building pull request description...")
    pr_title = f"feat({prompt_path.stem}): implement {prompt_path.stem}"
    pr_body = build_pr_description(prompt_path, branch_name, base_branch=base)

    logger.info("Opening pull request via gh pr create...")
    create_res = _make_github_forge(gh_bin).create_pr_with_body(
        branch=branch_name,
        base_branch=base,
        title=pr_title,
        body=pr_body,
    )
    if create_res.returncode == 0:
        logger.info("Pull request created successfully: %s", create_res.stdout.strip())
        send_notification(
            "PR Opened",
            f"Pull request created for {prompt_path.name}",
            sound="Glass",
            subtitle=branch_name,
        )
        return True
    err_out = (create_res.stderr or "") + ("\n" + create_res.stdout if create_res.stdout else "")
    if "No commits between" in err_out:
        logger.info(
            "Branch '%s' has no commits ahead of main or is already merged into main.",
            branch_name,
        )
        return True
    logger.error("Failed to open PR: %s", create_res.stderr)
    return False


def load_pr_tracking_data(pr_number: int | str) -> dict[str, Any]:
    """Load tracking dictionary for a PR from logs/pr_reviews.json."""
    if not PR_REVIEWS_FILE.exists():
        return {}
    try:
        data = json.loads(PR_REVIEWS_FILE.read_text(encoding="utf-8"))
        pr_entry = data.get(str(pr_number), {})
        if isinstance(pr_entry, dict):
            return pr_entry
    except Exception as e:
        logger.warning("Could not read %s: %s", PR_REVIEWS_FILE, e)
    return {}


def save_pr_tracking_data(pr_number: int | str, tracking_data: dict[str, Any]) -> None:
    """Save tracking dictionary for a PR to logs/pr_reviews.json."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if PR_REVIEWS_FILE.exists():
        try:
            data = json.loads(PR_REVIEWS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pr_key = str(pr_number)
    existing = data.get(pr_key, {})
    if not isinstance(existing, dict):
        existing = {}
    existing.update(tracking_data)
    existing["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data[pr_key] = existing
    try:
        PR_REVIEWS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save %s: %s", PR_REVIEWS_FILE, e)


def get_ci_heal_attempts(pr_number: int | str) -> int:
    """Get the number of CI healing attempts made for a PR."""
    tracking = load_pr_tracking_data(pr_number)
    return int(tracking.get("ci_heal_attempts", 0))


def increment_ci_heal_attempts(pr_number: int | str) -> int:
    """Increment and return the CI healing attempts counter for a PR."""
    current = get_ci_heal_attempts(pr_number) + 1
    save_pr_tracking_data(pr_number, {"ci_heal_attempts": current})
    return current


def get_review_feedback_attempts(pr_number: int | str) -> int:
    """Get the number of review feedback fix attempts made for a PR."""
    tracking = load_pr_tracking_data(pr_number)
    return int(tracking.get("review_feedback_attempts", 0))


def increment_review_feedback_attempts(pr_number: int | str) -> int:
    """Increment and return the review feedback fix attempts counter for a PR."""
    current = get_review_feedback_attempts(pr_number) + 1
    save_pr_tracking_data(pr_number, {"review_feedback_attempts": current})
    return current


def load_processed_comment_ids(pr_number: int | str) -> set[str]:
    """Load previously processed comment/review IDs for a PR from disk."""
    tracking = load_pr_tracking_data(pr_number)
    return set(str(cid) for cid in tracking.get("processed_ids", []))


def save_processed_comment_ids(pr_number: int | str, comment_ids: Iterable[str | int]) -> None:
    """Save processed comment/review IDs for a PR to disk."""
    existing = load_processed_comment_ids(pr_number)
    existing.update(str(cid) for cid in comment_ids)
    save_pr_tracking_data(pr_number, {"processed_ids": sorted(existing)})


def fetch_unhandled_pr_feedback(gh_bin: str, pr_number: int | str) -> list[dict[str, Any]]:
    """Fetch unhandled review comments, review summaries, and issue comments on a PR."""
    processed_ids = load_processed_comment_ids(pr_number)
    unhandled, approved_ids = _make_github_forge(gh_bin).fetch_unprocessed_reviews(
        int(pr_number), list(processed_ids)
    )
    for rid in approved_ids:
        save_processed_comment_ids(pr_number, [rid])
    return unhandled


def build_review_feedback_prompt(
    pr_number: int | str,
    pr_title: str,
    comments: list[dict[str, Any]],
) -> str:
    """Format review comments and feedback into a clear, actionable session prompt."""
    sections: list[str] = [
        f"Reviewer feedback has been submitted on Pull Request #{pr_number} ('{pr_title}').",
        "\n### Review Comments to Address:\n",
    ]

    for idx, c in enumerate(comments, 1):
        author = c.get("author") or "Reviewer"
        body = c.get("body", "")
        path = c.get("path")
        line = c.get("line")
        diff_hunk = c.get("diff_hunk")

        if path:
            loc = f"`{path}:{line}`" if line else f"`{path}`"
            sections.append(f"#### {idx}. Review comment from @{author} on {loc}:")
            if diff_hunk:
                diff_clean = diff_hunk.strip()
                sections.append(f"```diff\n{diff_clean}\n```")
            sections.append(f"> {body}\n")
        elif c.get("type") == "review_summary":
            sections.append(f"#### {idx}. Review summary from @{author}:")
            sections.append(f"> {body}\n")
        else:
            sections.append(f"#### {idx}. General comment from @{author}:")
            sections.append(f"> {body}\n")

    sections.append(
        "---\n\n"
        "### Instructions:\n"
        "1. Address all feedback requested above thoroughly and carefully.\n"
        "2. Ensure all tests and `make check` pass locally.\n"
        "3. Update `docs/PR_DESCRIPTION.md` to note the fixes or investigations made.\n"
        "4. If answering questions or inquiries that do not require code changes, "
        "provide a thorough, factual response explaining your findings.\n"
        "5. Conclude your turn with a clear summary explaining how the feedback was addressed "
        "(this response will be automatically posted back to the GitHub PR comment thread)."
    )
    return "\n".join(sections)


def address_pr_feedback(
    gh_bin: str,
    agy_bin: str,
    pr_number: int | str,
    pr_title: str,
    branch_name: str,
    prompt_path: Path,
    new_comments: list[dict[str, Any]],
    model: str | None = None,
    effort: str | None = None,
    agent_backend: AgentBackend | None = None,
) -> bool:
    """Resume session to address reviewer feedback, commit fixes, and push to origin."""
    logger.info(
        "Addressing %d review comment(s) on PR #%s (branch: %s)...",
        len(new_comments),
        pr_number,
        branch_name,
    )

    # 1. Checkout the PR branch
    current_branch = get_current_branch()
    if current_branch != branch_name:
        logger.info("Switching to branch '%s' to address review feedback...", branch_name)
        checkout_res = run_cmd(["git", "checkout", branch_name], check=False)
        if checkout_res.returncode != 0:
            logger.error("Failed to checkout branch '%s': %s", branch_name, checkout_res.stderr)
            return False

    initial_sha_res = run_cmd(["git", "rev-parse", "--short", "HEAD"], check=False)
    initial_sha = initial_sha_res.stdout.strip() if initial_sha_res.returncode == 0 else ""

    # 2. Build the feedback prompt
    feedback_prompt = build_review_feedback_prompt(pr_number, pr_title, new_comments)

    # 3. Resume the session with agy -c
    logger.info("Resuming session for %s with reviewer feedback...", prompt_path.name)
    success = execute_prompt_session(
        agy_bin=agy_bin,
        prompt_path=prompt_path,
        model=model,
        effort=effort,
        resume=True,
        custom_prompt=feedback_prompt,
        agent_backend=agent_backend,
    )
    if not success:
        logger.error("Session failed while addressing feedback for PR #%s.", pr_number)
        return False

    # 4. Commit any changes if not already committed by the session
    status_res = run_cmd(["git", "status", "--porcelain"], check=False)
    if status_res.stdout.strip():
        logger.info("Committing changes made in response to PR review...")
        run_cmd(["git", "add", "."])
        safe, issues = verify_git_staging_safety()
        if not safe:
            logger.error(
                "Git staging safety check failed after review fix: %s. Aborting push.", issues
            )
            send_notification(
                "Git Safety Check Failed",
                "Ignored files in protected directories",
                sound="Basso",
            )
            return False
        commit_msg = f"fix: address review feedback on PR #{pr_number}"
        run_cmd(["git", "commit", "-m", commit_msg])

    # 6. Push updated branch to origin if new commit was made
    current_sha_res = run_cmd(["git", "rev-parse", "--short", "HEAD"], check=False)
    current_sha = current_sha_res.stdout.strip() if current_sha_res.returncode == 0 else ""
    has_new_commit = bool(current_sha and current_sha != initial_sha)

    if has_new_commit:
        logger.info("Pushing updated branch '%s' to origin...", branch_name)
        push_res = run_cmd(["git", "push", "origin", branch_name], check=False)
        if push_res.returncode != 0:
            logger.error("Failed to push updated branch '%s': %s", branch_name, push_res.stderr)
            return False
    else:
        logger.info("No code changes were made for review feedback (inquiry response).")

    # 7. Post a detailed reply comment on GitHub PR
    agent_summary = extract_latest_agent_summary()
    commit_note = (
        f"in commit `{current_sha}`"
        if has_new_commit
        else "(findings verified without code changes)"
    )
    comment_parts = [
        f"{DAEMON_TAG} Addressed {len(new_comments)} review comment(s) {commit_note}.",
    ]
    if agent_summary:
        agent_summary = truncate_pr_comment(agent_summary)
        comment_parts.append(f"### Findings & Response:\n{agent_summary}")

    comment_parts.append(
        "Definition of Done checks (`make check`) passed locally. Ready for re-review."
    )
    comment_body = "\n\n".join(comment_parts)
    _make_github_forge(gh_bin).post_pr_comment(int(pr_number), comment_body)

    # 8. Mark comment IDs as processed and reset attempt counter
    comment_ids = [c["id"] for c in new_comments if c.get("id")]
    save_processed_comment_ids(pr_number, comment_ids)
    save_pr_tracking_data(pr_number, {"review_feedback_attempts": 0})
    logger.info(
        "✅ Successfully addressed feedback for PR #%s (commit: %s, new_commit: %s).",
        pr_number,
        current_sha,
        has_new_commit,
    )
    return True


CI_BILLING_OR_QUOTA_PATTERNS: tuple[str, ...] = _FORGE_CI_BILLING_OR_QUOTA_PATTERNS


def is_ci_billing_or_quota_failure(run_info_or_logs: dict[str, Any] | str | None) -> bool:
    """Return True if a CI run failed due to GitHub Actions billing, spending limit, or quota."""
    return _forge_is_ci_billing_or_quota_failure(run_info_or_logs)


def verify_local_tests_for_pr(
    branch_name: str,
    candidate: Path | None = None,
) -> tuple[bool, str]:
    """Verify local tests on branch_name when GitHub Actions is unavailable due to billing/quota."""
    current_branch = get_current_branch()
    if branch_name and current_branch and current_branch != branch_name:
        logger.info("Switching to branch '%s' to verify local tests...", branch_name)
        checkout_res = run_cmd(["git", "checkout", branch_name], check=False)
        if checkout_res.returncode != 0:
            msg = f"Failed to checkout branch '{branch_name}': {checkout_res.stderr}"
            logger.error(msg)
            return False, msg

    if candidate is None and branch_name:
        candidate = find_prompt_path_for_branch(branch_name)

    if candidate is not None:
        return check_definition_of_done(candidate)

    logger.info("Running local checks: 'make check' (safety timeout: 10m)...")
    res = run_cmd(["make", "check"], check=False, timeout=600.0)
    if res.returncode != 0:
        output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
        if len(output) > 4000:
            output = output[-4000:]
        msg = (
            f"'make check' failed with exit status {res.returncode}:\n\n"
            f"```\n{output.strip()}\n```\n"
        )
        logger.error("'make check' failed:\n%s\n%s", res.stdout, res.stderr)
        return False, msg

    logger.info("'make check' passed successfully.")
    return True, ""


def fetch_latest_failed_ci_run(gh_bin: str, branch_name: str) -> dict[str, Any] | None:
    """Check if the latest workflow run for branch_name failed and fetch its logs."""
    run = _make_github_forge(gh_bin).fetch_latest_failed_ci_run(branch_name)
    if run is not None:
        run["billing_or_quota_failure"] = is_ci_billing_or_quota_failure(run.get("failed_logs", ""))
    return run


def build_ci_failure_prompt(
    pr_number: int | str,
    pr_title: str,
    run_info: dict[str, Any],
) -> str:
    """Format CI failure details and logs into an actionable agent session prompt."""
    run_id = run_info.get("databaseId")
    workflow = run_info.get("workflowName", "CI")
    head_sha = run_info.get("headSha", "")
    url = run_info.get("url", "")
    logs = run_info.get("failed_logs", "").strip()

    sections = [
        f"GitHub Actions {workflow} checks failed on Pull Request #{pr_number} ('{pr_title}')!",
        f"Commit: `{head_sha}` | Run ID: {run_id} | URL: {url}\n",
        "### Failed CI Run Logs:",
        "```text",
        logs if logs else "No detailed log output captured. Check run URL above.",
        "```",
        "\n---",
        "### Instructions to Fix:",
        "1. Diagnose the exact failure from the CI logs above.",
        "2. Reproduce and fix the issue in the codebase (check for missing git files, unanchored "
        ".gitignore rules, cross-platform issues, or broken unit tests).",
        "3. Run `make check` locally and ensure ALL format, lint, type, and unit tests pass with 0 "
        "errors.",
        "4. Update `docs/PR_DESCRIPTION.md` if necessary to document the fix.",
        "5. Conclude your turn with a concise explanation of what was broken and how you resolved "
        "it.",
    ]
    return "\n".join(sections)


def address_ci_failure(
    gh_bin: str,
    agy_bin: str,
    pr_number: int | str,
    pr_title: str,
    branch_name: str,
    prompt_path: Path,
    run_info: dict[str, Any],
    model: str | None = None,
    effort: str | None = None,
    agent_backend: AgentBackend | None = None,
) -> bool:
    """Resume agent session with CI failure logs, verify fix, commit, and push."""
    run_id = str(run_info.get("databaseId", ""))
    attempt_num = increment_ci_heal_attempts(pr_number)
    logger.info(
        "🔧 Auto-healing CI failure for PR #%s (Run ID: %s, attempt %d)...",
        pr_number,
        run_id,
        attempt_num,
    )

    # 1. Checkout PR branch
    current_branch = get_current_branch()
    if current_branch != branch_name:
        logger.info("Switching to branch '%s' to address CI failure...", branch_name)
        checkout_res = run_cmd(["git", "checkout", branch_name], check=False)
        if checkout_res.returncode != 0:
            logger.error("Failed to checkout branch '%s': %s", branch_name, checkout_res.stderr)
            return False

    # 2. Build CI failure prompt
    ci_prompt = build_ci_failure_prompt(pr_number, pr_title, run_info)

    # 3. Resume session with agy -c
    logger.info("Resuming session for %s with CI failure logs...", prompt_path.name)
    success = execute_prompt_session(
        agy_bin=agy_bin,
        prompt_path=prompt_path,
        model=model,
        effort=effort,
        resume=True,
        custom_prompt=ci_prompt,
        agent_backend=agent_backend,
    )
    if not success:
        logger.error("Session failed while attempting to resolve CI failure on PR #%s.", pr_number)
        return False

    # 4. Commit any changes made by the session
    status_res = run_cmd(["git", "status", "--porcelain"], check=False)
    if status_res.stdout.strip():
        logger.info("Committing changes made to fix CI failure...")
        run_cmd(["git", "add", "."])
        safe, issues = verify_git_staging_safety()
        if not safe:
            logger.error("Git staging safety check failed after CI fix: %s. Aborting push.", issues)
            send_notification(
                "Git Safety Check Failed",
                "Ignored files in protected directories",
                sound="Basso",
            )
            return False
        commit_msg = f"fix(ci): address CI failure in workflow run {run_id}"
        run_cmd(["git", "commit", "-m", commit_msg])

    # 6. Push updated branch to origin to re-trigger CI
    logger.info("Pushing CI fix on branch '%s' to origin...", branch_name)
    push_res = run_cmd(["git", "push", "origin", branch_name], check=False)
    if push_res.returncode != 0:
        logger.error("Failed to push CI fix on branch '%s': %s", branch_name, push_res.stderr)
        return False

    # 7. Post comment on PR
    agent_summary = extract_latest_agent_summary()
    head_sha_res = run_cmd(["git", "rev-parse", "--short", "HEAD"], check=False)
    head_sha = head_sha_res.stdout.strip() if head_sha_res.returncode == 0 else ""
    comment_parts = [
        f"{DAEMON_TAG} Addressed CI failure (Run #{run_id}) in commit `{head_sha}` "
        f"(auto-heal attempt {attempt_num}).",
    ]
    if agent_summary:
        agent_summary = truncate_pr_comment(agent_summary)
        comment_parts.append(f"### Diagnosis & Fix Summary:\n{agent_summary}")
    comment_parts.append(
        "Local Definition of Done checks (`make check`) passed. Waiting for re-run."
    )
    comment_body = "\n\n".join(comment_parts)
    _make_github_forge(gh_bin).post_pr_comment(int(pr_number), comment_body)

    # 8. Record run_id as processed
    save_processed_comment_ids(pr_number, [f"ci-run-{run_id}"])
    logger.info(
        "✅ Successfully committed and pushed CI fix for PR #%s (commit: %s).",
        pr_number,
        head_sha,
    )
    return True


def _is_stdin_interactive() -> bool:
    """Return True if standard input is attached to an interactive terminal."""
    try:
        return bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
    except Exception:
        return False


def _interruptible_sleep(seconds: float) -> bool:
    """Sleep for up to `seconds`, waking immediately if Enter is pressed on stdin.

    Returns:
        True if the sleep was interrupted by the user pressing Enter,
        False if the full sleep duration elapsed without input.
    """
    if not _is_stdin_interactive():
        for _ in range(max(1, int(seconds * 2))):
            time.sleep(0.5)
        return False

    start_time = time.time()
    while time.time() - start_time < seconds:
        remaining = seconds - (time.time() - start_time)
        step = min(0.5, max(0.0, remaining))
        if step <= 0:
            break
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], step)
            if rlist:
                line = sys.stdin.readline()
                if not line:
                    # Stdin closed / EOF reached
                    time.sleep(remaining)
                    return False
                return True
        except (io.UnsupportedOperation, OSError):
            time.sleep(step)
    return False


def wait_for_pr_merge(
    gh_bin: str,
    poll_interval: int,
    max_interval: int = 1800,
    max_auto_heal_attempts: int = DEFAULT_MAX_AUTO_HEAL_ATTEMPTS,
    agy_bin: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> float:
    """Wait until all blocking PRs are merged or closed.

    Checks for unhandled reviewer feedback on open PRs, automatically resuming
    the session via agy -c to address comments and push fixes.
    Applies circuit breaker retry limits for automated CI healing and review feedback.
    Uses exponential backoff up to max_interval.
    Returns total wait duration in seconds.
    """
    start_wait_time = time.time()
    current_wait = poll_interval
    last_ci_state = None
    green_notified_prs: set[str] = set()

    try:
        while True:
            open_prs = get_open_prs(gh_bin)
            if not open_prs:
                logger.info("No open pull requests pending. Ready for the next prompt.")
                return time.time() - start_wait_time

            pr = open_prs[0]
            pr_number = pr.get("number")
            pr_title = pr.get("title")
            branch_name = pr.get("headRefName")

            # Check for unhandled reviewer feedback/comments
            if agy_bin is not None and pr_number and branch_name:
                unhandled_feedback = fetch_unhandled_pr_feedback(gh_bin, pr_number)
                if unhandled_feedback:
                    current_feedback_attempts = get_review_feedback_attempts(pr_number)
                    if current_feedback_attempts >= max_auto_heal_attempts:
                        alert_msg = (
                            f"Circuit breaker tripped on PR #{pr_number}: "
                            f"Reached maximum review feedback attempts "
                            f"({current_feedback_attempts}/{max_auto_heal_attempts}). "
                            "Halting daemon to prevent runaway token usage."
                        )
                        logger.critical("🛑 [CIRCUIT BREAKER] %s", alert_msg)
                        send_notification(
                            "Circuit Breaker Tripped",
                            (
                                f"PR #{pr_number} reached max review feedback "
                                f"attempts ({max_auto_heal_attempts})."
                            ),
                            sound="Basso",
                            subtitle="Review handling halted",
                        )
                        comment_body = (
                            f"{DAEMON_TAG} 🛑 **Circuit Breaker Tripped**\n\n"
                            f"The automated daemon has reached the maximum of "
                            f"{max_auto_heal_attempts} review feedback fix attempts for "
                            "this pull request.\n\n"
                            "Automated review addressing has been paused for manual inspection."
                        )
                        run_cmd(
                            [gh_bin, "pr", "comment", str(pr_number), "--body", comment_body],
                            check=False,
                        )
                        sys.exit(1)

                    logger.info(
                        "🔔 Found %d new review comment(s) on PR #%s ('%s')!",
                        len(unhandled_feedback),
                        pr_number,
                        pr_title,
                    )
                    candidate = find_prompt_path_for_branch(branch_name)
                    if candidate:
                        feedback_ok = address_pr_feedback(
                            gh_bin=gh_bin,
                            agy_bin=agy_bin,
                            pr_number=pr_number,
                            pr_title=pr_title or "",
                            branch_name=branch_name,
                            prompt_path=candidate,
                            new_comments=unhandled_feedback,
                            model=model,
                            effort=effort,
                        )
                        if feedback_ok:
                            # Reset backoff interval since new commit was pushed
                            current_wait = poll_interval
                            continue
                        else:
                            current_feedback_attempts = increment_review_feedback_attempts(
                                pr_number
                            )
                            if current_feedback_attempts >= max_auto_heal_attempts:
                                alert_msg = (
                                    f"Circuit breaker tripped on PR #{pr_number}: "
                                    f"Reached maximum review feedback attempts "
                                    f"({current_feedback_attempts}/{max_auto_heal_attempts}). "
                                    "Halting daemon to prevent runaway token usage."
                                )
                                logger.critical("🛑 [CIRCUIT BREAKER] %s", alert_msg)
                                send_notification(
                                    "Circuit Breaker Tripped",
                                    (
                                        f"PR #{pr_number} reached max review feedback "
                                        f"attempts ({max_auto_heal_attempts})."
                                    ),
                                    sound="Basso",
                                    subtitle="Review handling halted",
                                )
                                comment_body = (
                                    f"{DAEMON_TAG} 🛑 **Circuit Breaker Tripped**\n\n"
                                    f"The automated daemon has reached the maximum of "
                                    f"{max_auto_heal_attempts} review feedback fix attempts for "
                                    "this pull request.\n\n"
                                    "Automated review addressing has been paused for manual "
                                    "inspection."
                                )
                                run_cmd(
                                    [
                                        gh_bin,
                                        "pr",
                                        "comment",
                                        str(pr_number),
                                        "--body",
                                        comment_body,
                                    ],
                                    check=False,
                                )
                                sys.exit(1)
                            else:
                                wait_str = format_interval(current_wait)
                                logger.warning(
                                    "⚠️  Failed addressing review feedback on PR #%s "
                                    "(attempt %d/%d). Will retry in %s...",
                                    pr_number,
                                    current_feedback_attempts,
                                    max_auto_heal_attempts,
                                    wait_str,
                                )
                                time.sleep(current_wait)
                                current_wait = min(current_wait * 1.5, max_interval)
                                continue
                    else:
                        logger.warning(
                            "Could not locate prompt file for branch '%s'. "
                            "Skipping automatic review handling for these comments.",
                            branch_name,
                        )
                        comment_ids = [c["id"] for c in unhandled_feedback if c.get("id")]
                        if comment_ids:
                            save_processed_comment_ids(pr_number, comment_ids)

            checks = get_pr_checks(gh_bin, pr_number)
            if checks:
                pending = [c for c in checks if c.get("bucket") == "pending"]
                failed = [c for c in checks if c.get("bucket") == "fail"]
                passed = [c for c in checks if c.get("bucket") == "pass"]

                ci_billing_local_green = False
                failed_run: dict[str, Any] | None = None
                if failed and not pending and branch_name:
                    failed_run = fetch_latest_failed_ci_run(gh_bin, branch_name)
                    if failed_run and is_ci_billing_or_quota_failure(failed_run):
                        run_id = failed_run.get("databaseId")
                        run_id_key = f"ci-run-{run_id}"
                        local_green_key = f"ci-billing-local-green-{run_id}"
                        processed_ids = load_processed_comment_ids(pr_number)
                        if local_green_key in processed_ids:
                            ci_billing_local_green = True
                        else:
                            logger.warning(
                                "⚠️ PR #%s CI run %s failed due to GitHub Actions billing/spending "
                                "limit or quota exhaustion. Relying on local test suite "
                                "verification...",
                                pr_number,
                                run_id,
                            )
                            candidate = find_prompt_path_for_branch(branch_name)
                            local_ok, local_error = verify_local_tests_for_pr(
                                branch_name, candidate
                            )
                            if local_ok:
                                save_processed_comment_ids(pr_number, [run_id_key, local_green_key])
                                ci_billing_local_green = True
                            else:
                                logger.error(
                                    "❌ Local test verification failed for PR #%s after CI "
                                    "billing/quota failure:\n%s",
                                    pr_number,
                                    local_error,
                                )
                                failed_run["failed_logs"] = local_error

                current_ci_state = (
                    "passed"
                    if ci_billing_local_green
                    else ("failed" if failed else ("pending" if pending else "passed"))
                )
                # Reset backoff if CI just transitioned state (e.g. pending -> passed)
                if last_ci_state is not None and last_ci_state != current_ci_state:
                    current_wait = poll_interval
                last_ci_state = current_ci_state

                wait_str = format_interval(current_wait)
                enter_hint = ", or press Enter to recheck now" if _is_stdin_interactive() else ""

                if failed and not ci_billing_local_green:
                    failed_names = ", ".join(c.get("name", "unknown") for c in failed)
                    logger.error(
                        "❌ PR #%s CI checks FAILED (%s)! "
                        "Definition of Done requires green CI before merge. (re-checking in %s%s)",
                        pr_number,
                        failed_names,
                        wait_str,
                        enter_hint,
                    )
                    # Automatically auto-heal CI failure if all checks are done and not pending
                    if not pending and agy_bin is not None and branch_name:
                        if failed_run is None:
                            failed_run = fetch_latest_failed_ci_run(gh_bin, branch_name)
                        if failed_run:
                            run_id_key = f"ci-run-{failed_run.get('databaseId')}"
                            processed_ids = load_processed_comment_ids(pr_number)
                            if run_id_key not in processed_ids:
                                current_heal_attempts = get_ci_heal_attempts(pr_number)
                                if current_heal_attempts >= max_auto_heal_attempts:
                                    alert_msg = (
                                        f"Circuit breaker tripped on PR #{pr_number}: "
                                        f"Reached maximum CI auto-healing attempts "
                                        f"({current_heal_attempts}/{max_auto_heal_attempts}). "
                                        "Halting daemon to prevent runaway token usage."
                                    )
                                    logger.critical("🛑 [CIRCUIT BREAKER] %s", alert_msg)
                                    send_notification(
                                        "Circuit Breaker Tripped",
                                        (
                                            f"PR #{pr_number} reached max CI heal "
                                            f"attempts ({max_auto_heal_attempts})."
                                        ),
                                        sound="Basso",
                                        subtitle="Auto-healing halted",
                                    )
                                    comment_body = (
                                        f"{DAEMON_TAG} 🛑 **Circuit Breaker Tripped**\n\n"
                                        f"The automated daemon has reached the maximum of "
                                        f"{max_auto_heal_attempts} CI healing attempts for this "
                                        "pull request without achieving a green build.\n\n"
                                        "Automated self-healing has been paused to conserve model "
                                        "quota and tokens. Please inspect the CI failure logs and "
                                        "repository state manually."
                                    )
                                    run_cmd(
                                        [
                                            gh_bin,
                                            "pr",
                                            "comment",
                                            str(pr_number),
                                            "--body",
                                            comment_body,
                                        ],
                                        check=False,
                                    )
                                    save_processed_comment_ids(pr_number, [run_id_key])
                                    sys.exit(1)

                                candidate = find_prompt_path_for_branch(branch_name)
                                if candidate:
                                    ci_fixed = address_ci_failure(
                                        gh_bin=gh_bin,
                                        agy_bin=agy_bin,
                                        pr_number=pr_number,
                                        pr_title=pr_title or "",
                                        branch_name=branch_name,
                                        prompt_path=candidate,
                                        run_info=failed_run,
                                        model=model,
                                        effort=effort,
                                    )
                                    if ci_fixed:
                                        current_wait = poll_interval
                                        continue
                                else:
                                    logger.warning(
                                        "Could not locate prompt file for branch '%s'. "
                                        "Skipping automatic CI healing.",
                                        branch_name,
                                    )
                                    save_processed_comment_ids(pr_number, [run_id_key])
                elif pending:
                    logger.info(
                        "⏳ PR #%s CI checks in progress (%d pending, %d passed)... "
                        "Waiting for green CI (re-checking in %s%s)",
                        pr_number,
                        len(pending),
                        len(passed),
                        wait_str,
                        enter_hint,
                    )
                else:
                    mark_pr_ci_green(gh_bin, pr_number)
                    if str(pr_number) not in green_notified_prs:
                        send_notification(
                            "PR Ready for Merge",
                            f"PR #{pr_number} CI is green! Ready for human review.",
                            sound="Hero",
                            subtitle=pr_title,
                        )
                        green_notified_prs.add(str(pr_number))

                    if ci_billing_local_green:
                        logger.info(
                            "✅ PR #%s ('%s') local checks are GREEN "
                            "(GitHub Actions billing/quota limit reached). "
                            "Waiting for owner to merge... (re-checking in %s%s)",
                            pr_number,
                            pr_title,
                            wait_str,
                            enter_hint,
                        )
                    else:
                        logger.info(
                            "✅ PR #%s ('%s') CI is GREEN (%d checks passed). "
                            "Waiting for owner to merge... (re-checking in %s%s)",
                            pr_number,
                            pr_title,
                            len(passed),
                            wait_str,
                            enter_hint,
                        )
            else:
                wait_str = format_interval(current_wait)
                enter_hint = ", or press Enter to recheck now" if _is_stdin_interactive() else ""
                logger.info(
                    "Waiting for open PR #%s ('%s') on branch '%s' to be merged... "
                    "(re-checking in %s%s)",
                    pr_number,
                    pr_title,
                    branch_name,
                    wait_str,
                    enter_hint,
                )

            # Responsive sleep waking immediately if Enter is pressed on stdin or on Ctrl+C
            user_rechecked = _interruptible_sleep(current_wait)
            if user_rechecked:
                logger.info("⚡ Instant recheck triggered (Enter pressed). Checking PR status...")
                current_wait = poll_interval
                continue

            # Exponential backoff: double wait time until reaching max_interval (e.g. 30m)
            current_wait = min(current_wait * 2, max_interval)
    except KeyboardInterrupt:
        logger.info(
            "\n[INTERRUPTED] Ctrl+C detected while waiting for PR merge. "
            "Exiting daemon runner cleanly."
        )
        sys.exit(0)


def detect_uncommitted_prompt_slug() -> str | None:
    """Detect if the working tree has uncommitted changes belonging to a prompt."""
    status_res = run_cmd(["git", "status", "--porcelain"], check=False)
    if not status_res.stdout.strip():
        return None
    for line in status_res.stdout.splitlines():
        # Match prompts/queued/<slug>.md or prompts/implemented/<slug>.md
        match = re.search(r"prompts/(?:queued|implemented)/(\d{4}-[^.\s]+)\.md", line)
        if match:
            return match.group(1)
        # Match docs/learnings/<slug>-learnings.md
        match_learnings = re.search(r"docs/learnings/(\d{4}-[^.\s]+)-learnings\.md", line)
        if match_learnings:
            return match_learnings.group(1)
    return None


def get_in_progress_prompt_branches(
    branch_prefix: str | None = None,
) -> list[tuple[int, str, str]]:
    """Find any local <branch_prefix>/<slug> branches.

    Returns a sorted list of (number, slug, branch_name).
    """
    prefix = branch_prefix or BRANCH_PREFIX
    res = run_cmd(
        ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}/"],
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return []

    branches: list[tuple[int, str, str]] = []
    for line in res.stdout.strip().splitlines():
        branch = line.strip()
        slug = branch.removeprefix(f"{prefix}/")
        match = re.match(r"^(\d{4})-", slug)
        if not match:
            continue
        num = int(match.group(1))
        branches.append((num, slug, branch))

    branches.sort(key=lambda x: x[0])
    return branches


def get_unmerged_implemented_prompts(
    base_branch: str | None = None,
) -> list[tuple[int, Path]]:
    """Find prompts in prompts/implemented/ that are not yet merged into origin."""
    if not IMPLEMENTED_DIR.is_dir():
        return []

    unmerged: list[tuple[int, Path]] = []
    for path in IMPLEMENTED_DIR.glob("*.md"):
        match = re.match(r"^(\d{4})-", path.name)
        if match:
            slug = path.stem
            if not is_prompt_merged_into_origin_main(slug, base_branch=base_branch):
                unmerged.append((int(match.group(1)), path))

    unmerged.sort(key=lambda x: x[0])
    return unmerged


def reconcile_startup_branch(
    gh_bin: str,
    base_branch: str | None = None,
    branch_prefix: str | None = None,
) -> tuple[str, Path | None, str | None]:
    """Inspect local repository and remote state to reconcile startup branch and active work.

    Returns:
        (current_branch, active_branch_candidate, active_branch_feedback)
    """
    base = base_branch or BASE_BRANCH
    prefix = branch_prefix or BRANCH_PREFIX
    current_branch = get_current_branch()

    # Step 1: If on base branch, check uncommitted prompt changes or unmerged local branches
    if current_branch == base:
        dirty_slug = detect_uncommitted_prompt_slug()
        if dirty_slug:
            target_branch = f"{prefix}/{dirty_slug}"
            logger.info(
                "Detected uncommitted changes for prompt %s on '%s'. Switching to branch '%s'...",
                dirty_slug,
                base,
                target_branch,
            )
            ref_check = run_cmd(
                ["git", "show-ref", "--verify", f"refs/heads/{target_branch}"],
                check=False,
            )
            if ref_check.returncode == 0:
                run_cmd(["git", "checkout", target_branch], check=True)
            else:
                run_cmd(["git", "checkout", "-b", target_branch], check=True)
            current_branch = target_branch
        else:
            run_cmd(["git", "fetch", "origin", base], check=False)
            for _, slug, branch in get_in_progress_prompt_branches(branch_prefix=prefix):
                branch_pr = get_branch_pr_status(gh_bin, branch)
                pr_state = (branch_pr.get("state") or "").upper() if branch_pr else ""
                if is_prompt_merged_into_origin_main(slug, base_branch=base):
                    continue
                logger.info(
                    "Found unmerged in-progress branch '%s' (PR state: %s). Switching to it...",
                    branch,
                    pr_state or "none",
                )
                run_cmd(["git", "checkout", branch], check=True)
                current_branch = branch
                break

    # Step 2: If on a prompt branch (<prefix>/* or gemini/* or orchestack/*), evaluate merge state
    active_prefix: str | None = None
    for cand_prefix in (prefix, "gemini", "orchestack"):
        if cand_prefix and current_branch.startswith(f"{cand_prefix}/"):
            active_prefix = cand_prefix
            break

    if active_prefix is not None:
        slug = current_branch.removeprefix(f"{active_prefix}/")
        candidate = find_prompt_path_for_branch(current_branch, branch_prefix=active_prefix)

        # 1. Fetch origin/<base> first so remote state is known
        run_cmd(["git", "fetch", "origin", base], check=False)

        # 2. Check if this active branch was already merged or already has an open PR
        branch_pr = get_branch_pr_status(gh_bin, current_branch)
        pr_state = (branch_pr.get("state") or "").upper() if branch_pr else ""

        status_res = run_cmd(["git", "status", "--porcelain"], check=False)
        has_uncommitted = bool(status_res.stdout.strip())
        merged_in_origin_main = is_prompt_merged_into_origin_main(slug, base_branch=base)

        ancestor_res = run_cmd(
            ["git", "merge-base", "--is-ancestor", current_branch, f"origin/{base}"],
            check=False,
        )
        is_ancestor = ancestor_res.returncode == 0

        # Branch is only merged if origin/<base> contains the implemented prompt,
        # the branch commits are an ancestor of origin/<base>, and the working tree is clean.
        is_merged = is_ancestor and merged_in_origin_main and not has_uncommitted

        if is_merged:
            pr_desc = f" (PR #{branch_pr['number']})" if branch_pr else ""
            logger.info(
                "Active branch '%s' has already been merged into %s%s. Switching to %s...",
                current_branch,
                base,
                pr_desc,
                base,
            )
            run_cmd(["git", "checkout", base], check=False)
            return (base, None, None)

        if pr_state == "OPEN":
            logger.info(
                "Active branch '%s' already has open PR #%s (%s). Resuming PR wait/monitoring...",
                current_branch,
                branch_pr.get("number"),
                branch_pr.get("url"),
            )
            return (current_branch, None, None)

        if candidate and candidate.exists():
            dod_ok, dod_error = check_definition_of_done(candidate)
            if dod_ok:
                if has_uncommitted:
                    logger.info("Validated changes detected on '%s'. Committing...", current_branch)
                    run_cmd(["git", "add", "."])
                    safe, issues = verify_git_staging_safety()
                    if not safe:
                        logger.error(
                            "Git staging safety check failed on '%s': %s. Halting daemon.",
                            current_branch,
                            issues,
                        )
                        send_notification(
                            "Daemon Halted",
                            f"Safety check failed on {current_branch}",
                            sound="Basso",
                        )
                        sys.exit(1)
                    run_cmd(["git", "commit", "-m", f"feat({slug}): implement {slug}"])
                logger.info("Ensuring PR is open for completed branch '%s'...", current_branch)
                pr_ok = ensure_pr_opened(gh_bin, current_branch, candidate, base_branch=base)
                if not pr_ok:
                    logger.error(
                        "Failed to open PR for branch '%s'. Halting daemon.", current_branch
                    )
                    sys.exit(1)
                return (current_branch, None, None)
            else:
                logger.warning(
                    "Active branch '%s' has in-progress work for %s that did not satisfy "
                    "Definition of Done: %s. Resuming implementation session to address failure...",
                    current_branch,
                    candidate.name,
                    dod_error.strip().split("\n")[0],
                )
                feedback = (
                    f"Definition of Done verification failed for {candidate.name}.\n\n"
                    f"{dod_error}\n\n"
                    f"Please diagnose and resolve this issue. If '{CHECK_CMD}' timed out, "
                    "look for infinite loops, hanging network requests, test deadlocks, "
                    f"or long-running commands. Ensure all tests and checks complete "
                    f"promptly and `{CHECK_CMD}` passes locally."
                )
                return (current_branch, candidate, feedback)

    return (current_branch, None, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Blue Team Harness Prompt Daemon Runner")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Target repository root directory (default: current repository root)",
    )
    parser.add_argument(
        "--base-branch",
        type=str,
        default=None,
        help="Target base branch for pull requests and git sync (e.g. main, master)",
    )
    parser.add_argument(
        "--branch-prefix",
        type=str,
        default=None,
        help="Branch prefix for prompt branches (e.g. orchestack, gemini)",
    )
    parser.add_argument(
        "--check-cmd",
        type=str,
        default=None,
        help="Verification command to run during Definition of Done check",
    )
    parser.add_argument(
        "--forge",
        type=str,
        default=None,
        choices=["auto", "github", "gitlab", "local"],
        help="Forge adapter type override (default: auto)",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default=None,
        choices=["agy", "gemini", "antigravity", "claude", "codex", "shell"],
        help="Agent runner type override (default: gemini)",
    )
    parser.add_argument(
        "--no-require-learnings",
        action="store_true",
        default=False,
        help="Do not require a docs/learnings/<slug>-learnings.md file in Definition of Done",
    )
    parser.add_argument(
        "--no-require-pr-description",
        action="store_true",
        default=False,
        help="Do not require a docs/PR_DESCRIPTION.md file in Definition of Done",
    )
    parser.add_argument(
        "--auto-whitelist",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Automatically trust repository collaborators/members for PR review feedback",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Initial seconds between polling for merged PRs (default: 30s)",
    )
    parser.add_argument(
        "--max-poll-interval",
        type=int,
        default=1800,
        help="Maximum backoff wait seconds for PR merge (default: 1800s / 30m)",
    )
    parser.add_argument(
        "--max-auto-heal-attempts",
        type=int,
        default=DEFAULT_MAX_AUTO_HEAL_ATTEMPTS,
        help=(
            f"Maximum automated CI healing or review fix attempts per PR "
            f"(default: {DEFAULT_MAX_AUTO_HEAL_ATTEMPTS})"
        ),
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=f"Model slug override to pass to agy (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--effort",
        type=str,
        default=DEFAULT_EFFORT,
        choices=["low", "medium", "high", "none"],
        help=f"Reasoning effort override to pass to agy (default: {DEFAULT_EFFORT})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one prompt session and exit without waiting for subsequent merges.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Explicitly resume an in-progress session on the active prompt branch using agy -c.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Force a clean start for the prompt even if an existing session or branch exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate PR detection, git sync, and prompt selection without executing agy.",
    )
    args = parser.parse_args()
    effort_val = None if args.effort == "none" else args.effort

    has_config_overrides = any(
        (
            args.repo is not None,
            args.base_branch is not None,
            args.branch_prefix is not None,
            args.check_cmd is not None,
            args.forge is not None,
            args.agent is not None,
            args.no_require_learnings,
            args.no_require_pr_description,
            args.auto_whitelist is not None,
            any(k.startswith("ORCHESTACK_") for k in os.environ),
        )
    )
    if has_config_overrides:
        runtime_cfg = load_repo_config(
            repo_root=args.repo if args.repo is not None else REPO_ROOT,
            base_branch=args.base_branch,
            branch_prefix=args.branch_prefix,
            check_cmd=args.check_cmd,
            forge_type=args.forge,
            agent_type=args.agent,
            require_learnings=False if args.no_require_learnings else None,
            require_pr_description=False if args.no_require_pr_description else None,
            auto_whitelist_collaborators=args.auto_whitelist,
        )
        configure_runtime(runtime_cfg)

    gh_bin = get_gh_bin()
    agy_bin = None
    if not args.dry_run:
        agy_bin = get_agy_bin()
        initialize_baseline_permissions()

    logger.info("=== Blue Team Harness Daemon Started (Sandboxed) ===")
    logger.info("Repository root   : %s", REPO_ROOT)
    logger.info("GitHub CLI        : %s", gh_bin)
    logger.info("Antigravity CLI   : %s", agy_bin or "(dry-run mode)")
    logger.info("Model             : %s", args.model or "(CLI default)")
    logger.info("Reasoning Effort  : %s", effort_val or "(none)")
    logger.info(
        "PR poll interval  : %ds initial (exponential backoff up to %s)",
        args.poll_interval,
        format_interval(args.max_poll_interval),
    )

    while True:
        active_branch_candidate: Path | None = None
        active_branch_feedback: str | None = None
        if not args.dry_run:
            current_branch, active_branch_candidate, active_branch_feedback = (
                reconcile_startup_branch(gh_bin)
            )

            if not active_branch_candidate:
                # Step 1: Wait for any currently open PRs to be merged with exponential backoff
                wait_for_pr_merge(
                    gh_bin,
                    args.poll_interval,
                    max_interval=args.max_poll_interval,
                    max_auto_heal_attempts=args.max_auto_heal_attempts,
                    agy_bin=agy_bin,
                    model=args.model,
                    effort=effort_val,
                )

                # Step 2: Sync main
                try:
                    update_main()
                except Exception as e:
                    logger.warning(
                        "Syncing main failed (%s). Retrying in %ds...", e, args.poll_interval
                    )
                    time.sleep(args.poll_interval)
                    continue
        else:
            logger.info("[DRY-RUN] Checking open PRs (non-blocking)...")
            open_prs = get_open_prs(gh_bin, non_blocking=True)
            if open_prs:
                pr = open_prs[0]
                pr_num = pr.get("number")
                logger.info(
                    "[DRY-RUN] Found open PR: #%s (%s)",
                    pr_num,
                    pr.get("title"),
                )
                if pr_num:
                    unhandled = fetch_unhandled_pr_feedback(gh_bin, pr_num)
                    if unhandled:
                        logger.info(
                            "[DRY-RUN] Found %d unhandled review comment(s) on PR #%s. "
                            "Would resume session.",
                            len(unhandled),
                            pr_num,
                        )
                    else:
                        logger.info(
                            "[DRY-RUN] PR #%s is open and awaiting review/merge. "
                            "In normal execution, daemon will wait for PR merge.",
                            pr_num,
                        )
                        break
            else:
                logger.info("[DRY-RUN] No open PRs detected.")

        if active_branch_candidate:
            next_prompt = active_branch_candidate
            active_branch_candidate = None
            feedback_for_session = active_branch_feedback
            active_branch_feedback = None
        else:
            feedback_for_session = None
            # Step 3: Find next queued prompt
            next_prompt = get_next_prompt()
            if not next_prompt:
                unmerged_impl = get_unmerged_implemented_prompts()
                if unmerged_impl:
                    _, next_prompt = unmerged_impl[0]
                    logger.info(
                        "Found unmerged prompt in prompts/implemented/: %s. Processing...",
                        next_prompt.name,
                    )
                else:
                    logger.info(
                        "✨ Queue is empty! All prompts in prompts/queued/ have been implemented."
                    )
                    break

            logger.info("🚀 Next prompt to implement: %s", next_prompt.name)

        # Step 4: Checkout clean prompt branch or resume in-progress work
        should_resume = False
        current_branch = get_current_branch()
        session_log = LOGS_DIR / f"session-{next_prompt.stem}.log"
        target_branch_name = f"{BRANCH_PREFIX}/{next_prompt.stem}"
        if feedback_for_session is not None or (
            not args.fresh
            and (
                args.resume
                or (
                    current_branch in {target_branch_name, f"gemini/{next_prompt.stem}"}
                    and session_log.exists()
                )
            )
        ):
            should_resume = True
            logger.info(
                "Resuming active session for prompt %s (pass --fresh to discard)...",
                next_prompt.name,
            )

        if args.dry_run:
            action = "resume" if should_resume else f"checkout branch '{target_branch_name}'"
            logger.info("[DRY-RUN] Would %s", action)
            logger.info(
                "[DRY-RUN] Would execute prompt '%s' with agy (--sandbox)", next_prompt.name
            )
            logger.info("[DRY-RUN] Dry run simulation completed.")
            break

        branch_name = setup_prompt_branch(
            next_prompt, resume=should_resume, fresh=args.fresh, gh_bin=gh_bin
        )

        # Step 5: Run headless agent session with sandbox & interactive gate
        assert agy_bin is not None
        session_start_time = time.time()
        success = execute_prompt_session(
            agy_bin,
            next_prompt,
            model=args.model,
            effort=effort_val,
            resume=should_resume,
            custom_prompt=feedback_for_session,
        )
        session_duration = time.time() - session_start_time
        args.resume = False
        if not success:
            logger.error(
                "Session failed for prompt %s. Halting daemon for inspection.", next_prompt.name
            )
            send_notification(
                "Daemon Halted",
                f"Session failed for {next_prompt.name}",
                sound="Basso",
                subtitle="Manual inspection required",
            )
            conv_id = get_latest_conversation_id()
            record_daemon_metrics(
                {
                    "prompt_name": next_prompt.name,
                    "prompt_slug": next_prompt.stem,
                    "branch": branch_name,
                    "pr_number": None,
                    "status": "session_failed",
                    "session_duration_seconds": round(session_duration, 1),
                    "ci_wait_seconds": 0.0,
                    "ci_heal_attempts": 0,
                    "review_feedback_attempts": 0,
                    **extract_session_transcript_metrics(conv_id),
                }
            )
            sys.exit(1)

        # Step 6: Verify commit count and auto-commit validated changes if needed
        new_commits = count_new_commits(branch_name)

        if new_commits == 0:
            status_res = run_cmd(["git", "status", "--porcelain"], check=False)
            if status_res.stdout.strip():
                logger.info(
                    "Validated changes detected in working tree. Committing implemented work..."
                )
                run_cmd(["git", "add", "."])
                safe, issues = verify_git_staging_safety()
                if not safe:
                    logger.error(
                        "Git staging safety check failed for prompt %s: %s. Halting daemon.",
                        next_prompt.name,
                        issues,
                    )
                    send_notification(
                        "Daemon Halted",
                        f"Safety check failed for {next_prompt.name}",
                        sound="Basso",
                        subtitle="Ignored or untracked files detected",
                    )
                    sys.exit(1)
                commit_msg = f"feat({next_prompt.stem}): implement {next_prompt.stem}"
                run_cmd(["git", "commit", "-m", commit_msg])
                new_commits = count_new_commits(branch_name)

        if new_commits == 0:
            logger.error("No commits were made by the session on branch '%s'.", branch_name)
            logger.error(
                "Definition of Done not met for prompt %s. Halting daemon for manual inspection.",
                next_prompt.name,
            )
            send_notification(
                "Daemon Halted",
                f"DoD not met for {next_prompt.name}",
                sound="Basso",
                subtitle="Manual inspection required",
            )
            sys.exit(1)

        # Step 7: Ensure PR is open
        pr_ok = ensure_pr_opened(gh_bin, branch_name, next_prompt)
        if not pr_ok:
            logger.error(
                "Failed to ensure PR is open for branch '%s'. Halting daemon.", branch_name
            )
            send_notification(
                "Daemon Halted",
                f"Failed to open PR for {branch_name}",
                sound="Basso",
            )
            sys.exit(1)

        # Refresh branch_name in case ensure_pr_opened renamed it to avoid collision
        branch_name = get_current_branch()

        # Step 8: Telemetry extraction and metrics recording
        pr_num = None
        pr_status = get_branch_pr_status(gh_bin, branch_name)
        if pr_status:
            pr_num = pr_status.get("number")

        conv_id = get_latest_conversation_id()
        record_daemon_metrics(
            {
                "prompt_name": next_prompt.name,
                "prompt_slug": next_prompt.stem,
                "branch": branch_name,
                "pr_number": pr_num,
                "status": "pr_opened",
                "session_duration_seconds": round(session_duration, 1),
                "ci_wait_seconds": 0.0,
                "ci_heal_attempts": get_ci_heal_attempts(pr_num) if pr_num else 0,
                "review_feedback_attempts": get_review_feedback_attempts(pr_num) if pr_num else 0,
                **extract_session_transcript_metrics(conv_id),
            }
        )

        if args.once:
            logger.info("Run-once flag specified. Exiting.")
            break

        logger.info(
            "Prompt %s session finished! Waiting for you to review and merge the PR on GitHub...",
            next_prompt.name,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n[INTERRUPTED] Daemon runner interrupted by user (Ctrl+C). Exiting cleanly.")
        sys.exit(0)

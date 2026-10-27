# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Google Antigravity CLI (`agy`) AgentBackend implementation and transcript/permission helpers."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestack.agents.base import (
    AgentBackend,
    AgentSessionResult,
    HeartbeatInfo,
    PermissionDenial,
)

logger = logging.getLogger("bth-daemon")

DEFAULT_MODEL = "gemini-3.8-flash-high"
DEFAULT_EFFORT = "high"

CLI_DIR = Path.home() / ".gemini" / "antigravity-cli"
SETTINGS_FILE = CLI_DIR / "settings.json"
CLI_BRAIN_DIR = CLI_DIR / "brain"
CLI_CONV_DIR = CLI_DIR / "conversations"
CLI_SUMMARIES_DB = CLI_DIR / "conversation_summaries.db"
CLI_LOG_FILE = CLI_DIR / "cli.log"

ALT_REPO_ROOT = Path.home() / "OneDrive - Virgin Media O2" / "Code" / "BlueTeamHarness"

NETWORK_AND_TRANSIENT_PATTERNS: list[tuple[str, str]] = [
    ("can't assign requested address", "Network interface down / unassigned address"),
    ("network is down", "Network down"),
    ("network is unreachable", "Network unreachable"),
    ("network unreachable", "Network unreachable"),
    ("no route to host", "No route to host"),
    ("connection reset", "Connection reset"),
    ("connection refused", "Connection refused"),
    ("broken pipe", "Broken pipe"),
    ("socket is not connected", "Socket not connected"),
    ("i/o timeout", "I/O timeout"),
    ("tls handshake timeout", "TLS handshake timeout"),
    ("context deadline exceeded", "Context deadline exceeded"),
    ("client.timeout", "Client timeout"),
    ("deadline_exceeded", "Deadline exceeded"),
    ("timed out", "Timeout"),
    ("temporary failure in name resolution", "DNS resolution failure"),
    ("nodename nor servname provided", "DNS lookup failure"),
    ("no such host", "DNS host lookup failure"),
    ("dial tcp", "TCP dial failure"),
    ("read tcp", "TCP read failure"),
    ("write tcp", "TCP write failure"),
    ("stream error", "Stream error"),
    ("streamgeneratecontent", "Stream generation error"),
    ("request failed", "API request failed"),
    ("eoferror", "EOF error"),
    ("unexpected eof", "Unexpected EOF"),
    ("internal (code 500)", "Internal error (code 500)"),
    ("unavailable (code 503)", "Service unavailable (code 503)"),
    ("resource_exhausted", "Resource exhausted (rate limit)"),
    ("improperly formatted function call", "Improperly formatted tool function call"),
    ("improperly formatted", "Improperly formatted tool call"),
    ("malformed function call", "Malformed tool function call"),
]


def build_safe_baseline_allow(
    repo_root: Path,
    alt_repo_root: Path = ALT_REPO_ROOT,
) -> list[str]:
    """Build granular, least-privilege baseline allowlist rules scoped to `repo_root`."""
    return [
        f"write_file({repo_root})",
        f"write_file({repo_root}/)",
        f"write_file({repo_root.resolve()})",
        f"write_file({repo_root.resolve()}/)",
        f"write_file({alt_repo_root})",
        f"write_file({alt_repo_root}/)",
        f"write_file({repo_root}/.git)",
        f"write_file({repo_root}/.git/)",
        f"write_file({repo_root.resolve()}/.git)",
        f"write_file({repo_root.resolve()}/.git/)",
        f"write_file({alt_repo_root}/.git)",
        f"write_file({alt_repo_root}/.git/)",
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
        "unsandboxed(uv)",
        "unsandboxed(uv *)",
    ]


def build_default_deny() -> list[str]:
    """Build default deny rules protecting sensitive host paths and dangerous commands."""
    return [
        f"write_file({Path.home()}/.ssh)",
        f"write_file({Path.home()}/.ssh/)",
        "write_file(/etc)",
        "write_file(/etc/)",
        "command(sudo)",
        "command(sudo *)",
        "command(rm -rf)",
        "command(rm -rf *)",
        "command(curl)",
        "command(curl *)",
        "command(wget)",
        "command(wget *)",
    ]


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


def parse_iso_timestamp(ts_str: Any) -> float | None:
    """Parse an ISO 8601 timestamp string from a transcript step into a POSIX timestamp."""
    if not isinstance(ts_str, str) or not ts_str.strip():
        return None
    try:
        clean_str = ts_str.strip()
        if clean_str.endswith("Z"):
            clean_str = clean_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:
        return None


def _normalize_db_timestamp(raw_ts: Any) -> float | None:
    """Normalize a database last_modified_time value (s/ms/us/ns or ISO string) to POSIX seconds."""
    if raw_ts is None:
        return None
    if isinstance(raw_ts, str):
        try:
            raw_ts = float(raw_ts)
        except ValueError:
            return parse_iso_timestamp(raw_ts)
    if isinstance(raw_ts, (int, float)):
        val = float(raw_ts)
        if val <= 0:
            return None
        if val > 1e16:
            return val / 1e9
        if val > 1e13:
            return val / 1e6
        if val > 1e11:
            return val / 1e3
        return val
    return None


def _get_conversation_mtime(conv_dir: Path) -> float:
    """Return the most recent modification time for a conversation directory and its transcripts."""
    mtime = conv_dir.stat().st_mtime
    logs_dir = conv_dir / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        t_file = logs_dir / name
        if t_file.exists():
            with contextlib.suppress(OSError):
                mtime = max(mtime, t_file.stat().st_mtime)
    return mtime


def get_latest_conversation_id(
    since_timestamp: float = 0.0,
    *,
    brain_dir: Path | None = None,
    summaries_db: Path | None = None,
) -> str | None:
    """Find the latest CLI conversation ID from `summaries_db` or `brain_dir`."""
    resolved_brain_dir = CLI_BRAIN_DIR if brain_dir is None else brain_dir
    resolved_summaries_db = CLI_SUMMARIES_DB if summaries_db is None else summaries_db

    db_conv_id: str | None = None
    db_mtime: float | None = None
    if resolved_summaries_db.exists():
        try:
            with sqlite3.connect(resolved_summaries_db) as con:
                row = (
                    con.cursor()
                    .execute(
                        "SELECT conversation_id, last_modified_time FROM conversation_summaries "
                        "ORDER BY last_modified_time DESC LIMIT 1"
                    )
                    .fetchone()
                )
                if row:
                    db_conv_id = str(row[0])
                    db_mtime = _normalize_db_timestamp(row[1])
        except Exception:
            pass

    brain_conv_id: str | None = None
    brain_mtime: float = 0.0
    db_conv_brain_mtime: float | None = None
    try:
        if resolved_brain_dir.is_dir():
            conv_dirs = [
                d for d in resolved_brain_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
            ]
            if conv_dirs:
                dir_mtimes = {d.name: _get_conversation_mtime(d) for d in conv_dirs}
                if db_conv_id and db_conv_id in dir_mtimes:
                    db_conv_brain_mtime = dir_mtimes[db_conv_id]
                if since_timestamp > 0:
                    dir_mtimes = {
                        k: v for k, v in dir_mtimes.items() if v >= (since_timestamp - 2.0)
                    }
                if dir_mtimes:
                    brain_conv_id, brain_mtime = max(dir_mtimes.items(), key=lambda kv: kv[1])
    except Exception:
        pass

    if since_timestamp > 0 and db_conv_id:
        if db_conv_brain_mtime is not None:
            db_is_fresh = db_conv_brain_mtime >= (since_timestamp - 2.0)
        elif db_mtime is not None:
            db_is_fresh = db_mtime >= (since_timestamp - 2.0)
        else:
            db_is_fresh = False
        if not db_is_fresh:
            db_conv_id = None

    if (
        db_conv_id
        and brain_conv_id
        and db_conv_brain_mtime is not None
        and brain_mtime > db_conv_brain_mtime
    ):
        return brain_conv_id

    return db_conv_id or brain_conv_id


def get_session_transcript_status(
    conv_id: str | None,
    since_timestamp: float = 0.0,
    *,
    brain_dir: Path | None = None,
) -> tuple[int | str, str] | None:
    """Read the latest step from transcript.jsonl for real-time progress reporting."""
    resolved_brain_dir = CLI_BRAIN_DIR if brain_dir is None else brain_dir
    candidate_paths: list[Path] = []
    if conv_id:
        candidate_paths.append(
            resolved_brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        )

    # Fallback to most recently updated transcript if conv_id not yet known
    has_valid_path = bool(candidate_paths and any(p.exists() for p in candidate_paths))
    if not has_valid_path and resolved_brain_dir.is_dir():
        recent = sorted(
            resolved_brain_dir.glob("*/.system_generated/logs/transcript.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if recent:
            candidate_paths.append(recent[0])

    target_file = next((p for p in candidate_paths if p.exists()), None)
    if not target_file:
        return None
    if since_timestamp > 0:
        with contextlib.suppress(OSError):
            if target_file.stat().st_mtime < (since_timestamp - 2.0):
                return None

    try:
        with target_file.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            seek_pos = max(0, size - 8192)
            f.seek(seek_pos)
            chunk = f.read().decode("utf-8", errors="ignore")

        lines = [line.strip() for line in chunk.strip().split("\n") if line.strip()]
        if not lines:
            return None

        for raw_line in reversed(lines):
            try:
                data = json.loads(raw_line)
            except Exception:
                continue

            if since_timestamp > 0:
                step_ts = parse_iso_timestamp(data.get("created_at"))
                if step_ts is not None and step_ts < (since_timestamp - 2.0):
                    break

            step_idx = data.get("step_index", "?")
            step_type = data.get("type", "UNKNOWN")

            if data.get("tool_calls"):
                tc = data["tool_calls"][0]
                fn = tc.get("function", {})
                name = fn.get("name") or tc.get("name") or "tool"
                args: Any = fn.get("arguments") or tc.get("args") or {}
                if isinstance(args, str):
                    with contextlib.suppress(Exception):
                        args = json.loads(args)
                if not isinstance(args, dict):
                    args = {}
                action = args.get("toolAction") or args.get("toolSummary") or ""
                cmd = args.get("CommandLine") or args.get("command") or ""
                target_file_arg = (
                    args.get("TargetFile") or args.get("AbsolutePath") or args.get("path") or ""
                )

                if action:
                    return step_idx, f"Tool {name}: {action}"
                if cmd:
                    clean_cmd = str(cmd).strip().replace("\n", " ")
                    if len(clean_cmd) > 50:
                        clean_cmd = clean_cmd[:47] + "..."
                    return step_idx, f"Tool {name}: `{clean_cmd}`"
                if target_file_arg:
                    file_name = Path(target_file_arg).name
                    return step_idx, f"Tool {name}: {file_name}"
                return step_idx, f"Tool: {name}"

            if step_type == "PLANNER_RESPONSE":
                content = (data.get("content") or "").strip()
                if content:
                    first_line = content.split("\n")[0].strip()
                    if len(first_line) > 60:
                        first_line = first_line[:57] + "..."
                    return step_idx, f"Gemini: {first_line}"
                return step_idx, "Gemini thinking / reasoning..."

            if step_type == "GENERIC":
                return step_idx, "Tool finished, processing output..."

            if step_type == "SYSTEM_MESSAGE":
                return step_idx, "Task update received, reasoning..."

            if step_type == "USER_INPUT":
                return step_idx, "Prompt received, starting turn..."

        return None
    except Exception:
        return None


def extract_latest_agent_summary(
    conv_id: str | None = None,
    *,
    brain_dir: Path | None = None,
) -> str | None:
    """Extract the concluding planner response from the latest session transcript."""
    resolved_brain_dir = CLI_BRAIN_DIR if brain_dir is None else brain_dir
    candidate_paths: list[Path] = []
    if conv_id:
        candidate_paths.extend(
            [
                resolved_brain_dir
                / conv_id
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl",
                resolved_brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl",
            ]
        )

    if resolved_brain_dir.is_dir():
        for p in sorted(
            resolved_brain_dir.glob("*/.system_generated/logs/transcript*.jsonl"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            candidate_paths.append(p)

    target_file = next((p for p in candidate_paths if p.is_file()), None)
    if not target_file:
        return None

    try:
        last_response: str | None = None
        with target_file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                    if data.get("source") == "MODEL" and data.get("type") == "PLANNER_RESPONSE":
                        content = data.get("content")
                        if content and isinstance(content, str) and content.strip():
                            last_response = content.strip()
                except Exception:
                    continue
        return last_response
    except Exception as e:
        logger.debug("Failed extracting latest agent summary from transcript: %s", e)
        return None


def extract_last_agent_response(
    conv_id: str | None = None,
    *,
    brain_dir: Path | None = None,
    cli_log_path: Path | None = None,
) -> str:
    """Extract the final agent response from session transcripts or fallback CLI log."""
    summary = extract_latest_agent_summary(conv_id=conv_id, brain_dir=brain_dir)
    if summary:
        return summary
    if cli_log_path and cli_log_path.is_file():
        with contextlib.suppress(OSError):
            lines = [
                line.strip()
                for line in cli_log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            ]
            if lines:
                return lines[-1]
    return ""


def extract_session_transcript_metrics(
    conv_id: str | None = None,
    *,
    brain_dir: Path | None = None,
) -> dict[str, Any]:
    """Parse session transcript to extract steps, tool counts, and estimated token/cost metrics."""
    resolved_brain_dir = CLI_BRAIN_DIR if brain_dir is None else brain_dir
    candidate_paths: list[Path] = []
    if conv_id:
        candidate_paths.extend(
            [
                resolved_brain_dir
                / conv_id
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl",
                resolved_brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl",
            ]
        )

    if resolved_brain_dir.is_dir():
        for p in sorted(
            resolved_brain_dir.glob("*/.system_generated/logs/transcript*.jsonl"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        ):
            candidate_paths.append(p)

    target_file = next((p for p in candidate_paths if p.is_file()), None)

    metrics: dict[str, Any] = {
        "total_steps": 0,
        "input_tokens_estimate": 0,
        "output_tokens_estimate": 0,
        "total_tokens_estimate": 0,
        "estimated_cost_usd": 0.0,
        "tool_calls_breakdown": {},
    }

    if not target_file:
        return metrics

    try:
        total_steps = 0
        input_tokens = 0
        output_tokens = 0
        tool_counts: dict[str, int] = {}

        with target_file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                except Exception:
                    continue

                total_steps += 1

                usage = data.get("usage") or data.get("token_usage") or {}
                if isinstance(usage, dict) and (
                    usage.get("prompt_tokens") or usage.get("input_tokens")
                ):
                    in_t = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                    out_t = (
                        usage.get("completion_tokens")
                        or usage.get("output_tokens")
                        or usage.get("candidates_token_count")
                        or 0
                    )
                    input_tokens += int(in_t)
                    output_tokens += int(out_t)
                else:
                    step_type = data.get("type", "")
                    content = str(data.get("content") or "")
                    thinking = str(data.get("thinking") or "")
                    if step_type in ("USER_INPUT", "SYSTEM_MESSAGE"):
                        input_tokens += max(1, len(content) // 4)
                    elif step_type == "PLANNER_RESPONSE":
                        output_tokens += max(1, (len(content) + len(thinking)) // 4)

                tool_calls = data.get("tool_calls") or []
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name") or tc.get("name") or "unknown_tool"
                    tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
                    args = fn.get("arguments") or tc.get("args") or ""
                    input_tokens += len(str(args)) // 4

        est_cost = (input_tokens * 1.25 + output_tokens * 5.0) / 1_000_000.0

        metrics["total_steps"] = total_steps
        metrics["input_tokens_estimate"] = input_tokens
        metrics["output_tokens_estimate"] = output_tokens
        metrics["total_tokens_estimate"] = input_tokens + output_tokens
        metrics["estimated_cost_usd"] = round(est_cost, 4)
        metrics["tool_calls_breakdown"] = tool_counts
    except Exception as e:
        logger.debug("Failed parsing transcript for telemetry metrics: %s", e)

    return metrics


def extract_command_prefix(command: str) -> str:
    """Extract the leading executable prefix from a single-line or multiline command string."""
    if not command or not command.strip():
        return ""

    # Find the first non-empty, non-comment line
    first_line = ""
    for raw_line in command.splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#"):
            first_line = stripped
            break
    if not first_line:
        return ""

    try:
        tokens = shlex.split(first_line, posix=True)
    except ValueError:
        tokens = first_line.split()

    if not tokens:
        return ""

    idx = 0
    # Skip leading environment variable assignments (KEY=VALUE) and `env` wrapper
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "env":
            idx += 1
            continue
        if "=" in tok and not tok.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            idx += 1
            continue
        break

    if idx >= len(tokens):
        return tokens[0]

    return tokens[idx]


def parse_denial_from_step(data: dict[str, Any]) -> tuple[str, str | None] | None:
    """Extract perm_kind and target from a transcript step if it recorded a denial."""
    err = str(data.get("error") or "")
    if not ("user denied permission" in err or "permission check failed" in err):
        return None

    # 1. Check for quoted target in permission check failed
    m_fail = re.search(
        r'permission check failed for (\w+)\s+("(?:[^"\\]|\\.)*")',
        err,
        re.DOTALL,
    )
    if m_fail:
        perm_kind = m_fail.group(1).strip()
        raw_target = m_fail.group(2).strip()
        try:
            target = json.loads(raw_target)
        except Exception:
            target = raw_target.strip('"')
        return perm_kind, target

    # 2. Check for multiline denied command block
    m_cmd = re.search(
        r"user denied permission to run command:\s*\n(.*?)"
        r"(?:\nDo not attempt to circumvent|\Z)",
        err,
        re.DOTALL,
    )
    if m_cmd:
        target = m_cmd.group(1).strip()
        perm_kind = "command"
        m_k = re.search(r"permission check failed for ([a-zA-Z0-9_]+)", err)
        if m_k:
            perm_kind = m_k.group(1).strip()
        return perm_kind, target

    # 3. Check for action(target)
    m_perm = re.search(
        r"user denied permission for ([a-zA-Z0-9_]+)\((.*?)\)",
        err,
        re.DOTALL,
    )
    if m_perm:
        perm_kind = m_perm.group(1).strip()
        target = m_perm.group(2).strip() or None
        return perm_kind, target

    m_fallback = re.search(r"permission check failed for (\w+)", err)
    if m_fallback:
        return m_fallback.group(1).strip(), None
    return "command", None


def check_recent_soft_denial_in_cli_log(
    since_offset: int = 0,
    *,
    cli_log_file: Path | None = None,
) -> str | None:
    """Inspect recent CLI log lines for soft-denied tool confirmations in print mode."""
    resolved_log_file = CLI_LOG_FILE if cli_log_file is None else cli_log_file
    if not resolved_log_file.exists():
        return None
    try:
        file_size = resolved_log_file.stat().st_size
        with resolved_log_file.open("r", encoding="utf-8", errors="ignore") as f:
            # If log was not truncated, seek to previous offset; if truncated, read from start
            if 0 < since_offset <= file_size:
                f.seek(since_offset)
            lines = f.readlines()
        for line in reversed(lines):
            m = re.search(r'soft-denying tool confirmation "([^"]+)"', line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def check_recent_transcript_denial(
    since_timestamp: float = 0.0,
    conv_id: str | None = None,
    *,
    brain_dir: Path | None = None,
) -> tuple[str, str | None] | None:
    """Check if the latest step in active conversation transcript recorded a denial."""
    resolved_brain_dir = CLI_BRAIN_DIR if brain_dir is None else brain_dir
    try:
        conv_dirs: list[Path] = []
        if conv_id and (resolved_brain_dir / conv_id).is_dir():
            conv_dirs = [resolved_brain_dir / conv_id]
        elif resolved_brain_dir.is_dir():
            conv_dirs = [
                d for d in resolved_brain_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
            ]
        if not conv_dirs:
            return None
        latest_conv = max(conv_dirs, key=lambda d: d.stat().st_mtime)
        for transcript_name in ("transcript_full.jsonl", "transcript.jsonl"):
            t_file = latest_conv / ".system_generated" / "logs" / transcript_name
            if not t_file.exists():
                continue
            if since_timestamp > 0 and t_file.stat().st_mtime < (since_timestamp - 2.0):
                continue
            try:
                lines = t_file.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines[-30:]):
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    step_ts = parse_iso_timestamp(data.get("created_at"))
                    if (
                        since_timestamp > 0
                        and step_ts is not None
                        and step_ts < (since_timestamp - 2.0)
                    ):
                        break
                    denial = parse_denial_from_step(data)
                    if denial is not None:
                        return denial
            except Exception:
                pass
    except Exception:
        pass
    return None


def extract_denied_permission(
    full_output: str,
    perm_hint: str | None = None,
    conv_id: str | None = None,
    since_timestamp: float = 0.0,
    *,
    brain_dir: Path | None = None,
    cli_log_file: Path | None = None,
) -> tuple[str, str | None]:
    """Inspect CLI output, recent transcripts, or CLI logs to find denied permission and target."""
    resolved_brain_dir = CLI_BRAIN_DIR if brain_dir is None else brain_dir
    resolved_log_file = CLI_LOG_FILE if cli_log_file is None else cli_log_file

    perm_kind = "command"
    if perm_hint:
        if perm_hint in ("ReadUrlContent", "read_url_content", "read_url"):
            perm_kind = "read_url"
        else:
            perm_kind = perm_hint
    target: str | None = None

    # 1. Parse jetski output or explicit permission denial directly from session output
    m_jetski = re.search(
        r'a tool required the "([^"]+)" permission that headless mode cannot prompt for',
        full_output,
    )
    if m_jetski:
        perm_kind = m_jetski.group(1).strip()

    m_fail = re.search(
        r'permission check failed for (\w+)\s+("(?:[^"\\]|\\.)*")',
        full_output,
        re.DOTALL,
    )
    if m_fail:
        perm_kind = m_fail.group(1).strip()
        raw_target = m_fail.group(2).strip()
        try:
            target = json.loads(raw_target)
        except Exception:
            target = raw_target.strip('"')
        return perm_kind, target

    m_cmd = re.search(
        r"user denied permission to run command:\s*\n(.*?)(?:\nDo not attempt to circumvent|\Z)",
        full_output,
        re.DOTALL,
    )
    if m_cmd:
        target = m_cmd.group(1).strip()
        m_k = re.search(r"permission check failed for ([a-zA-Z0-9_]+)", full_output)
        if m_k:
            perm_kind = m_k.group(1).strip()
        return perm_kind, target

    # 2. Inspect conversation transcript for exact error and target
    try:
        conv_dirs: list[Path] = []
        if conv_id and (resolved_brain_dir / conv_id).is_dir():
            conv_dirs = [resolved_brain_dir / conv_id]
        elif resolved_brain_dir.is_dir():
            conv_dirs = [
                d for d in resolved_brain_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
            ]

        if conv_dirs:
            latest_conv = max(conv_dirs, key=lambda d: d.stat().st_mtime)
            for transcript_name in ("transcript_full.jsonl", "transcript.jsonl"):
                t_file = latest_conv / ".system_generated" / "logs" / transcript_name
                if not t_file.exists():
                    continue
                if since_timestamp > 0 and t_file.stat().st_mtime < (since_timestamp - 2.0):
                    continue
                try:
                    lines = t_file.read_text(encoding="utf-8").splitlines()
                    # First search backwards for explicit error message
                    for line in reversed(lines):
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except Exception:
                            continue

                        step_ts = parse_iso_timestamp(data.get("created_at"))
                        if (
                            since_timestamp > 0
                            and step_ts is not None
                            and step_ts < (since_timestamp - 2.0)
                        ):
                            break

                        denial = parse_denial_from_step(data)
                        if denial is not None:
                            return denial

                    # If no explicit error string found, search for the most recent tool call
                    for line in reversed(lines):
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except Exception:
                            continue

                        step_ts = parse_iso_timestamp(data.get("created_at"))
                        if (
                            since_timestamp > 0
                            and step_ts is not None
                            and step_ts < (since_timestamp - 2.0)
                        ):
                            break

                        tool_calls = list(data.get("tool_calls") or [])
                        step_update = data.get("step_update", {})
                        if step_update.get("tool_name"):
                            tool_calls.append(
                                {
                                    "name": step_update.get("tool_name"),
                                    "args": step_update.get("tool_info", {}).get("parameters", {}),
                                }
                            )

                        for tc in reversed(tool_calls):
                            name = tc.get("name", "")
                            args = tc.get("args", {})
                            if perm_kind == "write_file" and name in (
                                "write_to_file",
                                "replace_file_content",
                            ):
                                raw_target = args.get("TargetFile")
                                if raw_target:
                                    raw_str = str(raw_target).strip()
                                    if raw_str.startswith('"') and raw_str.endswith('"'):
                                        try:
                                            target = json.loads(raw_str)
                                        except Exception:
                                            target = raw_str.strip('"')
                                    else:
                                        target = raw_str
                                    return perm_kind, target
                            elif perm_kind in ("command", "unsandboxed") and name == "run_command":
                                raw_target = args.get("CommandLine")
                                if raw_target:
                                    raw_str = str(raw_target).strip()
                                    if raw_str.startswith('"') and raw_str.endswith('"'):
                                        try:
                                            target = json.loads(raw_str)
                                        except Exception:
                                            target = raw_str.strip('"')
                                    else:
                                        target = raw_str
                                    return perm_kind, target
                            elif perm_kind == "read_url" or name in (
                                "read_url_content",
                                "ReadUrlContent",
                                "read_url",
                            ):
                                raw_url = args.get("Url") or args.get("url")
                                if raw_url:
                                    raw_str = str(raw_url).strip()
                                    if raw_str.startswith('"') and raw_str.endswith('"'):
                                        try:
                                            target = json.loads(raw_str)
                                        except Exception:
                                            target = raw_str.strip('"')
                                    else:
                                        target = raw_str
                                    return "read_url", target
                except Exception as e:
                    logger.debug("Error inspecting transcript %s: %s", t_file, e)
    except Exception as e:
        logger.debug("Error accessing CLI brain dir: %s", e)

    # 3. Fallback to CLI log inspection
    if resolved_log_file.exists():
        try:
            lines = resolved_log_file.read_text(encoding="utf-8").splitlines()[-60:]
            for line in reversed(lines):
                m_soft = re.search(r'soft-denying tool confirmation "([^"]+)"', line)
                if m_soft:
                    tool_name = m_soft.group(1).strip()
                    if tool_name in ("ReadUrlContent", "read_url_content", "read_url"):
                        return "read_url", target
                    return tool_name, target
                m = re.search(r"([a-zA-Z0-9_]+)\(([^)]+)\)", line)
                if m:
                    return m.group(1).strip(), m.group(2).strip()
        except Exception:
            pass

    return perm_kind, target


def initialize_baseline_permissions(
    *,
    settings_file: Path | None = None,
    repo_root: Path | None = None,
    safe_baseline_allow: list[str] | None = None,
    default_deny: list[str] | None = None,
) -> None:
    """Configure least-privilege permissions in settings.json."""
    resolved_settings = SETTINGS_FILE if settings_file is None else settings_file
    resolved_repo_root = Path.cwd() if repo_root is None else repo_root
    resolved_allow = (
        build_safe_baseline_allow(resolved_repo_root)
        if safe_baseline_allow is None
        else safe_baseline_allow
    )
    resolved_deny = build_default_deny() if default_deny is None else default_deny

    try:
        data: dict[str, Any] = {}
        if resolved_settings.exists():
            try:
                data = json.loads(resolved_settings.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        # 1. Set toolPermission to proceed-in-sandbox
        data["toolPermission"] = "proceed-in-sandbox"

        # 2. Add repo paths to trustedWorkspaces
        trusted: list[str] = data.setdefault("trustedWorkspaces", [])
        for p in (str(resolved_repo_root), str(resolved_repo_root.resolve())):
            if p not in trusted:
                trusted.append(p)

        perms: dict[str, Any] = data.setdefault("permissions", {})
        allow_list: list[str] = perms.setdefault("allow", [])
        ask_list: list[str] = perms.setdefault("ask", [])
        deny_list: list[str] = perms.setdefault("deny", [])

        # 3. Remove dangerous blanket wildcards and unsupported globs
        if "command(*)" in allow_list:
            allow_list.remove("command(*)")
            logger.info("Removed blanket 'command(*)' from allowlist.")

        perms["allow"] = [
            r
            for r in allow_list
            if not (
                r.startswith("write_file(")
                and (not r.startswith("write_file(/") or "/*" in r or r == "write_file(*)")
            )
        ]
        allow_list = perms["allow"]

        perms["deny"] = [
            r
            for r in deny_list
            if not (
                r.startswith("write_file(")
                and (
                    not r.startswith("write_file(/")
                    or "/*" in r
                    or "/.git" in r
                    or r.endswith("/.git)")
                    or r.endswith("/.git/)")
                )
            )
        ]
        deny_list = perms["deny"]

        # 4. Remove blanket ask rules so granular allow rules take effect
        for item in ("command(*)", "write_file(*)", "unsandboxed(*)"):
            if item in ask_list:
                ask_list.remove(item)
                logger.info("Removed '%s' from ask list to enable granular allow rules.", item)

        # 5. Add safe baseline commands
        for rule in resolved_allow:
            if rule not in allow_list:
                allow_list.append(rule)

        # 6. Add dangerous commands to deny
        for rule in resolved_deny:
            if rule not in deny_list:
                deny_list.append(rule)

        resolved_settings.parent.mkdir(parents=True, exist_ok=True)
        resolved_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Initialized least-privilege permissions in %s", resolved_settings)
    except Exception as e:
        logger.warning("Could not initialize permissions in settings.json: %s", e)


def add_allowed_rules(
    rules: list[str] | str,
    *,
    settings_file: Path | None = None,
) -> None:
    """Add verified rule(s) to permissions.allow in settings.json."""
    resolved_settings = SETTINGS_FILE if settings_file is None else settings_file
    rule_list = [rules] if isinstance(rules, str) else rules

    try:
        data: dict[str, Any] = {}
        if resolved_settings.exists():
            try:
                data = json.loads(resolved_settings.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        perms = data.setdefault("permissions", {})
        allow_list: list[str] = perms.setdefault("allow", [])
        for rule in rule_list:
            if rule not in allow_list:
                allow_list.append(rule)
                logger.info("[+] Whitelisted rule: %s", rule)

        resolved_settings.parent.mkdir(parents=True, exist_ok=True)
        resolved_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("Failed to write rule to settings.json: %s", e)


def whitelist_command_in_settings(
    command_or_prefix: str,
    *,
    perm_kind: str = "command",
    prefix_only: bool = False,
    settings_file: Path | None = None,
) -> list[str]:
    """Whitelist a command or its executable prefix in Antigravity `settings.json`."""
    kind = perm_kind if perm_kind in ("command", "unsandboxed") else "command"
    target = command_or_prefix.strip()
    if not target:
        return []
    if prefix_only:
        prefix = extract_command_prefix(target) or target.split()[0]
        rules = [f"{kind}({prefix})", f"{kind}({prefix} *)"]
    else:
        rules = [f"{kind}({target})"]
        if f"{kind}({target} *)" not in rules:
            rules.append(f"{kind}({target} *)")
    add_allowed_rules(rules, settings_file=settings_file)
    return rules


def whitelist_url_in_settings(
    url_or_pattern: str,
    *,
    domain_wide: bool = False,
    settings_file: Path | None = None,
) -> str:
    """Whitelist a `read_url(...)` specific URL or domain pattern in `settings.json`."""
    raw = url_or_pattern.strip()
    if raw.startswith("read_url(") and raw.endswith(")"):
        rule = raw
    elif domain_wide:
        domain = ""
        with contextlib.suppress(Exception):
            domain = urllib.parse.urlparse(raw).netloc
        rule = f"read_url({domain}/*)" if domain else f"read_url({raw or '*'})"
    else:
        rule = f"read_url({raw})" if raw else "read_url(*)"
    add_allowed_rules(rule, settings_file=settings_file)
    return rule


def is_transient_session_error(full_output: str, returncode: int) -> tuple[bool, str]:
    """Check if session failed due to a transient API, HTTP, or network error."""
    full_lower = full_output.lower()

    # 1. Check AGY_ERROR JSON payload
    match = re.search(r"AGY_ERROR:\s*(\{.*?\})", full_output)
    if match:
        try:
            err_data = json.loads(match.group(1))
            code = err_data.get("error_code")
            status = str(err_data.get("status") or "").upper()
            kind = err_data.get("code_kind")
            short_err = str(err_data.get("short_error") or "")
            short_lower = short_err.lower()

            if (
                code in (429, 500, 502, 503, 504)
                or status in ("INTERNAL", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED")
                or kind == "http"
            ):
                return True, short_err or f"API error {code} ({status})"

            for pattern, desc in NETWORK_AND_TRANSIENT_PATTERNS:
                if pattern in short_lower:
                    return True, desc
        except Exception:
            pass

    # 2. Check full output for known network/transient error patterns
    for pattern, desc in NETWORK_AND_TRANSIENT_PATTERNS:
        if pattern in full_lower:
            return True, desc

    return False, ""


class AntigravityAgent(AgentBackend):
    """Google Antigravity CLI (`agy`) backend implementation."""

    name: str = "antigravity"

    def __init__(
        self,
        *,
        agy_bin: str = "agy",
        model: str | None = DEFAULT_MODEL,
        effort: str | None = DEFAULT_EFFORT,
        cli_dir: Path | None = None,
        brain_dir: Path | None = None,
        summaries_db: Path | None = None,
        cli_log_file: Path | None = None,
        settings_file: Path | None = None,
        extra_bin_paths: list[Path] | None = None,
    ) -> None:
        self.agy_bin = agy_bin
        self.model = model
        self.effort = effort
        base_dir = CLI_DIR if cli_dir is None else cli_dir
        self.cli_dir = base_dir
        self.brain_dir = (base_dir / "brain") if brain_dir is None else brain_dir
        self.summaries_db = (
            (base_dir / "conversation_summaries.db") if summaries_db is None else summaries_db
        )
        self.cli_log_file = (base_dir / "cli.log") if cli_log_file is None else cli_log_file
        self.settings_file = (
            (base_dir / "settings.json") if settings_file is None else settings_file
        )
        self.extra_bin_paths = (
            [
                Path.home() / ".local" / "bin",
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
            ]
            if extra_bin_paths is None
            else extra_bin_paths
        )
        self._last_turn_output: str = ""

    def check_available(self) -> tuple[bool, str]:
        """Return `(True, binary_path)` if `agy` is available, else `(False, reason)`."""
        found = find_binary(self.agy_bin, self.extra_bin_paths)
        if found:
            return True, found
        return False, f"Antigravity CLI ('{self.agy_bin}') not found in PATH or ~/.local/bin/agy"

    def build_command(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> list[str]:
        """Build headless `agy chat -m agent [-r <id>] <prompt>` without `--project`."""
        ok, resolved = self.check_available()
        binary = resolved if ok else self.agy_bin
        cmd = [binary, "chat", "-m", "agent"]
        if session_id:
            cmd.extend(["-r", session_id])
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        cmd.append(prompt)
        return cmd

    def run_turn(
        self,
        prompt: str,
        repo_root: Path,
        session_id: str | None = None,
        timeout: int = 1800,
        cli_log_path: Path | None = None,
    ) -> AgentSessionResult:
        """Execute a single headless Antigravity turn and return normalized `AgentSessionResult`."""
        log_path = self.cli_log_file if cli_log_path is None else cli_log_path
        cli_log_offset = log_path.stat().st_size if log_path.exists() else 0
        turn_start_time = time.time()
        cmd = self.build_command(prompt, session_id=session_id)

        try:
            proc = subprocess.run(
                cmd,
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            exit_code = proc.returncode
            stdout_str = proc.stdout or ""
            stderr_str = proc.stderr or ""
            full_output = stdout_str + ("\n" + stderr_str if stderr_str else "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            out = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            )
            err = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "")
            )
            full_output = out + ("\n" + err if err else "")
            stderr_str = f"Antigravity turn timed out after {timeout}s"

        self._last_turn_output = full_output
        resolved_session_id = (
            self.get_latest_session_id(repo_root, since_timestamp=turn_start_time) or session_id
        )
        last_response = ""
        if resolved_session_id:
            last_response = self.extract_last_response(resolved_session_id, cli_log_path=log_path)
        if not last_response:
            last_response = full_output.strip()

        is_transient, transient_reason = is_transient_session_error(full_output, exit_code)
        completed_normally = exit_code == 0 and not is_transient
        error_message: str | None = None
        if not completed_normally:
            if is_transient and transient_reason:
                error_message = transient_reason
            else:
                error_message = (
                    stderr_str.strip()
                    or full_output.strip()
                    or f"Antigravity exited with code {exit_code}"
                )

        return AgentSessionResult(
            session_id=resolved_session_id,
            exit_code=exit_code,
            completed_normally=completed_normally,
            error_message=error_message,
            last_response=last_response,
            cli_log_offset=cli_log_offset,
            turn_start_time=turn_start_time,
        )

    def get_latest_session_id(
        self,
        repo_root: Path,
        since_timestamp: float | None = None,
    ) -> str | None:
        """Resolve the latest conversation ID from `conversation_summaries.db` or `brain/`."""
        return get_latest_conversation_id(
            since_timestamp=0.0 if since_timestamp is None else since_timestamp,
            brain_dir=self.brain_dir,
            summaries_db=self.summaries_db,
        )

    def get_session_status(
        self,
        session_id: str,
        since_timestamp: float | None = None,
    ) -> tuple[bool, str | None]:
        """Return `(True, summary)` if active/recent transcript status exists for `session_id`."""
        status = get_session_transcript_status(
            session_id,
            since_timestamp=0.0 if since_timestamp is None else since_timestamp,
            brain_dir=self.brain_dir,
        )
        if status is None:
            return False, None
        step_idx, desc = status
        return True, f"Step {step_idx}: {desc}"

    def get_heartbeat(
        self,
        session_id: str | None,
        since_timestamp: float | None = None,
    ) -> HeartbeatInfo:
        """Return a `HeartbeatInfo` snapshot for `session_id`."""
        status = get_session_transcript_status(
            session_id,
            since_timestamp=0.0 if since_timestamp is None else since_timestamp,
            brain_dir=self.brain_dir,
        )
        if status is None:
            return HeartbeatInfo(active=False, summary="", updated_at=0.0)
        step_idx, desc = status
        updated_at = time.time()
        if session_id and (self.brain_dir / session_id).is_dir():
            with contextlib.suppress(OSError):
                updated_at = _get_conversation_mtime(self.brain_dir / session_id)
        summary = f"Step {step_idx}: {desc}"
        return HeartbeatInfo(active=True, summary=summary, updated_at=updated_at)

    def extract_last_response(
        self,
        session_id: str,
        cli_log_path: Path | None = None,
    ) -> str:
        """Return the concluding planner response for `session_id`."""
        return extract_last_agent_response(
            conv_id=session_id,
            brain_dir=self.brain_dir,
            cli_log_path=self.cli_log_file if cli_log_path is None else cli_log_path,
        )

    def detect_permission_denials(
        self,
        session_id: str | None,
        cli_log_path: Path | None,
        cli_log_offset: int = 0,
        turn_start_time: float | None = None,
    ) -> list[PermissionDenial]:
        """Detect turn-scoped permission denials from `cli.log` and conversation transcripts."""
        log_path = self.cli_log_file if cli_log_path is None else cli_log_path
        since_ts = 0.0 if turn_start_time is None else turn_start_time

        soft_denied_tool = check_recent_soft_denial_in_cli_log(
            since_offset=cli_log_offset,
            cli_log_file=log_path,
        )
        transcript_denial = check_recent_transcript_denial(
            since_timestamp=since_ts,
            conv_id=session_id,
            brain_dir=self.brain_dir,
        )

        has_output_denial = bool(
            self._last_turn_output
            and (
                "jetski: no output produced" in self._last_turn_output
                or "auto-denied" in self._last_turn_output
                or "permission check failed" in self._last_turn_output
                or "user denied permission" in self._last_turn_output
            )
        )

        perm_info: tuple[str, str | None] | None = None
        if transcript_denial and (transcript_denial[0] or transcript_denial[1]):
            perm_info = transcript_denial
        elif soft_denied_tool or has_output_denial:
            perm_info = extract_denied_permission(
                self._last_turn_output,
                perm_hint=soft_denied_tool,
                conv_id=session_id,
                since_timestamp=since_ts,
                brain_dir=self.brain_dir,
                cli_log_file=log_path,
            )

        if perm_info is None:
            return []

        perm_kind, target = perm_info
        resource = target or ""
        cmd_prefix: str | None = None
        if perm_kind in ("command", "unsandboxed") and resource:
            cmd_prefix = extract_command_prefix(resource) or None

        raw_reason = f"{perm_kind}({resource})" if resource else perm_kind
        return [
            PermissionDenial(
                tool_name=perm_kind,
                resource=resource,
                command_prefix=cmd_prefix,
                raw_reason=raw_reason,
            )
        ]

    def whitelist_denial(
        self,
        denial: PermissionDenial,
        repo_root: Path,
    ) -> bool:
        """Persist an allow rule for `denial` in `settings.json`."""
        try:
            if denial.tool_name == "read_url":
                whitelist_url_in_settings(
                    denial.resource,
                    settings_file=self.settings_file,
                )
            elif denial.tool_name == "write_file":
                if denial.resource:
                    add_allowed_rules(
                        f"write_file({denial.resource})",
                        settings_file=self.settings_file,
                    )
                else:
                    add_allowed_rules(
                        [
                            f"write_file({repo_root})",
                            f"write_file({repo_root}/)",
                            f"write_file({repo_root.resolve()})",
                            f"write_file({repo_root.resolve()}/)",
                        ],
                        settings_file=self.settings_file,
                    )
            elif denial.tool_name in ("command", "unsandboxed"):
                target = denial.resource or denial.command_prefix or ""
                if not target:
                    return False
                whitelist_command_in_settings(
                    target,
                    perm_kind=denial.tool_name,
                    settings_file=self.settings_file,
                )
            else:
                rule = (
                    f"{denial.tool_name}({denial.resource})"
                    if denial.resource
                    else f"{denial.tool_name}(*)"
                )
                add_allowed_rules(rule, settings_file=self.settings_file)
            return True
        except Exception:
            return False

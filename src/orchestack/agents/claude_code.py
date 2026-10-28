# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Anthropic Claude Code (`claude` CLI) agent backend implementation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orchestack.agents.antigravity import (
    extract_command_prefix,
    is_transient_session_error,
    parse_iso_timestamp,
)
from orchestack.agents.base import (
    AgentSessionResult,
    HeartbeatInfo,
    PermissionDenial,
)

DEFAULT_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

_DENIAL_PHRASES: tuple[str, ...] = (
    "permission to use ",
    "has been denied",
    "requires approval",
    "permission denied",
    "auto-denied",
    "not allowed to use",
    "tool permission denied",
)

_DENIAL_TOOL_RE = re.compile(
    r"Permission to use\s+([A-Za-z0-9_:-]+)"
    r"(?:\(([^)]*)\)|\s+with\s+command\s+['\"]([^'\"]+)['\"])?\s+has been denied",
    re.IGNORECASE,
)
_APPROVAL_CMD_RE = re.compile(
    r"(?:Bash\s+command|command|tool)\s+['\"]([^'\"]+)['\"]\s+requires approval",
    re.IGNORECASE,
)


def project_slug_for_repo(repo_root: Path) -> str:
    """Return the Claude Code project directory slug for `repo_root`."""
    resolved = str(repo_root.resolve())
    return re.sub(r"[^a-zA-Z0-9-]", "-", resolved)


def parse_claude_stream_jsonl(
    raw_text: str,
) -> list[dict[str, Any]]:
    """Parse JSONL event objects from Claude `--output-format stream-json` output."""
    events: list[dict[str, Any]] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def read_claude_log_slice(
    cli_log_path: Path | None,
    cli_log_offset: int = 0,
) -> str:
    """Read `cli_log_path` starting from byte offset `cli_log_offset` (`B2`)."""
    if cli_log_path is None or not cli_log_path.is_file():
        return ""
    try:
        file_size = cli_log_path.stat().st_size
        start = max(0, cli_log_offset) if file_size >= cli_log_offset else 0
        with cli_log_path.open("rb") as f:
            f.seek(start)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _event_timestamp(event: dict[str, Any]) -> float:
    """Extract epoch timestamp in seconds from a JSONL event if present."""
    for key in ("timestamp", "created_at", "createdAt", "time"):
        val = event.get(key)
        if isinstance(val, (int, float)):
            fval = float(val)
            return fval / 1000.0 if fval > 1e11 else fval
        if isinstance(val, str) and val.strip():
            parsed = parse_iso_timestamp(val.strip())
            if parsed is not None and parsed > 0:
                return parsed
    return 0.0


def _format_tool_use_activity(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a live heartbeat description from a Claude `tool_use` block."""
    normalized = tool_name.strip()
    if normalized == "Bash":
        cmd = str(tool_input.get("command") or tool_input.get("description") or "").strip()
        if cmd:
            first_line = cmd.splitlines()[0].strip()
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            return f"Tool: Bash ({first_line})"
        return "Tool: Bash"
    if normalized in {"Edit", "Write", "Read", "MultiEdit"}:
        path = str(
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        ).strip()
        if path:
            return f"Tool: {normalized} ({path})"
        return f"Tool: {normalized}"
    if normalized == "WebFetch":
        url = str(tool_input.get("url") or "").strip()
        if url:
            return f"Tool: WebFetch ({url})"
        return "Tool: WebFetch"
    return f"Tool: {normalized}"


def extract_session_id_from_events(events: Sequence[dict[str, Any]]) -> str | None:
    """Extract `session_id` from `system.init`, `result`, or other Claude JSONL events."""
    session_id: str | None = None
    for ev in events:
        ev_type = ev.get("type")
        if (ev_type == "system" and ev.get("subtype") == "init") or ev_type == "result":
            sid = ev.get("session_id")
            if isinstance(sid, str) and sid.strip():
                session_id = sid.strip()
        elif session_id is None:
            sid = ev.get("session_id") or ev.get("sessionId")
            if isinstance(sid, str) and sid.strip():
                session_id = sid.strip()
    return session_id


def extract_heartbeat_from_events(
    events: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
    since_timestamp: float | None = None,
) -> str | None:
    """Extract the latest activity description from Claude JSONL events."""
    latest_activity: str | None = None
    for ev in events:
        if session_id:
            ev_sid = ev.get("session_id") or ev.get("sessionId")
            if isinstance(ev_sid, str) and ev_sid.strip() and ev_sid.strip() != session_id:
                continue
        if since_timestamp is not None:
            ev_ts = _event_timestamp(ev)
            if ev_ts > 0 and ev_ts < since_timestamp:
                continue

        ev_type = ev.get("type")
        if ev_type == "assistant":
            msg = ev.get("message")
            content_list: list[Any] = []
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                content_list = msg["content"]
            elif isinstance(ev.get("content"), list):
                content_list = ev["content"]

            for block in content_list:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    tname = str(block.get("name") or "").strip()
                    tinput = block.get("input")
                    input_dict = tinput if isinstance(tinput, dict) else {}
                    if tname:
                        latest_activity = _format_tool_use_activity(tname, input_dict)
                elif btype == "text":
                    text = str(block.get("text") or "").strip()
                    if text and latest_activity is None:
                        first_line = text.splitlines()[0].strip()
                        latest_activity = first_line[:100]
        elif ev_type == "tool_use":
            tname = str(ev.get("name") or "").strip()
            tinput = ev.get("input")
            input_dict = tinput if isinstance(tinput, dict) else {}
            if tname:
                latest_activity = _format_tool_use_activity(tname, input_dict)
        elif ev_type == "result":
            subtype = str(ev.get("subtype") or "success")
            if latest_activity is None:
                latest_activity = f"Result: {subtype}"

    return latest_activity


def extract_result_from_events(
    events: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> tuple[str, str | None, bool | None]:
    """Extract `(response_text, subtype, is_error)` from Claude JSONL events."""
    result_text = ""
    result_subtype: str | None = None
    is_error: bool | None = None
    last_assistant_text = ""

    for ev in events:
        if session_id:
            ev_sid = ev.get("session_id") or ev.get("sessionId")
            if isinstance(ev_sid, str) and ev_sid.strip() and ev_sid.strip() != session_id:
                continue

        ev_type = ev.get("type")
        if ev_type == "assistant":
            msg = ev.get("message")
            content_list: list[Any] = []
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                content_list = msg["content"]
            elif isinstance(ev.get("content"), list):
                content_list = ev["content"]
            elif isinstance(msg, dict) and isinstance(msg.get("content"), str):
                text_val = msg["content"].strip()
                if text_val:
                    last_assistant_text = text_val

            text_parts: list[str] = []
            for block in content_list:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = str(block.get("text") or "").strip()
                    if t:
                        text_parts.append(t)
            if text_parts:
                last_assistant_text = "\n".join(text_parts)

        elif ev_type == "result":
            subtype_raw = ev.get("subtype")
            if isinstance(subtype_raw, str) and subtype_raw.strip():
                result_subtype = subtype_raw.strip()
            err_flag = ev.get("is_error")
            if isinstance(err_flag, bool):
                is_error = err_flag
            elif result_subtype in {"error_max_turns", "error_during_execution"}:
                is_error = True
            elif result_subtype == "success":
                is_error = False

            res_val = ev.get("result")
            if isinstance(res_val, str) and res_val.strip():
                result_text = res_val.strip()

    final_text = result_text if result_text else last_assistant_text
    return final_text, result_subtype, is_error


def _build_denial(
    tool_name: str,
    resource: str,
    raw_reason: str,
) -> PermissionDenial:
    """Create a normalized `PermissionDenial` for a Claude tool denial."""
    clean_tool = tool_name.strip() or "Bash"
    clean_res = resource.strip()
    cmd_prefix: str | None = None
    if clean_tool.lower() in {"bash", "command", "run_command"} and clean_res:
        cmd_prefix = extract_command_prefix(clean_res)
    return PermissionDenial(
        tool_name=clean_tool,
        resource=clean_res or clean_tool,
        command_prefix=cmd_prefix,
        raw_reason=raw_reason.strip(),
    )


def parse_claude_permission_denials(
    raw_text: str,
    *,
    session_id: str | None = None,
    turn_start_time: float | None = None,
) -> list[PermissionDenial]:
    """Detect permission denials from Claude stream-json events and stderr lines."""
    denials: list[PermissionDenial] = []
    seen: set[tuple[str, str]] = set()

    def _add(denial: PermissionDenial) -> None:
        key = (denial.tool_name, denial.resource)
        if key not in seen:
            seen.add(key)
            denials.append(denial)

    events = parse_claude_stream_jsonl(raw_text)
    tool_use_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    last_tool_use: tuple[str, dict[str, Any]] | None = None

    for ev in events:
        if session_id:
            ev_sid = ev.get("session_id") or ev.get("sessionId")
            if isinstance(ev_sid, str) and ev_sid.strip() and ev_sid.strip() != session_id:
                continue
        if turn_start_time is not None:
            ev_ts = _event_timestamp(ev)
            if ev_ts > 0 and ev_ts < turn_start_time:
                continue

        ev_type = ev.get("type")

        # 1. Track assistant tool_use blocks so subsequent tool_result denials can be resolved
        if ev_type == "assistant":
            msg = ev.get("message")
            content_list: list[Any] = []
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                content_list = msg["content"]
            elif isinstance(ev.get("content"), list):
                content_list = ev["content"]
            for block in content_list:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    t_id = str(block.get("id") or "").strip()
                    t_name = str(block.get("name") or "Bash").strip()
                    t_in = block.get("input")
                    t_input = t_in if isinstance(t_in, dict) else {}
                    last_tool_use = (t_name, t_input)
                    if t_id:
                        tool_use_by_id[t_id] = (t_name, t_input)

        # 2. Check result event's `permission_denials` array
        if ev_type == "result":
            raw_denials = ev.get("permission_denials")
            if isinstance(raw_denials, list):
                for item in raw_denials:
                    if isinstance(item, dict):
                        t_name = str(
                            item.get("tool_name") or item.get("tool") or item.get("name") or "Bash"
                        ).strip()
                        t_input = item.get("tool_input") or item.get("input") or {}
                        resource = ""
                        if isinstance(t_input, dict):
                            resource = str(
                                t_input.get("command")
                                or t_input.get("url")
                                or t_input.get("file_path")
                                or t_input.get("path")
                                or ""
                            ).strip()
                        if not resource:
                            resource = str(
                                item.get("command")
                                or item.get("resource")
                                or item.get("url")
                                or item.get("file_path")
                                or ""
                            ).strip()
                        reason = str(
                            item.get("reason")
                            or item.get("message")
                            or f"Permission to use {t_name} has been denied"
                        )
                        _add(_build_denial(t_name, resource, reason))
                    elif isinstance(item, str) and item.strip():
                        m = re.match(r"^([A-Za-z0-9_]+)\((.*)\)$", item.strip())
                        if m:
                            t_name = m.group(1)
                            res_val = m.group(2).removesuffix(":*")
                            _add(_build_denial(t_name, res_val, item.strip()))
                        else:
                            _add(_build_denial(item.strip(), item.strip(), item.strip()))

        # 3. Check user tool_result blocks or error events for denial messages
        blocks_to_check: list[dict[str, Any]] = []
        if ev_type == "user":
            msg = ev.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict):
                        blocks_to_check.append(b)
            elif isinstance(ev.get("content"), list):
                for b in ev["content"]:
                    if isinstance(b, dict):
                        blocks_to_check.append(b)
        elif ev_type in {"tool_result", "error", "system"}:
            blocks_to_check.append(ev)

        for block in blocks_to_check:
            content_raw = block.get("content") or block.get("error") or block.get("message") or ""
            if isinstance(content_raw, list):
                content_str = " ".join(
                    str(c.get("text") or c) if isinstance(c, dict) else str(c) for c in content_raw
                )
            else:
                content_str = str(content_raw)
            lower_content = content_str.lower()
            if not any(phrase in lower_content for phrase in _DENIAL_PHRASES):
                continue

            tool_use_id = str(block.get("tool_use_id") or "").strip()
            matched_tool = tool_use_by_id.get(tool_use_id) or last_tool_use

            m_tool = _DENIAL_TOOL_RE.search(content_str)
            m_cmd = _APPROVAL_CMD_RE.search(content_str)

            t_name = ""
            resource = ""
            if matched_tool is not None:
                t_name, t_input = matched_tool
                resource = str(
                    t_input.get("command")
                    or t_input.get("url")
                    or t_input.get("file_path")
                    or t_input.get("path")
                    or ""
                ).strip()
            if m_tool:
                if not t_name:
                    t_name = m_tool.group(1)
                if not resource:
                    resource = (m_tool.group(2) or m_tool.group(3) or "").strip()
            if m_cmd and not resource:
                if not t_name:
                    t_name = "Bash"
                resource = m_cmd.group(1).strip()

            _add(_build_denial(t_name or "Bash", resource, content_str))

    # 4. Also scan non-JSON stderr / plain text lines for denial phrases
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("{"):
            continue
        lower_line = line.lower()
        if not any(phrase in lower_line for phrase in _DENIAL_PHRASES):
            continue
        m_tool = _DENIAL_TOOL_RE.search(line)
        m_cmd = _APPROVAL_CMD_RE.search(line)
        if m_tool:
            t_name = m_tool.group(1)
            resource = (m_tool.group(2) or m_tool.group(3) or "").strip()
            _add(_build_denial(t_name, resource, line))
        elif m_cmd:
            _add(_build_denial("Bash", m_cmd.group(1).strip(), line))
        elif "webfetch" in lower_line:
            _add(_build_denial("WebFetch", "WebFetch", line))
        else:
            _add(_build_denial("Bash", "", line))

    return denials


def format_claude_allow_rule(denial: PermissionDenial) -> str:
    """Format a `PermissionDenial` into a Claude Code `permissions.allow` rule."""
    tool_lower = denial.tool_name.strip().lower()
    if tool_lower in {"webfetch", "web_fetch", "read_url", "fetch"}:
        return "WebFetch"
    if tool_lower in {"bash", "command", "run_command"}:
        prefix = denial.command_prefix or extract_command_prefix(denial.resource)
        if prefix and prefix != denial.tool_name:
            clean_prefix = prefix.removesuffix(":*").strip()
            if clean_prefix:
                return f"Bash({clean_prefix}:*)"
        if denial.resource and denial.resource != denial.tool_name:
            extracted = extract_command_prefix(denial.resource).removesuffix(":*").strip()
            if extracted:
                return f"Bash({extracted}:*)"
        return "Bash"
    if re.match(r"^[A-Za-z0-9_]+\(.*\)$", denial.tool_name.strip()):
        return denial.tool_name.strip()
    if denial.resource and denial.resource != denial.tool_name:
        return f"{denial.tool_name.strip()}({denial.resource.strip()})"
    return denial.tool_name.strip()


def whitelist_claude_denial_in_settings(
    denial: PermissionDenial,
    repo_root: Path,
) -> tuple[bool, str]:
    """Update `<repo_root>/.claude/settings.local.json` `permissions.allow` with `denial`."""
    rule = format_claude_allow_rule(denial)
    settings_path = Path(repo_root) / ".claude" / "settings.local.json"
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if settings_path.is_file():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except json.JSONDecodeError:
                data = {}

        perms = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            data["permissions"] = perms

        allow_list = perms.get("allow")
        if not isinstance(allow_list, list):
            allow_list = []
            perms["allow"] = allow_list

        if rule not in allow_list:
            allow_list.append(rule)

        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True, rule
    except OSError:
        return False, rule


class ClaudeCodeAgent:
    """`AgentBackend` implementation for the Anthropic Claude Code (`claude`) CLI."""

    name: str = "claude"

    def __init__(
        self,
        claude_bin: str = "claude",
        model: str | None = None,
        allowed_tools: Sequence[str] | None = None,
        extra_args: Sequence[str] | None = None,
        projects_dir: Path | None = None,
        cli_log_path: Path | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.model = model
        self._allowed_tools: list[str] = list(allowed_tools) if allowed_tools else []
        self.extra_args: list[str] = list(extra_args) if extra_args else []
        self.projects_dir = projects_dir or DEFAULT_CLAUDE_PROJECTS_DIR
        self.cli_log_path = cli_log_path
        self._last_session_id: str | None = None
        self._last_session_time: float = 0.0

    def get_allowed_tools(self) -> list[str]:
        """Return currently configured/whitelisted `--allowedTools` rules."""
        return list(self._allowed_tools)

    def check_available(self) -> tuple[bool, str]:
        """Return `(True, path_or_version)` if `claude` is installed, else `(False, reason)`."""
        resolved = (
            self.claude_bin
            if os.path.isabs(self.claude_bin) and os.access(self.claude_bin, os.X_OK)
            else shutil.which(self.claude_bin)
        )
        if not resolved:
            return False, f"Claude Code binary {self.claude_bin!r} not found in PATH"
        try:
            res = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                return True, res.stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            pass
        return True, resolved

    def build_command(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> list[str]:
        """Construct the `claude -p ... --output-format stream-json --verbose` command."""
        cmd = [self.claude_bin, "-p", prompt]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["--output-format", "stream-json", "--verbose"])
        if self.model:
            cmd.extend(["--model", self.model])
        if self._allowed_tools:
            cmd.extend(["--allowedTools", *self._allowed_tools])
        if self.extra_args:
            cmd.extend(self.extra_args)
        return cmd

    def run_turn(
        self,
        prompt: str,
        repo_root: Path,
        session_id: str | None = None,
        timeout: int = 1800,
        cli_log_path: Path | None = None,
    ) -> AgentSessionResult:
        """Execute a single Claude Code turn against `repo_root`."""
        log_path = cli_log_path or self.cli_log_path
        cli_log_offset = log_path.stat().st_size if log_path and log_path.exists() else 0
        turn_start_time = time.time()

        cmd = self.build_command(prompt, session_id=session_id)
        try:
            res = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            exit_code = res.returncode
            stdout = res.stdout or ""
            stderr = res.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            )
            stderr = f"Claude session timed out after {timeout}s"

        combined = stdout + ("\n" + stderr if stderr else "")
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(combined)
            except OSError:
                pass

        events = parse_claude_stream_jsonl(combined)
        resolved_session_id = (
            extract_session_id_from_events(events)
            or session_id
            or self.get_latest_session_id(repo_root, since_timestamp=turn_start_time)
        )
        if resolved_session_id:
            self._last_session_id = resolved_session_id
            self._last_session_time = time.time()

        last_response, subtype, is_error = extract_result_from_events(
            events,
            session_id=resolved_session_id,
        )
        if not last_response and resolved_session_id:
            last_response = self.extract_last_response(
                resolved_session_id,
                cli_log_path=log_path,
            )

        completed_normally = exit_code == 0 and not bool(is_error)
        if subtype in {"error_max_turns", "error_during_execution"}:
            completed_normally = False

        error_message: str | None = None
        if not completed_normally:
            is_trans, reason = is_transient_session_error(combined, exit_code)
            if is_trans:
                error_message = reason
            elif subtype in {"error_max_turns", "error_during_execution"}:
                error_message = last_response or f"Claude session failed with subtype: {subtype}"
            else:
                error_message = (
                    stderr.strip() or last_response or f"Claude exited with status {exit_code}"
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

    def _find_session_file(
        self,
        session_id: str,
        repo_root: Path | None = None,
    ) -> Path | None:
        """Locate `<projects_dir>/<project-slug>/<session_id>.jsonl` on disk."""
        if not session_id or not self.projects_dir.is_dir():
            return None
        if repo_root is not None:
            slug_dir = self.projects_dir / project_slug_for_repo(repo_root)
            direct = slug_dir / f"{session_id}.jsonl"
            if direct.is_file():
                return direct
        for candidate in self.projects_dir.rglob(f"{session_id}.jsonl"):
            if candidate.is_file():
                return candidate
        return None

    def get_latest_session_id(
        self,
        repo_root: Path,
        since_timestamp: float | None = None,
    ) -> str | None:
        """Resolve the most recent Claude `session_id` since `since_timestamp` (`B1`)."""
        # 1. Check projects_dir/<project-slug>/*.jsonl first, then fallback to **/*.jsonl
        candidates: list[tuple[float, str]] = []
        if self.projects_dir.is_dir():
            slug_dir = self.projects_dir / project_slug_for_repo(repo_root)
            search_dirs = [slug_dir] if slug_dir.is_dir() else [self.projects_dir]
            for base_dir in search_dirs:
                pattern = "*.jsonl" if base_dir != self.projects_dir else "**/*.jsonl"
                for path in base_dir.glob(pattern):
                    if not path.is_file():
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if since_timestamp is not None and mtime < since_timestamp:
                        continue
                    candidates.append((mtime, path.stem))

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

        # 2. Check configured cli_log_path if updated since since_timestamp
        if self.cli_log_path and self.cli_log_path.is_file():
            try:
                mtime = self.cli_log_path.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    text = self.cli_log_path.read_text(encoding="utf-8", errors="replace")
                    sid = extract_session_id_from_events(parse_claude_stream_jsonl(text))
                    if sid:
                        return sid
            except OSError:
                pass

        # 3. Check in-memory last session ID if created after since_timestamp
        if self._last_session_id and (
            since_timestamp is None or self._last_session_time >= since_timestamp
        ):
            return self._last_session_id

        return None

    def get_session_status(
        self,
        session_id: str,
        since_timestamp: float | None = None,
    ) -> tuple[bool, str | None]:
        """Return `(is_active_or_found, latest_activity)` for `session_id`."""
        events: list[dict[str, Any]] = []

        session_file = self._find_session_file(session_id)
        if session_file is not None:
            try:
                mtime = session_file.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    raw = session_file.read_text(encoding="utf-8", errors="replace")
                    events.extend(parse_claude_stream_jsonl(raw))
            except OSError:
                pass

        if self.cli_log_path and self.cli_log_path.is_file():
            try:
                mtime = self.cli_log_path.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    raw = self.cli_log_path.read_text(encoding="utf-8", errors="replace")
                    events.extend(parse_claude_stream_jsonl(raw))
            except OSError:
                pass

        if not events:
            return False, None

        activity = extract_heartbeat_from_events(
            events,
            session_id=session_id,
            since_timestamp=since_timestamp,
        )
        if activity is None:
            return False, None
        return True, activity

    def get_heartbeat(
        self,
        session_id: str | None,
        since_timestamp: float | None = None,
        cli_log_path: Path | None = None,
    ) -> HeartbeatInfo:
        """Return a `HeartbeatInfo` snapshot for `session_id`."""
        log_path = cli_log_path or self.cli_log_path
        events: list[dict[str, Any]] = []
        updated_at = 0.0

        if session_id:
            session_file = self._find_session_file(session_id)
            if session_file is not None:
                try:
                    mtime = session_file.stat().st_mtime
                    if since_timestamp is None or mtime >= since_timestamp:
                        updated_at = max(updated_at, mtime)
                        raw = session_file.read_text(encoding="utf-8", errors="replace")
                        events.extend(parse_claude_stream_jsonl(raw))
                except OSError:
                    pass

        if log_path and log_path.is_file():
            try:
                mtime = log_path.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    updated_at = max(updated_at, mtime)
                    raw = log_path.read_text(encoding="utf-8", errors="replace")
                    events.extend(parse_claude_stream_jsonl(raw))
            except OSError:
                pass

        activity = extract_heartbeat_from_events(
            events,
            session_id=session_id,
            since_timestamp=since_timestamp,
        )
        if not activity:
            return HeartbeatInfo(active=False, summary="", updated_at=0.0)
        return HeartbeatInfo(
            active=True,
            summary=activity,
            updated_at=updated_at or time.time(),
        )

    def extract_last_response(
        self,
        session_id: str,
        cli_log_path: Path | None = None,
    ) -> str:
        """Extract final response from stream-json `result` event or `~/.claude/projects/`."""
        log_path = cli_log_path or self.cli_log_path
        if log_path and log_path.is_file():
            try:
                raw = log_path.read_text(encoding="utf-8", errors="replace")
                events = parse_claude_stream_jsonl(raw)
                text, _, _ = extract_result_from_events(events, session_id=session_id or None)
                if text:
                    return text
            except OSError:
                pass

        if session_id:
            session_file = self._find_session_file(session_id)
            if session_file is not None:
                try:
                    raw = session_file.read_text(encoding="utf-8", errors="replace")
                    events = parse_claude_stream_jsonl(raw)
                    text, _, _ = extract_result_from_events(events, session_id=session_id)
                    if text:
                        return text
                except OSError:
                    pass

        return ""

    def detect_permission_denials(
        self,
        session_id: str | None,
        cli_log_path: Path | None,
        cli_log_offset: int = 0,
        turn_start_time: float | None = None,
    ) -> list[PermissionDenial]:
        """Detect turn-scoped permission denials since `cli_log_offset` (`B2`)."""
        log_path = cli_log_path or self.cli_log_path
        denials: list[PermissionDenial] = []
        seen: set[tuple[str, str]] = set()

        def _merge(items: Sequence[PermissionDenial]) -> None:
            for item in items:
                key = (item.tool_name, item.resource)
                if key not in seen:
                    seen.add(key)
                    denials.append(item)

        if log_path is not None and log_path.is_file():
            sliced = read_claude_log_slice(log_path, cli_log_offset=cli_log_offset)
            if sliced:
                _merge(
                    parse_claude_permission_denials(
                        sliced,
                        session_id=session_id,
                        turn_start_time=turn_start_time,
                    )
                )

        if session_id:
            session_file = self._find_session_file(session_id)
            if session_file is not None:
                try:
                    mtime = session_file.stat().st_mtime
                    if turn_start_time is None or mtime >= turn_start_time:
                        raw = session_file.read_text(encoding="utf-8", errors="replace")
                        _merge(
                            parse_claude_permission_denials(
                                raw,
                                session_id=session_id,
                                turn_start_time=turn_start_time,
                            )
                        )
                except OSError:
                    pass

        return denials

    def whitelist_denial(
        self,
        denial: PermissionDenial,
        repo_root: Path,
    ) -> bool:
        """Update `<repo_root>/.claude/settings.local.json` and `--allowedTools` for resume."""
        ok, rule = whitelist_claude_denial_in_settings(denial, repo_root)
        if rule and rule not in self._allowed_tools:
            self._allowed_tools.append(rule)
        return ok

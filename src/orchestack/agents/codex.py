# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""OpenAI Codex (`codex` CLI) agent backend implementation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
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

DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

_CODEX_DENIAL_PHRASES: tuple[str, ...] = (
    "sandbox_error",
    "approval_required",
    "operation not permitted",
    "permission denied",
    "requires approval",
    "blocked by sandbox",
    "read-only file system",
)

_ROLLOUT_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|thread_[A-Za-z0-9_-]+|sess_[A-Za-z0-9_-]+)$"
)
_PATH_IN_ERROR_RE = re.compile(r"(?:['\"])(/[^'\"]+)(?:['\"])|(/[\w./-]+)")


def parse_codex_jsonl(raw_text: str) -> list[dict[str, Any]]:
    """Parse JSONL event objects from `codex exec --json` output or rollout files."""
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


def read_codex_log_slice(
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
    """Extract epoch timestamp in seconds from a Codex JSONL event if present."""
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


def _normalize_command_field(cmd_val: Any) -> str:
    """Normalize a Codex `command` field (string or list of strings) into a single string."""
    if isinstance(cmd_val, str):
        return cmd_val.strip()
    if isinstance(cmd_val, list):
        parts = [str(p) for p in cmd_val if p is not None]
        if (
            len(parts) >= 3
            and parts[0] in {"bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh"}
            and parts[1] in {"-c", "-lc"}
        ):
            return parts[2].strip()
        return " ".join(parts).strip()
    return ""


def extract_thread_id_from_events(events: Sequence[dict[str, Any]]) -> str | None:
    """Extract `thread_id` / `session_id` from Codex JSONL events."""
    thread_id: str | None = None
    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "thread.started":
            tid = ev.get("thread_id") or ev.get("session_id")
            if isinstance(tid, str) and tid.strip():
                thread_id = tid.strip()
        elif ev_type == "session_meta":
            payload = ev.get("payload")
            if isinstance(payload, dict):
                tid = payload.get("id") or payload.get("thread_id") or payload.get("session_id")
                if isinstance(tid, str) and tid.strip():
                    thread_id = tid.strip()
        if thread_id is None:
            for key in ("thread_id", "session_id", "threadId", "sessionId"):
                val = ev.get(key)
                if isinstance(val, str) and val.strip():
                    thread_id = val.strip()
                    break
    return thread_id


def extract_thread_id_from_rollout_path(path: Path) -> str:
    """Extract `thread_id` from a `rollout-*.jsonl` file or its filename."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        tid = extract_thread_id_from_events(parse_codex_jsonl(raw))
        if tid:
            return tid
    except OSError:
        pass
    stem = path.stem
    m = _ROLLOUT_UUID_RE.search(stem)
    if m:
        return m.group(1)
    if stem.startswith("rollout-"):
        return stem.removeprefix("rollout-")
    return stem


def extract_codex_heartbeat_from_events(
    events: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
    since_timestamp: float | None = None,
) -> str | None:
    """Extract live heartbeat from `item.started` / `item.completed` events."""
    latest: str | None = None
    for ev in events:
        if session_id:
            ev_tid = ev.get("thread_id") or ev.get("session_id")
            if isinstance(ev_tid, str) and ev_tid.strip() and ev_tid.strip() != session_id:
                continue
        if since_timestamp is not None:
            ev_ts = _event_timestamp(ev)
            if ev_ts > 0 and ev_ts < since_timestamp:
                continue

        ev_type = str(ev.get("type") or "")
        if ev_type in {"item.started", "item.completed", "item.updated"}:
            item = ev.get("item")
            if not isinstance(item, dict):
                item = ev
            item_type = str(item.get("type") or item.get("item_type") or "").strip()
            if item_type == "command_execution":
                cmd_str = _normalize_command_field(item.get("command") or item.get("cmd"))
                if cmd_str:
                    first_line = cmd_str.splitlines()[0].strip()
                    if len(first_line) > 80:
                        first_line = first_line[:77] + "..."
                    latest = f"command_execution: {first_line}"
                else:
                    latest = "command_execution"
            elif item_type == "file_change":
                changes = item.get("changes")
                paths: list[str] = []
                if isinstance(changes, list):
                    for ch in changes:
                        if isinstance(ch, dict) and ch.get("path"):
                            paths.append(str(ch["path"]))
                        elif isinstance(ch, str):
                            paths.append(ch)
                if not paths:
                    single = item.get("path") or item.get("file_path")
                    if single:
                        paths.append(str(single))
                latest = f"file_change: {', '.join(paths)}" if paths else "file_change"
            elif item_type == "agent_message":
                text = str(
                    item.get("text") or item.get("message") or item.get("content") or ""
                ).strip()
                if text:
                    first_line = text.splitlines()[0].strip()
                    latest = f"agent_message: {first_line[:80]}"
                else:
                    latest = "agent_message"
            elif item_type:
                latest = f"{ev_type}: {item_type}"
    return latest


def extract_codex_last_response_from_events(
    events: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> str:
    """Extract the final `agent_message` text before `turn.completed` (or latest seen)."""
    current_agent_message = ""
    completed_agent_message = ""

    for ev in events:
        if session_id:
            ev_tid = ev.get("thread_id") or ev.get("session_id")
            if isinstance(ev_tid, str) and ev_tid.strip() and ev_tid.strip() != session_id:
                continue

        ev_type = str(ev.get("type") or "")
        if ev_type in {"item.completed", "item.started", "item.updated", "agent_message"}:
            raw_item = ev.get("item")
            item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else ev
            item_type = str(item.get("type") or item.get("item_type") or "")
            if ev_type == "agent_message" or item_type == "agent_message":
                text = str(
                    item.get("text") or item.get("message") or item.get("content") or ""
                ).strip()
                if text:
                    current_agent_message = text
        elif ev_type == "turn.completed":
            if current_agent_message:
                completed_agent_message = current_agent_message
            msg_field = ev.get("last_agent_message") or ev.get("result") or ev.get("message")
            if isinstance(msg_field, str) and msg_field.strip() and not completed_agent_message:
                completed_agent_message = msg_field.strip()

    return completed_agent_message or current_agent_message


def parse_codex_permission_denials(
    raw_text: str,
    *,
    session_id: str | None = None,
    turn_start_time: float | None = None,
) -> list[PermissionDenial]:
    """Detect sandbox/approval denials (`sandbox_error`, `approval_required`, etc.)."""
    denials: list[PermissionDenial] = []
    seen: set[tuple[str, str]] = set()

    def _add(tool_name: str, resource: str, raw_reason: str) -> None:
        clean_tool = tool_name.strip() or "command_execution"
        clean_res = resource.strip() or clean_tool
        cmd_prefix: str | None = None
        if (
            clean_tool
            in {"command_execution", "sandbox_error", "approval_required", "Bash", "command"}
            and clean_res != clean_tool
        ):
            cmd_prefix = extract_command_prefix(clean_res)
        key = (clean_tool, clean_res)
        if key not in seen:
            seen.add(key)
            denials.append(
                PermissionDenial(
                    tool_name=clean_tool,
                    resource=clean_res,
                    command_prefix=cmd_prefix,
                    raw_reason=raw_reason.strip(),
                )
            )

    events = parse_codex_jsonl(raw_text)
    for ev in events:
        if session_id:
            ev_tid = ev.get("thread_id") or ev.get("session_id")
            if isinstance(ev_tid, str) and ev_tid.strip() and ev_tid.strip() != session_id:
                continue
        if turn_start_time is not None:
            ev_ts = _event_timestamp(ev)
            if ev_ts > 0 and ev_ts < turn_start_time:
                continue

        ev_type = str(ev.get("type") or "")
        raw_item = ev.get("item")
        item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else ev
        item_type = str(item.get("type") or item.get("item_type") or "")
        err_obj = ev.get("error") or item.get("error")
        err_type = ""
        err_msg = ""
        if isinstance(err_obj, dict):
            err_type = str(err_obj.get("type") or err_obj.get("code") or "")
            err_msg = str(err_obj.get("message") or err_obj.get("reason") or "")
        elif isinstance(err_obj, str):
            err_msg = err_obj

        status_str = str(item.get("status") or ev.get("status") or "")
        output_str = str(
            item.get("aggregated_output")
            or item.get("stderr")
            or item.get("output")
            or ev.get("message")
            or ev.get("reason")
            or ""
        )
        cmd_str = _normalize_command_field(
            item.get("command") or item.get("cmd") or ev.get("command")
        )
        path_str = str(item.get("path") or item.get("file_path") or ev.get("path") or "").strip()

        combined_reason = " ".join(
            part for part in (ev_type, item_type, status_str, err_type, err_msg, output_str) if part
        )
        lower_combined = combined_reason.lower()

        is_Explicit_denial_type = (
            ev_type in {"sandbox_error", "approval_required"}
            or item_type in {"sandbox_error", "approval_required"}
            or err_type in {"sandbox_error", "approval_required"}
            or status_str in {"sandbox_error", "approval_required", "denied"}
        )
        has_denial_phrase = any(phrase in lower_combined for phrase in _CODEX_DENIAL_PHRASES)

        if is_Explicit_denial_type or has_denial_phrase:
            tool = (
                "sandbox_error"
                if "sandbox_error" in lower_combined
                else (
                    "approval_required"
                    if "approval_required" in lower_combined
                    else (item_type or "command_execution")
                )
            )
            resource = cmd_str or path_str
            if not resource:
                m_path = _PATH_IN_ERROR_RE.search(output_str or err_msg)
                if m_path:
                    resource = (m_path.group(1) or m_path.group(2) or "").strip()
            reason_text = err_msg or output_str or combined_reason
            _add(tool, resource or tool, reason_text)

    # Also scan non-JSON lines for sandbox/approval denials
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("{"):
            continue
        lower_line = line.lower()
        if any(phrase in lower_line for phrase in _CODEX_DENIAL_PHRASES):
            tool = (
                "sandbox_error"
                if "sandbox_error" in lower_line
                else (
                    "approval_required"
                    if "approval_required" in lower_line
                    else "command_execution"
                )
            )
            m_path = _PATH_IN_ERROR_RE.search(line)
            res_str = (m_path.group(1) or m_path.group(2) or "").strip() if m_path else tool
            _add(tool, res_str, line)

    return denials


def _serialize_codex_toml(data: dict[str, Any]) -> str:
    """Serialize a flat/simple Codex configuration dictionary to valid TOML."""
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []

    def _fmt_val(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            inner = ", ".join(json.dumps(str(x)) for x in v)
            return f"[{inner}]"
        return json.dumps(str(v))

    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{k} = {_fmt_val(v)}")

    for table_name, table_dict in tables:
        if lines:
            lines.append("")
        lines.append(f"[{table_name}]")
        for tk, tv in table_dict.items():
            lines.append(f"{tk} = {_fmt_val(tv)}")

    return "\n".join(lines) + "\n"


def whitelist_codex_denial_in_config(
    denial: PermissionDenial,
    repo_root: Path,
) -> tuple[bool, str | None]:
    """Update `<repo_root>/.codex/config.toml` to permit `denial`.

    Returns `(success, added_dir_if_any)`.
    """
    config_path = Path(repo_root) / ".codex" / "config.toml"
    added_dir: str | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if config_path.is_file():
            try:
                loaded = tomllib.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = dict(loaded)
            except Exception:
                data = {}

        if "approval_policy" not in data:
            data["approval_policy"] = "never"
        if "sandbox_mode" not in data:
            data["sandbox_mode"] = "workspace-write"

        allowed_cmds_raw = data.get("allowed_commands")
        allowed_cmds: list[str] = (
            [str(x) for x in allowed_cmds_raw] if isinstance(allowed_cmds_raw, list) else []
        )

        writable_roots_raw = data.get("writable_roots")
        writable_roots: list[str] = (
            [str(x) for x in writable_roots_raw] if isinstance(writable_roots_raw, list) else []
        )

        res = denial.resource.strip()
        prefix = (denial.command_prefix or "").strip()

        # Check if denial resource or reason points to a directory/file path
        if res.startswith("/") or res.startswith("~"):
            expanded = Path(res).expanduser()
            dir_str = str(expanded if expanded.is_dir() else expanded.parent)
            if dir_str and dir_str not in writable_roots:
                writable_roots.append(dir_str)
            added_dir = dir_str
        else:
            m_path = _PATH_IN_ERROR_RE.search(denial.raw_reason)
            if m_path:
                raw_p = (m_path.group(1) or m_path.group(2) or "").strip()
                if raw_p.startswith("/"):
                    p_obj = Path(raw_p)
                    dir_str = str(p_obj if p_obj.is_dir() else p_obj.parent)
                    if dir_str and dir_str not in writable_roots:
                        writable_roots.append(dir_str)
                    added_dir = dir_str

        cmd_rule = prefix or res
        if cmd_rule and cmd_rule not in allowed_cmds:
            allowed_cmds.append(cmd_rule)

        data["allowed_commands"] = allowed_cmds
        if writable_roots:
            data["writable_roots"] = writable_roots

        config_path.write_text(_serialize_codex_toml(data), encoding="utf-8")
        return True, added_dir
    except OSError:
        return False, None


class CodexAgent:
    """`AgentBackend` implementation for the OpenAI Codex (`codex`) CLI."""

    name: str = "codex"

    def __init__(
        self,
        codex_bin: str = "codex",
        model: str | None = None,
        sandbox_mode: str | None = None,
        full_auto: bool = True,
        add_dirs: Sequence[str] | None = None,
        extra_args: Sequence[str] | None = None,
        repo_root: Path | None = None,
        sessions_dir: Path | None = None,
        cli_log_path: Path | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.model = model
        self.sandbox_mode = sandbox_mode
        self.full_auto = full_auto
        self._add_dirs: list[str] = list(add_dirs) if add_dirs else []
        self.extra_args: list[str] = list(extra_args) if extra_args else []
        self.repo_root = repo_root
        self.sessions_dir = sessions_dir or DEFAULT_CODEX_SESSIONS_DIR
        self.cli_log_path = cli_log_path
        self._last_session_id: str | None = None
        self._last_session_time: float = 0.0

    def get_add_dirs(self) -> list[str]:
        """Return directories configured to be passed via `--add-dir`."""
        return list(self._add_dirs)

    def check_available(self) -> tuple[bool, str]:
        """Return `(True, path_or_version)` if `codex` is installed, else `(False, reason)`."""
        resolved = (
            self.codex_bin
            if os.path.isabs(self.codex_bin) and os.access(self.codex_bin, os.X_OK)
            else shutil.which(self.codex_bin)
        )
        if not resolved:
            return False, f"Codex binary {self.codex_bin!r} not found in PATH"
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
        repo_root: Path | None = None,
    ) -> list[str]:
        """Construct `codex exec` or `codex exec resume` CLI arguments."""
        target_root = repo_root or self.repo_root or Path.cwd()
        if session_id:
            cmd = [self.codex_bin, "exec", "resume", session_id, prompt, "--json"]
        else:
            cmd = [self.codex_bin, "exec", prompt, "--json", "-C", str(target_root)]

        if self.sandbox_mode:
            cmd.extend(["--sandbox", self.sandbox_mode])
        elif self.full_auto:
            cmd.append("--full-auto")

        if self.model:
            cmd.extend(["--model", self.model])

        for directory in self._add_dirs:
            cmd.extend(["--add-dir", directory])

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
        """Execute a single Codex turn against `repo_root`."""
        log_path = cli_log_path or self.cli_log_path
        cli_log_offset = log_path.stat().st_size if log_path and log_path.exists() else 0
        turn_start_time = time.time()

        cmd = self.build_command(prompt, session_id=session_id, repo_root=repo_root)
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
            stderr = f"Codex session timed out after {timeout}s"

        combined = stdout + ("\n" + stderr if stderr else "")
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(combined)
            except OSError:
                pass

        events = parse_codex_jsonl(combined)
        resolved_session_id = (
            extract_thread_id_from_events(events)
            or session_id
            or self.get_latest_session_id(repo_root, since_timestamp=turn_start_time)
        )
        if resolved_session_id:
            self._last_session_id = resolved_session_id
            self._last_session_time = time.time()

        last_response = extract_codex_last_response_from_events(
            events,
            session_id=resolved_session_id,
        )
        if not last_response and resolved_session_id:
            last_response = self.extract_last_response(
                resolved_session_id,
                cli_log_path=log_path,
            )

        has_turn_failed = any(ev.get("type") in {"turn.failed", "error"} for ev in events)
        completed_normally = exit_code == 0 and not has_turn_failed

        error_message: str | None = None
        if not completed_normally:
            is_trans, reason = is_transient_session_error(combined, exit_code)
            if is_trans:
                error_message = reason
            else:
                error_message = (
                    stderr.strip() or last_response or f"Codex exited with status {exit_code}"
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

    def _find_rollout_file(self, session_id: str) -> Path | None:
        """Locate the `rollout-*.jsonl` file matching `session_id` under `sessions_dir`."""
        if not session_id or not self.sessions_dir.is_dir():
            return None
        for path in self.sessions_dir.rglob("rollout-*.jsonl"):
            if not path.is_file():
                continue
            if session_id in path.stem:
                return path
            tid = extract_thread_id_from_rollout_path(path)
            if tid == session_id:
                return path
        return None

    def get_latest_session_id(
        self,
        repo_root: Path,
        since_timestamp: float | None = None,
    ) -> str | None:
        """Resolve the most recent `thread_id` from `~/.codex/sessions/` (`B1`)."""
        candidates: list[tuple[float, Path]] = []
        if self.sessions_dir.is_dir():
            for path in self.sessions_dir.rglob("rollout-*.jsonl"):
                if not path.is_file():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if since_timestamp is not None and mtime < since_timestamp:
                    continue
                candidates.append((mtime, path))

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return extract_thread_id_from_rollout_path(candidates[0][1])

        if self.cli_log_path and self.cli_log_path.is_file():
            try:
                mtime = self.cli_log_path.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    raw = self.cli_log_path.read_text(encoding="utf-8", errors="replace")
                    tid = extract_thread_id_from_events(parse_codex_jsonl(raw))
                    if tid:
                        return tid
            except OSError:
                pass

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

        rollout_file = self._find_rollout_file(session_id)
        if rollout_file is not None:
            try:
                mtime = rollout_file.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    raw = rollout_file.read_text(encoding="utf-8", errors="replace")
                    events.extend(parse_codex_jsonl(raw))
            except OSError:
                pass

        if self.cli_log_path and self.cli_log_path.is_file():
            try:
                mtime = self.cli_log_path.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    raw = self.cli_log_path.read_text(encoding="utf-8", errors="replace")
                    events.extend(parse_codex_jsonl(raw))
            except OSError:
                pass

        if not events:
            return False, None

        activity = extract_codex_heartbeat_from_events(
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
            rollout_file = self._find_rollout_file(session_id)
            if rollout_file is not None:
                try:
                    mtime = rollout_file.stat().st_mtime
                    if since_timestamp is None or mtime >= since_timestamp:
                        updated_at = max(updated_at, mtime)
                        raw = rollout_file.read_text(encoding="utf-8", errors="replace")
                        events.extend(parse_codex_jsonl(raw))
                except OSError:
                    pass

        if log_path and log_path.is_file():
            try:
                mtime = log_path.stat().st_mtime
                if since_timestamp is None or mtime >= since_timestamp:
                    updated_at = max(updated_at, mtime)
                    raw = log_path.read_text(encoding="utf-8", errors="replace")
                    events.extend(parse_codex_jsonl(raw))
            except OSError:
                pass

        activity = extract_codex_heartbeat_from_events(
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
        """Extract final agent response from the last `agent_message` before `turn.completed`."""
        log_path = cli_log_path or self.cli_log_path
        if log_path and log_path.is_file():
            try:
                raw = log_path.read_text(encoding="utf-8", errors="replace")
                events = parse_codex_jsonl(raw)
                text = extract_codex_last_response_from_events(
                    events,
                    session_id=session_id or None,
                )
                if text:
                    return text
            except OSError:
                pass

        if session_id:
            rollout_file = self._find_rollout_file(session_id)
            if rollout_file is not None:
                try:
                    raw = rollout_file.read_text(encoding="utf-8", errors="replace")
                    events = parse_codex_jsonl(raw)
                    text = extract_codex_last_response_from_events(events, session_id=session_id)
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
        """Detect turn-scoped sandbox/approval denials since `cli_log_offset` (`B2`)."""
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
            sliced = read_codex_log_slice(log_path, cli_log_offset=cli_log_offset)
            if sliced:
                _merge(
                    parse_codex_permission_denials(
                        sliced,
                        session_id=session_id,
                        turn_start_time=turn_start_time,
                    )
                )

        if session_id:
            rollout_file = self._find_rollout_file(session_id)
            if rollout_file is not None:
                try:
                    mtime = rollout_file.stat().st_mtime
                    if turn_start_time is None or mtime >= turn_start_time:
                        raw = rollout_file.read_text(encoding="utf-8", errors="replace")
                        _merge(
                            parse_codex_permission_denials(
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
        """Update `<repo_root>/.codex/config.toml` and add `--add-dir` / sandbox flags."""
        ok, added_dir = whitelist_codex_denial_in_config(denial, repo_root)
        if added_dir and added_dir not in self._add_dirs:
            self._add_dirs.append(added_dir)
        if not self.sandbox_mode:
            self.sandbox_mode = "workspace-write"
        return ok

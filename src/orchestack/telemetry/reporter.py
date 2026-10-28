# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Multi-agent session telemetry extraction and reporting across Antigravity, Claude, and Codex."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestack.agents.claude_code import DEFAULT_CLAUDE_PROJECTS_DIR, parse_claude_stream_jsonl
from orchestack.agents.codex import (
    DEFAULT_CODEX_SESSIONS_DIR,
    extract_thread_id_from_rollout_path,
    parse_codex_jsonl,
)

IDE_BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
CLI_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"
IDE_SUMMARIES_DB = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
CLI_SUMMARIES_DB = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
IDE_STATE_VSCDB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Antigravity"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)

# Default pricing per 1M tokens (USD)
INPUT_COST_PER_MILLION = 1.25
CACHED_INPUT_COST_PER_MILLION = 0.3125
OUTPUT_COST_PER_MILLION = 5.00

CLAUDE_INPUT_COST_PER_MILLION = 3.00
CLAUDE_CACHED_COST_PER_MILLION = 0.30
CLAUDE_OUTPUT_COST_PER_MILLION = 15.00

CODEX_INPUT_COST_PER_MILLION = 2.00
CODEX_CACHED_COST_PER_MILLION = 0.50
CODEX_OUTPUT_COST_PER_MILLION = 8.00

# Known UUID mapping for foundational pair-programming sessions (0001-0006)
FOUNDATIONAL_SESSIONS: dict[str, str] = {
    "99c957ab-37b8-408c-9983-01a771c34c8f": "0001",
    "4dbfff7c-1f1f-4f27-9c28-c15b8f88fd3d": "0002",
    "ca2a37ff-6bc0-42b2-8727-5d6f2a8476ff": "0003",
    "c38d200c-d369-4aa1-875c-08b23b750ed3": "0004",
    "3487dfb2-2e6e-4302-aefa-68d618429292": "0005",
    "65e9c037-daad-4db6-a82b-5ed0157db297": "0006",
}


@dataclass
class SessionTelemetry:
    """Normalized telemetry record for a single coding agent session."""

    session_id: str
    backend: str = "agy"  # "agy" | "claude" | "codex"
    model: str = ""
    prompt_num: str | None = None
    prompt_slug: str = ""
    total_steps: int = 0
    num_turns: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_seconds: float = 0.0
    start_time: str | None = None
    end_time: str | None = None
    tool_calls_breakdown: dict[str, int] = field(default_factory=dict)
    permission_denials_count: int = 0
    dod_auto_heal_count: int = 0
    mtime: float = 0.0
    title: str = ""
    source_path: str = ""

    def __post_init__(self) -> None:
        if self.total_tokens == 0 and (
            self.input_tokens or self.cached_input_tokens or self.output_tokens
        ):
            self.total_tokens = self.input_tokens + self.cached_input_tokens + self.output_tokens
        if self.num_turns == 0 and self.total_steps > 0:
            self.num_turns = self.total_steps

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary compatible with both v2 telemetry and legacy scripts."""
        return {
            "session_id": self.session_id,
            "conv_id": self.session_id,
            "backend": self.backend,
            "model": self.model,
            "prompt_num": self.prompt_num,
            "prompt_slug": self.prompt_slug,
            "total_steps": self.total_steps,
            "num_turns": self.num_turns,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_tokens_estimate": self.input_tokens + self.cached_input_tokens,
            "output_tokens_estimate": self.output_tokens,
            "total_tokens_estimate": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "duration_seconds": round(self.duration_seconds, 1),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "tool_calls_breakdown": dict(self.tool_calls_breakdown),
            "permission_denials_count": self.permission_denials_count,
            "dod_auto_heal_count": self.dod_auto_heal_count,
            "mtime": self.mtime,
            "title": self.title,
            "source_path": self.source_path,
        }


def build_canonical_slug_map(
    repo_root: Path | None = None,
    *,
    implemented_dir: Path | None = None,
    queued_dir: Path | None = None,
) -> dict[str, str]:
    """Return mapping of 4-digit prompt numbers (`'0001'`) to prompt slug stems."""
    root = (repo_root or Path.cwd()).resolve()
    impl_dir = implemented_dir if implemented_dir is not None else root / "prompts" / "implemented"
    q_dir = queued_dir if queued_dir is not None else root / "prompts" / "queued"

    slug_map: dict[str, str] = {}
    for p_dir in (impl_dir, q_dir):
        if p_dir.is_dir():
            for f in sorted(p_dir.glob("*.md")):
                m = re.match(r"^(\d{4})-(.*)", f.stem)
                if m:
                    slug_map[m.group(1)] = f.stem
    return slug_map


def infer_prompt_num(
    combined_user: str,
    *,
    combined_sample: str = "",
    conv_id: str = "",
    title: str = "",
    source_name: str = "",
    slug_map: Mapping[str, str] | None = None,
) -> str | None:
    """Infer a 4-digit prompt number from user messages, sample text, title, or filename."""
    # 1. Known foundational pair-programming sessions
    if conv_id and conv_id in FOUNDATIONAL_SESSIONS:
        return FOUNDATIONAL_SESSIONS[conv_id]

    # 2. Explicit daemon log filename (e.g. session-0006-slug.log)
    if source_name:
        m_src = re.search(r"(?:session-|prompt-)(\d{4})-", source_name)
        if m_src:
            return m_src.group(1)

    # 3. Explicit daemon prompt kickoff in user messages
    m_p = re.search(
        r"(?:Orchestack|BlueTeamHarness)\s*[-—]+\s*Prompt\s+(\d{4})",
        combined_user,
        re.IGNORECASE,
    )
    if m_p:
        return m_p.group(1)

    # 4. Explicit prompt file path in user messages
    m_q = re.search(r"prompts/(?:queued|implemented)/(\d{4})-", combined_user)
    if m_q:
        return m_q.group(1)

    # 5. Explicit branch in user messages or thinking
    combined_all = f"{combined_user} {combined_sample}"
    m_b = re.search(
        r"branch\s+['\"]?(?:gemini|orchestack|prompt|claude|codex|agy)/(\d{4})-",
        combined_all,
        re.IGNORECASE,
    )
    if m_b:
        return m_b.group(1)

    # 6. Fallback: prompt file in initial text sample
    m_f = re.search(r"\b(\d{4})-[\w-]+\.md\b", combined_all)
    if m_f and (not slug_map or m_f.group(1) in slug_map):
        return m_f.group(1)

    # 7. Fallback: DB Title if match
    if title:
        m_t = re.search(r"(?:Prompt|^#?\s*)(\d{4})\b", title, re.IGNORECASE)
        if m_t:
            cand = m_t.group(1)
            if cand != "0007" or "0007" in combined_all:
                return cand

    return None


def _compute_duration_from_timestamps(timestamps: Sequence[str]) -> float:
    """Compute elapsed seconds between first and last ISO-8601 timestamps."""
    if len(timestamps) < 2:
        return 0.0
    try:
        t0_str = timestamps[0].strip()
        t1_str = timestamps[-1].strip()
        if t0_str.endswith("Z"):
            t0_str = t0_str[:-1] + "+00:00"
        if t1_str.endswith("Z"):
            t1_str = t1_str[:-1] + "+00:00"
        t0 = datetime.datetime.fromisoformat(t0_str)
        t1 = datetime.datetime.fromisoformat(t1_str)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=datetime.UTC)
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=datetime.UTC)
        return max(0.0, (t1 - t0).total_seconds())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 1. Antigravity (`agy`) Telemetry
# ---------------------------------------------------------------------------


def discover_antigravity_sessions(
    cli_brain_dir: Path = CLI_BRAIN_DIR,
    ide_brain_dir: Path = IDE_BRAIN_DIR,
) -> dict[str, dict[str, Any]]:
    """Find all Antigravity session directories and their transcripts across CLI and IDE storage."""
    sessions: dict[str, dict[str, Any]] = {}

    for bdir in (cli_brain_dir, ide_brain_dir):
        if not bdir.is_dir():
            continue
        for t_path in bdir.glob("*/.system_generated/logs/transcript*.jsonl"):
            conv_id = t_path.parent.parent.parent.name
            if conv_id not in sessions:
                sessions[conv_id] = {"full": None, "compact": None, "mtime": 0.0}

            if t_path.name == "transcript_full.jsonl":
                sessions[conv_id]["full"] = t_path
            elif t_path.name == "transcript.jsonl":
                sessions[conv_id]["compact"] = t_path

            try:
                mtime = t_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            sessions[conv_id]["mtime"] = max(float(sessions[conv_id]["mtime"]), mtime)

    return sessions


def discover_all_sessions() -> dict[str, dict[str, Any]]:
    """Legacy alias for `discover_antigravity_sessions()`."""
    return discover_antigravity_sessions()


def load_antigravity_titles(
    db_paths: Sequence[Path] | None = None,
) -> dict[str, str]:
    """Retrieve conversation titles from summaries and `state.vscdb` SQLite DBs."""
    paths = (
        list(db_paths)
        if db_paths is not None
        else [CLI_SUMMARIES_DB, IDE_SUMMARIES_DB, IDE_STATE_VSCDB]
    )
    titles: dict[str, str] = {}
    for db_path in paths:
        if not db_path.is_file():
            continue
        try:
            with sqlite3.connect(db_path) as conn:
                tables = {
                    str(r[0])
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "conversation_summaries" in tables:
                    for row in conn.execute(
                        "SELECT conversation_id, title FROM conversation_summaries"
                    ):
                        if row[0] and row[1]:
                            titles[str(row[0])] = str(row[1])
                if "ItemTable" in tables:
                    for row in conn.execute("SELECT key, value FROM ItemTable"):
                        key_str = str(row[0] or "")
                        val_str = str(row[1] or "")
                        if not val_str.startswith("{") and not val_str.startswith("["):
                            continue
                        if "conversation" not in key_str.lower() and "title" not in val_str.lower():
                            continue
                        try:
                            parsed = json.loads(val_str)
                        except Exception:
                            continue
                        if isinstance(parsed, dict):
                            cid = parsed.get("conversation_id") or parsed.get("id")
                            title = parsed.get("title")
                            if isinstance(cid, str) and isinstance(title, str) and cid and title:
                                titles.setdefault(cid, title)
                            for k, v in parsed.items():
                                if isinstance(v, dict):
                                    sub_title = v.get("title") or v.get("summary")
                                    if isinstance(sub_title, str) and sub_title:
                                        titles.setdefault(str(k), sub_title)
                                elif isinstance(v, str) and v:
                                    titles.setdefault(str(k), v)
        except Exception:
            pass
    return titles


def load_conversation_titles() -> dict[str, str]:
    """Legacy alias for `load_antigravity_titles()`."""
    return load_antigravity_titles()


def parse_antigravity_transcript(
    conv_id: str,
    files: Mapping[str, Any],
    slug_map: Mapping[str, str],
    db_titles: Mapping[str, str],
) -> SessionTelemetry | None:
    """Parse an Antigravity transcript JSONL into a `SessionTelemetry` record."""
    target_file: Path | None = files.get("full") or files.get("compact")
    if not isinstance(target_file, Path) or not target_file.is_file():
        return None

    total_steps = 0
    input_tokens = 0
    cached_tokens = 0
    output_tokens = 0
    tool_counts: dict[str, int] = {}
    timestamps: list[str] = []
    user_prompts: list[str] = []
    text_sample: list[str] = []
    denials_count = 0
    model_name = "gemini"

    try:
        with target_file.open("r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f):
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue

                total_steps += 1
                ts = data.get("created_at")
                if ts:
                    timestamps.append(str(ts))

                if isinstance(data.get("model"), str) and data["model"].strip():
                    model_name = data["model"].strip()

                source = str(data.get("source") or "")
                step_type = str(data.get("type") or "")
                content = str(data.get("content") or "")
                thinking = str(data.get("thinking") or "")

                if source == "USER" or step_type == "USER_INPUT":
                    user_prompts.append(content)

                if idx < 30:
                    text_sample.append(f"{content} {thinking}")

                if "permission denied" in content.lower() or "auto-denied" in content.lower():
                    denials_count += 1

                usage = data.get("usage") or data.get("token_usage") or {}
                if isinstance(usage, dict) and (
                    usage.get("prompt_tokens") or usage.get("input_tokens")
                ):
                    in_t = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                    cache_t = int(
                        usage.get("cached_input_tokens")
                        or usage.get("cache_read_input_tokens")
                        or usage.get("cached_content_token_count")
                        or 0
                    )
                    out_t = int(
                        usage.get("completion_tokens")
                        or usage.get("output_tokens")
                        or usage.get("candidates_token_count")
                        or 0
                    )
                    input_tokens += in_t
                    cached_tokens += cache_t
                    output_tokens += out_t
                else:
                    if step_type in ("USER_INPUT", "SYSTEM_MESSAGE"):
                        input_tokens += max(1, len(content) // 4)
                    elif step_type == "PLANNER_RESPONSE":
                        output_tokens += max(1, (len(content) + len(thinking)) // 4)

                for tc in data.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    tname = (
                        (fn.get("name") if isinstance(fn, dict) else None)
                        or tc.get("name")
                        or "unknown_tool"
                    )
                    tool_counts[str(tname)] = tool_counts.get(str(tname), 0) + 1
                    args = (
                        (fn.get("arguments") if isinstance(fn, dict) else None)
                        or tc.get("args")
                        or ""
                    )
                    input_tokens += len(str(args)) // 4

    except Exception as e:
        print(f"Warning: error reading {target_file}: {e}", file=sys.stderr)
        return None

    title = db_titles.get(conv_id, "")
    prompt_num = infer_prompt_num(
        "\n".join(user_prompts),
        combined_sample=" ".join(text_sample),
        conv_id=conv_id,
        title=title,
        source_name=target_file.name,
        slug_map=slug_map,
    )

    duration_s = _compute_duration_from_timestamps(timestamps)
    est_cost = (
        input_tokens * INPUT_COST_PER_MILLION
        + cached_tokens * CACHED_INPUT_COST_PER_MILLION
        + output_tokens * OUTPUT_COST_PER_MILLION
    ) / 1_000_000.0

    slug = slug_map.get(prompt_num or "", f"other-{conv_id[:8]}")
    mtime_val = float(files.get("mtime") or 0.0)

    return SessionTelemetry(
        session_id=conv_id,
        backend="agy",
        model=model_name,
        prompt_num=prompt_num,
        prompt_slug=slug,
        total_steps=total_steps,
        num_turns=total_steps,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + cached_tokens + output_tokens,
        estimated_cost_usd=round(est_cost, 4),
        duration_seconds=round(duration_s, 1),
        start_time=timestamps[0] if timestamps else None,
        end_time=timestamps[-1] if timestamps else None,
        tool_calls_breakdown=tool_counts,
        permission_denials_count=denials_count,
        mtime=mtime_val,
        title=title,
        source_path=str(target_file),
    )


def parse_session_transcript(
    conv_id: str,
    files: dict[str, Any],
    slug_map: dict[str, str],
    db_titles: dict[str, str],
) -> dict[str, Any] | None:
    """Legacy wrapper returning dict representation of `parse_antigravity_transcript`."""
    telemetry = parse_antigravity_transcript(conv_id, files, slug_map, db_titles)
    return telemetry.to_dict() if telemetry is not None else None


# ---------------------------------------------------------------------------
# 2. Claude Code (`claude`) Telemetry
# ---------------------------------------------------------------------------


def parse_claude_events_telemetry(
    events: Sequence[dict[str, Any]],
    *,
    default_session_id: str = "",
    source_path: Path | None = None,
    mtime: float = 0.0,
    slug_map: Mapping[str, str] | None = None,
) -> SessionTelemetry | None:
    """Parse Claude Code stream-json or project JSONL events into `SessionTelemetry`."""
    if not events:
        return None

    session_id = default_session_id
    model = ""
    total_steps = 0
    num_turns = 0
    result_input_tokens = 0
    result_cached_tokens = 0
    result_output_tokens = 0
    has_result_usage = False
    assistant_input_tokens = 0
    assistant_cached_tokens = 0
    assistant_output_tokens = 0
    explicit_cost_usd: float | None = None
    explicit_duration_ms: float = 0.0
    tool_counts: dict[str, int] = {}
    denials_count = 0
    timestamps: list[str] = []
    user_texts: list[str] = []
    sample_texts: list[str] = []

    for idx, ev in enumerate(events):
        total_steps += 1
        ev_type = str(ev.get("type") or "")

        sid = ev.get("session_id") or ev.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            session_id = sid.strip()

        for ts_key in ("timestamp", "created_at", "createdAt"):
            ts_val = ev.get(ts_key)
            if isinstance(ts_val, str) and ts_val.strip():
                timestamps.append(ts_val.strip())
                break

        if ev_type == "system" and ev.get("subtype") == "init":
            m_val = ev.get("model")
            if isinstance(m_val, str) and m_val.strip():
                model = m_val.strip()

        elif ev_type in {"user", "human"}:
            msg = ev.get("message")
            content_str = ""
            if isinstance(msg, dict):
                raw_c = msg.get("content")
                if isinstance(raw_c, str):
                    content_str = raw_c
                elif isinstance(raw_c, list):
                    parts = [
                        str(b.get("text") or b.get("content") or "")
                        for b in raw_c
                        if isinstance(b, dict)
                    ]
                    content_str = " ".join(parts)
            elif isinstance(ev.get("content"), str):
                content_str = str(ev["content"])
            if content_str:
                user_texts.append(content_str)
                if idx < 25:
                    sample_texts.append(content_str)

        elif ev_type == "assistant":
            msg = ev.get("message")
            if isinstance(msg, dict):
                m_val = msg.get("model")
                if isinstance(m_val, str) and m_val.strip() and not model:
                    model = m_val.strip()
                msg_usage = msg.get("usage")
                if isinstance(msg_usage, dict):
                    assistant_input_tokens += int(msg_usage.get("input_tokens") or 0)
                    assistant_cached_tokens += int(
                        msg_usage.get("cache_read_input_tokens")
                        or msg_usage.get("cached_input_tokens")
                        or 0
                    )
                    assistant_output_tokens += int(msg_usage.get("output_tokens") or 0)

                content_list = msg.get("content")
                if isinstance(content_list, list):
                    for block in content_list:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_use":
                            tname = str(block.get("name") or "unknown_tool").strip()
                            tool_counts[tname] = tool_counts.get(tname, 0) + 1
                        elif btype == "text" and idx < 25:
                            sample_texts.append(str(block.get("text") or ""))

        elif ev_type == "tool_use":
            tname = str(ev.get("name") or "unknown_tool").strip()
            tool_counts[tname] = tool_counts.get(tname, 0) + 1

        elif ev_type == "result":
            m_val = ev.get("model")
            if isinstance(m_val, str) and m_val.strip() and not model:
                model = m_val.strip()

            turns_val = ev.get("num_turns")
            if isinstance(turns_val, (int, float)) and int(turns_val) > 0:
                num_turns += int(turns_val)
            else:
                num_turns += 1

            cost_val = ev.get("total_cost_usd")
            if cost_val is None:
                cost_val = ev.get("cost_usd")
            if isinstance(cost_val, (int, float)):
                explicit_cost_usd = (explicit_cost_usd or 0.0) + float(cost_val)

            dur_val = ev.get("duration_ms") or ev.get("duration_api_ms")
            if isinstance(dur_val, (int, float)) and float(dur_val) > 0:
                explicit_duration_ms += float(dur_val)

            usage = ev.get("usage")
            if isinstance(usage, dict):
                has_result_usage = True
                result_input_tokens += int(usage.get("input_tokens") or 0)
                result_cached_tokens += int(
                    usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens") or 0
                )
                result_output_tokens += int(usage.get("output_tokens") or 0)

            denials = ev.get("permission_denials")
            if isinstance(denials, list):
                denials_count += len(denials)

            res_text = ev.get("result")
            if isinstance(res_text, str) and idx < 25:
                sample_texts.append(res_text)

    if not session_id:
        session_id = source_path.stem if source_path is not None else "claude-session"

    input_tokens = result_input_tokens if has_result_usage else assistant_input_tokens
    cached_tokens = result_cached_tokens if has_result_usage else assistant_cached_tokens
    output_tokens = result_output_tokens if has_result_usage else assistant_output_tokens
    total_tokens = input_tokens + cached_tokens + output_tokens

    if explicit_duration_ms > 0:
        duration_s = explicit_duration_ms / 1000.0
    else:
        duration_s = _compute_duration_from_timestamps(timestamps)

    if explicit_cost_usd is not None:
        cost_usd = explicit_cost_usd
    else:
        cost_usd = (
            input_tokens * CLAUDE_INPUT_COST_PER_MILLION
            + cached_tokens * CLAUDE_CACHED_COST_PER_MILLION
            + output_tokens * CLAUDE_OUTPUT_COST_PER_MILLION
        ) / 1_000_000.0

    src_name = source_path.name if source_path is not None else ""
    prompt_num = infer_prompt_num(
        "\n".join(user_texts),
        combined_sample=" ".join(sample_texts),
        conv_id=session_id,
        source_name=src_name,
        slug_map=slug_map,
    )
    slug = (
        slug_map.get(prompt_num, f"prompt-{prompt_num}")
        if (prompt_num and slug_map)
        else (f"prompt-{prompt_num}" if prompt_num else f"claude-{session_id[:8]}")
    )

    return SessionTelemetry(
        session_id=session_id,
        backend="claude",
        model=model or "claude",
        prompt_num=prompt_num,
        prompt_slug=slug,
        total_steps=total_steps,
        num_turns=num_turns if num_turns > 0 else total_steps,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=round(cost_usd, 4),
        duration_seconds=round(duration_s, 1),
        start_time=timestamps[0] if timestamps else None,
        end_time=timestamps[-1] if timestamps else None,
        tool_calls_breakdown=tool_counts,
        permission_denials_count=denials_count,
        mtime=mtime,
        source_path=str(source_path) if source_path else "",
    )


def parse_claude_session_file(
    file_path: Path,
    slug_map: Mapping[str, str] | None = None,
) -> SessionTelemetry | None:
    """Parse a Claude Code `.jsonl` transcript file into `SessionTelemetry`."""
    if not file_path.is_file():
        return None
    try:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        mtime = file_path.stat().st_mtime
    except OSError:
        return None

    events = parse_claude_stream_jsonl(raw_text)
    if not events:
        return None

    # Verify this looks like a Claude event stream
    has_claude_event = any(
        ev.get("type") in {"system", "assistant", "user", "human", "result", "tool_use"}
        for ev in events
    )
    if not has_claude_event:
        return None

    return parse_claude_events_telemetry(
        events,
        default_session_id=file_path.stem,
        source_path=file_path,
        mtime=mtime,
        slug_map=slug_map,
    )


def discover_claude_sessions(
    claude_projects_dir: Path = DEFAULT_CLAUDE_PROJECTS_DIR,
    slug_map: Mapping[str, str] | None = None,
) -> list[SessionTelemetry]:
    """Discover and parse all Claude Code `.jsonl` transcripts under `~/.claude/projects`."""
    results: list[SessionTelemetry] = []
    if not claude_projects_dir.is_dir():
        return results

    for jsonl_path in sorted(claude_projects_dir.glob("**/*.jsonl")):
        telemetry = parse_claude_session_file(jsonl_path, slug_map=slug_map)
        if telemetry is not None:
            results.append(telemetry)
    return results


# ---------------------------------------------------------------------------
# 3. OpenAI Codex (`codex`) Telemetry
# ---------------------------------------------------------------------------


def parse_codex_events_telemetry(
    events: Sequence[dict[str, Any]],
    *,
    default_session_id: str = "",
    source_path: Path | None = None,
    mtime: float = 0.0,
    slug_map: Mapping[str, str] | None = None,
) -> SessionTelemetry | None:
    """Parse Codex JSONL events (`thread.started`, `turn.completed`) into `SessionTelemetry`."""
    if not events:
        return None

    session_id = default_session_id
    model = ""
    total_steps = 0
    num_turns = 0
    input_tokens = 0
    cached_tokens = 0
    output_tokens = 0
    explicit_cost_usd: float | None = None
    explicit_duration_ms: float = 0.0
    tool_counts: dict[str, int] = {}
    denials_count = 0
    timestamps: list[str] = []
    user_texts: list[str] = []
    sample_texts: list[str] = []

    for idx, ev in enumerate(events):
        total_steps += 1
        ev_type = str(ev.get("type") or "")

        for ts_key in ("timestamp", "created_at", "createdAt"):
            ts_val = ev.get(ts_key)
            if isinstance(ts_val, str) and ts_val.strip():
                timestamps.append(ts_val.strip())
                break

        if ev_type == "thread.started":
            tid = ev.get("thread_id") or ev.get("session_id")
            if isinstance(tid, str) and tid.strip():
                session_id = tid.strip()
            m_val = ev.get("model")
            if isinstance(m_val, str) and m_val.strip():
                model = m_val.strip()

        elif ev_type == "session_meta":
            payload = ev.get("payload")
            if isinstance(payload, dict):
                tid = payload.get("id") or payload.get("thread_id") or payload.get("session_id")
                if isinstance(tid, str) and tid.strip():
                    session_id = tid.strip()
                m_val = payload.get("model")
                if isinstance(m_val, str) and m_val.strip() and not model:
                    model = m_val.strip()

        elif ev_type in {"item.completed", "item.started"}:
            raw_item = ev.get("item")
            item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else ev
            item_type = str(item.get("type") or item.get("item_type") or "").strip()

            if ev_type == "item.completed":
                if item_type == "command_execution":
                    tool_counts["command_execution"] = tool_counts.get("command_execution", 0) + 1
                    status_str = str(item.get("status") or "").lower()
                    output_str = str(
                        item.get("aggregated_output")
                        or item.get("output")
                        or item.get("error")
                        or ""
                    ).lower()
                    if (
                        "denied" in status_str
                        or "sandbox_error" in output_str
                        or "operation not permitted" in output_str
                        or "permission denied" in output_str
                    ):
                        denials_count += 1
                elif item_type == "file_change":
                    tool_counts["file_change"] = tool_counts.get("file_change", 0) + 1
                elif item_type in {"mcp_tool_call", "function_call", "tool_call"}:
                    tname = str(item.get("name") or item.get("tool_name") or item_type).strip()
                    tool_counts[tname] = tool_counts.get(tname, 0) + 1

            if item_type in {"user_message", "input_text", "prompt"}:
                txt = str(item.get("text") or item.get("content") or item.get("message") or "")
                if txt:
                    user_texts.append(txt)
                    if idx < 25:
                        sample_texts.append(txt)
            elif item_type == "agent_message" and idx < 25:
                txt = str(item.get("text") or item.get("content") or item.get("message") or "")
                if txt:
                    sample_texts.append(txt)

        elif ev_type == "turn.completed":
            num_turns += 1
            m_val = ev.get("model")
            if isinstance(m_val, str) and m_val.strip() and not model:
                model = m_val.strip()

            usage = ev.get("usage")
            if isinstance(usage, dict):
                input_tokens += int(usage.get("input_tokens") or 0)
                cached_val = usage.get("cached_input_tokens")
                if cached_val is None:
                    details = usage.get("input_tokens_details")
                    if isinstance(details, dict):
                        cached_val = details.get("cached_tokens")
                cached_tokens += int(cached_val or 0)
                output_tokens += int(usage.get("output_tokens") or 0)

            cost_val = ev.get("total_cost_usd") or ev.get("cost_usd")
            if isinstance(cost_val, (int, float)):
                explicit_cost_usd = (explicit_cost_usd or 0.0) + float(cost_val)

            dur_val = ev.get("duration_ms")
            if isinstance(dur_val, (int, float)) and float(dur_val) > 0:
                explicit_duration_ms += float(dur_val)

        elif ev_type in {"user", "prompt"}:
            txt = str(ev.get("text") or ev.get("prompt") or ev.get("content") or "")
            if txt:
                user_texts.append(txt)

    if not session_id:
        if source_path is not None:
            session_id = extract_thread_id_from_rollout_path(source_path)
        else:
            session_id = "codex-session"

    total_tokens = input_tokens + cached_tokens + output_tokens
    if explicit_duration_ms > 0:
        duration_s = explicit_duration_ms / 1000.0
    else:
        duration_s = _compute_duration_from_timestamps(timestamps)

    if explicit_cost_usd is not None:
        cost_usd = explicit_cost_usd
    else:
        cost_usd = (
            input_tokens * CODEX_INPUT_COST_PER_MILLION
            + cached_tokens * CODEX_CACHED_COST_PER_MILLION
            + output_tokens * CODEX_OUTPUT_COST_PER_MILLION
        ) / 1_000_000.0

    src_name = source_path.name if source_path is not None else ""
    prompt_num = infer_prompt_num(
        "\n".join(user_texts),
        combined_sample=" ".join(sample_texts),
        conv_id=session_id,
        source_name=src_name,
        slug_map=slug_map,
    )
    slug = (
        slug_map.get(prompt_num, f"prompt-{prompt_num}")
        if (prompt_num and slug_map)
        else (f"prompt-{prompt_num}" if prompt_num else f"codex-{session_id[:8]}")
    )

    return SessionTelemetry(
        session_id=session_id,
        backend="codex",
        model=model or "codex",
        prompt_num=prompt_num,
        prompt_slug=slug,
        total_steps=total_steps,
        num_turns=num_turns if num_turns > 0 else total_steps,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=round(cost_usd, 4),
        duration_seconds=round(duration_s, 1),
        start_time=timestamps[0] if timestamps else None,
        end_time=timestamps[-1] if timestamps else None,
        tool_calls_breakdown=tool_counts,
        permission_denials_count=denials_count,
        mtime=mtime,
        source_path=str(source_path) if source_path else "",
    )


def parse_codex_session_file(
    file_path: Path,
    slug_map: Mapping[str, str] | None = None,
) -> SessionTelemetry | None:
    """Parse a Codex `rollout-*.jsonl` or session `.jsonl` file into `SessionTelemetry`."""
    if not file_path.is_file():
        return None
    try:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        mtime = file_path.stat().st_mtime
    except OSError:
        return None

    events = parse_codex_jsonl(raw_text)
    if not events:
        return None

    has_codex_event = any(
        str(ev.get("type") or "")
        in {
            "thread.started",
            "session_meta",
            "turn.started",
            "turn.completed",
            "item.started",
            "item.completed",
        }
        for ev in events
    )
    if not has_codex_event:
        return None

    default_tid = extract_thread_id_from_rollout_path(file_path)
    return parse_codex_events_telemetry(
        events,
        default_session_id=default_tid,
        source_path=file_path,
        mtime=mtime,
        slug_map=slug_map,
    )


def discover_codex_sessions(
    codex_sessions_dir: Path = DEFAULT_CODEX_SESSIONS_DIR,
    slug_map: Mapping[str, str] | None = None,
) -> list[SessionTelemetry]:
    """Discover and parse all Codex `rollout-*.jsonl` files under `~/.codex/sessions`."""
    results: list[SessionTelemetry] = []
    if not codex_sessions_dir.is_dir():
        return results

    seen_paths: set[Path] = set()
    for pattern in ("**/rollout-*.jsonl", "**/*.jsonl"):
        for jsonl_path in sorted(codex_sessions_dir.glob(pattern)):
            resolved = jsonl_path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            telemetry = parse_codex_session_file(jsonl_path, slug_map=slug_map)
            if telemetry is not None:
                results.append(telemetry)
    return results


# ---------------------------------------------------------------------------
# 4. Daemon Logs (`.daemon/logs/` & `logs/`) Discovery
# ---------------------------------------------------------------------------


def parse_daemon_log_file(
    file_path: Path,
    slug_map: Mapping[str, str] | None = None,
) -> list[SessionTelemetry]:
    """Parse `.daemon/logs/` or `logs/` JSONL files for Claude, Codex, or metrics records."""
    if not file_path.is_file():
        return []

    if file_path.name == "daemon_metrics.jsonl":
        records: list[SessionTelemetry] = []
        try:
            raw_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            mtime = file_path.stat().st_mtime
        except OSError:
            return []
        for idx, line in enumerate(raw_lines):
            if not line.strip().startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or "prompt_slug" not in rec:
                continue
            slug = str(rec.get("prompt_slug") or "")
            m_num = re.match(r"^(\d{4})", slug)
            pnum = m_num.group(1) if m_num else None
            backend = str(rec.get("backend") or rec.get("agent") or "agy")
            sid = str(rec.get("session_id") or rec.get("conv_id") or f"metrics-{slug}-{idx}")
            in_tok = int(rec.get("input_tokens") or rec.get("input_tokens_estimate") or 0)
            cache_tok = int(rec.get("cached_input_tokens") or 0)
            out_tok = int(rec.get("output_tokens") or rec.get("output_tokens_estimate") or 0)
            tot_tok = int(
                rec.get("total_tokens")
                or rec.get("total_tokens_estimate")
                or (in_tok + cache_tok + out_tok)
            )
            tools = rec.get("tool_calls_breakdown")
            records.append(
                SessionTelemetry(
                    session_id=sid,
                    backend=backend,
                    model=str(rec.get("model") or backend),
                    prompt_num=pnum,
                    prompt_slug=slug,
                    total_steps=int(rec.get("total_steps") or 0),
                    num_turns=int(rec.get("num_turns") or rec.get("total_steps") or 0),
                    input_tokens=in_tok,
                    cached_input_tokens=cache_tok,
                    output_tokens=out_tok,
                    total_tokens=tot_tok,
                    estimated_cost_usd=float(rec.get("estimated_cost_usd") or 0.0),
                    duration_seconds=float(rec.get("session_duration_seconds") or 0.0),
                    end_time=str(rec["timestamp"]) if rec.get("timestamp") else None,
                    tool_calls_breakdown=dict(tools) if isinstance(tools, dict) else {},
                    dod_auto_heal_count=int(rec.get("ci_heal_attempts") or 0),
                    mtime=mtime,
                    source_path=str(file_path),
                )
            )
        return records

    # Try Codex JSONL first, then Claude stream-json
    codex_telemetry = parse_codex_session_file(file_path, slug_map=slug_map)
    if codex_telemetry is not None:
        return [codex_telemetry]

    claude_telemetry = parse_claude_session_file(file_path, slug_map=slug_map)
    if claude_telemetry is not None:
        return [claude_telemetry]

    return []


# ---------------------------------------------------------------------------
# 5. Unified Collection & Formatting
# ---------------------------------------------------------------------------


def collect_multi_agent_telemetry(
    repo_root: Path | None = None,
    *,
    agy_cli_brain_dir: Path | None = None,
    agy_ide_brain_dir: Path | None = None,
    agy_summaries_dbs: Sequence[Path] | None = None,
    claude_projects_dir: Path | None = None,
    codex_sessions_dir: Path | None = None,
    daemon_logs_dirs: Sequence[Path] | None = None,
) -> tuple[list[SessionTelemetry], dict[str, list[SessionTelemetry]]]:
    """Collect unified telemetry across Antigravity, Claude Code, Codex, and `.daemon/logs/`."""
    root = (repo_root or Path.cwd()).resolve()
    slug_map = build_canonical_slug_map(root)

    cli_bdir = agy_cli_brain_dir if agy_cli_brain_dir is not None else CLI_BRAIN_DIR
    ide_bdir = agy_ide_brain_dir if agy_ide_brain_dir is not None else IDE_BRAIN_DIR
    c_dir = claude_projects_dir if claude_projects_dir is not None else DEFAULT_CLAUDE_PROJECTS_DIR
    x_dir = codex_sessions_dir if codex_sessions_dir is not None else DEFAULT_CODEX_SESSIONS_DIR
    log_dirs = (
        list(daemon_logs_dirs)
        if daemon_logs_dirs is not None
        else [root / ".daemon" / "logs", root / ".daemon", root / "logs"]
    )

    by_key: dict[tuple[str, str], SessionTelemetry] = {}

    # 1. Antigravity sessions
    db_titles = load_antigravity_titles(agy_summaries_dbs)
    raw_agy = discover_antigravity_sessions(cli_brain_dir=cli_bdir, ide_brain_dir=ide_bdir)
    for conv_id, files in raw_agy.items():
        parsed = parse_antigravity_transcript(conv_id, files, slug_map, db_titles)
        if parsed is not None:
            by_key[("agy", parsed.session_id)] = parsed

    # 2. Claude Code sessions
    for claude_sess in discover_claude_sessions(c_dir, slug_map=slug_map):
        by_key[("claude", claude_sess.session_id)] = claude_sess

    # 3. Codex sessions
    for codex_sess in discover_codex_sessions(x_dir, slug_map=slug_map):
        by_key[("codex", codex_sess.session_id)] = codex_sess

    # 4. Daemon logs (.daemon/logs/ and logs/)
    seen_log_files: set[Path] = set()
    for ldir in log_dirs:
        if not ldir.is_dir():
            continue
        for candidate in sorted(ldir.glob("*")):
            if not candidate.is_file():
                continue
            if candidate.suffix not in {".jsonl", ".log"}:
                continue
            resolved_cand = candidate.resolve()
            if resolved_cand in seen_log_files:
                continue
            seen_log_files.add(resolved_cand)
            for item in parse_daemon_log_file(candidate, slug_map=slug_map):
                key = (item.backend, item.session_id)
                if key not in by_key:
                    by_key[key] = item
                else:
                    existing = by_key[key]
                    if not existing.prompt_num and item.prompt_num:
                        existing.prompt_num = item.prompt_num
                        existing.prompt_slug = item.prompt_slug

    all_sessions = sorted(by_key.values(), key=lambda s: (s.mtime, s.session_id))
    by_prompt: dict[str, list[SessionTelemetry]] = {}
    for sess in all_sessions:
        if sess.prompt_num:
            by_prompt.setdefault(sess.prompt_num, []).append(sess)

    return all_sessions, by_prompt


def collect_telemetry_dataset(
    repo_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Legacy wrapper returning dict representations of sessions and prompt groupings."""
    all_sessions, by_prompt = collect_multi_agent_telemetry(repo_root=repo_root)
    all_dicts = [s.to_dict() for s in all_sessions]
    by_prompt_dicts = {
        pnum: [s.to_dict() for s in sess_list] for pnum, sess_list in by_prompt.items()
    }
    return all_dicts, by_prompt_dicts


def format_telemetry_json(
    all_sessions: Sequence[SessionTelemetry],
    by_prompt: Mapping[str, Sequence[SessionTelemetry]],
    slug_map: Mapping[str, str],
) -> str:
    """Format multi-agent telemetry as a structured JSON report."""
    by_backend: dict[str, dict[str, Any]] = {}
    for s in all_sessions:
        b_stats = by_backend.setdefault(
            s.backend,
            {
                "sessions_count": 0,
                "total_steps": 0,
                "num_turns": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "duration_seconds": 0.0,
            },
        )
        b_stats["sessions_count"] += 1
        b_stats["total_steps"] += s.total_steps
        b_stats["num_turns"] += s.num_turns
        b_stats["input_tokens"] += s.input_tokens
        b_stats["cached_input_tokens"] += s.cached_input_tokens
        b_stats["output_tokens"] += s.output_tokens
        b_stats["total_tokens"] += s.total_tokens
        b_stats["estimated_cost_usd"] = round(
            float(b_stats["estimated_cost_usd"]) + s.estimated_cost_usd, 4
        )
        b_stats["duration_seconds"] = round(
            float(b_stats["duration_seconds"]) + s.duration_seconds, 1
        )

    summary_by_prompt: dict[str, Any] = {}
    for pnum, sessions in sorted(by_prompt.items()):
        backends = sorted({s.backend for s in sessions})
        in_tok = sum(s.input_tokens for s in sessions)
        cache_tok = sum(s.cached_input_tokens for s in sessions)
        out_tok = sum(s.output_tokens for s in sessions)
        tot_tok = sum(s.total_tokens for s in sessions)
        summary_by_prompt[pnum] = {
            "slug": slug_map.get(pnum, f"prompt-{pnum}"),
            "backends": backends,
            "sessions_count": len(sessions),
            "total_steps": sum(s.total_steps for s in sessions),
            "num_turns": sum(s.num_turns for s in sessions),
            "input_tokens": in_tok,
            "cached_input_tokens": cache_tok,
            "output_tokens": out_tok,
            "total_tokens": tot_tok,
            "input_tokens_estimate": in_tok + cache_tok,
            "output_tokens_estimate": out_tok,
            "total_tokens_estimate": tot_tok,
            "estimated_cost_usd": round(sum(s.estimated_cost_usd for s in sessions), 4),
            "duration_seconds": round(sum(s.duration_seconds for s in sessions), 1),
        }

    payload = {
        "summary": {
            "total_sessions": len(all_sessions),
            "total_steps": sum(s.total_steps for s in all_sessions),
            "num_turns": sum(s.num_turns for s in all_sessions),
            "input_tokens": sum(s.input_tokens for s in all_sessions),
            "cached_input_tokens": sum(s.cached_input_tokens for s in all_sessions),
            "output_tokens": sum(s.output_tokens for s in all_sessions),
            "total_tokens": sum(s.total_tokens for s in all_sessions),
            "estimated_cost_usd": round(sum(s.estimated_cost_usd for s in all_sessions), 4),
            "duration_seconds": round(sum(s.duration_seconds for s in all_sessions), 1),
            "by_backend": by_backend,
        },
        "summary_by_prompt": summary_by_prompt,
        "sessions": [s.to_dict() for s in all_sessions],
    }
    return json.dumps(payload, indent=2)


def format_telemetry_markdown(
    all_sessions: Sequence[SessionTelemetry],
    by_prompt: Mapping[str, Sequence[SessionTelemetry]],
    slug_map: Mapping[str, str],
) -> str:
    """Generate a unified GitHub-flavored Markdown telemetry report across all agent backends."""
    lines = [
        "# Orchestack Multi-Agent Telemetry Report",
        "",
        "## Summary by Prompt",
        "",
        (
            "| Prompt | Name / Slug | Backends | Sessions | Steps | Input Tokens "
            "| Cached Tokens | Output Tokens | Total Tokens | Cost (USD) | Duration |"
        ),
        "| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    tot_steps = 0
    tot_in = 0
    tot_cache = 0
    tot_out = 0
    tot_tokens = 0
    tot_cost = 0.0
    tot_dur = 0.0
    tot_prompt_sessions = 0

    all_prompt_keys = sorted(set(slug_map.keys()) | set(by_prompt.keys()))
    for pnum in all_prompt_keys:
        sessions = by_prompt.get(pnum, [])
        if not sessions:
            continue
        slug = slug_map.get(pnum, sessions[0].prompt_slug or f"prompt-{pnum}")
        backends_str = ", ".join(sorted({s.backend for s in sessions}))
        sess_count = len(sessions)
        steps = sum(s.total_steps for s in sessions)
        in_tok = sum(s.input_tokens for s in sessions)
        cache_tok = sum(s.cached_input_tokens for s in sessions)
        out_tok = sum(s.output_tokens for s in sessions)
        tokens = sum(s.total_tokens for s in sessions)
        cost = sum(s.estimated_cost_usd for s in sessions)
        dur_s = sum(s.duration_seconds for s in sessions)
        dur_str = f"{dur_s / 60.0:.1f}m" if dur_s < 3600 else f"{dur_s / 3600.0:.1f}h"

        tot_prompt_sessions += sess_count
        tot_steps += steps
        tot_in += in_tok
        tot_cache += cache_tok
        tot_out += out_tok
        tot_tokens += tokens
        tot_cost += cost
        tot_dur += dur_s

        lines.append(
            f"| `{pnum}` | `{slug}` | `{backends_str}` | {sess_count} | {steps:,} | "
            f"{in_tok:,} | {cache_tok:,} | {out_tok:,} | ~{tokens:,} | "
            f"${cost:.4f} | {dur_str} |"
        )

    tot_dur_h = tot_dur / 3600.0
    lines.append(
        f"| **Total** | **All Prompts** | **All** | **{tot_prompt_sessions}** | "
        f"**{tot_steps:,}** | **{tot_in:,}** | **{tot_cache:,}** | **{tot_out:,}** | "
        f"**~{tot_tokens:,}** | **${tot_cost:.4f}** | **{tot_dur_h:.1f}h** |"
    )

    if all_sessions:
        lines.extend(
            [
                "",
                "## Sessions by Backend",
                "",
                (
                    "| Session ID | Backend | Model | Prompt | Turns | Input "
                    "| Cached | Output | Cost (USD) | Duration |"
                ),
                "| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
            ]
        )
        for s in all_sessions:
            p_label = s.prompt_num or "unmapped"
            dur_str = f"{s.duration_seconds:.1f}s"
            lines.append(
                f"| `{s.session_id}` | `{s.backend}` | `{s.model or s.backend}` | `{p_label}` | "
                f"{s.num_turns} | {s.input_tokens:,} | {s.cached_input_tokens:,} | "
                f"{s.output_tokens:,} | ${s.estimated_cost_usd:.4f} | {dur_str} |"
            )

    return "\n".join(lines)


def export_markdown_table(
    by_prompt: dict[str, list[dict[str, Any]]],
    slug_map: dict[str, str],
) -> str:
    """Legacy markdown table exporter accepting dictionary session records."""
    lines = [
        "| Prompt | Name / Slug | Sessions | Steps | Tokens | Cost (USD) | Duration |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
    ]

    tot_steps = 0
    tot_tokens = 0
    tot_cost = 0.0
    tot_dur = 0.0

    for pnum in sorted(slug_map.keys()):
        slug = slug_map[pnum]
        sessions = by_prompt.get(pnum, [])
        if not sessions:
            continue
        sess_count = len(sessions)
        steps = sum(int(s.get("total_steps", 0)) for s in sessions)
        tokens = sum(
            int(s.get("total_tokens_estimate") or s.get("total_tokens") or 0) for s in sessions
        )
        cost = sum(float(s.get("estimated_cost_usd", 0.0)) for s in sessions)
        dur_s = sum(float(s.get("duration_seconds", 0.0)) for s in sessions)
        dur_str = f"{dur_s / 60.0:.1f}m" if dur_s < 3600 else f"{dur_s / 3600.0:.1f}h"

        tot_steps += steps
        tot_tokens += tokens
        tot_cost += cost
        tot_dur += dur_s

        lines.append(
            f"| `{pnum}` | `{slug}` | {sess_count} | {steps:,} | ~{tokens:,} | "
            f"${cost:.4f} | {dur_str} |"
        )

    tot_dur_h = tot_dur / 3600.0
    lines.append(
        f"| **Total** | **All Prompts** | **{sum(len(v) for v in by_prompt.values())}** | "
        f"**{tot_steps:,}** | **~{tot_tokens:,}** | **${tot_cost:.4f}** | **{tot_dur_h:.1f}h** |"
    )
    return "\n".join(lines)


def backfill_daemon_metrics_file(
    by_prompt: dict[str, list[dict[str, Any]]],
    slug_map: dict[str, str],
    *,
    daemon_metrics_file: Path | None = None,
) -> int:
    """Backfill missing completed prompt events into `logs/daemon_metrics.jsonl`."""
    metrics_path = (
        daemon_metrics_file
        if daemon_metrics_file is not None
        else Path.cwd() / "logs" / "daemon_metrics.jsonl"
    )
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    existing_slugs: set[str] = set()
    if metrics_path.is_file():
        try:
            with metrics_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            d = json.loads(line)
                            if isinstance(d, dict) and d.get("prompt_slug"):
                                existing_slugs.add(str(d["prompt_slug"]))
                        except Exception:
                            pass
        except Exception as e:
            print(f"Warning reading {metrics_path}: {e}", file=sys.stderr)

    backfilled_count = 0
    with metrics_path.open("a", encoding="utf-8") as f:
        for pnum in sorted(slug_map.keys()):
            slug = slug_map[pnum]
            if slug in existing_slugs:
                continue

            sessions = by_prompt.get(pnum, [])
            if not sessions:
                continue

            total_steps = sum(int(s.get("total_steps", 0)) for s in sessions)
            in_tokens = sum(
                int(s.get("input_tokens_estimate") or s.get("input_tokens") or 0) for s in sessions
            )
            out_tokens = sum(
                int(s.get("output_tokens_estimate") or s.get("output_tokens") or 0)
                for s in sessions
            )
            total_tokens = sum(
                int(s.get("total_tokens_estimate") or s.get("total_tokens") or 0) for s in sessions
            )
            cost = sum(float(s.get("estimated_cost_usd", 0.0)) for s in sessions)
            dur_s = sum(float(s.get("duration_seconds", 0.0)) for s in sessions)

            all_tools: dict[str, int] = {}
            for s in sessions:
                tools = s.get("tool_calls_breakdown")
                if isinstance(tools, dict):
                    for tname, cnt in tools.items():
                        all_tools[str(tname)] = all_tools.get(str(tname), 0) + int(cnt)

            rec = {
                "prompt_name": f"{slug}.md",
                "prompt_slug": slug,
                "branch": f"gemini/{slug}",
                "pr_number": None,
                "status": "completed",
                "session_duration_seconds": dur_s,
                "ci_wait_seconds": 0.0,
                "ci_heal_attempts": 0,
                "review_feedback_attempts": max(0, len(sessions) - 1),
                "total_steps": total_steps,
                "input_tokens_estimate": in_tokens,
                "output_tokens_estimate": out_tokens,
                "total_tokens_estimate": total_tokens,
                "estimated_cost_usd": round(cost, 4),
                "tool_calls_breakdown": all_tools,
                "timestamp": sessions[-1].get("end_time")
                or datetime.datetime.now(datetime.UTC).isoformat(),
            }
            f.write(json.dumps(rec) + "\n")
            backfilled_count += 1

    return backfilled_count

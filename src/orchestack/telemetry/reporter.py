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

# Default pricing per 1M tokens (USD) across unified pricing buckets:
# - input_tokens: uncached input (1.0x base input rate)
# - cache_write_tokens: cache creation / write (1.25x on Claude 5-min TTL; 0/1.0x on OpenAI/Gemini)
# - cache_read_tokens: cache hit / read (0.10x on Claude; 0.25x-0.50x on OpenAI/Gemini)
# - output_tokens: output + thinking/reasoning tokens
INPUT_COST_PER_MILLION = 1.25
CACHE_WRITE_COST_PER_MILLION = 1.25
CACHE_READ_COST_PER_MILLION = 0.3125
CACHED_INPUT_COST_PER_MILLION = CACHE_READ_COST_PER_MILLION
OUTPUT_COST_PER_MILLION = 5.00

CLAUDE_INPUT_COST_PER_MILLION = 3.00
CLAUDE_CACHE_WRITE_COST_PER_MILLION = 3.75
CLAUDE_CACHE_READ_COST_PER_MILLION = 0.30
CLAUDE_CACHED_COST_PER_MILLION = CLAUDE_CACHE_READ_COST_PER_MILLION
CLAUDE_OUTPUT_COST_PER_MILLION = 15.00

CODEX_INPUT_COST_PER_MILLION = 2.00
CODEX_CACHE_WRITE_COST_PER_MILLION = 2.00
CODEX_CACHE_READ_COST_PER_MILLION = 0.50
CODEX_CACHED_COST_PER_MILLION = CODEX_CACHE_READ_COST_PER_MILLION
CODEX_OUTPUT_COST_PER_MILLION = 8.00


@dataclass(frozen=True)
class ModelPricing:
    """Per-1M-token USD rates across the four unified pricing buckets."""

    input_per_million: float
    cache_write_per_million: float
    cache_read_per_million: float
    output_per_million: float


MODEL_PRICING_TABLE: dict[str, ModelPricing] = {
    # Google Gemini (Antigravity)
    "gemini-2.5-pro": ModelPricing(1.25, 1.25, 0.3125, 5.00),
    "gemini-3.1-pro": ModelPricing(1.25, 1.25, 0.3125, 5.00),
    "gemini-3-pro": ModelPricing(1.25, 1.25, 0.3125, 5.00),
    "gemini-2.5-flash-lite": ModelPricing(0.10, 0.10, 0.025, 0.40),
    "gemini-2.5-flash": ModelPricing(0.30, 0.30, 0.075, 2.50),
    "gemini-2.0-flash": ModelPricing(0.30, 0.30, 0.075, 2.50),
    "gemini": ModelPricing(
        INPUT_COST_PER_MILLION,
        CACHE_WRITE_COST_PER_MILLION,
        CACHE_READ_COST_PER_MILLION,
        OUTPUT_COST_PER_MILLION,
    ),
    # Anthropic Claude
    "claude-opus-4-6": ModelPricing(5.00, 6.25, 0.50, 25.00),
    "claude-opus-4-5": ModelPricing(5.00, 6.25, 0.50, 25.00),
    "claude-4-6-opus": ModelPricing(5.00, 6.25, 0.50, 25.00),
    "claude-4-5-opus": ModelPricing(5.00, 6.25, 0.50, 25.00),
    "claude-opus-4": ModelPricing(15.00, 18.75, 1.50, 75.00),
    "claude-3-opus": ModelPricing(15.00, 18.75, 1.50, 75.00),
    "claude-haiku-4-5": ModelPricing(1.00, 1.25, 0.10, 5.00),
    "claude-4-5-haiku": ModelPricing(1.00, 1.25, 0.10, 5.00),
    "claude-3-5-haiku": ModelPricing(0.80, 1.00, 0.08, 4.00),
    "claude-3-haiku": ModelPricing(0.80, 1.00, 0.08, 4.00),
    "claude-sonnet-4": ModelPricing(3.00, 3.75, 0.30, 15.00),
    "claude-3-7-sonnet": ModelPricing(3.00, 3.75, 0.30, 15.00),
    "claude-3-5-sonnet": ModelPricing(3.00, 3.75, 0.30, 15.00),
    "claude": ModelPricing(
        CLAUDE_INPUT_COST_PER_MILLION,
        CLAUDE_CACHE_WRITE_COST_PER_MILLION,
        CLAUDE_CACHE_READ_COST_PER_MILLION,
        CLAUDE_OUTPUT_COST_PER_MILLION,
    ),
    # OpenAI Codex
    "gpt-5": ModelPricing(1.25, 1.25, 0.125, 10.00),
    "codex-1": ModelPricing(1.25, 1.25, 0.125, 10.00),
    "codex-mini": ModelPricing(1.10, 1.10, 0.275, 4.40),
    "o4-mini": ModelPricing(1.10, 1.10, 0.275, 4.40),
    "o3-mini": ModelPricing(1.10, 1.10, 0.275, 4.40),
    "gpt-4.1-mini": ModelPricing(0.40, 0.40, 0.10, 1.60),
    "gpt-4.1-nano": ModelPricing(0.10, 0.10, 0.025, 0.40),
    "gpt-4o": ModelPricing(2.50, 2.50, 1.25, 10.00),
    "gpt-4.1": ModelPricing(2.00, 2.00, 0.50, 8.00),
    "o3": ModelPricing(2.00, 2.00, 0.50, 8.00),
    "codex": ModelPricing(
        CODEX_INPUT_COST_PER_MILLION,
        CODEX_CACHE_WRITE_COST_PER_MILLION,
        CODEX_CACHE_READ_COST_PER_MILLION,
        CODEX_OUTPUT_COST_PER_MILLION,
    ),
}


def get_model_pricing(model: str = "", backend: str = "agy") -> ModelPricing:
    """Resolve per-1M-token pricing from model identifier and agent backend fallback."""
    norm_model = model.strip().lower()
    if norm_model in MODEL_PRICING_TABLE:
        return MODEL_PRICING_TABLE[norm_model]

    for key in sorted(MODEL_PRICING_TABLE.keys(), key=len, reverse=True):
        if key in norm_model:
            return MODEL_PRICING_TABLE[key]

    norm_backend = backend.strip().lower()
    if norm_backend == "claude":
        return MODEL_PRICING_TABLE["claude"]
    if norm_backend == "codex":
        return MODEL_PRICING_TABLE["codex"]
    return MODEL_PRICING_TABLE["gemini"]


def calculate_token_cost(
    model: str = "",
    *,
    backend: str = "agy",
    input_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Calculate USD cost from the 4 unified token buckets and model/backend pricing."""
    pricing = get_model_pricing(model=model, backend=backend)
    return (
        input_tokens * pricing.input_per_million
        + cache_write_tokens * pricing.cache_write_per_million
        + cache_read_tokens * pricing.cache_read_per_million
        + output_tokens * pricing.output_per_million
    ) / 1_000_000.0


# Known UUID mapping for foundational pair-programming sessions (0001-0008)
FOUNDATIONAL_SESSIONS: dict[str, str] = {
    "548deadd-ce26-47e0-92a4-4a5004dbe9c4": "0001",
    "27b57106-6bb8-4335-8465-44f87941b143": "0002",
    "eb7433c9-a221-4cf1-947b-f484b2867504": "0003",
    "10e31f4e-14e0-4a8b-b37d-7feb9243ff02": "0004",
    "41485088-753c-4a8e-9cc8-f3f959b48f87": "0005",
    "1e606897-2bb7-484c-8472-9229a45a9d0c": "0006",
    "833a355e-f199-4bd8-a57a-761d36be8f42": "0007",
    "ccd6d114-2b66-4ace-b874-f33832bfd63e": "0008",
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
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
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
        if self.cache_read_tokens == 0 and self.cached_input_tokens > 0:
            self.cache_read_tokens = self.cached_input_tokens
        elif self.cached_input_tokens == 0 and self.cache_read_tokens > 0:
            self.cached_input_tokens = self.cache_read_tokens

        bucket_sum = (
            self.input_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
            + self.output_tokens
        )
        if self.total_tokens == 0 and bucket_sum > 0:
            self.total_tokens = bucket_sum
        if self.estimated_cost_usd == 0.0 and bucket_sum > 0:
            self.estimated_cost_usd = round(
                calculate_token_cost(
                    input_tokens=self.input_tokens,
                    cache_write_tokens=self.cache_write_tokens,
                    cache_read_tokens=self.cache_read_tokens,
                    output_tokens=self.output_tokens,
                    model=self.model,
                    backend=self.backend,
                ),
                4,
            )
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
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cached_input_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_tokens_estimate": (
                self.input_tokens + self.cache_write_tokens + self.cache_read_tokens
            ),
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
    plan_path: Path | None = None,
) -> dict[str, str]:
    """Return mapping of 4-digit prompt numbers (`'0001'`) to prompt slug stems."""
    root = (repo_root or Path.cwd()).resolve()
    impl_dir = implemented_dir if implemented_dir is not None else root / "prompts" / "implemented"
    q_dir = queued_dir if queued_dir is not None else root / "prompts" / "queued"
    p_path = plan_path if plan_path is not None else root / "docs" / "PLAN.md"

    slug_map: dict[str, str] = {}
    for p_dir in (impl_dir, q_dir):
        if p_dir.is_dir():
            for f in sorted(p_dir.glob("*.md")):
                m = re.match(r"^(\d{4})-(.*)", f.stem)
                if m:
                    slug_map[m.group(1)] = f.stem

    if p_path.is_file():
        try:
            plan_text = p_path.read_text(encoding="utf-8", errors="replace")
            for line in plan_text.splitlines():
                m_plan = re.match(r"^\|\s*`?(\d{4})`?\s*\|\s*`?([a-zA-Z0-9_-]+)`?\s*\|", line)
                if m_plan:
                    pnum = m_plan.group(1)
                    raw_slug = m_plan.group(2)
                    if pnum not in slug_map:
                        full_slug = (
                            raw_slug if raw_slug.startswith(f"{pnum}-") else f"{pnum}-{raw_slug}"
                        )
                        slug_map[pnum] = full_slug
        except OSError:
            pass

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

    combined_all = f"{combined_user} {combined_sample}"
    if (
        "blueteamharness" in combined_all.lower()
        and "orchestack" not in combined_all.lower()
        and conv_id not in FOUNDATIONAL_SESSIONS
    ):
        return None

    # 3. Explicit daemon prompt kickoff in user messages
    m_p = re.search(
        r"Orchestack\s*[-—]+\s*Prompt\s+(\d{4})",
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
            if (not slug_map or cand in slug_map) and (cand != "0007" or "0007" in combined_all):
                return cand

    return None


def _compute_duration_from_timestamps(timestamps: Sequence[str]) -> float:
    """Compute active elapsed seconds across ISO-8601 step timestamps, excluding long idle gaps."""
    if len(timestamps) < 2:
        return 0.0
    parsed: list[datetime.datetime] = []
    for raw in timestamps:
        s = raw.strip()
        if not s:
            continue
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            parsed.append(dt)
        except Exception:
            continue
    if len(parsed) < 2:
        return 0.0
    if len(parsed) == 2:
        return max(0.0, (parsed[1] - parsed[0]).total_seconds())
    active_seconds = 0.0
    for i in range(1, len(parsed)):
        delta = (parsed[i] - parsed[i - 1]).total_seconds()
        if 0.0 <= delta <= 1800.0:
            active_seconds += delta
    if active_seconds > 0.0:
        return active_seconds
    return max(0.0, (parsed[-1] - parsed[0]).total_seconds())


# ---------------------------------------------------------------------------
# 1. Antigravity (`agy`) Telemetry
# ---------------------------------------------------------------------------


def _register_antigravity_transcript(
    sessions: dict[str, dict[str, Any]],
    conv_id: str,
    t_path: Path,
) -> None:
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


def discover_antigravity_sessions(
    cli_brain_dir: Path = CLI_BRAIN_DIR,
    ide_brain_dir: Path = IDE_BRAIN_DIR,
) -> dict[str, dict[str, Any]]:
    """Find all Antigravity session directories and their transcripts across CLI and IDE storage."""
    sessions: dict[str, dict[str, Any]] = {}

    for bdir in (cli_brain_dir, ide_brain_dir):
        try:
            if not bdir.is_dir():
                continue
        except OSError:
            continue
        try:
            for t_path in bdir.glob("*/.system_generated/logs/transcript*.jsonl"):
                conv_id = t_path.parent.parent.parent.name
                _register_antigravity_transcript(sessions, conv_id, t_path)
        except OSError:
            pass

        # Direct path lookup fallback when directory listing on `bdir` is restricted by sandbox
        if bdir in {CLI_BRAIN_DIR, IDE_BRAIN_DIR}:
            for conv_id in FOUNDATIONAL_SESSIONS:
                if conv_id in sessions:
                    continue
                logs_dir = bdir / conv_id / ".system_generated" / "logs"
                for fname in ("transcript_full.jsonl", "transcript.jsonl"):
                    cand = logs_dir / fname
                    try:
                        if cand.is_file():
                            _register_antigravity_transcript(sessions, conv_id, cand)
                    except OSError:
                        continue

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
    explicit_input_tokens = 0
    explicit_cache_write_tokens = 0
    explicit_cache_read_tokens = 0
    explicit_output_tokens = 0
    has_explicit_usage = False
    heuristic_input_tokens = 0
    heuristic_output_tokens = 0
    tool_counts: dict[str, int] = {}
    timestamps: list[str] = []
    user_prompts: list[str] = []
    text_sample: list[str] = []
    denials_count = 0
    model_name = "gemini"

    _usage_keys = (
        "prompt_tokens",
        "input_tokens",
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
        "cached_content_token_count",
        "completion_tokens",
        "output_tokens",
        "candidates_token_count",
    )

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

                raw_usage = data.get("usage") or data.get("token_usage")
                usage_src: Mapping[str, Any] | None = None
                if isinstance(raw_usage, Mapping) and any(
                    raw_usage.get(k) is not None for k in _usage_keys
                ):
                    usage_src = raw_usage
                elif any(data.get(k) is not None for k in _usage_keys):
                    usage_src = data

                if usage_src is not None:
                    has_explicit_usage = True
                    in_t = int(usage_src.get("prompt_tokens") or usage_src.get("input_tokens") or 0)
                    cache_w_t = int(
                        usage_src.get("cache_write_tokens")
                        or usage_src.get("cache_creation_input_tokens")
                        or 0
                    )
                    cache_r_t = int(
                        usage_src.get("cache_read_tokens")
                        or usage_src.get("cached_input_tokens")
                        or usage_src.get("cache_read_input_tokens")
                        or usage_src.get("cached_content_token_count")
                        or 0
                    )
                    out_t = int(
                        usage_src.get("completion_tokens")
                        or usage_src.get("output_tokens")
                        or usage_src.get("candidates_token_count")
                        or 0
                    )
                    explicit_input_tokens += in_t
                    explicit_cache_write_tokens += cache_w_t
                    explicit_cache_read_tokens += cache_r_t
                    explicit_output_tokens += out_t
                else:
                    if step_type in ("USER_INPUT", "SYSTEM_MESSAGE", "GENERIC", "CHECKPOINT"):
                        heuristic_input_tokens += max(1, len(content) // 4)
                    elif step_type == "PLANNER_RESPONSE":
                        heuristic_output_tokens += max(1, (len(content) + len(thinking)) // 4)

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
                    heuristic_input_tokens += len(str(args)) // 4

    except PermissionError:
        return None
    except Exception as e:
        print(f"Warning: error reading {target_file}: {e}", file=sys.stderr)
        return None

    input_tokens = explicit_input_tokens if has_explicit_usage else heuristic_input_tokens
    cache_write_tokens = explicit_cache_write_tokens if has_explicit_usage else 0
    cache_read_tokens = explicit_cache_read_tokens if has_explicit_usage else 0
    output_tokens = explicit_output_tokens if has_explicit_usage else heuristic_output_tokens

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
    est_cost = calculate_token_cost(
        model_name,
        backend="agy",
        input_tokens=input_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        output_tokens=output_tokens,
    )

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
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        cached_input_tokens=cache_read_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + cache_write_tokens + cache_read_tokens + output_tokens,
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
    result_cache_write_tokens = 0
    result_cache_read_tokens = 0
    result_output_tokens = 0
    has_result_usage = False
    assistant_input_tokens = 0
    assistant_cache_write_tokens = 0
    assistant_cache_read_tokens = 0
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
                    assistant_cache_write_tokens += int(
                        msg_usage.get("cache_creation_input_tokens")
                        or msg_usage.get("cache_write_tokens")
                        or 0
                    )
                    assistant_cache_read_tokens += int(
                        msg_usage.get("cache_read_input_tokens")
                        or msg_usage.get("cache_read_tokens")
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
                result_cache_write_tokens += int(
                    usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0
                )
                result_cache_read_tokens += int(
                    usage.get("cache_read_input_tokens")
                    or usage.get("cache_read_tokens")
                    or usage.get("cached_input_tokens")
                    or 0
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
    cache_write_tokens = (
        result_cache_write_tokens if has_result_usage else assistant_cache_write_tokens
    )
    cache_read_tokens = (
        result_cache_read_tokens if has_result_usage else assistant_cache_read_tokens
    )
    output_tokens = result_output_tokens if has_result_usage else assistant_output_tokens
    total_tokens = input_tokens + cache_write_tokens + cache_read_tokens + output_tokens

    if explicit_duration_ms > 0:
        duration_s = explicit_duration_ms / 1000.0
    else:
        duration_s = _compute_duration_from_timestamps(timestamps)

    effective_model = model or "claude"
    if explicit_cost_usd is not None:
        cost_usd = explicit_cost_usd
    else:
        cost_usd = calculate_token_cost(
            effective_model,
            backend="claude",
            input_tokens=input_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=output_tokens,
        )

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
        model=effective_model,
        prompt_num=prompt_num,
        prompt_slug=slug,
        total_steps=total_steps,
        num_turns=num_turns if num_turns > 0 else total_steps,
        input_tokens=input_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        cached_input_tokens=cache_read_tokens,
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
    try:
        if not claude_projects_dir.is_dir():
            return results
        paths = sorted(claude_projects_dir.glob("**/*.jsonl"))
    except OSError:
        return results

    for jsonl_path in paths:
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
    cache_write_tokens = 0
    cache_read_tokens = 0
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
                cache_write_tokens += int(
                    usage.get("cache_write_tokens") or usage.get("cache_creation_input_tokens") or 0
                )
                cached_val = (
                    usage.get("cache_read_tokens")
                    if usage.get("cache_read_tokens") is not None
                    else usage.get("cached_input_tokens")
                )
                if cached_val is None:
                    details = usage.get("input_tokens_details")
                    if isinstance(details, dict):
                        cached_val = details.get("cached_tokens")
                cache_read_tokens += int(cached_val or 0)
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

    total_tokens = input_tokens + cache_write_tokens + cache_read_tokens + output_tokens
    if explicit_duration_ms > 0:
        duration_s = explicit_duration_ms / 1000.0
    else:
        duration_s = _compute_duration_from_timestamps(timestamps)

    effective_model = model or "codex"
    if explicit_cost_usd is not None:
        cost_usd = explicit_cost_usd
    else:
        cost_usd = calculate_token_cost(
            effective_model,
            backend="codex",
            input_tokens=input_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=output_tokens,
        )

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
        model=effective_model,
        prompt_num=prompt_num,
        prompt_slug=slug,
        total_steps=total_steps,
        num_turns=num_turns if num_turns > 0 else total_steps,
        input_tokens=input_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        cached_input_tokens=cache_read_tokens,
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
    try:
        if not codex_sessions_dir.is_dir():
            return results
    except OSError:
        return results

    seen_paths: set[Path] = set()
    for pattern in ("**/rollout-*.jsonl", "**/*.jsonl"):
        try:
            paths = sorted(codex_sessions_dir.glob(pattern))
        except OSError:
            continue
        for jsonl_path in paths:
            try:
                resolved = jsonl_path.resolve()
            except OSError:
                continue
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

    if file_path.name in {"daemon_metrics.jsonl", "metrics.jsonl"}:
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
            model_str = str(rec.get("model") or ("gemini" if backend == "agy" else backend))
            sid = str(rec.get("session_id") or rec.get("conv_id") or f"metrics-{slug}-{idx}")
            in_tok = int(rec.get("input_tokens") or rec.get("input_tokens_estimate") or 0)
            cache_write_tok = int(
                rec.get("cache_write_tokens") or rec.get("cache_creation_input_tokens") or 0
            )
            cache_read_tok = int(
                rec.get("cache_read_tokens")
                or rec.get("cached_input_tokens")
                or rec.get("cache_read_input_tokens")
                or 0
            )
            out_tok = int(rec.get("output_tokens") or rec.get("output_tokens_estimate") or 0)
            tot_tok = int(
                rec.get("total_tokens")
                or rec.get("total_tokens_estimate")
                or (in_tok + cache_write_tok + cache_read_tok + out_tok)
            )
            raw_cost = float(rec.get("estimated_cost_usd") or 0.0)
            est_cost = (
                raw_cost
                if raw_cost > 0.0
                else calculate_token_cost(
                    model_str,
                    backend=backend,
                    input_tokens=in_tok,
                    cache_write_tokens=cache_write_tok,
                    cache_read_tokens=cache_read_tok,
                    output_tokens=out_tok,
                )
            )
            tools = rec.get("tool_calls_breakdown")
            records.append(
                SessionTelemetry(
                    session_id=sid,
                    backend=backend,
                    model=model_str,
                    prompt_num=pnum,
                    prompt_slug=slug,
                    total_steps=int(rec.get("total_steps") or 0),
                    num_turns=int(rec.get("num_turns") or rec.get("total_steps") or 0),
                    input_tokens=in_tok,
                    cache_write_tokens=cache_write_tok,
                    cache_read_tokens=cache_read_tok,
                    cached_input_tokens=cache_read_tok,
                    output_tokens=out_tok,
                    total_tokens=tot_tok,
                    estimated_cost_usd=round(est_cost, 4),
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
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
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
        b_stats["cache_write_tokens"] += s.cache_write_tokens
        b_stats["cache_read_tokens"] += s.cache_read_tokens
        b_stats["cached_input_tokens"] += s.cache_read_tokens
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
        cache_write_tok = sum(s.cache_write_tokens for s in sessions)
        cache_read_tok = sum(s.cache_read_tokens for s in sessions)
        out_tok = sum(s.output_tokens for s in sessions)
        tot_tok = sum(s.total_tokens for s in sessions)
        summary_by_prompt[pnum] = {
            "slug": slug_map.get(pnum, f"prompt-{pnum}"),
            "backends": backends,
            "sessions_count": len(sessions),
            "total_steps": sum(s.total_steps for s in sessions),
            "num_turns": sum(s.num_turns for s in sessions),
            "input_tokens": in_tok,
            "cache_write_tokens": cache_write_tok,
            "cache_read_tokens": cache_read_tok,
            "cached_input_tokens": cache_read_tok,
            "output_tokens": out_tok,
            "total_tokens": tot_tok,
            "input_tokens_estimate": in_tok + cache_write_tok + cache_read_tok,
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
            "cache_write_tokens": sum(s.cache_write_tokens for s in all_sessions),
            "cache_read_tokens": sum(s.cache_read_tokens for s in all_sessions),
            "cached_input_tokens": sum(s.cache_read_tokens for s in all_sessions),
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
            "| Prompt | Name / Slug | Backends | Sessions | Steps | input_tokens "
            "| cache_write_tokens | cache_read_tokens | output_tokens | Total Tokens "
            "| Cost (USD) | Duration |"
        ),
        (
            "| :--- | :--- | :--- | :---: | :---: | :---: "
            "| :---: | :---: | :---: | :---: | :---: | :---: |"
        ),
    ]

    tot_steps = 0
    tot_in = 0
    tot_cache_write = 0
    tot_cache_read = 0
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
        cache_write_tok = sum(s.cache_write_tokens for s in sessions)
        cache_read_tok = sum(s.cache_read_tokens for s in sessions)
        out_tok = sum(s.output_tokens for s in sessions)
        tokens = sum(s.total_tokens for s in sessions)
        cost = sum(s.estimated_cost_usd for s in sessions)
        dur_s = sum(s.duration_seconds for s in sessions)
        dur_str = f"{dur_s / 60.0:.1f}m" if dur_s < 3600 else f"{dur_s / 3600.0:.1f}h"

        tot_prompt_sessions += sess_count
        tot_steps += steps
        tot_in += in_tok
        tot_cache_write += cache_write_tok
        tot_cache_read += cache_read_tok
        tot_out += out_tok
        tot_tokens += tokens
        tot_cost += cost
        tot_dur += dur_s

        lines.append(
            f"| `{pnum}` | `{slug}` | `{backends_str}` | {sess_count} | {steps:,} | "
            f"{in_tok:,} | {cache_write_tok:,} | {cache_read_tok:,} | {out_tok:,} | "
            f"~{tokens:,} | ${cost:.4f} | {dur_str} |"
        )

    tot_dur_h = tot_dur / 3600.0
    lines.append(
        f"| **Total** | **All Prompts** | **All** | **{tot_prompt_sessions}** | "
        f"**{tot_steps:,}** | **{tot_in:,}** | **{tot_cache_write:,}** | "
        f"**{tot_cache_read:,}** | **{tot_out:,}** | **~{tot_tokens:,}** | "
        f"**${tot_cost:.4f}** | **{tot_dur_h:.1f}h** |"
    )

    if all_sessions:
        lines.extend(
            [
                "",
                "## Sessions by Backend",
                "",
                (
                    "| Session ID | Backend | Model | Prompt | Turns | input_tokens "
                    "| cache_write_tokens | cache_read_tokens | output_tokens "
                    "| Cost (USD) | Duration |"
                ),
                (
                    "| :--- | :--- | :--- | :--- | :---: | :---: "
                    "| :---: | :---: | :---: | :---: | :---: |"
                ),
            ]
        )
        for s in all_sessions:
            p_label = s.prompt_num or "unmapped"
            dur_str = f"{s.duration_seconds:.1f}s"
            lines.append(
                f"| `{s.session_id}` | `{s.backend}` | `{s.model or s.backend}` | `{p_label}` | "
                f"{s.num_turns} | {s.input_tokens:,} | {s.cache_write_tokens:,} | "
                f"{s.cache_read_tokens:,} | {s.output_tokens:,} | "
                f"${s.estimated_cost_usd:.4f} | {dur_str} |"
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


# ---------------------------------------------------------------------------
# 6. Phase Implementation Statistics (`docs/IMPLEMENTATION_STATS.md`)
# ---------------------------------------------------------------------------


@dataclass
class PhaseImplementationStats:
    """Aggregated implementation time, unified token breakdown, and cost for a single phase."""

    phase_num: str
    phase_slug: str
    model: str = "gemini"
    sessions_count: int = 0
    duration_seconds: float = 0.0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.cache_read_tokens == 0 and self.cached_input_tokens > 0:
            self.cache_read_tokens = self.cached_input_tokens
        elif self.cached_input_tokens == 0 and self.cache_read_tokens > 0:
            self.cached_input_tokens = self.cache_read_tokens
        computed_total = (
            self.input_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
            + self.output_tokens
        )
        if self.total_tokens == 0 and computed_total > 0:
            self.total_tokens = computed_total
        if self.estimated_cost_usd == 0.0 and computed_total > 0:
            self.estimated_cost_usd = round(
                calculate_token_cost(
                    self.model,
                    input_tokens=self.input_tokens,
                    cache_write_tokens=self.cache_write_tokens,
                    cache_read_tokens=self.cache_read_tokens,
                    output_tokens=self.output_tokens,
                ),
                4,
            )


@dataclass
class TelemetryReport:
    """Structured multi-agent telemetry report across all discovered sessions and phases."""

    sessions: list[SessionTelemetry] = field(default_factory=list)
    by_prompt: dict[str, list[SessionTelemetry]] = field(default_factory=dict)
    slug_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.by_prompt and self.sessions:
            grouped: dict[str, list[SessionTelemetry]] = {}
            for sess in self.sessions:
                if sess.prompt_num:
                    grouped.setdefault(sess.prompt_num, []).append(sess)
            self.by_prompt = grouped


def format_duration(seconds: float) -> str:
    """Format duration in seconds into human-readable `Xs`, `Xm Ys`, or `Xh Ym Zs`."""
    total_seconds = max(0, round(seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        mins, secs = divmod(total_seconds, 60)
        return f"{mins}m {secs}s"
    hours, rem = divmod(total_seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}h {mins}m {secs}s"


def parse_duration_str(text: str) -> float:
    """Parse a human-readable duration cell (`1h 2m 3s`, `18m 55s`, `45s`) back into seconds."""
    cleaned = text.strip().replace("*", "")
    if not cleaned:
        return 0.0

    m_raw = re.search(r"\(([0-9,]+(?:\.[0-9]+)?)\s*s\)", cleaned)
    if m_raw:
        try:
            return float(m_raw.group(1).replace(",", ""))
        except ValueError:
            pass

    total = 0.0
    matched = False
    for unit_pattern, multiplier in (
        (r"([0-9]+(?:\.[0-9]+)?)\s*h", 3600.0),
        (r"([0-9]+(?:\.[0-9]+)?)\s*m", 60.0),
        (r"([0-9]+(?:\.[0-9]+)?)\s*s", 1.0),
    ):
        m_u = re.search(unit_pattern, cleaned, re.IGNORECASE)
        if m_u:
            try:
                total += float(m_u.group(1)) * multiplier
                matched = True
            except ValueError:
                pass
    if matched:
        return total

    try:
        return float(cleaned.replace(",", ""))
    except ValueError:
        return 0.0


def _parse_int_cell(cell: str) -> int:
    """Parse an integer token count cell (stripping commas, bold markers, or `~`)."""
    cleaned = cell.strip().strip("*`~ ")
    m = re.search(r"([0-9,]+)", cleaned)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def _parse_cost_cell(cell: str) -> float:
    """Parse a USD cost cell (`$0.42`, `$1.60 (150,000 tokens)`) into a float."""
    m_cost = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", cell)
    if m_cost:
        try:
            return float(m_cost.group(1).replace(",", ""))
        except ValueError:
            return 0.0
    return 0.0


def _effective_session_model(sess: SessionTelemetry) -> str:
    """Return the canonical model label for a session (`gemini`, `claude`, `codex`, or explicit)."""
    if sess.model and sess.model.strip():
        return sess.model.strip()
    if sess.backend == "agy":
        return "gemini"
    return sess.backend or "gemini"


def parse_existing_implementation_stats(
    stats_source: Path | str,
) -> dict[str, PhaseImplementationStats]:
    """Parse existing phase rows from `docs/IMPLEMENTATION_STATS.md` content or file path.

    Supports both the 9-column pricing-grouped format (`Phase`, `Model`, `Implementation Time`,
    `input_tokens`, `cache_write_tokens`, `cache_read_tokens`, `output_tokens`, `Total Tokens`,
    `Total Cost`) and the legacy 3-column format (`Phase`, `Implementation Time`,
    `Total Token Cost`).
    """
    if isinstance(stats_source, Path):
        if not stats_source.is_file():
            return {}
        try:
            raw_text = stats_source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
    else:
        raw_text = stats_source

    parsed: dict[str, PhaseImplementationStats] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        phase_cell = cells[0]
        if (
            not phase_cell
            or phase_cell.lower() == "phase"
            or "total" in phase_cell.lower()
            or set(phase_cell) <= {":", "-", " "}
        ):
            continue

        clean_phase = phase_cell.strip("`* ")
        m_phase = re.match(r"^(\d{4})(?:-(.+))?$", clean_phase)
        if not m_phase:
            continue
        pnum = m_phase.group(1)
        pslug = clean_phase if m_phase.group(2) else f"prompt-{pnum}"

        if len(cells) >= 9:
            model_cell = cells[1].strip("`* ") or "gemini"
            dur_s = parse_duration_str(cells[2])
            in_tok = _parse_int_cell(cells[3])
            cache_write_tok = _parse_int_cell(cells[4])
            cache_read_tok = _parse_int_cell(cells[5])
            out_tok = _parse_int_cell(cells[6])
            tot_tok = _parse_int_cell(cells[7]) or (
                in_tok + cache_write_tok + cache_read_tok + out_tok
            )
            cost_usd = _parse_cost_cell(cells[8])
            parsed[pnum] = PhaseImplementationStats(
                phase_num=pnum,
                phase_slug=pslug,
                model=model_cell,
                sessions_count=1 if (dur_s > 0 or tot_tok > 0 or cost_usd > 0) else 0,
                duration_seconds=dur_s,
                input_tokens=in_tok,
                cache_write_tokens=cache_write_tok,
                cache_read_tokens=cache_read_tok,
                cached_input_tokens=cache_read_tok,
                output_tokens=out_tok,
                total_tokens=tot_tok,
                estimated_cost_usd=cost_usd,
            )
        else:
            time_cell, cost_cell = cells[1], cells[2]
            dur_s = parse_duration_str(time_cell)
            cost_usd = _parse_cost_cell(cost_cell)
            tot_tokens = 0
            m_tok = re.search(r"([0-9,]+)\s*tokens?", cost_cell, re.IGNORECASE)
            if m_tok:
                try:
                    tot_tokens = int(m_tok.group(1).replace(",", ""))
                except ValueError:
                    tot_tokens = 0

            parsed[pnum] = PhaseImplementationStats(
                phase_num=pnum,
                phase_slug=pslug,
                sessions_count=1 if (dur_s > 0 or tot_tokens > 0 or cost_usd > 0) else 0,
                duration_seconds=dur_s,
                total_tokens=tot_tokens,
                estimated_cost_usd=cost_usd,
            )

    return parsed


def _coerce_session_telemetry(item: SessionTelemetry | Mapping[str, Any]) -> SessionTelemetry:
    if isinstance(item, SessionTelemetry):
        return item
    p_slug = str(item.get("prompt_slug") or "")
    p_num = item.get("prompt_num")
    if not p_num and p_slug:
        m = re.match(r"^(\d{4})", p_slug)
        if m:
            p_num = m.group(1)
    in_tok = int(item.get("input_tokens") or item.get("input_tokens_estimate") or 0)
    cache_write_tok = int(
        item.get("cache_write_tokens") or item.get("cache_creation_input_tokens") or 0
    )
    cache_read_tok = int(
        item.get("cache_read_tokens")
        or item.get("cached_input_tokens")
        or item.get("cache_read_input_tokens")
        or 0
    )
    out_tok = int(item.get("output_tokens") or item.get("output_tokens_estimate") or 0)
    tot_tok = int(
        item.get("total_tokens")
        or item.get("total_tokens_estimate")
        or (in_tok + cache_write_tok + cache_read_tok + out_tok)
    )
    return SessionTelemetry(
        session_id=str(item.get("session_id") or item.get("conv_id") or "session"),
        backend=str(item.get("backend") or "agy"),
        model=str(item.get("model") or ""),
        prompt_num=str(p_num) if p_num else None,
        prompt_slug=p_slug,
        total_steps=int(item.get("total_steps") or 0),
        num_turns=int(item.get("num_turns") or item.get("total_steps") or 0),
        input_tokens=in_tok,
        cache_write_tokens=cache_write_tok,
        cache_read_tokens=cache_read_tok,
        cached_input_tokens=cache_read_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        estimated_cost_usd=float(item.get("estimated_cost_usd") or 0.0),
        duration_seconds=float(
            item.get("duration_seconds") or item.get("session_duration_seconds") or 0.0
        ),
    )


def _normalize_report_input(
    report: (
        TelemetryReport | Sequence[SessionTelemetry | Mapping[str, Any]] | Mapping[str, Any] | None
    ),
    repo_root: Path | None = None,
    slug_map: Mapping[str, str] | None = None,
) -> tuple[dict[str, list[SessionTelemetry]], dict[str, str]]:
    effective_slug_map: dict[str, str] = {}
    if repo_root is not None:
        effective_slug_map.update(build_canonical_slug_map(repo_root))
    if slug_map is not None:
        effective_slug_map.update(slug_map)
    has_canonical_slugs = bool(effective_slug_map)

    by_prompt: dict[str, list[SessionTelemetry]] = {}

    if report is None:
        _, collected_by_prompt = collect_multi_agent_telemetry(repo_root=repo_root)
        by_prompt = collected_by_prompt
        if not effective_slug_map:
            effective_slug_map.update(build_canonical_slug_map(repo_root))
            has_canonical_slugs = bool(effective_slug_map)
    elif isinstance(report, TelemetryReport):
        if report.slug_map:
            for k, v in report.slug_map.items():
                effective_slug_map.setdefault(k, v)
            has_canonical_slugs = bool(effective_slug_map)
        for pnum, sess_list in report.by_prompt.items():
            by_prompt[pnum] = [_coerce_session_telemetry(s) for s in sess_list]
    elif isinstance(report, Mapping):
        if "by_prompt" in report and isinstance(report["by_prompt"], Mapping):
            for pnum, sess_list in report["by_prompt"].items():
                if isinstance(sess_list, Sequence):
                    by_prompt[str(pnum)] = [_coerce_session_telemetry(s) for s in sess_list]
        elif "sessions" in report and isinstance(report["sessions"], Sequence):
            for raw_s in report["sessions"]:
                if isinstance(raw_s, (SessionTelemetry, Mapping)):
                    s = _coerce_session_telemetry(raw_s)
                    if s.prompt_num:
                        by_prompt.setdefault(s.prompt_num, []).append(s)
        else:
            for pnum, val in report.items():
                if isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
                    by_prompt[str(pnum)] = [_coerce_session_telemetry(s) for s in val]
                elif isinstance(val, (SessionTelemetry, Mapping)):
                    s = _coerce_session_telemetry(val)
                    by_prompt.setdefault(str(pnum), []).append(s)
    elif isinstance(report, Sequence):
        for raw_s in report:
            s = _coerce_session_telemetry(raw_s)
            if s.prompt_num:
                by_prompt.setdefault(s.prompt_num, []).append(s)

    for pnum, sess_list in by_prompt.items():
        if pnum not in effective_slug_map and sess_list:
            first_slug = sess_list[0].prompt_slug
            if has_canonical_slugs and (
                not first_slug or first_slug.startswith("other-") or "-other-" in first_slug
            ):
                continue
            if first_slug:
                effective_slug_map[pnum] = (
                    first_slug if first_slug.startswith(f"{pnum}-") else f"{pnum}-{first_slug}"
                )
            else:
                effective_slug_map[pnum] = f"prompt-{pnum}"

    return by_prompt, effective_slug_map


def render_implementation_stats_markdown(
    report: (
        TelemetryReport | Sequence[SessionTelemetry | Mapping[str, Any]] | Mapping[str, Any] | None
    ) = None,
    repo_root: Path | None = None,
    *,
    existing_stats: Mapping[str, PhaseImplementationStats] | Path | str | None = None,
    slug_map: Mapping[str, str] | None = None,
) -> str:
    """Render `docs/IMPLEMENTATION_STATS.md` with pricing-grouped token breakdown and model cost."""
    by_prompt, effective_slug_map = _normalize_report_input(
        report,
        repo_root=repo_root,
        slug_map=slug_map,
    )

    parsed_existing: dict[str, PhaseImplementationStats] = {}
    if existing_stats is not None:
        if isinstance(existing_stats, (Path, str)):
            parsed_existing = parse_existing_implementation_stats(existing_stats)
        else:
            parsed_existing = dict(existing_stats)
    elif repo_root is not None:
        default_stats_file = repo_root / "docs" / "IMPLEMENTATION_STATS.md"
        if default_stats_file.is_file():
            parsed_existing = parse_existing_implementation_stats(default_stats_file)

    for pnum, ex_stat in parsed_existing.items():
        if (
            pnum not in effective_slug_map
            and ex_stat.phase_slug
            and not ex_stat.phase_slug.startswith("prompt-")
            and "-other-" not in ex_stat.phase_slug
        ):
            effective_slug_map[pnum] = ex_stat.phase_slug

    implemented_pnums: set[str] = set()
    if repo_root is not None:
        impl_dir = repo_root / "prompts" / "implemented"
        if impl_dir.is_dir():
            for f in impl_dir.glob("*.md"):
                m = re.match(r"^(\d{4})-", f.stem)
                if m:
                    implemented_pnums.add(m.group(1))

    if effective_slug_map:
        all_pnums = sorted(effective_slug_map.keys())
    else:
        all_pnums = sorted(set(by_prompt.keys()) | set(parsed_existing.keys()))

    phase_rows: list[PhaseImplementationStats] = []
    for pnum in all_pnums:
        sessions = by_prompt.get(pnum, [])
        ex = parsed_existing.get(pnum)
        canonical_slug = effective_slug_map.get(
            pnum,
            ex.phase_slug
            if ex is not None
            else (sessions[0].prompt_slug if sessions else f"prompt-{pnum}"),
        )

        live_models = list(dict.fromkeys(_effective_session_model(s) for s in sessions))
        live_model_str = ", ".join(live_models) if live_models else (ex.model if ex else "gemini")
        live_dur = sum(s.duration_seconds for s in sessions)
        live_in = sum(s.input_tokens for s in sessions)
        live_cache_write = sum(s.cache_write_tokens for s in sessions)
        live_cache_read = sum(s.cache_read_tokens for s in sessions)
        live_out = sum(s.output_tokens for s in sessions)
        live_tokens = sum(s.total_tokens for s in sessions)
        live_cost = sum(s.estimated_cost_usd for s in sessions)
        has_live = bool(sessions) and (live_dur > 0 or live_tokens > 0 or live_cost > 0)
        has_existing = ex is not None and (
            ex.duration_seconds > 0 or ex.total_tokens > 0 or ex.estimated_cost_usd > 0
        )

        if (
            not has_live
            and not has_existing
            and pnum not in implemented_pnums
            and (by_prompt or parsed_existing or implemented_pnums)
        ):
            # Skip unimplemented future phases that have no recorded telemetry yet
            continue

        if has_existing and ex is not None:
            ex_missing_breakdown = (
                ex.input_tokens == 0
                and ex.cache_write_tokens == 0
                and ex.cache_read_tokens == 0
                and ex.output_tokens == 0
            )
            if has_live and (
                live_tokens > ex.total_tokens
                or round(live_cost, 2) > round(ex.estimated_cost_usd, 2)
                or (
                    ex.total_tokens == 0
                    and ex.estimated_cost_usd == 0
                    and live_dur > ex.duration_seconds
                )
                or (ex_missing_breakdown and live_tokens == ex.total_tokens and live_tokens > 0)
            ):
                chosen = PhaseImplementationStats(
                    phase_num=pnum,
                    phase_slug=canonical_slug,
                    model=live_model_str,
                    sessions_count=len(sessions),
                    duration_seconds=max(live_dur, ex.duration_seconds)
                    if live_tokens == ex.total_tokens
                    else live_dur,
                    input_tokens=live_in,
                    cache_write_tokens=live_cache_write,
                    cache_read_tokens=live_cache_read,
                    cached_input_tokens=live_cache_read,
                    output_tokens=live_out,
                    total_tokens=live_tokens,
                    estimated_cost_usd=live_cost,
                )
            else:
                chosen = PhaseImplementationStats(
                    phase_num=pnum,
                    phase_slug=canonical_slug,
                    model=ex.model or live_model_str,
                    sessions_count=max(ex.sessions_count, len(sessions)),
                    duration_seconds=ex.duration_seconds,
                    input_tokens=ex.input_tokens,
                    cache_write_tokens=ex.cache_write_tokens,
                    cache_read_tokens=ex.cache_read_tokens,
                    cached_input_tokens=ex.cache_read_tokens,
                    output_tokens=ex.output_tokens,
                    total_tokens=ex.total_tokens,
                    estimated_cost_usd=ex.estimated_cost_usd,
                )
        else:
            chosen = PhaseImplementationStats(
                phase_num=pnum,
                phase_slug=canonical_slug,
                model=live_model_str,
                sessions_count=len(sessions),
                duration_seconds=live_dur,
                input_tokens=live_in,
                cache_write_tokens=live_cache_write,
                cache_read_tokens=live_cache_read,
                cached_input_tokens=live_cache_read,
                output_tokens=live_out,
                total_tokens=live_tokens,
                estimated_cost_usd=live_cost,
            )

        phase_rows.append(chosen)

    lines = [
        "# Implementation Statistics",
        "",
        (
            "This document tracks the cumulative implementation time, pricing-grouped token "
            "breakdown (`input_tokens`, `cache_write_tokens`, `cache_read_tokens`, "
            "`output_tokens`), and model-based USD cost for each Orchestack delivery phase, "
            "aggregated across all coding agent implementation sessions and PR/MR review or "
            "auto-heal turns."
        ),
        "",
        (
            "| Phase | Model | Implementation Time | `input_tokens` | `cache_write_tokens` "
            "| `cache_read_tokens` | `output_tokens` | Total Tokens | Total Cost |"
        ),
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    total_duration = 0.0
    total_input = 0
    total_cache_write = 0
    total_cache_read = 0
    total_output = 0
    total_tokens = 0
    total_cost = 0.0

    for row in phase_rows:
        total_duration += row.duration_seconds
        total_input += row.input_tokens
        total_cache_write += row.cache_write_tokens
        total_cache_read += row.cache_read_tokens
        total_output += row.output_tokens
        total_tokens += row.total_tokens
        total_cost += row.estimated_cost_usd
        dur_str = format_duration(row.duration_seconds)
        lines.append(
            f"| `{row.phase_slug}` | `{row.model}` | {dur_str} | {row.input_tokens:,} | "
            f"{row.cache_write_tokens:,} | {row.cache_read_tokens:,} | "
            f"{row.output_tokens:,} | {row.total_tokens:,} | ${row.estimated_cost_usd:.2f} |"
        )

    tot_dur_str = format_duration(total_duration)
    lines.append(
        f"| **Total** | **—** | **{tot_dur_str}** | **{total_input:,}** | "
        f"**{total_cache_write:,}** | **{total_cache_read:,}** | "
        f"**{total_output:,}** | **{total_tokens:,}** | **${total_cost:.2f}** |"
    )
    lines.append("")
    return "\n".join(lines)


def update_implementation_stats(
    repo_root: Path | None = None,
    output_path: Path | None = None,
    *,
    report: (
        TelemetryReport | Sequence[SessionTelemetry | Mapping[str, Any]] | Mapping[str, Any] | None
    ) = None,
    agy_cli_brain_dir: Path | None = None,
    agy_ide_brain_dir: Path | None = None,
    agy_summaries_dbs: Sequence[Path] | None = None,
    claude_projects_dir: Path | None = None,
    codex_sessions_dir: Path | None = None,
    daemon_logs_dirs: Sequence[Path] | None = None,
) -> Path:
    """Collect multi-agent telemetry and write/update `docs/IMPLEMENTATION_STATS.md`."""
    root = (repo_root or Path.cwd()).resolve()
    target_path = (
        output_path if output_path is not None else root / "docs" / "IMPLEMENTATION_STATS.md"
    )

    existing: dict[str, PhaseImplementationStats] = {}
    default_stats_path = root / "docs" / "IMPLEMENTATION_STATS.md"
    if default_stats_path.is_file():
        existing.update(parse_existing_implementation_stats(default_stats_path))
    if target_path.is_file() and target_path.resolve() != default_stats_path.resolve():
        existing.update(parse_existing_implementation_stats(target_path))

    slug_map = build_canonical_slug_map(root)

    if report is None:
        sessions, by_prompt = collect_multi_agent_telemetry(
            repo_root=root,
            agy_cli_brain_dir=agy_cli_brain_dir,
            agy_ide_brain_dir=agy_ide_brain_dir,
            agy_summaries_dbs=agy_summaries_dbs,
            claude_projects_dir=claude_projects_dir,
            codex_sessions_dir=codex_sessions_dir,
            daemon_logs_dirs=daemon_logs_dirs,
        )
        report = TelemetryReport(sessions=sessions, by_prompt=by_prompt, slug_map=slug_map)

    markdown = render_implementation_stats_markdown(
        report,
        repo_root=root,
        existing_stats=existing,
        slug_map=slug_map,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(markdown, encoding="utf-8")
    return target_path

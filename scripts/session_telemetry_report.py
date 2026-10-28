#!/usr/bin/env python3
# Copyright 2026 The Orchestack Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
session_telemetry_report.py — Historical Multi-Agent Session Telemetry, Token & Cost Reporter

Extracts and aggregates steps, token usage estimates, costs, tool calls, and duration
across Antigravity, Claude Code, and Codex sessions in an Orchestack repository.

Usage:
    uv run python scripts/session_telemetry_report.py [options]

Options:
    --repo PATH         Target repository root path.
    --format FORMAT     Output format: markdown or json.
    --output PATH       Destination file path for the generated report.
    --sessions, -s      Show granular breakdown of each individual session run.
    --backfill          Backfill historical prompt metrics to logs/daemon_metrics.jsonl.
    --json              Output aggregated statistics as JSON.
    --markdown          Output aggregated statistics as a GitHub Markdown table.
    --csv               Export summary as CSV.
    --update-stats      Write/update per-phase implementation stats in docs/IMPLEMENTATION_STATS.md.
    --stats-file PATH   Custom destination path for the implementation stats Markdown document.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from orchestack.telemetry.reporter import (  # noqa: E402
    DEFAULT_CLAUDE_PROJECTS_DIR,
    DEFAULT_CODEX_SESSIONS_DIR,
    FOUNDATIONAL_SESSIONS,
    INPUT_COST_PER_MILLION,
    OUTPUT_COST_PER_MILLION,
    SessionTelemetry,
    collect_multi_agent_telemetry,
    discover_claude_sessions,
    discover_codex_sessions,
    format_telemetry_json,
    format_telemetry_markdown,
    parse_claude_events_telemetry,
    parse_codex_events_telemetry,
    parse_daemon_log_file,
    update_implementation_stats,
)

CLAUDE_PROJECTS_DIR = DEFAULT_CLAUDE_PROJECTS_DIR
CODEX_SESSIONS_DIR = DEFAULT_CODEX_SESSIONS_DIR

LOGS_DIR = REPO_ROOT / "logs"
DAEMON_METRICS_FILE = LOGS_DIR / "daemon_metrics.jsonl"
IMPLEMENTED_DIR = REPO_ROOT / "prompts" / "implemented"
QUEUED_DIR = REPO_ROOT / "prompts" / "queued"

IDE_BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
CLI_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"
IDE_SUMMARIES_DB = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
CLI_SUMMARIES_DB = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"

__all__ = [
    "CLAUDE_PROJECTS_DIR",
    "CLI_BRAIN_DIR",
    "CLI_SUMMARIES_DB",
    "CODEX_SESSIONS_DIR",
    "DAEMON_METRICS_FILE",
    "FOUNDATIONAL_SESSIONS",
    "IDE_BRAIN_DIR",
    "IDE_SUMMARIES_DB",
    "IMPLEMENTED_DIR",
    "INPUT_COST_PER_MILLION",
    "LOGS_DIR",
    "OUTPUT_COST_PER_MILLION",
    "QUEUED_DIR",
    "REPO_ROOT",
    "SessionTelemetry",
    "backfill_daemon_metrics_file",
    "build_canonical_slug_map",
    "collect_multi_agent_telemetry",
    "collect_telemetry_dataset",
    "discover_all_sessions",
    "discover_claude_sessions",
    "discover_codex_sessions",
    "export_markdown_table",
    "format_telemetry_json",
    "format_telemetry_markdown",
    "load_conversation_titles",
    "main",
    "parse_claude_events_telemetry",
    "parse_codex_events_telemetry",
    "parse_daemon_log_file",
    "parse_session_transcript",
    "print_individual_sessions",
    "print_prompt_summary_table",
]


def build_canonical_slug_map() -> dict[str, str]:
    """Return mapping of 4-digit prompt numbers to prompt slug names."""
    slug_map: dict[str, str] = {}
    for p_dir in (IMPLEMENTED_DIR, QUEUED_DIR):
        if p_dir.is_dir():
            for f in p_dir.glob("*.md"):
                m = re.match(r"^(\d{4})-(.*)", f.stem)
                if m:
                    slug_map[m.group(1)] = f.stem
    return slug_map


def discover_all_sessions() -> dict[str, dict[str, Any]]:
    """Find all session directories and their transcripts across CLI and IDE brain storage."""
    sessions: dict[str, dict[str, Any]] = {}

    for bdir in (CLI_BRAIN_DIR, IDE_BRAIN_DIR):
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

            sessions[conv_id]["mtime"] = max(sessions[conv_id]["mtime"], t_path.stat().st_mtime)

    return sessions


def load_conversation_titles() -> dict[str, str]:
    """Retrieve title metadata from conversation summaries SQLite DBs."""
    titles: dict[str, str] = {}
    for db_path in (CLI_SUMMARIES_DB, IDE_SUMMARIES_DB):
        if db_path.is_file():
            try:
                with sqlite3.connect(db_path) as conn:
                    for row in conn.execute(
                        "SELECT conversation_id, title FROM conversation_summaries"
                    ):
                        if row[0] and row[1]:
                            titles[str(row[0])] = str(row[1])
            except Exception:
                pass
    return titles


def parse_session_transcript(
    conv_id: str,
    files: dict[str, Any],
    slug_map: dict[str, str],
    db_titles: dict[str, str],
) -> dict[str, Any] | None:
    """Parse transcript JSONL to compute steps, tokens, costs, tool calls, and prompt mapping."""
    target_file: Path | None = files.get("full") or files.get("compact")
    if not target_file or not target_file.is_file():
        return None

    total_steps = 0
    input_tokens = 0
    output_tokens = 0
    tool_counts: dict[str, int] = {}
    timestamps: list[str] = []
    user_prompts: list[str] = []
    text_sample: list[str] = []

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

                total_steps += 1
                ts = data.get("created_at")
                if ts:
                    timestamps.append(str(ts))

                source = data.get("source", "")
                step_type = data.get("type", "")
                content = str(data.get("content") or "")
                thinking = str(data.get("thinking") or "")

                if source == "USER" or step_type == "USER_INPUT":
                    user_prompts.append(content)

                if idx < 30:
                    text_sample.append(f"{content} {thinking}")

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
                    if step_type in ("USER_INPUT", "SYSTEM_MESSAGE"):
                        input_tokens += max(1, len(content) // 4)
                    elif step_type == "PLANNER_RESPONSE":
                        output_tokens += max(1, (len(content) + len(thinking)) // 4)

                for tc in data.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    tname = fn.get("name") or tc.get("name") or "unknown_tool"
                    tool_counts[tname] = tool_counts.get(tname, 0) + 1
                    args = fn.get("arguments") or tc.get("args") or ""
                    input_tokens += len(str(args)) // 4

    except Exception as e:
        print(f"Warning: error reading {target_file}: {e}", file=sys.stderr)
        return None

    # Identify prompt mapping
    prompt_num: str | None = None
    combined_user = "\n".join(user_prompts)
    combined_sample = " ".join(text_sample)

    # 1. Known foundational pair programming sessions
    if conv_id in FOUNDATIONAL_SESSIONS:
        prompt_num = FOUNDATIONAL_SESSIONS[conv_id]

    # 2. Explicit daemon prompt kickoff in user messages
    if not prompt_num:
        m_p = re.search(
            r"(?:BlueTeamHarness|Orchestack)\s*-\s*Prompt\s+(\d{4})",
            combined_user,
            re.IGNORECASE,
        )
        if m_p:
            prompt_num = m_p.group(1)

    # 3. Explicit prompt file path in user messages
    if not prompt_num:
        m_q = re.search(r"prompts/(?:queued|implemented)/(\d{4})-", combined_user)
        if m_q:
            prompt_num = m_q.group(1)

    # 4. Explicit branch in user messages or thinking
    if not prompt_num:
        m_b = re.search(
            r"branch\s+['\"]?(?:gemini|orchestack)/(\d{4})-",
            combined_user + " " + combined_sample,
        )
        if m_b:
            prompt_num = m_b.group(1)

    # 5. Fallback: prompt file in initial text sample
    if not prompt_num:
        m_f = re.search(r"(\d{4})-[\w-]+\.md", combined_sample)
        if m_f and m_f.group(1) in slug_map:
            prompt_num = m_f.group(1)

    # 6. Fallback: DB Title if exact match
    if not prompt_num:
        title = db_titles.get(conv_id, "")
        m_t = re.search(r"Prompt\s+(\d{4})", title)
        if m_t:
            cand = m_t.group(1)
            # Only use if not the old 0007 fallback bug or if actually 0007
            if cand != "0007" or "0007" in combined_sample:
                prompt_num = cand

    duration_s = 0.0
    if len(timestamps) >= 2:
        try:
            t0 = datetime.datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            t1 = datetime.datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            duration_s = max(0.0, (t1 - t0).total_seconds())
        except Exception:
            pass

    est_cost = (
        input_tokens * INPUT_COST_PER_MILLION + output_tokens * OUTPUT_COST_PER_MILLION
    ) / 1_000_000.0

    slug = slug_map.get(prompt_num or "", f"other-{conv_id[:8]}")

    return {
        "agent": "antigravity",
        "conv_id": conv_id,
        "prompt_num": prompt_num,
        "prompt_slug": slug,
        "total_steps": total_steps,
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "total_tokens_estimate": input_tokens + output_tokens,
        "estimated_cost_usd": round(est_cost, 4),
        "duration_seconds": round(duration_s, 1),
        "start_time": timestamps[0] if timestamps else None,
        "end_time": timestamps[-1] if timestamps else None,
        "tool_calls_breakdown": tool_counts,
        "mtime": files["mtime"],
        "title": db_titles.get(conv_id, ""),
    }


def collect_telemetry_dataset() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Scan and process all sessions, returning (all_sessions, by_prompt)."""
    slug_map = build_canonical_slug_map()
    db_titles = load_conversation_titles()
    raw_sessions = discover_all_sessions()

    all_parsed: list[dict[str, Any]] = []
    for conv_id, files in raw_sessions.items():
        parsed = parse_session_transcript(conv_id, files, slug_map, db_titles)
        if parsed:
            all_parsed.append(parsed)

    # Also include Claude Code and Codex sessions
    for claude_sess in discover_claude_sessions(CLAUDE_PROJECTS_DIR, slug_map=slug_map):
        all_parsed.append(claude_sess.to_dict())
    for codex_sess in discover_codex_sessions(CODEX_SESSIONS_DIR, slug_map=slug_map):
        all_parsed.append(codex_sess.to_dict())

    all_parsed.sort(key=lambda s: s["mtime"])

    by_prompt: dict[str, list[dict[str, Any]]] = {}
    for s in all_parsed:
        pnum = s.get("prompt_num")
        if pnum:
            if pnum not in by_prompt:
                by_prompt[pnum] = []
            by_prompt[pnum].append(s)

    return all_parsed, by_prompt


def print_prompt_summary_table(
    by_prompt: dict[str, list[dict[str, Any]]],
    slug_map: dict[str, str],
) -> None:
    """Print an ASCII telemetry summary table grouped by prompt."""
    print("\n" + "=" * 105)
    print("📊 Orchestack — Autonomous Daemon & Prompt Telemetry Summary")
    print("=" * 105)
    print(
        f"{'Prompt':<11} | {'Slug / Description':<38} | {'Sess':<4} | "
        f"{'Steps':<5} | {'Tokens':<8} | {'Est Cost':<9} | {'Duration':<8}"
    )
    print("-" * 105)

    tot_prompts = 0
    tot_sessions = 0
    tot_steps = 0
    tot_tokens = 0
    tot_cost = 0.0
    tot_duration = 0.0
    all_tools: dict[str, int] = {}

    for pnum in sorted(slug_map.keys()):
        slug = slug_map[pnum]
        sessions = by_prompt.get(pnum, [])
        if not sessions:
            print(f"Prompt {pnum} | {slug:<38} |    0 |     0 |        0 | $0.0000   |    0.0m")
            continue

        tot_prompts += 1
        sess_count = len(sessions)
        tot_sessions += sess_count

        steps = sum(s["total_steps"] for s in sessions)
        tokens = sum(s["total_tokens_estimate"] for s in sessions)
        cost = sum(s["estimated_cost_usd"] for s in sessions)
        dur_s = sum(s["duration_seconds"] for s in sessions)

        for s in sessions:
            for tname, cnt in s.get("tool_calls_breakdown", {}).items():
                all_tools[tname] = all_tools.get(tname, 0) + cnt

        tot_steps += steps
        tot_tokens += tokens
        tot_cost += cost
        tot_duration += dur_s

        dur_str = f"{dur_s / 60.0:5.1f}m" if dur_s < 3600 else f"{dur_s / 3600.0:4.1f}h"
        print(
            f"Prompt {pnum} | {slug:<38} | {sess_count:4d} | "
            f"{steps:5d} | {tokens:8d} | ${cost:8.4f} | {dur_str:>8}"
        )

    print("=" * 105)
    tot_dur_h = tot_duration / 3600.0
    print(
        f"TOTALS ({tot_prompts} Prompts, {tot_sessions} Sessions): "
        f"{tot_steps:,} steps | ~{tot_tokens:,} tokens | "
        f"${tot_cost:,.4f} USD | {tot_dur_h:.1f} hours"
    )
    avg_cost = tot_cost / tot_prompts if tot_prompts else 0.0
    avg_steps = tot_steps / tot_prompts if tot_prompts else 0.0
    avg_tokens = tot_tokens / tot_prompts if tot_prompts else 0.0
    print(
        f"AVERAGES PER PROMPT: {avg_steps:,.1f} steps | "
        f"~{avg_tokens:,.0f} tokens | ${avg_cost:.4f} USD"
    )
    print("=" * 105)

    if all_tools:
        print("\n🔧 Tool Usage Breakdown (Cumulative Across Prompts):")
        sorted_tools = sorted(all_tools.items(), key=lambda x: x[1], reverse=True)
        for tname, cnt in sorted_tools:
            pct = (cnt / sum(all_tools.values())) * 100.0
            bar = "█" * int(pct / 4)
            print(f"  - {tname:<25}: {cnt:5d} calls ({pct:4.1f}%) {bar}")
    print()


def print_individual_sessions(all_sessions: list[dict[str, Any]]) -> None:
    """Print chronological granular breakdown of all individual sessions."""
    print("\n" + "=" * 115)
    print("📋 Individual Session Runs (Chronological)")
    print("=" * 115)
    print(
        f"{'Start Time (UTC)':<17} | {'Conv UUID':<8} | {'Prompt':<11} | "
        f"{'Steps':<5} | {'Tokens':<8} | {'Est Cost':<9} | {'Duration':<8} | {'Slug / Title'}"
    )
    print("-" * 115)

    for s in all_sessions:
        st = (s.get("start_time") or "Unknown")[:16].replace("T", " ")
        cid = s["conv_id"][:8]
        pnum = f"Prompt {s['prompt_num']}" if s.get("prompt_num") else "Interactive"
        steps = s["total_steps"]
        tokens = s["total_tokens_estimate"]
        cost = s["estimated_cost_usd"]
        dur_s = s["duration_seconds"]
        dur_str = f"{dur_s / 60.0:5.1f}m"
        desc = s.get("prompt_slug") or s.get("title") or "Session"

        print(
            f"{st:<17} | {cid:<8} | {pnum:<11} | {steps:5d} | "
            f"{tokens:8d} | ${cost:8.4f} | {dur_str:>8} | {desc}"
        )
    print("=" * 115)


def export_markdown_table(
    by_prompt: dict[str, list[dict[str, Any]]],
    slug_map: dict[str, str],
) -> str:
    """Generate GitHub-flavored markdown table of prompt telemetry."""
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
        steps = sum(s["total_steps"] for s in sessions)
        tokens = sum(s["total_tokens_estimate"] for s in sessions)
        cost = sum(s["estimated_cost_usd"] for s in sessions)
        dur_s = sum(s["duration_seconds"] for s in sessions)
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
) -> int:
    """Backfill missing completed prompt events into logs/daemon_metrics.jsonl."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    existing_slugs: set[str] = set()
    if DAEMON_METRICS_FILE.is_file():
        try:
            with DAEMON_METRICS_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            d = json.loads(line)
                            if d.get("prompt_slug"):
                                existing_slugs.add(d["prompt_slug"])
                        except Exception:
                            pass
        except Exception as e:
            print(f"Warning reading {DAEMON_METRICS_FILE}: {e}", file=sys.stderr)

    backfilled_count = 0
    with DAEMON_METRICS_FILE.open("a", encoding="utf-8") as f:
        for pnum in sorted(slug_map.keys()):
            slug = slug_map[pnum]
            if slug in existing_slugs:
                continue

            sessions = by_prompt.get(pnum, [])
            if not sessions:
                continue

            total_steps = sum(s["total_steps"] for s in sessions)
            in_tokens = sum(s["input_tokens_estimate"] for s in sessions)
            out_tokens = sum(s["output_tokens_estimate"] for s in sessions)
            total_tokens = sum(s["total_tokens_estimate"] for s in sessions)
            cost = sum(s["estimated_cost_usd"] for s in sessions)
            dur_s = sum(s["duration_seconds"] for s in sessions)

            all_tools: dict[str, int] = {}
            for s in sessions:
                for tname, cnt in s.get("tool_calls_breakdown", {}).items():
                    all_tools[tname] = all_tools.get(tname, 0) + cnt

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and aggregate historical multi-agent session telemetry, steps, "
            "tokens, and cost."
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Target repository root path.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default=None,
        help="Output format (markdown or json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output file path.",
    )
    parser.add_argument(
        "--sessions",
        "-s",
        action="store_true",
        help="Display detailed individual session breakdown.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill historical completed prompt metrics into logs/daemon_metrics.jsonl.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw telemetry data as formatted JSON.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print summary formatted as a GitHub Markdown table.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Print summary in CSV format.",
    )
    parser.add_argument(
        "--update-stats",
        action="store_true",
        help="Update docs/IMPLEMENTATION_STATS.md with aggregated per-phase implementation stats.",
    )
    parser.add_argument(
        "--stats-file",
        type=Path,
        default=None,
        help="Optional output path for IMPLEMENTATION_STATS.md when using --update-stats.",
    )
    args = parser.parse_args()

    if args.update_stats or args.stats_file is not None:
        repo_path = (args.repo or REPO_ROOT).resolve()
        stats_target = args.stats_file.expanduser().resolve() if args.stats_file else None
        written = update_implementation_stats(repo_root=repo_path, output_path=stats_target)
        if (
            args.format is None
            and args.output is None
            and not args.json
            and not args.markdown
            and not args.csv
        ):
            print(f"Updated implementation stats at {written}")
            return

    if args.format is not None or args.repo is not None or args.output is not None:
        repo_path = (args.repo or REPO_ROOT).resolve()
        slug_map = build_canonical_slug_map()
        sessions, by_prompt_objs = collect_multi_agent_telemetry(repo_root=repo_path)
        fmt = args.format or ("json" if args.json else "markdown")
        if fmt == "json":
            rendered = format_telemetry_json(sessions, by_prompt_objs, slug_map)
        else:
            rendered = format_telemetry_markdown(sessions, by_prompt_objs, slug_map)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return

    slug_map = build_canonical_slug_map()
    all_sessions, by_prompt = collect_telemetry_dataset()

    if args.json:
        payload = {
            "summary_by_prompt": {
                pnum: {
                    "slug": slug_map.get(pnum, f"prompt-{pnum}"),
                    "sessions_count": len(sessions),
                    "total_steps": sum(s["total_steps"] for s in sessions),
                    "input_tokens_estimate": sum(s["input_tokens_estimate"] for s in sessions),
                    "output_tokens_estimate": sum(s["output_tokens_estimate"] for s in sessions),
                    "total_tokens_estimate": sum(s["total_tokens_estimate"] for s in sessions),
                    "estimated_cost_usd": round(sum(s["estimated_cost_usd"] for s in sessions), 4),
                    "duration_seconds": round(sum(s["duration_seconds"] for s in sessions), 1),
                }
                for pnum, sessions in sorted(by_prompt.items())
            },
            "sessions": all_sessions,
        }
        print(json.dumps(payload, indent=2))
        return

    if args.markdown:
        print(export_markdown_table(by_prompt, slug_map))
        return

    if args.csv:
        print("prompt,slug,sessions,steps,tokens,cost_usd,duration_min")
        for pnum in sorted(slug_map.keys()):
            sessions = by_prompt.get(pnum, [])
            if not sessions:
                continue
            slug = slug_map[pnum]
            cnt = len(sessions)
            steps = sum(s["total_steps"] for s in sessions)
            tokens = sum(s["total_tokens_estimate"] for s in sessions)
            cost = sum(s["estimated_cost_usd"] for s in sessions)
            dur_m = round(sum(s["duration_seconds"] for s in sessions) / 60.0, 1)
            print(f"{pnum},{slug},{cnt},{steps},{tokens},{cost:.4f},{dur_m}")
        return

    # Default display
    print_prompt_summary_table(by_prompt, slug_map)

    if args.sessions:
        print_individual_sessions(all_sessions)

    if args.backfill:
        count = backfill_daemon_metrics_file(by_prompt, slug_map)
        print(f"✅ Successfully backfilled {count} prompt records into {DAEMON_METRICS_FILE}.\n")


if __name__ == "__main__":
    main()

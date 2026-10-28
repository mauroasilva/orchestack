# 0007 — Phase Implementation Stats Tracking Learnings

## Summary

- Added `RepoConfig.implementation_stats_path` in `src/orchestack/config.py` pointing to `<repo_root>/docs/IMPLEMENTATION_STATS.md`.
- Implemented `PhaseImplementationStats`, `TelemetryReport`, `format_duration`, `parse_duration_str`, `parse_existing_implementation_stats`, `render_implementation_stats_markdown`, and `update_implementation_stats` in `src/orchestack/telemetry/reporter.py` (and re-exported via `src/orchestack/telemetry/__init__.py`) to aggregate per-phase duration, token counts, and estimated USD cost across initial implementation and PR/MR review turns while preserving historical rows.
- Extended `orchestack telemetry` (`src/orchestack/cli.py`) and `scripts/session_telemetry_report.py` with `--update-stats` and `--stats-file <path>`, and wired `scripts/daemon_runner.py` (`record_daemon_metrics()` and `main()` Step 6) to automatically refresh `docs/IMPLEMENTATION_STATS.md`.
- Generated `docs/IMPLEMENTATION_STATS.md` with per-phase telemetry for phases `0001` through `0007`, updated `docs/PLAN.md` (§4, §9, §10, §13), and added unit tests in `tests/test_implementation_stats.py`.

## Key Decisions

- **Historical Row Preservation Across Machines & Sandboxes**: `render_implementation_stats_markdown()` and `update_implementation_stats()` parse any existing `docs/IMPLEMENTATION_STATS.md` table (`parse_existing_implementation_stats()`) before merging newly discovered session telemetry. If historical sessions for earlier phases (`0001`–`0006`) are absent on the current machine or inaccessible inside a sandbox, their previously recorded duration, token count, and cost are preserved rather than overwritten with zeros. When new sessions (such as PR/MR review or auto-heal turns) increase a phase's token count or cost, the row is updated and the `**Total**` row is recomputed.
- **Canonical Slug Resolution & Out-of-Repo Filtering**: `build_canonical_slug_map()` reads `prompts/implemented/*.md`, `prompts/queued/*.md`, and the `docs/PLAN.md` §13 delivery table. When canonical repository slugs are present, unmapped out-of-repo sessions (`other-*`) and `BlueTeamHarness`-only daemon transcripts are excluded so `docs/IMPLEMENTATION_STATS.md` only tracks phases belonging to the active repository.
- **Active Step Duration Calculation**: `_compute_duration_from_timestamps()` sums consecutive step intervals (`<= 1800s`) across multi-step transcripts (while preserving simple `t1 - t0` span for 2-timestamp synthetic records), preventing multi-hour or multi-day idle gaps between turns from inflating phase implementation times.

## Gotchas & Edge Cases

- **Sandbox Access to `~/.gemini/antigravity*/brain`**: Directory listing on `~/.gemini/antigravity/brain` is blocked inside the standard macOS seatbelt sandbox, whereas the active conversation's own transcript directory (`~/.gemini/antigravity/brain/<conv-id>/.system_generated/logs/transcript_full.jsonl`) is readable directly by path. Adding a direct path fallback in `discover_antigravity_sessions()` for known `FOUNDATIONAL_SESSIONS` UUIDs allows `session_telemetry_report.py --update-stats` to extract live telemetry for the active conversation right inside the standard sandbox.
- **Tool Output Token Estimation in Antigravity Transcripts**: Antigravity step logs record tool execution outputs in `GENERIC` steps and context compactions in `CHECKPOINT` steps without a `usage` object. Counting character length (`len(content) // 4`) across `USER_INPUT`, `SYSTEM_MESSAGE`, `GENERIC`, and `CHECKPOINT` steps provides realistic input token estimates when explicit `usage` metadata is absent.

## Verification

- Ran `make fmt && make check` (`ruff check`, `ruff format --check`, `mypy --strict src tests`, `pytest`, and `tests/docs/test_indexes.py`).
- All 82 baseline tests in `scripts/tests/test_daemon_runner.py` and 116 package/docs tests in `tests/` (198 total tests) pass.

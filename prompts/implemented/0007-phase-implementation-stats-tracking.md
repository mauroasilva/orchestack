# 0007 — Phase Implementation Stats Tracking (`docs/IMPLEMENTATION_STATS.md`)

## Read first
- `docs/PROTOCOL.md` (§1, §3, §3.1 `B1`–`B8`, §4, §5)
- `docs/PLAN.md` (§2 `D2`, `D10`, `D11`, `D12`, §4, §8, §9, §10, §12, §13)
- `docs/learnings/` (Read the `## Summary` section of all existing files before starting)
- `src/orchestack/telemetry/reporter.py`, `src/orchestack/config.py`, `src/orchestack/cli.py`, `scripts/daemon_runner.py`, and `scripts/session_telemetry_report.py`

## Objective
Add automated tracking and reporting of per-phase implementation time and total token cost in `docs/IMPLEMENTATION_STATS.md`. For every phase, Orchestack should aggregate session duration and token cost across all implementation and review/heal turns and record them in `docs/IMPLEMENTATION_STATS.md`, which must contain a short description of what the document contains followed by a Markdown table with each phase, its implementation time, and its total token cost.

## Deliverables

1. **Repository Configuration (`src/orchestack/config.py`)**
   - Add an `implementation_stats_path` property to `RepoConfig` returning `self.repo_root / "docs" / "IMPLEMENTATION_STATS.md"`.

2. **Implementation Stats Generator & Updater (`src/orchestack/telemetry/reporter.py` & `src/orchestack/telemetry/__init__.py`)**
   - Implement `render_implementation_stats_markdown(report: TelemetryReport, repo_root: Path | None = None) -> str` and `update_implementation_stats(repo_root: Path | None = None, output_path: Path | None = None, ...) -> Path` in `src/orchestack/telemetry/reporter.py` (and export them in `src/orchestack/telemetry/__init__.py`):
     - Discover all phases from `prompts/implemented/`, `prompts/queued/`, and `docs/PLAN.md` §13 (`build_canonical_slug_map`), merging telemetry sessions from `collect_multi_agent_telemetry()` (and `.daemon/metrics.jsonl` fallback if raw session transcripts are unavailable on the current machine).
     - Preserve existing recorded non-zero rows from an already-existing `docs/IMPLEMENTATION_STATS.md` when local machine logs for older phases are not present, while updating any phase that has newer/higher telemetry data.
     - Render `docs/IMPLEMENTATION_STATS.md` with:
       1. A clear title (`# Implementation Statistics`) and a short description of what the document contains (how implementation time and total token cost are recorded per phase across implementation and PR/MR review turns).
       2. A Markdown table listing each phase in ascending numerical order with columns for the phase identifier (`Phase`), implementation time (`Implementation Time`, formatted via `format_duration` as `Xm Ys` / `Xh Ym Zs` along with raw seconds or clean human-readable duration), and total token cost (`Total Token Cost`, showing total tokens and/or estimated USD cost `$X.XX` formatted consistently), plus a final `**Total**` summary row.

3. **CLI & Daemon Integration (`src/orchestack/cli.py`, `scripts/session_telemetry_report.py`, `scripts/daemon_runner.py`)**
   - Add `--update-stats` (and optional `--stats-file PATH`) to `orchestack telemetry` in `src/orchestack/cli.py` and `scripts/session_telemetry_report.py` so users or scripts can regenerate `docs/IMPLEMENTATION_STATS.md` on demand.
   - Hook `update_implementation_stats` into `scripts/daemon_runner.py` (`record_daemon_metrics` / post-session step before `commit_all_changes`) so that after each phase session completes, `docs/IMPLEMENTATION_STATS.md` is automatically refreshed before changes are staged and committed.
   - Update `docs/PLAN.md` (§4, §9, §10, §13) to document `docs/IMPLEMENTATION_STATS.md` and `--update-stats`.

4. **Generate `docs/IMPLEMENTATION_STATS.md` & Unit Tests (`tests/test_implementation_stats.py`)**
   - Generate and commit `docs/IMPLEMENTATION_STATS.md` populated with the recorded stats for phases `0001` through `0007`.
   - Create `tests/test_implementation_stats.py` (with the SPDX license header from `docs/LICENSE-HEADER.md`) testing:
     - Markdown rendering of `docs/IMPLEMENTATION_STATS.md` ( verifying the short description, phase rows, implementation time formatting, total token cost formatting, and totals row).
     - Preserving existing phase stats in `docs/IMPLEMENTATION_STATS.md` when local session logs are absent for historical phases while updating newly executed phases.
     - `orchestack telemetry --update-stats` CLI invocation writing `docs/IMPLEMENTATION_STATS.md`.
     - Daemon runner integration ensuring `record_daemon_metrics` / phase completion updates `docs/IMPLEMENTATION_STATS.md` without breaking any of the 82 baseline tests in `scripts/tests/test_daemon_runner.py`.

## Acceptance Criteria
- [ ] `docs/IMPLEMENTATION_STATS.md` exists, contains a short description of what it contains, and includes a Markdown table with each phase, its implementation time, and its total token cost.
- [ ] `orchestack telemetry --update-stats` and `scripts/daemon_runner.py` automatically update `docs/IMPLEMENTATION_STATS.md`.
- [ ] All 82 existing unit tests in `scripts/tests/test_daemon_runner.py` pass without modification (`B8`).
- [ ] New unit tests in `tests/test_implementation_stats.py` pass under `pytest`.
- [ ] `make fmt && make check` passes with zero errors (`ruff check`, `ruff format --check`, `mypy --strict src tests`, `pytest`, and `tests/docs/test_indexes.py`).
- [ ] `docs/PLAN.md` §13 status for `0007` is updated to `Implemented`, `docs/learnings/0007-phase-implementation-stats-tracking-learnings.md` is created, and `docs/PR_DESCRIPTION.md` is written.

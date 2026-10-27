# 0000 — Baseline import from BlueTeamHarness — Learnings

## Summary
- **What shipped**: Imported the production-hardened `scripts/daemon_runner.py` (4,290 lines), `scripts/tests/test_daemon_runner.py` (2,553 lines, 82 unit tests), `scripts/session_telemetry_report.py` (596 lines), and `scripts/run_daemon.sh` as the Day-0 baseline of Orchestack.
- **Key packages/files**: `scripts/daemon_runner.py`, `scripts/tests/test_daemon_runner.py`, `scripts/session_telemetry_report.py`, `docs/PROTOCOL.md`, `docs/PLAN.md`.
- **Key interfaces/types added or changed**: Baseline functions in `scripts/daemon_runner.py` remain intact as a backward-compatible facade while modularization into `src/orchestack/` proceeds across Phases `0001`–`0006`.
- **Decisions made/affected**: D1–D12 in `docs/PLAN.md`.
- **Gotchas / non-obvious constraints**: All 82 unit tests in `scripts/tests/test_daemon_runner.py` test real production bugfixes (stale transcript timestamps, turn-scoped permission denials, `-impl` branch collision handling, CI billing/quota local test fallback, single-run DoD, `[bot]`/approved review filtering, git staging safety pre-checks). Never delete or weaken these tests; keep `scripts/daemon_runner.py` re-exporting symbols or delegating to `src/orchestack/` so all 82 baseline tests stay green at every phase.
- **What the NEXT session must know**: Start with `prompts/queued/0001-core-package-and-repo-config.md`. Build `src/orchestack/config.py` (`RepoConfig`) and wire `scripts/daemon_runner.py` to use it while keeping module-level defaults backward-compatible for existing tests.

## Details

### Battle-Tested Fixes Imported in Baseline
1. **Stale transcript filtering (`c13a733`)**: `get_latest_conversation_id()`, `get_session_transcript_status()`, and `check_recent_transcript_denial()` accept `since_timestamp` and use `_normalize_db_timestamp()` so old conversations do not pollute live status or permission checks.
2. **Turn-scoped permission denials (`a485f34`)**: `execute_prompt_session()` captures `cli_log_offset` and `turn_start_time` per turn, handles `cli.log` truncation, and extracts multiline commands and `read_url` targets.
3. **False `MERGED` loop & `-impl` branch collisions (`87684f2`, `e5a8bf3`)**: Verifies `is_prompt_merged_into_origin_main()` and `git merge-base --is-ancestor`, renames branches to `-impl` when a prior PR on the same branch name was merged without the prompt being in `origin/main`, and resolves `-impl` and 4-digit prefixes in `find_prompt_path_for_branch()`.
4. **Infinite review loop & circuit breakers (`e5a8bf3`, `a485f34`)**: Saves processed comment IDs even when a prompt path cannot be resolved for a branch, ignores `[bot]` and `DAEMON_TAG`, marks `APPROVED` reviews as seen, truncates >60k char summaries, and enforces `max_auto_heal_attempts`.
5. **CI billing/quota exhaustion fallback (`fb76552`)**: `is_ci_billing_or_quota_failure()` and `fetch_latest_failed_ci_run()` detect billing/spending-limit failures (including fallback to `gh run view`) and run `verify_local_tests_for_pr()` locally.
6. **Headless CLI isolation & single-run DoD (`cb99f34`)**: Never touches `~/.gemini/antigravity` IDE GUI state, never passes `--project`, and runs `check_definition_of_done()` once inside `execute_prompt_session()`.
7. **Git staging safety & `PR_DESCRIPTION.md` (`4b4cb36`)**: `verify_git_staging_safety()` blocks accidentally ignored files in protected directories while allowing `docs/PR_DESCRIPTION.md` and deleting stale `docs/PR_DESCRIPTION.md` on fresh branch setup.

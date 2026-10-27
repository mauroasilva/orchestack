# Orchestack — Session Protocol

> **Every implementation session MUST read this file first, in full.** It is
> short by design. It tells a fresh coding agent session (Antigravity, Claude Code,
> Codex, etc.) exactly how to pick up work, implement one prompt, and hand off
> cleanly to the next session without regressing existing battle-tested behavior.

To start a session, see `docs/KICKOFF.md` for the exact kickoff prompt to paste.

---

## 0. TL;DR of a session

1. Read this protocol (`docs/PROTOCOL.md`) in full.
2. Navigate `docs/PLAN.md`, the architecture source of truth. Start at its
   section index, then read the sections and decisions your prompt names (§1).
3. Read the **Summary block** of each file in `docs/learnings/` (especially
   `0000-baseline-import-from-bth-learnings.md`). Open a full learnings file
   only when it is relevant to your prompt.
4. Take the **lowest-numbered** prompt in `prompts/queued/`, unless the user
   names a specific one. That prompt is your entire task.
5. Work on the branch the session was given (§2).
6. Implement the prompt while preserving **all Mandatory Regression Invariants (§3.1)**.
   Meet the Definition of Done (§4).
7. Move the prompt file from `prompts/queued/` to `prompts/implemented/`, with an
   unchanged name, **in the same PR**.
8. Write a learnings file to `docs/learnings/` (§5) and author `docs/PR_DESCRIPTION.md` (§8).
9. Open a PR/MR (or let the daemon open it). Iterate with the user until green and mergeable.

---

## 1. Startup reading order (context budget)

Read in this order, and **stop reading as soon as you have what you need**:

1. `docs/PROTOCOL.md` — this file, always, in full.
2. `docs/PLAN.md` — the architecture, never re-derived. Navigate it:
   - `grep -n "^#\{1,3\} " docs/PLAN.md` prints the section index. Start there.
   - Read the sections and decisions your prompt's `## Read first` names.
   - §2's **register** states every architectural decision (`D1`–`D12`).
3. `docs/learnings/*` — read **only the Summary block** of each file first.
4. Your target prompt in `prompts/queued/`.

---

## 2. Branching

- **Use the branch the session was given** (e.g. `gemini/0001-...`, `claude/0001-...`, `codex/0001-...`).
- **When the name is yours to choose**, use `<agent>/NNNN-short-description` matching the prompt.
- Branch off the **latest default branch**:
  `git fetch origin main && git checkout -B <branch> origin/main`.
- **Never push to `main`.** Never stack new work on merged history.

---

## 3. Doing the work

- **Scope discipline.** Implement exactly what the prompt specifies. If you find
  work that belongs to a later phase, note it in your learnings file or add a
  queued prompt (§6).
- **Follow the plan.** Match `docs/PLAN.md`: package layout (`src/orchestack/`),
  interfaces (`RepoConfig`, `ForgeProvider`, `AgentBackend`), and decisions (`D1`–`D12`).
- **Keep the plan's indexes fresh.** If you change a decision or phase, update
  `docs/PLAN.md` §2 or §13 in the same PR (`tests/docs/test_indexes.py` enforces this).
- **Add the SPDX header** (`docs/LICENSE-HEADER.md`) to every new source and test file.
- **No live network/CLI calls in unit tests.** All tests must use mocks/fakes by default.

### 3.1 Mandatory Regression Invariants (NON-NEGOTIABLE)

Orchestack was seeded from the production-hardened `scripts/daemon_runner.py`
(4,290 lines) and `scripts/tests/test_daemon_runner.py` (82 unit tests). As you
modularize `scripts/daemon_runner.py` into `src/orchestack/` across Phases
`0001`–`0006`, **you MUST preserve 100% of the following battle-tested fixes and
keep all 82 baseline unit tests passing**:

1. **Stale Transcript & Session Status Filtering**:
   - `get_latest_conversation_id()`, `get_session_transcript_status()`, and
     `check_recent_transcript_denial()` MUST filter by `since_timestamp` (with a
     2.0s clock-skew tolerance) and normalize s/ms/us/ns DB timestamps via
     `_normalize_db_timestamp()`. If `CLI_BRAIN_DIR` has a newer mtime than the
     DB entry, prefer the newer brain directory.
2. **Turn-Scoped Permission Denials & Log Offset Tracking**:
   - Before each agent invocation turn, record `cli_log_offset` and
     `turn_start_time`. Only inspect `cli.log` from `since_offset=cli_log_offset`
     (handling log truncation gracefully) and transcripts from
     `since_timestamp=turn_start_time` so old denials never re-trigger the
     interactive security gate on subsequent turns.
   - Preserve extraction of multiline denied commands, quoted targets, tool call
     fallbacks (`run_command`, `write_to_file`, `replace_file_content`,
     `read_url_content`), and `read_url` domain/URL rules.
3. **False `MERGED` Loop & Branch Reuse Collision Prevention**:
   - Never trust a forge's `MERGED` PR/MR state alone. Cross-check
     `is_prompt_merged_into_origin_main()` (checking both exact slug, stripped
     `-impl` suffix, and 4-digit prefix in `origin/<base>:prompts/implemented/`)
     AND `git merge-base --is-ancestor` AND a clean working tree.
   - If a branch name already has a `MERGED` PR/MR on the forge while the prompt
     is not yet in `origin/<base>` (or has unmerged commits ahead), automatically
     rename/use `<branch>-impl` (`setup_prompt_branch()`, `ensure_pr_opened()`).
   - `find_prompt_path_for_branch()` must strip `<agent>/` prefixes and `-impl`
     suffixes and fall back to matching the 4-digit `NNNN-` prefix across both
     `implemented/` and `queued/`.
4. **Infinite Review Loop Prevention & Circuit Breakers**:
   - Filter out bot comments (`[bot]`, `github-actions`, `codecov`, GitLab system
     notes/bots) and comments containing `DAEMON_TAG`.
   - Mark `APPROVED` reviews as processed immediately so they never trigger a
     feedback session.
   - If `find_prompt_path_for_branch()` cannot resolve a prompt file for an open
     PR/MR, still save the unhandled comment IDs as processed so the daemon does
     not loop infinitely on the same comments.
   - Truncate agent summaries >60,000 characters before posting PR/MR comments.
   - Enforce `max_auto_heal_attempts` circuit breakers independently for CI
     auto-healing and review feedback retries; post a circuit-breaker notice on
     the PR/MR and halt cleanly when tripped.
5. **CI Billing / Quota Exhaustion Fallback**:
   - Detect cloud CI billing/spending-limit/quota failures (both GitHub Actions
     and GitLab CI shared runner quota messages; falling back to run/pipeline
     summary view when job logs are missing).
   - When detected, verify the branch locally (`verify_local_tests_for_pr()`) and
     mark the run as locally green (`ci-billing-local-green-<id>`) instead of
     wasting agent turns trying to fix code for a billing failure.
6. **Pure Headless Isolation & Single-Run Definition of Done**:
   - Never mutate or symlink into IDE GUI directories (`~/.gemini/antigravity`)
     and never pass `--project` to `agy`.
   - Run `check_definition_of_done()` **once** inside `execute_prompt_session()`
     (even when `custom_prompt` is used for review/CI feedback) and **do not**
     run a duplicate `make check` in `main()`, `address_pr_feedback()`, or
     `address_ci_failure()`.
   - If `check_definition_of_done()` fails or times out (exit code 124 after
     600s), feed the failure output back into the agent session (`-c` / resume)
     up to `max_turns`.
7. **Git Staging Safety Pre-Check & `PR_DESCRIPTION.md` Hygiene**:
   - Before committing, `verify_git_staging_safety()` must check both
     `git ls-files --others --ignored --exclude-standard` and untracked files in
     protected directories (`src`, `tests/fixtures`, `docs`) while allowing
     `BENIGN_IGNORED_PATTERNS` (including `docs/PR_DESCRIPTION.md`).
   - Delete stale `docs/PR_DESCRIPTION.md` when creating a fresh branch in
     `setup_prompt_branch()`.
8. **Cloud-Sync (`OneDrive`), Offline Resilience & Interactive Recheck**:
   - Remove stale `.git/**/*.lock` files before fetching in `update_main()`.
   - Include both logical and `.resolve()` paths in sandbox `write_file` rules.
   - Wait indefinitely with exponential backoff on network disconnect
     (`wait_for_network_connectivity()`) and resume the session once online.
   - Support pressing `Enter` on an interactive terminal (`_interruptible_sleep()`)
     to trigger an immediate PR/MR recheck, and track both `total` cumulative
     runtime across resumed turns (`extract_previous_cumulative_time()`) and
     per-turn agent time.

---

## 4. Definition of Done (all must hold before requesting merge)

- [ ] The prompt's stated deliverables and acceptance criteria are met.
- [ ] All 82 baseline unit tests in `scripts/tests/test_daemon_runner.py` AND all
      new unit tests in `tests/` pass.
- [ ] `make check` passes locally: `ruff check`, `ruff format --check`,
      `mypy --strict src tests`, `pytest`, and `tests/docs`.
- [ ] Every new source file carries the SPDX header (`docs/LICENSE-HEADER.md`).
- [ ] `docs/PLAN.md` is updated if the architecture changed, and its §2 register
      and §13 phase table pass `tests/docs/test_indexes.py`.
- [ ] The prompt file has moved `prompts/queued/` → `prompts/implemented/`
      (same filename) in this PR.
- [ ] A learnings file has been added to `docs/learnings/NNNN-short-description-learnings.md` (§5).
- [ ] `docs/PR_DESCRIPTION.md` has been written with a comprehensive summary (§8).
- [ ] All phase deliverables are staged and committed with a Conventional Commit message (§8), leaving `git status` clean.

---

## 5. Learnings file (the hand-off to future sessions)

Before concluding the session, add `docs/learnings/NNNN-short-description-learnings.md`
matching the prompt you implemented. It MUST begin with a **Summary block**:

```markdown
# 0001 — Core package and RepoConfig — Learnings

## Summary
- **What shipped**: <1–3 lines>
- **Key packages/files**: <paths>
- **Key interfaces/types added or changed**: <names>
- **Decisions made/affected**: <D-ids>
- **Gotchas / non-obvious constraints**: <1–3 lines>
- **What the NEXT session must know**: <1–3 lines>

## Details
<Everything else: rationale, how to extend, test notes, follow-ups.>
```

---

## 6. Prompt numbering invariants

Prompts are named `NNNN-short-description.md`, with a **4-digit** zero-padded
number giving the implementation order:
- No number repeats across `queued/` or `implemented/`.
- Implemented names are frozen.
- Queued numbers are contiguous and strictly above the highest implemented number.

---

## 7. Writing a self-contained prompt

Every prompt in `prompts/queued/` must include:
- A `## Read first` section referencing `docs/PROTOCOL.md` and specific `§`
  sections and `D` decisions of `docs/PLAN.md`.
- Clear objective, scope, exact file paths, interface signatures, acceptance
  criteria, and required unit tests.

---

## 8. Commits & PR Description

- **Commit style**: Conventional Commits (`feat(config): ...`, `fix(gitlab): ...`).
- **PR Description**: Author `docs/PR_DESCRIPTION.md` before finishing your
  session detailing:
  1. Summary of what was implemented
  2. Key deliverables, packages, and interfaces added or updated
  3. Issues, obstacles, and edge cases encountered and how they were resolved
  4. Testing and verification performed, confirming `make check` passes locally
  5. Complete Definition of Done checklist

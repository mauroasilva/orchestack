# Orchestack — Session Kickoff Prompts

## Option A: Interactive / Single-Session Kickoff Prompt

Paste this into a fresh coding agent session (Antigravity IDE/CLI, Claude Code, or Codex) opened in `/Users/mauro.silva/Code/orchestack`:

```text
Orchestack — Session Kickoff:
Read docs/PROTOCOL.md in full and follow it strictly.
Then inspect the section index of docs/PLAN.md, read the Summary blocks in docs/learnings/ (especially 0000-baseline-import-from-bth-learnings.md), and pick the lowest-numbered prompt in prompts/queued/.
Implement ONLY that prompt in this session.
CRITICAL: Preserve all Mandatory Regression Invariants in docs/PROTOCOL.md §3.1 and ensure all 82 baseline unit tests in scripts/tests/test_daemon_runner.py plus your new unit tests in tests/ and `make check` pass before concluding.
Before finishing, move the prompt to prompts/implemented/, write its learnings file in docs/learnings/, and write docs/PR_DESCRIPTION.md.
```

---

## Option B: Autonomous Daemon Self-Hosting (Dogfooding!)

Because Orchestack is seeded with the working `scripts/run_daemon.sh` and `scripts/daemon_runner.py`, once your git remote (`origin`) is configured on GitHub (or GitLab after Prompt `0003`), Orchestack can autonomously process its own prompt queue (`prompts/queued/0001-*.md` through `0008-*.md`):

```bash
./scripts/run_daemon.sh
```

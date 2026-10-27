# Orchestack

**Orchestack** is an autonomous multi-repository prompt queue runner, CI self-healer, and PR/MR review feedback daemon.

Point `orchestack` at any Git repository hosted on **GitHub** (`gh`) or **GitLab** (`glab`), choose your coding agent (**Google Antigravity `agy`**, **Claude Code `claude`**, or **OpenAI Codex `codex`**), and Orchestack will autonomously:

1. **Reconcile Repository & Branch State**: Detect in-progress branches, unmerged work, or open PRs/MRs on startup without losing uncommitted work or falling into false-merge loops.
2. **Execute Queued Prompts in Sandboxed Isolation**: Pick the lowest-numbered prompt in `prompts/queued/`, create a clean branch, run the coding agent in least-privilege sandbox mode with an interactive security gate, and verify the repository's Definition of Done (`check_cmd`, learnings handoff, PR description).
3. **Open Rich Pull / Merge Requests**: Push the branch, verify git staging safety (preventing accidental `.gitignore` omissions in protected directories), and open a comprehensive PR (GitHub) or MR (GitLab).
4. **Auto-Heal CI Failures (with Quota/Billing Fallback)**: Monitor GitHub Actions or GitLab CI pipelines, automatically feed failed job logs back into the agent session (`-c` / `resume --last`) to fix broken builds, or fall back to local test verification if cloud CI minutes/quotas are exhausted.
5. **Address Reviewer Feedback & Inline Diff Threads**: Poll PR/MR review comments, review summaries, and inline diff discussions, resume the agent session to implement fixes or answer technical questions, push commits, and post a structured response back to the PR/MR thread—guarded by configurable circuit breakers.
6. **Cross-Agent Telemetry & Cost Reporting**: Track steps, tool calls, token usage, and estimated USD cost across Antigravity (`agy`), Claude Code (`claude`), and Codex (`codex`).

---

## Quick Start

```bash
# Install dependencies
uv sync

# Run full verification suite (lint, format, strict mypy, 82+ unit tests, doc index checks)
make check

# Run the daemon on the current repository (or pass --repo /path/to/repo)
./scripts/run_daemon.sh
```

## Documentation & Architecture

- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — Session protocol and **Non-Negotiable Regression Invariants**.
- [`docs/PLAN.md`](docs/PLAN.md) — Architecture source of truth, decision register (`D1`–`D12`), and phased delivery roadmap (`0001`–`0006`).
- [`docs/KICKOFF.md`](docs/KICKOFF.md) — Kickoff prompts for interactive sessions or autonomous daemon execution.
- [`docs/learnings/`](docs/learnings/) — Durable session hand-offs and historical bugfix context.

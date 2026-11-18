# Orchestack

**Orchestack** is an autonomous multi-repository prompt queue runner, CI self-healer, and PR/MR review feedback daemon.

Point `orchestack` at any Git repository hosted on **GitHub** (`gh`) or **GitLab** (`glab`), choose your coding agent (**Google Antigravity `agy`**, **Claude Code `claude`**, or **OpenAI Codex `codex`**), and Orchestack will autonomously:

1. **Reconcile Repository & Branch State**: Detect in-progress branches, unmerged work, or open PRs/MRs on startup without losing uncommitted work or falling into false-merge loops.
2. **Execute Queued Prompts in Sandboxed Isolation**: Pick the lowest-numbered prompt in `prompts/queued/`, create a clean branch, run the coding agent in least-privilege sandbox mode with an interactive security gate, and verify the repository's Definition of Done (`check_cmd`, learnings handoff, PR description).
3. **Open Rich Pull / Merge Requests**: Push the branch, verify git staging safety (preventing accidental `.gitignore` omissions in protected directories), and open a comprehensive PR (GitHub) or MR (GitLab).
4. **Auto-Heal CI Failures (with Quota/Billing Fallback)**: Monitor GitHub Actions or GitLab CI pipelines, automatically feed failed job logs back into the agent session (`-c` / `resume --last`) to fix broken builds, or fall back to local test verification if cloud CI minutes/quotas are exhausted.
5. **Address Reviewer Feedback & Inline Diff Threads**: Poll PR/MR review comments, review summaries, and inline diff discussions, resume the agent session to implement fixes or answer technical questions, push commits, and post a structured response back to the PR/MR thread—guarded by configurable circuit breakers.
6. **Cross-Agent Telemetry & Cost Reporting**: Track steps, tool calls, token usage, and estimated USD cost across Antigravity (`agy`), Claude Code (`claude`), and Codex (`codex`), plus per-phase implementation time and cost in [`docs/IMPLEMENTATION_STATS.md`](docs/IMPLEMENTATION_STATS.md).
7. **Three-State Repository Bootstrap & PR Interview (`Phase 0000`)**: Support queue-ready, `docs/BRIEF.md`-only, and empty repositories via a `Phase 0000` bootstrap PR and iterative numbered-question PR-comment user interviews.

---

## Prerequisites

1. **Python 3.12+** and [**`uv`**](https://docs.astral.sh/uv/) for dependency and environment management.
2. **Git & Forge CLI** — Your target repository's `origin` remote must point to **GitHub** or **GitLab**, with the matching CLI installed and authenticated:
   - **GitHub**: `gh auth login` (verify with `gh auth status`)
   - **GitLab**: `glab auth login` (verify with `glab auth status`)
3. **Coding Agent CLI** — At least one supported agent binary available on your `PATH`:
   - Google Antigravity (`agy`), Claude Code (`claude`), or OpenAI Codex (`codex`).

---

## Installation

Clone Orchestack and install its dependencies:

```bash
git clone <orchestack-repo-url>
cd orchestack
uv sync
```

*(Optional)* Install the `orchestack` CLI onto your `PATH` so you can invoke `orchestack` directly from any directory instead of prefixing commands with `uv run`:

```bash
uv tool install -e .
```

> **Tip:** All examples below use `uv run orchestack ...` from the Orchestack checkout. If you ran `uv tool install -e .`, you can shorten `uv run orchestack` to `orchestack`.

---

## Getting Started (First-Time Guide)

### Step 1: Initialize or Prepare Your Target Repository

You can point Orchestack at a target Git repository in **any of three starting states**:

- **Queue-ready**: Already contains numbered prompts in `prompts/queued/NNNN-slug.md` (e.g., `0001-initial-api.md`).
- **Brief-only**: Contains a `docs/BRIEF.md` describing what you want built. Orchestack automatically runs `Phase 0000` to draft `docs/PLAN.md` and generate queued prompts, interviewing you via PR/MR comments if anything needs clarification.
- **Empty repository**: Even a brand-new repository works—Orchestack opens a `Phase 0000` bootstrap PR/MR and conducts a numbered-question interview in PR comments to define your architecture and prompt queue.

To scaffold `.orchestack.toml`, `prompts/{queued,implemented}/`, `docs/learnings/`, and `.gitignore` entries in a target repository:

```bash
uv run orchestack init --repo /path/to/target-repo --agent agy --forge auto
```

### Step 2: Run Pre-Flight Diagnostics (`doctor` & `status`)

Before launching the daemon, verify that your `.orchestack.toml`, `origin` remote, forge authentication (`gh`/`glab`), and agent CLI are ready:

```bash
# Validate configuration, git remote, forge auth, and agent CLI
uv run orchestack doctor --repo /path/to/target-repo

# Inspect detected bootstrap state, daemon lock, queued prompts, and open PRs/MRs
uv run orchestack status --repo /path/to/target-repo
```

### Step 3: Run the Autonomous Daemon

Start with a safe `--dry-run` or single-cycle `--once` pass, then run continuously:

```bash
# Preview what the daemon would do without invoking the coding agent
uv run orchestack run --repo /path/to/target-repo --dry-run

# Execute a single prompt / CI-heal / review cycle and exit
uv run orchestack run --repo /path/to/target-repo --once

# Run continuously (picks prompts, opens PRs/MRs, heals CI, and replies to reviews)
uv run orchestack run --repo /path/to/target-repo

# Override agent or forge on the fly
uv run orchestack run --repo /path/to/target-repo --agent claude --forge github
```

You can also launch the daemon via the convenience shell wrapper (which configures `PATH` and invokes `scripts/daemon_runner.py`):

```bash
./scripts/run_daemon.sh --repo /path/to/target-repo --agent agy
```

---

## Configuration & CLI Reference

Settings resolve in strict precedence order:
**CLI flags > `ORCHESTACK_*` environment variables > `.orchestack.toml` > auto-detected defaults**.

Running `orchestack init` generates a `.orchestack.toml` in your target repository root:

```toml
# Orchestack repository configuration
[repo]
base_branch = "main"
branch_prefix = "orchestack"
queued_dir = "prompts/queued"
implemented_dir = "prompts/implemented"
learnings_dir = "docs/learnings"
pr_description_path = "docs/PR_DESCRIPTION.md"
daemon_dir = ".daemon"

[dod]
check_cmd = "make check"
require_learnings = true
require_pr_description = true

[forge]
provider = "auto"

[agent]
backend = "agy"
```

### Subcommands

| Command | Description |
| :--- | :--- |
| `orchestack init` | Scaffold `.orchestack.toml`, prompt/learnings directories, and `.gitignore` entries. |
| `orchestack doctor` | Check `.orchestack.toml`, `origin` remote, `gh`/`glab` auth, and agent CLI availability. |
| `orchestack status` | Show bootstrap state, lock status, queued/implemented prompts, and open PRs/MRs (`--json` supported). |
| `orchestack run` | Launch the autonomous prompt queue, CI auto-healer, and PR/MR review daemon. |
| `orchestack telemetry` | Report steps, tool calls, tokens, and estimated USD cost across `agy`, `claude`, and `codex`. |
| `orchestack version` | Print the installed Orchestack version. |

### Common `orchestack run` Flags

- `--repo <path>` — Target repository root directory (defaults to current working directory).
- `--agent <agy|claude|codex>` — Coding agent backend (`ORCHESTACK_AGENT`).
- `--forge <auto|github|gitlab>` — Forge provider; `auto` detects from `origin` URL (`ORCHESTACK_FORGE`).
- `--check-cmd "<cmd>"` — Definition-of-Done check command; auto-detects `make check`, `pytest`, `npm test`, `cargo test`, or `go test ./...` (`ORCHESTACK_CHECK_CMD`).
- `--base-branch <name>` / `--branch-prefix <prefix>` — Base branch and feature branch prefix.
- `--model <name>` / `--effort <low|medium|high|none>` — Override the agent model or reasoning effort.
- `--interval <seconds>` — Polling interval between PR/MR CI and review checks.
- `--once` — Run a single cycle and exit.
- `--dry-run` — Simulate the daemon loop without invoking the coding agent.
- `--resume` / `--fresh` — Resume an existing session on the active branch or force a clean start.
- `--auto-whitelist` / `--no-auto-whitelist` — Enable or disable automatic collaborator whitelisting for PR/MR review feedback.

---

## Telemetry & Local Development

```bash
# View unified session telemetry (Markdown or --json)
uv run orchestack telemetry --repo /path/to/target-repo

# Refresh docs/IMPLEMENTATION_STATS.md with per-phase timing and token cost
uv run orchestack telemetry --repo /path/to/target-repo --update-stats

# Format and run Orchestack's full verification suite (ruff, strict mypy, pytest, doc checks)
make fmt && make check
```

---

## Documentation & Architecture

- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — Session protocol and **Non-Negotiable Regression Invariants**.
- [`docs/PLAN.md`](docs/PLAN.md) — Architecture source of truth, decision register (`D1`–`D13`), and phased delivery roadmap (`0001`–`0008`).
- [`docs/IMPLEMENTATION_STATS.md`](docs/IMPLEMENTATION_STATS.md) — Per-phase implementation time and total token cost across coding agent sessions and review turns.
- [`docs/KICKOFF.md`](docs/KICKOFF.md) — Kickoff prompts for interactive sessions or autonomous daemon execution.
- [`docs/learnings/`](docs/learnings/) — Durable session hand-offs and historical bugfix context.

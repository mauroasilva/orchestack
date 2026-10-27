# Orchestack — Technical Architecture & Implementation Plan

**Orchestack** is a standalone, repository-agnostic autonomous development daemon and CLI that processes queued prompts (`prompts/queued/NNNN-slug.md`) and pull/merge request review feedback (`PR` / `MR`) across **GitHub** and **GitLab** repositories using pluggable coding agent backends (**Google Antigravity `agy`**, **Anthropic Claude Code `claude`**, and **OpenAI Codex `codex`**).

---

## 1. Overview & Vision

Orchestack evolved from the battle-tested `scripts/daemon_runner.py` (4,290 lines) and `scripts/tests/test_daemon_runner.py` (82 unit tests) built in `BlueTeamHarness`. Rather than rewriting from scratch and risking regressions in edge cases discovered over dozens of production daemon runs, Orchestack seeds its repository directly with the proven baseline implementation and refactors it incrementally behind clean protocols while keeping all 82 baseline tests passing at every commit.

### Core Capabilities
1. **Point at Any Git Repository**: Run `orchestack run --repo /path/to/repo` (or inside any repo with `.orchestack.toml` or CLI flags) without copying daemon scripts into the target repository.
2. **Dual Forge Support (`ForgeProvider`)**: Native support for **GitHub** (`gh` CLI) and **GitLab** (`glab` CLI + GitLab REST API v4 for inline `DiffNote` threads, pipeline job trace logs, and MR approvals), with automatic detection from `git remote get-url origin`.
3. **Tri-Agent Support (`AgentBackend`)**: Run prompts and review fix loops on **Antigravity** (`agy`), **Claude Code** (`claude`), or **OpenAI Codex** (`codex`), with unified session resume, heartbeat monitoring, permission-denial auto-healing, and token/cost telemetry.
4. **Zero-Regression Guarantee**: Every battle-tested invariant (`B1`–`B8` in `docs/PROTOCOL.md` §3.1) is preserved and enforced by automated unit tests.

---

## 2. Key decisions

| ID | Decision | Status | Summary |
| :--- | :--- | :--- | :--- |
| **D1** | Seed & Strangler-Fig Refactor | Accepted | Seed repo with `scripts/daemon_runner.py` + 82 tests; refactor into `src/orchestack/` without breaking baseline tests. |
| **D2** | Target Repository Decoupling (`RepoConfig`) | Accepted | Replace hardcoded `REPO_ROOT` globals with an explicit `RepoConfig` loaded from `.orchestack.toml` + CLI flags (`--repo`). |
| **D3** | State & Lock Isolation | Accepted | Store daemon state, review loop state, and logs in `<target-repo>/.daemon/` (or `~/.orchestack/state/<repo-hash>/`). |
| **D4** | `ForgeProvider` Protocol | Accepted | Abstract all PR/MR, CI check, inline review comment, and merge operations behind `ForgeProvider` (`github`, `gitlab`). |
| **D5** | GitLab REST API via `glab api` | Accepted | Use `glab mr` for standard ops and `glab api projects/:id/merge_requests/:iid/...` for inline `DiffNote` threads, approvals, and job logs. |
| **D6** | `AgentBackend` Protocol | Accepted | Abstract session kickoff, resume, heartbeat, transcript status, summary extraction, and denial detection behind `AgentBackend`. |
| **D7** | Cross-Agent Session Resume Strategy | Accepted | Support native session continuation for Antigravity (`-r <uuid>`), Claude Code (`--resume <id>`), and Codex (`codex exec resume <id>`). |
| **D8** | Pluggable Permission-Denial Auto-Heal | Accepted | Normalize permission denials into `PermissionDenial` records and apply backend-specific whitelisting when `--auto-whitelist` is enabled. |
| **D9** | Multi-Forge CI Quota Fallback | Accepted | Detect both GitHub Actions and GitLab CI billing/quota failures and fall back to running `RepoConfig.check_cmd` locally on the PR/MR branch. |
| **D10** | Configurable Definition of Done (`DoD`) | Accepted | Generalize `check_definition_of_done()` via `RepoConfig` (`check_cmd`, `require_learnings`, `require_pr_description`). |
| **D11** | Backward-Compatible `scripts/daemon_runner.py` Facade | Accepted | Keep `scripts/daemon_runner.py` importing and re-exporting symbols from `orchestack.*` so all 82 baseline tests continue passing unchanged. |
| **D12** | Unified Multi-Agent Telemetry | Accepted | Normalize token usage, tool call counts, durations, and costs across Antigravity SQLite, Claude JSONL, and Codex JSONL logs. |

- **D1 — Seed & Strangler-Fig Refactor**: Rather than writing a greenfield daemon and re-discovering subtle race conditions, Orchestack starts with the exact `scripts/daemon_runner.py` and `scripts/tests/test_daemon_runner.py` from `BlueTeamHarness`. Every phase (`0001`–`0006`) extracts modules into `src/orchestack/` while keeping the 82 baseline tests green.
- **D2 — Target Repository Decoupling (`RepoConfig`)**: All path constants (`REPO_ROOT`, `QUEUED_DIR`, `IMPLEMENTED_DIR`, `LEARNINGS_DIR`, `PR_DESC_PATH`, `DAEMON_DIR`) become properties on `RepoConfig`. Target repositories can optionally commit a `.orchestack.toml` file or rely on auto-detected defaults and CLI flags (`--repo`, `--forge`, `--agent`, `--base-branch`, `--check-cmd`).
- **D3 — State & Lock Isolation**: Each target repository gets its own isolated daemon lock (`daemon.lock`), review loop state (`review_state.json`), and session logs under `<repo_root>/.daemon/` by default, preventing cross-repo state collisions when running multiple Orchestack daemons concurrently.
- **D4 — `ForgeProvider` Protocol**: All source-code hosting operations are defined by a structural `typing.Protocol` (`ForgeProvider`) returning normalized dataclasses (`PullRequestInfo`, `CICheckResult`, `ReviewItem`, `MergeStatus`).
- **D5 — GitLab REST API via `glab api`**: GitLab Merge Requests distinguish between top-level notes, inline `DiffNote` discussions (`projects/:id/merge_requests/:iid/discussions`), and formal MR approvals (`projects/:id/merge_requests/:iid/approvals`). `GitLabForge` uses `glab api` with URL-encoded project paths (`owner%2Frepo`) to achieve full parity with `GitHubForge`.
- **D6 — `AgentBackend` Protocol**: Coding agents differ in CLI flags, session storage, and output formats. `AgentBackend` provides a uniform interface (`start_session`, `resume_session`, `get_heartbeat`, `get_session_status`, `extract_last_response`, `detect_permission_denials`, `whitelist_denial`).
- **D7 — Cross-Agent Session Resume Strategy**: Antigravity uses `agy chat -m agent -r <chat_id> <prompt>`, Claude Code uses `claude -p <prompt> --resume <session_id> --output-format stream-json --verbose`, and Codex uses `codex exec resume <session_id> <prompt> --json`. All three preserve multi-turn context across DoD auto-heal and PR/MR review fix cycles.
- **D8 — Pluggable Permission-Denial Auto-Heal**: Each backend detects sandbox/tool blocks using turn-scoped offsets (`cli_log_offset`, `turn_start_time`) and maps them to backend-specific allowlists (`~/.gemini/antigravity/User/settings.json` for `agy`, `.claude/settings.local.json` or `--allowedTools` for `claude`, and `--add-dir` / sandbox config for `codex`).
- **D9 — Multi-Forge CI Quota Fallback**: When remote CI fails purely due to runner quota or billing exhaustion (GitHub Actions billing message or GitLab shared runner `ci_quota_exceeded` / `runner_system_failure`), Orchestack checks out the PR/MR branch in a temporary worktree or working tree and executes `RepoConfig.check_cmd` locally before merging.
- **D10 — Configurable Definition of Done (`DoD`)**: Different repositories use different test commands (`make check`, `pytest`, `npm test`, `cargo test`) and documentation conventions. `RepoConfig` makes `check_cmd`, `require_learnings`, `require_pr_description`, and `branch_prefix` configurable per repo.
- **D11 — Backward-Compatible `scripts/daemon_runner.py` Facade**: To ensure the 82 unit tests in `scripts/tests/test_daemon_runner.py` (which monkeypatch attributes on `daemon_runner`) continue working without weakening any assertions, `scripts/daemon_runner.py` remains as a thin facade delegating to `orchestack` modules.
- **D12 — Unified Multi-Agent Telemetry**: `orchestack telemetry` extends `scripts/session_telemetry_report.py` to parse Antigravity `~/.gemini/antigravity/conversations/*.pb` / `state.vscdb`, Claude Code `~/.claude/projects/**/*.jsonl`, and Codex `~/.codex/sessions/**/*.jsonl` into a unified `SessionTelemetry` report.

---

## 3. Baseline Regression Invariants (`B1`–`B8`)

Detailed in `docs/PROTOCOL.md` §3.1, the following 8 invariants are non-negotiable across all phases:
- **B1 — Turn-Scoped Timestamp Filtering**: `since_timestamp` and `_normalize_db_timestamp()` prevent stale session IDs or old denials from leaking into new turns.
- **B2 — Turn-Scoped Permission Denial & Multiline Command Extraction**: `cli_log_offset` and `turn_start_time` scope denial checks to the active turn; multiline shell blocks are collapsed and tokenized cleanly; URL denials whitelist `read_url`.
- **B3 — False `MERGED` State & `-impl` Branch Collision Prevention**: `is_prompt_merged_into_origin_main()` verifies `prompts/implemented/` on `origin/<base_branch>`, checks `git merge-base --is-ancestor`, and creates a `-impl` branch when an older PR was merged without moving the prompt file. `find_prompt_path_for_branch()` resolves prompt files across `-impl` suffixes and both `queued/` and `implemented/` directories.
- **B4 — Infinite PR/MR Review Loop Prevention**: `processed_ids` are always persisted (even when prompt path is missing); `[bot]` and `DAEMON_TAG` comments are ignored; `APPROVED` reviews are recorded as seen without triggering fix turns; summaries >60,000 chars are truncated; `max_auto_heal_attempts` circuit breakers halt runaway loops.
- **B5 — CI Billing/Quota Failure Local Test Fallback**: `is_ci_billing_or_quota_failure()` falls back to inspecting run/job logs when `conclusion == "FAILURE"` has no URL and verifies local tests via `verify_local_tests_for_pr()`.
- **B6 — Pure Headless CLI Isolation & Single-Run DoD**: Never mutate GUI IDE state during headless runs; execute `check_definition_of_done()` inside `execute_prompt_session()` rather than duplicating it in `main()`.
- **B7 — Git Staging Safety & Stale `PR_DESCRIPTION.md` Cleanup**: `verify_git_staging_safety()` blocks secrets/dotenvs/large binaries while allowing `docs/PR_DESCRIPTION.md`, and fresh prompt branches delete stale `docs/PR_DESCRIPTION.md` before launching the agent.
- **B8 — Cloud-Sync Lock Cleanup, Network Resilience, & Interactive Recheck**: Clean stale `.git/**/*.lock` files, wait for network connectivity with exponential backoff, and support pressing `Enter` during idle sleep via `_interruptible_sleep()`.

---

## 4. Target Repository & Configuration Architecture (`.orchestack.toml`)

Target repositories are configured via `.orchestack.toml` at the target repository root, overridden by CLI flags or environment variables (`ORCHESTACK_*`). All fields have sensible defaults so zero-config repositories work out of the box.

```toml
[repo]
base_branch = "main"                     # Default base branch (auto-detected from origin/HEAD if omitted)
branch_prefix = "prompt"                 # Branch naming: <branch_prefix>/<NNNN>-<slug>
queued_dir = "prompts/queued"
implemented_dir = "prompts/implemented"
learnings_dir = "docs/learnings"
pr_description_path = "docs/PR_DESCRIPTION.md"
daemon_dir = ".daemon"

[dod]
check_cmd = "make check"                 # Auto-detected if omitted (Makefile -> pytest -> npm test -> cargo test)
require_learnings = true
require_pr_description = true
max_auto_heal_attempts = 3

[forge]
provider = "auto"                        # "auto" | "github" | "gitlab"
host = ""                                # Optional custom host for self-hosted GitHub Enterprise / GitLab
project = ""                             # Optional owner/repo or group/subgroup/repo override
merge_method = "squash"                  # "squash" | "merge" | "rebase"
delete_branch_on_merge = true
local_test_fallback_on_quota = true

[agent]
backend = "agy"                          # "agy" | "claude" | "codex"
binary = ""                              # Optional path override for agent CLI binary
model = ""                               # Optional model override (e.g. claude-sonnet-4-20250514, o3, etc.)
timeout_seconds = 1800
heartbeat_interval_seconds = 30
auto_whitelist = false
extra_args = []
```

---

## 5. `ForgeProvider` Abstraction (GitHub & GitLab)

Located in `src/orchestack/forge/`:
- `base.py`: Defines `ForgeProvider(Protocol)` and normalized models:
  - `PullRequestInfo(number: int, title: str, url: str, head_ref: str, base_ref: str, state: str, is_draft: bool, mergeable: str)`
  - `CICheckResult(status: Literal["PENDING", "PASSED", "FAILED", "QUOTA_EXCEEDED"], details: str, failed_urls: list[str])`
  - `ReviewItem(id: str, author: str, body: str, created_at: str, path: str | None, line: int | None, item_type: Literal["review", "inline_comment", "discussion_comment"])`
- `github.py` (`GitHubForge`):
  - Wraps `gh pr list`, `gh pr view`, `gh pr create`, `gh pr checks`, `gh pr comment`, `gh pr merge`, `gh run view`, and `gh api repos/{owner}/{repo}/pulls/{num}/...`.
- `gitlab.py` (`GitLabForge`):
  - Wraps `glab mr list`, `glab mr view`, `glab mr create`, `glab mr note`, `glab mr merge`, and `glab api projects/{urlencoded_path}/merge_requests/{iid}/...`.
  - Queries `/discussions` to extract both general notes and inline `DiffNote` threads (extracting `position.new_path` and `position.new_line`), `/approvals` for approval state, and `/pipelines` + `/jobs/{job_id}/trace` to detect GitLab CI failures and quota exhaustion (`ci_quota_exceeded`, `shared_runners_minutes_limit_exceeded`).
- `detect.py`:
  - Parses `git remote get-url origin` (supporting HTTPS and SSH `git@host:group/subgroup/repo.git` URLs) and selects `GitHubForge` or `GitLabForge`.

---

## 6. `AgentBackend` Abstraction (Antigravity, Claude Code, OpenAI Codex)

Located in `src/orchestack/agents/`:
- `base.py`: Defines `AgentBackend(Protocol)` and normalized models:
  - `AgentSessionResult(session_id: str | None, exit_code: int, completed_normally: bool, error_message: str | None, last_response: str, cli_log_offset: int, turn_start_time: float)`
  - `PermissionDenial(tool_name: str, resource: str, command_prefix: str | None, raw_reason: str)`
  - `HeartbeatSnapshot(active: bool, latest_activity: str, updated_at: float)`
- `antigravity.py` (`AntigravityAgent`):
  - Kickoff: `agy chat -m agent <prompt>` (cwd = `repo_config.repo_root`).
  - Resume: `agy chat -m agent -r <session_id> <prompt>`.
  - State & Heartbeat: Reads SQLite `state.vscdb` and `~/.gemini/antigravity/conversations/<id>.pb` / brain task logs, preserving `since_timestamp` and `_normalize_db_timestamp()`.
- `claude_code.py` (`ClaudeCodeAgent`):
  - Kickoff: `claude -p <prompt> --output-format stream-json --verbose`.
  - Resume: `claude -p <prompt> --resume <session_id> --output-format stream-json --verbose`.
  - State & Heartbeat: Parses the JSONL event stream (`system.init` for `session_id`, `assistant` tool-use blocks for live heartbeat, `result` event for final summary, cost, and token usage) and `~/.claude/projects/<project-slug>/<session_id>.jsonl`.
- `codex.py` (`CodexAgent`):
  - Kickoff: `codex exec <prompt> --json -C <repo_root>`.
  - Resume: `codex exec resume <session_id> <prompt> --json`.
  - State & Heartbeat: Parses the JSONL event stream (`thread.started` / `session_id`, `item.completed` with `agent_message` or `command_execution`, `turn.completed` with token usage) and `~/.codex/sessions/**/rollout-*.jsonl`.

---

## 7. Permission Denial & Auto-Heal Engine Across Backends

When an agent is blocked from running a shell command or reading a URL/file:
1. `agent.detect_permission_denials(session_id, cli_log_path, cli_log_offset, turn_start_time)` scans only output generated during the current turn (`B1`, `B2`).
2. Multiline command strings are normalized via `extract_command_prefix()`.
3. If `--auto-whitelist` is enabled:
   - `AntigravityAgent`: Updates `antigravity.terminalCommandAutoExecution` allowlist or `read_url` policy in `~/.gemini/antigravity/User/settings.json`.
   - `ClaudeCodeAgent`: Adds `"Bash(<prefix>:*)"` or `"WebFetch"` to `<repo_root>/.claude/settings.local.json` (`permissions.allow`) or passes `--allowedTools`.
   - `CodexAgent`: Adjusts `<repo_root>/.codex/config.toml` approval policy or passes `--add-dir` / sandbox flags on resume.
4. Resumes the exact same session instructing the agent to retry the blocked step.

---

## 8. Daemon Loop & State Machine

Located in `src/orchestack/daemon/`:
- Each cycle executes two priority tiers:
  1. **Tier 1 — Open PR/MR Review & CI Loop**:
     - Lists open PRs/MRs matching `repo_config.branch_prefix`.
     - Checks CI status via `ForgeProvider.get_ci_status()`. If CI failed due to quota (`QUOTA_EXCEEDED`), runs `verify_local_tests_for_pr()`.
     - Fetches unprocessed human reviews and inline comments via `ForgeProvider.get_unprocessed_reviews()`.
     - If actionable feedback or non-quota CI failure exists (and `attempts < max_auto_heal_attempts`), resumes or starts an `AgentBackend` session on the PR/MR branch, verifies DoD, commits, pushes, and posts a truncated (`<= 60,000` chars) summary comment tagged with `DAEMON_TAG`.
     - If CI passed (or local fallback passed) and no unresolved reviews remain, merges the PR/MR via `ForgeProvider.merge_pr()`.
  2. **Tier 2 — Next Queued Prompt Execution**:
     - Scans `repo_config.queued_dir` for the lowest-numbered `NNNN-*.md` prompt.
     - Checks `is_prompt_merged_into_origin_main()` (`B3`). Creates branch `<prefix>/<NNNN>-<slug>` (or `-impl` if an earlier branch was merged without moving the prompt file).
     - Removes any stale `docs/PR_DESCRIPTION.md` (`B7`).
     - Runs `execute_prompt_session()` via `AgentBackend`, enforcing `check_definition_of_done()` (`B6`) and permission auto-healing (`B2`) up to `max_auto_heal_attempts`.
     - Verifies git staging safety (`B7`), moves prompt from `queued/` to `implemented/`, commits, pushes, and opens the PR/MR via `ForgeProvider.create_pr()`.

---

## 9. Multi-Agent Telemetry & Reporting

Located in `src/orchestack/telemetry/`:
- Generalizes `scripts/session_telemetry_report.py` to support Antigravity, Claude Code, and Codex sessions.
- Produces Markdown and JSON telemetry reports detailing per-session and aggregate metrics: prompt ID, backend, model, turn count, wall-clock duration, input/cached/output tokens, estimated cost, tool invocations, permission denials healed, and DoD auto-heal count.

---

## 10. CLI Surface & Entry Points

Located in `src/orchestack/cli.py`:
- `orchestack run [--repo PATH] [--forge auto|github|gitlab] [--agent agy|claude|codex] [--once] [--interval SEC] [--auto-whitelist]`
- `orchestack init [--repo PATH] [--forge ...] [--agent ...]` — Scaffolds `.orchestack.toml`, `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`, and `.gitignore` entries in any target git repository.
- `orchestack status [--repo PATH]` — Displays queued/implemented prompt counts, open PR/MR status, lock status, and recent session state.
- `orchestack doctor [--repo PATH]` — Verifies git remote, forge CLI auth (`gh auth status` / `glab auth status`), agent CLI binary availability (`agy`, `claude`, `codex`), and `.orchestack.toml` validity.
- `orchestack telemetry [--repo PATH] [--format markdown|json]` — Generates multi-agent session telemetry reports.
- Legacy flags passed directly (`orchestack --once` or `python3 scripts/daemon_runner.py --once`) remain 100% supported (`D11`).

---

## 11. Security, Secrets, & Git Staging Safety

- `verify_git_staging_safety()` runs before every `git add` / `git commit` in the target repository.
- Blocks `.env*`, private keys (`*.pem`, `*.key`, `id_rsa`), credentials files, and binaries >5 MB while explicitly permitting `repo_config.pr_description_path`.
- Never logs raw API tokens or mutates global IDE GUI state (`B6`).

---

## 12. Testing & Quality Bar

- `make check` runs `ruff check`, `ruff format --check`, `mypy`, `pytest` (including all 82 baseline tests in `scripts/tests/test_daemon_runner.py` plus new unit tests in `tests/`), and `tests/docs/test_indexes.py`.
- All external CLI calls (`git`, `gh`, `glab`, `agy`, `claude`, `codex`) must be mocked in unit tests; no unit test may make real network requests or spawn live LLM sessions.

---

## 13. Phased delivery

| Phase | Slug | Scope & Deliverables | Status |
| :--- | :--- | :--- | :--- |
| 0001 | `core-package-and-repo-config` | Extract `RepoConfig`, `.orchestack.toml` loader, `--repo` CLI support, and configurable DoD while keeping `scripts/daemon_runner.py` and all 82 baseline tests green. | Implemented |
| 0002 | `forge-provider-protocol-and-github` | Define `ForgeProvider` protocol & data models in `src/orchestack/forge/` and extract `GitHubForge` from `daemon_runner.py`. | Implemented |
| 0003 | `gitlab-forge-provider` | Implement `GitLabForge` (`glab mr` + `glab api` for inline `DiffNote` discussions, approvals, and CI job trace quota checks) and forge auto-detection from git remotes. | Queued |
| 0004 | `agent-backend-protocol-and-antigravity` | Define `AgentBackend` protocol & `PermissionDenial` models in `src/orchestack/agents/` and extract `AntigravityAgent` with all `B1`/`B2`/`B6` invariants preserved. | Queued |
| 0005 | `claude-code-and-codex-agent-backends` | Implement `ClaudeCodeAgent` (`claude`) and `CodexAgent` (`codex`) with session resume, JSONL heartbeat/summary parsing, and backend-specific permission auto-whitelisting. | Queued |
| 0006 | `cli-subcommands-and-multi-agent-telemetry` | Implement `orchestack` subcommands (`run`, `init`, `status`, `doctor`, `telemetry`), update `scripts/run_daemon.sh`, and add cross-agent telemetry reporting. | Queued |

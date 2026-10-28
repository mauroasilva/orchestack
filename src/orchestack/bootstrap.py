# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Repository bootstrap state detection and Phase 0000 PR-comment interview helpers."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

PHASE_0000_SLUG = "0000-bootstrap-plan-and-prompts"
PHASE_0000_FILENAME = f"{PHASE_0000_SLUG}.md"
BOOTSTRAP_STATE_FILENAME = "bootstrap_state.json"
PHASE_0000_QUESTIONS_FILENAME = "phase0000_questions.md"

_PHASE_PROMPT_RE = re.compile(r"^(\d{4})-.+\.md$")
_NUMBERED_ITEM_RE = re.compile(r"^\s*(\d+)[\.\)]\s+(.+)$")


class RepoBootstrapState(StrEnum):
    """Three repository states supported by Orchestack (`D13`)."""

    QUEUE_READY = "queue_ready"
    BRIEF_ONLY = "brief_only"
    EMPTY_REPO = "empty_repo"


def is_phase_0000_prompt(prompt_path: Path | str | None) -> bool:
    """Return True if `prompt_path` refers to the Phase 0000 bootstrap prompt."""
    if prompt_path is None:
        return False
    name = Path(prompt_path).name
    return name == PHASE_0000_FILENAME or name.startswith("0000-")


def _read_recorded_initial_state(repo_root: Path) -> RepoBootstrapState | None:
    """Read `.daemon/bootstrap_state.json` if present."""
    state_file = repo_root / ".daemon" / BOOTSTRAP_STATE_FILENAME
    if not state_file.is_file():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
        raw = str(data.get("initial_state") or data.get("bootstrap_state") or "").strip().lower()
        if raw == RepoBootstrapState.EMPTY_REPO.value:
            return RepoBootstrapState.EMPTY_REPO
        if raw == RepoBootstrapState.BRIEF_ONLY.value:
            return RepoBootstrapState.BRIEF_ONLY
        if raw == RepoBootstrapState.QUEUE_READY.value:
            return RepoBootstrapState.QUEUE_READY
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return None


def has_queued_implementation_prompts(repo_root: Path) -> bool:
    """Return True if `prompts/queued/` has at least one `NNNN-*.md` (`NNNN != '0000'`)."""
    queued_dir = repo_root / "prompts" / "queued"
    if not queued_dir.is_dir():
        return False
    for entry in queued_dir.iterdir():
        if not entry.is_file():
            continue
        match = _PHASE_PROMPT_RE.match(entry.name)
        if match and match.group(1) != "0000":
            return True
    return False


def detect_repo_bootstrap_state(repo_root: Path) -> RepoBootstrapState:
    """Detect whether `repo_root` is `QUEUE_READY`, `BRIEF_ONLY`, or `EMPTY_REPO`."""
    resolved_root = Path(repo_root).resolve()
    implemented_dir = resolved_root / "prompts" / "implemented"

    # 1. QUEUE_READY if prompts/queued has any NNNN != 0000 or prompts/implemented has any NNNN-*.md
    if has_queued_implementation_prompts(resolved_root):
        return RepoBootstrapState.QUEUE_READY

    if implemented_dir.is_dir():
        for entry in implemented_dir.iterdir():
            if not entry.is_file():
                continue
            match = _PHASE_PROMPT_RE.match(entry.name)
            if match:
                return RepoBootstrapState.QUEUE_READY

    # 2. Check if an active EMPTY_REPO bootstrap recorded its initial state while Phase 0000
    # is actively queued on disk
    phase_0000_queued = (resolved_root / "prompts" / "queued" / PHASE_0000_FILENAME).is_file()
    recorded = _read_recorded_initial_state(resolved_root)
    if phase_0000_queued and recorded == RepoBootstrapState.EMPTY_REPO:
        return RepoBootstrapState.EMPTY_REPO

    # 3. BRIEF_ONLY if docs/BRIEF.md exists with non-whitespace content
    brief_path = resolved_root / "docs" / "BRIEF.md"
    if brief_path.is_file():
        try:
            content = brief_path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return RepoBootstrapState.BRIEF_ONLY
        except OSError:
            pass

    if phase_0000_queued and recorded == RepoBootstrapState.BRIEF_ONLY:
        return RepoBootstrapState.BRIEF_ONLY

    # 4. Otherwise EMPTY_REPO
    return RepoBootstrapState.EMPTY_REPO


def ensure_base_branch_initialized(
    repo_root: Path,
    base_branch: str,
    run_cmd_fn: Callable[..., Any] | None = None,
) -> None:
    """Ensure the git repository has an initial commit on `base_branch`."""
    resolved_root = Path(repo_root).resolve()

    def _default_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=resolved_root,
            capture_output=True,
            text=True,
            check=check,
        )

    runner = run_cmd_fn if run_cmd_fn is not None else _default_run

    if not (resolved_root / ".git").exists():
        runner(["git", "init", "-b", base_branch], check=False)

    head_res = runner(["git", "rev-parse", "--verify", "HEAD"], check=False)
    if getattr(head_res, "returncode", 0) != 0:
        gitignore_path = resolved_root / ".gitignore"
        if not gitignore_path.is_file():
            gitignore_path.write_text(".daemon/\ndocs/PR_DESCRIPTION.md\n", encoding="utf-8")
        runner(["git", "checkout", "-B", base_branch], check=False)
        runner(["git", "add", ".gitignore"], check=False)
        runner(
            [
                "git",
                "commit",
                "-m",
                "chore: initialize repository for Orchestack Phase 0000 bootstrap",
            ],
            check=False,
        )
        runner(["git", "push", "-u", "origin", base_branch], check=False)


def check_phase_0000_skeleton_files(
    repo_root: Path,
    state: RepoBootstrapState | None = None,
) -> tuple[bool, list[str]]:
    """Verify whether the Phase 0000 skeleton files exist in `repo_root`."""
    resolved_root = Path(repo_root).resolve()
    effective_state = state if state is not None else detect_repo_bootstrap_state(resolved_root)

    required_files = [
        "docs/PLAN.md",
        "docs/PROTOCOL.md",
        "docs/LICENSE-HEADER.md",
        "README.md",
    ]
    if effective_state == RepoBootstrapState.EMPTY_REPO:
        required_files.insert(0, "docs/BRIEF.md")

    missing: list[str] = []
    for rel_path in required_files:
        target = resolved_root / rel_path
        if not target.is_file():
            missing.append(rel_path)
            continue
        try:
            if not target.read_text(encoding="utf-8", errors="replace").strip():
                missing.append(rel_path)
        except OSError:
            missing.append(rel_path)

    for rel_dir in ("prompts/queued", "prompts/implemented", "docs/learnings"):
        if not (resolved_root / rel_dir).is_dir():
            missing.append(rel_dir)

    return len(missing) == 0, missing


def scaffold_phase_0000_prompt(
    repo_root: Path,
    state: RepoBootstrapState | None = None,
) -> Path:
    """Record bootstrap state and create `prompts/queued/0000-bootstrap-plan-and-prompts.md`."""
    resolved_root = Path(repo_root).resolve()
    effective_state = state if state is not None else detect_repo_bootstrap_state(resolved_root)
    if effective_state == RepoBootstrapState.QUEUE_READY:
        effective_state = (
            RepoBootstrapState.BRIEF_ONLY
            if (resolved_root / "docs" / "BRIEF.md").is_file()
            else RepoBootstrapState.EMPTY_REPO
        )

    queued_dir = resolved_root / "prompts" / "queued"
    implemented_dir = resolved_root / "prompts" / "implemented"
    learnings_dir = resolved_root / "docs" / "learnings"
    for directory in (queued_dir, implemented_dir, learnings_dir):
        directory.mkdir(parents=True, exist_ok=True)

    _, missing = check_phase_0000_skeleton_files(resolved_root, effective_state)

    daemon_dir = resolved_root / ".daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    state_file = daemon_dir / BOOTSTRAP_STATE_FILENAME
    state_file.write_text(
        json.dumps(
            {
                "bootstrap_state": effective_state.value,
                "initial_state": effective_state.value,
                "phase_0000_prompt": f"prompts/queued/{PHASE_0000_FILENAME}",
                "missing_skeleton_files": missing,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    prompt_path = queued_dir / PHASE_0000_FILENAME
    if prompt_path.is_file():
        return prompt_path

    if effective_state == RepoBootstrapState.EMPTY_REPO:
        mode_title = (
            "Empty Repository (`empty_repo`) — Build `docs/BRIEF.md`, "
            "`docs/PLAN.md`, and Prompt Queue"
        )
        brief_instructions = (
            "- This repository started in **State 3 (`empty_repo`)**. "
            "No `docs/BRIEF.md` was found.\n"
            "- Create and iteratively refine `docs/BRIEF.md` alongside the skeleton "
            "repository based on what you discover from any existing files and from "
            "interviewing the user via PR comments.\n"
        )
        skeleton_items = (
            "1. **Scaffold the Skeleton Repository**:\n"
            "   - Create `docs/BRIEF.md` (initial project brief draft),\n"
            "   - `docs/KICKOFF.md`, `docs/PROTOCOL.md`, `docs/PLAN.md`, "
            "`docs/LICENSE-HEADER.md`, `README.md`,\n"
            "   - `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`,\n"
            "   - `.orchestack.toml`, and `.gitignore` "
            "(ignoring `.daemon/` and `docs/PR_DESCRIPTION.md`)."
        )
    else:
        mode_title = (
            "Brief-Only Repository (`brief_only`) — Build `docs/PLAN.md` "
            "and Prompt Queue from `docs/BRIEF.md`"
        )
        brief_instructions = (
            "- This repository started in **State 2 (`brief_only`)** where "
            "`docs/BRIEF.md` exists.\n"
            "- Read `docs/BRIEF.md` carefully before making any architectural decisions.\n"
        )
        skeleton_items = (
            "1. **Scaffold the Skeleton Repository**:\n"
            "   - Create `docs/KICKOFF.md`, `docs/PROTOCOL.md`, `docs/PLAN.md`, "
            "`docs/LICENSE-HEADER.md`, `README.md`,\n"
            "   - `prompts/queued/`, `prompts/implemented/`, `docs/learnings/`,\n"
            "   - `.orchestack.toml`, and `.gitignore` "
            "(ignoring `.daemon/` and `docs/PR_DESCRIPTION.md`)."
        )

    prompt_lines = [
        "# 0000 — Bootstrap Architecture Plan & Implementation Prompt Queue",
        "",
        "## Bootstrap Mode",
        mode_title,
        "",
        brief_instructions.rstrip(),
        "",
        "## Objective",
        "Interview the user iteratively via PR/MR comments—**assuming nothing**—until you have "
        "complete clarity to finalize `docs/BRIEF.md`, `docs/PLAN.md`, and the entire "
        "implementation prompt queue (`prompts/queued/0001-*.md`, `0002-*.md`, ...).",
        "",
        "## Workflow & Deliverables",
        "",
        skeleton_items,
        "",
        "2. **Assume Nothing — Summarize Understanding in `docs/PR_DESCRIPTION.md`**:",
        "   - Write `docs/PR_DESCRIPTION.md` containing a clear, structured summary of what you "
        "understand the project is trying to build so far, including goals, architecture, "
        "tech stack, and open questions.",
        "   - Orchestack uses `docs/PR_DESCRIPTION.md` as the initial PR description so the user "
        "can review your understanding immediately.",
        "",
        "3. **Iterative PR-Comment Interview via `.daemon/phase0000_questions.md`**:",
        "   - Whenever **any** requirement, architectural decision, target stack, verification "
        "command, constraint, or phase boundary is ambiguous or underspecified, "
        "**do not guess or assume**.",
        "   - Write a **numbered list of questions** (`1. ...`, `2. ...`, `3. ...`) to "
        "`.daemon/phase0000_questions.md` (and include the same numbered list at the end "
        "of your response).",
        f"   - **Keep `prompts/queued/{PHASE_0000_FILENAME}` in `prompts/queued/`** "
        "(do **not** move it to `prompts/implemented/` yet) while questions remain open.",
        "   - End your turn cleanly so Orchestack can commit the skeleton/updates, open or "
        "update the PR with `docs/PR_DESCRIPTION.md`, and post your numbered list of questions "
        "as a PR comment for the user to answer.",
        "",
        "4. **Finalizing Phase 0000 (Only After All User Questions Are Answered)**:",
        "   - Once the user has replied via PR comments and you have **zero remaining questions**:",
        "     1. Finalize `docs/BRIEF.md` and `docs/PLAN.md` (including all architectural "
        "decisions and the complete Phase Prompt Index).",
        "     2. Generate all self-contained implementation phase prompts "
        "(`prompts/queued/0001-*.md`, `0002-*.md`, ...).",
        "     3. Delete `.daemon/phase0000_questions.md` if it exists.",
        f"     4. Move `prompts/queued/{PHASE_0000_FILENAME}` to "
        f"`prompts/implemented/{PHASE_0000_FILENAME}`.",
        "     5. Write `docs/learnings/0000-bootstrap-plan-and-prompts-learnings.md` "
        "with a `## Summary` section.",
        "     6. Update `docs/PR_DESCRIPTION.md` summarizing the finalized plan "
        "and generated prompt queue.",
        "",
    ]
    prompt_path.write_text("\n".join(prompt_lines), encoding="utf-8")
    return prompt_path


def _normalize_numbered_questions(raw_text: str) -> str | None:
    """Extract or normalize numbered questions (`1. ...`, `2. ...`) from markdown text."""
    if not raw_text or not raw_text.strip():
        return None

    lines = raw_text.strip().splitlines()
    numbered_blocks: list[str] = []
    current_item: list[str] = []

    for line in lines:
        stripped = line.strip()
        match = _NUMBERED_ITEM_RE.match(line)
        if match:
            if current_item:
                numbered_blocks.append("\n".join(current_item))
            idx = len(numbered_blocks) + 1
            current_item = [f"{idx}. {match.group(2).strip()}"]
        elif current_item and stripped and not stripped.startswith("#"):
            current_item.append(f"   {stripped}")
        elif current_item and not stripped:
            pass

    if current_item:
        numbered_blocks.append("\n".join(current_item))

    if numbered_blocks:
        return "\n".join(numbered_blocks)

    return None


def extract_phase_0000_questions(
    repo_root: Path,
    agent_summary: str | None = None,
) -> str | None:
    """Extract numbered Phase 0000 interview questions from file or `agent_summary`."""
    resolved_root = Path(repo_root).resolve()
    questions_file = resolved_root / ".daemon" / PHASE_0000_QUESTIONS_FILENAME

    if questions_file.is_file():
        try:
            raw_file_text = questions_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            raw_file_text = ""
        if raw_file_text:
            normalized = _normalize_numbered_questions(raw_file_text)
            if normalized:
                return normalized
            plain_items: list[str] = []
            for line in raw_file_text.splitlines():
                cleaned = line.strip()
                if not cleaned or cleaned.startswith("#"):
                    continue
                cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
                plain_items.append(cleaned)
            if plain_items:
                return "\n".join(f"{i}. {item}" for i, item in enumerate(plain_items, start=1))

    if agent_summary:
        normalized_summary = _normalize_numbered_questions(agent_summary)
        if normalized_summary:
            return normalized_summary

    return None


def is_phase_0000_interview_pending(
    repo_root: Path,
    prompt_path: Path | None = None,
    agent_summary: str | None = None,
) -> bool:
    """Return True if Phase 0000 is in `prompts/queued/` and has pending interview questions."""
    resolved_root = Path(repo_root).resolve()
    target_prompt = (
        Path(prompt_path)
        if prompt_path is not None
        else resolved_root / "prompts" / "queued" / PHASE_0000_FILENAME
    )
    if not is_phase_0000_prompt(target_prompt):
        return False

    queued_file = resolved_root / "prompts" / "queued" / target_prompt.name
    implemented_file = resolved_root / "prompts" / "implemented" / target_prompt.name
    if implemented_file.is_file() or not queued_file.is_file():
        return False

    questions = extract_phase_0000_questions(resolved_root, agent_summary=agent_summary)
    return bool(questions)


def is_phase_0000_complete(
    repo_root: Path,
    prompt_path: Path | None = None,
) -> bool:
    """Return True once Phase 0000 is moved to `prompts/implemented/` and `0001+` prompts exist."""
    resolved_root = Path(repo_root).resolve()
    target_name = Path(prompt_path).name if prompt_path is not None else PHASE_0000_FILENAME
    queued_file = resolved_root / "prompts" / "queued" / target_name
    implemented_file = resolved_root / "prompts" / "implemented" / target_name

    if queued_file.is_file() or not implemented_file.is_file():
        return False
    if is_phase_0000_interview_pending(resolved_root, prompt_path):
        return False
    return has_queued_implementation_prompts(resolved_root)


def format_phase_0000_questions_comment(
    questions: str,
    *,
    iteration: int | None = None,
) -> str:
    """Format a PR comment presenting a numbered list of Phase 0000 clarifying questions."""
    heading = (
        f"### Phase 0000 Bootstrap — Clarifying Questions (Round {iteration})"
        if iteration is not None
        else "### Phase 0000 Bootstrap — Clarifying Questions"
    )
    return (
        f"{heading}\n\n"
        "Please reply in a PR comment with your answers to the numbered questions below so "
        "Orchestack can refine the project brief, architecture plan (`docs/PLAN.md`), and "
        "implementation prompt queue (`prompts/queued/0001-*.md`, ...):\n\n"
        f"{questions.strip()}\n"
    )


def format_phase_0000_completion_comment(repo_root: Path) -> str:
    """Format a PR comment announcing that Phase 0000 bootstrap and prompt queue are complete."""
    resolved_root = Path(repo_root).resolve()
    queued_dir = resolved_root / "prompts" / "queued"
    queued_names: list[str] = []
    if queued_dir.is_dir():
        for entry in sorted(queued_dir.glob("*.md")):
            match = _PHASE_PROMPT_RE.match(entry.name)
            if match and match.group(1) != "0000":
                queued_names.append(entry.name)

    bullet_list = (
        "\n".join(f"- `prompts/queued/{name}`" for name in queued_names)
        if queued_names
        else "- *(Prompt queue finalized)*"
    )
    return (
        "### Phase 0000 Bootstrap Complete — Plan & Prompt Queue Finalized\n\n"
        "All clarifying questions have been resolved. `docs/PLAN.md` and the implementation "
        "prompt queue have been generated:\n\n"
        f"{bullet_list}\n\n"
        "This Phase 0000 PR is now ready for review and merge."
    )


def build_phase_0000_interview_prompt(
    repo_root: Path,
    prompt_path: Path,
    pr_number: int,
    formatted_comments: list[str],
) -> str:
    """Build the agent prompt for a Phase 0000 PR-comment interview iteration."""
    resolved_root = Path(repo_root).resolve()
    state = detect_repo_bootstrap_state(resolved_root)
    comments_block = "\n\n".join(formatted_comments)
    rel_prompt = f"prompts/queued/{prompt_path.name}"

    brief_note = (
        "- Update and refine `docs/BRIEF.md` with the user's answers "
        "(this repository started in `empty_repo` state).\n"
        if state == RepoBootstrapState.EMPTY_REPO
        else "- Update `docs/BRIEF.md` if the user's answers refine or expand the project brief.\n"
    )

    return (
        "You have received new **Phase 0000 Bootstrap Interview response(s)** on "
        f"Pull Request #{pr_number} (`{rel_prompt}`).\n\n"
        "The user has replied on the PR with answers/feedback to your previous "
        "clarifying questions:\n\n"
        f"{comments_block}\n\n"
        "### Instructions (Assume Nothing)\n"
        f"{brief_note}"
        "- Incorporate the user's answers into `docs/PLAN.md` and update `docs/PR_DESCRIPTION.md` "
        "so the PR description reflects your updated summary of what we are building.\n"
        "- **Assume nothing**: carefully evaluate whether any requirements, technical choices, "
        "constraints, or phase boundaries remain ambiguous or need follow-up clarification.\n"
        "- **If you have any follow-up questions**:\n"
        "  1. Write the new **numbered list of questions** (`1. ...`, `2. ...`, ...) to "
        "`.daemon/phase0000_questions.md` (and include them at the end of your response).\n"
        f"  2. Keep `{rel_prompt}` in `prompts/queued/` "
        "(do NOT move it to `prompts/implemented/` yet).\n"
        "  3. Do not run `git commit` or `git push` yourself; the daemon will commit, "
        "update the PR, and post your numbered questions comment.\n"
        "- **If all questions are answered and the user's intent is completely clear**:\n"
        "  1. Finalize `docs/BRIEF.md` and `docs/PLAN.md`.\n"
        "  2. Generate all implementation prompts (`prompts/queued/0001-*.md`, `0002-*.md`, ...).\n"
        "  3. Remove `.daemon/phase0000_questions.md` if present.\n"
        f"  4. Move `{rel_prompt}` to `prompts/implemented/{prompt_path.name}`.\n"
        "  5. Create `docs/learnings/0000-bootstrap-plan-and-prompts-learnings.md` "
        "with a `## Summary` block.\n"
        "  6. Update `docs/PR_DESCRIPTION.md` summarizing the finalized plan and prompt queue.\n"
        "  7. Do not run `git commit` or `git push` yourself; the daemon handles git operations.\n"
    )

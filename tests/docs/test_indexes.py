# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Documentation index and numbering invariant tests."""

import re
from collections import Counter
from pathlib import Path

import pytest


def parse_decision_register(text: str) -> tuple[list[int], list[int]]:
    """Extract decision row IDs and entry IDs from PLAN.md §2 text."""
    s2_match = re.search(r"## 2\. Key decisions.*?(?=## 3\.|\Z)", text, re.DOTALL)
    if not s2_match:
        raise ValueError("Section 2 (Key decisions) not found")
    s2_text = s2_match.group(0)

    table_ids = [int(m.group(1)) for m in re.finditer(r"\|\s*\*\*D(\d+)\*\*\s*\|", s2_text)]
    entry_ids = [int(m.group(1)) for m in re.finditer(r"(?m)^-\s+\*\*D(\d+)\s+[—\-]", s2_text)]
    return table_ids, entry_ids


def validate_decision_register(table_ids: list[int], entry_ids: list[int]) -> None:
    """Validate that decision register rows and entries match and are contiguous from D1."""
    if not table_ids:
        raise ValueError("No decisions found in register table")
    if not entry_ids:
        raise ValueError("No decision entries found below register table")

    expected_contiguous = list(range(1, len(table_ids) + 1))
    if table_ids != expected_contiguous:
        raise ValueError(
            f"Decision table IDs are not contiguous starting at D1: got {table_ids}, "
            f"expected {expected_contiguous}"
        )

    table_set = set(table_ids)
    entry_set = set(entry_ids)

    missing_entries = table_set - entry_set
    if missing_entries:
        raise ValueError(
            f"Decision register rows missing corresponding entries: {sorted(missing_entries)}"
        )

    missing_rows = entry_set - table_set
    if missing_rows:
        raise ValueError(
            f"Decision entries missing corresponding register rows: {sorted(missing_rows)}"
        )


def parse_phase_table(text: str) -> list[str]:
    """Extract 4-digit phase numbers from PLAN.md §13 phase table."""
    s13_match = re.search(r"## 13\. Phased delivery.*?(?=## 14\.|\Z)", text, re.DOTALL)
    if not s13_match:
        raise ValueError("Section 13 (Phased delivery) not found")
    s13_text = s13_match.group(0)

    return re.findall(r"(?m)^\|\s*(\d{4})\s*\|", s13_text)


def validate_phase_table(
    phase_numbers: list[str],
    prompt_files: list[str],
    withdrawn_numbers: set[str] | None = None,
) -> None:
    """Validate that each phase has exactly one prompt or withdrawn learning, and vice versa."""
    if withdrawn_numbers is None:
        withdrawn_numbers = set()

    prompt_numbers: list[str] = []
    for f in prompt_files:
        m = re.match(r"^(\d{4})-", f)
        if m:
            prompt_numbers.append(m.group(1))

    phase_set = set(phase_numbers)
    if len(phase_set) != len(phase_numbers):
        duplicates = [num for num in phase_numbers if phase_numbers.count(num) > 1]
        raise ValueError(f"Duplicate phase numbers in phase table: {set(duplicates)}")

    prompt_set = set(prompt_numbers)
    orphan_prompts = prompt_set - phase_set
    if orphan_prompts:
        raise ValueError(f"Prompt files with no row in phase table: {sorted(orphan_prompts)}")

    counts = Counter(prompt_numbers)
    for num in withdrawn_numbers:
        counts[num] += 1

    missing_phases = [p for p in phase_numbers if counts[p] == 0]
    if missing_phases:
        raise ValueError(
            f"Phase table rows with no prompt file or withdrawn learning: {missing_phases}"
        )

    duplicate_implementations = [p for p in phase_numbers if counts[p] > 1]
    if duplicate_implementations:
        raise ValueError(
            "Phase numbers implemented more than once (or conflicting with withdrawn): "
            f"{duplicate_implementations}"
        )


def validate_prompt_numbering(
    queued_files: list[str],
    implemented_files: list[str],
    retired_numbers: set[int] | None = None,
) -> None:
    """Validate prompt numbering invariants per PROTOCOL.md §6."""
    pattern = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
    retired = retired_numbers or set()

    for f in queued_files:
        if not pattern.match(f):
            raise ValueError(f"Queued prompt filename does not match pattern: {f}")
    for f in implemented_files:
        if not pattern.match(f):
            raise ValueError(f"Implemented prompt filename does not match pattern: {f}")

    queued_nums = {int(f[:4]) for f in queued_files}
    impl_nums = {int(f[:4]) for f in implemented_files}

    intersection = queued_nums & impl_nums
    if intersection:
        raise ValueError(f"Prompt number appears in both queued and implemented: {intersection}")

    if queued_nums:
        max_impl = max(impl_nums) if impl_nums else 0
        min_queued = min(queued_nums)
        if min_queued <= max_impl:
            raise ValueError(
                f"Queued prompt numbers ({min_queued}) must be strictly above "
                f"highest implemented ({max_impl})"
            )

        expected_range = set(range(min_queued, max(queued_nums) + 1))
        actual_covered = queued_nums | {r for r in retired if min_queued <= r <= max(queued_nums)}
        missing = expected_range - actual_covered
        if missing:
            raise ValueError(
                f"Queued prompt numbers are not contiguous; missing: {sorted(missing)}"
            )


def validate_read_first_section(prompt_text: str, filename: str = "prompt.md") -> None:
    """Validate that prompt has a ## Read first section mentioning PROTOCOL.md
    and at least one §."""
    rf_match = re.search(r"(?m)^## Read first\s*\n(.*?)(?=\n## |\Z)", prompt_text, re.DOTALL)
    if not rf_match:
        raise ValueError(f"{filename}: missing '## Read first' section")

    rf_content = rf_match.group(1)
    if "docs/PROTOCOL.md" not in rf_content:
        raise ValueError(f"{filename}: '## Read first' does not mention 'docs/PROTOCOL.md'")

    if "§" not in rf_content:
        raise ValueError(f"{filename}: '## Read first' does not mention at least one '§'")


def get_withdrawn_numbers(learnings_dir: Path) -> set[str]:
    """Find prompt numbers marked withdrawn in docs/learnings/."""
    withdrawn: set[str] = set()
    if not learnings_dir.is_dir():
        return withdrawn
    for f in learnings_dir.glob("*.md"):
        m = re.match(r"^(\d{4})-", f.name)
        if not m:
            continue
        text = f.read_text(encoding="utf-8")
        summary_match = re.search(r"(?m)^## Summary\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if summary_match and "withdrawn" in summary_match.group(1).lower():
            withdrawn.add(m.group(1))
    return withdrawn


# --- Positive tests against repository files ---


def test_decision_register_matches_entries() -> None:
    plan_path = Path(__file__).resolve().parents[2] / "docs" / "PLAN.md"
    text = plan_path.read_text(encoding="utf-8")
    table_ids, entry_ids = parse_decision_register(text)
    validate_decision_register(table_ids, entry_ids)


def test_phase_table_matches_prompts() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    plan_path = repo_root / "docs" / "PLAN.md"
    text = plan_path.read_text(encoding="utf-8")
    phase_numbers = parse_phase_table(text)

    queued_files = [f.name for f in (repo_root / "prompts" / "queued").glob("*.md")]
    impl_files = [f.name for f in (repo_root / "prompts" / "implemented").glob("*.md")]
    prompt_files = queued_files + impl_files

    withdrawn = get_withdrawn_numbers(repo_root / "docs" / "learnings")
    validate_phase_table(phase_numbers, prompt_files, withdrawn)


def test_prompt_numbering_invariants() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    queued_files = [f.name for f in (repo_root / "prompts" / "queued").glob("*.md")]
    impl_files = [f.name for f in (repo_root / "prompts" / "implemented").glob("*.md")]
    withdrawn = {int(w) for w in get_withdrawn_numbers(repo_root / "docs" / "learnings")}

    validate_prompt_numbering(queued_files, impl_files, withdrawn)


def test_queued_prompts_have_valid_read_first() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    queued_dir = repo_root / "prompts" / "queued"
    for prompt_file in queued_dir.glob("*.md"):
        text = prompt_file.read_text(encoding="utf-8")
        validate_read_first_section(text, filename=prompt_file.name)


# --- Negative tests on synthetic input ---


def test_negative_decision_register_missing_entry() -> None:
    table_ids = [1, 2, 3]
    entry_ids = [1, 2]  # Missing 3
    with pytest.raises(ValueError, match="missing corresponding entries"):
        validate_decision_register(table_ids, entry_ids)


def test_negative_decision_register_missing_row() -> None:
    table_ids = [1, 2]
    entry_ids = [1, 2, 3]  # Missing row 3
    with pytest.raises(ValueError, match="missing corresponding register rows"):
        validate_decision_register(table_ids, entry_ids)


def test_negative_decision_register_not_contiguous() -> None:
    table_ids = [1, 3]  # Gap
    entry_ids = [1, 3]
    with pytest.raises(ValueError, match="not contiguous"):
        validate_decision_register(table_ids, entry_ids)


def test_negative_phase_table_missing_prompt() -> None:
    phase_numbers = ["0001", "0002"]
    prompt_files = ["0001-scaffold.md"]
    with pytest.raises(ValueError, match="no prompt file or withdrawn learning"):
        validate_phase_table(phase_numbers, prompt_files)


def test_negative_phase_table_orphan_prompt() -> None:
    phase_numbers = ["0001"]
    prompt_files = ["0001-scaffold.md", "0002-data-model.md"]
    with pytest.raises(ValueError, match="no row in phase table"):
        validate_phase_table(phase_numbers, prompt_files)


def test_negative_phase_table_duplicate_prompt() -> None:
    phase_numbers = ["0001"]
    prompt_files = ["0001-scaffold.md", "0001-duplicate.md"]
    with pytest.raises(ValueError, match="more than once"):
        validate_phase_table(phase_numbers, prompt_files)


def test_negative_prompt_numbering_bad_name() -> None:
    queued = ["1-scaffold.md"]  # Not 4 digits
    with pytest.raises(ValueError, match="does not match pattern"):
        validate_prompt_numbering(queued, [])


def test_negative_prompt_numbering_duplicate_in_both_folders() -> None:
    queued = ["0002-model.md"]
    impl = ["0002-model.md"]
    with pytest.raises(ValueError, match="appears in both queued and implemented"):
        validate_prompt_numbering(queued, impl)


def test_negative_prompt_numbering_queued_not_above_implemented() -> None:
    queued = ["0001-first.md"]
    impl = ["0002-second.md"]
    with pytest.raises(ValueError, match="strictly above highest implemented"):
        validate_prompt_numbering(queued, impl)


def test_negative_prompt_numbering_gap_in_queue() -> None:
    queued = ["0002-foo.md", "0004-bar.md"]  # Missing 0003
    impl = ["0001-init.md"]
    with pytest.raises(ValueError, match="not contiguous"):
        validate_prompt_numbering(queued, impl)


def test_negative_read_first_missing_section() -> None:
    text = "# 0001 — Title\n\n## Objective\nDo something.\n"
    with pytest.raises(ValueError, match="missing '## Read first' section"):
        validate_read_first_section(text)


def test_negative_read_first_missing_protocol() -> None:
    text = "# 0001 — Title\n\n## Read first\n- `docs/PLAN.md` §3.1\n\n## Objective\n"
    with pytest.raises(ValueError, match=r"does not mention 'docs/PROTOCOL\.md'"):
        validate_read_first_section(text)


def test_negative_read_first_missing_section_sign() -> None:
    text = (
        "# 0001 — Title\n\n## Read first\n- `docs/PROTOCOL.md`\n- `docs/PLAN.md`\n\n## Objective\n"
    )
    with pytest.raises(ValueError, match="does not mention at least one '§'"):
        validate_read_first_section(text)

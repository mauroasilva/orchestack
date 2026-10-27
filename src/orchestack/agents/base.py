# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""AgentBackend protocol and normalized session/permission models for Orchestack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PermissionDenial:
    """Normalized representation of a sandbox or tool permission denial."""

    tool_name: str
    resource: str
    command_prefix: str | None = None
    raw_reason: str = ""


@dataclass(frozen=True)
class AgentSessionResult:
    """Outcome of a single agent execution turn."""

    session_id: str | None
    exit_code: int
    completed_normally: bool
    error_message: str | None
    last_response: str
    cli_log_offset: int = 0
    turn_start_time: float = 0.0


@dataclass(frozen=True)
class HeartbeatInfo:
    """Live heartbeat and transcript progress snapshot for an active session."""

    active: bool
    summary: str = ""
    updated_at: float = 0.0
    latest_activity: str = ""

    def __post_init__(self) -> None:
        if self.summary and not self.latest_activity:
            object.__setattr__(self, "latest_activity", self.summary)
        elif self.latest_activity and not self.summary:
            object.__setattr__(self, "summary", self.latest_activity)


HeartbeatSnapshot = HeartbeatInfo


@runtime_checkable
class AgentBackend(Protocol):
    """Protocol implemented by all coding agent CLI backends."""

    name: str

    def check_available(self) -> tuple[bool, str]:
        """Return `(True, path_or_version)` if CLI is available, else `(False, reason)`."""
        ...

    def run_turn(
        self,
        prompt: str,
        repo_root: Path,
        session_id: str | None = None,
        timeout: int = 1800,
        cli_log_path: Path | None = None,
    ) -> AgentSessionResult:
        """Execute a single agent turn against `repo_root`, optionally resuming `session_id`."""
        ...

    def get_latest_session_id(
        self,
        repo_root: Path,
        since_timestamp: float | None = None,
    ) -> str | None:
        """Resolve the most recent session ID created or updated since `since_timestamp`."""
        ...

    def get_session_status(
        self,
        session_id: str,
        since_timestamp: float | None = None,
    ) -> tuple[bool, str | None]:
        """Return `(is_active_or_found, status_summary)` for `session_id`."""
        ...

    def extract_last_response(
        self,
        session_id: str,
        cli_log_path: Path | None = None,
    ) -> str:
        """Return the final assistant/planner response text for `session_id`."""
        ...

    def detect_permission_denials(
        self,
        session_id: str | None,
        cli_log_path: Path | None,
        cli_log_offset: int = 0,
        turn_start_time: float | None = None,
    ) -> list[PermissionDenial]:
        """Inspect transcripts and CLI logs from the current turn for permission denials."""
        ...

    def whitelist_denial(
        self,
        denial: PermissionDenial,
        repo_root: Path,
    ) -> bool:
        """Persist an allow rule for `denial` in the agent's settings file."""
        ...

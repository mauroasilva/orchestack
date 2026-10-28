# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Re-exports for `ClaudeCodeAgent` from `orchestack.agents.claude_code`."""

from __future__ import annotations

from orchestack.agents.claude_code import (
    DEFAULT_CLAUDE_PROJECTS_DIR,
    ClaudeCodeAgent,
    extract_heartbeat_from_events,
    extract_result_from_events,
    extract_session_id_from_events,
    format_claude_allow_rule,
    parse_claude_permission_denials,
    parse_claude_stream_jsonl,
    project_slug_for_repo,
    read_claude_log_slice,
    whitelist_claude_denial_in_settings,
)

__all__ = [
    "DEFAULT_CLAUDE_PROJECTS_DIR",
    "ClaudeCodeAgent",
    "extract_heartbeat_from_events",
    "extract_result_from_events",
    "extract_session_id_from_events",
    "format_claude_allow_rule",
    "parse_claude_permission_denials",
    "parse_claude_stream_jsonl",
    "project_slug_for_repo",
    "read_claude_log_slice",
    "whitelist_claude_denial_in_settings",
]

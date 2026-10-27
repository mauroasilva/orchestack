# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Coding agent CLI backend protocols and implementations for Orchestack."""

from __future__ import annotations

from orchestack.agents.antigravity import (
    AntigravityAgent,
    _get_conversation_mtime,
    _normalize_db_timestamp,
    add_allowed_rules,
    check_recent_soft_denial_in_cli_log,
    check_recent_transcript_denial,
    extract_command_prefix,
    extract_denied_permission,
    extract_last_agent_response,
    extract_latest_agent_summary,
    extract_session_transcript_metrics,
    get_latest_conversation_id,
    get_session_transcript_status,
    initialize_baseline_permissions,
    is_transient_session_error,
    parse_denial_from_step,
    parse_iso_timestamp,
    whitelist_command_in_settings,
    whitelist_url_in_settings,
)
from orchestack.agents.base import (
    AgentBackend,
    AgentSessionResult,
    HeartbeatInfo,
    HeartbeatSnapshot,
    PermissionDenial,
)

__all__ = [
    "AgentBackend",
    "AgentSessionResult",
    "AntigravityAgent",
    "HeartbeatInfo",
    "HeartbeatSnapshot",
    "PermissionDenial",
    "_get_conversation_mtime",
    "_normalize_db_timestamp",
    "add_allowed_rules",
    "check_recent_soft_denial_in_cli_log",
    "check_recent_transcript_denial",
    "extract_command_prefix",
    "extract_denied_permission",
    "extract_last_agent_response",
    "extract_latest_agent_summary",
    "extract_session_transcript_metrics",
    "get_latest_conversation_id",
    "get_session_transcript_status",
    "initialize_baseline_permissions",
    "is_transient_session_error",
    "parse_denial_from_step",
    "parse_iso_timestamp",
    "whitelist_command_in_settings",
    "whitelist_url_in_settings",
]

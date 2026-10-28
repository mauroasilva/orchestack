#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

set -euo pipefail

# Ensure Homebrew and local bin are in PATH
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export UV_NO_SYNC="${UV_NO_SYNC:-1}"

# Move to repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

show_help() {
    cat <<'EOF'
Usage: scripts/run_daemon.sh [OPTIONS]

Wrapper script for the Orchestack autonomous daemon runner.

Common Options:
  --repo PATH             Target repository root path (default: current working directory)
  --forge FORGE           Forge provider override: auto, github, gitlab (default: auto)
  --agent AGENT           Agent backend override: antigravity, claude, codex
  --once                  Run a single prompt cycle and exit
  --auto-whitelist        Automatically trust repository collaborators/members for PR review feedback
  --no-auto-whitelist     Disable automatic collaborator whitelisting
  --interval SECONDS      Initial seconds between polling for merged PRs (alias for --poll-interval)
  --poll-interval SECONDS Initial seconds between polling for merged PRs (default: 30)
  -h, --help              Show this help message and full daemon_runner.py options
EOF
}

FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            if [[ $# -lt 2 ]]; then
                echo "Error: --repo requires a path argument" >&2
                exit 2
            fi
            FORWARD_ARGS+=("--repo" "$2")
            shift 2
            ;;
        --repo=*)
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --forge)
            if [[ $# -lt 2 ]]; then
                echo "Error: --forge requires a provider argument" >&2
                exit 2
            fi
            FORWARD_ARGS+=("--forge" "$2")
            shift 2
            ;;
        --forge=*)
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --agent)
            if [[ $# -lt 2 ]]; then
                echo "Error: --agent requires an agent backend argument" >&2
                exit 2
            fi
            FORWARD_ARGS+=("--agent" "$2")
            shift 2
            ;;
        --agent=*)
            FORWARD_ARGS+=("$1")
            shift
            ;;
        --interval)
            if [[ $# -lt 2 ]]; then
                echo "Error: --interval requires a seconds argument" >&2
                exit 2
            fi
            FORWARD_ARGS+=("--poll-interval" "$2")
            shift 2
            ;;
        --interval=*)
            FORWARD_ARGS+=("--poll-interval" "${1#*=}")
            shift
            ;;
        --once|--auto-whitelist|--no-auto-whitelist)
            FORWARD_ARGS+=("$1")
            shift
            ;;
        -h|--help)
            show_help
            echo ""
            FORWARD_ARGS+=("--help")
            shift
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

echo "=================================================="
echo " Starting Orchestack Autonomous Daemon            "
echo " Model: gemini-3.8-flash-high (Reasoning: High)   "
echo "=================================================="

# Check for uv / python3
if command -v uv >/dev/null 2>&1; then
    exec uv run python scripts/daemon_runner.py ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
else
    exec python3 scripts/daemon_runner.py ${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}
fi

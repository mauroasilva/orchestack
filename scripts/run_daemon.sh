#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Blue Team Harness Authors

# Ensure Homebrew and local bin are in PATH
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

# Move to repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1

echo "=================================================="
echo " Starting Blue Team Harness Autonomous Daemon     "
echo " Model: gemini-3.8-flash-high (Reasoning: High)   "
echo "=================================================="

# Check for uv / python3
if command -v uv >/dev/null 2>&1; then
    exec uv run python scripts/daemon_runner.py "$@"
else
    exec python3 scripts/daemon_runner.py "$@"
fi

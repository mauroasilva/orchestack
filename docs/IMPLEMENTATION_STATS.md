# Implementation Statistics

This document tracks the cumulative implementation time, pricing-grouped token breakdown (`input_tokens`, `cache_write_tokens`, `cache_read_tokens`, `output_tokens`), and model-based USD cost for each Orchestack delivery phase, aggregated across all coding agent implementation sessions and PR/MR review or auto-heal turns.

| Phase | Model | Implementation Time | `input_tokens` | `cache_write_tokens` | `cache_read_tokens` | `output_tokens` | Total Tokens | Total Cost |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `0001-core-package-and-repo-config` | `gemini` | 38m 7s | 737,316 | 0 | 5,851,050 | 68,363 | 6,656,729 | $3.09 |
| `0002-forge-provider-protocol-and-github` | `gemini` | 19m 30s | 532,121 | 0 | 4,682,030 | 72,164 | 5,286,315 | $2.49 |
| `0003-gitlab-forge-provider` | `gemini` | 1h 1m 43s | 322,999 | 0 | 2,315,730 | 56,462 | 2,695,191 | $1.41 |
| `0004-agent-backend-protocol-and-antigravity` | `gemini` | 29m 57s | 486,240 | 0 | 4,659,386 | 81,183 | 5,226,809 | $2.47 |
| `0005-claude-code-and-codex-agent-backends` | `gemini` | 23m 29s | 506,677 | 0 | 3,799,466 | 87,245 | 4,393,388 | $2.26 |
| `0006-cli-subcommands-and-multi-agent-telemetry` | `gemini` | 22m 2s | 317,363 | 0 | 4,629,054 | 81,998 | 5,028,415 | $2.25 |
| `0007-phase-implementation-stats-tracking` | `gemini` | 2h 30m 0s | 1,770,733 | 0 | 17,006,306 | 239,661 | 19,016,700 | $8.73 |
| `0008-phase-0000-bootstrap-and-pr-interview` | `gemini` | 53m 47s | 991,390 | 0 | 6,034,592 | 84,892 | 7,110,874 | $3.55 |
| **Total** | **—** | **6h 38m 35s** | **5,664,839** | **0** | **48,977,614** | **771,968** | **55,414,421** | **$26.25** |

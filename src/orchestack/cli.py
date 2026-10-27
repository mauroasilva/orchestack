# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Command-line entrypoint for Orchestack."""

from __future__ import annotations

import typer

from orchestack import __version__

app = typer.Typer(
    name="orchestack",
    help=(
        "Autonomous multi-repo prompt queue, CI auto-healer, and PR/MR review daemon "
        "for Antigravity (agy), Claude Code (claude), and OpenAI Codex (codex) "
        "across GitHub and GitLab."
    ),
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the Orchestack version."""
    typer.echo(f"orchestack {__version__}")


if __name__ == "__main__":
    app()

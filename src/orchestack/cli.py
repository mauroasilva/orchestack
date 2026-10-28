# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Orchestack Authors

"""Command-line entrypoint and subcommands (`run`, `init`, `status`, `doctor`, `telemetry`)."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Annotated, Any

import typer

from orchestack import __version__
from orchestack.agents.factory import create_agent_backend, normalize_agent_backend_name
from orchestack.config import (
    RepoConfig,
    configure_runtime,
    detect_base_branch,
    detect_check_cmd,
    load_repo_config,
)
from orchestack.forge.detect import (
    create_forge_provider,
    detect_forge_type,
    get_git_remote_url,
    parse_git_remote,
)
from orchestack.telemetry.reporter import (
    build_canonical_slug_map,
    collect_multi_agent_telemetry,
    format_telemetry_json,
    format_telemetry_markdown,
)

app = typer.Typer(
    name="orchestack",
    help=(
        "Autonomous multi-repo prompt queue, CI auto-healer, and PR/MR review daemon "
        "for Antigravity (agy), Claude Code (claude), and OpenAI Codex (codex) "
        "across GitHub and GitLab."
    ),
    no_args_is_help=False,
)


def _load_daemon_runner_module() -> Any:
    """Import `daemon_runner` from `scripts/` or `sys.modules`."""
    for mod_name in ("daemon_runner", "scripts.daemon_runner"):
        if mod_name in sys.modules:
            return sys.modules[mod_name]

    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if scripts_dir.is_dir() and str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("daemon_runner")


def run_daemon(
    *,
    repo: Path | None = None,
    forge: str | None = None,
    agent: str | None = None,
    once: bool = False,
    interval: int | None = None,
    auto_whitelist: bool | None = None,
    dry_run: bool = False,
    base_branch: str | None = None,
    branch_prefix: str | None = None,
    check_cmd: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    resume: bool = False,
    fresh: bool = False,
    max_poll_interval: int | None = None,
    max_auto_heal_attempts: int | None = None,
    no_require_learnings: bool = False,
    no_require_pr_description: bool = False,
) -> None:
    """Configure runtime and invoke `daemon_runner.main()` with constructed CLI arguments."""
    target_root = (repo if repo is not None else Path.cwd()).expanduser().resolve()
    cfg = load_repo_config(
        repo_root=target_root,
        base_branch=base_branch,
        branch_prefix=branch_prefix,
        check_cmd=check_cmd,
        forge_type=forge,
        agent_type=agent,
        require_learnings=False if no_require_learnings else None,
        require_pr_description=False if no_require_pr_description else None,
        auto_whitelist_collaborators=auto_whitelist,
    )
    daemon_mod = _load_daemon_runner_module()
    configure_runtime(cfg)

    argv: list[str] = ["daemon_runner.py", "--repo", str(target_root)]
    if base_branch is not None:
        argv.extend(["--base-branch", base_branch])
    if branch_prefix is not None:
        argv.extend(["--branch-prefix", branch_prefix])
    if check_cmd is not None:
        argv.extend(["--check-cmd", check_cmd])
    if forge is not None:
        argv.extend(["--forge", forge])
    if agent is not None:
        argv.extend(["--agent", agent])
    if no_require_learnings:
        argv.append("--no-require-learnings")
    if no_require_pr_description:
        argv.append("--no-require-pr-description")
    if auto_whitelist is True:
        argv.append("--auto-whitelist")
    elif auto_whitelist is False:
        argv.append("--no-auto-whitelist")
    if interval is not None:
        argv.extend(["--poll-interval", str(interval)])
    if max_poll_interval is not None:
        argv.extend(["--max-poll-interval", str(max_poll_interval)])
    if max_auto_heal_attempts is not None:
        argv.extend(["--max-auto-heal-attempts", str(max_auto_heal_attempts)])
    if model is not None:
        argv.extend(["--model", model])
    if effort is not None:
        argv.extend(["--effort", effort])
    if once:
        argv.append("--once")
    if resume:
        argv.append("--resume")
    if fresh:
        argv.append("--fresh")
    if dry_run:
        argv.append("--dry-run")

    orig_argv = sys.argv[:]
    try:
        sys.argv = argv
        daemon_mod.main()
    finally:
        sys.argv = orig_argv


def init_repository(
    repo_root: Path | None = None,
    *,
    forge: str = "auto",
    agent: str = "agy",
    check_cmd: str | None = None,
    base_branch: str | None = None,
    branch_prefix: str = "orchestack",
) -> dict[str, Any]:
    """Scaffold `.orchestack.toml`, prompt/learnings directories, and `.gitignore` entries."""
    target_root = (repo_root if repo_root is not None else Path.cwd()).expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)

    queued_dir = target_root / "prompts" / "queued"
    implemented_dir = target_root / "prompts" / "implemented"
    learnings_dir = target_root / "docs" / "learnings"

    for directory in (queued_dir, implemented_dir, learnings_dir):
        directory.mkdir(parents=True, exist_ok=True)
        gitkeep = directory / ".gitkeep"
        if not any(directory.iterdir()):
            gitkeep.write_text("", encoding="utf-8")

    effective_base = base_branch if base_branch is not None else detect_base_branch(target_root)
    effective_check = check_cmd if check_cmd is not None else detect_check_cmd(target_root)
    normalized_agent = normalize_agent_backend_name(agent)

    toml_path = target_root / ".orchestack.toml"
    toml_content = (
        "# Orchestack repository configuration\n"
        "[repo]\n"
        f'base_branch = "{effective_base}"\n'
        f'branch_prefix = "{branch_prefix}"\n'
        'queued_dir = "prompts/queued"\n'
        'implemented_dir = "prompts/implemented"\n'
        'learnings_dir = "docs/learnings"\n'
        'pr_description_path = "docs/PR_DESCRIPTION.md"\n'
        'daemon_dir = ".daemon"\n'
        "\n"
        "[dod]\n"
        f'check_cmd = "{effective_check}"\n'
        "require_learnings = true\n"
        "require_pr_description = true\n"
        "\n"
        "[forge]\n"
        f'provider = "{forge}"\n'
        "\n"
        "[agent]\n"
        f'backend = "{normalized_agent}"\n'
    )
    toml_path.write_text(toml_content, encoding="utf-8")

    gitignore_path = target_root / ".gitignore"
    existing_gitignore = ""
    if gitignore_path.is_file():
        existing_gitignore = gitignore_path.read_text(encoding="utf-8", errors="replace")

    lines = [line.strip() for line in existing_gitignore.splitlines()]
    additions: list[str] = []
    if ".daemon/" not in lines and ".daemon" not in lines:
        additions.append(".daemon/")
    if "docs/PR_DESCRIPTION.md" not in lines:
        additions.append("docs/PR_DESCRIPTION.md")

    if additions:
        prefix = (
            existing_gitignore
            if not existing_gitignore or existing_gitignore.endswith("\n")
            else existing_gitignore + "\n"
        )
        gitignore_path.write_text(
            prefix + "\n".join(additions) + "\n",
            encoding="utf-8",
        )

    return {
        "repo_root": str(target_root),
        "config_file": str(toml_path),
        "queued_dir": str(queued_dir),
        "implemented_dir": str(implemented_dir),
        "learnings_dir": str(learnings_dir),
        "gitignore": str(gitignore_path),
        "forge": forge,
        "agent": normalized_agent,
        "base_branch": effective_base,
        "check_cmd": effective_check,
    }


def _resolve_effective_forge(config: RepoConfig) -> str:
    """Resolve the active forge provider name (`github`, `gitlab`, `local`, or `unknown`)."""
    raw = (config.forge_type or "auto").strip().lower()
    if raw not in {"auto", ""}:
        return raw
    try:
        return detect_forge_type(config.repo_root)
    except Exception:
        return "unknown"


def _resolve_effective_agent(config: RepoConfig) -> str:
    """Resolve the normalized agent backend name (`agy`, `claude`, `codex`)."""
    try:
        return normalize_agent_backend_name(config.agent_type)
    except ValueError:
        return config.agent_type


def collect_repo_status(repo_root: Path | None = None) -> dict[str, Any]:
    """Gather repository configuration, daemon lock, prompt queue, and open PR/MR status."""
    config = load_repo_config(repo_root=repo_root)
    detected_forge = _resolve_effective_forge(config)
    detected_agent = _resolve_effective_agent(config)

    daemon_locked = config.lock_file.is_file()
    lock_info = ""
    if daemon_locked:
        try:
            lock_info = config.lock_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            lock_info = "locked"

    queued_prompts: list[str] = []
    if config.queued_dir.is_dir():
        queued_prompts = sorted(f.name for f in config.queued_dir.glob("*.md"))

    implemented_prompts: list[str] = []
    if config.implemented_dir.is_dir():
        implemented_prompts = sorted(f.name for f in config.implemented_dir.glob("*.md"))

    open_prs_data: list[dict[str, Any]] = []
    if detected_forge in {"github", "gitlab"}:
        try:
            provider = create_forge_provider(config)
            open_prs = provider.list_open_prs()
            for pr in open_prs:
                ci_status = "UNKNOWN"
                unprocessed_reviews_count = 0
                try:
                    ci_res: Any = provider.get_ci_status(pr.number)
                    if isinstance(ci_res, tuple):
                        ci_status = str(ci_res[0])
                    else:
                        ci_status = str(getattr(ci_res, "status", ci_res))
                except Exception:
                    pass
                try:
                    reviews, _ = provider.fetch_unprocessed_reviews(pr.number, [])
                    unprocessed_reviews_count = len(reviews)
                except Exception:
                    pass
                open_prs_data.append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "url": pr.url,
                        "head_ref": pr.head_ref,
                        "base_ref": pr.base_ref,
                        "state": pr.state,
                        "ci_status": ci_status,
                        "unprocessed_reviews": unprocessed_reviews_count,
                    }
                )
        except Exception:
            pass

    return {
        "repo_root": str(config.repo_root),
        "base_branch": config.base_branch,
        "branch_prefix": config.branch_prefix,
        "check_cmd": config.check_cmd,
        "forge": detected_forge,
        "configured_forge": config.forge_type,
        "agent": detected_agent,
        "configured_agent": config.agent_type,
        "daemon_locked": daemon_locked,
        "lock_file": str(config.lock_file),
        "lock_info": lock_info,
        "queued_prompts": queued_prompts,
        "queued_count": len(queued_prompts),
        "implemented_prompts": implemented_prompts,
        "implemented_count": len(implemented_prompts),
        "open_prs": open_prs_data,
    }


def run_doctor_checks(
    repo_root: Path | None = None,
    *,
    forge_override: str | None = None,
    agent_override: str | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Run diagnostic checks on `.orchestack.toml`, git remote, forge CLI auth, and agent CLI."""
    target_root = (repo_root if repo_root is not None else Path.cwd()).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    # 1. Validate .orchestack.toml syntax and load RepoConfig
    config_file = target_root / ".orchestack.toml"
    config: RepoConfig | None = None
    if config_file.is_file():
        try:
            with config_file.open("rb") as f:
                tomllib.load(f)
            config = load_repo_config(
                repo_root=target_root,
                forge_type=forge_override,
                agent_type=agent_override,
            )
            checks.append(
                {
                    "name": "toml_config",
                    "passed": True,
                    "detail": f"Valid .orchestack.toml at {config_file}",
                }
            )
        except (tomllib.TOMLDecodeError, ValueError, OSError) as exc:
            checks.append(
                {
                    "name": "toml_config",
                    "passed": False,
                    "detail": f"Invalid .orchestack.toml ({config_file}): {exc}",
                }
            )
    else:
        try:
            config = load_repo_config(
                repo_root=target_root,
                forge_type=forge_override,
                agent_type=agent_override,
            )
            checks.append(
                {
                    "name": "toml_config",
                    "passed": True,
                    "detail": "No .orchestack.toml found (using auto-detected defaults)",
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "toml_config",
                    "passed": False,
                    "detail": f"Configuration error: {exc}",
                }
            )

    if config is None:
        config = RepoConfig(
            repo_root=target_root,
            forge_type=forge_override or "auto",
            agent_type=agent_override or "agy",
        )

    # 2. Check valid git repository & origin remote
    is_git_repo = False
    try:
        git_res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        is_git_repo = git_res.returncode == 0 and git_res.stdout.strip().lower() == "true"
    except (OSError, subprocess.SubprocessError):
        is_git_repo = (target_root / ".git").exists()

    remote_url = get_git_remote_url(target_root) if is_git_repo else None
    if is_git_repo and remote_url:
        checks.append(
            {
                "name": "git_repo",
                "passed": True,
                "detail": f"Git repository at {target_root} (origin: {remote_url})",
            }
        )
    elif is_git_repo:
        checks.append(
            {
                "name": "git_repo",
                "passed": False,
                "detail": f"Git repository at {target_root} is missing an 'origin' remote",
            }
        )
    else:
        checks.append(
            {
                "name": "git_repo",
                "passed": False,
                "detail": f"{target_root} is not a valid git repository",
            }
        )

    # 3. Check forge CLI installed & authenticated
    effective_forge = (forge_override or config.forge_type or "auto").strip().lower()
    if effective_forge == "auto":
        if remote_url:
            try:
                effective_forge, _, _ = parse_git_remote(remote_url)
            except ValueError:
                effective_forge = "unknown"
        else:
            effective_forge = "github"

    if effective_forge in {"github", "gitlab"}:
        cli_bin_name = "gh" if effective_forge == "github" else "glab"
        cli_bin_path = shutil.which(cli_bin_name)
        if not cli_bin_path:
            checks.append(
                {
                    "name": "forge_auth",
                    "passed": False,
                    "detail": f"Forge CLI '{cli_bin_name}' ({effective_forge}) not found in PATH",
                }
            )
        else:
            try:
                auth_res = subprocess.run(
                    [cli_bin_path, "auth", "status"],
                    cwd=target_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                auth_out = (auth_res.stdout or auth_res.stderr or "").strip()
                first_line = auth_out.splitlines()[0] if auth_out else f"{cli_bin_name} auth status"
                if auth_res.returncode == 0:
                    checks.append(
                        {
                            "name": "forge_auth",
                            "passed": True,
                            "detail": (
                                f"{effective_forge} CLI ({cli_bin_path}) "
                                f"authenticated: {first_line}"
                            ),
                        }
                    )
                else:
                    checks.append(
                        {
                            "name": "forge_auth",
                            "passed": False,
                            "detail": (
                                f"{effective_forge} CLI ({cli_bin_name}) "
                                f"not authenticated: {first_line}"
                            ),
                        }
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                checks.append(
                    {
                        "name": "forge_auth",
                        "passed": False,
                        "detail": f"Failed to run '{cli_bin_name} auth status': {exc}",
                    }
                )
    else:
        checks.append(
            {
                "name": "forge_auth",
                "passed": False,
                "detail": f"Could not determine supported forge provider (got {effective_forge!r})",
            }
        )

    # 4. Check selected agent CLI installed
    raw_agent = agent_override or config.agent_type or "agy"
    try:
        backend = create_agent_backend(raw_agent, repo_root=target_root)
        agent_ok, agent_detail = backend.check_available()
        checks.append(
            {
                "name": "agent_cli",
                "passed": agent_ok,
                "detail": f"Agent '{backend.name}': {agent_detail}",
            }
        )
    except ValueError as exc:
        checks.append(
            {
                "name": "agent_cli",
                "passed": False,
                "detail": str(exc),
            }
        )

    all_passed = all(bool(c["passed"]) for c in checks)
    return all_passed, checks


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    show_version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Print the Orchestack version and exit."),
    ] = False,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root directory."),
    ] = None,
    forge: Annotated[
        str | None,
        typer.Option("--forge", help="Forge provider: auto, github, or gitlab."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Coding agent backend: agy, claude, or codex."),
    ] = None,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single daemon cycle and exit."),
    ] = False,
    interval: Annotated[
        int | None,
        typer.Option(
            "--interval",
            "--poll-interval",
            help="Polling interval in seconds between PR/MR checks.",
        ),
    ] = None,
    auto_whitelist: Annotated[
        bool | None,
        typer.Option(
            "--auto-whitelist/--no-auto-whitelist",
            help="Automatically whitelist permission denials / collaborators.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Simulate daemon loop without invoking the agent."),
    ] = False,
    base_branch: Annotated[
        str | None,
        typer.Option("--base-branch", help="Target base branch (e.g. main, master)."),
    ] = None,
    branch_prefix: Annotated[
        str | None,
        typer.Option("--branch-prefix", help="Branch prefix for prompt branches."),
    ] = None,
    check_cmd: Annotated[
        str | None,
        typer.Option("--check-cmd", help="Verification command for Definition of Done."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model override for the coding agent."),
    ] = None,
    effort: Annotated[
        str | None,
        typer.Option("--effort", help="Reasoning effort override (low, medium, high, none)."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume an existing session on the active branch."),
    ] = False,
    fresh: Annotated[
        bool,
        typer.Option("--fresh", help="Force a clean branch/session start."),
    ] = False,
) -> None:
    """Orchestack CLI root callback supporting both subcommands and legacy daemon flags (`D11`)."""
    if show_version:
        typer.echo(f"orchestack {__version__}")
        raise typer.Exit(0)

    if ctx.invoked_subcommand is not None:
        return

    has_legacy_flags = any(
        (
            once,
            dry_run,
            repo is not None,
            forge is not None,
            agent is not None,
            interval is not None,
            auto_whitelist is not None,
            base_branch is not None,
            branch_prefix is not None,
            check_cmd is not None,
            model is not None,
            effort is not None,
            resume,
            fresh,
        )
    )
    if has_legacy_flags:
        run_daemon(
            repo=repo,
            forge=forge,
            agent=agent,
            once=once,
            interval=interval,
            auto_whitelist=auto_whitelist,
            dry_run=dry_run,
            base_branch=base_branch,
            branch_prefix=branch_prefix,
            check_cmd=check_cmd,
            model=model,
            effort=effort,
            resume=resume,
            fresh=fresh,
        )
        return

    typer.echo(ctx.get_help())


@app.command("run")
def run_cmd_entry(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root directory."),
    ] = None,
    forge: Annotated[
        str | None,
        typer.Option("--forge", help="Forge provider: auto, github, or gitlab."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Coding agent backend: agy, claude, or codex."),
    ] = None,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single daemon cycle and exit."),
    ] = False,
    interval: Annotated[
        int | None,
        typer.Option(
            "--interval",
            "--poll-interval",
            help="Polling interval in seconds between PR/MR checks.",
        ),
    ] = None,
    auto_whitelist: Annotated[
        bool | None,
        typer.Option(
            "--auto-whitelist/--no-auto-whitelist",
            help="Automatically whitelist permission denials / collaborators.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Simulate daemon loop without invoking the agent."),
    ] = False,
    base_branch: Annotated[
        str | None,
        typer.Option("--base-branch", help="Target base branch (e.g. main, master)."),
    ] = None,
    branch_prefix: Annotated[
        str | None,
        typer.Option("--branch-prefix", help="Branch prefix for prompt branches."),
    ] = None,
    check_cmd: Annotated[
        str | None,
        typer.Option("--check-cmd", help="Verification command for Definition of Done."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model override for the coding agent."),
    ] = None,
    effort: Annotated[
        str | None,
        typer.Option("--effort", help="Reasoning effort override (low, medium, high, none)."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume an existing session on the active branch."),
    ] = False,
    fresh: Annotated[
        bool,
        typer.Option("--fresh", help="Force a clean branch/session start."),
    ] = False,
) -> None:
    """Launch the Orchestack autonomous daemon against the target repository."""
    run_daemon(
        repo=repo,
        forge=forge,
        agent=agent,
        once=once,
        interval=interval,
        auto_whitelist=auto_whitelist,
        dry_run=dry_run,
        base_branch=base_branch,
        branch_prefix=branch_prefix,
        check_cmd=check_cmd,
        model=model,
        effort=effort,
        resume=resume,
        fresh=fresh,
    )


@app.command("init")
def init_cmd_entry(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root directory."),
    ] = None,
    forge: Annotated[
        str,
        typer.Option("--forge", help="Forge provider: auto, github, or gitlab."),
    ] = "auto",
    agent: Annotated[
        str,
        typer.Option("--agent", help="Coding agent backend: agy, claude, or codex."),
    ] = "agy",
    check_cmd: Annotated[
        str | None,
        typer.Option("--check-cmd", help="Verification command for Definition of Done."),
    ] = None,
    base_branch: Annotated[
        str | None,
        typer.Option("--base-branch", help="Default base branch (auto-detected if omitted)."),
    ] = None,
    branch_prefix: Annotated[
        str,
        typer.Option("--branch-prefix", help="Branch prefix for prompt branches."),
    ] = "orchestack",
) -> None:
    """Scaffold `.orchestack.toml`, prompt/learnings directories, and `.gitignore`."""
    try:
        result = init_repository(
            repo_root=repo,
            forge=forge,
            agent=agent,
            check_cmd=check_cmd,
            base_branch=base_branch,
            branch_prefix=branch_prefix,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Initialized Orchestack repository at {result['repo_root']}")
    typer.echo(f"  Config file     : {result['config_file']}")
    typer.echo(f"  Queued prompts  : {result['queued_dir']}")
    typer.echo(f"  Implemented     : {result['implemented_dir']}")
    typer.echo(f"  Learnings       : {result['learnings_dir']}")
    typer.echo(f"  Forge / Agent   : {result['forge']} / {result['agent']}")
    typer.echo(f"  Check command   : {result['check_cmd']}")


@app.command("status")
def status_cmd_entry(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root directory."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output repository status as formatted JSON."),
    ] = False,
) -> None:
    """Display target repo configuration, lock status, queued prompts, and open PRs/MRs."""
    status_data = collect_repo_status(repo_root=repo)
    if as_json:
        typer.echo(json.dumps(status_data, indent=2))
        return

    lock_str = "LOCKED" if status_data["daemon_locked"] else "unlocked"
    typer.echo(f"Orchestack Status — {status_data['repo_root']}")
    typer.echo(f"  Base branch     : {status_data['base_branch']}")
    typer.echo(f"  Forge provider  : {status_data['forge']}")
    typer.echo(f"  Agent backend   : {status_data['agent']}")
    typer.echo(f"  Daemon lock     : {lock_str} ({status_data['lock_file']})")
    typer.echo(f"  Implemented     : {status_data['implemented_count']} prompt(s)")
    typer.echo(f"  Queued          : {status_data['queued_count']} prompt(s)")
    for q_name in status_data["queued_prompts"]:
        typer.echo(f"    - {q_name}")

    open_prs = status_data["open_prs"]
    typer.echo(f"  Open PRs/MRs    : {len(open_prs)}")
    for pr in open_prs:
        typer.echo(
            f"    - #{pr['number']} [{pr['ci_status']}] {pr['title']} "
            f"(unprocessed reviews: {pr['unprocessed_reviews']})"
        )


@app.command("doctor")
def doctor_cmd_entry(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root directory."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output diagnostic checks as formatted JSON."),
    ] = False,
) -> None:
    """Run diagnostic checks on git remote, forge CLI auth, agent CLI, and `.orchestack.toml`."""
    all_passed, checks = run_doctor_checks(repo_root=repo)
    if as_json:
        typer.echo(json.dumps({"ok": all_passed, "checks": checks}, indent=2))
        if not all_passed:
            raise typer.Exit(1)
        return

    typer.echo("Orchestack Doctor Diagnostics:")
    for item in checks:
        badge = "[OK]" if item["passed"] else "[FAIL]"
        typer.echo(f"  {badge:<6} {item['name']}: {item['detail']}")

    if not all_passed:
        raise typer.Exit(1)


@app.command("telemetry")
def telemetry_cmd_entry(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repository root directory."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: markdown or json."),
    ] = "markdown",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write telemetry report to file instead of stdout."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Shorthand for --format json."),
    ] = False,
) -> None:
    """Generate unified session telemetry across Antigravity, Claude Code, and Codex."""
    target_root = (repo if repo is not None else Path.cwd()).expanduser().resolve()
    fmt = "json" if as_json else output_format.strip().lower()
    if fmt not in {"markdown", "md", "json"}:
        typer.echo(
            f"Error: Unsupported --format {output_format!r}. Choose 'markdown' or 'json'.", err=True
        )
        raise typer.Exit(1)

    slug_map = build_canonical_slug_map(target_root)
    all_sessions, by_prompt = collect_multi_agent_telemetry(repo_root=target_root)

    if fmt == "json":
        rendered = format_telemetry_json(all_sessions, by_prompt, slug_map)
    else:
        rendered = format_telemetry_markdown(all_sessions, by_prompt, slug_map)

    if output is not None:
        out_path = output.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        typer.echo(f"Telemetry report written to {out_path}")
    else:
        typer.echo(rendered)


@app.command("version")
def version() -> None:
    """Print the Orchestack version."""
    typer.echo(f"orchestack {__version__}")


if __name__ == "__main__":
    app()

"""Vimgym CLI — AI session memory for developers."""
from __future__ import annotations

import argparse
import json as _json
import sys
import webbrowser

from vimgym import __version__


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vg",
        description="Vimgym — AI session memory for developers",
    )
    parser.add_argument("--version", action="version", version=f"vimgym {__version__}")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("init",   help="Initialize vault and detect AI tool sources")
    sub.add_parser("start",  help="Start daemon (watcher + web server)")
    sub.add_parser("stop",   help="Stop daemon")
    sub.add_parser("status", help="Show daemon status and vault stats")
    sub.add_parser("open",   help="Open browser UI")

    search_p = sub.add_parser("search", help="Search sessions")
    search_p.add_argument("query", nargs="?", help="Search query")
    search_p.add_argument("--project", help="Filter by project name")
    search_p.add_argument("--branch", help="Filter by git branch")
    search_p.add_argument("--since", help="Filter by date (ISO or Nd: 7d, 30d)")
    search_p.add_argument("--limit", type=int, default=20)
    search_p.add_argument("--json", action="store_true", dest="as_json")

    config_p = sub.add_parser("config", help="View or modify configuration")
    config_sub = config_p.add_subparsers(dest="config_cmd", metavar="SUBCOMMAND")
    sources_p = config_sub.add_parser("sources", help="List configured AI tool sources")
    sources_p.add_argument("source_id", nargs="?", help="Source id to enable/disable")
    sources_p.add_argument("--enable",  action="store_true")
    sources_p.add_argument("--disable", action="store_true")

    return parser


def main() -> None:
    parser = _make_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    cmd = args.command
    if cmd == "init":
        sys.exit(_cmd_init())
    if cmd == "start":
        sys.exit(_cmd_start())
    if cmd == "stop":
        sys.exit(_cmd_stop())
    if cmd == "status":
        sys.exit(_cmd_status())
    if cmd == "open":
        sys.exit(_cmd_open())
    if cmd == "search":
        sys.exit(_cmd_search(args))
    if cmd == "config":
        sys.exit(_cmd_config(args))

    parser.print_help()
    sys.exit(1)


# ───────────────────────── command handlers ─────────────────────────


def _console():
    from rich.console import Console
    return Console()


def _load_cfg():
    from vimgym.config import load_config
    return load_config()


def _cmd_init() -> int:
    """Initialize the vault, detect sources, persist config."""
    from vimgym.config import init_vault

    console = _console()
    cfg = _load_cfg()
    cfg, newly_detected = init_vault(cfg)

    console.print(f"[green]✓ vault initialized[/green] {cfg.vault_dir}")
    if not cfg.sources:
        console.print("[yellow]⚠ no AI tool directories detected in $HOME[/yellow]")
        console.print("  Edit ~/.vimgym/config.json to add a source manually.")
        return 0

    console.print()
    console.print("[bold]Detected sources:[/bold]")
    for s in cfg.sources:
        if s.enabled:
            mark, status, color = "✓", "enabled", "green"
            note = ""
        elif s.type == "claude_code":
            mark, status, color = "⊘", "disabled", "yellow"
            note = ""
        else:
            mark, status, color = "⊘", "disabled", "yellow"
            note = "  parser coming in v2"
        console.print(
            f"  [{color}]{mark}[/{color}]  {s.name:<20} {s.path:<30} [{color}][{status}][/{color}]{note}"
        )

    console.print()
    console.print("Next: [cyan]vg start[/cyan]")
    return 0


def _cmd_start() -> int:
    from vimgym.config import init_vault
    from vimgym.daemon import is_running, start_daemon

    console = _console()
    cfg = _load_cfg()

    # Auto-init on first run.
    if not cfg.sources or not (cfg.vault_dir / "config.json").exists():
        cfg, newly = init_vault(cfg)
        if newly:
            console.print(f"[dim]auto-initialized vault, detected {len(newly)} source(s)[/dim]")

    if is_running(cfg):
        console.print(f"[yellow]vimgym already running[/yellow] on http://{cfg.server_host}:{cfg.server_port}")
        return 0

    try:
        pid = start_daemon(cfg)
    except RuntimeError as e:
        console.print(f"[red]start failed:[/red] {e}")
        return 1

    console.print(f"[green]vimgym started[/green] (pid {pid}) on http://{cfg.server_host}:{cfg.server_port}")
    console.print(f"  vault:    {cfg.vault_dir}")
    enabled = cfg.enabled_sources
    if enabled:
        for s in enabled:
            console.print(f"  watching: {s.expanded_path}  [dim]({s.id})[/dim]")
    else:
        console.print("  [yellow]watching: no enabled sources[/yellow]")

    if cfg.auto_open_browser:
        try:
            webbrowser.open(f"http://{cfg.server_host}:{cfg.server_port}")
        except Exception:
            pass
    return 0


def _cmd_stop() -> int:
    from vimgym.daemon import stop_daemon

    console = _console()
    cfg = _load_cfg()
    if stop_daemon(cfg):
        console.print("[green]vimgym stopped[/green]")
        return 0
    console.print("[yellow]vimgym was not running[/yellow]")
    return 0


def _cmd_status() -> int:
    from rich.table import Table

    from vimgym.daemon import get_pid, is_running
    from vimgym.db import get_connection
    from vimgym.storage.queries import get_stats

    console = _console()
    cfg = _load_cfg()
    running = is_running(cfg)
    pid = get_pid(cfg)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("status", "[green]running[/green]" if running else "[red]stopped[/red]")
    if running and pid is not None:
        table.add_row("pid", str(pid))
        table.add_row("url", f"http://{cfg.server_host}:{cfg.server_port}")
    table.add_row("vault", str(cfg.vault_dir))
    table.add_row("watching", str(cfg.watch_path))

    if cfg.db_path.exists():
        try:
            conn = get_connection(cfg.db_path)
            stats = get_stats(conn)
            table.add_row("sessions", str(stats.total_sessions))
            table.add_row("db size",  f"{stats.db_size_bytes / 1024 / 1024:.1f} MB")
        except Exception as e:
            table.add_row("db", f"[red]error: {e}[/red]")
    else:
        table.add_row("db", "(not initialized)")

    console.print(table)
    return 0


def _cmd_open() -> int:
    from vimgym.daemon import is_running

    console = _console()
    cfg = _load_cfg()
    if not is_running(cfg):
        console.print("[red]vimgym is not running[/red] — try `vg start`")
        return 1
    url = f"http://{cfg.server_host}:{cfg.server_port}"
    webbrowser.open(url)
    console.print(f"opening {url}")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    if not args.query:
        print("error: search requires a query", file=sys.stderr)
        return 2

    from vimgym.daemon import is_running

    cfg = _load_cfg()

    if is_running(cfg):
        results = _search_via_api(cfg, args)
    else:
        results = _search_via_db(cfg, args)

    if args.as_json:
        print(_json.dumps(results, indent=2, default=str))
        return 0

    _print_search_table(results, args.query)
    return 0


def _search_via_api(cfg, args: argparse.Namespace) -> list[dict]:
    import httpx

    params: dict = {"q": args.query, "limit": args.limit}
    if args.project:
        params["project"] = args.project
    if args.branch:
        params["branch"] = args.branch
    if args.since:
        params["since"] = args.since

    url = f"http://{cfg.server_host}:{cfg.server_port}/api/search"
    try:
        r = httpx.get(url, params=params, timeout=5.0)
        r.raise_for_status()
    except Exception as e:
        print(f"warning: API request failed ({e}); falling back to direct DB", file=sys.stderr)
        return _search_via_db(cfg, args)
    return r.json().get("results", [])


def _search_via_db(cfg, args: argparse.Namespace) -> list[dict]:
    if not cfg.db_path.exists():
        print("error: vault not initialized — run `vg start` once first", file=sys.stderr)
        return []
    from vimgym.db import get_connection
    from vimgym.storage.queries import search_sessions

    conn = get_connection(cfg.db_path)
    results = search_sessions(
        conn,
        args.query,
        project=args.project,
        branch=args.branch,
        since=args.since,
        limit=args.limit,
    )
    return [
        {
            "session_uuid": r.session_uuid,
            "project_name": r.project_name,
            "ai_title": r.ai_title,
            "started_at": r.started_at,
            "duration_secs": r.duration_secs,
            "git_branch": r.git_branch,
            "snippet": r.snippet,
        }
        for r in results
    ]


def _print_search_table(results: list[dict], query: str) -> None:
    from rich.table import Table

    console = _console()
    if not results:
        console.print(f"[yellow]no results for[/yellow] {query!r}")
        return

    table = Table(title=f"Results for {query!r}", show_lines=False)
    table.add_column("DATE", style="dim")
    table.add_column("ID")
    table.add_column("PROJECT")
    table.add_column("BRANCH", style="cyan")
    table.add_column("DUR", justify="right")
    table.add_column("TITLE")

    for r in results:
        date = (r.get("started_at") or "")[:10]
        uid = (r.get("session_uuid") or "")[:8]
        proj = r.get("project_name") or ""
        branch = r.get("git_branch") or ""
        dur = r.get("duration_secs")
        dur_str = f"{int(dur)//60}m" if dur else "-"
        title = (r.get("ai_title") or "")[:60]
        table.add_row(date, uid, proj, branch, dur_str, title)

    console.print(table)


def _cmd_config(args: argparse.Namespace) -> int:
    from vimgym.config import save_config

    console = _console()
    cfg = _load_cfg()

    sub = getattr(args, "config_cmd", None)
    if sub == "sources":
        return _cmd_config_sources(args, cfg, console, save_config)

    # Default `vg config` — print active config summary.
    from rich.table import Table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("vault",  str(cfg.vault_dir))
    table.add_row("server", f"{cfg.server_host}:{cfg.server_port}")
    table.add_row("logs",   str(cfg.log_path))
    table.add_row("rules",  str(cfg.rules_path))
    table.add_row("sources",f"{len(cfg.sources)} configured, {len(cfg.enabled_sources)} active")
    console.print(table)
    console.print()
    console.print("[dim]Run [cyan]vg config sources[/cyan] for source details.[/dim]")
    return 0


def _cmd_config_sources(args: argparse.Namespace, cfg, console, save_config) -> int:
    from rich.table import Table

    target_id = getattr(args, "source_id", None)
    enable = getattr(args, "enable", False)
    disable = getattr(args, "disable", False)

    if target_id and (enable or disable):
        for s in cfg.sources:
            if s.id == target_id:
                s.enabled = bool(enable)
                save_config(cfg)
                state = "[green]enabled[/green]" if enable else "[yellow]disabled[/yellow]"
                console.print(f"source [cyan]{target_id}[/cyan] is now {state}")
                console.print("[dim](takes effect on next [cyan]vg start[/cyan])[/dim]")
                return 0
        console.print(f"[red]no source with id '{target_id}'[/red]")
        return 1

    if target_id and not (enable or disable):
        console.print("[yellow]use --enable or --disable[/yellow]")
        return 2

    if not cfg.sources:
        console.print("[yellow]no sources configured.[/yellow] Run [cyan]vg init[/cyan] to detect them.")
        return 0

    table = Table(title="vimgym sources", title_style="bold cyan")
    table.add_column("id", style="cyan")
    table.add_column("path")
    table.add_column("status", justify="center")
    table.add_column("parser")

    for s in cfg.sources:
        if s.enabled and s.exists():
            status = "[green]● on[/green]"
        elif s.enabled:
            status = "[yellow]● on (path missing)[/yellow]"
        else:
            status = "[dim]○ off[/dim]"
        parser_state = "[green]available[/green]" if s.type == "claude_code" else "[yellow]coming v2[/yellow]"
        table.add_row(s.id, s.path, status, parser_state)

    console.print(table)
    console.print()
    console.print("[dim]To enable: [cyan]vg config sources <id> --enable[/cyan][/dim]")
    console.print("[dim]Note: Only claude_code parser is available in v1.[/dim]")
    return 0


if __name__ == "__main__":
    main()

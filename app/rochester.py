"""
Rorchestra — Interactive CLI for the Roblox/Luau AI Orchestrator.

A Claude-Code-style REPL with rich formatting, slash commands,
and agent-driven context investigation.

Usage:
    python -m app.rochester
    rorchestra  (if installed as entry point)
"""

from __future__ import annotations

import os
import sys
import time
import shutil
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.completion import WordCompleter

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.columns import Columns
from rich.rule import Rule
from rich import box

console = Console()

# ── Session-level active repo ────────────────────────────────────────────

_active_repo = None   # Set during main() startup from CWD detection
_active_repo_id = None


# ── Saved plans (disk-persisted) ─────────────────────────────────────────

import json as _json
from datetime import datetime as _dt

_PLANS_DIR = None  # Set on startup to <repo_root>/.rorchestra/plans/


def _get_plans_dir() -> Path:
    """Get the plans directory, creating it if needed."""
    global _PLANS_DIR
    if _PLANS_DIR is None:
        repo = get_active_repo()
        if repo:
            _PLANS_DIR = Path(repo.root_path) / ".rorchestra" / "plans"
        else:
            _PLANS_DIR = Path.cwd() / ".rorchestra" / "plans"
    _PLANS_DIR.mkdir(parents=True, exist_ok=True)
    return _PLANS_DIR


def _save_plan(description: str, packet, investigation, repo_path: str, task_id: int) -> int:
    """Save a plan to disk and return its ID."""
    plans_dir = _get_plans_dir()

    # Find next ID
    existing = sorted(plans_dir.glob("plan_*.json"))
    next_id = 1
    if existing:
        try:
            next_id = int(existing[-1].stem.split("_")[1]) + 1
        except (ValueError, IndexError):
            next_id = len(existing) + 1

    plan_data = {
        "id": next_id,
        "description": description,
        "packet": packet.model_dump() if hasattr(packet, "model_dump") else {},
        "invariants": list(investigation.invariants) if hasattr(investigation, "invariants") else [],
        "risks": list(investigation.risks) if hasattr(investigation, "risks") else [],
        "uncertainties": list(investigation.uncertainties) if hasattr(investigation, "uncertainties") else [],
        "relevant_script_ids": list(investigation.relevant_script_ids) if hasattr(investigation, "relevant_script_ids") else [],
        "repo_path": repo_path,
        "task_id": task_id,
        "timestamp": _dt.now().isoformat(),
    }

    path = plans_dir / f"plan_{next_id:03d}.json"
    path.write_text(_json.dumps(plan_data, indent=2, default=str), encoding="utf-8")
    return next_id


def _load_plans() -> list[dict]:
    """Load all plans from disk."""
    plans_dir = _get_plans_dir()
    plans = []
    for p in sorted(plans_dir.glob("plan_*.json")):
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            plans.append(data)
        except Exception:
            pass
    return plans


def _load_plan(plan_id: int) -> dict | None:
    """Load a specific plan by ID."""
    plans_dir = _get_plans_dir()
    path = plans_dir / f"plan_{plan_id:03d}.json"
    if not path.exists():
        return None
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_active_repo():
    """Get the active repo for this session, falling back to repos[0]."""
    global _active_repo, _active_repo_id
    if _active_repo:
        return _active_repo

    from app.storage.database import get_session
    from app.models.entities import Repository
    from sqlalchemy import select

    session = get_session()
    repos = session.execute(select(Repository)).scalars().all()
    session.close()
    if repos:
        _active_repo = repos[0]
        _active_repo_id = repos[0].id
        return repos[0]
    return None


def _detect_rojo_project(path: str) -> bool:
    """Check if a directory looks like a Rojo project."""
    from pathlib import Path
    p = Path(path)
    # Has default.project.json → definitely Rojo
    if (p / "default.project.json").exists():
        return True
    # Has src/ with .luau files → likely Rojo
    if (p / "src").is_dir():
        luau_files = list((p / "src").rglob("*.luau"))
        if luau_files:
            return True
    # Has any .luau files at top level
    if list(p.glob("*.luau")):
        return True
    return False


def _auto_init_from_cwd() -> None:
    """Auto-detect and ingest the CWD project if it's a Rojo project."""
    global _active_repo, _active_repo_id

    import os
    from pathlib import Path
    from app.storage.database import get_session
    from app.models.entities import Repository, Script, MemoryRecord, Domain
    from sqlalchemy import select, func

    cwd = os.getcwd()
    cwd_path = Path(cwd).resolve()

    session = get_session()
    try:
        # Check if this path (or parent) is already ingested
        repos = session.execute(select(Repository)).scalars().all()
        for repo in repos:
            repo_path = Path(repo.root_path).resolve()
            if repo_path == cwd_path or cwd_path.is_relative_to(repo_path):
                _active_repo = repo
                _active_repo_id = repo.id
                _info(f"Project: [bold]{repo.name}[/bold]")
                _dim(str(repo.root_path))

                # Check if summaries are missing
                _auto_summarize_if_needed(session, repo)
                return

        # Not ingested yet — check if CWD is a Rojo project
        if _detect_rojo_project(cwd):
            _info(f"Detected Rojo project at [bold]{cwd_path.name}[/bold]")
            _dim("Auto-ingesting...")

            from app.services.ingest.pipeline import ingest_repository
            from app.services.memory.skill_loader import load_all_skills

            try:
                snapshot = ingest_repository(str(cwd_path))
                _success(
                    f"Ingested! {snapshot.script_count} scripts, "
                    f"{len(snapshot.domains)} domains, "
                    f"{snapshot.edge_count} edges"
                )

                # Set as active
                repo = session.get(Repository, snapshot.repo_id)
                _active_repo = repo
                _active_repo_id = repo.id

                # Auto-load skills
                loaded = load_all_skills()
                if loaded:
                    _dim(f"Skills loaded: {len(loaded)}")

                # Auto-summarize
                _auto_summarize_if_needed(session, repo)

            except Exception as e:
                _warn(f"Auto-ingest failed: {e}")
                _dim("Use /ingest <path> manually")
        elif not repos:
            _dim("No project detected. Use /ingest <path> to start.")
        elif repos:
            # Default to first repo
            _active_repo = repos[0]
            _active_repo_id = repos[0].id
            _info(f"Project: [bold]{repos[0].name}[/bold]")
    finally:
        session.close()


_AUTO_SUMMARIZE_WORKERS = 40


def _auto_summarize_if_needed(session, repo) -> None:
    """Run summarization if scripts are missing memory records."""
    from app.models.entities import Script, MemoryRecord, Domain
    from sqlalchemy import select, func

    script_count = session.execute(
        select(func.count(Script.id)).where(Script.repo_id == repo.id)
    ).scalar() or 0

    # Count distinct scripts that have at least one summary record
    # (not total records, which inflates from deep-read, env-validation, etc.)
    summarized_script_ids = session.execute(
        select(func.count(func.distinct(MemoryRecord.scope_id))).where(
            MemoryRecord.scope_id.like("script:%"),
            MemoryRecord.invalidated_by.is_(None),
        )
    ).scalar() or 0

    unsummarized = script_count - summarized_script_ids
    if unsummarized <= 0:
        _dim(f"Memory: {summarized_script_ids} summaries up to date")
        return

    _info(f"Summarizing {unsummarized} unsummarized scripts ({_AUTO_SUMMARIZE_WORKERS} workers)...")

    from app.services.summarization.summarizer import (
        summarise_scripts_parallel,
        summarise_domain,
    )

    scripts = session.execute(
        select(Script).where(Script.repo_id == repo.id)
    ).scalars().all()

    script_ids = [s.id for s in scripts]

    import threading
    completed = {"count": 0, "ok": 0, "fail": 0}
    total = len(script_ids)
    lock = threading.Lock()

    def _on_complete(sid, summary, error):
        with lock:
            completed["count"] += 1
            if error:
                completed["fail"] += 1
            else:
                completed["ok"] += 1
            n = completed["count"]
        if n % 20 == 0 or n == total:
            pct = int(n / total * 100)
            console.print(f"  [{DIM}]Progress: {n}/{total} ({pct}%)[/]")

    summarise_scripts_parallel(
        script_ids,
        repo.root_path,
        max_workers=_AUTO_SUMMARIZE_WORKERS,
        on_complete=_on_complete,
    )

    _success(f"Scripts: {completed['ok']} ok, {completed['fail']} failed")

    # Domains (few, sequential)
    domains = session.execute(
        select(Domain).where(Domain.repo_id == repo.id)
    ).scalars().all()

    for d in domains:
        summarise_domain(d.id, repo.root_path)

    _success(f"Domains: {len(domains)} summarized")

# ── Theme ────────────────────────────────────────────────────────────────

P1 = "#c084fc"   # Light purple
P2 = "#a855f7"   # Purple
P3 = "#7c3aed"   # Deep purple
A1 = "#38bdf8"   # Sky blue
A2 = "#22d3ee"   # Cyan
DIM = "#71717a"   # Zinc-500
BG_DIM = "#27272a"
SUCCESS = "#4ade80"
WARN = "#fbbf24"
ERROR = "#f87171"
MUTED = "#a1a1aa"

VERSION = "0.2.0"

# ── Gradient Logo ────────────────────────────────────────────────────────


def _print_logo():
    """Print the Rorchestra logo with a purple→cyan gradient."""
    lines = [
        "  ██████╗  ██████╗ ██████╗  ██████╗██╗  ██╗███████╗███████╗████████╗██████╗  █████╗ ",
        "  ██╔══██╗██╔═══██╗██╔══██╗██╔════╝██║  ██║██╔════╝██╔════╝╚══██╔══╝██╔══██╗██╔══██╗",
        "  ██████╔╝██║   ██║██████╔╝██║     ███████║█████╗  ███████╗   ██║   ██████╔╝███████║",
        "  ██╔══██╗██║   ██║██╔══██╗██║     ██╔══██║██╔══╝  ╚════██║   ██║   ██╔══██╗██╔══██║",
        "  ██║  ██║╚██████╔╝██║  ██║╚██████╗██║  ██║███████╗███████║   ██║   ██║  ██║██║  ██║",
        "  ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝",
    ]
    # Gradient from purple (#a855f7) through violet (#8b5cf6) to cyan (#22d3ee)
    gradient = ["#c084fc", "#a78bfa", "#818cf8", "#6366f1", "#38bdf8", "#22d3ee"]
    for i, line in enumerate(lines):
        color = gradient[i % len(gradient)]
        console.print(f"[bold {color}]{line}[/]")


def _print_welcome():
    """Print the welcome screen with logo and system info."""
    console.clear()
    console.print()
    _print_logo()
    console.print()

    # Subtitle
    console.print(
        f"  [bold {P1}]Roblox/Luau AI Orchestrator[/]  "
        f"[{DIM}]v{VERSION}[/]  "
        f"[{DIM}]·[/]  "
        f"[{A1}]agent-driven context engineering[/]",
    )
    console.print()

    # Quick-start hints in a subtle panel
    hints = Text()
    hints.append("  💬 ", style="bold")
    hints.append("Type naturally to ask questions  ", style=MUTED)
    hints.append("·  ", style=DIM)
    hints.append("/help", style=f"bold {A1}")
    hints.append(" for all commands  ", style=MUTED)
    hints.append("·  ", style=DIM)
    hints.append("/quit", style=f"bold {A1}")
    hints.append(" to exit", style=MUTED)

    console.print(Panel(
        hints,
        border_style=P3,
        box=box.HEAVY,
        padding=(0, 1),
    ))
    console.print()


# ── Slash commands ───────────────────────────────────────────────────────

SLASH_COMMANDS = {
    "/help":      ("📋", "Show available commands"),
    "/status":    ("📊", "Show repo status — scripts, domains, memories"),
    "/ingest":    ("📥", "Ingest a Rojo project  ·  /ingest <path>"),
    "/summarize": ("🧠", "Summarize all scripts  ·  /summarize [--workers N]"),
    "/edit":      ("✏️ ", "Agent-driven code edit  ·  /edit <desc> [--scope X] [--side server] [--investigation-workers N]"),
    "/ask":       ("💬", "Ask about the codebase  ·  /ask <question> [--scope X]"),
    "/tokens":    ("⟡ ", "Show token usage for this session"),
    "/skills":    ("⚡", "Load / reload skill memories"),
    "/normalize": ("🔧", "Fix literal \\t and \\\" in repo .luau files"),
    "/clear":     ("🧹", "Clear the screen"),
    "/quit":      ("👋", "Exit Rorchestra"),
}

command_completer = WordCompleter(
    list(SLASH_COMMANDS.keys()) + ["/q", "/exit"],
    sentence=True,
)


def show_help():
    console.print()
    console.print(Rule(f"[bold {P1}]Rorchestra Commands[/]", style=P3))
    console.print()

    for cmd, (icon, desc) in SLASH_COMMANDS.items():
        console.print(
            f"  {icon}  [bold {A1}]{cmd:13s}[/]  [{MUTED}]{desc}[/]"
        )

    console.print()
    console.print(f"  [{DIM}]Tip: Just type a question without / to ask about the codebase[/]")
    console.print()


# ── Utility formatters ──────────────────────────────────────────────────


def _info(msg: str):
    console.print(f"  [{A1}]ℹ[/]  {msg}")


def _success(msg: str):
    console.print(f"  [{SUCCESS}]✓[/]  {msg}")


def _warn(msg: str):
    console.print(f"  [{WARN}]⚠[/]  [{WARN}]{msg}[/]")


def _error(msg: str):
    console.print(f"  [{ERROR}]✗[/]  [{ERROR}]{msg}[/]")


def _header(msg: str):
    console.print()
    console.print(f"  [bold {P1}]▸ {msg}[/]")


def _dim(msg: str):
    console.print(f"  [{DIM}]{msg}[/]")


def _stat_row(label: str, value: str | int, color: str = A1) -> Text:
    t = Text()
    t.append(f"  {label}: ", style=MUTED)
    t.append(str(value), style=f"bold {color}")
    return t


def _print_token_line(result=None):
    """Print a subtle token usage line after a Gemini invocation."""
    from app.services.token_tracker import record, last_line
    if result is not None:
        record(result)
    line = last_line()
    if line:
        console.print(f"  [{DIM}]⟡ {line}[/]")


# ── Command handlers ────────────────────────────────────────────────────


def handle_status(args: str):
    """Show repo status."""
    from app.services.agents.tools import list_domains
    from app.storage.database import get_session
    from app.models.entities import Repository, MemoryRecord, GraphEdge, Script
    from sqlalchemy import select, func

    session = get_session()
    repos = session.execute(select(Repository)).scalars().all()

    if not repos:
        _warn("No repositories ingested. Use /ingest <path>")
        session.close()
        return

    for repo in repos:
        script_count = session.execute(
            select(func.count(Script.id)).where(Script.repo_id == repo.id)
        ).scalar()
        edge_count = session.execute(
            select(func.count(GraphEdge.id))
        ).scalar()
        mem_count = session.execute(
            select(func.count(MemoryRecord.id)).where(
                MemoryRecord.invalidated_by.is_(None)
            )
        ).scalar()
        stale_count = session.execute(
            select(func.count(MemoryRecord.id)).where(
                MemoryRecord.invalidated_by.isnot(None)
            )
        ).scalar()

        domains = list_domains(repo.id)

        console.print()
        console.print(Rule(
            f"[bold {P1}]📦 {repo.name}[/]",
            style=P3,
        ))
        console.print()

        # Stats grid
        col1 = Text()
        col1.append("  Scripts    ", style=MUTED)
        col1.append(str(script_count), style=f"bold {A1}")
        col1.append("\n  Domains    ", style=MUTED)
        col1.append(str(len(domains)), style=f"bold {A1}")
        col1.append("\n  Edges      ", style=MUTED)
        col1.append(str(edge_count), style=f"bold {A1}")

        col2 = Text()
        col2.append("  Memories   ", style=MUTED)
        col2.append(str(mem_count), style=f"bold {SUCCESS}")
        col2.append("\n  Stale      ", style=MUTED)
        col2.append(str(stale_count), style=f"bold {WARN}" if stale_count else f"bold {SUCCESS}")
        col2.append("\n  Root       ", style=MUTED)
        col2.append(repo.root_path, style=DIM)

        console.print(Columns([col1, col2], padding=(0, 4)))
        console.print()

        # Domain breakdown
        if domains:
            for d in domains:
                bar_len = min(d['script_count'] // 3, 30)
                bar = "█" * bar_len
                console.print(
                    f"  [{A1}]{d['name']:10s}[/]  "
                    f"[{P1}]{bar}[/]  "
                    f"[{DIM}]{d['script_count']} scripts · {d['kind']}[/]"
                )
            console.print()

    session.close()


def handle_ingest(args: str):
    """Ingest a Rojo project."""
    global _active_repo, _active_repo_id
    path = args.strip()
    if not path:
        # Default to CWD if no path given
        path = os.getcwd()

    from app.services.ingest.pipeline import ingest_repository
    from app.services.memory.skill_loader import load_all_skills

    _header("Ingesting project")
    _dim(path)

    with console.status(f"  [bold {P1}]Scanning...[/]", spinner="dots"):
        snapshot = ingest_repository(path)

    _success(f"Ingested! Repo #{snapshot.repo_id}")
    _dim(f"Scripts: {snapshot.script_count}  ·  Domains: {len(snapshot.domains)}  ·  Edges: {snapshot.edge_count}")

    # Set as active repo for this session
    from app.storage.database import get_session as _gs
    from app.models.entities import Repository as _R
    _s = _gs()
    _active_repo = _s.get(_R, snapshot.repo_id)
    _active_repo_id = snapshot.repo_id
    _s.close()
    _success(f"Active project: {_active_repo.name}")

    with console.status(f"  [bold {P1}]Loading skills...[/]", spinner="dots"):
        load_all_skills()
    _success("Skills loaded")


def handle_summarize(args: str):
    """Summarize scripts in parallel."""
    import re
    from sqlalchemy import select
    from app.models.entities import Script, Repository, Domain
    from app.services.summarization.summarizer import summarise_scripts_parallel, summarise_domain
    from app.storage.database import get_session
    import threading

    workers = 4
    m = re.search(r'--workers\s+(\d+)', args)
    if m:
        workers = int(m.group(1))

    repo_id = 1
    m = re.search(r'--repo-id\s+(\d+)', args)
    if m:
        repo_id = int(m.group(1))

    session = get_session()
    repo = session.get(Repository, repo_id)
    if not repo:
        _error(f"Repository {repo_id} not found")
        session.close()
        return

    scripts = session.execute(
        select(Script).where(Script.repo_id == repo_id)
    ).scalars().all()

    script_ids = [s.id for s in scripts]
    id_to_path = {s.id: (s.instance_path or s.file_path) for s in scripts}
    total = len(script_ids)

    completed = {"count": 0, "ok": 0, "fail": 0}
    lock = threading.Lock()

    def _on_complete(sid, summary, error):
        with lock:
            completed["count"] += 1
            if error:
                completed["fail"] += 1
            else:
                completed["ok"] += 1
            n = completed["count"]
        pct = int(n / total * 100)
        bar_fill = int(pct / 5)
        bar = f"[{P1}]{'━' * bar_fill}[/][{DIM}]{'─' * (20 - bar_fill)}[/]"
        status = f"[{SUCCESS}]✓[/]" if not error else f"[{ERROR}]✗[/]"
        name = id_to_path.get(sid, str(sid)).split(".")[-1]
        console.print(f"  {bar}  [{A1}]{n:3d}[/][{DIM}]/{total}[/]  {status}  [{DIM}]{name}[/]")

    _header(f"Summarizing {total} scripts")
    _dim(f"{workers} workers · repo {repo.name}")
    console.print()

    summarise_scripts_parallel(script_ids, repo.root_path, max_workers=workers, on_complete=_on_complete)

    console.print()
    _success(f"Scripts: {completed['ok']} ok, {completed['fail']} failed")

    domains = session.execute(
        select(Domain).where(Domain.repo_id == repo_id)
    ).scalars().all()

    _header("Summarizing domains")
    for d in domains:
        with console.status(f"  [{P1}]{d.name}...[/]", spinner="dots"):
            summarise_domain(d.id, repo.root_path)
        _success(d.name)

    session.close()
    console.print()
    _success("All summarization complete")


def handle_edit(args: str):
    """Run an agent-driven edit."""
    import re

    if not args.strip():
        _warn("Usage: /edit <description> [--scope X] [--side server|client|shared] [--plan] [--debug] [--verbose|-v]")
        return

    scope = ""
    side = "unknown"
    investigation_workers = None
    verbose = False
    plan_only = False
    debug_mode = False
    m = re.search(r'--scope\s+(\S+)', args)
    if m:
        scope = m.group(1)
        args = args[:m.start()] + args[m.end():]
    m = re.search(r'--side\s+(\S+)', args)
    if m:
        side = m.group(1)
        args = args[:m.start()] + args[m.end():]
    m = re.search(r'--investigation-workers\s+(\d+)', args)
    if m:
        investigation_workers = int(m.group(1))
        args = args[:m.start()] + args[m.end():]
    if re.search(r'(^|\s)--plan(\s|$)', args):
        plan_only = True
        args = re.sub(r'\s*--plan\b', '', args)
    if re.search(r'(^|\s)--debug(\s|$)', args):
        debug_mode = True
        args = re.sub(r'\s*--debug\b', '', args)
    if re.search(r'(^|\s)(--verbose|-v)(\s|$)', args):
        verbose = True
        args = re.sub(r'\s*--verbose\b', '', args)
        args = re.sub(r'\s*-v\b', '', args)
        args = re.sub(r'\b-v\s*', '', args)

    description = args.strip().strip('"').strip("'")
    
    # Heuristic for side if unknown
    if side == "unknown":
        dl = description.lower()
        if "replicate" in dl or "sync" in dl or "shared" in dl or "remote" in dl:
            side = "shared"
        elif "client" in dl and "server" not in dl:
            side = "client"
        elif "server" in dl and "client" not in dl:
            side = "server"

    from app.models.entities import Task, TaskStatus, Repository
    from app.services.agents.orchestrator import run_investigation
    from app.services.workers.lifecycle import invoke_edit_worker, save_proposal
    from app.policies.safety import is_high_risk, require_review
    from app.storage.database import get_session
    from sqlalchemy import select

    repo = get_active_repo()
    if not repo:
        _error("No repositories. Use /ingest first.")
        return
    session = get_session()

    if is_high_risk(description):
        _warn("High-risk task detected — proceeding carefully")

    task = Task(
        repo_id=repo.id,
        description=description,
        status=TaskStatus.pending,
        target_scope=scope or None,
        runtime_side=side,
    )
    session.add(task)
    session.commit()
    task_id = task.id
    session.close()

    # Investigation
    mode_tags = []
    if plan_only:
        mode_tags.append("[bold cyan]PLAN[/]")
    if debug_mode:
        mode_tags.append("[bold yellow]DEBUG[/]")
    if verbose:
        mode_tags.append("[bold]verbose[/]")
    mode_str = "  ·  ".join(mode_tags)

    _header("Investigating")
    _dim(f"scope={scope or '(auto)'}  ·  side={side}" + (f"  ·  {mode_str}" if mode_str else ""))
    console.print()

    if verbose:
        console.print(f"  [dim]Running investigation with phase I/O visible (no spinner).[/]\n")
        packet, investigation = run_investigation(task_id, investigation_workers=investigation_workers, verbose=True)
    else:
        with console.status(f"  [{P1}]🔍 Repo investigator exploring...[/]", spinner="dots"):
            packet, investigation = run_investigation(task_id, investigation_workers=investigation_workers)

    # Results panel
    results = Text()
    results.append("  Scripts    ", style=MUTED)
    results.append(str(len(investigation.relevant_script_ids)), style=f"bold {A1}")
    results.append("\n  Files      ", style=MUTED)
    results.append(str(len(packet.file_bodies)), style=f"bold {A1}")
    results.append("\n  Invariants ", style=MUTED)
    results.append(str(len(investigation.invariants)), style=f"bold {A1}")

    console.print(Panel(
        results,
        title=f"[bold {P1}]Investigation Complete[/]",
        border_style=P3,
        box=box.ROUNDED,
        padding=(0, 1),
    ))

    for r in investigation.risks[:3]:
        _warn(f"Risk: {r}")
    for u in investigation.uncertainties[:3]:
        _info(f"Uncertain: {u}")

    # ── Plan mode: show details and stop ──
    if plan_only:
        console.print()
        _header("Plan Details")

        # Show target files
        _info("Target files:")
        for fp in sorted(packet.file_bodies.keys()):
            _dim(f"  {fp}")

        # Show invariants
        if investigation.invariants:
            console.print()
            _info("Invariants:")
            for inv in investigation.invariants:
                console.print(f"  [{A1}]•[/] {inv}")

        # Show risks
        if investigation.risks:
            console.print()
            _info("Risks:")
            for r in investigation.risks:
                console.print(f"  [{WARN}]⚠[/] {r}")

        # Show relevant scripts
        if packet.relevant_scripts:
            console.print()
            _info("Scripts:")
            for s in packet.relevant_scripts:
                path = s.get('instance_path', s.get('file_path', '?'))
                console.print(f"  [{DIM}]{path}[/]")

        # Save plan to disk for later retrieval
        plan_id = _save_plan(description, packet, investigation, repo.root_path, task_id)

        console.print()
        _success(f"Plan #{plan_id} saved.")
        console.print()

        # Interactive prompt: execute, edit description, or cancel
        try:
            choice = console.input(f"  [{A1}]Execute this plan?[/] ([bold]y[/]es / [bold]n[/]o / [bold]e[/]dit description): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "n"

        if choice in ("n", "no", ""):
            _dim("Cancelled.")
            return
        elif choice in ("e", "edit"):
            try:
                new_desc = console.input(f"  [{A1}]New description:[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                _dim("Cancelled.")
                return
            if new_desc:
                # Update the packet objective with the edited description
                packet.objective = new_desc
                _info(f"Updated objective: {new_desc}")
            else:
                _dim("No change, proceeding with original.")
        elif choice not in ("y", "yes"):
            _dim("Cancelled.")
            return

        # Fall through to the edit worker below
        console.print()

    # Worker: batch by max_files_per_edit when many files so each run gets a bounded set
    from app.config import settings
    from app.models.schemas import ContextPacketSchema
    from app.services.patch_apply import apply_patch_to_dir

    max_per_edit = getattr(settings, "max_files_per_edit", 25)
    file_paths = list(packet.file_bodies.keys())
    n_files = len(file_paths)

    if n_files <= max_per_edit:
        # Debug: show the full internal plan before sending to worker
        if debug_mode:
            console.print()
            _header("Debug: Internal Plan")

            console.print(f"  [{DIM}]Objective:[/]  {packet.objective}")
            console.print(f"  [{DIM}]Scope:[/]      {packet.target_scope or '(auto)'}")
            console.print(f"  [{DIM}]Side:[/]       {packet.runtime_side}")
            console.print(f"  [{DIM}]Files:[/]      {n_files}")
            console.print(f"  [{DIM}]CWD:[/]        {repo.root_path}")
            console.print()

            _info("Invariants sent to worker:")
            for inv in (packet.local_invariants or []):
                console.print(f"  [{A1}]•[/] {inv}")

            if packet.known_risks:
                console.print()
                _info("Risks sent to worker:")
                for r in packet.known_risks:
                    console.print(f"  [{WARN}]⚠[/] {r}")

            if packet.uncertainties:
                console.print()
                _info("Uncertainties:")
                for u in packet.uncertainties:
                    console.print(f"  [{DIM}]?[/] {u}")

            console.print()
            _info("File bodies:")
            for fp, body in packet.file_bodies.items():
                lines = body.count('\n') + 1 if body else 0
                console.print(f"  [{DIM}]{fp}[/] ({lines} lines)")

            # Show the prompt that will be sent
            from app.services.workers.lifecycle import _build_tool_prompt
            worker_prompt = _build_tool_prompt(packet)
            console.print()
            _info("Worker prompt preview (first 2000 chars):")
            console.print(Panel(
                worker_prompt[:2000] + ("\n..." if len(worker_prompt) > 2000 else ""),
                border_style=DIM,
                box=box.SIMPLE,
                padding=(0, 1),
            ))
            console.print()

        # Single run
        console.print()
        with console.status(f"  [{P1}]⚙️  Edit worker running...[/]", spinner="dots"):
            result = invoke_edit_worker(packet, cwd=repo.root_path, debug=debug_mode)
        _print_token_line(result)
        result_patch = result.patch_content
        single_run = True
    else:
        # Batched: run edit worker per batch, apply each patch, merge into one proposal
        n_batches = (n_files + max_per_edit - 1) // max_per_edit
        all_patches = []
        all_applied = []
        batch_failures = 0
        for b in range(n_batches):
            start = b * max_per_edit
            batch_fps = file_paths[start : start + max_per_edit]
            batch_bodies = {fp: packet.file_bodies[fp] for fp in batch_fps}
            batch_scripts = [s for s in packet.relevant_scripts if s.get("file_path") in batch_fps]
            batch_packet = ContextPacketSchema(
                task_id=packet.task_id,
                objective=packet.objective,
                target_scope=packet.target_scope,
                runtime_side=packet.runtime_side,
                relevant_scripts=batch_scripts,
                relevant_contracts=packet.relevant_contracts,
                local_invariants=packet.local_invariants,
                known_risks=packet.known_risks,
                uncertainties=packet.uncertainties,
                file_bodies=batch_bodies,
                token_budget=packet.token_budget,
                migration_brief=packet.migration_brief,
            )
            console.print(f"  [{P1}]⚙️  Edit worker batch {b + 1}/{n_batches} ({len(batch_fps)} files)...[/]")
            result = invoke_edit_worker(batch_packet, cwd=repo.root_path)
            if result.patch_content:
                all_patches.append(result.patch_content)
                applied, errs = apply_patch_to_dir(result.patch_content, repo.root_path)
                all_applied.extend(applied)
                for e in errs:
                    if "corrupt" not in e.lower() and "No valid patches" not in e:
                        _dim(e)
            else:
                batch_failures += 1
        _print_token_line(result)
        result_patch = "\n".join(all_patches) if all_patches else None
        single_run = False
        if all_applied:
            _success(f"Batched edit: updated {len(all_applied)} file(s) across {n_batches} batch(es).")
        if batch_failures:
            _warn(f"{batch_failures} batch(es) produced no patch.")

    if result_patch:
        proposal_id = save_proposal(task_id, result_patch)

        diff_preview = result_patch[:2000]
        if len(result_patch) > 2000:
            diff_preview += "\n... (truncated)"

        # Tool-based workers already applied changes to disk
        tool_mode = single_run  # single runs use tools by default now
        mode_label = " (applied)" if tool_mode else " (batched)" if not single_run else ""

        console.print(Panel(
            Markdown(f"```diff\n{diff_preview}\n```"),
            title=f"[bold {SUCCESS}]✓ Patch #{proposal_id}[/]{mode_label}",
            border_style=SUCCESS,
            box=box.ROUNDED,
            padding=(0, 1),
        ))

        import re
        changed_files = []
        for line in result_patch.splitlines():
            if line.startswith("+++ b/"):
                changed_files.append(line[6:].strip())
            elif line.startswith("+++ "):
                changed_files.append(line[4:].strip())
        changed_files = list(dict.fromkeys(changed_files))

        if changed_files:
            from app.services.memory.refresh import analyze_invalidation_impact
            invalidations = analyze_invalidation_impact(changed_files)
            if any(invalidations.values()):
                _dim(f"Impact: {invalidations['script']} script memories, {invalidations['domain']} domain memories will be invalidated.")

        # Display luau-lsp diagnostics if any
        if result.luau_diagnostics:
            errors = [d for d in result.luau_diagnostics if d.get("severity", "").lower() in ("error",)]
            warnings = [d for d in result.luau_diagnostics if d.get("severity", "").lower() in ("warning",)]
            other = [d for d in result.luau_diagnostics if d not in errors and d not in warnings]

            if errors:
                _error(f"luau-lsp: {len(errors)} error(s) found")
            if warnings:
                _warn(f"luau-lsp: {len(warnings)} warning(s) found")
            if not errors and not warnings and other:
                _dim(f"luau-lsp: {len(other)} diagnostic(s)")

            # Show up to 10 diagnostics
            shown = (errors + warnings + other)[:10]
            for d in shown:
                sev = d.get("severity", "?")
                msg = d.get("message", "")
                file = d.get("file", d.get("range", {}).get("file", "?"))
                line = d.get("line", d.get("range", {}).get("start", {}).get("line", "?"))
                color = "red" if sev.lower() == "error" else "yellow" if sev.lower() == "warning" else "dim"
                console.print(f"  [{color}]{sev}[/] {file}:{line} — {msg}")

            if len(result.luau_diagnostics) > 10:
                _dim(f"  ... and {len(result.luau_diagnostics) - 10} more")
        elif single_run and result.exit_code == 0:
            _success("luau-lsp: no issues found ✓")

        if tool_mode:
            # Tool-based: files already modified on disk
            _success(f"Changes applied to disk. Run `/apply {proposal_id}` to commit memory invalidation.")
        elif require_review(description, result_patch):
            _warn(f"This patch requires manual review before applying. Run `/apply {proposal_id}` to confirm memory rebuild.")
        else:
            _success(f"Patch may be applied directly. Run `/apply {proposal_id}` to confirm memory rebuild.")
    else:
        if single_run:
            _error(f"Worker failed (exit={result.exit_code})")
            if result.stderr:
                _dim(result.stderr[-600:])
            if result.stdout and result.exit_code != 0:
                _dim(result.stdout[-600:])
        else:
            _error("No patches produced from batched edit.")


def handle_ask(args: str):
    """Ask a question about the codebase."""
    import re

    if not args.strip():
        _warn("Usage: /ask <question> [--scope X]")
        return

    scope = ""
    m = re.search(r'--scope\s+(\S+)', args)
    if m:
        scope = m.group(1)
        args = args[:m.start()] + args[m.end():]

    question = args.strip()

    from app.models.entities import Repository
    from app.services.agents.tools import list_scripts, list_domains, get_contracts
    from app.adapters.gemini_cli import invoke_standalone

    repo = get_active_repo()
    if not repo:
        _error("No repositories. Use /ingest first.")
        return

    domains = list_domains(repo.id)
    domain_ctx = "\n".join(
        f"## Domain: {d['name']} ({d['kind']}, {d['script_count']} scripts)\n{d['summary']}"
        for d in domains
    )

    script_ctx = ""
    if scope:
        scopes = [s.strip() for s in scope.split(",") if s.strip()]
        all_scripts = []
        for sc in scopes:
            all_scripts.extend(list_scripts(repo.id, pattern=sc))
        seen = set()
        unique = [s for s in all_scripts if not (s["id"] in seen or seen.add(s["id"]))]
        script_ctx = "\n\n## Relevant Scripts\n" + "\n".join(
            f"- **{s['instance_path']}** ({s['script_type']}): {s['summary']}"
            for s in unique
        )

    contracts = get_contracts(repo.id)
    contract_ctx = ""
    if contracts:
        contract_ctx = "\n\n## Contracts\n" + "\n".join(
            f"- {c['name']} ({c['kind']}): {c['summary']}" for c in contracts
        )

    prompt = f"""\
You are a Roblox/Luau codebase expert. Answer concisely.

# Repository: {repo.name}

{domain_ctx}
{script_ctx}
{contract_ctx}

# Question
{question}
"""

    with console.status(f"  [{P1}]💭 Thinking...[/]", spinner="dots"):
        result = invoke_standalone(prompt, timeout=120, cwd=repo.root_path)

    _print_token_line(result)

    if result.exit_code == 0:
        console.print()
        console.print(Panel(
            Markdown(result.stdout.strip()),
            title=f"[bold {A1}]Rorchestra[/]",
            subtitle=f"[{DIM}]{datetime.now().strftime('%H:%M')}[/]",
            border_style=P3,
            box=box.ROUNDED,
            padding=(1, 2),
        ))
    else:
        _error(f"Error: {result.stderr[:300]}")


def handle_tokens(args: str):
    """Display token usage for the current session."""
    from app.services.token_tracker import summary as token_summary, _fmt

    stats = token_summary()

    if stats["invocations"] == 0:
        _dim("No Gemini invocations yet this session.")
        return

    table = Table(title="Token Usage", box=box.SIMPLE_HEAVY)
    table.add_column("", style=MUTED, no_wrap=True)
    table.add_column("Input", style=f"bold {A1}", justify="right")
    table.add_column("Output", style=f"bold {A2}", justify="right")
    table.add_column("Total", style=f"bold {P1}", justify="right")

    table.add_row(
        "Last call",
        _fmt(stats["last_input"]),
        _fmt(stats["last_output"]),
        _fmt(stats["last_input"] + stats["last_output"]),
    )
    table.add_row(
        "Session",
        _fmt(stats["session_input"]),
        _fmt(stats["session_output"]),
        _fmt(stats["session_total"]),
    )

    console.print()
    console.print(f"  [{DIM}]Invocations: {stats['invocations']}[/]")
    console.print(table)
    console.print()


def handle_skills(args: str):
    """Load/reload skills."""
    from app.services.memory.skill_loader import load_all_skills
    with console.status(f"  [{P1}]Loading skills...[/]", spinner="dots"):
        load_all_skills()
    _success("Skills loaded")


def handle_apply(args: str):
    """Apply a patch proposal and commit the memory invalidation cascade."""
    if not args.strip().isdigit():
        _warn("Usage: /apply <proposal_id>")
        return
        
    proposal_id = int(args.strip())
    from sqlalchemy import select
    from app.models.entities import EditProposal, Task, Repository
    from app.storage.database import get_session
    from app.services.memory.refresh import invalidate_hierarchy
    from app.services.patch_apply import apply_patch_to_dir

    session = get_session()
    try:
        proposal = session.get(EditProposal, proposal_id)
        if not proposal:
            _error(f"Proposal #{proposal_id} not found.")
            return

        # Patch is pre-normalized at write time by save_proposal()
        content = (proposal.patch_content or "").strip()

        # Extract changed files from diff headers
        import re
        changed_files = []
        for line in content.splitlines():
            if line.startswith("+++ b/"):
                changed_files.append(line[6:].strip())
            elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
                changed_files.append(line[4:].strip())
        changed_files = list(dict.fromkeys(changed_files))

        if not changed_files:
            _warn("No changed files found in this patch proposal.")
            return

        # Apply patch to repo on disk
        task = session.get(Task, proposal.task_id)
        repo = session.get(Repository, task.repo_id) if task else None
        if repo and getattr(repo, "root_path", None):
            applied, patch_errors = apply_patch_to_dir(content, repo.root_path)
            if patch_errors:
                for err in patch_errors:
                    _warn(err)
            if applied:
                _success(f"Updated {len(applied)} file(s) in repo: " + ", ".join(applied[:5]) + ("..." if len(applied) > 5 else ""))
                changed_files = applied
        else:
            _warn("No repo root for this task; patch not applied to disk.")

        invalidations = invalidate_hierarchy(proposal.task_id, changed_files)
        
        if any(invalidations.values()):
            _success(
                f"Patch #{proposal_id} applied! Memory rebuilt: "
                f"{invalidations['script']} scripts, {invalidations['domain']} domains invalidated."
            )
        else:
            _success(f"Patch #{proposal_id} applied! (No memory cascade required).")
    finally:
        session.close()

def handle_normalize(args: str):
    """Fix literal \\t and \\\" in all .luau files under the ingested repo."""
    from pathlib import Path
    from app.storage.database import get_session
    from app.models.entities import Repository
    from app.services.patch_apply import _normalize_patch_output
    from sqlalchemy import select

    session = get_session()
    try:
        repos = session.execute(select(Repository)).scalars().all()
        if not repos:
            _error("No repositories. Use /ingest first.")
            return
        repo = repos[0]
        root = getattr(repo, "root_path", None)
        if not root or not Path(root).exists():
            _error("Repo root not set or missing.")
            return
        root_path = Path(root)
        updated = 0
        for path in root_path.rglob("*.luau"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                normalized = _normalize_patch_output(text)
                if normalized != text:
                    path.write_text(normalized, encoding="utf-8")
                    updated += 1
                    _dim(str(path.relative_to(root_path)))
            except Exception as e:
                _warn(f"{path.name}: {e}")
        if updated:
            _success(f"Normalized {updated} file(s).")
        else:
            _info("No files needed normalization.")
    finally:
        session.close()


def handle_mcp(args: str):
    """Display status of configured MCP servers."""
    import subprocess
    from rich.table import Table

    # Run from orchestrator root so Gemini CLI finds .gemini/settings.json
    _mcp_cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    with console.status(f"  [{P1}]Checking MCP servers...[/]", spinner="dots"):
        try:
            cmd_name = "gemini.cmd" if os.name == "nt" else "gemini"
            result = subprocess.run(
                [cmd_name, "mcp", "list"],
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                cwd=_mcp_cwd,
            )
        except Exception as e:
            _error(f"Failed to run 'gemini mcp list': {e}")
            return

    if result.returncode != 0:
        _error("Failed to fetch MCP status.")
        if result.stderr:
            _dim(result.stderr.strip())
        return

    # gemini CLI sometimes outputs to stderr
    output = result.stdout + "\n" + result.stderr
    
    import re
    output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
    lines = []
    for raw_line in output.splitlines():
        res = []
        for c in raw_line:
            if c == '\r':
                res.clear()
            elif c == '\x08':
                if res: res.pop()
            else:
                res.append(c)
        final = "".join(res).strip()
        if final:
            lines.append(final)
    
    table = Table(title="Connected MCP Servers")
    table.add_column("State", justify="center")
    table.add_column("Server Name", style="cyan", no_wrap=True)
    table.add_column("Command", style="dim")

    count = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("Loaded cached") or line.startswith("Configured MCP"):
            continue
            
        # Example format: "✗ Roblox_Studio: C:\Tools... (stdio) - Disconnected"
        # or "✓ Roblox_Studio: ... - Connected"
        
        state_icon = ""
        if line.startswith("✓"):
            state_icon = "[green]✓ Connected[/green]"
            line = line[1:].strip()
        elif line.startswith("✗"):
            state_icon = "[red]✗ Disconnected[/red]"
            line = line[1:].strip()
        elif line.startswith("⚠") or line.startswith("!"):
            state_icon = "[yellow]⚠ Error[/yellow]"
            line = line[1:].strip()
            
        if ":" in line:
            name, rest = line.split(":", 1)
            # Remove the " - Disconnected" / " - Connected" suffix from command string
            if " - Disconnected" in rest:
                rest = rest.replace(" - Disconnected", "")
            elif " - Connected" in rest:
                rest = rest.replace(" - Connected", "")
                
            table.add_row(state_icon, name.strip(), rest.strip())
            count += 1

    if count == 0:
        _warn("No active MCP servers found.")
        return

    console.print()
    console.print(table)
    console.print()


def handle_natural_language(text: str):
    """Handle natural language — route to /ask."""
    handle_ask(text)


def handle_plans(args: str):
    """List, view, or execute saved plans."""
    args = args.strip()

    # /plans run <N> — execute a saved plan
    if args.lower().startswith("run"):
        num_str = args[3:].strip()
        if not num_str.isdigit():
            _warn("Usage: /plans run <N>")
            return
        plan_id = int(num_str)
        plan = _load_plan(plan_id)
        if not plan:
            _error(f"Plan #{plan_id} not found. Use /plans to list.")
            return

        _header(f"Executing Plan #{plan_id}")
        _dim(plan["description"][:100])
        console.print()

        # Reconstruct packet from saved JSON
        from app.models.schemas import ContextPacketSchema
        packet = ContextPacketSchema(**plan["packet"])
        repo_path = plan["repo_path"]

        from app.config import settings
        from app.services.workers.lifecycle import invoke_edit_worker
        from app.services.patch_apply import apply_patch_to_dir

        max_per_edit = getattr(settings, "max_files_per_edit", 25)
        file_paths = list(packet.file_bodies.keys())
        n_files = len(file_paths)

        if n_files <= max_per_edit:
            with console.status(f"  [{P1}]⚙️  Edit worker running...[/]", spinner="dots"):
                result = invoke_edit_worker(packet, cwd=repo_path)
            _print_token_line(result)

            if result.patch_content:
                applied, errs = apply_patch_to_dir(result.patch_content, repo_path)
                if applied:
                    _success(f"Applied changes to {len(applied)} file(s)")
                    for f in applied:
                        _dim(f"  {f}")
                for e in errs:
                    _warn(e)
            elif result.exit_code == 0:
                _success("Worker completed (changes applied directly)")
            else:
                _error(f"Worker failed (exit={result.exit_code})")
                if result.stderr:
                    _dim(result.stderr[:300])
        else:
            _warn(f"Plan has {n_files} files — batched execution not yet supported via /plans run.")
            _dim("Run the original /edit command without --plan instead.")
        return

    # /plans <N> — view a specific plan
    if args.isdigit():
        plan_id = int(args)
        plan = _load_plan(plan_id)
        if not plan:
            _error(f"Plan #{plan_id} not found.")
            return

        packet_data = plan.get("packet", {})
        ts = plan.get("timestamp", "")[:19].replace("T", " ")

        _header(f"Plan #{plan_id}  ({ts})")
        console.print(f"  [{DIM}]Description:[/] {plan['description'][:120]}")
        console.print(f"  [{DIM}]Files:[/]       {len(packet_data.get('file_bodies', {}))}")
        console.print(f"  [{DIM}]Invariants:[/]  {len(plan.get('invariants', []))}")
        console.print(f"  [{DIM}]Risks:[/]       {len(plan.get('risks', []))}")
        console.print()

        _info("Files:")
        for fp in sorted(packet_data.get("file_bodies", {}).keys()):
            _dim(f"  {fp}")

        if plan.get("invariants"):
            console.print()
            _info("Invariants:")
            for inv_text in plan["invariants"]:
                console.print(f"  [{A1}]•[/] {inv_text}")

        if plan.get("risks"):
            console.print()
            _info("Risks:")
            for r in plan["risks"]:
                console.print(f"  [{WARN}]⚠[/] {r}")

        console.print()
        _dim(f"Run with: /plans run {plan_id}")
        return

    # /plans — list all saved plans
    plans = _load_plans()
    if not plans:
        _dim("No saved plans. Use /edit <desc> --plan to create one.")
        return

    _header("Saved Plans")
    for p in plans:
        ts = p.get("timestamp", "")[:16].replace("T", " ")
        n_files = len(p.get("packet", {}).get("file_bodies", {}))
        desc_short = p["description"][:80]
        console.print(
            f"  [{A1}]#{p['id']}[/]  [{DIM}]{ts}[/]  "
            f"[{MUTED}]{n_files} files[/]  {desc_short}"
        )
    console.print()
    _dim("View: /plans <N>  ·  Execute: /plans run <N>")


# ── Command dispatch ────────────────────────────────────────────────────


HANDLERS = {
    "/help": lambda a: show_help(),
    "/status": handle_status,
    "/ingest": handle_ingest,
    "/summarize": handle_summarize,
    "/edit": handle_edit,
    "/ask": handle_ask,
    "/plans": handle_plans,
    "/plan": handle_plans,
    "/skills": handle_skills,
    "/apply": handle_apply,
    "/normalize": handle_normalize,
    "/mcp": handle_mcp,
    "/tokens": handle_tokens,
    "/clear": lambda a: (os.system("cls" if os.name == "nt" else "clear"), _print_logo()),
    "/quit": lambda a: (console.print(f"\n  [{P1}]👋 Goodbye![/]\n"), sys.exit(0)),
    "/q": lambda a: (console.print(f"\n  [{P1}]👋 Goodbye![/]\n"), sys.exit(0)),
    "/exit": lambda a: (console.print(f"\n  [{P1}]👋 Goodbye![/]\n"), sys.exit(0)),
}


def dispatch(user_input: str):
    """Parse and dispatch user input."""
    stripped = user_input.strip()
    if not stripped:
        return

    if stripped.startswith("/"):
        parts = stripped.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler = HANDLERS.get(cmd)
        if handler:
            try:
                handler(args)
            except Exception as exc:
                _error(str(exc))
                import traceback
                from rich.markup import escape
                _dim(escape(traceback.format_exc()[-300:]))
        else:
            _warn(f"Unknown command: {cmd}  ·  Try /help")
    else:
        handle_natural_language(stripped)


# ── Prompt styling ──────────────────────────────────────────────────────


prompt_style = PTStyle.from_dict({
    "prompt_arrow":  f"{P1} bold",
    "prompt_dot":    f"{DIM}",
})


def _get_prompt():
    name = _active_repo.name if _active_repo else "no project"
    return [
        ("class:prompt_dot", f"{name} "),
        ("class:prompt_arrow", "❯ "),
    ]


# ── Main REPL ───────────────────────────────────────────────────────────


def main():
    """Entry point for Rorchestra interactive CLI."""
    from app.storage.database import init_db
    init_db()

    _print_welcome()

    # Auto-detect project from CWD
    _auto_init_from_cwd()

    history_path = os.path.expanduser("~/.rorchestra_history")
    session = PromptSession(
        history=FileHistory(history_path),
        auto_suggest=AutoSuggestFromHistory(),
        completer=command_completer,
        style=prompt_style,
        complete_while_typing=True,
    )

    last_interrupt = 0.0
    while True:
        try:
            user_input = session.prompt(_get_prompt)
            dispatch(user_input)
            console.print()
            last_interrupt = 0.0
        except KeyboardInterrupt:
            now = time.time()
            if now - last_interrupt < 1.5:
                console.print(f"\n  [{P1}]👋 Goodbye![/]\n")
                break
            last_interrupt = now
            console.print(f"\n  [{DIM}]Press Ctrl+C again to exit[/]")
        except EOFError:
            console.print(f"\n  [{P1}]👋 Goodbye![/]\n")
            break


if __name__ == "__main__":
    main()

"""
CLI entry point for the orchestrator.

Usage:
    python -m app.main ingest <repo_path>
    python -m app.main summarize --repo-id <id>
    python -m app.main edit "<description>" --repo-id <id> --scope <scope>
    python -m app.main validate --task-id <id>
    python -m app.main check <uncertainty_type> <target>
    python -m app.main status
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from app.config import settings
from app.storage.database import init_db

cli = typer.Typer(
    name="orch",
    help="Roblox/Luau AI orchestration system",
    no_args_is_help=True,
)
console = Console()


@cli.callback()
def startup():
    """Initialise the database on every invocation (idempotent)."""
    init_db()


# ── ingest ────────────────────────────────────────────────────────────────


@cli.command()
def ingest(
    repo_path: str = typer.Argument(..., help="Path to a Rojo project root"),
):
    """Ingest a Rojo/Luau repository — build sourcemap, index scripts, create graph."""
    from app.services.ingest.pipeline import ingest_repository
    from app.services.graph.builder import build_or_refresh_graph
    from app.telemetry.metrics import record_ingest

    with console.status("[bold green]Ingesting repository…"):
        snapshot = ingest_repository(repo_path)

    console.print(f"[bold]Repository ingested:[/bold] {snapshot.name}")
    console.print(f"  Scripts: {snapshot.script_count}")
    console.print(f"  Domains: {len(snapshot.domains)}")
    for d in snapshot.domains:
        console.print(f"    • {d.name} ({d.kind}) — {len(d.scripts)} scripts")

    with console.status("[bold green]Building graph edges…"):
        delta = build_or_refresh_graph(snapshot.repo_id)

    console.print(
        f"  Graph: +{delta.added_edges} edges "
        f"(replaced {delta.removed_edges})"
    )
    record_ingest(snapshot.repo_id, snapshot.script_count, delta.added_edges)

    # Auto-load skills into memory
    from app.services.memory.skill_loader import load_all_skills
    loaded = load_all_skills()
    if loaded:
        console.print(f"  Skills loaded: {len(loaded)}")
        for sk in loaded:
            console.print(f"    • {sk['name']}")


# ── load-skills ───────────────────────────────────────────────────────────


@cli.command(name="load-skills")
def load_skills():
    """Load or reload skill files from the skills directory into procedural memory."""
    from app.services.memory.skill_loader import load_all_skills

    loaded = load_all_skills()
    if not loaded:
        console.print("[dim]No skill files found in skills/ directory.[/dim]")
        return

    console.print(f"[bold green]Loaded {len(loaded)} skill(s):[/bold green]")
    for sk in loaded:
        triggers = sk.get('triggers', {})
        sides = triggers.get('runtime_sides', ['all'])
        keywords = triggers.get('scope_keywords', [])
        console.print(f"  • [cyan]{sk['name']}[/cyan]")
        console.print(f"    Sides: {', '.join(sides)}")
        if keywords:
            console.print(f"    Keywords: {', '.join(keywords[:8])}{'…' if len(keywords) > 8 else ''}")


# ── summarize ─────────────────────────────────────────────────────────────


@cli.command()
def summarize(
    repo_id: int = typer.Option(..., "--repo-id", "-r", help="Repository ID to summarise"),
    domain_only: bool = typer.Option(False, "--domain-only", help="Only summarise domains, skip scripts"),
    workers: int = typer.Option(4, "--workers", "-w", help="Max parallel Gemini CLI workers (1=sequential)"),
):
    """Generate semantic memory summaries for scripts and domains."""
    from sqlalchemy import select
    from app.models.entities import Domain, Script, Repository
    from app.services.summarization.summarizer import (
        summarise_domain,
        summarise_scripts_parallel,
    )
    from app.storage.database import get_session
    import threading

    session = get_session()
    repo = session.get(Repository, repo_id)
    if not repo:
        console.print(f"[red]Repository {repo_id} not found[/red]")
        raise typer.Exit(1)

    repo_root = repo.root_path

    if not domain_only:
        scripts = session.execute(
            select(Script).where(Script.repo_id == repo_id)
        ).scalars().all()

        script_ids = [s.id for s in scripts]
        # Build ID -> path lookup for progress display
        id_to_path = {s.id: (s.instance_path or s.file_path) for s in scripts}

        completed = {"count": 0}
        total = len(script_ids)
        lock = threading.Lock()

        def _on_complete(sid, summary, error):
            with lock:
                completed["count"] += 1
                n = completed["count"]
            status = "[green]ok[/green]" if not error else f"[red]{error[:60]}[/red]"
            console.print(f"  [{n}/{total}] {id_to_path.get(sid, sid)} — {status}")

        console.print(f"[bold]Summarising {total} scripts with {workers} workers…[/bold]")
        summarise_scripts_parallel(
            script_ids,
            repo_root,
            max_workers=workers,
            on_complete=_on_complete,
        )
        console.print(f"[bold green]All {total} scripts summarised.[/bold green]")

    domains = session.execute(
        select(Domain).where(Domain.repo_id == repo_id)
    ).scalars().all()
    # Domains are done sequentially (there are only a few)
    with console.status(f"[bold green]Summarising {len(domains)} domains…"):
        for d in domains:
            summarise_domain(d.id, repo_root)
            console.print(f"  ok {d.name}")

    session.close()
    console.print("[bold green]Summarisation complete.[/bold green]")


# ── ask ───────────────────────────────────────────────────────────────────


@cli.command()
def ask(
    question: str = typer.Argument(..., help="Question about the codebase"),
    repo_id: int = typer.Option(1, "--repo-id", "-r"),
    scope: str = typer.Option("", "--scope", "-s", help="Optional scope(s) to focus on, comma-separated"),
):
    """Ask a question about the codebase using dynamic context retrieval."""
    from app.models.entities import Repository
    from app.services.agents.tools import list_scripts, list_domains, get_contracts
    from app.adapters.gemini_cli import invoke_standalone
    from app.storage.database import get_session

    session = get_session()
    repo = session.get(Repository, repo_id)
    if not repo:
        console.print(f"[red]Repository {repo_id} not found[/red]")
        raise typer.Exit(1)
    session.close()

    # Dynamic context retrieval via agent tools
    domains = list_domains(repo_id)
    domain_ctx = "\n".join(
        f"## Domain: {d['name']} ({d['kind']}, {d['script_count']} scripts)\n{d['summary']}"
        for d in domains
    )

    script_ctx = ""
    if scope:
        scopes = [s.strip() for s in scope.split(",") if s.strip()]
        all_scripts = []
        for sc in scopes:
            all_scripts.extend(list_scripts(repo_id, pattern=sc))
        # Deduplicate by id
        seen = set()
        unique = []
        for s in all_scripts:
            if s["id"] not in seen:
                seen.add(s["id"])
                unique.append(s)
        script_ctx = "\n\n## Relevant Scripts\n" + "\n".join(
            f"- **{s['instance_path']}** ({s['script_type']}): {s['summary']}"
            for s in unique
        )

    contracts = get_contracts(repo_id)
    contract_ctx = ""
    if contracts:
        contract_ctx = "\n\n## Contracts\n" + "\n".join(
            f"- {c['name']} ({c['kind']}): {c['summary']}" for c in contracts
        )

    prompt = f"""\
You are a Roblox/Luau codebase expert. Answer the following question
using ONLY the repository knowledge provided below. Be concise.

# Repository: {repo.name}

{domain_ctx}
{script_ctx}
{contract_ctx}

# Question
{question}
"""

    with console.status("[bold green]Thinking..."):
        result = invoke_standalone(prompt, timeout=60)

    if result.exit_code == 0:
        console.print(result.stdout.strip())
    else:
        console.print(f"[red]Error: {result.stderr[:300]}[/red]")


# ── edit ──────────────────────────────────────────────────────────────────


@cli.command()
def edit(
    description: str = typer.Argument(..., help="Natural-language edit request"),
    repo_id: int = typer.Option(..., "--repo-id", "-r"),
    scope: str = typer.Option("", "--scope", "-s", help="Target scope(s), comma-separated (e.g. 'DashHandler,CooldownModule')"),
    side: str = typer.Option("unknown", "--side", help="Runtime side: server|client|shared"),
    investigation_workers: int | None = typer.Option(None, "--investigation-workers", "-w", help="Parallel workers for investigation (docs + deep read). Default from config."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show each investigation phase's input and subagent output (no spinner)."),
):
    """Investigate context dynamically, assemble packet, and launch edit worker."""
    from app.models.entities import Task, TaskStatus, Repository
    from app.services.agents.orchestrator import run_investigation
    from app.services.workers.lifecycle import invoke_edit_worker, save_proposal
    from app.services.memory.hierarchy import propagate_invalidation
    from app.policies.safety import is_high_risk, require_review
    from app.telemetry.metrics import record_packet, record_worker
    from app.storage.database import get_session

    if side == "unknown":
        dl = description.lower()
        if "replicate" in dl or "sync" in dl or "shared" in dl or "remote" in dl:
            side = "shared"
        elif "client" in dl and "server" not in dl:
            side = "client"
        elif "server" in dl and "client" not in dl:
            side = "server"

    session = get_session()
    repo = session.get(Repository, repo_id)
    if not repo:
        console.print(f"[red]Repository {repo_id} not found[/red]")
        raise typer.Exit(1)

    if is_high_risk(description):
        console.print("[yellow]High-risk task detected. Proceeding with caution.[/yellow]")

    # 1. Create task
    task = Task(
        repo_id=repo_id,
        description=description,
        status=TaskStatus.pending,
        target_scope=scope or None,
        runtime_side=side,
    )
    session.add(task)
    session.commit()
    task_id = task.id
    session.close()

    # 2. Investigate (agents explore dynamically, packet assembled LAST)
    if verbose:
        console.print("[bold green]Investigating (verbose: phase I/O visible)...[/]\n")
        packet, investigation = run_investigation(task_id, investigation_workers=investigation_workers, verbose=True)
    else:
        with console.status("[bold green]Repo investigator exploring..."):
            packet, investigation = run_investigation(task_id, investigation_workers=investigation_workers)

    record_packet(task_id, len(packet.model_dump_json()))
    console.print(
        f"[bold]Investigation complete[/bold] — "
        f"{len(investigation.relevant_script_ids)} scripts identified, "
        f"{len(packet.file_bodies)} file bodies in packet"
    )
    if investigation.risks:
        for r in investigation.risks[:3]:
            console.print(f"  [yellow]Risk:[/yellow] {r}")
    if investigation.uncertainties:
        for u in investigation.uncertainties[:3]:
            console.print(f"  [cyan]Uncertain:[/cyan] {u}")

    # 3. Launch fresh external edit worker
    with console.status("[bold green]Running edit worker..."):
        result = invoke_edit_worker(packet, cwd=repo.root_path)

    record_worker(task_id, result.worker_type, result.exit_code, result.elapsed_secs)

    if result.patch_content:
        proposal_id = save_proposal(task_id, result.patch_content)
        console.print(f"[bold green]Patch proposal #{proposal_id} saved.[/bold green]")

        # Parse patch to find changed files for invalidation analysis
        import re
        changed_files = []
        for line in result.patch_content.splitlines():
            if line.startswith("+++ b/"):
                changed_files.append(line[6:].strip())
            elif line.startswith("+++ "):
                changed_files.append(line[4:].strip())
        
        if changed_files:
            from app.services.memory.refresh import analyze_invalidation_impact
            invalidations = analyze_invalidation_impact(changed_files)
            if any(invalidations.values()):
                console.print(
                    f"[dim]Candidate Impact: Applying this patch will invalidate "
                    f"{invalidations['script']} scripts, and "
                    f"{invalidations['domain']} domains.[/dim]"
                )

        if require_review(description, result.patch_content):
            console.print("[yellow]This patch requires manual review before applying.[/yellow]")
        else:
            console.print("[dim]Patch may be applied directly.[/dim]")
    else:
        console.print(f"[red]Worker failed (exit={result.exit_code})[/red]")
        if result.stderr:
            console.print(f"[dim]{result.stderr[:500]}[/dim]")


# ── validate ──────────────────────────────────────────────────────────────


@cli.command()
def validate(
    task_id: int = typer.Option(..., "--task-id", "-t"),
):
    """Run static validation on the latest patch proposal for a task."""
    from sqlalchemy import select
    from app.models.entities import EditProposal, Task, Repository
    from app.services.validation.static import validate_patch_static
    from app.telemetry.metrics import record_validation
    from app.storage.database import get_session

    session = get_session()
    task = session.get(Task, task_id)
    if not task:
        console.print(f"[red]Task {task_id} not found[/red]")
        raise typer.Exit(1)

    repo = session.get(Repository, task.repo_id)
    proposal = session.execute(
        select(EditProposal)
        .where(EditProposal.task_id == task_id)
        .order_by(EditProposal.created_at.desc())
    ).scalars().first()

    if not proposal:
        console.print("[red]No patch proposal found for this task.[/red]")
        raise typer.Exit(1)

    console.print(f"Validating proposal #{proposal.id}…")
    result = validate_patch_static(
        repo.root_path,
        proposal.patch_content,
        task.target_scope or "",
    )
    record_validation(task_id, result["status"], "static")

    if result["status"] == "pass":
        console.print("[bold green]✓ Static validation passed[/bold green]")
    else:
        console.print("[bold red]✗ Static validation failed[/bold red]")
        for d in result.get("new_diagnostics", []):
            console.print(f"  • {d}")

    session.close()


# ── check ─────────────────────────────────────────────────────────────────


@cli.command()
def check(
    uncertainty_type: str = typer.Argument(..., help="ui_existence | remote_existence | runtime_path_mismatch"),
    target: str = typer.Argument(..., help="Instance path to check"),
):
    """Run an uncertainty-triggered MCP validation check against live Studio."""
    from app.services.mcp.trigger_policy import should_trigger_mcp
    from app.services.mcp.validator import run_mcp_check
    from app.telemetry.metrics import record_mcp_call

    if not should_trigger_mcp(uncertainty_type, target):
        console.print("[dim]MCP check not needed — valid environment memory exists.[/dim]")
        return

    with console.status("[bold green]Querying Studio…"):
        vr = run_mcp_check(uncertainty_type, target)

    record_mcp_call(uncertainty_type, vr.status)

    colour = {"pass": "green", "fail": "red", "uncertain": "yellow"}.get(vr.status, "white")
    console.print(f"[bold {colour}]{vr.status.upper()}[/bold {colour}]  {vr.key_findings}")
    if vr.actual_paths:
        console.print(f"  Paths: {', '.join(vr.actual_paths)}")
    if vr.recommended_action:
        console.print(f"  Action: {vr.recommended_action}")


# ── status ────────────────────────────────────────────────────────────────


@cli.command()
def status():
    """Show a summary of repositories, domains, scripts, and memory health."""
    from sqlalchemy import select, func
    from app.models.entities import Repository, Domain, Script, MemoryRecord, GraphEdge
    from app.storage.database import get_session

    session = get_session()

    repos = session.execute(select(Repository)).scalars().all()
    if not repos:
        console.print("[dim]No repositories ingested yet.[/dim]")
        return

    for repo in repos:
        table = Table(title=f"Repository: {repo.name}", show_lines=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")

        script_count = session.execute(
            select(func.count(Script.id)).where(Script.repo_id == repo.id)
        ).scalar()
        domain_count = session.execute(
            select(func.count(Domain.id)).where(Domain.repo_id == repo.id)
        ).scalar()
        edge_count = session.execute(
            select(func.count(GraphEdge.id))
        ).scalar()
        total_memories = session.execute(
            select(func.count(MemoryRecord.id))
        ).scalar()
        stale_memories = session.execute(
            select(func.count(MemoryRecord.id)).where(
                MemoryRecord.invalidated_by.isnot(None)
            )
        ).scalar()

        table.add_row("Root path", str(repo.root_path))
        table.add_row("Scripts", str(script_count))
        table.add_row("Domains", str(domain_count))
        table.add_row("Graph edges", str(edge_count))
        table.add_row("Memory records", str(total_memories))
        table.add_row("Stale memories", str(stale_memories))

        console.print(table)

    session.close()


# ── __main__ support ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()

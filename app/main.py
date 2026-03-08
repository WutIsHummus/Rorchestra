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


# ── summarize ─────────────────────────────────────────────────────────────


@cli.command()
def summarize(
    repo_id: int = typer.Option(..., "--repo-id", "-r", help="Repository ID to summarise"),
    domain_only: bool = typer.Option(False, "--domain-only", help="Only summarise domains, skip scripts"),
):
    """Generate semantic memory summaries for scripts and domains."""
    from sqlalchemy import select
    from app.models.entities import Domain, Script, Repository
    from app.services.summarization.summarizer import summarise_domain, summarise_script
    from app.storage.database import get_session

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
        with console.status(f"[bold green]Summarising {len(scripts)} scripts…"):
            for s in scripts:
                summarise_script(s.id, repo_root)
                console.print(f"  ✓ {s.instance_path or s.file_path}")

    domains = session.execute(
        select(Domain).where(Domain.repo_id == repo_id)
    ).scalars().all()
    with console.status(f"[bold green]Summarising {len(domains)} domains…"):
        for d in domains:
            summarise_domain(d.id, repo_root)
            console.print(f"  ✓ {d.name}")

    session.close()
    console.print("[bold green]Summarisation complete.[/bold green]")


# ── edit ──────────────────────────────────────────────────────────────────


@cli.command()
def edit(
    description: str = typer.Argument(..., help="Natural-language edit request"),
    repo_id: int = typer.Option(..., "--repo-id", "-r"),
    scope: str = typer.Option("", "--scope", "-s", help="Target scope (instance path fragment)"),
    side: str = typer.Option("server", "--side", help="Runtime side: server|client|shared"),
):
    """Assemble a context packet and launch a fresh edit worker."""
    from app.models.entities import Task, TaskStatus, Repository
    from app.services.packets.assembler import assemble_packet
    from app.services.workers.lifecycle import invoke_edit_worker, save_proposal
    from app.policies.safety import is_high_risk, require_review
    from app.telemetry.metrics import record_packet, record_worker
    from app.storage.database import get_session

    session = get_session()
    repo = session.get(Repository, repo_id)
    if not repo:
        console.print(f"[red]Repository {repo_id} not found[/red]")
        raise typer.Exit(1)

    if is_high_risk(description):
        console.print("[yellow]⚠  High-risk task detected. Proceeding with caution.[/yellow]")

    # Create task
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

    # Assemble packet
    with console.status("[bold green]Assembling context packet…"):
        packet = assemble_packet(task_id, repo.root_path)

    record_packet(task_id, len(packet.model_dump_json()))
    console.print(f"[bold]Packet assembled[/bold] — {len(packet.file_bodies)} file bodies")

    # Launch worker
    with console.status("[bold green]Running edit worker…"):
        result = invoke_edit_worker(packet, cwd=repo.root_path)

    record_worker(task_id, result.worker_type, result.exit_code, result.elapsed_secs)

    if result.patch_content:
        proposal_id = save_proposal(task_id, result.patch_content)
        console.print(f"[bold green]Patch proposal #{proposal_id} saved.[/bold green]")

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

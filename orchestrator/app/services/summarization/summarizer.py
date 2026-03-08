"""
Script and domain summariser — generates compact summaries
using Gemini CLI and stores them as semantic memory records.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.adapters.gemini_cli import invoke_standalone
from app.models.entities import Domain, MemoryType, Script
from app.services.memory.store import upsert_memory
from app.storage.database import get_session


_SCRIPT_SUMMARY_PROMPT = """\
You are a Luau/Roblox code analyst.  Summarise the following script in ≤5 bullet points.
Focus on:  purpose, exported API surface, key dependencies, runtime side (server/client/shared),
and any cross-cutting contracts (remotes, shared config, UI bindings).

Script path: {instance_path}
Script type: {script_type}
Requires: {requires}

--- SOURCE ---
{source}
--- END SOURCE ---

Return ONLY the bullet-point summary, no preamble.
"""

_DOMAIN_SUMMARY_PROMPT = """\
You are a Roblox game architecture analyst.  Summarise the following domain
(logical grouping of scripts on the {kind} side) in ≤8 bullet points.
Focus on: key modules, public APIs, internal patterns, invariants, risks.

Domain: {name}
Scripts in this domain:
{script_summaries}

Return ONLY the bullet-point summary, no preamble.
"""


def summarise_script(
    script_id: int,
    repo_root: str | Path,
) -> str:
    """Generate and store a summary for a single script."""
    session = get_session()
    try:
        script = session.get(Script, script_id)
        if script is None:
            raise ValueError(f"Script {script_id} not found")

        abs_path = Path(repo_root) / script.file_path
        source = abs_path.read_text(encoding="utf-8", errors="replace") if abs_path.exists() else "(file not found)"

        # Truncate very large files to stay within token budgets
        if len(source) > 12_000:
            source = source[:12_000] + "\n... (truncated)"

        prompt = _SCRIPT_SUMMARY_PROMPT.format(
            instance_path=script.instance_path or script.file_path,
            script_type=script.script_type or "unknown",
            requires=", ".join(script.requires) or "none",
            source=source,
        )

        result = invoke_standalone(prompt, timeout=60)
        summary = result.stdout.strip() if result.exit_code == 0 else f"(summarisation failed: {result.stderr[:200]})"

        # Store as semantic memory
        scope_id = f"script:{script_id}"
        upsert_memory(
            scope_id,
            MemoryType.semantic,
            summary,
            source_refs=[script.file_path],
        )

        script.summary = summary
        session.commit()

        return summary
    finally:
        session.close()


def summarise_domain(domain_id: int, repo_root: str | Path) -> str:
    """Generate and store a summary for a domain by aggregating script summaries."""
    session = get_session()
    try:
        domain = session.get(Domain, domain_id)
        if domain is None:
            raise ValueError(f"Domain {domain_id} not found")

        scripts = session.execute(
            select(Script).where(Script.domain_id == domain_id)
        ).scalars().all()

        # Collect script summaries (generate missing ones)
        summaries: list[str] = []
        for s in scripts:
            if not s.summary:
                summarise_script(s.id, repo_root)
                session.refresh(s)
            summaries.append(f"- **{s.instance_path or s.file_path}**: {s.summary}")

        prompt = _DOMAIN_SUMMARY_PROMPT.format(
            name=domain.name,
            kind=domain.kind.value if domain.kind else "unknown",
            script_summaries="\n".join(summaries) or "(no scripts)",
        )

        result = invoke_standalone(prompt, timeout=90)
        summary = result.stdout.strip() if result.exit_code == 0 else f"(summarisation failed: {result.stderr[:200]})"

        scope_id = f"domain:{domain_id}"
        source_refs = [s.file_path for s in scripts]
        upsert_memory(
            scope_id,
            MemoryType.semantic,
            summary,
            source_refs=source_refs,
        )

        domain.summary = summary
        session.commit()
        return summary
    finally:
        session.close()

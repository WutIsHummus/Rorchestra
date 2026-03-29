"""
Agent orchestrator — coordinates multi-level subagent investigation
for dynamic context retrieval.

Flow:
    1. Task arrives with intent + optional scope hints
    2. Load repo/domain-level memory (lightweight initial context)
    3. Repo-investigator explores and narrows relevant scopes
    4. Domain-investigator drills into specific scripts/contracts
    5. MCP-validator runs only if uncertainty is flagged
    6. Packet assembler creates compact final packet from agent outputs
    7. Fresh external edit worker receives the packet
    8. Patch reviewer validates
    9. Memory invalidation propagates
"""

from __future__ import annotations

import json
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.gemini_cli import invoke_standalone
from app.config import settings
from app.models.entities import Repository, Task, TaskStatus, TaskClass, MemoryScope
from app.models.schemas import (
    ContextPacketSchema,
    InvestigationProvenance,
    InvariantEntry,
    RiskEntry,
    SelectionProvenance,
    SelectedDomain,
    SelectedScript,
    SelectedContract,
    merge_invariant_entries,
    merge_risk_entries,
)
from app.services.agents.tools import (
    list_scripts,
    list_domains,
    read_memory,
    read_script_source,
    get_contracts,
    search_graph,
)
from app.services.memory.hierarchy import get_stale_scopes
from app.storage.database import get_session

# Global concurrency budget: outer chunk workers + inner source-read workers
_investigation_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def _get_concurrency_semaphore() -> threading.Semaphore:
    """Lazy-init semaphore from settings.investigation_concurrency."""
    global _investigation_semaphore
    with _semaphore_lock:
        if _investigation_semaphore is None:
            cap = getattr(settings, "investigation_concurrency", 10)
            _investigation_semaphore = threading.Semaphore(max(1, cap))
        return _investigation_semaphore


# ── Task Classification ──────────────────────────────────────────────────

_CROSS_CUTTING_KEYWORDS = {
    "decouple", "refactor", "restructure", "architecture", "cross-cutting",
    "all scripts", "every script", "system-wide", "codebase", "everywhere",
    "contract", "interface", "coupling", "dependency",
}
_MIGRATION_KEYWORDS = {
    "migrate", "migration", "rewrite", "convert", "replace all",
    "deprecate", "large-scale", "revamp", "overhaul", "rearchitect",
}
_RUNTIME_KEYWORDS = {
    "teleport", "disappear", "runtime", "live", "studio", "in-game",
    "after join", "on spawn", "on death", "not showing", "invisible",
    "broken in", "only happens when",
}


def classify_task(task: Task) -> TaskClass:
    """Deterministic task classification via keyword heuristics."""
    desc = (task.description or "").lower()
    tokens = set(desc.split())

    if any(k in desc for k in _MIGRATION_KEYWORDS):
        return TaskClass.migration_refactor
    if any(k in desc for k in _CROSS_CUTTING_KEYWORDS):
        return TaskClass.cross_cutting
    if any(k in desc for k in _RUNTIME_KEYWORDS):
        return TaskClass.runtime_uncertain
    return TaskClass.semantic_localized


@dataclass
class InvestigationReport:
    """Structured output from the investigation phase."""
    task_id: int
    relevant_script_ids: list[int] = field(default_factory=list)
    file_bodies: dict[str, str] = field(default_factory=dict)
    invariants: list[str] = field(default_factory=list)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    agent_notes: str = ""
    # Selection provenance from hybrid triage
    selected_domains: list[SelectedDomain] = field(default_factory=list)
    selected_scripts: list[SelectedScript] = field(default_factory=list)
    selected_contracts: list[SelectedContract] = field(default_factory=list)
    # Large-change (revamp) workflow
    impact_script_ids: list[int] = field(default_factory=list)
    impact_contract_ids: list[int] = field(default_factory=list)
    revamp_session_id: int | None = None
    migration_brief: dict[str, Any] = field(default_factory=dict)


def _build_initial_context(task: Task, repo: Repository) -> str:
    """
    Build the lightweight initial context that repo-investigator receives.
    Includes: task intent, domain summaries, stale scope warnings.
    """
    domains = list_domains(repo.id)
    domain_text = "\n".join(
        f"- **{d['name']}** ({d['kind']}, {d['script_count']} scripts): {d['summary']}"
        for d in domains
    )

    # Check for stale scopes that need re-investigation
    stale = get_stale_scopes()
    stale_text = ""
    if stale:
        stale_text = f"\n\n## Stale Memories (need re-investigation)\n" + "\n".join(
            f"- {s}" for s in stale[:20]
        )

    return f"""\
# Task
{task.description}

## Target Scope
{task.target_scope or '(not specified — you must determine the relevant scope)'}

## Runtime Side
{task.runtime_side or 'unknown'}

## Repository: {repo.name}
Root: {repo.root_path}

## Domains
{domain_text}
{stale_text}
"""


import math
import re
from collections import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.entities import MemoryRecord, MemoryScope

_STOP = {"the", "a", "an", "to", "with", "for", "and", "or", "in", "on",
         "is", "it", "of", "by", "as", "at", "be", "do", "this", "that",
         "from", "not", "all", "use", "each", "every", "replace", "add",
         "remove", "create", "make", "update", "change", "fix", "implement",
         "which", "when", "how", "what", "where", "why", "can", "will", "would"}


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful alpha keywords from text, dropping common stop words."""
    return set(_tokenize(text))


def _tokenize(text: str) -> list[str]:
    """Tokenize for BM25: lowercase, alpha words, no stop words, len > 2."""
    if not text:
        return []
    words = re.findall(r"[a-z]+", str(text).lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def _bm25_scores(docs: list[str], query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """
    BM25 relevance scores for each doc against the query.
    Higher = more relevant. Returns one float per doc.
    """
    if not docs or not query:
        return [0.0] * len(docs)
    doc_tokens = [_tokenize(d) for d in docs]
    query_tokens = _tokenize(query)
    if not query_tokens:
        return [0.0] * len(docs)
    N = len(docs)
    doc_lens = [len(t) for t in doc_tokens]
    avgdl = sum(doc_lens) / N if N else 0
    doc_freq: Counter[str, int] = Counter()
    for toks in doc_tokens:
        for t in set(toks):
            doc_freq[t] += 1

    def idf(t: str) -> float:
        df = doc_freq.get(t, 0)
        return math.log((N - df + 0.5) / (df + 0.5) + 1.0)

    scores = []
    for toks, dlen in zip(doc_tokens, doc_lens):
        if dlen == 0:
            scores.append(0.0)
            continue
        s = 0.0
        tf = Counter(toks)
        for t in query_tokens:
            if t not in tf:
                continue
            tf_t = tf[t]
            s += idf(t) * (tf_t * (k1 + 1)) / (tf_t + k1 * (1 - b + b * dlen / avgdl))
        scores.append(s)
    return scores

def _triage_domains(task: Task, repo: Repository, session: Session) -> list[int]:
    """
    Phase 1: Domain Triage.
    Scans `domain` MemoryRecords and scores them against task keywords.
    Returns the top 2-3 domain IDs.
    """
    keywords = _extract_keywords(task.description)
    if task.target_scope:
        keywords.update(_extract_keywords(task.target_scope))

    # Fetch all domain memories (high-level structural context)
    domain_mems = session.execute(
        select(MemoryRecord).where(
            MemoryRecord.scope_level == MemoryScope.domain,
            MemoryRecord.invalidated_by.is_(None)
        )
    ).scalars().all()

    # If memory is empty (first run), fallback to raw domains table
    if not domain_mems:
        from app.models.entities import Domain
        domains = session.execute(select(Domain).where(Domain.repo_id == repo.id)).scalars().all()
        mem_source = [{"id": d.id, "text": f"{d.name} {d.summary}"} for d in domains]
    else:
        # Extract domain ID from scope_id (e.g. "domain:server" -> resolve to actual domain ID)
        from app.models.entities import Domain
        mem_source = []
        for m in domain_mems:
            d_name = m.scope_id.replace("domain:", "")
            d = session.execute(select(Domain.id).where(Domain.name == d_name, Domain.repo_id == repo.id)).scalar()
            if d:
                mem_source.append({"id": d, "text": m.content})

    # Score each domain
    scored = []
    for item in mem_source:
        item_words = _extract_keywords(item["text"])
        score = len(keywords.intersection(item_words))
        # If task explicitly names a domain (e.g. "server"), boost it heavily
        if task.target_scope and task.target_scope.lower() in item["text"].lower():
            score += 10
        if score > 0:
            scored.append((score, item["id"]))

    # If no keywords matched (vague/broad task), include all domains
    if not scored:
        scored = [(0.1, item["id"]) for item in mem_source]

    scored.sort(key=lambda x: x[0], reverse=True)
    cap = getattr(settings, "max_domains_triage", 10)
    if cap <= 0:
        return [did for _score, did in scored]
    return [did for _score, did in scored[:cap]]


def _ai_review_domains(
    task: Task,
    prefilter: list[tuple[float, int]],
    session: Session,
    repo: Repository,
    console,
    *,
    verbose: bool = False,
) -> list[SelectedDomain]:
    """
    Phase 1b: AI Investigator Review of domain candidates.
    Receives the BM25-scored prefilter output and can reorder, expand (bounded),
    narrow, and explain. Falls back to prefilter on timeout/error.
    """
    from app.models.entities import Domain

    # Build candidate info for the prompt
    domain_info = []
    for score, did in prefilter:
        d = session.get(Domain, did)
        if d:
            domain_info.append({
                "id": did,
                "name": d.name,
                "kind": d.kind.value if d.kind else "shared",
                "summary": d.summary or "(no summary)",
                "bm25_score": round(score, 2),
            })

    # All available domains (for AI to request additions)
    all_domains = session.execute(
        select(Domain).where(Domain.repo_id == repo.id)
    ).scalars().all()
    prefilter_ids = {did for _, did in prefilter}
    available_extras = [
        {"id": d.id, "name": d.name, "kind": d.kind.value if d.kind else "shared"}
        for d in all_domains if d.id not in prefilter_ids
    ]

    task_class = task.task_class.value if task.task_class else "semantic_localized"
    prompt = f"""\
You are a repo-investigator for a Roblox/Luau project.
Your job is to review domain candidates selected by BM25 and decide which are truly relevant.

# Task
{task.description}

## Task Class: {task_class}
## Target Scope: {task.target_scope or "(auto)"}
## Runtime Side: {task.runtime_side or "unknown"}

## BM25-Ranked Domain Candidates
{json.dumps(domain_info, indent=2)}

## Other Available Domains (not in candidates)
{json.dumps(available_extras, indent=2)}

## Instructions
- Review each candidate. Keep domains that are relevant; drop those that scored well lexically but are semantically irrelevant.
- You may add up to {settings.max_ai_domain_additions} extra domains from the "Other Available" list if architecturally necessary.
- For each selected domain, provide a 1-line reason.
- Return ONLY a JSON object:
```json
{{
    "selected": [
        {{"id": 1, "reason": "owns the DataManager module", "confidence": 0.9}},
    ],
    "dropped": [
        {{"id": 3, "reason": "UI domain not relevant to save logic"}}
    ]
}}
```
"""
    if not verbose:
        console.print(f"[dim]  Phase 1b: AI reviewing {len(domain_info)} domain candidates...[/dim]")

    gemini_cwd = str(settings.gemini_cli_cwd) if getattr(settings, "gemini_cli_cwd", None) else repo.root_path
    result = invoke_standalone(prompt, timeout=settings.triage_ai_timeout_secs, cwd=gemini_cwd, no_mcp=True)

    if verbose:
        _verbose_phase_io(console, "Domain Investigator (Phase 1b)", prompt[:3000], result)

    # Parse AI response; on failure, fall back to prefilter with BM25 provenance
    domain_by_id = {d.id: d for d in all_domains}
    fallback = [
        SelectedDomain(
            domain_id=did,
            domain_name=domain_by_id[did].name if did in domain_by_id else f"domain:{did}",
            provenance=SelectionProvenance(
                selected_by="bm25",
                selection_reason=f"BM25 score {score:.2f}",
                evidence_refs=[f"bm25_score:{score:.2f}"],
                confidence=min(0.9, 0.5 + score * 0.1),
            ),
        )
        for score, did in prefilter
        if did in domain_by_id
    ]

    if result.exit_code != 0:
        console.print(f"[dim]  Phase 1b: AI review failed (exit {result.exit_code}), using prefilter.[/dim]")
        return fallback

    parsed = _try_parse_json(result.stdout.strip() if result.stdout else "")
    if not parsed or "selected" not in parsed:
        console.print(f"[dim]  Phase 1b: AI response unparseable, using prefilter.[/dim]")
        return fallback

    selected: list[SelectedDomain] = []
    additions_count = 0
    for entry in parsed.get("selected", []):
        did = entry.get("id")
        reason = entry.get("reason", "AI selected")
        confidence = entry.get("confidence", 0.8)
        if did not in domain_by_id:
            continue
        is_addition = did not in prefilter_ids
        if is_addition:
            if additions_count >= settings.max_ai_domain_additions:
                continue
            additions_count += 1
        selected.append(SelectedDomain(
            domain_id=did,
            domain_name=domain_by_id[did].name,
            provenance=SelectionProvenance(
                selected_by="ai_investigator" if is_addition else "bm25+ai_confirmed",
                selection_reason=reason,
                evidence_refs=[f"bm25_score:{next((s for s, d in prefilter if d == did), 0):.2f}"] if not is_addition else [],
                confidence=confidence,
            ),
        ))

    if not selected:
        return fallback

    n_dropped = len(parsed.get("dropped", []))
    console.print(
        f"[cyan]Phase 1b: AI Review[/] -> Kept {len(selected)} domains"
        f"{f', added {additions_count}' if additions_count else ''}"
        f"{f', dropped {n_dropped}' if n_dropped else ''}."
    )
    return selected

def _content_patterns_for_task(description: str) -> list[str] | None:
    """If the task is about replication/stats/Value Object, return patterns to find all affected scripts."""
    d = (description or "").lower()
    if not any(
        k in d
        for k in (
            "replication",
            "value object",
            "stat sync",
            "clientstatstore",
            "getstat",
            "buffer-compressed",
            "physics recv",
        )
    ):
        return None
    return ["GetStat", "ClientStatStore", ".Value", "ValueObject", "stat", "replicat"]


def _script_ids_matching_content(
    repo_root: str,
    scripts: list[Any],
    patterns: list[str],
) -> set[int]:
    """Return script ids whose file content contains any of the given patterns."""
    from pathlib import Path
    repo_path = Path(repo_root)
    out: set[int] = set()
    for s in scripts:
        fp = getattr(s, "file_path", None)
        if not fp:
            continue
        path = repo_path / fp
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if any(p in content for p in patterns):
                out.add(s.id)
        except Exception:
            pass
    return out


def _triage_scripts(
    task: Task,
    domain_ids: list[int],
    session: Session,
    *,
    impact_script_ids: list[int] | None = None,
    impact_contract_ids: list[int] | None = None,
    repo_root: str | None = None,
) -> tuple[list[int], list[int]]:
    """
    Phase 2: Script and Contract Triage.
    Scores with BM25 over enriched docs; keeps scripts above a relevance threshold;
    takes a seed set (top 5–12 by score spread), expands one hop (or two in large-change
    mode) via requires/provides_contract/consumes_contract with decay; re-ranks by
    relevance, graph proximity, and (in revamp) recent-edit priority.
    When impact_script_ids/impact_contract_ids are provided (large-change mode), the
    scored pool is restricted to the impact set and expansion is 2-hop; migration-phase
    memories are preferred and scripts from recent accepted edits in the same revamp are boosted.
    """
    from app.models.entities import Script, Contract, Domain, GraphEdge, EdgeKind, MemoryPhase

    large = getattr(task, "large_change_mode", 0) or impact_script_ids is not None
    max_hops = 2 if large else 1

    # Domain scope for memory: use domain_ids from triage, or derive from impact set
    if not domain_ids and impact_script_ids:
        domain_ids = list({
            r[0] for r in session.execute(
                select(Script.domain_id).where(
                    Script.id.in_(impact_script_ids),
                    Script.domain_id.isnot(None),
                )
            ).all()
        })
    if not domain_ids and not impact_script_ids:
        return [], []

    query_text = task.description or ""
    if task.target_scope:
        query_text += " " + task.target_scope

    domain_by_id: dict[int, tuple[Any, Any]] = {}
    for row in session.execute(select(Domain.id, Domain.name, Domain.kind).where(Domain.id.in_(domain_ids))).all():
        domain_by_id[row[0]] = (row[1], row[2])
    parent_scopes = [f"domain:{n}" for n, _ in domain_by_id.values()]

    # Script -> contract names (for enriched doc)
    script_contract_names: dict[int, list[str]] = {}
    edges = session.execute(
        select(GraphEdge.source_id, GraphEdge.target_id).where(
            GraphEdge.source_type == "script",
            GraphEdge.edge_kind.in_([EdgeKind.provides_contract, EdgeKind.consumes_contract]),
        )
    ).all()
    contract_ids_from_edges = set()
    for src, tgt in edges:
        contract_ids_from_edges.add(tgt)
        script_contract_names.setdefault(src, []).append(tgt)
    contract_id_to_name = {}
    if contract_ids_from_edges:
        for c in session.execute(select(Contract.id, Contract.name).where(Contract.id.in_(contract_ids_from_edges))).all():
            contract_id_to_name[c[0]] = c[1]
    for sid in script_contract_names:
        script_contract_names[sid] = [contract_id_to_name.get(cid, "") for cid in script_contract_names[sid] if cid in contract_id_to_name]

    # Script memory by script id; when memory_phase exists, prefer migration-phase in large-change mode
    script_mem_query = select(MemoryRecord.scope_id, MemoryRecord.content).where(
        MemoryRecord.scope_level == MemoryScope.script,
        MemoryRecord.parent_scope_id.in_(parent_scopes),
        MemoryRecord.invalidated_by.is_(None),
    )
    if large and hasattr(MemoryRecord, "memory_phase"):
        script_mem_query = select(
            MemoryRecord.scope_id, MemoryRecord.content, MemoryRecord.memory_phase
        ).where(
            MemoryRecord.scope_level == MemoryScope.script,
            MemoryRecord.parent_scope_id.in_(parent_scopes),
            MemoryRecord.invalidated_by.is_(None),
        )
    script_mems = session.execute(script_mem_query).all()
    mem_by_script: dict[int, str] = {}
    for row in script_mems:
        scope_id, content = row[0], row[1]
        phase = row[2] if len(row) > 2 else None
        try:
            sid = int(scope_id.replace("script:", ""))
            existing = mem_by_script.get(sid)
            if existing is None or (large and phase == MemoryPhase.migration):
                mem_by_script[sid] = content or ""
        except ValueError:
            pass

    if impact_script_ids:
        valid_scripts = session.execute(select(Script).where(Script.id.in_(impact_script_ids))).scalars().all()
    elif domain_ids:
        valid_scripts = session.execute(select(Script).where(Script.domain_id.in_(domain_ids))).scalars().all()
    else:
        # No domain filter — fall back to all scripts in the repo
        valid_scripts = session.execute(select(Script).where(Script.repo_id == task.repo_id)).scalars().all()
    s_docs: list[dict[str, Any]] = []
    for s in valid_scripts:
        name = (s.instance_path or "").split(".")[-1] or (s.file_path or "").split("/")[-1].split("\\")[-1]
        domain_name, domain_kind = domain_by_id.get(s.domain_id, ("", ""))
        contract_names = " ".join(script_contract_names.get(s.id, []))
        memory_snippet = mem_by_script.get(s.id, "")
        parts = [
            s.instance_path or "",
            name,
            s.summary or "",
            " ".join(s.exports or []),
            contract_names,
            domain_name,
            getattr(domain_kind, "value", str(domain_kind)) if domain_kind else "",
            memory_snippet,
        ]
        text = " ".join(p for p in parts if p)
        s_docs.append({"id": s.id, "type": "script", "text": text, "updated_at": s.updated_at})

    # BM25 score scripts
    script_texts = [d["text"] for d in s_docs]
    script_scores = _bm25_scores(script_texts, query_text)
    # When task says "server side" or "only server", boost server-domain scripts so they get full scope
    desc_lower = (task.description or "").lower()
    if "server side" in desc_lower or "only server" in desc_lower:
        for i, s in enumerate(valid_scripts):
            if i < len(script_scores):
                domain_name, _ = domain_by_id.get(s.domain_id, ("", ""))
                if domain_name and "server" in domain_name.lower():
                    script_scores[i] = script_scores[i] + 3.0
    min_relevance = 0.1
    max_script_score = max(script_scores) if script_scores else 0
    # Broader threshold when task is about replication/stats so more scripts pass
    is_broad_task = _content_patterns_for_task(task.description or "") is not None
    threshold = max(0.08 if is_broad_task else 0.1, max_script_score * (0.15 if is_broad_task else 0.2))
    scored_scripts = [(script_scores[i], s_docs[i]) for i in range(len(s_docs)) if script_scores[i] >= threshold]
    scored_scripts.sort(key=lambda x: x[0], reverse=True)

    # Seed set: top 5–20 by score spread; when "server side" or broad (replication/stats) take more
    seed_cap = 20 if ("server side" in desc_lower or "only server" in desc_lower) else 12
    if is_broad_task:
        seed_cap = max(seed_cap, 30)
    best_score = scored_scripts[0][0] if scored_scripts else 0
    seed_cutoff = best_score * 0.4
    seed_list = [item for score, item in scored_scripts if score >= seed_cutoff][:seed_cap]
    if len(seed_list) < 5 and scored_scripts:
        seed_list = [scored_scripts[i][1] for i in range(min(5, len(scored_scripts)))]
    seed_ids = {s["id"] for s in seed_list}
    seed_score_by_id = {s["id"]: next((sc for sc, it in scored_scripts if it["id"] == s["id"]), 0.0) for s in seed_list}

    # One-hop (or two-hop in large-change) expansion: requires -> scripts; provides/consumes -> contracts
    expansion_edge_kinds = ["requires", "provides_contract", "consumes_contract"]
    decay = 0.7
    expanded_script_scores: dict[int, float] = {}
    contract_ids_from_seeds: set[int] = set()
    frontier: set[int] = set(seed_ids)
    hop_scores: dict[int, float] = {sid: seed_score_by_id.get(sid, 0) for sid in seed_ids}
    for hop in range(max_hops):
        next_frontier: set[int] = set()
        for sid in frontier:
            for edge_kind in expansion_edge_kinds:
                for e in search_graph(sid, "script", edge_kind, "outgoing"):
                    tid = e.get("target_id")
                    ttype = e.get("target_type", "")
                    if ttype == "script" and edge_kind == "requires" and tid:
                        if tid not in seed_ids:
                            base = hop_scores.get(sid, 0)
                            new_score = (decay ** (hop + 1)) * base
                            expanded_script_scores[tid] = max(expanded_script_scores.get(tid, 0), new_score)
                            next_frontier.add(tid)
                    elif ttype == "contract" and tid:
                        contract_ids_from_seeds.add(tid)
        if not next_frontier:
            break
        frontier = next_frontier
        hop_scores = expanded_script_scores

    # Re-rank: seeds first (by score), then expanded (by decayed score), then by freshness
    def _ts(item: dict) -> float:
        u = item.get("updated_at")
        return u.timestamp() if u and hasattr(u, "timestamp") else 0.0

    def script_sort_key(item: dict) -> tuple:
        iid = item["id"]
        if iid in seed_ids:
            return (0, -seed_score_by_id.get(iid, 0), -_ts(item))
        return (1, -expanded_script_scores.get(iid, 0), -_ts(item))

    combined_script_items = list(seed_list)
    for sid, esc in expanded_script_scores.items():
        if sid in seed_ids:
            continue
        match = next((d for d in s_docs if d["id"] == sid), None)
        if match:
            combined_script_items.append(match)
    combined_script_items.sort(key=script_sort_key)
    final_script_ids = [d["id"] for d in combined_script_items]

    # Pattern-based expansion: include every script that references the affected patterns (entire affected code)
    content_patterns = _content_patterns_for_task(task.description or "")
    if repo_root and content_patterns:
        pattern_ids = _script_ids_matching_content(repo_root, valid_scripts, content_patterns)
        if pattern_ids:
            seen = set(final_script_ids)
            for sid in pattern_ids:
                if sid not in seen:
                    final_script_ids.append(sid)
                    seen.add(sid)

    # Optional cap for very large revamps (0 = no limit)
    max_scripts = getattr(settings, "max_scripts_per_investigation", 0)
    if max_scripts > 0 and len(final_script_ids) > max_scripts:
        final_script_ids = final_script_ids[:max_scripts]

    # Contracts: enriched text, BM25, adaptive policy, plus graph-referenced
    if impact_contract_ids:
        valid_contracts = session.execute(select(Contract).where(Contract.id.in_(impact_contract_ids))).scalars().all()
    else:
        valid_contracts = session.execute(select(Contract)).scalars().all()
    c_docs = [{"id": c.id, "text": f"{c.name} {c.kind} {c.summary or ''}"} for c in valid_contracts]
    contract_texts = [d["text"] for d in c_docs]
    contract_scores = _bm25_scores(contract_texts, query_text)
    best_contract = max(contract_scores) if contract_scores else 0
    strong_threshold = max(0.1, best_contract * 0.2)
    above_threshold = [(contract_scores[i], c_docs[i]["id"]) for i in range(len(c_docs)) if contract_scores[i] >= strong_threshold]
    above_threshold.sort(key=lambda x: x[0], reverse=True)
    if len(above_threshold) <= 5:
        final_contract_ids = [cid for _, cid in above_threshold]
    else:
        drop_off = 0.85
        final_contract_ids = [cid for score, cid in above_threshold if score >= best_contract * drop_off]
    final_contract_ids = list(dict.fromkeys(final_contract_ids + list(contract_ids_from_seeds)))

    return final_script_ids, final_contract_ids


def _ai_review_scripts(
    task: Task,
    prefilter_script_ids: list[int],
    prefilter_contract_ids: list[int],
    selected_domains: list[SelectedDomain],
    session: Session,
    repo: Repository,
    console,
    *,
    verbose: bool = False,
) -> tuple[list[SelectedScript], list[SelectedContract]]:
    """
    Phase 2b: AI Investigator Review of script/contract candidates.
    Receives the BM25+graph-expansion output and can explain, request neighbors,
    flag missing scripts, and drop irrelevant ones. Falls back to prefilter.
    """
    from app.models.entities import Script, Contract

    # Build candidate info (summaries only — no source code)
    script_info = []
    for sid in prefilter_script_ids[:50]:  # Cap prompt size
        s = session.get(Script, sid)
        if s:
            script_info.append({
                "id": sid,
                "instance_path": s.instance_path or s.file_path,
                "script_type": s.script_type,
                "summary": s.summary or "(no summary)",
            })

    domain_context = [{"name": sd.domain_name, "reason": sd.provenance.selection_reason} for sd in selected_domains]
    task_class = task.task_class.value if task.task_class else "semantic_localized"

    prompt = f"""\
You are a script-investigator for a Roblox/Luau project.
Your job is to review script candidates selected by BM25 + graph expansion.
You do NOT read source code — work from summaries and metadata only.

# Task
{task.description}

## Task Class: {task_class}
## Target Scope: {task.target_scope or "(auto)"}
## Runtime Side: {task.runtime_side or "unknown"}

## Selected Domains
{json.dumps(domain_context, indent=2)}

## Script Candidates (top {len(script_info)})
{json.dumps(script_info, indent=2)}

## Instructions
- For each candidate, decide keep or drop with a 1-line reason.
- You may request up to {settings.max_ai_neighbor_requests} neighbor scripts by providing the ID of a candidate and the edge kind ("requires", "provides_contract", "consumes_contract").
- You may flag up to 3 script names/paths you expect to exist but are missing from candidates.
- Return ONLY a JSON object:
```json
{{
    "kept": [
        {{"id": 1, "reason": "owns DataSaveManager", "confidence": 0.9}}
    ],
    "dropped": [
        {{"id": 5, "reason": "unrelated UI module"}}
    ],
    "requested_neighbors": [
        {{"source_id": 1, "edge_kind": "requires"}}
    ],
    "flagged_missing": ["DataRetryHandler", "SaveQueue"]
}}
```
"""
    if not verbose:
        console.print(f"[dim]  Phase 2b: AI reviewing {len(script_info)} script candidates...[/dim]")

    gemini_cwd = str(settings.gemini_cli_cwd) if getattr(settings, "gemini_cli_cwd", None) else repo.root_path
    result = invoke_standalone(prompt, timeout=settings.triage_ai_timeout_secs, cwd=gemini_cwd, no_mcp=True)

    if verbose:
        _verbose_phase_io(console, "Script Investigator (Phase 2b)", prompt[:3000], result)

    # Build fallback with deterministic provenance
    all_scripts = {s.id: s for s in session.execute(select(Script).where(Script.repo_id == repo.id)).scalars().all()}
    fallback_scripts = [
        SelectedScript(
            script_id=sid,
            instance_path=all_scripts[sid].instance_path or all_scripts[sid].file_path if sid in all_scripts else f"script:{sid}",
            provenance=SelectionProvenance(
                selected_by="bm25+graph",
                selection_reason="Deterministic prefilter (BM25 + graph expansion)",
                confidence=0.7,
            ),
        )
        for sid in prefilter_script_ids
        if sid in all_scripts
    ]
    all_contracts = {c.id: c for c in session.execute(select(Contract)).scalars().all()}
    fallback_contracts = [
        SelectedContract(
            contract_id=cid,
            contract_name=all_contracts[cid].name if cid in all_contracts else f"contract:{cid}",
            provenance=SelectionProvenance(
                selected_by="bm25+graph",
                selection_reason="Deterministic prefilter",
                confidence=0.6,
            ),
        )
        for cid in prefilter_contract_ids
        if cid in all_contracts
    ]

    if result.exit_code != 0:
        console.print(f"[dim]  Phase 2b: AI review failed (exit {result.exit_code}), using prefilter.[/dim]")
        return fallback_scripts, fallback_contracts

    parsed = _try_parse_json(result.stdout.strip() if result.stdout else "")
    if not parsed or "kept" not in parsed:
        console.print(f"[dim]  Phase 2b: AI response unparseable, using prefilter.[/dim]")
        return fallback_scripts, fallback_contracts

    # Process kept scripts
    kept_ids = set()
    selected_scripts: list[SelectedScript] = []
    for entry in parsed.get("kept", []):
        sid = entry.get("id")
        if sid not in all_scripts:
            continue
        kept_ids.add(sid)
        selected_scripts.append(SelectedScript(
            script_id=sid,
            instance_path=all_scripts[sid].instance_path or all_scripts[sid].file_path,
            provenance=SelectionProvenance(
                selected_by="bm25+ai_confirmed",
                selection_reason=entry.get("reason", "AI confirmed"),
                confidence=entry.get("confidence", 0.8),
            ),
        ))

    # Process neighbor requests (bounded)
    neighbor_count = 0
    for req in parsed.get("requested_neighbors", [])[:settings.max_ai_neighbor_requests]:
        source_id = req.get("source_id")
        edge_kind = req.get("edge_kind", "requires")
        if not source_id:
            continue
        edges = search_graph(source_id, "script", edge_kind, "outgoing")
        for e in edges:
            tid = e.get("target_id")
            ttype = e.get("target_type", "")
            if ttype == "script" and tid in all_scripts and tid not in kept_ids:
                kept_ids.add(tid)
                neighbor_count += 1
                selected_scripts.append(SelectedScript(
                    script_id=tid,
                    instance_path=all_scripts[tid].instance_path or all_scripts[tid].file_path,
                    provenance=SelectionProvenance(
                        selected_by="ai_neighbor_request",
                        selection_reason=f"Neighbor of script:{source_id} via {edge_kind}",
                        evidence_refs=[f"edge:{source_id}->{tid}:{edge_kind}"],
                        confidence=0.6,
                    ),
                ))

    # Process flagged-missing scripts (name/path lookup)
    missing_count = 0
    for name in parsed.get("flagged_missing", [])[:3]:
        matches = session.execute(
            select(Script).where(
                Script.repo_id == repo.id,
                Script.instance_path.contains(str(name)),
            )
        ).scalars().all()
        for s in matches[:2]:
            if s.id not in kept_ids:
                kept_ids.add(s.id)
                missing_count += 1
                selected_scripts.append(SelectedScript(
                    script_id=s.id,
                    instance_path=s.instance_path or s.file_path,
                    provenance=SelectionProvenance(
                        selected_by="ai_investigator",
                        selection_reason=f"AI flagged missing: '{name}'",
                        confidence=0.65,
                    ),
                ))

    if not selected_scripts:
        return fallback_scripts, fallback_contracts

    n_dropped = len(parsed.get("dropped", []))
    console.print(
        f"[cyan]Phase 2b: AI Review[/] -> Kept {len(selected_scripts)} scripts"
        f"{f', +{neighbor_count} neighbors' if neighbor_count else ''}"
        f"{f', +{missing_count} flagged' if missing_count else ''}"
        f"{f', dropped {n_dropped}' if n_dropped else ''}."
    )
    return selected_scripts, fallback_contracts


def _try_parse_json(text: str) -> dict | None:
    """Attempt to parse a JSON block out of the agent output."""
    import json
    import re
    # Try finding markdown code blocks
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        raw = match.group(1)
    else:
        # Fallback to the first brace to the last brace
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = text[start:end+1]
        else:
            raw = text
            
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _verbose_phase_io(
    console,
    phase_name: str,
    input_text: str,
    result: Any,
    *,
    max_input: int = 4000,
    max_output: int = 3000,
) -> None:
    """Print phase input and subagent output when verbose mode is on."""
    sep = "─" * 60
    console.print(f"\n[bold cyan]{sep}[/]")
    console.print(f"[bold]  Phase: {phase_name}[/]")
    console.print(f"[bold cyan]{sep}[/]\n")
    in_preview = input_text if len(input_text) <= max_input else input_text[:max_input] + "\n... [truncated]"
    console.print("[dim]INPUT:[/dim]")
    console.print(in_preview)
    console.print()
    out_text = getattr(result, "patch_content", None) or getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", None) or ""
    if stderr:
        out_text += "\n[stderr]\n" + stderr
    out_preview = out_text if len(out_text) <= max_output else out_text[:max_output] + "\n... [truncated]"
    console.print("[dim]OUTPUT (exit_code=%s):[/dim]" % getattr(result, "exit_code", "?"))
    console.print(out_preview)
    console.print()


def _investigate_docs(task: Task, console, *, verbose: bool = False, repo: Repository | None = None) -> list[InvariantEntry]:
    """
    Phase 3: Docs Retrieval.
    If the task involves potentially unfamiliar or complex Roblox APIs,
    agent queries the mcp-roblox-docs MCP server for context.
    Returns list of InvariantEntry with phase=docs for merge.
    """
    from app.services.workers.lifecycle import invoke_subagent

    sem = _get_concurrency_semaphore()
    sem.acquire()
    try:
        phase_timeout = getattr(settings, "investigation_phase_timeout_secs", 300)
        context = f"# Task\n{task.description}\n"
        if not verbose:
            console.print(f"[dim]  _investigate_docs: querying docs-investigator for API references...[/dim]")
        cwd = str(settings.gemini_cli_cwd) if getattr(settings, "gemini_cli_cwd", None) else (repo.root_path if repo else None)
        result = invoke_subagent("docs-investigator", context, timeout=phase_timeout, cwd=cwd)
        if verbose:
            _verbose_phase_io(console, "Docs investigator", context, result)
        if result.exit_code == 0 and result.patch_content:
            parsed = _try_parse_json(result.patch_content)
            if parsed and parsed.get("needs_docs") and parsed.get("retrieved_docs"):
                docs = parsed.get("retrieved_docs", [])
                console.print(f"[cyan]Phase 3: Docs Investigator[/cyan] -> Retrieved {len(docs)} snippets.")
                for d in docs:
                    console.print(f"  [dim]• {d[:100]}...[/dim]")
                prov = InvestigationProvenance(phase="docs", chunk_id=None, script_ids=[])
                return [InvariantEntry(text=f"[ROBLOX DOCS] {d}", provenance=prov) for d in docs]
            console.print(f"[cyan]Phase 3: Docs Investigator[/cyan] -> No API documentation was required for this task.")
        elif result.exit_code != 0:
            import sys
            print(f"  [docs-investigator] failed: {result.stderr[:100]}", file=sys.stderr)
        return []
    finally:
        sem.release()


def _deep_read_chunk(
    task: Task,
    repo: Repository,
    script_ids: list[int],
    console,
    chunk_id: int | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run domain-investigator on a subset of scripts. Returns agent-only fields
    with InvariantEntry/RiskEntry (provenance phase=deep_read, chunk_id, script_ids).
    Uses global concurrency semaphore for each source read and for the agent call.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services.agents.tools import read_script_source

    sem = _get_concurrency_semaphore()
    phase_timeout = getattr(settings, "investigation_phase_timeout_secs", 300)
    file_bodies: dict[str, str] = {}
    sources: list[str] = []

    def _read_one(sid: int) -> tuple[int, dict]:
        sem.acquire()
        try:
            return sid, read_script_source(sid, repo_root=repo.root_path)
        finally:
            sem.release()

    # Per-script cap for the prompt only (keep full source in file_bodies for the edit worker)
    _max_source_chars = 8000

    with ThreadPoolExecutor(max_workers=min(len(script_ids), 8)) as pool:
        results = list(pool.map(_read_one, script_ids))
    for sid, script_data in results:
        if "error" not in script_data:
            full_source = script_data["source"]
            file_bodies[script_data["file_path"]] = full_source
            prompt_source = full_source if len(full_source) <= _max_source_chars else full_source[:_max_source_chars] + "\n... (truncated for analysis)"
            sources.append(f"### [id:{sid}] {script_data['file_path']}\n```lua\n{prompt_source}\n```")

    sem.acquire()
    try:
        prompt = f"""\
You are a domain-investigator for a Roblox/Luau project.

# Task
{task.description}

## Source Code
The following scripts have been identified as highly relevant to this task:

{chr(10).join(sources)}

## Output Format
Analyze the code and extract exact constraints the edit worker must follow.
Return a JSON object:
```json
{{
    "invariants": ["Must check player.UserId > 0", "Dash cooldown must be >= 0.5s"],
    "risks": ["Save method has no retry logic", "Client handles physics locally"],
    "uncertainties": ["Not sure if 'DashEvent' remote exists in Studio"],
    "agent_notes": "brief summary of how the code works"
}}
```
Return ONLY the JSON.
"""
        gemini_cwd = str(settings.gemini_cli_cwd) if getattr(settings, "gemini_cli_cwd", None) else repo.root_path
        result = invoke_standalone(prompt, timeout=phase_timeout, cwd=gemini_cwd, no_mcp=True)
        if verbose:
            _verbose_phase_io(console, "Domain investigator (chunk %s)" % (chunk_id or 0), prompt[:4000], result)
    finally:
        sem.release()

    prov = InvestigationProvenance(phase="deep_read", chunk_id=chunk_id, script_ids=list(script_ids))
    out: dict[str, Any] = {
        "invariant_entries": [],
        "risk_entries": [],
        "uncertainties": [],
        "file_bodies": file_bodies,
        "agent_notes": "",
    }
    if result.exit_code == 0:
        parsed = _try_parse_json(result.stdout.strip())
        if parsed:
            out["invariant_entries"] = [InvariantEntry(text=t, provenance=prov) for t in parsed.get("invariants", [])]
            out["risk_entries"] = [RiskEntry(text=t, provenance=prov) for t in parsed.get("risks", [])]
            out["uncertainties"] = parsed.get("uncertainties", [])
            out["agent_notes"] = parsed.get("agent_notes", "")
    return out


def _deep_read_scripts(
    task: Task,
    repo: Repository,
    relevant_script_ids: list[int],
    console,
    investigation_workers_override: int | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Phase 4: Deep Read (Agent).
    Builds InvariantEntry/RiskEntry from skills and memory, then from agent chunk(s).
    Merges with schema-driven dedupe/conflict resolution. Returns merged entries
    plus new_inv_entries/new_risk_entries (deep_read only) for persistence.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services.agents.tools import read_memory
    from app.services.memory.skill_loader import get_relevant_skills

    prov_skills = InvestigationProvenance(phase="skills", chunk_id=None, script_ids=[])
    prov_memory = InvestigationProvenance(phase="memory", chunk_id=None, script_ids=[])
    inv_entries: list[InvariantEntry] = []
    risk_entries: list[RiskEntry] = []

    # 1. Skills as entries
    skill_rules = get_relevant_skills(
        runtime_side=task.runtime_side or "",
        target_scope=task.target_scope or "",
    )
    inv_entries.extend(InvariantEntry(text=s, provenance=prov_skills) for s in skill_rules)
    if skill_rules:
        console.print(f"[dim]  _deep_read_scripts: Loaded {len(skill_rules)} relevant global skills.[/dim]")

    # 2. Script-level memories as entries
    for sid in relevant_script_ids:
        mems = read_memory(f"script:{sid}")
        for m in mems:
            p = InvestigationProvenance(phase="memory", chunk_id=None, script_ids=[sid])
            if m["memory_type"] == "procedural":
                inv_entries.append(InvariantEntry(text=m["content"], provenance=p))
            elif m["memory_type"] == "episodic":
                risk_entries.append(RiskEntry(text=m["content"], provenance=p))

    max_per_chunk = getattr(settings, "max_scripts_per_deep_read_chunk", 25)
    max_parallel = getattr(settings, "max_deep_read_parallel_chunks", 8)
    n_scripts = len(relevant_script_ids)
    # Chunk by script count so each chunk stays under max_per_chunk (revamp = many files)
    n_chunks = max(1, (n_scripts + max_per_chunk - 1) // max_per_chunk)
    n_chunks = min(n_chunks, n_scripts)
    use_chunked = n_chunks > 1

    if not use_chunked:
        if not verbose:
            console.print(f"[dim]  _deep_read_scripts: domain-investigator reading {len(relevant_script_ids)} source files...[/dim]")
        chunk_result = _deep_read_chunk(task, repo, relevant_script_ids, console, chunk_id=0, verbose=verbose)
        inv_entries.extend(chunk_result["invariant_entries"])
        risk_entries.extend(chunk_result["risk_entries"])
        merged_inv = merge_invariant_entries(inv_entries)
        merged_risk = merge_risk_entries(risk_entries)
        new_inv_entries = [e for e in chunk_result["invariant_entries"]]
        new_risk_entries = [e for e in chunk_result["risk_entries"]]
        if new_inv_entries or new_risk_entries or chunk_result["uncertainties"]:
            console.print(
                f"[cyan]Phase 4: Deep Read[/] -> Extracted {len(new_inv_entries)} invariants, "
                f"{len(new_risk_entries)} risks, {len(chunk_result['uncertainties'])} uncertainties."
            )
        return {
            "invariant_entries": merged_inv,
            "risk_entries": merged_risk,
            "uncertainties": chunk_result["uncertainties"],
            "file_bodies": chunk_result["file_bodies"],
            "agent_notes": chunk_result["agent_notes"],
            "new_inv_entries": new_inv_entries,
            "new_risk_entries": new_risk_entries,
        }

    # Chunked: run per chunk in parallel, then merge (chunk_size capped for prompt size)
    chunk_size = min(max_per_chunk, (len(relevant_script_ids) + n_chunks - 1) // n_chunks)
    chunk_size = max(1, chunk_size)
    chunks = [
        relevant_script_ids[i : i + chunk_size]
        for i in range(0, len(relevant_script_ids), chunk_size)
    ]
    n_workers = min(len(chunks), max_parallel)
    if not verbose:
        console.print(f"[dim]  _deep_read_scripts: domain-investigator reading {len(relevant_script_ids)} scripts in {len(chunks)} parallel chunks...[/dim]")

    all_inv: list[InvariantEntry] = list(inv_entries)
    all_risk: list[RiskEntry] = list(risk_entries)
    merged_uncertainties: list[str] = []
    merged_bodies: dict[str, str] = {}
    merged_notes: list[str] = []
    new_inv_entries = []
    new_risk_entries = []

    def _run_chunk(cids: list[int], idx: int):
        return _deep_read_chunk(task, repo, cids, console, chunk_id=idx, verbose=verbose)

    phase_timeout = getattr(settings, "investigation_phase_timeout_secs", 300)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [
            pool.submit(_run_chunk, chunk_ids, i)
            for i, chunk_ids in enumerate(chunks)
        ]
        for future in futures:
            try:
                chunk_result = future.result(timeout=phase_timeout)
            except Exception:
                continue
            all_inv.extend(chunk_result["invariant_entries"])
            all_risk.extend(chunk_result["risk_entries"])
            merged_uncertainties.extend(chunk_result["uncertainties"])
            merged_bodies.update(chunk_result["file_bodies"])
            if chunk_result["agent_notes"]:
                merged_notes.append(chunk_result["agent_notes"])
            new_inv_entries.extend(chunk_result["invariant_entries"])
            new_risk_entries.extend(chunk_result["risk_entries"])

    merged_inv = merge_invariant_entries(all_inv)
    merged_risk = merge_risk_entries(all_risk)
    console.print(
        f"[cyan]Phase 4: Deep Read[/] -> Extracted {len(new_inv_entries)} invariants, "
        f"{len(new_risk_entries)} risks, {len(merged_uncertainties)} uncertainties (from {len(chunks)} chunks)."
    )
    return {
        "invariant_entries": merged_inv,
        "risk_entries": merged_risk,
        "uncertainties": merged_uncertainties,
        "file_bodies": merged_bodies,
        "agent_notes": "\n\n".join(merged_notes),
        "new_inv_entries": new_inv_entries,
        "new_risk_entries": new_risk_entries,
    }


def _persist_deep_read_memory(
    session: Session,
    new_inv_entries: list[InvariantEntry],
    new_risk_entries: list[RiskEntry],
    primary_script_id: int | None,
) -> None:
    """Persist new invariants and risks from deep-read phase as MemoryRecords with provenance in source_refs_json."""
    if not primary_script_id:
        return
    from app.models.entities import MemoryType, MemoryScope

    for e in new_inv_entries:
        scope_id = f"script:{primary_script_id}"
        if e.provenance.script_ids:
            scope_id = f"script:{e.provenance.script_ids[0]}"
        provenance_json = json.dumps({
            "phase": e.provenance.phase,
            "chunk_id": e.provenance.chunk_id,
            "script_ids": e.provenance.script_ids,
        })
        session.add(MemoryRecord(
            scope_id=scope_id,
            scope_level=MemoryScope.script,
            memory_type=MemoryType.procedural,
            content=e.text,
            source_refs_json=provenance_json,
        ))
    for e in new_risk_entries:
        scope_id = f"script:{primary_script_id}"
        if e.provenance.script_ids:
            scope_id = f"script:{e.provenance.script_ids[0]}"
        provenance_json = json.dumps({
            "phase": e.provenance.phase,
            "chunk_id": e.provenance.chunk_id,
            "script_ids": e.provenance.script_ids,
        })
        session.add(MemoryRecord(
            scope_id=scope_id,
            scope_level=MemoryScope.script,
            memory_type=MemoryType.episodic,
            content=e.text,
            source_refs_json=provenance_json,
        ))
    session.commit()


def _validate_environment(task: Task, report: InvestigationReport, session: Session, console):
    """
    Phase 5: Environment Validation (Code-based).
    Resolves uncertainties by searching the ingested source code.
    """
    if not report.uncertainties:
        console.print(f"[cyan]Phase 5: Environment Validation[/] -> No uncertainties to resolve.")
        return

    # Build source context from file bodies we already have
    source_snippets = []
    for fp, body in report.file_bodies.items():
        if body:
            source_snippets.append(f"### {fp}\n```luau\n{body[:3000]}\n```")

    source_context = "\n\n".join(source_snippets[:15])  # Cap to avoid overwhelming

    prompt = f"""\
You are a code-validator for a Roblox/Luau project.

# Uncertainties flagged by prior investigation:
{chr(10).join('- ' + u for u in report.uncertainties)}

## Source Code Available
{source_context}

## Action
Review the source code above to resolve each uncertainty.
Search for evidence in the code — look at function calls, variable references,
RemoteEvent/RemoteFunction usage, module requires, and data flow.

## Output Format
Return a JSON object:
```json
{{
    "facts": ["TeleportData includes lastPosition based on line X in AFK.server.luau"],
    "unresolved": ["Could not find evidence for Y in available code"]
}}
```
Return ONLY the JSON.
"""
    console.print(f"[dim]  _validate_environment: resolving {len(report.uncertainties)} uncertainties from source code...[/dim]")

    from app.models.entities import Repository
    repo = session.get(Repository, task.repo_id)
    gemini_cwd = repo.root_path if repo else (str(settings.gemini_cli_cwd) if getattr(settings, "gemini_cli_cwd", None) else None)

    result = invoke_standalone(
        prompt,
        timeout=45,
        cwd=gemini_cwd,
        no_mcp=True,
    )

    if result.exit_code == 0:
        parsed = _try_parse_json(result.stdout.strip())
        if parsed:
            facts = parsed.get("facts", [])
            unresolved = parsed.get("unresolved", [])
            
            console.print(f"[cyan]Phase 5: Environment Validation[/] -> {len(facts)} facts confirmed, {len(unresolved)} unresolved.")
            if facts:
                for f in facts[:3]:
                    console.print(f"    [dim]• [green]✓[/green] {f}[/dim]")
            if unresolved:
                for u in unresolved[:3]:
                    console.print(f"    [dim]• [red]?[/red] {u}[/dim]")
            
            report.invariants.extend([f"[ENV FACT] {f}" for f in facts])
            
            # Persist as environment memory
            from app.models.entities import MemoryType, MemoryScope
            for f in facts:
                session.add(MemoryRecord(
                    scope_id="environment:global",
                    scope_level=MemoryScope.environment,
                    memory_type=MemoryType.environment,
                    content=f
                ))
            session.commit()
    else:
        console.print(f"  [red][code-validator] failed.[/red]", style="dim")

def assemble_from_report(
    task: Task,
    report: InvestigationReport,
    console,
    *,
    verbose: bool = False,
    repo: Repository | None = None,
) -> ContextPacketSchema:
    """
    Phase 6: Synthesis (Packet Assembly).
    Agents fuse the extracted invariants/docs/facts into a minimal packet.
    """
    from app.services.packets.assembler import _estimate_tokens
    from app.services.workers.lifecycle import invoke_subagent

    session = get_session()
    try:
        from app.models.entities import Script
        relevant_scripts = []
        for sid in report.relevant_script_ids:
            s = session.get(Script, sid)
            if s:
                relevant_scripts.append({
                    "instance_path": s.instance_path,
                    "file_path": s.file_path,
                    "script_type": s.script_type,
                    "summary": s.summary or "(no summary)",
                })

        # Ask the packet-assembler agent to synthesize invariants/docs/risks
        context = f"""
# Task
{task.description}

## Triaged Invariants (Docs, Skills, Script rules)
{chr(10).join('- ' + i for i in report.invariants)}

## Known Risks
{chr(10).join('- ' + r for r in report.risks)}
"""
        if not verbose:
            console.print(f"[dim]  assemble_from_report: synthesizing {len(report.invariants)} constraints and {len(report.risks)} risks into final guidelines...[/dim]")
        cwd = str(settings.gemini_cli_cwd) if getattr(settings, "gemini_cli_cwd", None) else (repo.root_path if repo else None)
        result = invoke_subagent("packet-assembler", context, timeout=300, cwd=cwd)
        if verbose:
            _verbose_phase_io(console, "Packet assembler", context, result)

        # Truncate JSON parsing here: packet assembler returns markdown, not JSON, so we just attach its output
        # to the description block or keep invariants separated
        synthesized_text = result.patch_content or result.stdout

        # Include every triaged script; share token budget across all file bodies (no hard drop)
        from app.services.packets.assembler import truncate_to_tokens
        budget = settings.default_token_budget
        items = list(report.file_bodies.items())
        total_tokens = sum(_estimate_tokens(b) for _, b in items)
        if total_tokens <= budget:
            trimmed_bodies = dict(items)
        else:
            n = max(1, len(items))
            per_file = max(400, budget // n)
            trimmed_bodies = {}
            for fp, body in items:
                tokens = _estimate_tokens(body)
                trimmed_bodies[fp] = body if tokens <= per_file else truncate_to_tokens(body, per_file)

        # If the packet assembler threw errors, just fallback to raw lists
        # but apply a simplicity filter to remove over-engineered suggestions
        if result.exit_code != 0:
            console.print(f"  [red][packet-assembler] failed, falling back to raw lists.[/red]", style="dim")
            _OVERENGINEERING_SIGNALS = [
                "_G.", "getfenv", "shared.", "rawget",
                "ReplicatedStorage:FindFirstChild",
                "pcall(require",
            ]
            filtered = []
            for inv in report.invariants:
                # Skip invariants that suggest adding lookups for undefined things
                if any(sig in inv for sig in _OVERENGINEERING_SIGNALS):
                    continue
                filtered.append(inv)
            # Prepend a simplicity reminder
            filtered.insert(0, "[SIMPLICITY] Prefer removing broken references over adding guards. Use existing local constants.")
            final_invariants = filtered
            final_risks = report.risks
        else:
            console.print(f"[cyan]Phase 6: Synthesis[/] -> Compiled constraints into final packet.")
            # We treat the synthesized markdown text as a single "mega-invariant" for the worker to read
            final_invariants = [f"[SYNTHESIZED CONTEXT]\n{synthesized_text}"]
            final_risks = report.risks # Keep raw risks just in case

        # Build provenance summary for the packet
        prov_summary = []
        for sd in report.selected_domains:
            prov_summary.append({"type": "domain", "name": sd.domain_name, **sd.provenance.model_dump()})
        for ss in report.selected_scripts:
            prov_summary.append({"type": "script", "path": ss.instance_path, **ss.provenance.model_dump()})
        for sc in report.selected_contracts:
            prov_summary.append({"type": "contract", "name": sc.contract_name, **sc.provenance.model_dump()})

        return ContextPacketSchema(
            task_id=task.id,
            objective=task.description,
            target_scope=task.target_scope or "",
            runtime_side=task.runtime_side or "unknown",
            task_class=task.task_class.value if task.task_class else "",
            relevant_scripts=relevant_scripts,
            relevant_contracts=report.contracts,
            local_invariants=final_invariants,
            known_risks=final_risks,
            uncertainties=report.uncertainties,
            file_bodies=trimmed_bodies,
            token_budget=settings.default_token_budget,
            migration_brief=report.migration_brief,
            selection_provenance=prov_summary,
        )
    finally:
        session.close()


def run_investigation(
    task_id: int,
    investigation_workers: int | None = None,
    verbose: bool = False,
) -> tuple[ContextPacketSchema, InvestigationReport]:
    """
    Full hybrid hierarchical orchestration loop:
      0. Classify task
      1a. Deterministic Domain Triage (BM25 over MemoryRecords)
      1b. AI Domain Investigator Review
      2a. Deterministic Script/Contract Triage (BM25 + graph expansion)
      2b. AI Script Investigator Review
      3. Docs retrieval (Optional Agent via MCP)
      4. Deep Read (Agent extracts constraints, updates memory)
      5. Environment Validation (Optional Agent via MCP)
      6. Assemble Packet

    investigation_workers: Optional override for parallel workers (Phase 3+4 and chunked Phase 4).
    verbose: If True, print each phase's input and subagent output (no spinner) so you can see exact context and results.
    """
    session = get_session()
    try:
        task = session.get(Task, task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        repo = session.get(Repository, task.repo_id)
        if not repo:
            raise ValueError(f"Repository {task.repo_id} not found")

        task.status = TaskStatus.in_progress

        # Phase 0: Task Classification
        task.task_class = classify_task(task)
        session.commit()

        # Initialize the progressive report
        report = InvestigationReport(task_id=task.id)

        from rich.console import Console
        from rich.tree import Tree
        from rich import box
        from rich.panel import Panel
        console = Console()
        
        console.print("\n[dim]-- Hybrid Investigation Pipeline --[/dim]")
        console.print(f"[dim]  Task class: [bold]{task.task_class.value}[/bold][/dim]")

        import time as _time
        _pipeline_start = _time.monotonic()

        # Phase 1a: Deterministic Domain Prefilter
        _t0 = _time.monotonic()
        domain_ids = _triage_domains(task, repo, session)
        # Build scored list for AI review (reconstruct scores from the triage)
        scored_domains = [(1.0, did) for did in domain_ids]  # Default score; _triage_domains already sorted
        _elapsed = _time.monotonic() - _t0
        if domain_ids:
            from app.models.entities import Domain
            d_names = session.execute(select(Domain.name).where(Domain.id.in_(domain_ids))).scalars().all()
            console.print(f"[cyan]Phase 1a: Domain Prefilter[/] -> {len(domain_ids)} domains ({', '.join(d_names)}) [dim]⏱ {_elapsed:.1f}s[/dim]")
        else:
            console.print(f"[cyan]Phase 1a: Domain Prefilter[/] -> No specific domains matched [dim]⏱ {_elapsed:.1f}s[/dim]")

        # Phase 1b: AI Domain Review (skip for small candidate sets — not worth a Gemini call)
        _t0 = _time.monotonic()
        _SKIP_AI_DOMAIN_THRESHOLD = 3
        if domain_ids and len(domain_ids) > _SKIP_AI_DOMAIN_THRESHOLD:
            report.selected_domains = _ai_review_domains(
                task, scored_domains, session, repo, console, verbose=verbose,
            )
            domain_ids = [sd.domain_id for sd in report.selected_domains]
            console.print(f"[dim]  ⏱ Phase 1b: {_time.monotonic() - _t0:.1f}s[/dim]")
        elif domain_ids:
            console.print(f"[dim]  Phase 1b: skipped (≤{_SKIP_AI_DOMAIN_THRESHOLD} domains) ⏱ 0.0s[/dim]")
            from app.models.entities import Domain
            report.selected_domains = [
                SelectedDomain(
                    domain_id=did,
                    domain_name=session.execute(select(Domain.name).where(Domain.id == did)).scalar() or "?",
                    provenance=SelectionProvenance(selected_by="prefilter", selection_reason="deterministic", confidence=0.8),
                ) for did in domain_ids
            ]
        else:
            report.selected_domains = []

        # Large-change: impact analysis and migration brief before script triage
        if getattr(task, "large_change_mode", 0):
            from app.services.agents.large_change import (
                run_impact_analysis,
                ensure_migration_brief,
                get_migration_brief,
            )
            report.impact_script_ids, report.impact_contract_ids, _ = run_impact_analysis(domain_ids, session)
            revamp_id, brief = ensure_migration_brief(
                task, session, console,
                impact_script_ids=report.impact_script_ids,
                impact_contract_ids=report.impact_contract_ids,
            )
            report.revamp_session_id = revamp_id
            report.migration_brief = brief
            console.print(f"[cyan]  [large-change][/] Impact: {len(report.impact_script_ids)} scripts, {len(report.impact_contract_ids)} contracts; migration brief ready.")

        # Phase 2a: Deterministic Script/Contract Triage
        _t0 = _time.monotonic()
        prefilter_script_ids, prefilter_contract_ids = _triage_scripts(
            task, domain_ids, session,
            impact_script_ids=report.impact_script_ids or None,
            impact_contract_ids=report.impact_contract_ids or None,
            repo_root=repo.root_path if repo else None,
        )
        _elapsed = _time.monotonic() - _t0
        if prefilter_script_ids:
            from app.models.entities import Script
            s_names = session.execute(select(Script.instance_path).where(Script.id.in_(prefilter_script_ids))).scalars().all()
            console.print(f"[cyan]Phase 2a: Script Prefilter[/] -> {len(prefilter_script_ids)} scripts ({', '.join(s_names[:3])}{'...' if len(s_names)>3 else ''}) [dim]⏱ {_elapsed:.1f}s[/dim]")
        else:
            console.print(f"[cyan]Phase 2a: Script Prefilter[/] -> No scripts matched [dim]⏱ {_elapsed:.1f}s[/dim]")

        # Phase 2b: AI Script Review (skip for localized tasks with few candidates)
        _t0 = _time.monotonic()
        _SKIP_AI_SCRIPT_THRESHOLD = 3
        _skip_2b = (
            task.task_class == TaskClass.semantic_localized
            and len(prefilter_script_ids) <= _SKIP_AI_SCRIPT_THRESHOLD
        )
        if prefilter_script_ids and not _skip_2b:
            report.selected_scripts, report.selected_contracts = _ai_review_scripts(
                task, prefilter_script_ids, prefilter_contract_ids,
                report.selected_domains, session, repo, console, verbose=verbose,
            )
            report.relevant_script_ids = [ss.script_id for ss in report.selected_scripts]
            contract_ids = [sc.contract_id for sc in report.selected_contracts]
            console.print(f"[dim]  ⏱ Phase 2b: {_time.monotonic() - _t0:.1f}s[/dim]")
        elif prefilter_script_ids and _skip_2b:
            console.print(f"[dim]  Phase 2b: skipped (semantic_localized + ≤{_SKIP_AI_SCRIPT_THRESHOLD} scripts) ⏱ 0.0s[/dim]")
            from app.models.entities import Script
            report.selected_scripts = [
                SelectedScript(
                    script_id=sid,
                    instance_path=session.execute(select(Script.instance_path).where(Script.id == sid)).scalar() or "?",
                    provenance=SelectionProvenance(selected_by="prefilter", selection_reason="deterministic", confidence=0.8),
                ) for sid in prefilter_script_ids
            ]
            report.selected_contracts = []
            report.relevant_script_ids = prefilter_script_ids
            contract_ids = prefilter_contract_ids
        else:
            report.selected_scripts = []
            report.selected_contracts = []
            report.relevant_script_ids = prefilter_script_ids
            contract_ids = prefilter_contract_ids

        # Fetch basic contract info for the report
        if contract_ids:
            from app.models.entities import Contract
            contracts_db = session.execute(
                select(Contract).where(Contract.id.in_(contract_ids))
            ).scalars().all()
            report.contracts = [
                {"id": c.id, "name": c.name, "kind": c.kind, "summary": c.summary}
                for c in contracts_db
            ]

        # Phase 3 + 4: Docs and Deep Read in parallel (with per-phase timeout and schema-driven merge)
        _t0 = _time.monotonic()
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from concurrent.futures import ThreadPoolExecutor

        phase_timeout = getattr(settings, "investigation_phase_timeout_secs", 300)
        workers = investigation_workers if investigation_workers is not None else getattr(settings, "investigation_workers", 2)
        max_workers = max(2, workers)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_docs = pool.submit(lambda: _investigate_docs(task, console, verbose=verbose, repo=repo))
            future_deep = (
                pool.submit(
                    lambda: _deep_read_scripts(
                        task,
                        repo,
                        report.relevant_script_ids,
                        console,
                        investigation_workers_override=investigation_workers,
                        verbose=verbose,
                    )
                )
                if report.relevant_script_ids
                else None
            )
            try:
                doc_entries = future_docs.result(timeout=phase_timeout)
            except FuturesTimeoutError:
                console.print("[yellow]  Phase 3 (docs) timed out; using partial/empty doc invariants.[/yellow]")
                doc_entries = []
            deep_result: dict[str, Any] = {}
            if future_deep:
                try:
                    deep_result = future_deep.result(timeout=phase_timeout)
                except FuturesTimeoutError:
                    console.print("[yellow]  Phase 4 (deep read) timed out; using partial/empty deep-read result.[/yellow]")
                    deep_result = {
                        "invariant_entries": [],
                        "risk_entries": [],
                        "uncertainties": [],
                        "file_bodies": {},
                        "agent_notes": "",
                        "new_inv_entries": [],
                        "new_risk_entries": [],
                    }
            inv_entries_from_deep = deep_result.get("invariant_entries", [])
            risk_entries_from_deep = deep_result.get("risk_entries", [])
            merged_inv = merge_invariant_entries(doc_entries + inv_entries_from_deep)
            merged_risk = merge_risk_entries(risk_entries_from_deep)
            report.invariants = [e.text for e in merged_inv]
            report.risks = [e.text for e in merged_risk]
            report.uncertainties = deep_result.get("uncertainties", [])
            report.file_bodies = deep_result.get("file_bodies", {})
            report.agent_notes = deep_result.get("agent_notes", "")
            _persist_deep_read_memory(
                session,
                deep_result.get("new_inv_entries", []),
                deep_result.get("new_risk_entries", []),
                report.relevant_script_ids[0] if report.relevant_script_ids else None,
            )
        console.print(f"[dim]  ⏱ Phase 3+4 (parallel): {_time.monotonic() - _t0:.1f}s[/dim]")

        # Phase 5: Environment Validation
        # Skip for semantic_localized (simple fixes don't need uncertainty resolution)
        # Skip for ≤1 uncertainty (not worth a Gemini call)
        _t0 = _time.monotonic()
        _skip_phase5 = (
            task.task_class == TaskClass.semantic_localized
            or len(report.uncertainties) <= 1
        )
        if not _skip_phase5 and report.uncertainties:
            _validate_environment(task, report, session, console)
            console.print(f"[dim]  ⏱ Phase 5: {_time.monotonic() - _t0:.1f}s[/dim]")
        elif report.uncertainties:
            console.print(f"[dim]  Phase 5: skipped (semantic_localized or ≤1 uncertainty) ⏱ 0.0s[/dim]")

        # Phase 6: Synthesis (Packet Assembly)
        _t0 = _time.monotonic()
        packet = assemble_from_report(task, report, console, verbose=verbose, repo=repo)
        console.print(f"[dim]  ⏱ Phase 6 (synthesis): {_time.monotonic() - _t0:.1f}s[/dim]")

        _total = _time.monotonic() - _pipeline_start
        console.print(f"[bold dim]  ⏱ Total investigation: {_total:.1f}s[/bold dim]")

        # Persist packet
        from app.models.entities import ContextPacket as ContextPacketRow
        from app.services.packets.assembler import _estimate_tokens

        packet_json = packet.model_dump_json(indent=2)
        row = ContextPacketRow(
            task_id=task_id,
            packet_json=packet_json,
            token_estimate=_estimate_tokens(packet_json),
        )
        session.add(row)
        session.commit()

        return packet, report

    finally:
        session.close()

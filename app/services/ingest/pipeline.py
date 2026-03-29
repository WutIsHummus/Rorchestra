"""
Repository ingestion pipeline.

Loads a Rojo project, generates a sourcemap, indexes all Luau files,
extracts require() relationships, and persists everything to the DB.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select, delete

from app.adapters import rojo
from app.config import settings
from app.models.entities import (
    Domain,
    DomainKind,
    GraphEdge,
    EdgeKind,
    Repository,
    Script,
)
from app.models.schemas import RepoSnapshot, DomainInfo, ScriptInfo, SourcemapNode
from app.storage.database import get_session

# ── Require extraction ────────────────────────────────────────────────────

# Matches:  require(script.Parent.Module)
#           require(game.ReplicatedStorage.Shared.Utils)
#           local X = require(...)
_REQUIRE_RE = re.compile(
    r"""require\s*\(\s*([^)]+?)\s*\)""",
    re.MULTILINE,
)


def _extract_requires(source: str) -> list[str]:
    """Return a list of raw require argument strings from Luau source."""
    return [m.group(1).strip() for m in _REQUIRE_RE.finditer(source)]


# ── Script type detection ────────────────────────────────────────────────

_SCRIPT_TYPE_SUFFIXES = {
    ".server.luau": "Script",
    ".server.lua": "Script",
    ".client.luau": "LocalScript",
    ".client.lua": "LocalScript",
    ".luau": "ModuleScript",
    ".lua": "ModuleScript",
}


def _detect_script_type(file_path: str, class_name: str | None = None) -> str:
    """Infer Script / LocalScript / ModuleScript from filename or className."""
    if class_name and class_name in ("Script", "LocalScript", "ModuleScript"):
        return class_name
    lower = file_path.lower()
    for suffix, stype in _SCRIPT_TYPE_SUFFIXES.items():
        if lower.endswith(suffix):
            return stype
    return "ModuleScript"


# ── Domain auto-detection ────────────────────────────────────────────────


def _resolve_domain(
    instance_path: str,
    domain_cache: dict[str, tuple[str, DomainKind]],
) -> tuple[str, DomainKind]:
    """
    Determine which domain a script belongs to based on its instance path.

    Uses user overrides first, then well-known hints, then falls back to
    "unknown".  Populates *domain_cache* for reuse.
    """
    parts = instance_path.split(".")
    # Walk from top-level down to find the first matching service name
    for part in parts:
        if part in domain_cache:
            return domain_cache[part]

    # Check overrides then hints
    merged = {**settings.domain_hints, **settings.domain_map_overrides}
    for part in parts:
        for key, domain_name in merged.items():
            if part.lower() == key.lower():
                kind = DomainKind(domain_name) if domain_name in DomainKind.__members__ else DomainKind.shared
                domain_cache[part] = (domain_name, kind)
                return domain_name, kind

    return "unknown", DomainKind.shared


# ── Main pipeline ─────────────────────────────────────────────────────────


def ingest_repository(repo_path: str | Path) -> RepoSnapshot:
    """
    Full ingestion pipeline:
      1. Find & parse Rojo project file.
      2. Generate / read sourcemap.json.
      3. Walk sourcemap, index .luau files.
      4. Extract require() relationships.
      5. Assign domains.
      6. Persist to DB.
      7. Return a lightweight RepoSnapshot.
    """
    repo_root = Path(repo_path).resolve()
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Repository root not found: {repo_root}")

    # 1. Rojo project
    project_path = rojo.find_project_file(repo_root)
    project_data = rojo.parse_project_file(project_path) if project_path else {}

    # 2. Sourcemap
    sourcemap_path = repo_root / "sourcemap.json"
    if not sourcemap_path.exists():
        if project_path:
            sourcemap_path = rojo.generate_sourcemap(repo_root, project_path)
        else:
            raise RuntimeError(
                "No Rojo project file found and no sourcemap.json present."
            )

    sourcemap = rojo.read_sourcemap(sourcemap_path)
    entries = rojo.walk_sourcemap(sourcemap)

    # 3-4. Index files and extract requires
    domain_cache: dict[str, tuple[str, DomainKind]] = {}
    scripts_by_domain: dict[str, list[ScriptInfo]] = {}
    script_infos: list[ScriptInfo] = []

    for entry in entries:
        for fp in entry["filePaths"]:
            abs_fp = repo_root / fp
            if not abs_fp.exists():
                continue
            if abs_fp.suffix not in (".luau", ".lua"):
                continue

            source = abs_fp.read_text(encoding="utf-8", errors="replace")
            requires = _extract_requires(source)
            line_count = source.count("\n") + 1
            script_type = _detect_script_type(fp, entry.get("className"))
            instance_path = entry["instancePath"]
            domain_name, domain_kind = _resolve_domain(instance_path, domain_cache)

            info = ScriptInfo(
                file_path=fp,
                rojo_path=fp,
                instance_path=instance_path,
                script_type=script_type,
                line_count=line_count,
                requires=requires,
            )
            script_infos.append(info)
            scripts_by_domain.setdefault(domain_name, []).append(info)

    # 5. Build domain list
    domain_infos: list[DomainInfo] = []
    for dname, sinfos in scripts_by_domain.items():
        _, dkind = domain_cache.get(dname, (dname, DomainKind.shared))
        domain_infos.append(DomainInfo(name=dname, kind=dkind.value, scripts=sinfos))

    # 6. Persist
    session = get_session()
    try:
        # Check for existing repo at this path (re-ingest case)
        existing = session.execute(
            select(Repository).where(Repository.root_path == str(repo_root))
        ).scalar_one_or_none()

        if existing:
            repo = existing
            repo.rojo_project_path = str(project_path) if project_path else None
            repo.sourcemap_path = str(sourcemap_path)
            from datetime import datetime
            repo.updated_at = datetime.now()

            # Clear old children so we can re-index cleanly
            old_script_ids = [
                s.id for s in session.execute(
                    select(Script.id).where(Script.repo_id == repo.id)
                ).scalars().all()
            ]
            if old_script_ids:
                session.execute(
                    delete(GraphEdge).where(
                        GraphEdge.source_id.in_(old_script_ids),
                        GraphEdge.source_type == "script",
                    )
                )
            session.execute(
                delete(Script).where(Script.repo_id == repo.id)
            )
            session.execute(
                delete(Domain).where(Domain.repo_id == repo.id)
            )
            session.flush()
        else:
            repo = Repository(
                name=repo_root.name,
                root_path=str(repo_root),
                rojo_project_path=str(project_path) if project_path else None,
                sourcemap_path=str(sourcemap_path),
            )
            session.add(repo)
            session.flush()  # get repo.id

        # Re-index domains, scripts, and edges
        domain_id_map: dict[str, int] = {}
        for di in domain_infos:
            kind = DomainKind(di.kind) if di.kind in DomainKind.__members__ else DomainKind.shared
            domain_obj = Domain(
                repo_id=repo.id,
                name=di.name,
                kind=kind,
            )
            session.add(domain_obj)
            session.flush()
            domain_id_map[di.name] = domain_obj.id

        edge_count = 0
        for si in script_infos:
            domain_name, _ = _resolve_domain(si.instance_path or "", domain_cache)
            script_obj = Script(
                repo_id=repo.id,
                domain_id=domain_id_map.get(domain_name),
                file_path=si.file_path,
                rojo_path=si.rojo_path,
                instance_path=si.instance_path,
                script_type=si.script_type,
                line_count=si.line_count,
            )
            script_obj.requires = si.requires
            session.add(script_obj)
            session.flush()

            # belongs_to_domain edge
            if domain_name in domain_id_map:
                session.add(GraphEdge(
                    source_id=script_obj.id,
                    source_type="script",
                    target_id=domain_id_map[domain_name],
                    target_type="domain",
                    edge_kind=EdgeKind.belongs_to_domain,
                ))
                edge_count += 1

        session.commit()

        return RepoSnapshot(
            repo_id=repo.id,
            name=repo.name,
            root_path=str(repo_root),
            rojo_project_path=str(project_path) if project_path else None,
            domains=domain_infos,
            script_count=len(script_infos),
            edge_count=edge_count,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

"""
Skill loader — scans the skills directory for Markdown skill files,
parses their frontmatter (description + triggers), and upserts them
as procedural memory records.

Skills are stable rules and are never invalidated by file changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.models.entities import MemoryType
from app.services.memory.store import upsert_memory


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """
    Split a Markdown file into YAML-ish frontmatter dict and body.
    We do a lightweight parse to avoid pulling in a YAML library.
    """
    frontmatter: dict[str, Any] = {}
    body = text

    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, re.DOTALL)
    if not match:
        return frontmatter, body

    raw_fm = match.group(1)
    body = text[match.end():]

    for line in raw_fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" not in line:
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        # Handle inline lists: [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            items = [
                v.strip().strip("'\"")
                for v in value[1:-1].split(",")
                if v.strip()
            ]
            frontmatter[key] = items
        else:
            frontmatter[key] = value

    return frontmatter, body


def _parse_triggers(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Extract the triggers sub-dict from frontmatter."""
    triggers: dict[str, Any] = {}

    # Triggers may be nested under a 'triggers' key or flattened
    if "triggers" in frontmatter and isinstance(frontmatter["triggers"], dict):
        return frontmatter["triggers"]

    # Check for flattened trigger keys in the raw frontmatter
    # Our lightweight parser doesn't handle nested YAML, so we re-parse
    # the original text for nested keys like "  runtime_sides: [client]"
    return triggers


def load_skill_file(skill_path: Path) -> dict[str, Any]:
    """
    Parse a single skill file and return its metadata.

    Returns::

        {
            "name": "roblox-ui-scaling",
            "description": "...",
            "triggers": {"runtime_sides": [...], "scope_keywords": [...]},
            "content": "... (body text)",
        }
    """
    text = skill_path.read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(text)

    # Re-parse for nested trigger keys within frontmatter only
    triggers: dict[str, Any] = {}
    # Extract just the frontmatter block
    fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, re.DOTALL)
    if fm_match:
        fm_lines = fm_match.group(1).splitlines()
        in_triggers = False
        for line in fm_lines:
            stripped = line.strip()
            if stripped.startswith("triggers:"):
                in_triggers = True
                continue
            if in_triggers:
                # End of triggers block: non-indented, non-empty line
                if stripped and not line.startswith(" ") and not line.startswith("\t"):
                    in_triggers = False
                    continue
                if ":" in stripped:
                    key, _, val = stripped.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if val.startswith("[") and val.endswith("]"):
                        triggers[key] = [
                            v.strip().strip("'\"")
                            for v in val[1:-1].split(",")
                            if v.strip()
                        ]
                    else:
                        triggers[key] = val

    return {
        "name": skill_path.stem,
        "description": frontmatter.get("description", skill_path.stem),
        "triggers": triggers,
        "content": body.strip(),
    }


def load_all_skills(skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """
    Scan the skills directory, parse all .md files, and upsert them
    as procedural memory records.

    Returns the list of loaded skill metadata dicts.
    """
    skills_dir = skills_dir or settings.skills_dir
    if not skills_dir.exists():
        return []

    loaded: list[dict[str, Any]] = []

    for md_file in sorted(skills_dir.glob("*.md")):
        skill = load_skill_file(md_file)

        # Upsert as procedural memory — skills are never auto-invalidated
        import json
        upsert_memory(
            scope_id=f"skill:{skill['name']}",
            memory_type=MemoryType.procedural,
            content=json.dumps({
                "description": skill["description"],
                "triggers": skill["triggers"],
                "rules": skill["content"],
            }),
            confidence=1.0,
            source_refs=[f"skill:{md_file.name}"],
            promotion_policy="permanent",
        )

        loaded.append(skill)

    return loaded


def get_relevant_skills(
    runtime_side: str,
    target_scope: str,
) -> list[str]:
    """
    Return the rule content of skills whose triggers match the given
    runtime_side and target_scope.
    """
    import json
    from app.services.memory.store import get_memories
    from sqlalchemy import select
    from app.models.entities import MemoryRecord, MemoryType
    from app.storage.database import get_session

    session = get_session()
    try:
        records = session.execute(
            select(MemoryRecord).where(
                MemoryRecord.scope_id.like("skill:%"),
                MemoryRecord.memory_type == MemoryType.procedural,
                MemoryRecord.invalidated_by.is_(None),
            )
        ).scalars().all()

        matched_rules: list[str] = []

        for rec in records:
            try:
                data = json.loads(rec.content)
            except json.JSONDecodeError:
                continue

            triggers = data.get("triggers", {})

            # Check runtime_side trigger
            sides = triggers.get("runtime_sides", [])
            if sides and runtime_side not in sides:
                continue

            # Check scope_keywords trigger
            keywords = triggers.get("scope_keywords", [])
            if keywords:
                scope_lower = (target_scope or "").lower()
                if not any(kw.lower() in scope_lower for kw in keywords):
                    # Also check runtime_side match alone (e.g. "client" matches all client tasks)
                    if sides and runtime_side in sides:
                        pass  # Side match is enough
                    else:
                        continue

            matched_rules.append(data.get("rules", ""))

        return matched_rules
    finally:
        session.close()

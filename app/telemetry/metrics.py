"""
Telemetry — structured JSONL event collector for observability.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from app.config import settings


_LOG_FILE: Path | None = None


def _ensure_log() -> Path:
    global _LOG_FILE
    if _LOG_FILE is None:
        log_dir = settings.artifacts_dir / "telemetry"
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = log_dir / "events.jsonl"
    return _LOG_FILE


def emit(event_type: str, data: dict[str, Any] | None = None) -> None:
    """Append a structured event to the JSONL telemetry log."""
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event_type,
        **(data or {}),
    }
    with open(_ensure_log(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── Convenience helpers ───────────────────────────────────────────────────


def record_ingest(repo_id: int, script_count: int, edge_count: int) -> None:
    emit("ingest", {"repo_id": repo_id, "scripts": script_count, "edges": edge_count})


def record_packet(task_id: int, token_estimate: int) -> None:
    emit("packet_assembled", {"task_id": task_id, "token_estimate": token_estimate})


def record_worker(task_id: int, worker_type: str, exit_code: int, elapsed: float) -> None:
    emit("worker_invocation", {
        "task_id": task_id,
        "worker_type": worker_type,
        "exit_code": exit_code,
        "elapsed_secs": elapsed,
    })


def record_mcp_call(capability: str, status: str) -> None:
    emit("mcp_call", {"capability": capability, "status": status})


def record_validation(task_id: int, status: str, source: str) -> None:
    emit("validation", {"task_id": task_id, "status": status, "source": source})

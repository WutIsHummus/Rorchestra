# Roblox/Luau AI Orchestration System

A Python orchestration layer that maintains hierarchical memory of a Rojo/Luau codebase, assembles compact context packets, and dispatches Gemini CLI workers for scoped code edits.

## Quick Start

```bash
cd orchestrator
pip install -e ".[dev]"

# Ingest a Rojo project
python -m app.main ingest C:\path\to\your\rojo\project

# Generate memory summaries
python -m app.main summarize --repo-id 1

# Request an edit
python -m app.main edit "Add error handling to the data save module" --repo-id 1 --scope DataManager --side server

# Validate a patch
python -m app.main validate --task-id 1

# Check live Studio state (uncertainty-triggered)
python -m app.main check ui_existence StarterGui.ScreenGui.MainHUD

# View system status
python -m app.main status

# Enter the interactive REPL (Rorchestra)
python -m app.rochester
# Inside the REPL, you can use:
# /edit "description"  -> Propose an edit
# /apply <id>          -> Apply a patch and rebuild memory cascade
# /mcp                 -> Check connected MCP servers and statuses
```

## Architecture

| Layer | Purpose |
|---|---|
| **Adapters** | Rojo, luau-lsp, Gemini CLI, MCP dispatcher |
| **Services** | Ingest, graph, memory, summarization, packets, workers, validation, MCP |
| **Policies** | Safety gates, MCP trigger policy |
| **Telemetry** | JSONL event log |
| **Storage** | SQLite (ORM), file-based artifact store |

## MCP Integration

Uses a **canonical capability dispatcher** that routes through:
- **Primary**: Official Roblox Studio MCP
- **Fallback**: Community robloxstudio-mcp (filtered toolset)

Raw MCP output is stored out-of-band in `artifacts/mcp_raw/` — never injected into planner context.

## Memory Model

Memory records are **invalidation-driven**, not time-based. A record is only stale when its source files change:
- Accepted patches trigger `invalidate_by_file()`
- Stale scopes are re-summarised on demand via `summarize`

## Requirements

- Python 3.11+
- Rojo (on PATH)
- luau-lsp (optional, for static validation)
- Gemini CLI (for worker invocations and summarisation)

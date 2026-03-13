# Rorchestra

**AI-powered orchestration for Roblox/Luau codebases.**

Rorchestra ingests your [Rojo](https://rojo.space) project, builds a dependency graph and hierarchical memory, then uses [Gemini CLI](https://github.com/google-gemini/gemini-cli) agents to investigate context and generate scoped code edits — all from an interactive REPL.

## Features

- 🔍 **Smart Investigation** — Agent-driven context discovery that reads your dependency graph, identifies relevant scripts, and assembles focused context packets before making any edits
- 🧠 **Hierarchical Memory** — Invalidation-driven summaries at script, domain, and project levels. Memory stays fresh automatically when patches are applied
- ✏️ **Scoped Edits** — Multi-file code generation with unified diffs, automatic patch application, and safety gates for high-risk changes
- 🔌 **MCP Integration** — Connects to Roblox Studio MCP servers for live game state queries (UI existence checks, property reads, etc.)
- ⟡ **Token Tracking** — Real-time visibility into Gemini API token usage per operation and across your session
- 📋 **Plan & Review** — Investigate first with `--plan`, review the context packet, then execute when ready

## Installation

```bash
pip install rorchestra
```

Or install from source:

```bash
git clone https://github.com/YOUR_USERNAME/rorchestra.git
cd rorchestra
pip install -e ".[dev]"
```

### Requirements

- **Python 3.11+**
- **[Rojo](https://rojo.space)** on PATH (for project ingestion)
- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** on PATH (for AI workers)
- **luau-lsp** *(optional)* — for static validation

## Quick Start

```bash
# Navigate to your Rojo project directory
cd path/to/your/rojo/project

# Launch the REPL — it auto-detects and ingests your project
rorchestra
```

Rorchestra will automatically:
1. Detect your `default.project.json`
2. Ingest all scripts and build the dependency graph
3. Generate memory summaries
4. Drop you into the interactive REPL

## Commands

| Command | Description |
|---------|-------------|
| `/edit <desc>` | Agent-driven code edit with investigation |
| `/edit <desc> --plan` | Investigate and review before executing |
| `/edit <desc> --debug` | Show full internal context sent to the worker |
| `/ask <question>` | Ask questions about your codebase |
| `/status` | Show repo stats — scripts, domains, memory health |
| `/ingest <path>` | Ingest a Rojo project |
| `/summarize` | Re-summarize all scripts |
| `/plans` | List and manage saved investigation plans |
| `/tokens` | Show token usage for this session |
| `/mcp` | Check MCP server connection status |
| `/skills` | Load procedural skill memories |
| `/help` | Show all commands |

You can also just type naturally without a `/` to ask questions about your codebase.

## Architecture

```
orchestrator/
├── app/
│   ├── adapters/        # Rojo, luau-lsp, Gemini CLI, MCP
│   ├── models/          # SQLAlchemy entities & Pydantic schemas
│   ├── services/
│   │   ├── agents/      # Investigation orchestrator & tool definitions
│   │   ├── ingest/      # Rojo project scanner & graph builder
│   │   ├── memory/      # Invalidation-driven memory system
│   │   ├── mcp/         # MCP capability router (primary + fallback)
│   │   ├── packets/     # Context packet assembler with token budgets
│   │   ├── summarization/  # Parallel script & domain summarizer
│   │   └── workers/     # Edit worker lifecycle & patch application
│   ├── policies/        # Safety gates & MCP trigger policy
│   ├── telemetry/       # JSONL event logging & metrics
│   └── storage/         # SQLite ORM & file artifact store
```

### How It Works

1. **Ingest** — Scans your Rojo project tree, extracts all Luau scripts, and builds a `require()` dependency graph
2. **Summarize** — Generates concise AI summaries for every script and domain, stored as invalidation-driven memory records
3. **Investigate** — When you request an edit, agents explore the dependency graph to find all relevant scripts, identify invariants and risks
4. **Edit** — A focused context packet (within token budget) is sent to a Gemini CLI worker that generates a unified diff
5. **Apply** — The diff is applied to your source files and affected memory records are invalidated for re-summarization

## Configuration

All settings can be overridden via environment variables with the `ORCH_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCH_GEMINI_CLI_BIN` | `gemini` | Path to Gemini CLI |
| `ORCH_ROJO_BIN` | `rojo` | Path to Rojo binary |
| `ORCH_ROBLOX_STUDIO_MCP_EXE` | *(empty)* | Path to official Roblox Studio MCP |
| `ORCH_COMMUNITY_MCP_ENTRYPOINT` | `npx -y robloxstudio-mcp@latest` | Community MCP command |
| `ORCH_DEFAULT_TOKEN_BUDGET` | `64000` | Token budget for context packets |
| `ORCH_WORKER_TIMEOUT_SECS` | `300` | Timeout for worker invocations |

## License

MIT

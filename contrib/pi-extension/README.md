# pi-kioku-mesh

[kioku-mesh](https://github.com/h-wata/kioku-mesh) shared memory tools for the
[pi coding agent](https://pi.dev).

pi deliberately ships without MCP support — extensions are its native way to add
tools. This package registers kioku-mesh's memory tools (`save_observation`,
`search_memory`, `get_memory`, `recall_context`) as first-class pi tools, so the
agent sees them with their full typed schemas and proactive-use guidance instead
of hiding them behind a generic MCP proxy.

Under the hood it is a thin shim: the `kioku-mesh-mcp` stdio server is spawned
lazily on the first tool call and spoken to over plain JSON-RPC. Zero runtime
npm dependencies — the extension is a single TypeScript file loaded directly by
pi (via jiti, no build step).

## Prerequisites

kioku-mesh itself is a Python package and must be installed separately:

```bash
uv tool install kioku-mesh   # or: pip install kioku-mesh
kioku-mesh init --mode local
```

The extension checks for `kioku-mesh-mcp` on PATH at session start and shows an
install hint if it is missing.

## Install

```bash
pi install git:github.com/h-wata/pi-kioku-mesh
```

Or for local development, clone and install by path:

```bash
git clone https://github.com/h-wata/pi-kioku-mesh
pi install ./pi-kioku-mesh
```

## Tools

| Tool | Purpose |
|---|---|
| `save_observation` | Persist a decision / bug root cause / pattern / config change into shared memory. The description tells the agent to call it proactively. |
| `search_memory` | Search shared memory; empty `and` searches auto-retry as `or`. |
| `get_memory` | Fetch one observation's full record by its 32-char id. |
| `recall_context` | Grouped Markdown view of recent/relevant context; good first call on a project. |

## Identity and configuration

Per kioku-mesh's ADR-0019, agent identity is resolved from the environment, not
from tool arguments. The extension launches the server with:

| Env var | Default | Meaning |
|---|---|---|
| `KIOKU_MESH_AGENT_FAMILY` | `pi` | Agent family recorded on saved observations. |
| `KIOKU_MESH_CLIENT_ID` | `pi` | Client id recorded on saved observations. |
| `KIOKU_MESH_MCP_COMMAND` | `kioku-mesh-mcp` | Override the server binary (e.g. an absolute path). |

Set these in the environment you run `pi` from to override.

## Development

```bash
npm install
npm run typecheck
```

There is no build step: pi loads `src/index.ts` directly.

With kioku-mesh installed locally, `npx tsx smoke.ts` round-trips all four
tools against a real `kioku-mesh-mcp` server.

## Roadmap

- Auto-recall on session start (`before_agent_start` injection of
  `recall_context` output), opt-in via env var.
- Custom `renderResult` widgets for search/recall output in pi's TUI.
- Cancellation support (wire pi's `AbortSignal` through to the MCP call).
- npm publish for `pi install npm:pi-kioku-mesh`.

## License

MIT

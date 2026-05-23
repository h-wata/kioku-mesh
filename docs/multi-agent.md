# Multi-agent identity (single host, multiple agents)

A single PC often runs several agents simultaneously — two Claude Code
windows in different terminals, a Codex CLI session, and an autonomous
nightly cleanup job. Each agent must carry a **distinct identity** so its
observations land at non-colliding Zenoh keys.

## Identity tiers

kioku-mesh composes the key prefix from four levels:

| Tier | Source | Purpose |
|------|--------|---------|
| `agent_family` | env `MESH_MEM_AGENT_FAMILY` | Implementation: `claude-code`, `codex`, `auto-agent`, `kachaka-bridge`, ... |
| `client_id`    | env `MESH_MEM_CLIENT_ID`    | Distinct instance within the same family |
| `pc_id`        | persisted in `$MESH_MEM_STATE_DIR/pc_id` (auto-generated UUID) | Per-host identity, stable across restarts |
| `session_id`   | env `MESH_MEM_SESSION_ID` or auto-generated `{ts}-{uuid8}` | Per-process boot, stable for the agent's lifetime |

Zenoh key layout: `mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}`.

Two agents on the same host with **distinct `client_id`** will never collide
on a key, even if they share `agent_family` and `pc_id`. Conversely, two
agents that share `agent_family` AND `client_id` are treated as the same
logical agent (their observations land at overlapping keys, last-writer-wins
within the same `session_id`).

## Running two Claude Code instances on the same host

```bash
# Terminal 1
export MESH_MEM_AGENT_FAMILY=claude-code
export MESH_MEM_CLIENT_ID=claude-instance-1
claude

# Terminal 2 (same host)
export MESH_MEM_AGENT_FAMILY=claude-code
export MESH_MEM_CLIENT_ID=claude-instance-2
claude

# Autonomous agent (e.g. cron / systemd timer)
export MESH_MEM_AGENT_FAMILY=auto-agent
export MESH_MEM_CLIENT_ID=nightly-cleanup
~/.venv/mesh-mem/bin/kioku-mesh gc --retention-days 30
```

## Naming conventions (recommended)

- `agent_family`: lowercase, hyphenated (`claude-code`, `codex`, `auto-agent`).
- `client_id`: `{family}-{purpose}-{N}` if you need a hint at what it does
  (`claude-code-research-1`, `codex-refactor-2`); any unique string is fine.
- Reuse the same `client_id` across restarts so the agent's history stays
  searchable as one stream — only change it when the agent's role changes.

For shells that hop between projects, `direnv` keeps the env scoped per
directory:

```bash
# .envrc in ~/projects/foo
export MESH_MEM_CLIENT_ID=claude-code-foo

# .envrc in ~/projects/bar
export MESH_MEM_CLIENT_ID=claude-code-bar
```

## Filtering by agent at search time

```bash
# Only this Claude Code instance
kioku-mesh search "auth" --client-id claude-instance-1

# Everything from any Claude Code on any peer
kioku-mesh search "auth" --agent-family claude-code

# A specific autonomous agent
kioku-mesh search "" --client-id nightly-cleanup --limit 100
```

## MCP-launched agents

Agents started via an MCP harness (Claude Code, Claude Desktop, Codex,
Gemini) cannot read shell exports — they inherit only what the harness
passes. Set `MESH_MEM_CLIENT_ID` (and `MESH_MEM_AGENT_FAMILY` when it
differs from the family default) in the MCP server entry's `env` block. See
[ADR-0004](adr/0004-identity-env-and-persistent-file.md) for why MCP tool
arguments do not expose identity.

```jsonc
// ~/.claude.json or claude_desktop_config.json
{
  "mcpServers": {
    "mesh_mem": {
      "command": "/home/USER/.venv/mesh-mem/bin/kioku-mesh-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/127.0.0.1:7447",
        "MESH_MEM_AGENT_FAMILY": "claude-code",
        "MESH_MEM_CLIENT_ID": "claude-code-instance-1"
      }
    }
  }
}
```

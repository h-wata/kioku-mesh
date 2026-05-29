<p align="center">
  <img src="docs/assets/kioku-mesh-logo.png" alt="kioku-mesh" width="420">
</p>

<p align="center">
  <a href="https://pypi.org/project/kioku-mesh/"><img src="https://img.shields.io/pypi/v/kioku-mesh.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/kioku-mesh/"><img src="https://img.shields.io/pypi/pyversions/kioku-mesh.svg" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/h-wata/kioku-mesh.svg" alt="License"></a>
</p>

<p align="center">
  <strong>Shared memory for AI coding agents, across tools and machines.</strong>
</p>

<p align="center">
  <img src="docs/assets/demo.gif" alt="One agent saves a decision; another agent recalls it over the mesh" width="760">
</p>

`kioku` (記憶) means memory.

kioku-mesh gives coding agents a shared memory store. Claude Code, Codex CLI,
Gemini CLI, and other MCP clients can save and search the same observations from
one machine or from several machines on a trusted LAN/VPN mesh.

The default setup is local and needs no daemon. Mesh mode is available when you
want the same memory pool replicated between hosts.

## Why kioku-mesh

Coding-agent context gets fragmented across machines: which laptop did that work,
what did the agent on the other host decide, and why does a secondary agent have
to re-read everything from scratch just to give a quick second opinion?
kioku-mesh keeps that memory in one shared pool so any agent, on any of your
machines, can recall it.

Unlike hosted memory services, the shared pool is a peer-to-peer mesh you run
yourself across your own machines (LAN / VPN / Tailscale) — no SaaS, no central
account. The same memory is readable by Claude Code, Codex CLI, Gemini CLI, and
any other MCP client.

## Quickstart

```bash
pip install kioku-mesh

kioku-mesh init --mode local
kioku-mesh save "Chose Postgres over SQLite for analytics"
kioku-mesh search "Postgres"
```

Install the MCP server for your agent:

```bash
kioku-mesh mcp install --client claude-code
kioku-mesh mcp install --client codex-cli
```

The package installs two commands:

- `kioku-mesh`: the CLI.
- `kioku-mesh-mcp`: the stdio MCP server launched by your agent.

## Modes

| Mode | Use it when | Persistence | Extra service |
|---|---|---|---|
| `local` | You want memory on one machine | SQLite | none |
| `localhost` | You want to smoke-test Zenoh locally | in-memory | `zenohd` |
| `hub` | This machine is the always-on mesh hub | RocksDB | `zenohd` |
| `spoke` | This machine connects to a hub | RocksDB | `zenohd` |

`local` is the easiest starting point. Re-run `kioku-mesh init --mode <mode>
--force` when you want to switch.

In mesh mode the Zenoh/RocksDB store is the source of truth, and each host's
SQLite is a fast local read cache rebuilt from it — not a separate copy you have
to reconcile. `local` mode is a standalone, SQLite-only setup for a single
machine, so its saves live only in that local store and are not replicated to a
mesh.

## CLI

```bash
kioku-mesh save "Decided to keep billing events append-only" \
  --memory-type decision \
  --importance 4 \
  --subject billing

kioku-mesh search "billing events"
kioku-mesh get-memory <observation_id>
kioku-mesh delete <observation_id>
kioku-mesh gc --retention-days 30
kioku-mesh doctor
```

Useful environment variables:

| Variable | Purpose |
|---|---|
| `MESH_MEM_AGENT_FAMILY` | Agent family, such as `claude` or `codex` |
| `MESH_MEM_CLIENT_ID` | Client name, such as `claude-code` |
| `MESH_MEM_SESSION_ID` | Optional stable session id |
| `MESH_MEM_STATE_DIR` | State directory; defaults under the user data dir |
| `MESH_MEM_FORCE_REBUILD=1` | Rebuild the local index at CLI startup |
| `MESH_MEM_DISABLE_INDEX=1` | Use the legacy Zenoh scan path instead of SQLite index |

## MCP Clients

`kioku-mesh mcp install` handles the common setups:

```bash
kioku-mesh mcp install --client claude-code
kioku-mesh mcp install --client codex-cli
```

For Claude Desktop, Gemini CLI, ChatGPT Desktop, manual JSON/TOML examples,
SessionStart hooks, and multi-agent identity recipes, see
[docs/mcp-clients.md](docs/mcp-clients.md) and
[docs/multi-agent.md](docs/multi-agent.md).

## Multi-Host Mesh

Each host serves its agents from a fast local SQLite read index, backed by a
Zenoh router + RocksDB store, and hosts replicate to each other over the mesh:

```mermaid
flowchart LR
  subgraph HostA["🖥️ Host A"]
    direction TB
    A1["Claude Code"]
    A2["Codex CLI"]
    AS[("SQLite index<br/>local read path")]
    AZ["zenohd<br/>Zenoh router + RocksDB<br/>(source of truth)"]
    A1 & A2 -->|"save / search"| AZ
    AZ -->|"subscriber · rebuild"| AS
    A1 & A2 -.->|"fast reads"| AS
  end
  subgraph HostB["🖥️ Host B"]
    direction TB
    B1["Codex CLI"]
    B2["Gemini CLI"]
    BS[("SQLite index<br/>local read path")]
    BZ["zenohd<br/>Zenoh router + RocksDB<br/>(source of truth)"]
    B1 & B2 -->|"save / search"| BZ
    BZ -->|"subscriber · rebuild"| BS
    B1 & B2 -.->|"fast reads"| BS
  end
  AZ <==>|"Zenoh mesh replication<br/>LAN / VPN / Tailscale · TCP 7447"| BZ
```

The recommended topology is one hub and any number of spokes. The hub listens on
addresses reachable from the spokes; every spoke dials only the hub.

```mermaid
flowchart TB
  HUB["⭐ Hub<br/>always-on peer<br/>listens on LAN / Tailscale / VPN"]
  S1["Spoke · laptop"]
  S2["Spoke · desktop"]
  S3["Spoke · CI / server"]
  S1 -->|"dials hub (TCP 7447)"| HUB
  S2 -->|"dials hub"| HUB
  S3 -->|"dials hub"| HUB
  S1 -.->|"router transit<br/>(no direct link)"| S3
  S2 -.->|"router transit"| S3
```

```bash
# hub
kioku-mesh init --mode hub \
  --listen 127.0.0.1 \
  --listen 192.168.3.10

# spoke
kioku-mesh init --mode spoke \
  --listen 127.0.0.1 \
  --connect 192.168.3.10
```

Mesh mode requires `zenohd` and `zenoh-backend-rocksdb` on `PATH`. The current
target is Zenoh 1.9.0.

```bash
zenohd -c ~/.config/kioku-mesh/zenohd.json5
```

kioku-mesh is designed to run inside a closed, trusted network. Keep port
`7447/tcp` reachable only between trusted peers, and do not expose it to the
internet or an untrusted LAN. Today it relies on network admission (Tailscale,
WireGuard, firewall rules, or a trusted LAN) rather than transport-level
authentication; mTLS-based peer authentication is being considered for setups
where network admission alone is not enough.

For a full walkthrough with firewall notes, five-peer examples, add/remove
procedures, and smoke tests, see
[config/peers/example_5peer.md](config/peers/example_5peer.md).

## Development

```bash
pip install -e '.[dev,test]'
pytest tests/ -q
```

Run focused MCP checks with:

```bash
pytest tests/test_mcp_server.py tests/test_mcp_cli.py -v
```

## Notes

- Python 3.10+ is required.
- Linux is the primary development and deployment target.
- Windows users should prefer WSL2. Native setup notes are in
  [docs/windows-setup.md](docs/windows-setup.md).
- macOS support is not verified yet.
- `delete` writes a tombstone. `gc` performs physical cleanup.
- `0.x` releases are experimental; breaking changes can happen in minor
  versions.

More detail lives in [docs/Spec.md](docs/Spec.md), [CHANGELOG.md](CHANGELOG.md),
and the design records under [docs/adr/](docs/adr/).

## Acknowledgments

kioku-mesh was influenced by
[engram](https://github.com/Gentleman-Programming/engram) and
[claude-mem](https://github.com/thedotmack/claude-mem). No code is copied from
either project.

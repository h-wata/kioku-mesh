# mesh-mem

Cross-agent distributed memory over a mesh transport (currently Zenoh).

Multiple AI coding agents (Claude Code, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop) share observations across PCs. Nodes form a mesh; the PoC transport is [Zenoh](https://zenoh.io/) 1.9 with the RocksDB storage backend for eventual-consistent persistence.

現状仕様は [docs/Spec.md](./docs/Spec.md) にまとめています。設計判断の背景は [docs/adr/](./docs/adr/) を、検証記録は [docs/poc-reports/](./docs/poc-reports/) を参照してください。

## Architecture summary

- **Source of truth:** Zenoh + RocksDB storage under `mem/obs/**` and `mem/tomb/**`.
- **Read path:** SQLite local sidecar index by default; Zenoh full-scan fallback with `MESH_MEM_DISABLE_INDEX=1`.
- **Delete model:** logical delete is an existence-based tombstone; physical delete is handled by `mesh-mem gc`.
- **Identity:** `agent_family` / `client_id` come from env, `pc_id` is persisted per host, `session_id` is stable per process.
- **Interfaces:** CLI (`mesh-mem`) and stdio MCP server (`mesh-mem-mcp`) share the same store primitives.

## Status

PoC implementation is usable but still an experimental `0.x` project. LAN replication / DR / split-brain behavior has been verified on a 2-host setup, and the current code uses a SQLite sidecar index for the default search path. APIs and on-disk schema may still change before `1.0`.

See [docs/Spec.md](./docs/Spec.md) for the current behavior, [plan.md](./plan.md) for the broader design notes, and `gh issue list --state open` for live tracking status.

## Quick start (PoC)

```bash
# 1. two PCs (role names: `home` and `office`)
# 2. edit config/zenohd_*.json5 to replace 192.168.3.x / 192.168.3.y with real LAN IPs
# 3. on each PC:
export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"
mkdir -p "$ZENOH_BACKEND_ROCKSDB_ROOT"
zenohd -c config/zenohd_home.json5    # or zenohd_office.json5

# 4. install the Python package in each PC venv
python3 -m venv ~/.venv/mesh-mem
~/.venv/mesh-mem/bin/pip install -e '.[dev]'

# 5. exercise from the CLI
export MESH_MEM_AGENT_FAMILY=claude
export MESH_MEM_CLIENT_ID=claude-code

# basic save
mesh-mem save "note content" --project demo --tags a,b

# structured save (memory_type / importance / subject / summary; all optional)
mesh-mem save "store oidc tokens server-side, never in client cookies" \
    --project demo --memory-type decision --importance 4 \
    --subject "auth flow" --summary "pick OIDC over session cookies"

mesh-mem search "note"               # default --limit 50, summary-first display
mesh-mem get-memory <observation_id> # full record (32-char id) — extended fields included
mesh-mem status
```

After restarting zenohd or your host, mesh-mem may briefly return
fewer results until peer alignment completes (typically 5-10 s, up
to ~3 min for cold-era data). Use `mesh-mem status` to check
readiness (`mesh_ready: yes` when alignment is complete).

### CLI startup: `--rebuild` and `MESH_MEM_FORCE_REBUILD`

`mesh-mem` (the CLI) is a one-shot process. Since v0.2.4 it **skips**
the startup `rebuild_from_zenoh` scan by default — on a populated mesh
that scan can add ~15 s to *every* CLI invocation, which made interactive
use unworkable (#38). The local SQLite index still converges via the
replication subscriber while the process is running, so `save` /
`search` / `get-memory` / `delete` / `status` all see live writes from
this and other peers.

Long-running processes (`mesh-mem-mcp`, autonomous agents) keep the
default — they pay the rebuild cost once at startup, which is exactly
what the index is designed for.

Opt back in for a single CLI run when you need the index aligned with
the on-disk zenoh storage (e.g., after the SQLite sidecar has been
deleted, or before a one-shot `gc --project ...` against historical
data on a peer this host never received via replication):

```bash
mesh-mem --rebuild status               # explicit per-invocation flag
MESH_MEM_FORCE_REBUILD=1 mesh-mem search hello   # env-level equivalent
```

`--rebuild` (the typed flag) outranks **everything else**, including an
ambient `MESH_MEM_SKIP_REBUILD=1` exported from a shell profile or
wrapper script — direct user intent on this invocation always wins.
Below that, `MESH_MEM_FORCE_REBUILD=1` outranks `MESH_MEM_SKIP_REBUILD=1`
when both env vars are set. Full resolution order: `--rebuild` flag >
`MESH_MEM_FORCE_REBUILD` > `MESH_MEM_SKIP_REBUILD` > module default
(`True` for long-lived processes, `False` for the CLI).

## Multi-agent identity (single host, multiple agents)

A single PC often runs several agents simultaneously — two Claude Code
windows in different terminals, a Codex CLI session, and an autonomous
nightly cleanup job. Each agent must carry a **distinct identity** so its
observations land at non-colliding Zenoh keys.

### Identity tiers

mesh-mem composes the key prefix from four levels:

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

### Running two Claude Code instances on the same host

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
~/.venv/mesh-mem/bin/mesh-mem gc --retention-days 30
```

### Naming conventions (recommended)

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

### Filtering by agent at search time

```bash
# Only this Claude Code instance
mesh-mem search "auth" --client-id claude-instance-1

# Everything from any Claude Code on any peer
mesh-mem search "auth" --agent-family claude-code

# A specific autonomous agent
mesh-mem search "" --client-id nightly-cleanup --limit 100
```

### MCP-launched agents

Agents started via an MCP harness (Claude Code, Claude Desktop, Codex,
Gemini) cannot read shell exports — they inherit only what the harness
passes. Set `MESH_MEM_CLIENT_ID` (and `MESH_MEM_AGENT_FAMILY` when it
differs from the family default) in the MCP server entry's `env` block. See
[ADR-0004](docs/adr/0004-identity-env-and-persistent-file.md) for why MCP tool
arguments do not expose identity.

```jsonc
// ~/.claude.json or claude_desktop_config.json
{
  "mcpServers": {
    "mesh_mem": {
      "command": "/home/USER/.venv/mesh-mem/bin/mesh-mem-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/127.0.0.1:7447",
        "MESH_MEM_AGENT_FAMILY": "claude-code",
        "MESH_MEM_CLIENT_ID": "claude-code-instance-1"
      }
    }
  }
}
```

## Multi-host mesh setup

The Quick start above ran two PCs (`home` / `office`) using
`config/zenohd_home.json5` and `config/zenohd_office.json5`. For 3+ peers,
use `config/zenohd_peer.json5.template` and replicate it once per host.

The recommended layout is **1 hub + N spokes**: one always-on peer acts as
the hub and listens on every IP that any spoke can reach (LAN, Tailscale,
VPN). Each spoke dials only the hub. Zenoh router transit then carries
traffic between spokes without a direct link, so adding a new spoke does
**not** require touching any existing peer's config or restarting them.
Verified empirically with a 3-PC test on 2026-05-10
(`docs/poc-reports/topology-2026-05-10.md`).

### Steps

1. **Pick the hub.** Choose the always-on peer (typically a desktop /
   home server). Make sure its `listen.endpoints` cover every network
   that any spoke can reach: LAN, Tailscale, VPN — aggregate them now so
   later spokes don't force a hub restart.
2. **Per-peer config.**
   - On the hub, copy `config/zenohd_home.json5` (or the template) and
     keep `connect.endpoints: []`.
   - On each spoke, copy `config/zenohd_peer.json5.template`, replace
     `{SELF_IP}` with the spoke's own IP, and `{HUB_IP}` with the hub's
     reachable IP. Spokes do **not** list each other.
   The full walkthrough lives at
   [config/peers/example_5peer.md](config/peers/example_5peer.md).
3. **Open the firewall.** TCP/7447 from every spoke to the hub (the hub's
   inbound rule is what matters; spokes typically only need outbound). See
   Firewall section below for `ufw` / `iptables` recipes.
4. **Start zenohd on each peer.** Hub first is convenient but not
   required; spokes retry their `connect` until the hub answers.
5. **Verify connectivity.**

```bash
# on peer1
mesh-mem save "mesh-check from peer1" --project mesh-check

# on every other peer
mesh-mem search "mesh-check from peer1" --limit 5
# every peer should see the observation once the replication interval elapses
```

### Troubleshooting

| Symptom | Likely cause | Check |
|---------|-------------|-------|
| `nc -vz <peer> 7447` refused / times out | Firewall, NAT, VPN down | Open port; confirm tunnel route |
| Connects but no observations cross | Storage block mismatch (different `interval` / `hot` / `warm`) | `diff` the two configs |
| Some peers see it, others don't | Clock drift > a few seconds | `chronyc tracking` (see [Time sync](#time-sync)) |
| Search returns nothing on all peers | `MESH_MEM_DISABLE_INDEX=1` plus stale fallback | `unset MESH_MEM_DISABLE_INDEX` and retry |
| Observations duplicate in search results | Pre-v0.2.0 `MESH_MEM_DISABLE_INDEX=1` fallback | Upgrade to v0.2.0 (#12 fix) |

For the full 5-peer setup with example IPs, firewall rules, and add/remove
procedures, see [config/peers/example_5peer.md](config/peers/example_5peer.md).

To run the localhost 5-peer smoke test (requires `pip install -e '.[dev,test]'`):

```bash
pip install -e '.[dev,test]'   # installs PyYAML and other test deps
PYTHONPATH=src python3 scripts/smoke_5peer_mesh.py
```

## Windows host setup

> **Experimental — WSL2 strongly recommended.** Native Windows is not in CI;
> the `zenohd` Windows binary, RocksDB plugin, and firewall/service plumbing
> are user-maintained and may regress without notice. For a Windows
> workstation, **prefer running mesh-mem inside WSL2** as a regular Linux
> peer (see Quick start above). Keep WSL2 networking in `mirrored` mode
> (Windows 11 23H2+) so the WSL guest is reachable from other LAN peers on
> TCP/7447. The steps below remain for the rare case where a native
> Windows install is unavoidable (e.g. Claude Desktop on Windows, which
> cannot reach a WSL2 stdio MCP from the Windows host).

mesh-mem development is Linux-first. Windows 10 / 11 hosts *can* join the
Zenoh mesh as peers natively; the steps below cover the differences from
the Linux quick start. Identity env vars, CLI commands, and MCP
registration all work the same — only the **path style** differs:
wherever the Linux examples reference `~/.venv/mesh-mem/bin/<binary>`,
the Windows equivalent is `C:\Users\<user>\.venv\mesh-mem\Scripts\<binary>.exe`.

### 1. Install Python and mesh-mem

- Install Python 3.10+ from python.org with **Add to PATH** checked. No
  admin rights are needed for the per-user installer.
- mesh-mem is **not published on PyPI yet**. Install from a checkout:

  ```powershell
  git clone https://github.com/h-wata/mesh-mem.git
  cd mesh-mem
  python -m venv $env:USERPROFILE\.venv\mesh-mem
  & "$env:USERPROFILE\.venv\mesh-mem\Scripts\python.exe" -m pip install -e .
  ```

  (`pip install mesh-mem` resolves to nothing today — the package will
  appear on PyPI as part of the v1.0 release.)

### 2. Install zenohd

Grab the two standalone zip files matching your zenoh version
(1.9.0 at time of writing) from the
[Eclipse Zenoh releases page](https://github.com/eclipse-zenoh/zenoh/releases):

- `zenoh-1.9.0-x86_64-pc-windows-msvc-standalone.zip`
- `zenoh-backend-rocksdb-1.9.0-x86_64-pc-windows-msvc-standalone.zip`

(The releases page lists four naming patterns per asset — `msvc` is the
right default for Windows 10 / 11 unless you have a specific reason to
prefer `gnu`.)

Choose an install location — admin status drives the choice:

- **Have admin?** Extract both zips to `C:\Program Files\zenoh\` and add
  the directory to **machine** PATH.
- **No admin (typical corporate Win11)?** Extract to
  `%LOCALAPPDATA%\Programs\zenoh\` and append to **user** PATH instead
  (System Properties → Environment Variables → User variables → `Path`).
  Both `zenohd.exe` and the rocksdb plugin DLL must end up next to each
  other in whichever directory you pick.

### 3. Per-peer config

- Copy `config\zenohd_peer.json5.template` and replace `{SELF_IP}` /
  `{PEER_N_IP}` with real IPs. The walkthrough at
  [config/peers/example_5peer.md](config/peers/example_5peer.md) applies
  unchanged; only the path style differs.
- Use forward slashes inside JSON5 string values for the rocksdb dir to
  avoid escaping headaches:

  ```json5
  // optional override; default storage dir is %LOCALAPPDATA%\mesh-mem
  // when ZENOH_BACKEND_ROCKSDB_ROOT points there.
  // Forward slashes work on Windows in zenoh's config parser.
  ```

### 4. Run zenohd, optionally as a service

For interactive use:

```powershell
$env:ZENOH_BACKEND_ROCKSDB_ROOT = "$env:LOCALAPPDATA\mesh-mem"
New-Item -ItemType Directory -Force -Path $env:ZENOH_BACKEND_ROCKSDB_ROOT | Out-Null
zenohd.exe --config C:\path\to\zenohd_peer.json5
```

For auto-start, register zenohd as a Windows service. NSSM
(Non-Sucking Service Manager, https://nssm.cc) handles stdout / stderr
logging more cleanly than `sc.exe`:

```powershell
nssm install zenohd "C:\Program Files\zenoh\zenohd.exe" "--config C:\path\to\zenohd_peer.json5"
nssm set     zenohd AppEnvironmentExtra "ZENOH_BACKEND_ROCKSDB_ROOT=C:\Users\<user>\AppData\Local\mesh-mem"
nssm start   zenohd
```

### 5. Windows Defender Firewall

`New-NetFirewallRule` requires an **elevated PowerShell** — without
admin, it fails silently with `Access is denied.`. Right-click
PowerShell → "Run as administrator", or invoke
`Start-Process powershell -Verb RunAs` to trigger the UAC prompt:

```powershell
New-NetFirewallRule -DisplayName "mesh-mem zenohd" `
                    -Direction Inbound -Action Allow `
                    -Protocol TCP -LocalPort 7447 `
                    -RemoteAddress 192.168.1.0/24,10.0.0.14
```

Tighten `-RemoteAddress` to the actual LAN/VPN range you mesh with.

A peer that only **initiates outbound** zenoh connections (i.e., never
needs other peers to dial in) can skip this step entirely — Windows
Firewall lets the return traffic flow on the established socket. In the
hub-and-spoke layout that means **spokes do not need this inbound rule**;
only the hub does. Add the rule when this host appears in some other
peer's `connect.endpoints` list.

### 6. Time sync (w32time)

Windows ships with the `w32time` service; mesh-mem only needs sub-second
agreement across peers. Verify and force a resync if needed:

```powershell
# Status
w32tm /query /status
w32tm /query /source

# Force an immediate correction (Linux's `chronyc makestep` equivalent)
w32tm /resync /force

# Cross-check against another peer
w32tm /stripchart /computer:192.168.1.10 /samples:5 /dataonly
```

If skew exceeds a few hundred milliseconds, change the source to a
reliable NTP server with `w32tm /config /update /manualpeerlist:"time.cloudflare.com"`.

### 7. Data directory

From v0.2.1 onward, mesh-mem resolves its state directory per OS:

- **Windows**: `%LOCALAPPDATA%\mesh-mem` (e.g. `C:\Users\<user>\AppData\Local\mesh-mem`) — via `platformdirs`
- **macOS**: `~/Library/Application Support/mesh-mem` — via `platformdirs`
- **Linux**: `~/.local/share/mesh-mem` (fixed, unchanged from v0.2.0;
  `XDG_DATA_HOME` is intentionally NOT honored to preserve the
  pre-v0.2.1 path and avoid silently migrating users who had set it)

To override (e.g. point at a faster NVMe):

```powershell
$env:MESH_MEM_STATE_DIR = "D:\mesh-mem-state"
```

### 8. Smoke check

```powershell
$env:MESH_MEM_AGENT_FAMILY = "claude-code"
$env:MESH_MEM_CLIENT_ID    = "claude-windows-1"

mesh-mem save "hello from windows" --project demo --memory-type note
# From any other peer:
mesh-mem search "hello from windows" --project demo --limit 5
```

When this host is the **new** peer joining an established mesh, the
local SQLite index is empty and the in-process replication subscriber
will populate it as observations arrive — `save` / `search` against
freshly-published data work fine. To pull historical entries from the
existing peers' zenohd RocksDB into the local index in one shot, run
once with `--rebuild`:

```powershell
mesh-mem --rebuild status   # one-time alignment scan
```

Expect the rebuild to take **tens of seconds** on a populated mesh
(observed ~15 s against ~117k records). Subsequent CLI invocations
default to skipping that scan (#38) so interactive use stays
sub-second.

### Known limitations

- **No CI coverage.** mesh-mem's CI runs Linux-only; native Windows
  regressions are caught by the user at run time, not in pre-merge tests.
  This is the primary reason the section above is marked Experimental.
- WSL2: a Windows-host zenohd is reachable from WSL only when the WSL
  network mode is set to `mirrored` (Windows 11 23H2+) or you forward
  TCP/7447 manually. The default `nat` mode hides Windows from the WSL
  guest. (If you can run mesh-mem inside WSL2 instead, do that.)
- Search latency on Windows mirrors Linux for the SQLite-first path
  (per-OS layer is just `pathlib`); the v0.2.0 benchmark numbers carry
  over.
- Claude Desktop on Windows cannot launch an MCP server that lives inside
  WSL2 over stdio. If you need Desktop integration on Windows, the native
  install above is currently the only path — accept the experimental
  caveats.

## Continuous Integration

- Pull requests run lint (pre-commit) and tests automatically (see #22).

## Development

```bash
# Install with test dependencies (required to run MCP smoke tests)
pip install -e '.[dev,test]'

# Run all tests
pytest tests/ -q

# Run only MCP smoke tests
pytest tests/test_mcp_server.py tests/test_mcp_cli.py -v
```

## Requirements

- Python >= 3.10
- Zenoh 1.9.0 (`eclipse-zenoh` Python binding and `zenohd` + `zenoh-backend-rocksdb` router). Older `zenohd` 1.5 builds will talk to a 1.9 client but do not ship rocksdb replication as first-class; stick to 1.9 on real hosts.
- `MESH_MEM_STATE_DIR` (default `~/.local/share/mesh-mem`) must be on a filesystem that supports POSIX hard links — ext4 / btrfs / xfs / tmpfs / NFSv3+ all qualify. FAT / exFAT and certain older SMB mounts do NOT, and `get_pc_id()` will fail on first run in that case.
- NTP/chrony clock sync (see [Time sync](#time-sync)). Replication uses HLC timestamps; clock skew > a few seconds breaks digest comparison.

## MCP registration

Register the installed `mesh-mem-mcp` console script. Use the **absolute path** inside the venv — the PATH-dependent form breaks when agents are launched from a desktop shortcut with a different environment. Each agent carries its own `MESH_MEM_CLIENT_ID`; only `MESH_MEM_AGENT_FAMILY` is shared across siblings of the same family.

### Claude Code

Use `claude mcp add` — it writes to `~/.claude.json`, which is the only location the CLI actually reads. Entries placed under `mcpServers` in `~/.claude/settings.json` are silently ignored by `claude mcp list`, so do not hand-edit that file for MCP registration.

```bash
claude mcp add mesh_mem -s user \
  -e ZENOH_CONNECT=tcp/127.0.0.1:7447 \
  -e MESH_MEM_AGENT_FAMILY=claude \
  -e MESH_MEM_CLIENT_ID=claude-code \
  -- /home/USER/.venv/mesh-mem/bin/mesh-mem-mcp

claude mcp list   # expect: mesh_mem: ... - ✓ Connected
```

#### Non-interactive smoke from `claude -p`

When invoking MCP tools from a non-interactive `claude -p` session, pass
`--permission-mode bypassPermissions`. In `-p` mode there is no permission
dialog, so without this flag the first tool call lands in
`permission_denials` of the JSON output and the LLM exits early with a
"permission needed" message. The flag does NOT affect interactive
sessions.

```bash
claude -p --permission-mode bypassPermissions --output-format json \
  "mesh-mem MCP の save_observation で 'smoke' を保存して" \
  | jq '{result, denials:.permission_denials, error:.is_error}'
```

### Claude Desktop

- Linux: `~/.config/Claude/claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Claude Desktop does read `mcpServers` from its own config file:

```json
{
  "mcpServers": {
    "mesh-mem": {
      "command": "/home/USER/.venv/mesh-mem/bin/mesh-mem-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/localhost:7447",
        "MESH_MEM_AGENT_FAMILY": "claude",
        "MESH_MEM_CLIENT_ID": "claude-desktop"
      }
    }
  }
}
```

### Gemini CLI — `~/.gemini/settings.json`

`"MESH_MEM_AGENT_FAMILY": "gemini"`, `"MESH_MEM_CLIENT_ID": "gemini-cli"`.

### Codex CLI / ChatGPT Desktop

Follow the same pattern with `codex` / `chatgpt` family and the matching `*-cli` / `*-desktop` client id. The `observation_id` space is shared; mis-tagging the client id just makes `search_memory --client-id` filters useless — it does not corrupt storage.

### Optional: session id pinning

Agents that expose a launch hook can set `MESH_MEM_SESSION_ID` to a value they control (e.g. the conversation id). When unset, mesh-mem autogenerates `{YYYYMMDDTHHMMSSZ}-{short-uuid}` once per process and caches it.

### Claude Code SessionStart hook

Claude Code supports `SessionStart` hooks from `~/.claude/settings.json`. A
hook can load recent mesh-mem activity from the current project and inject it
into the first prompt of a new session, which is especially useful when the
activity happened on another PC and replicated through Zenoh.

This repo ships a sample hook at `scripts/hooks/session-start.sh`. Install it
under `~/.claude/hooks/`:

```bash
install -d ~/.claude/hooks
cp /ABSOLUTE/PATH/TO/mesh-mem/scripts/hooks/session-start.sh \
  ~/.claude/hooks/session-start-mesh-mem.sh
chmod +x ~/.claude/hooks/session-start-mesh-mem.sh
```

Then add a `SessionStart` hook entry to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/session-start-mesh-mem.sh"
          }
        ]
      }
    ]
  }
}
```

What the sample does:

- derives `PROJECT=$(basename "$PWD")`
- computes a UTC ISO8601 `--since` timestamp for 7 days ago via embedded Python
- runs `mesh-mem search --project "$PROJECT" --since "$SINCE" --limit 10 --format markdown`
- prints a top-level heading plus markdown bullets when matches exist
- stays silent when no rows match, so the session reminder does not get noise

To verify it:

1. Save a few observations under the current project with `mesh-mem save ... -p "$PROJECT"`.
2. Start a fresh Claude Code session in that project.
3. Confirm the first prompt contains a `## Recent mesh-mem context ...` section.
4. Run `/hooks` in Claude Code if you want to confirm the hook is loaded from `~/.claude/settings.json`.

## systemd unit (zenohd)

Drop the following at `~/.config/systemd/user/mesh-mem-zenohd.service` and enable with `systemctl --user enable --now mesh-mem-zenohd`. **Replace `/ABSOLUTE/PATH/TO/mesh-mem` with the actual checkout path on the host** — the unit has no way to discover where you cloned the repo.

```ini
[Unit]
Description=mesh-mem zenohd router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=ZENOH_BACKEND_ROCKSDB_ROOT=%h/.local/share/mesh-mem
ExecStartPre=/usr/bin/install -d %h/.local/share/mesh-mem
# EDIT: absolute path to your mesh-mem checkout, plus home or office config.
ExecStart=/usr/bin/zenohd -c /ABSOLUTE/PATH/TO/mesh-mem/config/zenohd_home.json5
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
```

Swap `zenohd_home.json5` for `zenohd_office.json5` on the office host. For system-scope, move to `/etc/systemd/system/` and replace `%h` with an absolute home path too.

## Auto-start with systemd (system-wide drop-in)

If `zenohd` was installed via apt, it ships a base unit at
`/usr/lib/systemd/system/zenohd.service` whose `ExecStart` targets
`/etc/zenohd/zenohd.json5` — not the mesh-mem config. Use a drop-in
override to redirect it without modifying the base unit.

```bash
# 1. Create the drop-in directory
sudo mkdir -p /etc/systemd/system/zenohd.service.d/

# 2. Copy the example
sudo cp docs/systemd-zenohd-override.example.conf \
    /etc/systemd/system/zenohd.service.d/override.conf

# 3. Edit User= and ExecStart= for your environment
sudo $EDITOR /etc/systemd/system/zenohd.service.d/override.conf

# 4. Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now zenohd.service
sudo systemctl status zenohd.service
```

- **Home node**: use `config/zenohd_home.json5`
- **Office node**: change `ExecStart=` to `config/zenohd_office.json5`
- `%h` in the example expands to the home directory of `User=`; no
  absolute paths needed except for the config file itself.

## Firewall

7447/tcp must be reachable **only** between the two peer PCs on the LAN.

### ufw

```bash
# home, assuming office is 192.168.3.y
sudo ufw allow from 192.168.3.y to any port 7447 proto tcp comment 'mesh-mem'
sudo ufw reload
```

### iptables

```bash
sudo iptables -A INPUT -p tcp --dport 7447 -s 192.168.3.y -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 7447 -j DROP
```

Do NOT open 7447 to the whole LAN or the internet. The PoC has no transport-level auth; anyone who reaches the port can read and write `mem/**`.

## Time sync

mesh-mem depends on the host wall clock in three places:

- **`created_at`** — set at save time by `models.py:_utc_now_iso()`; appears in `search_memory` output and in `--since-iso` comparisons.
- **`--since-iso` filter** — `search_observations` parses and compares timestamps from both hosts; silent clock skew shifts the effective cutoff by the drift amount.
- **`gc --retention-days`** — tombstone expiry is evaluated using `deleted_at` from each replica's local clock; skewed clocks cause asymmetric expiry.

### Install chrony (recommended)

`chrony` is the recommended NTP client. It supports `makestep` for immediate large-offset correction, which `systemd-timesyncd` (the default on Ubuntu) does not.

```bash
# Debian / Ubuntu
sudo apt install chrony
sudo systemctl enable --now chrony
```

### Verify alignment — both hosts

```bash
# Per-host offset (run on each node)
chronyc tracking | grep -E 'Stratum|Last offset|RMS offset|System time'

# Upstream source quality
chronyc sources -v

# Cross-host sanity check — run simultaneously on both nodes and compare
date -u
```

Recommended thresholds:
- Each node `Last offset` < 100 ms
- Observed inter-host drift (`date -u` difference) < 100 ms

> **⚠ `timedatectl status` alone is not a reliable indicator of inter-host alignment.**
>
> During NTP skew testing (TASK-122) both Home and Office reported
> `NTP service: active / synchronized: yes` via `timedatectl`, yet the actual
> wall-clock difference between the two hosts was **12.75 seconds**. This happened
> because `synchronized: yes` only means the host has reached its own NTP server —
> it says nothing about whether two hosts share the same time reference or have
> converged to within a useful tolerance of each other.
>
> Always cross-check with `date -u` on both nodes, or use `chronyc tracking` and
> compare `System time` offsets.

### Drift recovery

If you detect a large offset after the fact:

```bash
# timedatectl set-ntp true only slews the clock slowly — may take minutes/hours
# for large offsets. Use chronyc makestep for immediate correction:
sudo chronyc makestep

# Confirm
chronyc tracking | grep 'Last offset'
```

### PoC verification

See [docs/poc-reports/raw/TASK-122-ntp-skew-result.yaml](docs/poc-reports/raw/TASK-122-ntp-skew-result.yaml)
and [docs/poc-reports/SUMMARY.md §8.4](docs/poc-reports/SUMMARY.md#84-ntp-skew-境界テスト-部分実施結果task-122)
for the full skew boundary test results. Key findings: replication integrity held at ±10 s skew,
but `--since-iso` filter cutoffs shifted proportionally and `timedatectl` proved unreliable as
an inter-host alignment signal.

## Retention / gc

`mesh-mem gc` performs physical deletion; the default retention is 30 days.

```bash
# Daily retention sweep via user cron (run on ONE host; replication carries the deletes).
# Appends to the existing crontab rather than replacing it. Re-running is idempotent only
# if the exact same line is not already present, so check `crontab -l` afterwards.
( crontab -l 2>/dev/null; echo '15 3 * * * ~/.venv/mesh-mem/bin/mesh-mem gc --retention-days 30' ) | crontab -
```

## Emergency purge (機微情報混入時)

When sensitive data lands in the mesh unintentionally:

```bash
# 1. On the host where you first noticed it — tombstones via MCP or CLI also work,
#    but force-id covers the case where the obs is still unreachable locally.
mesh-mem gc --force-id <32-char observation_id>

# 2. Repeat on every peer PC. broadcast purge is best-effort;
#    running on each replica is the safety guarantee.
```

`gc --force-id` always exits 0 once the broadcast has been sent — even when the local replica never held the record — because a reachable peer may have completed the purge.

## Status & known limitations

mesh-mem is an **experimental / early preview**. API and on-disk storage schema may change between `0.x` releases without migration tooling.

- **No transport-level auth or encryption.** `mem/**` and `mem/tomb/**` are open to anyone reaching port 7447. LAN-only — never expose to the internet or to an untrusted LAN segment.
- **stdio MCP only.** Works with Claude Code, Claude Desktop, Gemini CLI, and Codex CLI. Web apps (`claude.ai`, `chatgpt.com`) are not supported — they require HTTP/SSE transport + tunnel + auth, which this PoC does not ship.
- **Multi-host (Home↔Office) field-tested on LAN (post-`v0.1.0`).** Smoke / split-brain (TASK-094), Tier-1/2/3 benchmarks (TASK-097/113/115), and a 24-hour disconnect-recovery (DR) run (TASK-119, 1,192 writes, ~24.45 h partition) all pass for data integrity (G3 / G4 / Tombstone). One known caveat: cold-era resync is **step-function** rather than incremental — Office stayed at 0 obs for 97–282 s after re-link, then jumped to the full count. Hot/warm-era reconvergence stays at ~5 s as designed. See `plan.md` §実機検証結果サマリ.
- **Logical vs physical delete.** `mesh-mem delete` / `delete_memory` write a tombstone; the observation is hidden from `search` but still stored. `mesh-mem gc --retention-days N` (default 30) physically removes expired tombstones plus their observations. `mesh-mem gc --force-id <obs_id>` broadcasts a best-effort immediate purge to every replica.
- **No FTS5 full-text search yet.** The default search path is the SQLite sidecar index and `query` is a case-insensitive substring match against `payload_json`. With `MESH_MEM_DISABLE_INDEX=1`, the legacy Zenoh full-scan fallback is used. `MAX_SEARCH=10000` is a return-size cap, not a scan budget.
- **gc broadcast is best-effort.** A replica that was unreachable during `gc --force-id` catches up on its next local `gc --retention-days` run; there is no delivery-confirmation channel.

## Migration

### `zenoh-mem` → `mesh-mem` (v0.1.x)

`ZENOH_BACKEND_ROCKSDB_ROOT` のデフォルトパスが `~/.local/share/zenoh-mem` から
`~/.local/share/mesh-mem` に変更されました。既存データを引き継ぐ場合は手動で移行してください。

```bash
# 既存データを移行する場合（オプション）
mv ~/.local/share/zenoh-mem ~/.local/share/mesh-mem
```

`~/.config/systemd/user/mesh-mem-zenohd.service` を使っている場合は、
`Environment=ZENOH_BACKEND_ROCKSDB_ROOT` の値も `mesh-mem` に更新して
`systemctl --user daemon-reload` を実行してください。

## Acknowledgments

mesh-mem は先行する "AI エージェント向け永続メモリ" プロジェクトから大きなインスピレーションを受けています。設計思想・API 形状のアイデアを参考にさせてもらった各プロジェクトに感謝します。

- **[engram](https://github.com/Gentleman-Programming/engram)** by Gentleman-Programming — MCP ベースのクロスセッションメモリ (MIT)。`save_observation` / `search_memory` のツール分割と "observation" という単位の切り出しは engram の設計を参考にしています。
- **[claude-mem](https://github.com/thedotmack/claude-mem)** by Alex Newman ([@thedotmack](https://github.com/thedotmack)) — Claude Code プラグイン、セッションの自動キャプチャ & 圧縮 (AGPL-3.0)。"エージェントの長期記憶を別プロセスに切り出す" という発想の先例として大きく影響を受けました。

両プロジェクトからはコードを引いていません（いずれも参考・インスピレーション）。差別化ポイントは Zenoh メッシュによる **マルチホスト・マルチエージェント共有** です。

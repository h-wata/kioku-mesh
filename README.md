# kioku-mesh

Persistent, mesh-synced memory for your AI coding agent.
Start on one machine in 30 seconds. Use the same memory across machines
when you need it.

## 30-second local start

```bash
pip install kioku-mesh
# or: uv tool install kioku-mesh
```

```
$ kioku-mesh init --mode local
wrote ~/.config/mesh-mem/config.yaml
local backend ready — no zenohd required.
next: kioku-mesh save "hello local"

$ kioku-mesh save "Chose Postgres over SQLite for analytics"
saved: a1b2c3d4...

$ kioku-mesh search "Postgres"
[note][2] 2026-05-22T07:51:47
Chose Postgres over SQLite for analytics <id=a1b2c3d4...>

$ kioku-mesh mcp install --client claude-code
```

No daemon. No extra binary. Your agent starts reading and writing memory immediately.

## What this is

kioku-mesh gives your AI coding agents (Claude Code, Codex CLI, Gemini CLI…) a shared
long-term memory that survives session resets and syncs across hosts over a LAN, VPN,
or mesh-VPN. A decision saved on your desktop is instantly searchable on your laptop —
or by a different agent on the same machine.

### Architecture: local vs mesh

| Mode | What runs | What you get | Dependencies |
|---|---|---|---|
| **Local** (default) | SQLite only (no zenoh library started) | Single-machine persistence (save / search) | None beyond kioku-mesh |
| **Mesh** | `zenohd` + zenoh-backend-rocksdb | Persistent multi-host mesh | zenohd (auto-provisioned) |

**Local** is the default `--mode local`. It writes to a local SQLite database and
requires nothing beyond the kioku-mesh package itself. Zero daemon, zero extra install.

**Mesh** adds persistence across restarts AND multi-host replication by routing through
`zenohd` with the RocksDB backend. Observations survive host reboots and propagate to
peers that were offline during a write. This is the setup documented in the
[Power users](#power-users-multi-host-mesh) section.

> **Try mesh without zenohd (demo path).** `kioku-mesh mesh start` / `mesh join` open
> an in-process Zenoh router (no `zenohd` binary required) so you can see multi-host
> sync working in 60 seconds. Cross-host replication is **ephemeral** — observations
> sync while every peer is online, but writes made while a peer was offline are not
> replayed later. Use this to evaluate mesh before installing `zenohd`; switch to the
> Mesh mode above for production.

### What you get

- **Single-machine persistence** — save and search on one machine, no daemon, no extra
  install (Local mode, `--mode local`). Your agent's memory survives session resets.
- **One-command MCP registration** — `kioku-mesh mcp install --client {claude-code,codex-cli}` wires the MCP server into
  your agent's config with sensible defaults; no JSON / TOML hand-editing required.
- **Multi-agent support** — Claude Code, Codex CLI, Claude Desktop, Gemini CLI, and
  ChatGPT Desktop can all read and write the same memory pool.
- **Self-diagnosis** — `kioku-mesh doctor` checks backend reachability, config, and
  storage health with actionable hints.
- **SQLite-first local search** — fast substring search and tab-completion without any
  network round-trips.
- **Soft-delete with GC** — `kioku-mesh delete` writes a tombstone; `kioku-mesh gc`
  physically purges expired records.
- **stdio MCP transport** — works with all agents that support MCP stdio; no HTTP
  tunnel or auth layer needed for trusted-LAN usage.
- **Multi-host shared memory (opt-in)** — extend to a multi-machine mesh via
  [Power users: multi-host mesh](#power-users-multi-host-mesh). No changes to
  save / search / MCP workflows required.

## Try it (local-only quickstart)

### Install kioku-mesh

```bash
# recommended — from PyPI
pip install kioku-mesh
# or:
uv tool install kioku-mesh

# alternatives (development / bleeding-edge):
#   uv tool install git+https://github.com/h-wata/kioku-mesh.git    # latest main
#   uv tool install --editable .                                    # local checkout
#   python3 -m venv ~/.venv/mesh-mem && \
#     ~/.venv/mesh-mem/bin/pip install -e '.[dev]'
```

After install, both `kioku-mesh` (CLI) and `kioku-mesh-mcp` (MCP server) land on `PATH`.

> **Two binaries — know the difference**
>
> - `kioku-mesh` (one hyphen) — the **CLI**. Run this yourself.
> - `kioku-mesh-mcp` (two hyphens) — the **stdio MCP server**. Spawned by MCP clients
>   automatically; running it from a terminal prints a usage message and exits.

### Initialize local backend

```bash
kioku-mesh init --mode local
# wrote ~/.config/mesh-mem/config.yaml
# local backend ready — no zenohd required.
```

### Save and search

```bash
# basic save
kioku-mesh save "note content" --project demo --tags a,b

# structured save (memory_type / importance / subject / summary; all optional)
kioku-mesh save "store oidc tokens server-side, never in client cookies" \
    --project demo --memory-type decision --importance 4 \
    --subject "auth flow" --summary "pick OIDC over session cookies"

kioku-mesh search "note"               # default --limit 50, summary-first display
kioku-mesh get-memory <observation_id> # full record (32-char id)
kioku-mesh status
```

### Verify with `kioku-mesh doctor`

```bash
kioku-mesh doctor          # human-readable PASS / FAIL with hints
kioku-mesh doctor --json   # machine-readable; same checks
```

Exit code: `0` all pass, `1` warnings, `2` any failure (scriptable). Current v0.3
check set:

- `config_file` — `~/.config/mesh-mem/config.yaml` exists (`kioku-mesh init` if not)
- `state_dir_hardlinks` — `MESH_MEM_STATE_DIR` is writable and supports POSIX hard links

JSON shape:

```json
{
  "ok": false,
  "worst_status": "fail",
  "checks": [
    {"name": "config_file", "status": "fail",
     "summary": "~/.config/mesh-mem/config.yaml not found",
     "hint": "Run: kioku-mesh init --mode local"}
  ]
}
```

Ready to use memory across multiple machines? See [Power users: multi-host mesh](#power-users-multi-host-mesh).

## Use it with your agent (MCP)

> **Two binaries — know the difference**
>
> - `kioku-mesh` (one hyphen) — the **CLI**. Run this yourself: `kioku-mesh mcp install --client claude-code`, `kioku-mesh status`, etc.
> - `kioku-mesh-mcp` (two hyphens) — the **stdio MCP server**. It is spawned in the background by an MCP
>   client (Claude Code, Codex CLI, Claude Desktop…); you do not type this command directly.
>   Running it from a terminal will print a usage message and exit.

### One-shot install: `kioku-mesh mcp install`

For the two most common clients, `kioku-mesh mcp install` automates the
registration so you don't have to hand-edit a JSON or TOML config:

```bash
# Claude Code (delegates to `claude mcp add` under the hood)
kioku-mesh mcp install --client claude-code

# Codex CLI (writes the [mcp_servers.mesh_mem] block into ~/.codex/config.toml)
kioku-mesh mcp install --client codex-cli
```

Both forms bake the absolute path to `kioku-mesh-mcp` into the
registration, set sensible `MESH_MEM_AGENT_FAMILY` / `MESH_MEM_CLIENT_ID`
defaults per client.

Useful flags:

| Flag | Purpose |
|------|---------|
| `--name NAME` | registry key (default `mesh_mem`; matches existing docs) |
| `-e KEY=VALUE` | extra env var; repeatable. Overrides the per-client defaults. |
| `--dry-run` | print the `claude mcp add` command or the TOML block instead of executing |
| `--force` | replace an existing registration of the same name |

Claude Desktop, Gemini CLI, and ChatGPT Desktop are still set up via the
manual recipes in [docs/mcp-clients.md](docs/mcp-clients.md) — Claude
Desktop pending macOS / Windows verification, the rest pending stable
upstream config schemas.

### Manual registration

Register the `kioku-mesh-mcp` console script in each agent's MCP config. Use the **absolute path** to the installed binary — typically `~/.local/bin/kioku-mesh-mcp` when installed via `uv tool install`, or `~/.venv/mesh-mem/bin/kioku-mesh-mcp` for a manual venv. The PATH-dependent form breaks when agents are launched from a desktop shortcut with a different environment. Per-client setup (Claude Code via `claude mcp add`, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop), the non-interactive `claude -p` smoke recipe, optional `MESH_MEM_SESSION_ID` pinning, and the Claude Code **SessionStart hook** for cross-peer context injection all live in [docs/mcp-clients.md](docs/mcp-clients.md) ([Japanese](docs/mcp-clients.ja.md)).

## Power users: multi-host mesh

The recommended layout is **1 hub + N spokes**: one always-on peer acts as
the hub and listens on every IP that any spoke can reach (LAN, Tailscale,
VPN). Each spoke dials only the hub. Zenoh router transit then carries
traffic between spokes without a direct link, so adding a new spoke does
**not** require touching any existing peer's config or restarting them.
Verified empirically with a 3-PC test on 2026-05-10
(`docs/poc-reports/topology-2026-05-10.md`).

### Step 1 — Install zenohd

<a id="install-zenohd"></a>

kioku-mesh stores observations in a Zenoh router (`zenohd`) with the RocksDB storage
backend. Both must be on `PATH` before running `kioku-mesh init`. They are **separate
packages** not pulled in by the kioku-mesh install.

Target version: **Zenoh 1.9.0**. Older `zenohd` 1.5 builds are reachable but lack
first-class RocksDB replication.

#### apt (Debian / Ubuntu)

```bash
sudo install -d /etc/apt/keyrings
curl -L https://download.eclipse.org/zenoh/debian-repo/zenoh-public-key \
  | sudo gpg --dearmor --yes --output /etc/apt/keyrings/zenoh-public-key.gpg
echo 'deb [signed-by=/etc/apt/keyrings/zenoh-public-key.gpg] https://download.eclipse.org/zenoh/debian-repo/ /' \
  | sudo tee /etc/apt/sources.list.d/eclipse-zenoh.list > /dev/null
sudo apt update
sudo apt install zenoh zenoh-backend-rocksdb
```

Required packages: `zenoh` (the `zenohd` binary) and `zenoh-backend-rocksdb`
(`libzenoh_backend_rocksdb.so`).

#### Other platforms / non-apt installs

Follow the official Zenoh docs:
[zenoh.io/docs/getting-started/installation](https://zenoh.io/docs/getting-started/installation/).
Both `zenohd` and `zenoh-backend-rocksdb` must be version-matched (mixing 1.9 with
1.5 or 2.x will silently misbehave at storage startup).

#### Verify

```bash
zenohd --version
# expected: zenohd v1.9.0 ...

zenohd -c ~/.config/mesh-mem/zenohd.json5
# look for: Successfully loaded backend "rocksdb" ...
```

If the rocksdb log line is missing, the backend library is not on the plugin search
path — re-check your install.

### Step 2 — Configure and start zenohd

```bash
kioku-mesh init          # writes ~/.config/mesh-mem/zenohd.json5 (loopback, single host)
zenohd -c ~/.config/mesh-mem/zenohd.json5   # leave running in another terminal
```

The default `init` writes a single-host config: loopback listen, in-memory volume (no
RocksDB dependency, no persistence across restarts), multicast scouting disabled.

To pin identity across runs:

```bash
export MESH_MEM_AGENT_FAMILY=claude
export MESH_MEM_CLIENT_ID=claude-code
```

After restarting zenohd or your host, kioku-mesh may briefly return fewer results until
peer alignment completes (typically 5–10 s, up to ~3 min for cold-era data). Use
`kioku-mesh status` to check readiness (`mesh_ready: yes` when alignment is complete).

### Multi-host mesh setup

<a id="multi-host-mesh-setup"></a>

#### Steps

1. **Pick the hub.** Choose the always-on peer (typically a desktop /
   home server). Make sure its `listen` endpoints cover every network
   that any spoke can reach: LAN, Tailscale, VPN — aggregate them now so
   later spokes don't force a hub restart.
2. **Generate per-peer configs with `kioku-mesh init`.**

   ```bash
   # on the hub — listen on loopback + every LAN/VPN IF spokes will reach
   kioku-mesh init --mode hub \
       --listen 127.0.0.1 \
       --listen 192.168.3.10 \
       --listen 100.64.0.5

   # on each spoke — dial the hub
   kioku-mesh init --mode spoke \
       --listen 127.0.0.1 \
       --listen 192.168.3.21 \
       --connect 192.168.3.10
   ```

   Both modes write to `~/.config/mesh-mem/zenohd.json5` by default and
   emit a rocksdb + replication block whose digest parameters match
   byte-for-byte across peers. `kioku-mesh init` without `--listen` opens
   an interactive picker that lists detected interface IPs.

   For 5+ peers or a precise walkthrough with example IPs, see
   [config/peers/example_5peer.md](config/peers/example_5peer.md). The
   bundled `config/zenohd_peer.json5.template` is still available for
   anyone who prefers hand-editing.
3. **Open the firewall.** TCP/7447 from every spoke to the hub (the hub's
   inbound rule is what matters; spokes typically only need outbound). See
   Firewall section below for `ufw` / `iptables` recipes.
4. **Start zenohd on each peer.**

   ```bash
   export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"
   mkdir -p "$ZENOH_BACKEND_ROCKSDB_ROOT"
   zenohd -c ~/.config/mesh-mem/zenohd.json5
   ```

   Hub first is convenient but not required; spokes retry their
   `connect` until the hub answers.
5. **Verify connectivity.**

```bash
# on peer1
kioku-mesh save "mesh-check from peer1" --project mesh-check

# on every other peer
kioku-mesh search "mesh-check from peer1" --limit 5
# every peer should see the observation once the replication interval elapses
```

#### Troubleshooting

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

## Reference / config / troubleshooting

### `kioku-mesh init` flags

| Flag | Purpose |
|------|---------|
| `--mode local` (default) | SQLite-only local backend; no zenoh, no daemon |
| `--mode localhost` | loopback + in-memory volume; no rocksdb, no replication |
| `--mode hub` | LAN-facing router with rocksdb + replication; spokes dial in |
| `--mode spoke` | rocksdb + replication; dials the hub (requires `--connect`) |
| `--listen ENDPOINT` | repeatable. Accepts `ip`, `ip:port`, or `tcp/ip:port`. If omitted on hub/spoke, an interactive picker lists detected IFs. |
| `--connect ENDPOINT` | repeatable. Required for `--mode spoke`. |
| `--out PATH` | override output path (default: `~/.config/mesh-mem/config.yaml`, honors `XDG_CONFIG_HOME`) |
| `--force` | overwrite an existing file |
| `--print` | emit to stdout instead of writing a file |
| `--install-systemd` | also write a user-scope systemd unit at `~/.config/systemd/user/mesh-mem-zenohd.service` so `zenohd` starts on login. Linux only — macOS / Windows / non-systemd hosts get a clear error. |

### CLI startup: `--rebuild` and `MESH_MEM_FORCE_REBUILD`

`kioku-mesh` (the CLI) is a one-shot process. Since v0.2.4 it **skips** the startup
`rebuild_from_zenoh` scan by default — on a populated mesh that scan can add ~15 s to
*every* CLI invocation (#38). The local SQLite index still converges via the
replication subscriber while the process is running, so `save` / `search` /
`get-memory` / `delete` / `status` all see live writes from this and other peers.

Long-running processes (`kioku-mesh-mcp`, autonomous agents) keep the default — they pay
the rebuild cost once at startup.

Opt back in for a single CLI run when you need the index aligned with the on-disk Zenoh
storage:

```bash
kioku-mesh --rebuild status               # explicit per-invocation flag
MESH_MEM_FORCE_REBUILD=1 kioku-mesh search hello   # env-level equivalent
```

Full resolution order: `--rebuild` flag > `MESH_MEM_FORCE_REBUILD` >
`MESH_MEM_SKIP_REBUILD` > module default (`True` for long-lived processes, `False` for
the CLI).

### Shell completion (optional)

`kioku-mesh` ships an [argcomplete](https://kislyuk.github.io/argcomplete/)-based
completer for bash / zsh (#76):

```bash
pip install -e '.[completion]'
# bash
eval "$(register-python-argcomplete kioku-mesh)"
# zsh: add to ~/.zshrc
#   autoload -U bashcompinit && bashcompinit
#   eval "$(register-python-argcomplete kioku-mesh)"
```

`--project` / `--pc-id` / `--by-pc-id` use dynamic completers that read distinct
values from the **local SQLite index only** (no Zenoh round-trip) so tab-completion
stays fast even when the mesh is large.

### Architecture summary

- **Source of truth (Local mode):** SQLite local database under `MESH_MEM_STATE_DIR`.
- **Source of truth (Mesh mode):** Zenoh + RocksDB storage under `mem/obs/**` and `mem/tomb/**`.
- **Read path:** SQLite local sidecar index by default; Zenoh full-scan fallback with `MESH_MEM_DISABLE_INDEX=1`.
- **Delete model:** logical delete is an existence-based tombstone; physical delete is handled by `kioku-mesh gc`.
- **Identity:** `agent_family` / `client_id` come from env, `pc_id` is persisted per host, `session_id` is stable per process.
- **Interfaces:** CLI (`kioku-mesh`) and stdio MCP server (`kioku-mesh-mcp`) share the same store primitives.

### Status & known limitations

kioku-mesh is a LAN / VPN / mesh-VPN shared memory for trusted peers. Trust comes from network admission, not transport-level auth. LAN replication, DR, and split-brain recovery have been verified on a 2-host setup (1,192 writes across a ~24 h partition, data integrity verified G3 / G4 / Tombstone); the default read path uses a SQLite sidecar index backed by Zenoh + RocksDB.

#### Scope decision: no transport-level auth or encryption

kioku-mesh intentionally carries no in-protocol authentication or encryption. `mem/**` and `mem/tomb/**` are readable and writable by anyone who can reach port 7447 — the security boundary is the network itself. This is a deliberate scope choice: trusted-peer shared memory on a LAN, VPN, or mesh-VPN (Tailscale, WireGuard) where admission control is handled at the network layer.

> ⚠️ **Do not expose port 7447 to untrusted networks.** Never open to the internet or to an untrusted LAN segment. See [Firewall](#firewall) for per-peer allow rules.

**stdio MCP transport only.** Works with Claude Code, Claude Desktop, Gemini CLI, and Codex CLI. Web apps (`claude.ai`, `chatgpt.com`) are not supported — they require HTTP/SSE transport + tunnel + auth, which is out of this project's current scope.

#### Versioning

kioku-mesh follows SemVer with the explicit caveat that **`0.x` is experimental**: minor version bumps may include breaking changes to APIs and on-disk storage schema. The CHANGELOG announces every break, but a stable migration path is only guaranteed from `1.0` onward. `1.0` will lock in API stability and the storage schema.

#### Operational notes

- **Cold-era resync is step-function, not incremental.** After reconnect, a peer that has been offline may show 0 obs for 97–282 s before jumping to the full count. Hot/warm-era reconvergence stays at ~5 s as designed. See `plan.md` for the real-world validation results.
- **gc broadcast is best-effort.** A replica unreachable during `gc --force-id` catches up on its next local `gc --retention-days` run; there is no delivery-confirmation channel.
- **MAX_SEARCH is a return-size cap, not a scan budget.** The default search path is the SQLite sidecar index with case-insensitive substring matching against `payload_json`; `MAX_SEARCH=10000`. With `MESH_MEM_DISABLE_INDEX=1`, the legacy Zenoh full-scan fallback is used. No FTS5 full-text search.
- **Logical vs physical delete.** `kioku-mesh delete` / `delete_memory` write a tombstone; the observation is hidden from `search` but still stored. `kioku-mesh gc --retention-days N` (default 30) physically removes expired tombstones plus their observations. `kioku-mesh gc --force-id <obs_id>` broadcasts a best-effort immediate purge to every replica.

See [docs/Spec.md](./docs/Spec.md) for the current behavior, [plan.md](./plan.md) for design notes, and `gh issue list --state open` for live tracking.

### Multi-agent identity (single host, multiple agents)

kioku-mesh composes Zenoh keys from a 4-tier identity (`agent_family` / `client_id` / `pc_id` / `session_id`) so two agents on the same host land at non-colliding keys. Setup recipes for multiple terminals, `direnv`, and MCP-launched agents live in [docs/multi-agent.md](docs/multi-agent.md) ([Japanese](docs/multi-agent.ja.md)).

### Operations

#### systemd unit (zenohd)

Easiest path is `kioku-mesh init --install-systemd` (#86), which writes both
`~/.config/mesh-mem/zenohd.json5` AND
`~/.config/systemd/user/mesh-mem-zenohd.service` in one step, with the
absolute `zenohd` path baked in. Enable with `systemctl --user daemon-reload &&
systemctl --user enable --now mesh-mem-zenohd`.

The manual template below is still documented for operators who want
custom paths or system-scope (`/etc/systemd/system/`). Drop it at
`~/.config/systemd/user/mesh-mem-zenohd.service` and enable with the same
command. **Replace `/ABSOLUTE/PATH/TO/kioku-mesh` with the actual checkout
path on the host** — the unit has no way to discover where you cloned the
repo.

```ini
[Unit]
Description=kioku-mesh zenohd router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=ZENOH_BACKEND_ROCKSDB_ROOT=%h/.local/share/mesh-mem
ExecStartPre=/usr/bin/install -d %h/.local/share/mesh-mem
# EDIT: absolute path to your kioku-mesh checkout, plus home or office config.
ExecStart=/usr/bin/zenohd -c /ABSOLUTE/PATH/TO/mesh-mem/config/zenohd_home.json5
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
```

Swap `zenohd_home.json5` for `zenohd_office.json5` on the office host. For system-scope, move to `/etc/systemd/system/` and replace `%h` with an absolute home path too.

#### Auto-start with systemd (system-wide drop-in)

If `zenohd` was installed via apt, it ships a base unit at
`/usr/lib/systemd/system/zenohd.service` whose `ExecStart` targets
`/etc/zenohd/zenohd.json5` — not the kioku-mesh config. Use a drop-in
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

#### Firewall

<a id="firewall"></a>

7447/tcp must be reachable **only** between the two peer PCs on the LAN.

##### ufw

```bash
# home, assuming office is 192.168.3.y
sudo ufw allow from 192.168.3.y to any port 7447 proto tcp comment 'kioku-mesh'
sudo ufw reload
```

##### iptables

```bash
sudo iptables -A INPUT -p tcp --dport 7447 -s 192.168.3.y -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 7447 -j DROP
```

Do NOT open 7447 to the whole LAN or the internet. The PoC has no transport-level auth; anyone who reaches the port can read and write `mem/**`.

#### Time sync

<a id="time-sync"></a>

kioku-mesh depends on the host wall clock in three places:

- **`created_at`** — set at save time by `models.py:_utc_now_iso()`; appears in `search_memory` output and in `--since-iso` comparisons.
- **`--since-iso` filter** — `search_observations` parses and compares timestamps from both hosts; silent clock skew shifts the effective cutoff by the drift amount.
- **`gc --retention-days`** — tombstone expiry is evaluated using `deleted_at` from each replica's local clock; skewed clocks cause asymmetric expiry.

##### Install chrony (recommended)

`chrony` is the recommended NTP client. It supports `makestep` for immediate large-offset correction, which `systemd-timesyncd` (the default on Ubuntu) does not.

```bash
# Debian / Ubuntu
sudo apt install chrony
sudo systemctl enable --now chrony
```

##### Verify alignment — both hosts

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

##### Drift recovery

If you detect a large offset after the fact:

```bash
# timedatectl set-ntp true only slews the clock slowly — may take minutes/hours
# for large offsets. Use chronyc makestep for immediate correction:
sudo chronyc makestep

# Confirm
chronyc tracking | grep 'Last offset'
```

##### PoC verification

Skew boundary tests (±10 s on a 2-host setup) confirmed: replication integrity holds,
but `--since-iso` filter cutoffs shift proportionally and `timedatectl` is not
a reliable inter-host alignment signal — use `chronyc tracking` instead.

#### Retention / gc

`kioku-mesh gc` performs physical deletion; the default retention is 30 days.

```bash
# Daily retention sweep via user cron (run on ONE host; replication carries the deletes).
# Appends to the existing crontab rather than replacing it. Re-running is idempotent only
# if the exact same line is not already present, so check `crontab -l` afterwards.
( crontab -l 2>/dev/null; echo '15 3 * * * ~/.venv/mesh-mem/bin/kioku-mesh gc --retention-days 30' ) | crontab -
```

#### Emergency purge

When sensitive data lands in the mesh unintentionally:

```bash
# 1. On the host where you first noticed it — tombstones via MCP or CLI also work,
#    but force-id covers the case where the obs is still unreachable locally.
kioku-mesh gc --force-id <32-char observation_id>

# 2. Repeat on every peer PC. broadcast purge is best-effort;
#    running on each replica is the safety guarantee.
```

`gc --force-id` always exits 0 once the broadcast has been sent — even when the local replica never held the record — because a reachable peer may have completed the purge.

### Windows host setup

kioku-mesh development is Linux-first and **WSL2 is strongly recommended on Windows**. Native Windows host setup — Python install, zenohd + RocksDB plugin, Windows Defender Firewall, `w32time` sync, NSSM service registration — lives in [docs/windows-setup.md](docs/windows-setup.md) ([Japanese](docs/windows-setup.ja.md)).

### Continuous Integration

Pull requests run lint (pre-commit) and tests automatically (see #22).

### Developer scripts

Only a small number of maintained helper scripts remain under `scripts/`. At the moment, the documented ones are the 5-peer smoke test (`scripts/smoke_5peer_mesh.py`) and the Claude Code SessionStart hook sample (`scripts/hooks/session-start.sh`).

### Development

```bash
# Install with test dependencies (required to run MCP smoke tests)
pip install -e '.[dev,test]'

# Run all tests
pytest tests/ -q

# Run only MCP smoke tests
pytest tests/test_mcp_server.py tests/test_mcp_cli.py -v
```

### Requirements

- Python >= 3.10
- For Local mode: no extra dependencies beyond kioku-mesh itself.
- For Mesh mode (multi-host): Zenoh 1.9.0 (`eclipse-zenoh` Python binding and `zenohd` + `zenoh-backend-rocksdb` router). See [Install zenohd](#install-zenohd) for apt / prebuilt / cargo recipes.
- For the demo-only ephemeral mesh path (`mesh start` / `mesh join`): just `zenoh-python` — already a dependency of kioku-mesh. No `zenohd` binary required.
- `MESH_MEM_STATE_DIR` (default `~/.local/share/mesh-mem`) must be on a filesystem that supports POSIX hard links — ext4 / btrfs / xfs / tmpfs / NFSv3+ all qualify. FAT / exFAT and certain older SMB mounts do NOT, and `get_pc_id()` will fail on first run in that case.
- NTP/chrony clock sync (see [Time sync](#time-sync)). Replication uses HLC timestamps; clock skew > a few seconds breaks digest comparison.

### Migration

Migration notes from the `zenoh-mem` → `mesh-mem` v0.1.x transition (state directory move from `~/.local/share/zenoh-mem` to `~/.local/share/mesh-mem`) live in [docs/migration.md](docs/migration.md). The v0.3.0 PyPI rename from `mesh-mem` to `kioku-mesh` keeps all on-disk paths (`~/.local/share/mesh-mem`, `~/.config/mesh-mem/`) unchanged — only the package name and CLI binary changed.

## Acknowledgments

kioku-mesh draws significant inspiration from earlier "persistent memory for AI agents"
projects. Thanks to the following for design ideas and API shape that we studied
while building this:

- **[engram](https://github.com/Gentleman-Programming/engram)** by Gentleman-Programming — MCP-based cross-session memory (MIT). The split between `save_observation` / `search_memory` and the "observation" as a primary unit follow engram's design.
- **[claude-mem](https://github.com/thedotmack/claude-mem)** by Alex Newman ([@thedotmack](https://github.com/thedotmack)) — a Claude Code plugin for automatic session capture and compression (AGPL-3.0). The idea of factoring an agent's long-term memory into a separate process was a strong precedent for this work.

No code is copied from either project — both are referenced for inspiration only.
kioku-mesh's distinguishing contribution is **multi-host, multi-agent shared memory
over a Zenoh mesh**.

### Design documents

Current specifications live in [docs/Spec.md](./docs/Spec.md). Background for design
decisions is recorded in [docs/adr/](./docs/adr/), and validation results in
[docs/poc-reports/](./docs/poc-reports/).

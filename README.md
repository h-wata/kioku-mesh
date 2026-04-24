# mesh-mem

Cross-agent distributed memory over a mesh transport (currently Zenoh).

Multiple AI coding agents (Claude Code, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop) share observations across PCs. Nodes form a mesh; the PoC transport is [Zenoh](https://zenoh.io/) 1.9 with the RocksDB storage backend for eventual-consistent persistence.

See [plan.md](./plan.md) for the full design.

## Status

PoC / design complete, implementation in progress. See `src/mesh_mem/`.

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
mesh-mem save "note content" --project demo --tags a,b
mesh-mem search "note"
mesh-mem status
```

## Requirements

- Python >= 3.10
- Zenoh 1.9.0 (`eclipse-zenoh` Python binding and `zenohd` + `zenoh-backend-rocksdb` router). Older `zenohd` 1.5 builds will talk to a 1.9 client but do not ship rocksdb replication as first-class; stick to 1.9 on real hosts.
- `MESH_MEM_STATE_DIR` (default `~/.local/share/mesh-mem`) must be on a filesystem that supports POSIX hard links — ext4 / btrfs / xfs / tmpfs / NFSv3+ all qualify. FAT / exFAT and certain older SMB mounts do NOT, and `get_pc_id()` will fail on first run in that case.
- NTP/chrony clock sync (see [Time sync](#time-sync)). Replication uses HLC timestamps; clock skew > a few seconds breaks digest comparison.

## MCP registration

Register the installed `mesh-mem-mcp` console script. Use the **absolute path** inside the venv — the PATH-dependent form breaks when agents are launched from a desktop shortcut with a different environment. Each agent carries its own `MESH_MEM_CLIENT_ID`; only `MESH_MEM_AGENT_FAMILY` is shared across siblings of the same family.

### Claude Code — `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "mesh-mem": {
      "command": "/home/USER/.venv/mesh-mem/bin/mesh-mem-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/localhost:7447",
        "MESH_MEM_AGENT_FAMILY": "claude",
        "MESH_MEM_CLIENT_ID": "claude-code"
      }
    }
  }
}
```

### Claude Desktop

- Linux: `~/.config/Claude/claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Same block as above but with `"MESH_MEM_CLIENT_ID": "claude-desktop"`.

### Gemini CLI — `~/.gemini/settings.json`

`"MESH_MEM_AGENT_FAMILY": "gemini"`, `"MESH_MEM_CLIENT_ID": "gemini-cli"`.

### Codex CLI / ChatGPT Desktop

Follow the same pattern with `codex` / `chatgpt` family and the matching `*-cli` / `*-desktop` client id. The `observation_id` space is shared; mis-tagging the client id just makes `search_memory --client-id` filters useless — it does not corrupt storage.

### Optional: session id pinning

Agents that expose a launch hook can set `MESH_MEM_SESSION_ID` to a value they control (e.g. the conversation id). When unset, mesh-mem autogenerates `{YYYYMMDDTHHMMSSZ}-{short-uuid}` once per process and caches it.

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

```bash
# Debian / Ubuntu
sudo apt install chrony
sudo systemctl enable --now chrony

# Verify offset is within ~100ms on both hosts
chronyc tracking | grep -E 'Stratum|Last offset|RMS offset|System time'
```

If you see `Last offset` > 1s, replication's digest comparison may fail to converge even on a stable LAN; address the clock source before troubleshooting mesh-mem itself.

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

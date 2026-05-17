# mesh-mem

Cross-agent distributed memory over a mesh transport (currently Zenoh).

Multiple AI coding agents (Claude Code, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop) share observations across PCs. Nodes form a mesh; the PoC transport is [Zenoh](https://zenoh.io/) 1.9 with the RocksDB storage backend for eventual-consistent persistence.

現状仕様は [docs/Spec.md](./docs/Spec.md) にまとめています。設計判断の背景は [docs/adr/](./docs/adr/) を、検証記録は [docs/poc-reports/](./docs/poc-reports/) を参照してください。

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

## Architecture summary

- **Source of truth:** Zenoh + RocksDB storage under `mem/obs/**` and `mem/tomb/**`.
- **Read path:** SQLite local sidecar index by default; Zenoh full-scan fallback with `MESH_MEM_DISABLE_INDEX=1`.
- **Delete model:** logical delete is an existence-based tombstone; physical delete is handled by `mesh-mem gc`.
- **Identity:** `agent_family` / `client_id` come from env, `pc_id` is persisted per host, `session_id` is stable per process.
- **Interfaces:** CLI (`mesh-mem`) and stdio MCP server (`mesh-mem-mcp`) share the same store primitives.

## Status & known limitations

PoC implementation is usable but still an experimental `0.x` project. LAN replication / DR / split-brain behavior has been verified on a 2-host setup, and the current code uses a SQLite sidecar index for the default search path. APIs and on-disk schema may still change before `1.0`. See [docs/Spec.md](./docs/Spec.md) for the current behavior, [plan.md](./plan.md) for the broader design notes, and `gh issue list --state open` for live tracking status.

- **No transport-level auth or encryption.** `mem/**` and `mem/tomb/**` are open to anyone reaching port 7447. LAN-only — never expose to the internet or to an untrusted LAN segment.
- **stdio MCP only.** Works with Claude Code, Claude Desktop, Gemini CLI, and Codex CLI. Web apps (`claude.ai`, `chatgpt.com`) are not supported — they require HTTP/SSE transport + tunnel + auth, which this PoC does not ship.
- **Multi-host (Home↔Office) field-tested on LAN (post-`v0.1.0`).** Smoke / split-brain (TASK-094), Tier-1/2/3 benchmarks (TASK-097/113/115), and a 24-hour disconnect-recovery (DR) run (TASK-119, 1,192 writes, ~24.45 h partition) all pass for data integrity (G3 / G4 / Tombstone). One known caveat: cold-era resync is **step-function** rather than incremental — Office stayed at 0 obs for 97–282 s after re-link, then jumped to the full count. Hot/warm-era reconvergence stays at ~5 s as designed. See `plan.md` §実機検証結果サマリ.
- **Logical vs physical delete.** `mesh-mem delete` / `delete_memory` write a tombstone; the observation is hidden from `search` but still stored. `mesh-mem gc --retention-days N` (default 30) physically removes expired tombstones plus their observations. `mesh-mem gc --force-id <obs_id>` broadcasts a best-effort immediate purge to every replica.
- **No FTS5 full-text search yet.** The default search path is the SQLite sidecar index and `query` is a case-insensitive substring match against `payload_json`. With `MESH_MEM_DISABLE_INDEX=1`, the legacy Zenoh full-scan fallback is used. `MAX_SEARCH=10000` is a return-size cap, not a scan budget.
- **gc broadcast is best-effort.** A replica that was unreachable during `gc --force-id` catches up on its next local `gc --retention-days` run; there is no delivery-confirmation channel.

## Multi-agent identity (single host, multiple agents)

mesh-mem composes Zenoh keys from a 4-tier identity (`agent_family` / `client_id` / `pc_id` / `session_id`) so two agents on the same host land at non-colliding keys. Setup recipes for multiple terminals, `direnv`, and MCP-launched agents live in [docs/multi-agent.md](docs/multi-agent.md) ([日本語版](docs/multi-agent.ja.md)).

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

mesh-mem development is Linux-first and **WSL2 is strongly recommended on Windows**. Native Windows host setup — Python install, zenohd + RocksDB plugin, Windows Defender Firewall, `w32time` sync, NSSM service registration — lives in [docs/windows-setup.md](docs/windows-setup.md) ([日本語版](docs/windows-setup.ja.md)).

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

Register the `mesh-mem-mcp` console script in each agent's MCP config. Use the **absolute path** inside the venv. Per-client setup (Claude Code via `claude mcp add`, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop), the non-interactive `claude -p` smoke recipe, optional `MESH_MEM_SESSION_ID` pinning, and the Claude Code **SessionStart hook** for cross-peer context injection all live in [docs/mcp-clients.md](docs/mcp-clients.md) ([日本語版](docs/mcp-clients.ja.md)).

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

## Migration

Migration notes from the `zenoh-mem` → `mesh-mem` v0.1.x transition (state directory move from `~/.local/share/zenoh-mem` to `~/.local/share/mesh-mem`) live in [docs/migration.md](docs/migration.md).

## Acknowledgments

mesh-mem は先行する "AI エージェント向け永続メモリ" プロジェクトから大きなインスピレーションを受けています。設計思想・API 形状のアイデアを参考にさせてもらった各プロジェクトに感謝します。

- **[engram](https://github.com/Gentleman-Programming/engram)** by Gentleman-Programming — MCP ベースのクロスセッションメモリ (MIT)。`save_observation` / `search_memory` のツール分割と "observation" という単位の切り出しは engram の設計を参考にしています。
- **[claude-mem](https://github.com/thedotmack/claude-mem)** by Alex Newman ([@thedotmack](https://github.com/thedotmack)) — Claude Code プラグイン、セッションの自動キャプチャ & 圧縮 (AGPL-3.0)。"エージェントの長期記憶を別プロセスに切り出す" という発想の先例として大きく影響を受けました。

両プロジェクトからはコードを引いていません（いずれも参考・インスピレーション）。差別化ポイントは Zenoh メッシュによる **マルチホスト・マルチエージェント共有** です。

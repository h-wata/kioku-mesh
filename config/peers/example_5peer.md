# Example: 5-peer personal mesh (hub-and-spoke)

This walkthrough shows how to apply `config/zenohd_peer.json5.template` to a
five-host setup using the **1 hub + N spokes** pattern recommended in
[`docs/poc-reports/topology-2026-05-10.md`](../../docs/poc-reports/topology-2026-05-10.md).
The same recipe scales down to 2-3 peers (drop unused spokes) or up to ~10
peers (add more spokes — only the spoke's own config changes; the hub keeps
its `connect.endpoints: []`).

## Topology

| Peer | Role | LAN IP | Notes |
|------|------|--------|-------|
| peer1 | **hub** — always-on desktop / home server | 192.168.1.10 | Listens on every IP that any spoke can reach |
| peer2 | spoke — laptop                            | 192.168.1.11 | Often offline; dials peer1 only |
| peer3 | spoke — agent server                      | 192.168.1.12 | Hosts autonomous / scheduled agents |
| peer4 | spoke — single-board PC                   | 192.168.1.13 | Always-on, also the gc cron host |
| peer5 | spoke — remote (over VPN)                 | 10.0.0.14    | Routed via Wireguard / Tailscale |

The link layer is **not** fully meshed: only the hub accepts inbound; each
spoke holds a single connect endpoint to the hub. Zenoh router transit
carries spoke-to-spoke traffic (e.g. peer3 → peer5) through peer1
automatically — verified empirically on 2026-05-10 with WSL2 reaching an
office peer via a home hub without any direct link between them.

### Why hub-spoke (vs full-mesh)

- **Adding a new spoke does not touch the existing peers.** Drop in
  peerN's config with `{HUB_IP}=192.168.1.10`, start its `zenohd`, and
  the rest of the mesh sees it within one replication interval. No
  config edits or restarts on peer1..peer4.
- **Only the hub needs inbound firewall rules** for TCP/7447. Spokes can
  rely on Windows Firewall / nftables default-deny-inbound and let the
  outbound TCP session carry replies on the established socket.
- **Trade-off**: the hub is a single point of dependency for cross-spoke
  traffic. For ≤10-peer personal meshes this is acceptable; if the hub
  is down, each spoke still operates locally and resyncs when the hub
  returns. For mission-critical setups, run two hubs and have each
  spoke list both in `connect.endpoints` (Zenoh dedupes the resulting
  graph).

## Per-peer config

Prefer the `kioku-mesh init` wrapper. It renders the matching Zenoh/RocksDB
replication block, keeps the local loopback endpoint for same-host CLI/MCP
clients, and can install a user-scope systemd unit for the generated config.

### Hub (peer1)

The hub listens on every address a spoke may dial and does not connect outward:

```bash
kioku-mesh init --mode hub \
  --listen 127.0.0.1 \
  --listen 192.168.1.10 \
  --out ~/.config/kioku-mesh/zenohd_peer1_local.json5 \
  --force
```

Add more `--listen` values for Tailscale, VPN, or other reachable addresses.
`init --mode hub` keeps `connect.endpoints: []`.

### Spokes (peer2..peer5)

Each spoke listens locally and dials only the hub. Spokes never list each other.

```bash
# peer2 (laptop)
kioku-mesh init --mode spoke \
  --listen 127.0.0.1 \
  --listen 192.168.1.11 \
  --connect 192.168.1.10 \
  --out ~/.config/kioku-mesh/zenohd_peer2_local.json5 \
  --force

# peer3 (agent server)
kioku-mesh init --mode spoke \
  --listen 127.0.0.1 \
  --listen 192.168.1.12 \
  --connect 192.168.1.10 \
  --out ~/.config/kioku-mesh/zenohd_peer3_local.json5 \
  --force

# repeat for peer4 (192.168.1.13) and peer5 (10.0.0.14)
```

> Place the resolved per-host config in an untracked path (e.g.
> `~/.config/kioku-mesh/zenohd_peerN_local.json5`) so real IPs never end up in
> git. The committed `config/zenohd_*.json5` files are placeholder templates by
> convention and still work as low-level examples when you need to hand-edit.

## Boot order

Order does not matter, but starting the hub first speeds up convergence.
Each spoke retries its `connect` to peer1 until it answers, so spokes can
come up independently. Cold-start convergence on a 5-peer mesh typically
completes within 30-60 s once every peer is up; large `mem/**` deltas
extend it (each peer pulls only the observations it is missing).

```bash
zenohd -c ~/.config/kioku-mesh/zenohd_peerN_local.json5
```

For hosts that should auto-start on login, generate the default config with
`kioku-mesh init --mode <hub|spoke> ... --install-systemd`, or copy the generated
unit pattern and point `ExecStart` at the per-peer config path above. The wrapper
also keeps `ZENOH_BACKEND_ROCKSDB_ROOT` aligned with the current state directory
name (`kioku-mesh`, or legacy `mesh-mem` only on partially migrated hosts).

## Firewall

Only the **hub** must accept inbound TCP/7447 from every spoke's IP.
Spokes need outbound TCP/7447 to the hub but typically no inbound rule.

```bash
# On the hub (peer1) — accept from every spoke
sudo ufw allow from 192.168.1.11 to any port 7447 proto tcp
sudo ufw allow from 192.168.1.12 to any port 7447 proto tcp
sudo ufw allow from 192.168.1.13 to any port 7447 proto tcp
sudo ufw allow from 10.0.0.14    to any port 7447 proto tcp

# iptables equivalent
sudo iptables -A INPUT -p tcp --dport 7447 \
    -s 192.168.1.0/24 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 7447 \
    -s 10.0.0.14 -j ACCEPT
```

For the VPN spoke (peer5) confirm the VPN tunnel is up before starting
zenohd; otherwise the `connect` to the hub will keep failing until the
route appears.

## Verify the mesh is connected

From any peer, save a marker observation and search/get from another
spoke (the strong test — proves transit through the hub):

```bash
# on peer2 (spoke)
kioku-mesh save "ping from peer2" --project mesh-check --tags marker

# on peer5 (a different spoke, no direct link to peer2)
kioku-mesh search "ping from peer2" --limit 5
# expect: 1 hit, project=mesh-check — traffic transited via peer1
```

If the marker is visible from every peer, the mesh is healthy. If only some
peers see it, check (in order):

1. **Firewall** — `nc -vz <hub_ip> 7447` from the spoke that does not see
   the marker. If this fails, the spoke can't reach the hub.
2. **Hub's listen IPs** — confirm the hub's `listen.endpoints` includes an
   IP the spoke can reach. Adding interfaces here is the only operation
   that may require a hub restart.
3. **IP misconfig** — confirm `{SELF_IP}` matches the LAN-visible IP, not
   the loopback or Docker bridge IP.
4. **Clock drift** — `chronyc tracking` on each peer; >500 ms skew breaks
   replication digests (Zenoh hardcodes the tolerance — see README
   §Time sync, and
   [`docs/poc-reports/topology-2026-05-10.md`](../../docs/poc-reports/topology-2026-05-10.md) §B
   for the WSL2 manual-step recipe).
5. **Index disabled** — if the receiving peer has `MESH_MEM_DISABLE_INDEX=1`
   the search reads via Zenoh full scan; results should still match.
6. **Storage volume mismatch** — confirm every peer's config has the same
   `key_expr`, `strip_prefix`, and replication block byte-for-byte.

## Adding or removing peers later

- **Adding peer6 (a new spoke)**: write peer6's config with `{HUB_IP}` set
  to peer1's IP, start its `zenohd`. **Do not edit peer1..peer5.** The hub
  picks up the new inbound session as soon as peer6 dials in. (If peer1's
  `listen.endpoints` happens to lack an IP peer6 can reach, that is the
  one-time exception that requires a hub restart with the listen
  expanded.)
- **Removing a spoke**: stop its `zenohd`. The hub closes the session;
  observations the removed spoke published remain in every other peer's
  store via prior replication.
- **Changing the hub**: this is non-trivial. Pick a candidate spoke,
  rewrite it as the new hub (empty `connect.endpoints`, listen on every
  spoke-reachable IP), then update each remaining spoke's `{HUB_IP}` and
  restart that spoke. Plan a small maintenance window.

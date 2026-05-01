# Example: 5-peer personal mesh

This walkthrough shows how to apply `config/zenohd_peer.json5.template` to a
five-host setup. The same recipe scales down to 2-3 peers (drop unused
`connect.endpoints` lines) or up to ~10 peers (add lines).

## Topology

| Peer | Role | LAN IP | Notes |
|------|------|--------|-------|
| peer1 | PC-A — desktop workstation | 192.168.1.10 | Primary Claude Code host |
| peer2 | PC-B — laptop              | 192.168.1.11 | Secondary Claude Code, often offline |
| peer3 | PC-C — agent server        | 192.168.1.12 | Hosts autonomous / scheduled agents |
| peer4 | PC-D — single-board PC     | 192.168.1.13 | Always-on, also the gc cron host |
| peer5 | PC-E — remote (over VPN)   | 10.0.0.14    | Routed via Wireguard / Tailscale |

All peers run `zenohd` on TCP/7447. The link layer is fully meshed: every
peer lists every other peer in `connect.endpoints`. Redundant paths are
harmless — Zenoh deduplicates them.

## Per-peer config

For peer1 (replace `192.168.1.10` with its own IP and list the other four):

```bash
cp config/zenohd_peer.json5.template config/zenohd_peer1.json5
sed -i \
  -e 's/{SELF_IP}/192.168.1.10/' \
  -e 's/{PEER_1_IP}/192.168.1.11/' \
  -e 's/{PEER_2_IP}/192.168.1.12/' \
  -e 's/{PEER_3_IP}/192.168.1.13/' \
  -e 's/{PEER_4_IP}/10.0.0.14/' \
  config/zenohd_peer1.json5
```

Repeat for peer2..peer5, using each host's own IP for `{SELF_IP}` and the
other four for `{PEER_N_IP}`.

## Boot order

Order does not matter. Each `zenohd` retries the configured `connect`
endpoints until they answer, so peers can come up independently. Cold-start
convergence on a 5-peer mesh typically completes within 30-60 s once every
peer is up; large `mem/**` deltas extend it (each peer pulls only the
observations it is missing).

```bash
export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"
mkdir -p "$ZENOH_BACKEND_ROCKSDB_ROOT"
zenohd -c config/zenohd_peerN.json5
```

Wrap this in a systemd user unit (see README §systemd unit) on hosts that
should auto-start on login.

## Firewall

Each peer must accept TCP/7447 from every other peer's IP.

```bash
# ufw (per peer; repeat for each remote peer IP)
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

For the VPN peer (peer5) confirm the VPN tunnel is up before starting
zenohd; otherwise the `connect` to 10.0.0.14 will keep failing until the
route appears.

## Verify the mesh is connected

From any peer, save a marker observation and search from another:

```bash
# on peer1
mesh-mem save "ping from peer1" --project mesh-check --tags marker

# on peer2 (or any other peer)
mesh-mem search "ping from peer1" --limit 5
# expect: 1 hit, project=mesh-check
```

If the marker is visible from every peer, the mesh is healthy. If only some
peers see it, check (in order):

1. **Firewall** — `nc -vz <peer_ip> 7447` from the peer that does not see it.
2. **IP misconfig** — confirm `{SELF_IP}` matches the LAN-visible IP, not
   the loopback or Docker bridge IP.
3. **Clock drift** — `chronyc tracking` on each peer; >2 s skew breaks
   replication digests (see README §Time sync).
4. **Index disabled** — if the receiving peer has `MESH_MEM_DISABLE_INDEX=1`
   the search reads via Zenoh full scan; results should still match.
5. **Storage volume mismatch** — confirm every peer's config has the same
   `key_expr`, `strip_prefix`, and replication block byte-for-byte.

## Adding or removing peers later

- **Adding peer6**: edit every existing peer's `connect.endpoints` to add
  `tcp/<peer6_ip>:7447`, then start peer6 with all five existing IPs in its
  own `connect.endpoints`. Live peers do not need to restart — zenohd picks
  up the new endpoint within one `interval`.
- **Removing a peer**: stop its `zenohd`, then drop its IP from every other
  peer's config. Surviving peers will still hold the observations the
  removed peer ever published; nothing is lost.

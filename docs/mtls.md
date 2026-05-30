# Mutual TLS (mTLS) for the mesh

By default kioku-mesh trusts whoever can reach the Zenoh port (`7447/tcp`) — it
relies on **network admission** (Tailscale, WireGuard, firewall rules, a trusted
LAN). That is enough for a closed, trusted network and needs zero certificates.

When network admission alone is not enough — a shared LAN, a zero-trust
posture, an audit requirement — you can turn on **mutual TLS**: every peer
presents a certificate signed by your own private CA, and zenohd refuses any
link whose certificate it cannot verify. The link is also encrypted in transit.

## Trust model

```
        ca.key  (the only long-lived secret — never leaves the CA host)
           │ signs
   ┌───────┼─────────┐
   ▼       ▼         ▼
 hub.crt spoke1.crt spoke2.crt        ← each peer also holds its own peer.key,
                                         generated locally and never copied off-host
```

* **One CA.** `ca.key` signs every peer certificate. Guard it; if it leaks,
  anyone can mint a valid peer. `ca.crt` (its public half) is distributed to
  every peer so they can verify each other.
* **One key pair per peer.** The private key is generated **on the peer that
  owns it and never travels**. The peer emits a CSR (a signing request — public
  information), the CA signs it, and the signed certificate comes back.
* **The only things copied between hosts are non-secret**: the CSR going to the
  CA, and `peer.crt` + `ca.crt` coming back. Move them however is convenient
  (`scp` over SSH, Tailscale, a USB stick). The two secrets — `ca.key` and each
  `peer.key` — never move.

This is the same shape as `ssh-copy-id` pushing a public key over an existing
secure channel: secrets stay home, only public material is exchanged.

### SAN — what address is this cert for?

A certificate carries a **Subject Alternative Name (SAN)**: the IPs / hostnames
the cert is valid for. With `verify_name_on_connect` enabled, a spoke dialing
`192.168.3.10` checks that the hub's certificate actually lists `192.168.3.10`
in its SAN. So when you provision a peer, pass **every address other peers use
to reach it** via `--san` (e.g. its LAN IP *and* its Tailscale IP).

## Walkthrough: a hub and one spoke

All files live under `~/.config/kioku-mesh/tls/`.

### 1. Create the CA (once, on the host that will hold the CA key)

```bash
kioku-mesh tls init-ca
# wrote ~/.config/kioku-mesh/tls/ca.key   (secret — never copy this off-host)
# wrote ~/.config/kioku-mesh/tls/ca.crt   (public — distribute to every peer)
```

### 2. On the hub: request + sign its own cert

```bash
kioku-mesh tls request --san 192.168.3.10        # add --san <tailscale-ip> etc.
kioku-mesh tls sign ~/.config/kioku-mesh/tls/peer.csr \
  -o ~/.config/kioku-mesh/tls/peer.crt            # the CA lives here, so sign locally
kioku-mesh tls install \
  --cert ~/.config/kioku-mesh/tls/peer.crt \
  --ca   ~/.config/kioku-mesh/tls/ca.crt
```

### 3. On each spoke: request, get it signed, install

```bash
# on the spoke
kioku-mesh tls request --san 192.168.3.20
scp ~/.config/kioku-mesh/tls/peer.csr  hub:/tmp/spoke1.csr     # CSR is not secret

# on the CA host
kioku-mesh tls sign /tmp/spoke1.csr -o /tmp/spoke1.crt
scp /tmp/spoke1.crt ~/.config/kioku-mesh/tls/ca.crt  spoke:/tmp/   # neither is secret

# back on the spoke
kioku-mesh tls install --cert /tmp/spoke1.crt --ca /tmp/ca.crt
```

### 4. Generate the TLS-enabled zenohd config on each peer

```bash
# hub
kioku-mesh init --mode hub  --tls --listen 127.0.0.1 --listen 192.168.3.10 --force
# spoke
kioku-mesh init --mode spoke --tls --listen 127.0.0.1 --connect 192.168.3.10 --force
```

`--tls` switches **cross-host** endpoints to the `tls/` scheme and emits a
`transport.link.tls` block (`enable_mtls: true`, `verify_name_on_connect: true`)
pointing at the cert store. It refuses to run until the certs exist, so you
cannot generate a config that zenohd would reject at startup.

### Trust boundary: loopback stays plaintext

mTLS protects links that cross the network. The hop from a local CLI / MCP
client to *its own* zenohd goes over `tcp/127.0.0.1` and never touches the wire,
so `--tls` deliberately **leaves loopback endpoints plaintext** and encrypts only
cross-host (`tls/`) links. The trust boundary is therefore the host: anyone with
local access to the machine can talk to its router; remote peers must present a
cert your CA signed. Keep `--listen 127.0.0.1` in the command above so local
`save` / `search` / MCP clients can still reach the router — `init --tls` warns
if you omit it.

Start zenohd as usual; the mesh now only admits peers holding a cert your CA
signed.

## Checking and rotating

```bash
kioku-mesh tls info     # subject, SANs, and days-until-expiry for CA + peer
kioku-mesh doctor       # the tls_certs check WARNs under 30 days, FAILs if expired/missing
```

Peer certs default to 825 days, the CA to ~10 years. To rotate a peer before it
expires, just re-run `tls request` → `tls sign` → `tls install` on that peer;
the new key/cert replace the old in place. Replacing the **CA** (`init-ca
--force`) invalidates every peer cert at once, so avoid it unless the CA key was
compromised.

## Notes

* Keys are EC P-256 (small, fast, fully supported by zenoh's rustls stack).
* Peer certs carry both `serverAuth` and `clientAuth` EKUs because every zenoh
  router both accepts and dials links over the same identity.
* mTLS rides on TCP; `kioku-mesh doctor`'s reachability probe still works
  against `tls/` endpoints (it completes the TCP handshake, not the TLS one).
  Because TLS cannot wrap UDP, `init --tls` refuses a cross-host `udp/` listen
  or connect endpoint rather than emit an unauthenticated link.
* The replication block must still match byte-for-byte across peers — `--tls`
  only changes the transport/listen/connect sections, not replication.

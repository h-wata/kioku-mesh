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
  CA, and the signed cert + `ca.crt` coming back. Move them however is convenient
  — paste a block between terminals, drop it in a chat, `scp` it, a USB stick.
  The two secrets — `ca.key` and each `peer.key` — never move and never appear
  in a copy-paste block.

This is the same shape as `ssh-copy-id` pushing a public key over an existing
secure channel: secrets stay home, only public material is exchanged.

## Enrolling a peer

There are three ways to get a peer enrolled, in increasing order of automation.
All three generate the peer key locally (it never leaves) and end with the same
installed `peer.crt` + `ca.crt`; pick whichever fits what you have.

| Flow | When | Command shape |
| --- | --- | --- |
| **Copy-paste** (default) | always works, no SSH, no file shuffling | `tls request` → paste into `tls sign` → paste back into `tls install` |
| **One file** | you'd rather move a file than paste | `tls sign … -o bundle.txt`, copy the file, `tls install bundle.txt` |
| **SSH `enroll`** | you have SSH to the CA host | `tls enroll <ca-host> --san …` (one command) |

### A. Copy-paste (no SSH, no scp)

The blocks below are **not secret** — copy them between terminals however is
convenient.

```bash
# on the CA host (once)
kioku-mesh tls init-ca

# on the peer: prints a CSR block to copy
kioku-mesh tls request --san 192.168.3.20
#   -----BEGIN KIOKU-MESH CSR-----
#   …copy this whole block…
#   -----END KIOKU-MESH CSR-----

# on the CA host: paste the CSR block (reads to its -----END line)
kioku-mesh tls sign
#   -----BEGIN KIOKU-MESH CERT BUNDLE-----
#   …copy this whole block back to the peer…
#   -----END KIOKU-MESH CERT BUNDLE-----

# back on the peer: paste the bundle block
kioku-mesh tls install
#   installed ~/.config/kioku-mesh/tls/peer.crt
#   installed ~/.config/kioku-mesh/tls/ca.crt
```

The CSR / bundle blocks go to stdout; the human hints go to stderr, so you can
also pipe them (`kioku-mesh tls request --san … | …`) without slicing out prose.
`tls sign` and `tls install` also read a block from a file argument
(`tls sign saved.csr`, `tls install bundle.txt`) or from `-o`/file output, so a
"move one file" flow works too.

### B. SSH `enroll` (one command)

If the peer can SSH to the CA host, `enroll` folds request → sign → install into
a single command. The CSR/cert ride through SSH's own encrypted channel; the CA
key stays on the CA host and the peer key stays on the peer.

```bash
# on the peer
kioku-mesh tls enroll user@hub --san 192.168.3.20
#   signing on user@hub over SSH ...
#   enrolled via user@hub: installed …/peer.crt + …/ca.crt
```

`enroll` runs `kioku-mesh tls sign` on the CA host over SSH. Useful options:
`--ssh-port`, `--ssh-opt 'StrictHostKeyChecking=accept-new'` (repeatable, passed
to `ssh -o`), `--remote-mesh` if the CLI is named differently there, and
`--days` for the cert lifetime. If `ssh` isn't available, fall back to the
copy-paste flow above.

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

The CA lives on this host, so the hub signs its own CSR locally — no copying
needed. `tls sign` reads the CSR `tls request` just wrote and prints a bundle;
pipe it straight into `tls install`:

```bash
kioku-mesh tls request --san 192.168.3.10 -o /tmp/hub.csr   # add --san <tailscale-ip> etc.
kioku-mesh tls sign /tmp/hub.csr -o /tmp/hub.bundle
kioku-mesh tls install /tmp/hub.bundle
```

### 3. On each spoke: enroll

With SSH to the hub, one command does it:

```bash
# on the spoke
kioku-mesh tls enroll hub --san 192.168.3.20
```

Without SSH, copy-paste the two blocks (see **Enrolling a peer → A** above):

```bash
# on the spoke
kioku-mesh tls request --san 192.168.3.20      # copy the CSR block
# on the hub
kioku-mesh tls sign                            # paste the CSR block, copy the bundle block
# back on the spoke
kioku-mesh tls install                         # paste the bundle block
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

> **Running under systemd?** After `init --tls --force` rewrites `zenohd.json5`,
> apply it with `systemctl --user restart <unit>` (e.g. `kioku-mesh-zenohd`).
> Don't also launch `zenohd -c ...` by hand — the second process would try to
> bind `7447` again and fail with an address-in-use error.

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
* `tls install` checks the signed cert against the local `peer.key`, so a cert
  minted for a different peer (validly CA-signed but the wrong key) is rejected
  here instead of failing later at the zenohd handshake.
* The replication block must still match byte-for-byte across peers — `--tls`
  only changes the transport/listen/connect sections, not replication.

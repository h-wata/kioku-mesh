# Windows host setup

> **Experimental — WSL2 strongly recommended.** Native Windows is not in CI;
> the `zenohd` Windows binary, RocksDB plugin, and firewall/service plumbing
> are user-maintained and may regress without notice. For a Windows
> workstation, **prefer running kioku-mesh inside WSL2** as a regular Linux
> peer (see the README Quick start). Keep WSL2 networking in `mirrored` mode
> (Windows 11 23H2+) so the WSL guest is reachable from other LAN peers on
> TCP/7447. The steps below remain for the rare case where a native
> Windows install is unavoidable (e.g. Claude Desktop on Windows, which
> cannot reach a WSL2 stdio MCP from the Windows host).

kioku-mesh development is Linux-first. Windows 10 / 11 hosts *can* join the
Zenoh mesh as peers natively; the steps below cover the differences from
the Linux quick start. Identity env vars, CLI commands, and MCP
registration all work the same — only the **path style** differs:
wherever the Linux examples reference `~/.venv/mesh-mem/bin/<binary>`,
the Windows equivalent is `C:\Users\<user>\.venv\kioku-mesh\Scripts\<binary>.exe`.

## 1. Install Python and kioku-mesh

- Install Python 3.10+ from python.org with **Add to PATH** checked. No
  admin rights are needed for the per-user installer.
- kioku-mesh is **not published on PyPI yet**. Install from a checkout:

  ```powershell
  git clone https://github.com/h-wata/kioku-mesh.git
  cd kioku-mesh
  python -m venv $env:USERPROFILE\.venv\kioku-mesh
  & "$env:USERPROFILE\.venv\kioku-mesh\Scripts\python.exe" -m pip install -e .
  ```

  (`pip install kioku-mesh` resolves to nothing today — the package will
  appear on PyPI as part of the v1.0 release.)

## 2. Install zenohd

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

## 3. Per-peer config

- Copy `config\zenohd_peer.json5.template` and replace `{SELF_IP}` /
  `{PEER_N_IP}` with real IPs. The walkthrough at
  [config/peers/example_5peer.md](../config/peers/example_5peer.md) applies
  unchanged; only the path style differs.
- Use forward slashes inside JSON5 string values for the rocksdb dir to
  avoid escaping headaches:

  ```json5
  // optional override; default storage dir is %LOCALAPPDATA%\kioku-mesh
  // when ZENOH_BACKEND_ROCKSDB_ROOT points there.
  // Forward slashes work on Windows in zenoh's config parser.
  ```

## 4. Run zenohd, optionally as a service

For interactive use:

```powershell
$env:ZENOH_BACKEND_ROCKSDB_ROOT = "$env:LOCALAPPDATA\kioku-mesh"
New-Item -ItemType Directory -Force -Path $env:ZENOH_BACKEND_ROCKSDB_ROOT | Out-Null
zenohd.exe --config C:\path\to\zenohd_peer.json5
```

For auto-start, register zenohd as a Windows service. NSSM
(Non-Sucking Service Manager, https://nssm.cc) handles stdout / stderr
logging more cleanly than `sc.exe`:

```powershell
nssm install zenohd "C:\Program Files\zenoh\zenohd.exe" "--config C:\path\to\zenohd_peer.json5"
nssm set     zenohd AppEnvironmentExtra "ZENOH_BACKEND_ROCKSDB_ROOT=C:\Users\<user>\AppData\Local\kioku-mesh"
nssm start   zenohd
```

## 5. Windows Defender Firewall

`New-NetFirewallRule` requires an **elevated PowerShell** — without
admin, it fails silently with `Access is denied.`. Right-click
PowerShell → "Run as administrator", or invoke
`Start-Process powershell -Verb RunAs` to trigger the UAC prompt:

```powershell
New-NetFirewallRule -DisplayName "kioku-mesh zenohd" `
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

## 6. Time sync (w32time)

Windows ships with the `w32time` service; kioku-mesh only needs sub-second
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

## 7. Data directory

From v0.2.1 onward, kioku-mesh resolves its state directory per OS:

- **Windows**: `%LOCALAPPDATA%\mesh-mem` (e.g. `C:\Users\<user>\AppData\Local\mesh-mem`) — via `platformdirs`
- **macOS**: `~/Library/Application Support/mesh-mem` — via `platformdirs`
- **Linux**: `~/.local/share/mesh-mem` (fixed, unchanged from v0.2.0;
  `XDG_DATA_HOME` is intentionally NOT honored to preserve the
  pre-v0.2.1 path and avoid silently migrating users who had set it)

To override (e.g. point at a faster NVMe):

```powershell
$env:MESH_MEM_STATE_DIR = "D:\kioku-mesh-state"
```

## 8. Smoke check

```powershell
$env:MESH_MEM_AGENT_FAMILY = "claude-code"
$env:MESH_MEM_CLIENT_ID    = "claude-windows-1"

kioku-mesh save "hello from windows" --project demo --memory-type note
# From any other peer:
kioku-mesh search "hello from windows" --project demo --limit 5
```

When this host is the **new** peer joining an established mesh, the
local SQLite index is empty and the in-process replication subscriber
will populate it as observations arrive — `save` / `search` against
freshly-published data work fine. To pull historical entries from the
existing peers' zenohd RocksDB into the local index in one shot, run
once with `--rebuild`:

```powershell
kioku-mesh --rebuild status   # one-time alignment scan
```

Expect the rebuild to take **tens of seconds** on a populated mesh
(observed ~15 s against ~117k records). Subsequent CLI invocations
default to skipping that scan (#38) so interactive use stays
sub-second.

## Known limitations

- **No CI coverage.** kioku-mesh's CI runs Linux-only; native Windows
  regressions are caught by the user at run time, not in pre-merge tests.
  This is the primary reason the section above is marked Experimental.
- WSL2: a Windows-host zenohd is reachable from WSL only when the WSL
  network mode is set to `mirrored` (Windows 11 23H2+) or you forward
  TCP/7447 manually. The default `nat` mode hides Windows from the WSL
  guest. (If you can run kioku-mesh inside WSL2 instead, do that.)
- Search latency on Windows mirrors Linux for the SQLite-first path
  (per-OS layer is just `pathlib`); the v0.2.0 benchmark numbers carry
  over.
- Claude Desktop on Windows cannot launch an MCP server that lives inside
  WSL2 over stdio. If you need Desktop integration on Windows, the native
  install above is currently the only path — accept the experimental
  caveats.

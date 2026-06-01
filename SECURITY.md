# Security policy

## Supported versions

kioku-mesh is in `0.x`. Only the most recent **minor** release receives security
fixes; older minors are EOL the moment a new minor ships. Patch releases on the
current minor are issued as needed.

| Version | Supported |
|---------|-----------|
| 0.4.x   | ✅        |
| < 0.4   | ❌        |

## Reporting a vulnerability

Please **do not open a public issue** for security reports.

Use GitHub's private vulnerability reporting:
<https://github.com/h-wata/kioku-mesh/security/advisories/new>

This sends the report to the maintainer in a private discussion thread that
only becomes a public advisory once a fix ships.

Please include, where possible:

- the version (`kioku-mesh --version`) and the deployment mode (local-only,
  hub, spoke, mTLS on/off)
- a minimal reproduction or proof-of-concept
- the impact you believe it has

## What's in scope

- The `kioku-mesh` CLI, the MCP server (`kioku-mesh-mcp`), and the
  `mesh_mem` Python package.
- The mTLS / private-CA workflow under `kioku-mesh tls` and its on-disk
  material under `~/.config/kioku-mesh/tls/`.
- The replication and storage paths exposed to other mesh peers.

## What's out of scope

- The trusted-network assumption documented in `README.md` and
  `docs/mtls.md`: kioku-mesh assumes the underlying mesh (LAN, Tailscale,
  WireGuard, etc.) is itself trusted, with mTLS as defense-in-depth.
  Attacks that require already being on the trusted network and abusing
  that legitimate access are tracked as feature requests, not security
  bugs, unless they let an authenticated peer escalate beyond their
  intended capabilities.
- Issues in upstream dependencies (Zenoh, cryptography, etc.) — please
  report those upstream first.

## Response expectations

This is a single-maintainer project; reports will be acknowledged within
roughly **one week** and a fix or workaround will ship on the next patch
release once a vulnerability is confirmed.

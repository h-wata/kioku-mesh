# Tier 1 Fix-Forward Design Notes (#112 post-merge)

Date: 2026-05-22
Author: W2 (fix-forward per TASK-206)

> **2026-05-26 update:** "Tier 1" has been removed from the public README as a
> first-class architecture tier (Tier 0 / 1 / 2 → Local / Mesh, with the
> in-process router demoted to a "try mesh without zenohd" demo path). The
> code paths (`mesh start` / `mesh join`) and the analysis below remain
> accurate. See ADR-0013 for the naming-change rationale.

## Problem Statement (B1)

The original `mesh start` implementation opened an in-process zenoh router
(`mode=router`) but did NOT start an index subscriber within that process.
As a result:

- Peer saves published via `ZENOH_CONNECT=<router> mesh-mem save ...` reached
  the router over the network, but the router process had no subscriber to
  write those observations into its local SQLite index.
- `ZENOH_CONNECT=<router> mesh-mem search ...` from the router process read
  from an empty local SQLite — the observations were never written there.
- The e2e test (`test_2process_save_search`) read from the **peer's own** SQLite
  (same process context after save), which is why it passed despite the actual
  mesh exchange not working.

## Fix: `mesh start` starts index subscriber after router session opens

### Core change

After opening the router session, `_cmd_mesh_start` sets `ZENOH_CONNECT` to the
router's own loopback address and calls `get_index()`. This triggers:

1. `get_session()` → opens a **client** session to the router (same process)
2. `start_index_subscriber(session)` → subscribes to `mem/obs/**` / `mem/tomb/**`
3. Any peer save published to the router is received by this subscriber and
   written to the router process's local SQLite index.

### Why this doesn't violate §6 guardrails

- `store.put_observation()` is unchanged — no tier-specific branch.
- The change is confined to **session construction / initialization** in the
  `mesh start` command.
- The `MemoryBackend` abstraction (`get_backend()`) is unaffected.

### `0.0.0.0` → `127.0.0.1` translation

When `--listen tcp/0.0.0.0:PORT` is used, the router binds all interfaces.
The self-connect endpoint for the index subscriber replaces `0.0.0.0` with
`127.0.0.1` so the client session uses the loopback interface.

## Fix: `mesh join` becomes foreground + index subscriber

The original `mesh join` opened a peer session, printed a message, then
immediately `close()`d the session. This was useful for connectivity
verification only.

New behavior:
- Opens peer session and sets `ZENOH_CONNECT` to the peer endpoint.
- Calls `get_index()` to start an index subscriber on the peer session.
- Holds foreground (signal.pause) until Ctrl-C.

This enables `mesh join` to serve as a long-running background process that
accumulates mesh observations into the local SQLite, which can then be queried
via `ZENOH_CONNECT=<peer> mesh-mem search`.

## B2: doctor connected peers

Investigation result: `session.info.peers_zid()` returns the ZIDs of peers
connected to a **router** session. This API is available in zenoh-python 1.9.0.

However, `check_embedded_router()` in `doctor.py` uses a TCP probe (a short-lived
socket connection). It does not hold a zenoh session.

Fix approach:
- `check_embedded_router()` opens a short-lived **peer** session to the router
  endpoint, reads `session.info.routers_zid()` to confirm the router is
  accepting zenoh connections (not just TCP), then closes the session.
- The router's peer count (from `router_session.info.peers_zid()`) is NOT
  available from an external probe — it requires access to the router session
  object itself, which lives in a separate process.
- `details` will include `connected_to_router: bool` and `router_zids: list`
  (the ZIDs the probe session sees as routers), plus a note that in-process
  peer counting is not available via external probe.

This partially satisfies Issue #112 AC "connected peers" — we report whether
the router is accepting zenoh connections and its ZID identity, which is the
observable subset from an external probe.

## Tier 2 (#113) dependency

Tier 2 can only begin once Tier 1's actual mesh exchange (peer save → router
search round-trip) is verified. This fix unblocks TASK-203 (W1).

## §6 guardrails compliance

- `store.put_observation()`: no tier branch added ✓
- `wheel`: no zenohd/rocksdb bundled ✓
- `demo` subcommand (#108): not touched ✓
- Existing zenoh mode tests: not broken ✓

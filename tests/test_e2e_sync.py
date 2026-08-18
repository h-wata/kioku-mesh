"""End-to-end tests across two linked zenohd routers.

Acceptance scenarios for PoC Step 4/5:

1. ``test_offline_diff_sync``: observation published on router A while B is
   down must appear on B after B restarts and the next replication tick.
   To prove the data reached B's local storage (and not just via live query
   forwarding from A), A is stopped before the final query on B.

2. ``test_tombstone_propagates_across_split_brain``: a tombstone issued on
   A while B is offline must, once B rejoins, mask the corresponding
   observation on B even when A is no longer reachable.

These tests use the session-scoped ``dual_zenohd`` fixture (one pair of
routers for the whole session). ``_dual_fresh_state`` restarts both sides
between tests so the memory-volume state is reset — memory is non-durable
so a clean restart yields a clean slate.

Two things make the waits here subtle:

* Every hop between routers re-opens the store session, and a put issued in
  the window before the fresh session's declarations reach the router is
  dropped, not queued (measured: the sample never arrives). ``_point_store_at``
  therefore hands the session to ``handshake`` before returning it.
* A query issued on B is answered by *either* router's storage while both are
  up, so "B can see it" is not evidence that B holds a local replica. The
  replies carry the answering router's zid, so ``_wait_for_local_replica``
  waits for a reply from B itself — which is the property these tests then
  verify by stopping A.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from typing import Any

import pytest
import zenoh

from kioku_mesh import store
from kioku_mesh.models import Observation

from .wait_helpers import handshake
from .wait_helpers import storage_has
from .wait_helpers import wait_until

# Upper bound for replication to align a restarted peer, not an expected
# duration: the waits below return as soon as the replica is in place.
# ``interval`` is 2.0s and ``propagation_delay`` 0.25s, so alignment normally
# lands within a couple of ticks.
REPLICATION_TIMEOUT = 30.0


def _point_store_at(endpoint: str) -> Any:
    """Re-point the store session at ``endpoint`` and return it, path proven live."""
    os.environ['ZENOH_CONNECT'] = endpoint
    store._reset_session()
    sess = store.get_session()
    canary = _mk_obs('handshake canary', project='_handshake')
    handshake(
        lambda: sess.put(canary.key_expr, canary.to_json()),
        lambda: storage_has(sess, canary.key_expr),
        f'the store session -> {endpoint} storage path',
    )
    return sess


def _put_via(handle: Any, obs: Observation) -> None:
    _point_store_at(handle.endpoint)
    store.put_observation(obs)


def _tomb_via(handle: Any, obs: Observation) -> None:
    _point_store_at(handle.endpoint)
    store.put_tombstone(obs, reason='e2e')


def _search_via(handle: Any, **kwargs: Any) -> list[Observation]:
    _point_store_at(handle.endpoint)
    return store.search_observations(**kwargs)


def _wait_for_local_replica(handle: Any, key_expr: str, what: str) -> None:
    """Wait until ``handle``'s own storage answers a get on ``key_expr``.

    Replies carry the zid of the session that answered. A client connected to
    a single router only ever has that router as its direct peer, so a reply
    tagged with that zid came from this side's storage rather than being
    forwarded from the other router — exactly the condition the tests below
    check by stopping the other side.

    ``consolidation=NONE`` is essential: under the default consolidation both
    storages' copies collapse into one reply, and the surviving one is not
    necessarily this side's — the local replica would stay invisible until the
    peer is stopped, which is exactly what we are trying to check in advance.
    """
    sess = _point_store_at(handle.endpoint)
    local_zids = {str(z) for z in sess.info.routers_zid()}

    def replicated() -> bool:
        replies = sess.get(key_expr, timeout=2.0, consolidation=zenoh.ConsolidationMode.NONE)
        return any(str(r.replier_id.zid) in local_zids for r in replies if r.ok)

    wait_until(replicated, what, timeout=REPLICATION_TIMEOUT)


def _mk_obs(content: str, project: str) -> Observation:
    return Observation(
        content=content,
        agent_family='claude',
        client_id='claude-code',
        pc_id='e2e-pc',
        session_id='e2e-sess',
        project=project,
    )


@pytest.fixture(autouse=True)
def _dual_fresh_state(dual_zenohd: Any) -> Iterator[None]:
    """Restart both routers between tests so memory-volume state is clean.

    Also snapshots and restores ``ZENOH_CONNECT`` so the dual-router endpoint
    does not leak into later test modules (``test_mcp_server`` /
    ``test_mcp_cli`` request ``single_zenohd`` and expect its endpoint).
    """
    orig_connect = os.environ.get('ZENOH_CONNECT')
    a, b = dual_zenohd.a, dual_zenohd.b
    # Stopping both then starting both is the only reliable reset: a lone
    # restart would immediately be re-populated by the still-running peer
    # through replication.
    a.stop()
    b.stop()
    a.start()
    b.start()
    # No settle here: ``start`` already blocks until each router accepts client
    # sessions, and every wait in the tests below is a condition with its own
    # timeout, so a slow peer link is absorbed rather than raced against.
    try:
        yield
    finally:
        # Post-test: make sure both are running for the next test's reset cycle.
        if not a.running:
            a.start()
        if not b.running:
            b.start()
        # Restore ZENOH_CONNECT to whatever was set before this test so the
        # next test module (e.g. single_zenohd-backed tests) doesn't inherit
        # the dual endpoint. The cached store session is also reset so the
        # next call reopens against the restored endpoint.
        if orig_connect is None:
            os.environ.pop('ZENOH_CONNECT', None)
        else:
            os.environ['ZENOH_CONNECT'] = orig_connect
        store._reset_session()


def test_offline_diff_sync(dual_zenohd: Any) -> None:
    a, b = dual_zenohd.a, dual_zenohd.b

    # 1. B is offline; A publishes an observation.
    b.stop()
    obs = _mk_obs('stored on A while B offline', project='offline-diff')
    _put_via(a, obs)

    # 2. B rejoins; wait for replication to copy the observation into B's storage.
    b.start()
    _wait_for_local_replica(b, obs.key_expr, "B's own storage to hold the obs published while B was offline")

    # 3. Stop A so any subsequent hit MUST come from B's local replica.
    a.stop()

    results = _search_via(b, project='offline-diff')
    assert obs.observation_id in [r.observation_id for r in results], (
        f'expected {obs.observation_id} in B after offline-diff sync, got {[r.observation_id for r in results]}'
    )


def test_tombstone_propagates_across_split_brain(dual_zenohd: Any) -> None:
    a, b = dual_zenohd.a, dual_zenohd.b

    # 1. Both up; A publishes X. Wait until X is readable through B — while
    # both sides are up this may still be answered by A's storage, which is
    # all this precondition needs: it only establishes that X exists on the
    # mesh before the split.
    obs = _mk_obs('about to be tombstoned during split', project='split-tomb')
    _put_via(a, obs)
    wait_until(
        lambda: obs.observation_id in [r.observation_id for r in _search_via(b, project='split-tomb')],
        'X to be readable through B before the split',
        timeout=REPLICATION_TIMEOUT,
    )

    # Sanity: X is visible via B.
    pre = _search_via(b, project='split-tomb')
    assert obs.observation_id in [r.observation_id for r in pre], (
        'precondition failed: X did not replicate to B before split'
    )

    # 2. Split: B goes down.
    b.stop()

    # 3. A publishes the tombstone while B is offline.
    _tomb_via(a, obs)

    # 4. B rejoins; wait for replication to ship the tomb into B.
    b.start()
    _wait_for_local_replica(
        b,
        obs.tombstone_key_expr(),
        "B's own storage to hold the tombstone issued while B was offline",
    )

    # 5. Stop A so the search on B must read B's local state only.
    a.stop()

    post = _search_via(b, project='split-tomb')
    assert obs.observation_id not in [r.observation_id for r in post], (
        f'tombstone did not propagate: X still visible on B (results={[r.observation_id for r in post]})'
    )

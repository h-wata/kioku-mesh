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

Replication wait: ``interval`` (2.0s) + ``propagation_delay`` (0.25s) +
one tick of slack. We wait 5s which also absorbs initial link settling.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
import time
from typing import Any

import pytest

from mesh_mem import store
from mesh_mem.models import Observation

REPLICATION_WAIT = 5.0


def _point_store_at(endpoint: str) -> None:
    os.environ['ZENOH_CONNECT'] = endpoint
    store._reset_session()


def _put_via(handle: Any, obs: Observation) -> None:
    _point_store_at(handle.endpoint)
    store.put_observation(obs)


def _tomb_via(handle: Any, obs: Observation) -> None:
    _point_store_at(handle.endpoint)
    store.put_tombstone(obs, reason='e2e')


def _search_via(handle: Any, **kwargs: Any) -> list[Observation]:
    _point_store_at(handle.endpoint)
    return store.search_observations(**kwargs)


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
    """Restart both routers between tests so memory-volume state is clean."""
    a, b = dual_zenohd.a, dual_zenohd.b
    # Stopping both then starting both is the only reliable reset: a lone
    # restart would immediately be re-populated by the still-running peer
    # through replication.
    a.stop()
    b.stop()
    a.start()
    b.start()
    # Small settle for the peer link to re-establish before the test begins.
    time.sleep(0.5)
    yield
    # Post-test: make sure both are running for the next test's reset cycle.
    if not a.running:
        a.start()
    if not b.running:
        b.start()


def test_offline_diff_sync(dual_zenohd: Any) -> None:
    a, b = dual_zenohd.a, dual_zenohd.b

    # 1. B is offline; A publishes an observation.
    b.stop()
    obs = _mk_obs('stored on A while B offline', project='offline-diff')
    _put_via(a, obs)

    # 2. B rejoins; wait for replication to copy the observation into B's storage.
    b.start()
    time.sleep(REPLICATION_WAIT)

    # 3. Stop A so any subsequent hit MUST come from B's local replica.
    a.stop()

    results = _search_via(b, project='offline-diff')
    assert obs.observation_id in [
        r.observation_id for r in results
    ], f'expected {obs.observation_id} in B after offline-diff sync, got {[r.observation_id for r in results]}'


def test_tombstone_propagates_across_split_brain(dual_zenohd: Any) -> None:
    a, b = dual_zenohd.a, dual_zenohd.b

    # 1. Both up; A publishes X. Give the mesh a beat to replicate X to B.
    obs = _mk_obs('about to be tombstoned during split', project='split-tomb')
    _put_via(a, obs)
    time.sleep(REPLICATION_WAIT)

    # Sanity: X is visible via B.
    pre = _search_via(b, project='split-tomb')
    assert obs.observation_id in [
        r.observation_id for r in pre
    ], 'precondition failed: X did not replicate to B before split'

    # 2. Split: B goes down.
    b.stop()

    # 3. A publishes the tombstone while B is offline.
    _tomb_via(a, obs)

    # 4. B rejoins; wait for replication to ship the tomb into B.
    b.start()
    time.sleep(REPLICATION_WAIT)

    # 5. Stop A so the search on B must read B's local state only.
    a.stop()

    post = _search_via(b, project='split-tomb')
    assert obs.observation_id not in [
        r.observation_id for r in post
    ], f'tombstone did not propagate: X still visible on B (results={[r.observation_id for r in post]})'

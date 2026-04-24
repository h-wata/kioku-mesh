"""Integration tests against a single local zenohd router.

Covers the happy path (save → search), tombstone hiding, and session
reconnect after ``store._reset_session()``. Requires the ``zenohd`` binary
on PATH; the ``single_zenohd`` fixture handles spawn / teardown.
"""

from __future__ import annotations

from collections.abc import Iterator
import time
from typing import Any

import pytest

from mesh_mem import store
from mesh_mem.models import Observation


def _mk_obs(
    content: str,
    *,
    project: str = '',
    tags: list[str] | None = None,
    agent_family: str = 'claude',
    client_id: str = 'claude-code',
    pc_id: str = 'testpc',
    session_id: str = 'testsession',
) -> Observation:
    """Build an Observation with fully-explicit identity so tests are deterministic."""
    return Observation(
        content=content,
        agent_family=agent_family,
        client_id=client_id,
        pc_id=pc_id,
        session_id=session_id,
        project=project,
        tags=list(tags or []),
    )


def test_save_then_search_roundtrip(single_zenohd: Any) -> None:  # noqa: ARG001 — fixture side-effect
    obs = _mk_obs('hello mesh', project='demo', tags=['a', 'b'])
    store.put_observation(obs)

    results = store.search_observations(query='hello', project='demo')
    ids = [r.observation_id for r in results]
    assert obs.observation_id in ids
    hit = next(r for r in results if r.observation_id == obs.observation_id)
    assert hit.content == 'hello mesh'
    assert hit.project == 'demo'
    assert set(hit.tags) == {'a', 'b'}


def test_tombstone_hides_observation(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('about to be tombstoned', project='tomb-demo')
    store.put_observation(obs)

    # Sanity: visible before tombstone.
    pre = store.search_observations(project='tomb-demo')
    assert obs.observation_id in [r.observation_id for r in pre]

    store.put_tombstone(obs, reason='test')

    post = store.search_observations(project='tomb-demo')
    assert obs.observation_id not in [r.observation_id for r in post]

    # ``find_observation_by_id`` scans raw ``mem/obs/**`` and intentionally
    # bypasses tombstone filtering — the obs document itself is immutable and
    # should still be locatable for forensic purposes.
    raw = store.find_observation_by_id(obs.observation_id)
    assert raw is not None
    assert raw.observation_id == obs.observation_id


def test_reconnect_after_session_reset(single_zenohd: Any) -> None:  # noqa: ARG001
    obs1 = _mk_obs('before reset', project='reconnect')
    store.put_observation(obs1)

    # Simulate a transport blip: drop the cached client session.
    store._reset_session()

    obs2 = _mk_obs('after reset', project='reconnect')
    store.put_observation(obs2)

    # Give the storage a moment to absorb the second put before querying.
    time.sleep(0.1)

    results = store.search_observations(project='reconnect')
    ids = {r.observation_id for r in results}
    assert obs1.observation_id in ids
    assert obs2.observation_id in ids


def test_search_respects_project_filter(single_zenohd: Any) -> None:  # noqa: ARG001
    keep = _mk_obs('project keep', project='alpha')
    drop = _mk_obs('project drop', project='beta')
    store.put_observation(keep)
    store.put_observation(drop)

    results = store.search_observations(project='alpha')
    ids = {r.observation_id for r in results}
    assert keep.observation_id in ids
    assert drop.observation_id not in ids


def test_search_empty_returns_empty_not_error(single_zenohd: Any) -> None:  # noqa: ARG001
    # A project nobody has written to should yield [] without raising.
    results = store.search_observations(project='project-that-does-not-exist')
    assert results == []


@pytest.fixture(autouse=True)
def _purge_router_state(single_zenohd: Any) -> Iterator[None]:  # noqa: ARG001
    """Wipe ``mem/**`` between tests so memory-volume state does not bleed across tests.

    The ``single_zenohd`` fixture is session-scoped (expensive to restart), so
    test isolation must happen at the data layer. We enumerate every key under
    ``mem/obs/**`` and ``mem/tomb/**`` and delete them individually — wildcard
    ``session.delete`` semantics vary by storage backend, per-key delete is
    portable.
    """
    sess = store.get_session()
    for prefix in ('mem/obs/**', 'mem/tomb/**'):
        keys: list[str] = []
        for reply in sess.get(prefix, timeout=2.0):
            if reply.ok:
                keys.append(str(reply.ok.key_expr))
        for k in keys:
            sess.delete(k)
    # Storage absorbs deletes asynchronously — give it a beat before the test
    # starts reading. 150ms is well under the 5s GET timeout.
    time.sleep(0.15)
    yield

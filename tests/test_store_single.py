"""Integration tests against a single local zenohd router.

Covers the happy path (save → search), tombstone hiding, and session
reconnect after ``store._reset_session()``. Requires the ``zenohd`` binary
on PATH; the ``single_zenohd`` fixture handles spawn / teardown.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

from mesh_mem import store
from mesh_mem.models import Observation

# Zenoh put / delete is asynchronous — ingestion by the storage plugin happens
# on its own thread. We settle briefly after any write before querying so
# read-your-writes consistency is honored. 250ms is well below the 5s GET
# timeout and empirically enough to soak up the queue even when other session
# fixtures (dual_zenohd) run in parallel.
_INGEST_SETTLE = 0.25


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
    time.sleep(_INGEST_SETTLE)

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
    time.sleep(_INGEST_SETTLE)

    # Sanity: visible before tombstone.
    pre = store.search_observations(project='tomb-demo')
    assert obs.observation_id in [r.observation_id for r in pre]

    store.put_tombstone(obs, reason='test')
    time.sleep(_INGEST_SETTLE)

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
    time.sleep(_INGEST_SETTLE)

    results = store.search_observations(project='reconnect')
    ids = {r.observation_id for r in results}
    assert obs1.observation_id in ids
    assert obs2.observation_id in ids


def test_search_respects_project_filter(single_zenohd: Any) -> None:  # noqa: ARG001
    keep = _mk_obs('project keep', project='alpha')
    drop = _mk_obs('project drop', project='beta')
    store.put_observation(keep)
    store.put_observation(drop)
    time.sleep(_INGEST_SETTLE)

    results = store.search_observations(project='alpha')
    ids = {r.observation_id for r in results}
    assert keep.observation_id in ids
    assert drop.observation_id not in ids


def test_search_empty_returns_empty_not_error(single_zenohd: Any) -> None:  # noqa: ARG001
    # A project nobody has written to should yield [] without raising.
    results = store.search_observations(project='project-that-does-not-exist')
    assert results == []


def test_search_respects_session_id_filter(single_zenohd: Any) -> None:  # noqa: ARG001
    keep = _mk_obs('session keep', session_id='session-alpha')
    drop = _mk_obs('session drop', session_id='session-beta')
    store.put_observation(keep)
    store.put_observation(drop)
    time.sleep(_INGEST_SETTLE)

    results = store.search_observations(session_id='session-alpha')
    ids = {r.observation_id for r in results}
    assert keep.observation_id in ids
    assert drop.observation_id not in ids


def test_search_respects_since_iso_filter(single_zenohd: Any) -> None:  # noqa: ARG001
    old = dataclasses.replace(
        _mk_obs('old observation', project='since-test'),
        created_at='2020-01-01T00:00:00.000000Z',
    )
    recent = dataclasses.replace(
        _mk_obs('recent observation', project='since-test'),
        created_at='2025-06-01T00:00:00.000000Z',
    )
    store.put_observation(old)
    store.put_observation(recent)
    time.sleep(_INGEST_SETTLE)

    results = store.search_observations(project='since-test', since_iso='2024-01-01T00:00:00Z')
    ids = {r.observation_id for r in results}
    assert recent.observation_id in ids
    assert old.observation_id not in ids


def test_search_uses_sqlite_index_by_default(single_zenohd: Any) -> None:  # noqa: ARG001
    """Phase 3: ``search_observations`` reads from the local SQLite sidecar.

    Inject a sentinel row directly into the SQLite index without going
    through Zenoh, then verify ``search_observations`` returns it. If the
    index were not on the read path, this row would never appear.
    """
    obs = _mk_obs('sqlite-only sentinel', project='index-default')
    # Bypass put_observation (which writes to both Zenoh and the index)
    # by upserting straight into the LocalIndex.
    store.get_index().upsert(obs)

    results = store.search_observations(project='index-default')
    ids = {r.observation_id for r in results}
    assert obs.observation_id in ids, 'SQLite-first read path must surface index-only rows'


def test_search_falls_back_to_zenoh_when_index_disabled(
    monkeypatch: Any,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """``MESH_MEM_DISABLE_INDEX=1`` routes ``search_observations`` to Zenoh.

    Confirmed by writing a row only to Zenoh (LocalIndex disabled) and
    verifying it is still searchable. The flip side of the test above:
    when the index is unavailable, the legacy full-scan path remains.
    """
    monkeypatch.setenv('MESH_MEM_DISABLE_INDEX', '1')
    store._reset_index()  # pick up the env change

    obs = _mk_obs('zenoh-fallback row', project='fallback')
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    results = store.search_observations(project='fallback')
    ids = {r.observation_id for r in results}
    assert obs.observation_id in ids


def test_find_by_id_uses_sqlite_index_then_falls_back(single_zenohd: Any) -> None:  # noqa: ARG001
    """``find_observation_by_id`` hits the index first, then Zenoh on miss.

    Two cases covered:
        1. Row only in the SQLite index — index hit returns it.
        2. Row only in Zenoh (deleted from index) — Zenoh fallback finds it.
    """
    # Case 1: index-only sentinel.
    only_in_index = _mk_obs('index-hit', project='find-routing')
    store.get_index().upsert(only_in_index)
    hit = store.find_observation_by_id(only_in_index.observation_id)
    assert hit is not None
    assert hit.content == 'index-hit'

    # Case 2: row in Zenoh but absent from the index (simulate by deleting
    # the row from the sidecar after put). The Zenoh fallback must locate
    # it so the delete / gc paths still work for pre-Phase-2 observations.
    zenoh_only = _mk_obs('zenoh-only', project='find-routing')
    store.put_observation(zenoh_only)
    time.sleep(_INGEST_SETTLE)
    store.get_index().physical_delete(zenoh_only.observation_id)

    hit2 = store.find_observation_by_id(zenoh_only.observation_id)
    assert hit2 is not None
    assert hit2.content == 'zenoh-only'


def test_is_mesh_ready_returns_false_immediately_after_start(single_zenohd: Any) -> None:  # noqa: ARG001
    """is_mesh_ready returns False when the probe just completed (min_ready_sec not elapsed)."""
    # Inject a probe-success timestamp that is "just now" so the elapsed time
    # is effectively 0, well below any positive min_ready_sec.
    store._mesh_first_probe_success = time.monotonic()
    assert store.is_mesh_ready(min_ready_sec=1000.0) is False


def test_is_mesh_ready_returns_true_after_probe(single_zenohd: Any) -> None:  # noqa: ARG001
    """is_mesh_ready returns True once min_ready_sec has elapsed since first probe."""
    # Simulate a probe that completed 10 seconds ago.
    store._mesh_first_probe_success = time.monotonic() - 10.0
    assert store.is_mesh_ready(min_ready_sec=5.0) is True

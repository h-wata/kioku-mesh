"""Tests for Phase 4: startup rebuild and replication subscriber.

test_startup_rebuild_runs_when_index_empty and the two subscriber tests
require a live zenohd (single_zenohd fixture). The env-var skip test
is a pure unit test and does not need a router.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
import zenoh

from mesh_mem import store
from mesh_mem.models import Observation
from mesh_mem.models import Tombstone

_SETTLE = 0.4  # seconds to wait for async subscriber delivery


def _mk_obs(content: str, *, project: str = 'sub-test') -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test-client',
        pc_id='test-pc',
        session_id='test-session',
    )


def _remote_session(endpoint: str) -> zenoh.Session:
    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"client"')
    cfg.insert_json5('connect/endpoints', f'["{endpoint}"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    return zenoh.open(cfg)


def test_subscriber_picks_up_remote_put_into_index(single_zenohd: Any) -> None:
    """A put from a remote session lands in the local index via the subscriber."""
    idx = store.get_index()
    assert not idx.disabled

    obs = _mk_obs('replicated content', project='sub-obs')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, obs.to_json())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    ids = {r.observation_id for r in idx.search(project='sub-obs')}
    assert obs.observation_id in ids, 'subscriber must upsert replicated obs into index'


def test_subscriber_picks_up_remote_tombstone(single_zenohd: Any) -> None:
    """A tombstone published by a remote session marks the index row deleted."""
    idx = store.get_index()
    obs = _mk_obs('will be remote-deleted', project='sub-tomb')
    idx.upsert(obs)

    tomb = Tombstone(observation_id=obs.observation_id)
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.tombstone_key_expr(), tomb.to_json())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert idx.search(project='sub-tomb') == [], 'subscriber must mark row deleted'


def test_startup_rebuild_runs_when_index_empty(single_zenohd: Any) -> None:  # noqa: ARG001
    """After index reset, get_index triggers rebuild from zenoh."""
    obs = _mk_obs('pre-existing in zenoh', project='rebuild-start')
    store.put_observation(obs)
    time.sleep(0.25)

    # Simulate restart: clear the index (and subscriber).
    store._reset_index()

    # Next call to get_index should trigger rebuild from zenoh.
    results = store.search_observations(project='rebuild-start')
    ids = {r.observation_id for r in results}
    assert obs.observation_id in ids, 'rebuild must repopulate index from zenoh'


def test_startup_rebuild_skipped_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """MESH_MEM_SKIP_REBUILD=1 prevents rebuild_from_zenoh from running on init."""
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []
    orig = LocalIndex.rebuild_from_zenoh

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return orig(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('MESH_MEM_SKIP_REBUILD', '1')

    store._reset_index()  # force re-init on next get_index() call
    store.get_index()  # triggers startup logic; session is available via single_zenohd

    assert not rebuild_calls, 'rebuild_from_zenoh must not be called when MESH_MEM_SKIP_REBUILD=1'

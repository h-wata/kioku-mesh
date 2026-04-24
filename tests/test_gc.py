"""Integration tests for the gc path (physical delete + retention sweep)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import time
from typing import Any

from mesh_mem import store
from mesh_mem.models import Observation
from mesh_mem.models import Tombstone

# Zenoh ``put`` is asynchronous — the storage plugin ingests on its own thread.
# We sleep briefly after any put sequence before querying via ``_list_tombstones``
# / ``find_observation_by_id`` so the scan sees the latest state. 100ms is well
# below the 5s GET timeout and well above typical memory-backend ingestion.
_INGEST_SETTLE = 0.25


def _mk_obs(content: str, project: str = 'gc-demo') -> Observation:
    return Observation(
        content=content,
        agent_family='claude',
        client_id='claude-code',
        pc_id='gc-pc',
        session_id='gc-sess',
        project=project,
    )


def _obs_present(observation_id: str) -> bool:
    return store.find_observation_by_id(observation_id) is not None


def _tomb_keys_for(observation_id: str) -> list[str]:
    return [k for k, t in store._list_tombstones() if t.observation_id == observation_id]


def test_force_id_purges_obs_and_tomb(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('to be force-purged')
    store.put_observation(obs)
    store.put_tombstone(obs, reason='force-test')
    time.sleep(_INGEST_SETTLE)

    # Sanity: both keys present.
    assert _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) != []

    obs_removed, tomb_removed = store.physical_delete_observation(obs.observation_id)
    assert obs_removed is True
    assert tomb_removed is True

    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) == []


def test_force_id_on_missing_id_reports_nothing_to_purge(single_zenohd: Any) -> None:  # noqa: ARG001
    # 32-char id that was never put anywhere.
    phantom_id = 'f' * 32
    obs_removed, tomb_removed = store.physical_delete_observation(phantom_id)
    assert obs_removed is False
    assert tomb_removed is False


def test_force_id_sweeps_orphan_tombstone(single_zenohd: Any) -> None:  # noqa: ARG001
    """Tomb without a matching obs (e.g. replicated tomb arrived but obs did not) still gets purged."""
    obs = _mk_obs('orphan tomb target')
    # Emit only the tombstone; never put the observation.
    sess = store.get_session()
    tomb = Tombstone(observation_id=obs.observation_id, reason='orphan')
    sess.put(obs.tombstone_key_expr(), tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    assert _tomb_keys_for(obs.observation_id) != []
    assert not _obs_present(obs.observation_id)

    obs_removed, tomb_removed = store.physical_delete_observation(obs.observation_id)
    assert obs_removed is False
    assert tomb_removed is True
    time.sleep(_INGEST_SETTLE)
    assert _tomb_keys_for(obs.observation_id) == []


def test_retention_purges_aged_tombstone(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('aged — should be purged by retention')
    store.put_observation(obs)

    # Manually emit a back-dated tombstone so we do not have to sleep through retention.
    aged = datetime.now(timezone.utc) - timedelta(days=60)
    aged_tomb = Tombstone(
        observation_id=obs.observation_id,
        reason='aged',
        deleted_at=aged.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    )
    store.get_session().put(obs.tombstone_key_expr(), aged_tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30)
    assert purged >= 1
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) == []


def test_retention_keeps_fresh_tombstone(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('fresh — retention should NOT sweep')
    store.put_observation(obs)
    store.put_tombstone(obs, reason='fresh')
    time.sleep(_INGEST_SETTLE)

    before = len(_tomb_keys_for(obs.observation_id))
    assert before >= 1

    purged = store.gc_expired_tombstones(retention_days=30)
    assert purged == 0
    # Tomb and obs must both still be on the router.
    assert _obs_present(obs.observation_id)
    assert len(_tomb_keys_for(obs.observation_id)) == before


def test_retention_with_injected_now_is_deterministic(single_zenohd: Any) -> None:  # noqa: ARG001
    """``now`` override lets us express retention cutoffs without real sleep."""
    obs = _mk_obs('injected-now test')
    store.put_observation(obs)
    # Tomb dated 10 days ago.
    ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
    tomb = Tombstone(
        observation_id=obs.observation_id,
        deleted_at=ten_days_ago.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    )
    store.get_session().put(obs.tombstone_key_expr(), tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    # retention=30 with now=today → tomb is 10d old, within retention → kept.
    purged_kept = store.gc_expired_tombstones(retention_days=30)
    assert purged_kept == 0
    assert _obs_present(obs.observation_id)

    # retention=30 with now=40d in future → tomb is 50d old → purged.
    future = datetime.now(timezone.utc) + timedelta(days=40)
    purged = store.gc_expired_tombstones(retention_days=30, now=future)
    assert purged == 1
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)


def test_retention_skips_unparseable_deleted_at(single_zenohd: Any) -> None:  # noqa: ARG001
    """A tombstone with a garbled ``deleted_at`` is kept (conservative)."""
    obs = _mk_obs('garbled deleted_at')
    store.put_observation(obs)
    bad_tomb = Tombstone(observation_id=obs.observation_id, deleted_at='not-a-date')
    store.get_session().put(obs.tombstone_key_expr(), bad_tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=0)  # cutoff = now
    assert purged == 0
    # Both still present — bad timestamp should not unlock purge.
    assert _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) != []

"""Integration tests for the gc path (physical delete + retention sweep)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import time
from typing import Any

import pytest

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


def test_force_id_broadcast_reaches_unlocatable_key(single_zenohd: Any) -> None:  # noqa: ARG001
    """Wildcard broadcast purges an obs key that ``find_observation_by_id`` cannot locate.

    Simulates the split-brain edge case codex flagged on b09b82c: a key is
    present on the mesh but the live query cannot materialize it as an
    :class:`Observation` (here: malformed JSON). The per-key delete path is
    therefore skipped, and only the wildcard broadcast stops the key from
    surviving the emergency purge.
    """
    obs_id = '0' * 32
    malformed_key = f'mem/obs/claude/claude-code/xxx/sess/{obs_id}'
    store.get_session().put(malformed_key, '{ not valid json')
    time.sleep(_INGEST_SETTLE)

    # Sanity: key is present but unparseable, so find_observation_by_id misses it.
    assert store.find_observation_by_id(obs_id) is None
    sess = store.get_session()
    pre = [str(r.ok.key_expr) for r in sess.get('mem/obs/**', timeout=2.0) if r.ok]
    assert malformed_key in pre

    obs_removed, tomb_removed = store.physical_delete_observation(obs_id)
    # Local find missed → obs_removed stays False even though broadcast fires.
    assert obs_removed is False
    assert tomb_removed is False

    time.sleep(_INGEST_SETTLE)
    post = [str(r.ok.key_expr) for r in sess.get('mem/obs/**', timeout=2.0) if r.ok]
    assert (
        malformed_key not in post
    ), f'wildcard broadcast should have swept {malformed_key}, but still present in {post}'


def test_gc_skips_tomb_with_key_body_id_mismatch(single_zenohd: Any) -> None:  # noqa: ARG001
    """An aged tomb whose key-suffix disagrees with its body id must not purge an unrelated obs.

    Guards against the destructive path codex flagged on b09b82c: mirroring
    ``tomb_key`` → ``obs_key`` blindly would let a corrupted/misrouted
    tombstone delete an innocent observation. The gc sweep must refuse to
    act on such a mismatch.
    """
    protected = _mk_obs('must survive mismatched-tomb gc')
    store.put_observation(protected)

    # Bogus tomb: stored at a key ending with ``protected.observation_id`` but
    # whose body claims a different observation_id. Aged so retention would
    # otherwise purge it.
    aged = datetime.now(timezone.utc) - timedelta(days=60)
    bogus_tomb = Tombstone(
        observation_id='f' * 32,  # disagrees with the key suffix
        reason='bogus',
        deleted_at=aged.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    )
    store.get_session().put(protected.tombstone_key_expr(), bogus_tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30)
    # Mismatch must veto the purge for both the tomb and the mirrored obs.
    assert purged == 0
    time.sleep(_INGEST_SETTLE)
    assert _obs_present(protected.observation_id), 'protected obs was purged by a mismatched-body tombstone'


class _RaisingDeleteSession:
    """Session fake that raises on every ``delete`` call.

    Simulates a Zenoh backend that rejects wildcard ``session.delete``
    patterns, which the code must tolerate under :func:`store.physical_delete_observation`.
    ``get`` returns no replies so the per-key enumeration paths stay inert.
    """

    def __init__(self) -> None:
        self.delete_calls: list[str] = []

    def delete(self, key_expr: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.delete_calls.append(str(key_expr))
        raise RuntimeError(f'backend refuses wildcard delete: {key_expr}')

    def get(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        return iter([])

    def put(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        pass

    def close(self) -> None:
        pass


def test_broadcast_delete_best_effort_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """_broadcast_delete_best_effort must swallow backend-level refusals.

    Guards the regression codex flagged on the first fix pass: an
    unconditional wildcard delete would crash every ``gc --force-id`` on a
    backend that rejects the pattern. The helper must log + return False
    instead of propagating.
    """
    fake = _RaisingDeleteSession()
    monkeypatch.setattr(store, '_session', fake)
    ok = store._broadcast_delete_best_effort('mem/obs/*/*/*/*/abc')
    assert ok is False
    assert fake.delete_calls == ['mem/obs/*/*/*/*/abc']


def test_physical_delete_survives_wildcard_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """physical_delete_observation must not raise when the broadcast step fails.

    Asserts that the per-key path short-circuits cleanly (no local data in
    this fake) and the wildcard broadcast exceptions are swallowed — the
    emergency purge must stay callable on any backend.
    """
    fake = _RaisingDeleteSession()
    monkeypatch.setattr(store, '_session', fake)
    obs_removed, tomb_removed = store.physical_delete_observation('0' * 32)
    assert obs_removed is False
    assert tomb_removed is False
    # Both wildcard patterns were attempted despite raising each time.
    assert any(k.startswith('mem/obs/') for k in fake.delete_calls), fake.delete_calls
    assert any(k.startswith('mem/tomb/') for k in fake.delete_calls), fake.delete_calls


# ---------------------------------------------------------------------------
# --project filter tests
# ---------------------------------------------------------------------------


def _mk_aged_tombstone(obs: Observation, days_ago: int = 60) -> None:
    """Emit a back-dated tombstone for *obs* so retention would sweep it."""
    aged = datetime.now(timezone.utc) - timedelta(days=days_ago)
    tomb = Tombstone(
        observation_id=obs.observation_id,
        reason='aged',
        deleted_at=aged.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    )
    store.get_session().put(obs.tombstone_key_expr(), tomb.to_json())


def test_gc_project_filter_isolates(single_zenohd: Any) -> None:  # noqa: ARG001
    """Gc --project B must NOT remove tombstones whose observation has project=A."""
    obs_a = _mk_obs('project-A obs', project='proj-a')
    store.put_observation(obs_a)
    _mk_aged_tombstone(obs_a)
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30, project='proj-b')
    assert purged == 0
    time.sleep(_INGEST_SETTLE)
    assert _obs_present(obs_a.observation_id), 'proj-a obs must survive gc --project proj-b'
    assert _tomb_keys_for(obs_a.observation_id) != [], 'proj-a tomb must survive gc --project proj-b'


def test_gc_project_filter_targets(single_zenohd: Any) -> None:  # noqa: ARG001
    """Gc --project A removes tombstones whose observation has project=A."""
    obs_a = _mk_obs('project-A obs to purge', project='proj-a')
    store.put_observation(obs_a)
    _mk_aged_tombstone(obs_a)
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30, project='proj-a')
    assert purged >= 1
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs_a.observation_id)
    assert _tomb_keys_for(obs_a.observation_id) == []


def test_gc_no_project_unchanged(single_zenohd: Any) -> None:  # noqa: ARG001
    """--project omitted sweeps all projects (existing behaviour unchanged)."""
    obs_a = _mk_obs('multi-proj gc test A', project='proj-x')
    obs_b = _mk_obs('multi-proj gc test B', project='proj-y')
    store.put_observation(obs_a)
    store.put_observation(obs_b)
    _mk_aged_tombstone(obs_a)
    _mk_aged_tombstone(obs_b)
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30)
    assert purged >= 2
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs_a.observation_id)
    assert not _obs_present(obs_b.observation_id)


def test_gc_force_id_with_project_ignores_project(single_zenohd: Any) -> None:  # noqa: ARG001
    """--force-id takes precedence; --project is irrelevant to the ID-based path.

    When both --force-id and --project are supplied, physical_delete_observation
    is called for the given ID without any project check. This preserves the
    single-ID emergency-purge semantics.
    """
    obs = _mk_obs('force-id-project-combo', project='proj-z')
    store.put_observation(obs)
    store.put_tombstone(obs, reason='combo-test')
    time.sleep(_INGEST_SETTLE)

    # Simulates: mesh-mem gc --force-id <id> --project other-proj
    # _cmd_gc short-circuits to physical_delete_observation when force_id is set.
    obs_removed, tomb_removed = store.physical_delete_observation(obs.observation_id)
    assert obs_removed is True
    assert tomb_removed is True
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) == []

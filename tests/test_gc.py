"""Integration tests for the gc path (physical delete + retention sweep)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import time
from typing import Any

import pytest

from mesh_mem import store
from mesh_mem import transport
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
    monkeypatch.setattr(transport, '_session', fake)
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
    monkeypatch.setattr(transport, '_session', fake)
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


# ---------------------------------------------------------------------------
# Issue #32 — project-scoped gc fast path via SQLite index
# ---------------------------------------------------------------------------


def test_gc_project_filter_skips_global_tomb_scan(
    single_zenohd: Any,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gc --project X must NOT enumerate the full mem/tomb/** Zenoh namespace (#32).

    Spy on ``_list_tombstones`` and assert it is *not* called when the
    SQLite-backed fast path can answer the project-scoped query. This
    is what unlocks O(N) on the project subset instead of O(M) on the
    global tombstone count.
    """
    obs = _mk_obs('proj-X tombed', project='proj-fast')
    store.put_observation(obs)
    _mk_aged_tombstone(obs)
    time.sleep(_INGEST_SETTLE)

    list_tomb_calls: list[bool] = []
    real_list = store._list_tombstones

    def spy() -> Any:
        list_tomb_calls.append(True)
        return real_list()

    monkeypatch.setattr(store, '_list_tombstones', spy)

    purged = store.gc_expired_tombstones(retention_days=30, project='proj-fast')
    assert purged == 1
    assert not list_tomb_calls, 'project-scoped gc must not invoke _list_tombstones (full mem/tomb/** scan)'
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) == []


def test_gc_project_filter_always_rebuilds_for_correctness(
    single_zenohd: Any,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project-scoped gc must rebuild even when the SQLite sidecar is non-empty.

    Codex review P1: a partial sidecar (some rows from earlier short-
    lived runs) would otherwise let ``gc --project ...`` silently miss
    older project tombstones that legacy ``mem/tomb/**`` would have
    swept. The fix is to always realign the index from Zenoh before the
    project-scoped query, dropping the previous ``row_count() == 0``
    short-circuit.
    """
    from mesh_mem.local_index import LocalIndex

    pre = _mk_obs('pre-existing live row', project='proj-pre')
    store.put_observation(pre)
    target = _mk_obs('to-be-purged in same proj', project='proj-pre')
    store.put_observation(target)
    _mk_aged_tombstone(target)
    time.sleep(_INGEST_SETTLE)

    rebuild_calls: list[bool] = []
    orig = LocalIndex.rebuild_from_zenoh

    def tracking_rebuild(self: LocalIndex, session: object) -> Any:
        rebuild_calls.append(True)
        return orig(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)

    purged = store.gc_expired_tombstones(retention_days=30, project='proj-pre')
    assert purged == 1
    assert rebuild_calls, (
        'project-scoped gc must always rebuild before the SQLite query (codex P1) '
        'even when the index already has rows from prior writes'
    )


def test_gc_project_filter_falls_back_when_index_disabled(
    single_zenohd: Any,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gc fast path silently falls through to the global Zenoh scan when the index is disabled.

    With ``MESH_MEM_DISABLE_INDEX=1`` the SQLite-fast path cannot run;
    correctness is preserved by the legacy ``_list_tombstones`` sweep.
    """
    monkeypatch.setenv('MESH_MEM_DISABLE_INDEX', '1')
    store._reset_index()  # reopen as disabled

    obs = _mk_obs('disabled-index gc fallback', project='proj-fb')
    store.put_observation(obs)
    _mk_aged_tombstone(obs)
    time.sleep(_INGEST_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30, project='proj-fb')
    assert purged == 1
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) == []


def _mk_pc_obs(content: str, *, pc_id: str, session_id: str) -> Observation:
    return Observation(
        content=content,
        agent_family='claude',
        client_id='claude-code',
        pc_id=pc_id,
        session_id=session_id,
        project='bulk-pc-test',
    )


def test_bulk_purge_by_pc_id_dry_run_lists_matches_without_deleting(
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    target_pc = 'a' * 32
    other_pc = 'b' * 32
    target_obs = [_mk_pc_obs(f'bench-{i}', pc_id=target_pc, session_id='bench-tier4-x') for i in range(3)]
    keep_obs = _mk_pc_obs('legit', pc_id=other_pc, session_id='real-sess')
    for o in target_obs:
        store.put_observation(o)
    store.put_observation(keep_obs)
    time.sleep(_INGEST_SETTLE)

    result = store.bulk_purge_by_pc_id(target_pc, execute=False)
    assert result.executed is False
    assert len(result.matches) == 3
    assert result.purged == 0
    assert result.sessions['bench-tier4-x'] == 3

    # All target obs still present after dry-run.
    for o in target_obs:
        assert _obs_present(o.observation_id)
    assert _obs_present(keep_obs.observation_id)


def test_bulk_purge_by_pc_id_execute_deletes_only_matching_pc(
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    target_pc = 'c' * 32
    other_pc = 'd' * 32
    target_obs = [_mk_pc_obs(f'bench-{i}', pc_id=target_pc, session_id='bench-A') for i in range(2)]
    keep_obs = _mk_pc_obs('survivor', pc_id=other_pc, session_id='real')
    for o in target_obs:
        store.put_observation(o)
    store.put_observation(keep_obs)
    time.sleep(_INGEST_SETTLE)

    result = store.bulk_purge_by_pc_id(target_pc, execute=True)
    assert result.executed is True
    assert result.purged == 2
    assert result.failures == 0
    time.sleep(_INGEST_SETTLE)

    for o in target_obs:
        assert not _obs_present(o.observation_id)
    assert _obs_present(keep_obs.observation_id), 'unrelated pc_id must survive purge'


def test_bulk_purge_by_pc_id_session_prefix_narrows_scope(
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    pc = 'e' * 32
    bench_obs = _mk_pc_obs('bench', pc_id=pc, session_id='bench-tier4-7')
    real_obs = _mk_pc_obs('real work', pc_id=pc, session_id='engineering-1')
    store.put_observation(bench_obs)
    store.put_observation(real_obs)
    time.sleep(_INGEST_SETTLE)

    result = store.bulk_purge_by_pc_id(pc, session_prefix='bench', execute=True)
    assert result.purged == 1
    assert result.failures == 0
    time.sleep(_INGEST_SETTLE)

    assert not _obs_present(bench_obs.observation_id)
    assert _obs_present(real_obs.observation_id), 'session_prefix must shield non-bench sessions'


def test_bulk_purge_by_pc_id_no_match_returns_empty(
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    result = store.bulk_purge_by_pc_id('9' * 32, execute=True)
    assert result.matches == []
    assert result.executed is False
    assert result.purged == 0


def test_bulk_purge_by_pc_id_also_purges_mirrored_tombstone(
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """Legitimate tombstones under the targeted pc_id are cleaned up too.

    The bulk path skips the global ``mem/tomb/**`` sweep but mirrors each
    matched obs into ``mem/tomb/...`` exact-key delete (codex review
    IMPORTANT 2). Without this, a legitimate ``mesh-mem delete`` call that
    happened earlier on the targeted pc_id would leave its tomb behind,
    and a later ``rebuild_from_zenoh`` could resurrect bookkeeping.
    """
    pc = 'f' * 32
    obs = _mk_pc_obs('legit-then-tombed', pc_id=pc, session_id='real-1')
    store.put_observation(obs)
    store.put_tombstone(obs, reason='user-deleted')
    time.sleep(_INGEST_SETTLE)
    assert _tomb_keys_for(obs.observation_id) != []

    result = store.bulk_purge_by_pc_id(pc, execute=True)
    assert result.purged == 1
    assert result.tombs_purged == 1
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(obs.observation_id)
    assert _tomb_keys_for(obs.observation_id) == []


# ---------------------------------------------------------------------------
# Issue #70 — shadow retention sweep
# ---------------------------------------------------------------------------


def test_gc_expired_shadows_purges_aged_shadow_absent_upstream(single_zenohd: Any) -> None:  # noqa: ARG001
    """Aged shadow whose Zenoh obs is genuinely gone → physical_delete."""
    obs = _mk_obs('aged shadow', project='shadow-gc')
    idx = store.get_index()
    # Note: obs is NOT put to Zenoh, so the live re-verify will not find it.
    idx.upsert(obs)
    aged = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    idx.mark_shadowed_missing(obs.observation_id, aged)

    purged, revived = store.gc_expired_shadows(retention_days=30)
    assert (purged, revived) == (1, 0)
    assert idx.find_by_id(obs.observation_id, include_deleted=True) is None


def test_gc_expired_shadows_keeps_fresh_shadow(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('fresh shadow', project='shadow-gc')
    idx = store.get_index()
    idx.upsert(obs)
    fresh = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    idx.mark_shadowed_missing(obs.observation_id, fresh)

    # Fresh shadow has no expired candidates → early-return (0, 0) without
    # paying for the Zenoh re-verify scan.
    purged, revived = store.gc_expired_shadows(retention_days=30)
    assert (purged, revived) == (0, 0)
    assert idx.find_by_id(obs.observation_id, include_deleted=True) is not None


def test_gc_expired_shadows_respects_project_filter(single_zenohd: Any) -> None:  # noqa: ARG001
    a = _mk_obs('proj-a aged shadow', project='shadow-a')
    b = _mk_obs('proj-b aged shadow', project='shadow-b')
    idx = store.get_index()
    # Neither is put to Zenoh, so both would be purged if surfaced. The
    # project filter must isolate them.
    idx.upsert(a)
    idx.upsert(b)
    aged = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    idx.mark_shadowed_missing(a.observation_id, aged)
    idx.mark_shadowed_missing(b.observation_id, aged)

    purged, revived = store.gc_expired_shadows(retention_days=30, project='shadow-a')
    assert (purged, revived) == (1, 0)
    assert idx.find_by_id(a.observation_id, include_deleted=True) is None
    assert idx.find_by_id(b.observation_id, include_deleted=True) is not None


def test_gc_expired_shadows_revives_when_zenoh_still_has_obs(single_zenohd: Any) -> None:  # noqa: ARG001
    """Aged shadow but Zenoh still observes the obs → upsert revives the row.

    Regression for the false-shadow-deletion path. A rebuild flagged a row
    as shadowed during a transient gap. Retention has now elapsed, but the
    obs is once again visible upstream. The sweep must NOT physical-delete
    — that would erase still-live data and leave no obvious recovery
    short of replaying the original put.
    """
    obs = _mk_obs('shadow but upstream still has it', project='shadow-revive')
    # Put to Zenoh so the live re-verify hits the obs.
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    idx = store.get_index()
    idx.upsert(obs)
    aged = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    idx.mark_shadowed_missing(obs.observation_id, aged)
    # Row is hidden from default search due to shadowed_at.
    assert idx.find_by_id(obs.observation_id) is None

    purged, revived = store.gc_expired_shadows(retention_days=30)
    assert (purged, revived) == (0, 1)
    # Upsert cleared shadowed_at → row is live again.
    hit = idx.find_by_id(obs.observation_id)
    assert hit is not None
    assert hit.observation_id == obs.observation_id


def test_gc_expired_shadows_handles_mixed_revive_and_purge(single_zenohd: Any) -> None:  # noqa: ARG001
    """One shadow that Zenoh has + one shadow Zenoh doesn't → revive one, purge one."""
    surviving = _mk_obs('upstream still has me', project='shadow-mixed')
    gone = _mk_obs('upstream really lost me', project='shadow-mixed')
    store.put_observation(surviving)
    # ``gone`` is not put to Zenoh on purpose.
    time.sleep(_INGEST_SETTLE)

    idx = store.get_index()
    idx.upsert(surviving)
    idx.upsert(gone)
    aged = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    idx.mark_shadowed_missing(surviving.observation_id, aged)
    idx.mark_shadowed_missing(gone.observation_id, aged)

    purged, revived = store.gc_expired_shadows(retention_days=30)
    assert (purged, revived) == (1, 1)
    assert idx.find_by_id(surviving.observation_id) is not None
    assert idx.find_by_id(gone.observation_id, include_deleted=True) is None


def test_cli_gc_retention_sweeps_both_tomb_and_shadow(single_zenohd: Any) -> None:  # noqa: ARG001
    """``mesh-mem gc --retention-days N`` sweeps tombstones AND shadows by default."""
    from mesh_mem.__main__ import main as cli_main

    aged_iso = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    # Aged tombstone path (replays the existing tomb-retention test setup).
    tombed = _mk_obs('cli-gc tomb', project='cli-shadow-mix')
    store.put_observation(tombed)
    aged_tomb = Tombstone(observation_id=tombed.observation_id, reason='aged', deleted_at=aged_iso)
    store.get_session().put(tombed.tombstone_key_expr(), aged_tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    # Aged shadow path (purely local index manipulation).
    shadowed = _mk_obs('cli-gc shadow', project='cli-shadow-mix')
    idx = store.get_index()
    idx.upsert(shadowed)
    idx.mark_shadowed_missing(shadowed.observation_id, aged_iso)

    rc = cli_main(['gc', '--retention-days', '30'])
    assert rc == 0
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(tombed.observation_id)
    assert idx.find_by_id(shadowed.observation_id, include_deleted=True) is None


def test_cli_gc_no_shadow_prune_flag_skips_shadow_only(single_zenohd: Any) -> None:  # noqa: ARG001
    """``--no-shadow-prune`` keeps shadows untouched while still sweeping tombs."""
    from mesh_mem.__main__ import main as cli_main

    aged_iso = (datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    tombed = _mk_obs('cli-gc tomb (no-shadow)', project='cli-no-shadow')
    store.put_observation(tombed)
    aged_tomb = Tombstone(observation_id=tombed.observation_id, reason='aged', deleted_at=aged_iso)
    store.get_session().put(tombed.tombstone_key_expr(), aged_tomb.to_json())
    time.sleep(_INGEST_SETTLE)

    shadowed = _mk_obs('cli-gc shadow (no-shadow)', project='cli-no-shadow')
    idx = store.get_index()
    idx.upsert(shadowed)
    idx.mark_shadowed_missing(shadowed.observation_id, aged_iso)

    rc = cli_main(['gc', '--retention-days', '30', '--no-shadow-prune'])
    assert rc == 0
    time.sleep(_INGEST_SETTLE)
    assert not _obs_present(tombed.observation_id)
    # Shadow survives because --no-shadow-prune was set.
    assert idx.find_by_id(shadowed.observation_id, include_deleted=True) is not None


def test_rebuild_then_gc_pipeline_discovers_and_processes_stale_row(single_zenohd: Any) -> None:  # noqa: ARG001
    """The composed pipeline (rebuild → gc) covers the stale-but-not-yet-shadowed path.

    This is the underlying contract the CLI driver depends on (#70 Finding 2).
    ``gc_expired_shadows`` alone only sees pre-existing shadow rows; the
    discovery step that turns a stale local row into a shadow candidate is
    ``rebuild_from_zenoh``. Run them in the order the CLI does and verify
    that a row that started with no ``shadowed_at`` actually reaches the
    purge branch.
    """
    # Stale local row: live in SQLite, never published to Zenoh.
    stale = _mk_obs('stale only in index', project='pipeline-discovery')
    idx = store.get_index()
    idx.upsert(stale)

    # Surviving row: properly published. Rebuild must leave it live.
    surviving = _mk_obs('upstream-live alongside stale', project='pipeline-discovery')
    store.put_observation(surviving)
    time.sleep(_INGEST_SETTLE)

    # Pre-state: stale has no shadowed_at — list_expired_shadowed_obs alone
    # would not surface it.
    assert idx.find_by_id(stale.observation_id) is not None

    # Step 1: reconcile. After this, stale.shadowed_at is set to "now".
    stats = idx.rebuild_from_zenoh(store.get_session())
    assert stats.shadowed >= 1
    assert idx.find_by_id(stale.observation_id) is None  # hidden by shadow

    # Step 2: sweep. ``now`` injected a day into the future so the freshly
    # stamped shadow_at falls before the cutoff (cutoff = future - 0d).
    future = datetime.now(timezone.utc) + timedelta(days=1)
    purged, revived = store.gc_expired_shadows(retention_days=0, now=future)
    assert purged >= 1
    assert revived == 0  # surviving was never shadowed → not a candidate

    # End state: stale purged, surviving still live.
    assert idx.find_by_id(stale.observation_id, include_deleted=True) is None
    assert idx.find_by_id(surviving.observation_id) is not None


def test_cli_gc_runs_rebuild_then_sweep_for_stale_row(single_zenohd: Any) -> None:  # noqa: ARG001
    """End-to-end pin: a single ``mesh-mem gc`` invocation closes the loop.

    Reproduces the standalone-CLI scenario from #70 Finding 2: no external
    rebuild has run, the SQLite index carries a stale live row whose obs
    is absent from Zenoh, and a one-shot ``mesh-mem gc --retention-days 0``
    must discover it (via the in-driver rebuild) and purge it (via the
    re-verifying shadow sweep) in the same process.
    """
    from mesh_mem.__main__ import main as cli_main

    stale = _mk_obs('cli-discovery stale', project='cli-shadow-discovery')
    idx = store.get_index()
    idx.upsert(stale)

    surviving = _mk_obs('cli-discovery surviving', project='cli-shadow-discovery')
    store.put_observation(surviving)
    time.sleep(_INGEST_SETTLE)

    # Pre: stale has no shadowed_at yet — list_expired_shadowed_obs would miss it.
    assert idx.find_by_id(stale.observation_id) is not None

    # Tiny sleep so the rebuild-stamped shadow_at falls strictly before the
    # sweep's cutoff (both walk-clock ``datetime.now``; without this gap a
    # zero-retention sweep on a fast machine could see equality and skip).
    time.sleep(0.01)

    rc = cli_main(['gc', '--retention-days', '0'])
    assert rc == 0

    # Single CLI invocation did rebuild → shadow → re-verify → purge.
    assert idx.find_by_id(stale.observation_id, include_deleted=True) is None
    assert idx.find_by_id(surviving.observation_id) is not None

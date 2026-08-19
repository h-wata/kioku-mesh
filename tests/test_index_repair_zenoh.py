"""R3 Phase 4: end-to-end regression for repair-only realignment on a real zenohd.

``repair_from_zenoh`` (ADR-0035, PR #327) is unit-tested against fake sessions
in ``tests/test_local_index.py``. This module runs the same contract over the
real path — real router, real ``session.get`` scan, real SQLite index — because
the two properties it has to hold are both properties of what Zenoh actually
answers, not of a hand-written reply list:

1. :func:`test_repair_recovers_remote_writes_missed_while_subscriber_stopped` —
   the problem R3 exists for: writes published while this host's index
   subscriber is not listening never reach the index, and periodic repair is
   what closes that gap.
2. :func:`test_repair_keeps_local_only_row_on_full_success` and
   :func:`test_repair_keeps_local_only_row_on_partial_failure` — the repair-only
   contract: a row that exists locally and not in Zenoh is never shadowed or
   deleted, on a complete scan *and* on a scan that dies half-way.
   ``rebuild_from_zenoh`` does shadow such rows (that is its documented
   semantics, see ``test_rebuild_shadows_remote_delete_missed_while_subscriber_stopped``),
   and shipping that behavior on a periodic timer is the silent-data-loss shape
   mesh-mem PR #115 review B2 found. Phase 4 exists to prove repair does not
   inherit it.
3. :func:`test_repair_synthesizes_orphan_tombstone_over_real_zenoh` — R5's
   orphan tombstone placeholder (PR #326 / #327 N2) over the real path: a
   tombstone whose observation is nowhere yet must survive as a placeholder, so
   a later-arriving observation cannot resurrect a deleted record.

These use the session-scoped ``single_zenohd`` fixture, which serves the
``memory`` volume — no RocksDB backend plugin — so unlike
``tests/test_two_node_scope_harness.py`` they DO run on CI, which installs
``zenohd`` but not ``libzenoh_backend_rocksdb.so``. They SKIP only where
``zenohd`` itself is missing.
"""

from __future__ import annotations

from typing import Any

import pytest
import zenoh

from kioku_mesh import store
from kioku_mesh.local_index import RepairScanError
from kioku_mesh.models import Observation
from kioku_mesh.models import Tombstone

from .wait_helpers import storage_has
from .wait_helpers import wait_until

# A canonical, non-legacy obs key whose payload is not an Observation: the scan
# skips off-shape keys but must fail on a canonical one it cannot parse.
_CORRUPT_OBS_KEY = 'mem/mesh/obs/fam/cli/pc/sess/' + 'a' * 32


def _mk_obs(content: str, *, project: str) -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test-client',
        pc_id='test-pc',
        session_id='test-session',
        visibility='mesh',
    )


def _live_ids(idx: Any, project: str) -> set[str]:
    return {r.observation_id for r in idx.search(project=project)}


def _remote(endpoint: str) -> zenoh.Session:
    """Open a publisher session on the router, independent of the store session."""
    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"client"')
    cfg.insert_json5('connect/endpoints', f'["{endpoint}"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    return zenoh.open(cfg)


def _publish(remote: zenoh.Session, key: str, payload: str) -> None:
    """Publish and wait until the router's storage answers for ``key``.

    The repair scan reads storage, so "stored" is the condition that matters —
    and with the subscriber deliberately stopped, storage is the only place
    these tests can observe the sample arriving at all.
    """
    remote.put(key, payload)
    wait_until(lambda: storage_has(store.get_session(), key), f'zenoh storage to hold {key}')


@pytest.fixture
def muted_subscriber(single_zenohd: Any) -> Any:
    """Yield an open index with its subscriber stopped, re-armed on teardown.

    This is the state R3 is about: the index is live and queryable, but nothing
    is mirroring Zenoh into it, so remote writes are only recoverable by a scan.
    """
    idx = store.get_index()
    assert not idx.disabled
    store._reset_subscribers()  # noqa: SLF001 — deliberately deafen the index
    try:
        yield idx
    finally:
        # Restore the steady-state wiring for whatever runs next in this session.
        store._subscribers = store.start_index_subscriber(store.get_session())  # noqa: SLF001


def test_repair_recovers_remote_writes_missed_while_subscriber_stopped(
    single_zenohd: Any,
    muted_subscriber: Any,
) -> None:
    """The R3 problem itself: a repair scan backfills what the muted index missed.

    While the subscriber is down, a remote peer publishes one new observation
    and one tombstone for a row this host already has. Neither reaches the index
    live — asserted before the repair, so the assertions after it cannot pass
    vacuously — and ``repair_from_zenoh`` is what makes the index agree with the
    mesh again.
    """
    idx = muted_subscriber
    project = 'repair-missed'
    existing = _mk_obs('already indexed, deleted upstream', project=project)
    idx.upsert(existing)
    missed = _mk_obs('published while the subscriber was down', project=project)

    remote = _remote(single_zenohd.endpoint)
    try:
        _publish(remote, missed.key_expr, missed.to_json())
        _publish(remote, existing.tombstone_key_expr(), Tombstone(observation_id=existing.observation_id).to_json())
    finally:
        remote.close()

    assert _live_ids(idx, project) == {existing.observation_id}, (
        'the muted subscriber must have missed both samples, or this test proves nothing'
    )

    stats = idx.repair_from_zenoh(store.get_session())

    assert stats.added >= 1
    assert stats.marked_deleted == 1
    assert _live_ids(idx, project) == {missed.observation_id}
    assert idx.find_by_id(existing.observation_id, include_deleted=True) is not None, (
        'the tombstoned row must be marked deleted, not physically removed'
    )
    assert idx.alignment_state().last_full_repair_completed_at


def test_repair_keeps_local_only_row_on_full_success(
    single_zenohd: Any,
    muted_subscriber: Any,
) -> None:
    """A complete scan that does not mention a local row leaves it live.

    This is the repair-only contract on a full, successful response: absence
    from the scan is not evidence of deletion. The remote row travelling the
    same scan is the control — it proves the scan really ran and really saw
    Zenoh, which is what makes the local row's survival meaningful.
    """
    idx = muted_subscriber
    project = 'repair-local-only'
    local_only = _mk_obs('never published to zenoh', project=project)
    idx.upsert(local_only)
    remote_obs = _mk_obs('exists in zenoh only', project=project)

    remote = _remote(single_zenohd.endpoint)
    try:
        _publish(remote, remote_obs.key_expr, remote_obs.to_json())
    finally:
        remote.close()

    stats = idx.repair_from_zenoh(store.get_session())

    assert stats.scanned_obs >= 1, 'the scan must have seen the remote row (control)'
    assert _live_ids(idx, project) == {local_only.observation_id, remote_obs.observation_id}, (
        'repair must add the remote row and keep the local-only row live (no shadow, no delete)'
    )
    row = idx.find_by_id(local_only.observation_id, include_deleted=True)
    assert row is not None


def test_repair_keeps_local_only_row_on_partial_failure(
    single_zenohd: Any,
    muted_subscriber: Any,
) -> None:
    """A scan that dies part-way applies nothing at all — including to local-only rows.

    A canonical obs key carrying a payload that is not an Observation makes the
    scan raise ``RepairScanError`` after it has already collected replies, which
    is the partial-response shape on the real path. The local-only row must
    still be live, and the valid remote row must NOT have been applied: a repair
    is all-or-nothing, so a half-finished scan cannot leave the index in a state
    that a later reader would mistake for an aligned one.
    """
    idx = muted_subscriber
    project = 'repair-partial'
    local_only = _mk_obs('never published, must survive a failed scan', project=project)
    idx.upsert(local_only)
    remote_obs = _mk_obs('valid but must not be applied by a failed scan', project=project)

    remote = _remote(single_zenohd.endpoint)
    try:
        _publish(remote, remote_obs.key_expr, remote_obs.to_json())
        _publish(remote, _CORRUPT_OBS_KEY, 'not an observation payload')
    finally:
        remote.close()

    with pytest.raises(RepairScanError):
        idx.repair_from_zenoh(store.get_session())

    assert _live_ids(idx, project) == {local_only.observation_id}, (
        'a failed scan must neither hide the local-only row nor apply the partial results'
    )
    state = idx.alignment_state()
    assert state.last_full_repair_completed_at == '', 'a failed repair must not record a completion'
    assert state.last_failure_class == 'RepairScanError'


def test_repair_synthesizes_orphan_tombstone_over_real_zenoh(
    single_zenohd: Any,
    muted_subscriber: Any,
) -> None:
    """R5 over the real path: a tombstone with no observation anywhere is kept as a placeholder.

    A delete can reach this host before the observation it deletes — the two
    keys are independent samples and, with the subscriber muted, both are only
    seen by a scan. Discarding the orphan tombstone would let the observation,
    arriving later, resurrect a record the mesh already deleted. The second
    repair is that later arrival, and the row must stay deleted through it.
    """
    idx = muted_subscriber
    project = 'repair-orphan'
    doomed = _mk_obs('deleted before this host ever saw it', project=project)

    remote = _remote(single_zenohd.endpoint)
    try:
        _publish(remote, doomed.tombstone_key_expr(), Tombstone(observation_id=doomed.observation_id).to_json())

        first = idx.repair_from_zenoh(store.get_session())
        # The placeholder carries no payload, so it is only observable as a row:
        # find_by_id parses payload_json and cannot see it (see R5, PR #326).
        assert first.orphaned == 1, 'a tombstone with no obs in index or in zenoh must become a placeholder'
        assert idx.row_count() == 1, 'the orphan tombstone must be persisted as a row, not discarded'
        assert _live_ids(idx, project) == set()

        # The observation shows up afterwards, exactly the resurrection risk.
        _publish(remote, doomed.key_expr, doomed.to_json())
        idx.repair_from_zenoh(store.get_session())
    finally:
        remote.close()

    assert _live_ids(idx, project) == set(), 'a late-arriving obs must not resurrect a tombstoned record'
    assert idx.find_by_id(doomed.observation_id) is None, 'the row must stay invisible to normal lookups'
    assert idx.find_by_id(doomed.observation_id, include_deleted=True) is not None, (
        'the late obs payload must have landed on the still-tombstoned row'
    )

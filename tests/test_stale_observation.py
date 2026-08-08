"""Tests for the observation expiry / stale-cleanup flow (Issue #272).

Two halves:

  - the *guarantee* half: ``LocalIndex.search`` (and therefore
    ``search_memory`` / ``recall_context``) must never return a tombstoned,
    shadowed, superseded, or expired observation unless explicitly asked.
    Those exclusions existed for the first three but were only implied by
    tests of other features, so they are pinned here on purpose.
  - the *new behavior* half: ``expires_at`` / ``ttl_sec`` on the write path,
    the expired-row listing, and the dry-run gc candidate report.

Pure SQLite / function tests — no zenohd, no backend wiring.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator

import pytest

from kioku_mesh.__main__ import main as cli_main
from kioku_mesh.core.models import Observation
from kioku_mesh.core.models import resolve_expires_at
from kioku_mesh.memory.local_index import LocalIndex
from kioku_mesh.memory.purge import collect_gc_candidates


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


# Anchored on the real clock rather than a fixed literal: ``inspect_by_id``
# resolves ``expired`` against the actual current instant (it has no injection
# point), so a hardcoded NOW would flip that one assertion depending on which
# side of the literal the suite happens to run.
NOW = datetime.now(timezone.utc)
PAST = _iso(NOW - timedelta(hours=1))
FUTURE = _iso(NOW + timedelta(hours=1))


def _mk(
    content: str,
    *,
    project: str = 'expiry-demo',
    memory_type: str = 'note',
    subject: str = '',
    expires_at: str = '',
    supersedes: list[str] | None = None,
) -> Observation:
    return Observation(
        content=content,
        project=project,
        memory_type=memory_type,
        subject=subject,
        expires_at=expires_at,
        supersedes=list(supersedes or []),
    )


@pytest.fixture
def idx(tmp_path: Path) -> Iterator[LocalIndex]:
    index = LocalIndex.connect(str(tmp_path / 'index.db'))
    yield index
    index.close()


# -- resolve_expires_at --------------------------------------------------------


def test_resolve_expires_at_empty_means_never() -> None:
    assert resolve_expires_at() == ''
    assert resolve_expires_at(ttl_sec=0) == ''
    assert resolve_expires_at(ttl_sec=-5) == ''


def test_resolve_expires_at_explicit_wins_over_ttl() -> None:
    """Precedence matches messaging's is_expired contract: expires_at > ttl_sec."""
    resolved = resolve_expires_at(expires_at='2026-08-08T12:00:00Z', ttl_sec=999999, created_at=_iso(NOW))
    assert resolved == '2026-08-08T12:00:00.000000Z'


def test_resolve_expires_at_ttl_offsets_created_at() -> None:
    assert resolve_expires_at(ttl_sec=3600, created_at=_iso(NOW)) == _iso(NOW + timedelta(hours=1))


def test_resolve_expires_at_normalizes_offset_to_utc() -> None:
    assert resolve_expires_at(expires_at='2026-08-08T21:00:00+09:00') == '2026-08-08T12:00:00.000000Z'


def test_resolve_expires_at_rejects_garbage() -> None:
    with pytest.raises(ValueError, match='ISO 8601'):
        resolve_expires_at(expires_at='next tuesday')


# -- search: default exclusions ------------------------------------------------


def test_search_hides_expired_by_default(idx: LocalIndex) -> None:
    fresh = _mk('still valid', expires_at=FUTURE)
    stale = _mk('disposable ping', expires_at=PAST)
    idx.upsert(fresh)
    idx.upsert(stale)
    ids = [o.observation_id for o in idx.search(project='expiry-demo', now_iso=_iso(NOW))]
    assert ids == [fresh.observation_id]


def test_search_include_expired_returns_it(idx: LocalIndex) -> None:
    stale = _mk('disposable ping', expires_at=PAST)
    idx.upsert(stale)
    ids = [o.observation_id for o in idx.search(project='expiry-demo', include_expired=True, now_iso=_iso(NOW))]
    assert ids == [stale.observation_id]


def test_search_boundary_instant_counts_as_expired(idx: LocalIndex) -> None:
    """``expires_at == now`` is expired, matching messaging's ``now >= expires_at``."""
    obs = _mk('boundary', expires_at=_iso(NOW))
    idx.upsert(obs)
    assert idx.search(project='expiry-demo', now_iso=_iso(NOW)) == []


def test_search_without_expires_at_is_unaffected(idx: LocalIndex) -> None:
    durable = _mk('durable decision', memory_type='decision')
    idx.upsert(durable)
    ids = [o.observation_id for o in idx.search(project='expiry-demo', now_iso=_iso(NOW))]
    assert ids == [durable.observation_id]


def test_search_hides_tombstoned_by_default(idx: LocalIndex) -> None:
    obs = _mk('fixed already')
    idx.upsert(obs)
    idx.mark_deleted(obs.observation_id, _iso(NOW))
    assert idx.search(project='expiry-demo') == []
    assert [o.observation_id for o in idx.search(project='expiry-demo', include_deleted=True)] == [obs.observation_id]


def test_search_hides_shadowed_by_default(idx: LocalIndex) -> None:
    obs = _mk('not seen upstream')
    idx.upsert(obs)
    idx.mark_shadowed_missing(obs.observation_id, _iso(NOW))
    assert idx.search(project='expiry-demo') == []


def test_search_hides_superseded_by_default(idx: LocalIndex) -> None:
    old = _mk('use SQLite', memory_type='decision', subject='db')
    idx.upsert(old)
    new = _mk('use PostgreSQL', memory_type='decision', subject='db', supersedes=[old.observation_id])
    idx.upsert(new)
    ids = [o.observation_id for o in idx.search(project='expiry-demo')]
    assert ids == [new.observation_id]


def test_search_query_path_also_hides_expired(idx: LocalIndex) -> None:
    """The FTS / LIKE branches build their own SQL — the filter must reach them too."""
    stale = _mk('kioku expiry probe token', expires_at=PAST)
    idx.upsert(stale)
    assert idx.search(query='probe', now_iso=_iso(NOW)) == []
    assert len(idx.search(query='probe', include_expired=True, now_iso=_iso(NOW))) == 1


def test_search_or_mode_also_hides_expired(idx: LocalIndex) -> None:
    stale = _mk('kioku expiry probe token', expires_at=PAST)
    idx.upsert(stale)
    assert idx.search(query='probe token', search_mode='or', now_iso=_iso(NOW)) == []
    assert idx.search(query='probe token', search_mode='and_or', now_iso=_iso(NOW)) == []


# -- inspect_by_id / find_by_id ------------------------------------------------


def test_inspect_by_id_reports_expired_state(idx: LocalIndex) -> None:
    obs = _mk('disposable ping', expires_at=PAST)
    idx.upsert(obs)
    info = idx.inspect_by_id(obs.observation_id)
    assert info is not None
    assert info['state'] == 'expired'
    assert info['expires_at'] == PAST


def test_expired_row_is_still_reachable_by_id(idx: LocalIndex) -> None:
    """Explicit id lookups must keep working: gc and delete both need the payload."""
    obs = _mk('disposable ping', expires_at=PAST)
    idx.upsert(obs)
    found = idx.find_by_id(obs.observation_id)
    assert found is not None
    assert found.expires_at == PAST


# -- list_expired_ttl_obs ------------------------------------------------------


def test_list_expired_ttl_obs_returns_only_lapsed_live_rows(idx: LocalIndex) -> None:
    stale = _mk('lapsed', expires_at=PAST)
    fresh = _mk('not yet', expires_at=FUTURE)
    durable = _mk('no ttl')
    for obs in (stale, fresh, durable):
        idx.upsert(obs)
    rows = idx.list_expired_ttl_obs(now_iso=_iso(NOW))
    assert [r[0] for r in rows] == [stale.observation_id]


def test_list_expired_ttl_obs_skips_already_tombstoned(idx: LocalIndex) -> None:
    obs = _mk('lapsed and already deleted', expires_at=PAST)
    idx.upsert(obs)
    idx.mark_deleted(obs.observation_id, _iso(NOW))
    assert idx.list_expired_ttl_obs(now_iso=_iso(NOW)) == []


def test_list_expired_ttl_obs_project_filter(idx: LocalIndex) -> None:
    mine = _mk('mine', project='p-a', expires_at=PAST)
    theirs = _mk('theirs', project='p-b', expires_at=PAST)
    idx.upsert(mine)
    idx.upsert(theirs)
    rows = idx.list_expired_ttl_obs(now_iso=_iso(NOW), project='p-a')
    assert [r[0] for r in rows] == [mine.observation_id]


# -- schema migration ----------------------------------------------------------


def test_migration_adds_expires_at_and_backfills(tmp_path: Path) -> None:
    """A pre-#272 database gains the column, with the payload value carried over."""
    db_path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        'CREATE TABLE obs_index ('
        'observation_id TEXT PRIMARY KEY, project TEXT, created_at TEXT, memory_type TEXT, '
        'importance INTEGER, subject TEXT, summary TEXT, payload_json TEXT, deleted_at TEXT)'
    )
    obs = _mk('written by a newer peer', expires_at=PAST)
    conn.execute(
        'INSERT INTO obs_index (observation_id, project, created_at, memory_type, importance, '
        'subject, summary, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (obs.observation_id, obs.project, obs.created_at, obs.memory_type, obs.importance, '', '', obs.to_json()),
    )
    conn.commit()
    conn.close()

    index = LocalIndex.connect(str(db_path))
    try:
        info = index.inspect_by_id(obs.observation_id)
        assert info is not None
        assert info['expires_at'] == PAST
        assert index.search(project='expiry-demo', now_iso=_iso(NOW)) == []
    finally:
        index.close()


# -- collect_gc_candidates -----------------------------------------------------


def test_collect_gc_candidates_groups_three_buckets(idx: LocalIndex) -> None:
    ttl_gone = _mk('lapsed ttl', expires_at=PAST)
    tombed = _mk('old tombstone')
    shadowed = _mk('old shadow')
    live = _mk('keep me')
    for obs in (ttl_gone, tombed, shadowed, live):
        idx.upsert(obs)
    long_ago = _iso(NOW - timedelta(days=90))
    idx.mark_deleted(tombed.observation_id, long_ago)
    idx.mark_shadowed_missing(shadowed.observation_id, long_ago)

    candidates = collect_gc_candidates(idx, retention_days=30, now=NOW)
    assert [c.observation_id for c in candidates.expired_ttl] == [ttl_gone.observation_id]
    assert [c.observation_id for c in candidates.expired_tombstones] == [tombed.observation_id]
    assert [c.observation_id for c in candidates.expired_shadows] == [shadowed.observation_id]
    assert candidates.total() == 3
    assert {c.action for c in candidates.expired_ttl} == {'tombstone'}
    assert {c.action for c in candidates.expired_tombstones} == {'physical-delete'}


def test_collect_gc_candidates_is_read_only(idx: LocalIndex) -> None:
    """The dry-run path must not mutate anything, whatever it reports."""
    ttl_gone = _mk('lapsed ttl', expires_at=PAST)
    idx.upsert(ttl_gone)
    collect_gc_candidates(idx, retention_days=30, now=NOW)
    info = idx.inspect_by_id(ttl_gone.observation_id)
    assert info is not None
    assert info['deleted_at'] is None
    assert idx.row_count() == 1


def test_collect_gc_candidates_within_retention_is_not_a_candidate(idx: LocalIndex) -> None:
    tombed = _mk('recently tombstoned')
    idx.upsert(tombed)
    idx.mark_deleted(tombed.observation_id, _iso(NOW - timedelta(days=1)))
    candidates = collect_gc_candidates(idx, retention_days=30, now=NOW)
    assert candidates.expired_tombstones == []


def test_collect_gc_candidates_project_filter(idx: LocalIndex) -> None:
    mine = _mk('mine', project='p-a', expires_at=PAST)
    theirs = _mk('theirs', project='p-b', expires_at=PAST)
    idx.upsert(mine)
    idx.upsert(theirs)
    candidates = collect_gc_candidates(idx, project='p-a', now=NOW)
    assert [c.observation_id for c in candidates.expired_ttl] == [mine.observation_id]


def test_collect_gc_candidates_rejects_negative_retention(idx: LocalIndex) -> None:
    with pytest.raises(ValueError, match='retention_days'):
        collect_gc_candidates(idx, retention_days=-1, now=NOW)


# -- CLI: gc-observations ------------------------------------------------------


def _local_backend(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    """Switch to the local backend so the CLI runs without a zenohd router."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    from kioku_mesh.backend import get_backend  # noqa: PLC0415
    from kioku_mesh.backend import reset_backend  # noqa: PLC0415

    reset_backend()
    return get_backend()


def _inspect(observation_id: str) -> dict | None:
    """Inspect a row through a freshly resolved backend index.

    ``cli_main`` tears the process-wide backend down on exit, so the handle a
    test captured before the call may point at a closed connection.
    """
    from kioku_mesh.backend import get_backend  # noqa: PLC0415

    return get_backend()._idx.inspect_by_id(observation_id)  # noqa: SLF001


def test_gc_observations_defaults_to_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``--execute``: the lapsed row is reported, and it is still live afterwards."""
    backend = _local_backend(monkeypatch)
    obs = _mk('disposable ping', expires_at=PAST)
    backend.put_observation(obs)

    rc = cli_main(['gc-observations', '--project', 'expiry-demo'])
    out = capsys.readouterr().out

    assert rc == 0
    assert obs.observation_id in out
    assert 'reason=expired-ttl' in out
    assert 'total candidates: 1' in out
    assert 'Dry run — pass --execute' in out
    info = _inspect(obs.observation_id)
    assert info is not None
    assert info['deleted_at'] is None


def test_gc_observations_execute_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--execute`` without ``--yes`` on a non-tty refuses rather than deleting."""
    backend = _local_backend(monkeypatch)
    obs = _mk('disposable ping', expires_at=PAST)
    backend.put_observation(obs)

    rc = cli_main(['gc-observations', '--project', 'expiry-demo', '--execute'])
    err = capsys.readouterr().err

    assert rc == 2
    assert 'interactive confirmation' in err
    info = _inspect(obs.observation_id)
    assert info is not None
    assert info['deleted_at'] is None


def test_gc_observations_reports_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = _local_backend(monkeypatch)
    backend.put_observation(_mk('durable', memory_type='decision'))

    rc = cli_main(['gc-observations', '--project', 'expiry-demo'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'nothing to clean up.' in out


def test_gc_observations_execute_tombstones_expired(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--execute --yes`` soft-deletes the lapsed row — and only that row."""
    backend = _local_backend(monkeypatch)
    stale = _mk('disposable ping', expires_at=PAST)
    durable = _mk('durable', memory_type='decision')
    backend.put_observation(stale)
    backend.put_observation(durable)

    rc = cli_main(['gc-observations', '--project', 'expiry-demo', '--execute', '--yes'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'tombstoned 1 expired-TTL observations' in out
    stale_info = _inspect(stale.observation_id)
    durable_info = _inspect(durable.observation_id)
    assert stale_info is not None
    assert stale_info['deleted_at'] is not None
    assert durable_info is not None
    assert durable_info['deleted_at'] is None


# -- MCP save_observation ------------------------------------------------------


def test_save_observation_ttl_sec_hides_entry_once_lapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTL saved through the MCP tool drops out of search once it lapses."""
    backend = _local_backend(monkeypatch)
    from kioku_mesh.mcp_server import save_observation  # noqa: PLC0415

    raw = save_observation(
        content='verification ping, delete after the report lands',
        subject='verification ping',
        summary='disposable ping saved through the MCP tool',
        project='expiry-mcp',
        ttl_sec=3600,
    )
    payload = json.loads(raw)
    obs_id = payload['observation_id']
    assert payload['expires_at']

    idx = backend._idx  # noqa: SLF001
    assert [o.observation_id for o in idx.search(project='expiry-mcp')] == [obs_id]
    later = _iso(datetime.now(timezone.utc) + timedelta(hours=2))
    assert idx.search(project='expiry-mcp', now_iso=later) == []


def test_save_observation_without_ttl_stays_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward compat: an unspecified lifetime never expires."""
    backend = _local_backend(monkeypatch)
    from kioku_mesh.mcp_server import save_observation  # noqa: PLC0415

    payload = json.loads(
        save_observation(
            content='durable note',
            subject='durable note',
            summary='an entry saved without a lifetime',
            project='expiry-mcp',
        )
    )
    assert 'expires_at' not in payload
    far_future = _iso(datetime.now(timezone.utc) + timedelta(days=3650))
    ids = [o.observation_id for o in backend._idx.search(project='expiry-mcp', now_iso=far_future)]  # noqa: SLF001
    assert ids == [payload['observation_id']]


def test_save_observation_rejects_unparseable_expires_at(monkeypatch: pytest.MonkeyPatch) -> None:
    _local_backend(monkeypatch)
    from kioku_mesh.mcp_server import save_observation  # noqa: PLC0415

    assert 'ISO 8601' in save_observation(
        content='bad ttl',
        subject='bad ttl',
        summary='an unparseable expires_at is refused',
        expires_at='next tuesday',
    )


# -- PR #273 review B1/B2/B3 regressions ---------------------------------------


def test_expired_superseder_stops_hiding_the_durable_original(idx: LocalIndex) -> None:
    """[B3] A lapsed superseder must not keep a durable predecessor invisible.

    The supersedes filter hides ``old`` only while its superseder is
    *visible*. Once the superseder expires it is gone from every default
    result set, so continuing to hide ``old`` erases both rows at once —
    the default search returns nothing at all for content the user never
    marked disposable.
    """
    old = _mk('durable original', memory_type='decision')
    idx.upsert(old)
    newer = _mk('disposable correction', expires_at=PAST, supersedes=[old.observation_id])
    idx.upsert(newer)
    assert idx.inspect_by_id(old.observation_id)['superseded_by'] == newer.observation_id

    ids = [o.observation_id for o in idx.search(project='expiry-demo')]

    assert newer.observation_id not in ids, 'the lapsed superseder itself stays hidden'
    assert ids == [old.observation_id], 'the durable original must come back once its superseder lapses'


def test_shadowed_expired_row_is_not_a_ttl_candidate(idx: LocalIndex) -> None:
    """[B2] The TTL and shadow buckets must be disjoint.

    A shadowed row is a *guess* that the row vanished upstream, and
    ``gc_expired_shadows`` may still revive it. Tombstoning it from the TTL
    bucket first would settle that question destructively, before the
    re-verification that owns it ever runs.
    """
    shadowed = _mk('shadowed and lapsed', expires_at=PAST)
    idx.upsert(shadowed)
    idx.mark_shadowed_missing(shadowed.observation_id, _iso(NOW - timedelta(days=60)))

    ttl_ids = [row[0] for row in idx.list_expired_ttl_obs(now_iso=_iso(NOW))]
    shadow_ids = idx.list_expired_shadowed_obs(_iso(NOW - timedelta(days=1)))

    assert shadowed.observation_id not in ttl_ids, 'a shadowed row is the shadow sweep to resolve, not the TTL sweep'
    assert shadowed.observation_id in shadow_ids
    candidates = collect_gc_candidates(idx, retention_days=30, now=NOW)
    buckets = {
        'ttl': {c.observation_id for c in candidates.expired_ttl},
        'shadow': {c.observation_id for c in candidates.expired_shadows},
    }
    assert not (buckets['ttl'] & buckets['shadow']), 'candidate buckets must be mutually exclusive'


def test_gc_observations_execute_deletes_expired_tombstones(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The physical-delete bucket is actually swept by ``--execute``.

    The TTL bucket had CLI coverage; the tombstone bucket did not, so a
    regression that silently skipped it would have stayed green.
    """
    backend = _local_backend(monkeypatch)
    obs = _mk('already tombstoned', memory_type='note')
    backend.put_observation(obs)
    backend.put_tombstone(obs, reason='test')

    rc = cli_main(['gc-observations', '--retention-days', '0', '--execute', '--yes'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'reason=expired-tombstone' in out
    assert 'physically deleted 1 tombstones' in out
    assert _inspect(obs.observation_id) is None


def test_gc_observations_execute_only_touches_the_previewed_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[B1] ``--execute`` must delete exactly what the confirmation prompt showed.

    An orphan tombstone (raw row present, index row gone) is invisible to
    the candidate listing by design — ``kioku-mesh gc`` owns those. If
    execute re-derives its own global sweep instead of acting on the
    reviewed snapshot, the user confirms one deletion and gets two.
    """
    backend = _local_backend(monkeypatch)
    listed = _mk('tombstoned and listed', memory_type='note')
    orphan = _mk('tombstoned, index row gone', memory_type='note')
    for obs in (listed, orphan):
        backend.put_observation(obs)
        backend.put_tombstone(obs, reason='test')
    # Make ``orphan`` an orphan: the raw tombstone outlives both its obs
    # payload and its index row, so no candidate listing can surface it.
    backend._raw_store.delete_obs(orphan.observation_id)  # noqa: SLF001
    backend._idx.physical_delete(orphan.observation_id)  # noqa: SLF001

    rc = cli_main(['gc-observations', '--retention-days', '0', '--execute', '--yes'])
    out = capsys.readouterr().out

    assert rc == 0
    assert 'total candidates: 1' in out
    assert 'physically deleted 1 tombstones' in out, 'must not exceed the confirmed candidate set'
    from kioku_mesh.backend import get_backend  # noqa: PLC0415

    raw = get_backend()._raw_store  # noqa: SLF001
    assert not raw.obs_exists(listed.observation_id), 'the previewed tombstone is swept'
    assert raw.obs_exists(orphan.observation_id), 'the orphan is left for `kioku-mesh gc`'

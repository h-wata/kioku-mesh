"""Unit tests for ``mesh_mem.local_index.LocalIndex`` (Issue #7 Phase 2 + 3).

Covers schema creation, upsert idempotency, tombstone marking, the
disable env var, project-scoped query (Phase 2), and the richer
``search`` / ``find_by_id`` entry points (Phase 3) that
``store.search_observations`` / ``store.find_observation_by_id`` route
through.

Tests do not spin up zenohd — LocalIndex is a pure SQLite layer.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from mesh_mem.local_index import LocalIndex
from mesh_mem.local_index import RebuildStats
from mesh_mem.local_index import SCHEMA_VERSION
from mesh_mem.models import Observation
from mesh_mem.models import Tombstone


def _mk_obs(
    content: str,
    *,
    project: str = 'demo',
    tags: list[str] | None = None,
    supersedes: list[str] | None = None,
    subject: str = '',
    summary: str = '',
) -> Observation:
    return Observation(
        content=content,
        project=project,
        tags=list(tags or []),
        supersedes=list(supersedes or []),
        subject=subject,
        summary=summary,
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
    )


def test_local_index_creates_schema_on_first_open(tmp_path: Path) -> None:
    db = tmp_path / 'first.db'
    idx = LocalIndex.connect(str(db))
    try:
        assert db.exists(), 'DB file should be created on connect()'
        # schema_version must be stamped
        with sqlite3.connect(str(db)) as raw:
            (version,) = raw.execute('SELECT version FROM schema_version').fetchone()
            assert version == SCHEMA_VERSION
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'obs_index' in tables
            columns = {row[1] for row in raw.execute('PRAGMA table_info(obs_index)')}
            assert 'shadowed_at' in columns
            assert 'superseded_by' in columns  # ADR-0021
            fts_tables = {
                row[0]
                for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='obs_fts'")
            }
            assert 'obs_fts' in fts_tables, 'FTS5 table must exist on FTS-capable SQLite'
            indexes = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert 'idx_project_created' in indexes
            assert 'idx_created' in indexes
    finally:
        idx.close()


def test_local_index_migrates_legacy_schema_to_add_shadowed_at(tmp_path: Path) -> None:
    db = tmp_path / 'legacy.db'
    with sqlite3.connect(str(db)) as raw:
        raw.executescript("""
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version(version) VALUES (1);
CREATE TABLE obs_index (
  observation_id TEXT PRIMARY KEY,
  project TEXT,
  created_at TEXT,
  memory_type TEXT,
  importance INTEGER,
  subject TEXT,
  summary TEXT,
  payload_json TEXT,
  deleted_at TEXT
);
""")
        raw.commit()

    idx = LocalIndex.connect(str(db))
    try:
        with sqlite3.connect(str(db)) as raw:
            columns = {row[1] for row in raw.execute('PRAGMA table_info(obs_index)')}
            assert 'shadowed_at' in columns
            (version,) = raw.execute('SELECT version FROM schema_version').fetchone()
            assert version == SCHEMA_VERSION
    finally:
        idx.close()


def test_local_index_upsert_observation(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'upsert.db'))
    try:
        obs = _mk_obs('hello', project='proj-A', tags=['a', 'b'])
        idx.upsert(obs)
        assert idx.row_count() == 1
        # Round-trip through search_by_project to verify the payload is
        # decodable; equivalent to what Phase 3 readers will do.
        results = idx.search_by_project('proj-A', limit=10)
        assert len(results) == 1
        assert results[0].observation_id == obs.observation_id
        assert results[0].content == 'hello'
        assert results[0].tags == ['a', 'b']
    finally:
        idx.close()


def test_local_index_upsert_idempotent(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'idem.db'))
    try:
        obs = _mk_obs('original', project='proj-A')
        idx.upsert(obs)
        idx.upsert(obs)
        idx.upsert(obs)
        assert idx.row_count() == 1, 'PK conflict resolution should keep one row'

        # Mutate the same id and upsert again — content should be replaced,
        # row count must remain 1.
        obs.content = 'updated'
        idx.upsert(obs)
        assert idx.row_count() == 1
        results = idx.search_by_project('proj-A')
        assert len(results) == 1
        assert results[0].content == 'updated'
    finally:
        idx.close()


def test_local_index_mark_deleted_sets_timestamp(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'delete.db'))
    try:
        obs = _mk_obs('to be deleted', project='proj-A')
        idx.upsert(obs)
        assert idx.row_count() == 1

        deleted_at = '2026-04-27T21:38:00.000000Z'
        idx.mark_deleted(obs.observation_id, deleted_at)

        # Row count is unchanged — tombstone is a column update, not a delete.
        assert idx.row_count() == 1
        # search_by_project filters deleted rows; previously-live obs disappears.
        assert idx.search_by_project('proj-A') == []

        # Verify the deleted_at column was set via raw SQL (the public
        # search_by_project filters it out, so we assert the underlying
        # write happened).
        with sqlite3.connect(str(tmp_path / 'delete.db')) as raw:
            (got,) = raw.execute(
                'SELECT deleted_at FROM obs_index WHERE observation_id = ?',
                (obs.observation_id,),
            ).fetchone()
            assert got == deleted_at

        # mark_deleted on an unknown id is a silent no-op
        idx.mark_deleted('00000000000000000000000000000000', deleted_at)
        assert idx.row_count() == 1
    finally:
        idx.close()


def test_mark_deleted_clears_shadowed_state(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'delete_clears_shadow.db'))
    try:
        obs = _mk_obs('shadow then tomb', project='proj-A')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, '2026-05-18T00:00:00.000000Z')

        deleted_at = '2026-05-19T00:00:00.000000Z'
        idx.mark_deleted(obs.observation_id, deleted_at)

        with sqlite3.connect(str(tmp_path / 'delete_clears_shadow.db')) as raw:
            got = raw.execute(
                'SELECT deleted_at, shadowed_at FROM obs_index WHERE observation_id = ?',
                (obs.observation_id,),
            ).fetchone()
            assert got == (deleted_at, None)
    finally:
        idx.close()


def test_local_index_disable_env_var_makes_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('MESH_MEM_DISABLE_INDEX', '1')
    db_path = tmp_path / 'disabled.db'
    idx = LocalIndex.connect(str(db_path))
    try:
        assert idx.disabled is True
        # Methods short-circuit cleanly
        idx.upsert(_mk_obs('ignored'))
        idx.mark_deleted('whatever', '2026-04-27T00:00:00Z')
        assert idx.row_count() == 0
        assert idx.search_by_project('proj-A') == []
        # No DB file should be created when disabled
        assert not db_path.exists(), 'disabled LocalIndex must not touch disk'
    finally:
        idx.close()


def test_local_index_query_by_project_returns_recent(tmp_path: Path) -> None:
    """search_by_project orders DESC by created_at and respects limit.

    Phase 3 will rely on this ordering — guard it now so a future schema
    tweak that drops the idx_project_created index gets caught.
    """
    idx = LocalIndex.connect(str(tmp_path / 'order.db'))
    try:
        # Insert with explicit increasing timestamps so order is deterministic.
        for i in range(5):
            obs = _mk_obs(f'row-{i}', project='proj-A')
            obs.created_at = f'2026-04-27T21:0{i}:00.000000Z'
            idx.upsert(obs)
            time.sleep(0.001)  # cheap insurance against same-ts collisions on slow CI

        # Insert into a different project to ensure the WHERE filter works.
        other = _mk_obs('other', project='proj-B')
        other.created_at = '2026-04-27T21:99:00.000000Z'
        idx.upsert(other)

        results = idx.search_by_project('proj-A', limit=3)
        assert len(results) == 3
        contents = [r.content for r in results]
        assert contents == ['row-4', 'row-3', 'row-2'], 'must be DESC by created_at'

        # limit larger than rows returns all
        results_all = idx.search_by_project('proj-A', limit=100)
        assert len(results_all) == 5

        # cross-project query is empty for proj-C
        assert idx.search_by_project('proj-C') == []
    finally:
        idx.close()


def test_search_query_substring_match(tmp_path: Path) -> None:
    """``search(query=...)`` does a case-insensitive substring match against payload."""
    idx = LocalIndex.connect(str(tmp_path / 'sub.db'))
    try:
        idx.upsert(_mk_obs('Replication digest mismatch', project='ops'))
        idx.upsert(_mk_obs('hello world', project='ops'))
        idx.upsert(_mk_obs('zenoh hot era split', project='ops'))

        hits = idx.search(query='replication')
        contents = {r.content for r in hits}
        assert contents == {'Replication digest mismatch'}, 'case-insensitive match expected'

        # substring inside content also wins
        hits2 = idx.search(query='hot era')
        assert {r.content for r in hits2} == {'zenoh hot era split'}

        # non-match yields empty list, not error
        assert idx.search(query='nothing-matches-this') == []
    finally:
        idx.close()


def test_search_since_iso_filter(tmp_path: Path) -> None:
    """``since_iso`` keeps rows whose ``created_at`` is lex-greater-or-equal."""
    idx = LocalIndex.connect(str(tmp_path / 'since.db'))
    try:
        old = _mk_obs('old', project='since')
        old.created_at = '2020-01-01T00:00:00.000000Z'
        recent = _mk_obs('recent', project='since')
        recent.created_at = '2025-06-01T00:00:00.000000Z'
        idx.upsert(old)
        idx.upsert(recent)

        hits = idx.search(project='since', since_iso='2024-01-01T00:00:00Z')
        assert {r.content for r in hits} == {'recent'}
    finally:
        idx.close()


def test_search_until_iso_filter(tmp_path: Path) -> None:
    """``until_iso`` keeps rows whose ``created_at`` is lex-less-or-equal.

    Inclusive upper bound matters for the bulk-delete cursor (#66): the
    next page passes the previous page's last ``created_at`` as
    ``until_iso`` and relies on dedup-by-observation_id to drop the row
    that sits exactly on the boundary.
    """
    idx = LocalIndex.connect(str(tmp_path / 'until.db'))
    try:
        old = _mk_obs('old', project='until')
        old.created_at = '2020-01-01T00:00:00.000000Z'
        boundary = _mk_obs('boundary', project='until')
        boundary.created_at = '2024-01-01T00:00:00.000000Z'
        recent = _mk_obs('recent', project='until')
        recent.created_at = '2025-06-01T00:00:00.000000Z'
        idx.upsert(old)
        idx.upsert(boundary)
        idx.upsert(recent)

        hits = idx.search(project='until', until_iso='2024-01-01T00:00:00.000000Z')
        assert {r.content for r in hits} == {'old', 'boundary'}
    finally:
        idx.close()


def test_search_cursor_observation_id_strict_tuple(tmp_path: Path) -> None:
    """``cursor_observation_id`` switches ``until_iso`` to strict-tuple semantics (#66).

    Without strict-tuple cursor, the bulk-delete iterator could not walk
    past a timestamp shared by more rows than fit in one batch. The
    invariant: when both keywords are set, the result must satisfy
    ``(created_at, observation_id) < (until_iso, cursor_observation_id)``
    in DESC order; the boundary row itself is excluded.
    """
    idx = LocalIndex.connect(str(tmp_path / 'cursor.db'))
    try:
        ts = '2025-06-01T00:00:00.000000Z'
        rows = []
        for _ in range(5):
            obs = _mk_obs(f'row-{_}', project='cursor')
            obs.created_at = ts
            idx.upsert(obs)
            rows.append(obs)
        rows.sort(key=lambda o: o.observation_id, reverse=True)
        # Use the third-largest row as the cursor — expect only the
        # strictly smaller two ids to come back.
        cursor = rows[2]
        hits = idx.search(
            project='cursor',
            until_iso=ts,
            cursor_observation_id=cursor.observation_id,
            limit=10,
        )
        returned_ids = [r.observation_id for r in hits]
        assert returned_ids == [r.observation_id for r in rows[3:]], 'strict-tuple cursor must skip the boundary row'
    finally:
        idx.close()


def test_search_order_by_includes_observation_id_tiebreaker(tmp_path: Path) -> None:
    """Rows sharing the same ``created_at`` must sort by ``observation_id`` DESC.

    Stable secondary order is what makes the bulk-delete cursor (#66)
    correct under timestamp ties; without it pagination could re-emit or
    skip rows that share the boundary timestamp.
    """
    idx = LocalIndex.connect(str(tmp_path / 'tie.db'))
    try:
        ts = '2025-06-01T00:00:00.000000Z'
        a = _mk_obs('a', project='tie')
        a.created_at = ts
        b = _mk_obs('b', project='tie')
        b.created_at = ts
        c = _mk_obs('c', project='tie')
        c.created_at = ts
        idx.upsert(a)
        idx.upsert(b)
        idx.upsert(c)

        hits = idx.search(project='tie', limit=10)
        ids = [r.observation_id for r in hits]
        assert ids == sorted(ids, reverse=True), 'observation_id DESC tiebreaker expected'
    finally:
        idx.close()


def test_search_excludes_deleted_by_default(tmp_path: Path) -> None:
    """Tombstoned rows must not appear in default ``search`` results."""
    idx = LocalIndex.connect(str(tmp_path / 'exclude.db'))
    try:
        kept = _mk_obs('still alive', project='live')
        gone = _mk_obs('was deleted', project='live')
        idx.upsert(kept)
        idx.upsert(gone)
        idx.mark_deleted(gone.observation_id, '2026-04-30T00:00:00.000000Z')

        hits = idx.search(project='live')
        ids = {r.observation_id for r in hits}
        assert kept.observation_id in ids
        assert gone.observation_id not in ids
    finally:
        idx.close()


def test_search_includes_deleted_when_requested(tmp_path: Path) -> None:
    """``include_deleted=True`` surfaces tombstoned rows alongside live ones."""
    idx = LocalIndex.connect(str(tmp_path / 'include.db'))
    try:
        kept = _mk_obs('alive', project='live')
        gone = _mk_obs('tombstoned', project='live')
        idx.upsert(kept)
        idx.upsert(gone)
        idx.mark_deleted(gone.observation_id, '2026-04-30T00:00:00.000000Z')

        hits = idx.search(project='live', include_deleted=True)
        ids = {r.observation_id for r in hits}
        assert kept.observation_id in ids
        assert gone.observation_id in ids, 'tombstoned row must appear when include_deleted=True'
    finally:
        idx.close()


def test_search_excludes_shadowed_rows_by_default(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'shadow_exclude.db'))
    try:
        kept = _mk_obs('alive', project='live')
        hidden = _mk_obs('temporarily missing upstream', project='live')
        idx.upsert(kept)
        idx.upsert(hidden)
        idx.mark_shadowed_missing(hidden.observation_id, '2026-05-18T00:00:00.000000Z')

        hits = idx.search(project='live')
        ids = {r.observation_id for r in hits}
        assert kept.observation_id in ids
        assert hidden.observation_id not in ids
    finally:
        idx.close()


def test_search_includes_shadowed_rows_when_requested(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'shadow_include.db'))
    try:
        hidden = _mk_obs('temporarily missing upstream', project='live')
        idx.upsert(hidden)
        idx.mark_shadowed_missing(hidden.observation_id, '2026-05-18T00:00:00.000000Z')

        hits = idx.search(project='live', include_deleted=True)
        ids = {r.observation_id for r in hits}
        assert hidden.observation_id in ids
    finally:
        idx.close()


def test_find_by_id_returns_observation(tmp_path: Path) -> None:
    """``find_by_id`` round-trips a stored observation; missing ids return None."""
    idx = LocalIndex.connect(str(tmp_path / 'findby.db'))
    try:
        obs = _mk_obs('locate me', project='find')
        idx.upsert(obs)

        hit = idx.find_by_id(obs.observation_id)
        assert hit is not None
        assert hit.observation_id == obs.observation_id
        assert hit.content == 'locate me'

        assert idx.find_by_id('00000000000000000000000000000000') is None
    finally:
        idx.close()


def test_find_by_id_excludes_deleted_by_default_but_include_flag_works(tmp_path: Path) -> None:
    """Tombstoned obs are hidden from ``find_by_id`` unless ``include_deleted=True``."""
    idx = LocalIndex.connect(str(tmp_path / 'findbydel.db'))
    try:
        obs = _mk_obs('about to disappear', project='find')
        idx.upsert(obs)
        idx.mark_deleted(obs.observation_id, '2026-04-30T00:00:00.000000Z')

        assert idx.find_by_id(obs.observation_id) is None
        # Delete / gc paths in store.py rely on this branch to physically
        # purge tombstoned rows.
        hit = idx.find_by_id(obs.observation_id, include_deleted=True)
        assert hit is not None
        assert hit.observation_id == obs.observation_id
    finally:
        idx.close()


def test_find_by_id_excludes_shadowed_by_default_but_include_flag_works(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'findbyshadow.db'))
    try:
        obs = _mk_obs('shadow me', project='find')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, '2026-05-18T00:00:00.000000Z')

        assert idx.find_by_id(obs.observation_id) is None
        hit = idx.find_by_id(obs.observation_id, include_deleted=True)
        assert hit is not None
        assert hit.observation_id == obs.observation_id
    finally:
        idx.close()


def test_upsert_clears_shadowed_state(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'shadow_clear.db'))
    try:
        obs = _mk_obs('will return', project='shadow')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, '2026-05-18T00:00:00.000000Z')
        assert idx.search(project='shadow') == []

        idx.upsert(obs)
        hits = idx.search(project='shadow')
        assert [r.observation_id for r in hits] == [obs.observation_id]
    finally:
        idx.close()


def test_mark_shadowed_missing_is_noop_for_tombstoned_row(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'shadow_noop_tombed.db'))
    try:
        obs = _mk_obs('already tombstoned', project='shadow')
        idx.upsert(obs)
        deleted_at = '2026-05-19T00:00:00.000000Z'
        idx.mark_deleted(obs.observation_id, deleted_at)
        idx.mark_shadowed_missing(obs.observation_id, '2026-05-20T00:00:00.000000Z')

        with sqlite3.connect(str(tmp_path / 'shadow_noop_tombed.db')) as raw:
            got = raw.execute(
                'SELECT deleted_at, shadowed_at FROM obs_index WHERE observation_id = ?',
                (obs.observation_id,),
            ).fetchone()
            assert got == (deleted_at, None)
    finally:
        idx.close()


def test_physical_delete_removes_row(tmp_path: Path) -> None:
    """``physical_delete`` hard-deletes the row; subsequent lookups return None."""
    idx = LocalIndex.connect(str(tmp_path / 'phys.db'))
    try:
        obs = _mk_obs('purge me', project='gc')
        idx.upsert(obs)
        assert idx.row_count() == 1

        idx.physical_delete(obs.observation_id)
        assert idx.row_count() == 0
        assert idx.find_by_id(obs.observation_id, include_deleted=True) is None

        # Idempotent on miss
        idx.physical_delete('00000000000000000000000000000000')
    finally:
        idx.close()


def test_search_identity_filters(tmp_path: Path) -> None:
    """agent_family / client_id / pc_id / session_id filter via json_extract."""
    idx = LocalIndex.connect(str(tmp_path / 'identity.db'))
    try:
        a = _mk_obs('A', project='id')
        a.session_id = 'session-keep'
        b = _mk_obs('B', project='id')
        b.session_id = 'session-drop'
        idx.upsert(a)
        idx.upsert(b)

        hits = idx.search(project='id', session_id='session-keep')
        assert {r.content for r in hits} == {'A'}
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Helpers for rebuild_from_zenoh unit tests (no live zenohd required)
# ---------------------------------------------------------------------------


class _FakePayload:
    def __init__(self, s: str) -> None:
        self._s = s

    def to_string(self) -> str:
        return self._s


class _FakeOk:
    def __init__(self, s: str, key_expr: str) -> None:
        self.payload = _FakePayload(s)
        self.key_expr = key_expr


class _FakeReply:
    def __init__(self, s: str, key_expr: str) -> None:
        self.ok = _FakeOk(s, key_expr)


class _FakeSession:
    """Minimal zenoh session mock for rebuild_from_zenoh tests.

    Replies carry canonical key strings because ``rebuild_from_zenoh``
    validates each reply key against the keyspace parser before ingesting
    the payload (PR #177 Codex review).
    """

    def __init__(self, obs: list[Observation], tombs: list[Tombstone]) -> None:
        self._obs_replies = [_FakeReply(o.to_json(), o.key_expr) for o in obs]
        self._tomb_replies = [_FakeReply(t.to_json(), f'mem/tomb/f/c/p/s/{t.observation_id}') for t in tombs]

    def get(self, key_expr: str, **kwargs: object) -> list[_FakeReply]:  # type: ignore[override]
        # Selectors are namespace-broadened since ADR-0019 Phase A
        # ('mem/**/obs/**'), so dispatch on the obs/tomb marker chunk.
        if '/obs/' in key_expr:
            return self._obs_replies
        if '/tomb/' in key_expr:
            return self._tomb_replies
        return []


# ---------------------------------------------------------------------------
# rebuild_from_zenoh tests
# ---------------------------------------------------------------------------


def test_rebuild_from_zenoh_populates_empty_index(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_pop.db'))
    try:
        obs1 = _mk_obs('alpha', project='r')
        obs2 = _mk_obs('beta', project='r')
        session = _FakeSession([obs1, obs2], [])

        stats = idx.rebuild_from_zenoh(session)

        assert isinstance(stats, RebuildStats)
        assert stats.added == 2
        assert stats.marked_deleted == 0
        assert stats.unchanged == 0
        assert idx.row_count() == 2
        ids = {r.observation_id for r in idx.search(project='r')}
        assert obs1.observation_id in ids
        assert obs2.observation_id in ids
    finally:
        idx.close()


def test_rebuild_from_zenoh_marks_tombstones(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_tomb.db'))
    try:
        obs = _mk_obs('will be deleted', project='r')
        tomb = Tombstone(observation_id=obs.observation_id, deleted_at='2026-04-30T00:00:00.000000Z')
        session = _FakeSession([obs], [tomb])

        stats = idx.rebuild_from_zenoh(session)

        assert stats.added == 1
        assert stats.marked_deleted == 1
        # Row exists but is marked deleted; search (which filters deleted) returns empty.
        assert idx.search(project='r') == []
        assert idx.row_count() == 1
    finally:
        idx.close()


def test_rebuild_from_zenoh_does_not_overwrite_existing_tombstone_timestamp(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_tomb_preserve.db'))
    try:
        obs = _mk_obs('preserve first tomb time', project='r')
        first_deleted_at = '2026-04-30T00:00:00.000000Z'
        later_deleted_at = '2026-05-01T00:00:00.000000Z'
        idx.upsert(obs)
        idx.mark_deleted(obs.observation_id, first_deleted_at)

        stats = idx.rebuild_from_zenoh(
            _FakeSession([obs], [Tombstone(observation_id=obs.observation_id, deleted_at=later_deleted_at)])
        )

        assert stats.marked_deleted == 0
        with sqlite3.connect(str(tmp_path / 'rebuild_tomb_preserve.db')) as raw:
            (got,) = raw.execute(
                'SELECT deleted_at FROM obs_index WHERE observation_id = ?',
                (obs.observation_id,),
            ).fetchone()
            assert got == first_deleted_at
    finally:
        idx.close()


def test_rebuild_from_zenoh_clears_shadow_when_tombstone_arrives(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_shadow_tomb.db'))
    try:
        obs = _mk_obs('shadow then tomb', project='r')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, '2026-05-18T00:00:00.000000Z')

        deleted_at = '2026-05-19T00:00:00.000000Z'
        stats = idx.rebuild_from_zenoh(
            _FakeSession([], [Tombstone(observation_id=obs.observation_id, deleted_at=deleted_at)])
        )

        assert stats.marked_deleted == 1
        with sqlite3.connect(str(tmp_path / 'rebuild_shadow_tomb.db')) as raw:
            got = raw.execute(
                'SELECT deleted_at, shadowed_at FROM obs_index WHERE observation_id = ?',
                (obs.observation_id,),
            ).fetchone()
            assert got == (deleted_at, None)
    finally:
        idx.close()


def test_rebuild_from_zenoh_skips_writes_for_unchanged_live_rows(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_noop.db'))
    try:
        obs = _mk_obs('stable payload', project='r')
        idx.upsert(obs)
        initial_counter = idx._upserts_since_checkpoint  # noqa: SLF001

        stats = idx.rebuild_from_zenoh(_FakeSession([obs], []))

        assert stats.added == 0
        assert stats.unchanged == 1
        assert idx._upserts_since_checkpoint == initial_counter  # noqa: SLF001
    finally:
        idx.close()


def test_rebuild_from_zenoh_idempotent(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_idem.db'))
    try:
        obs = _mk_obs('stable', project='r')
        idx.upsert(obs)
        session = _FakeSession([obs], [])

        stats = idx.rebuild_from_zenoh(session)

        assert stats.added == 0
        assert stats.unchanged == 1
        assert idx.row_count() == 1

        # Running again is still a no-op.
        stats2 = idx.rebuild_from_zenoh(session)
        assert stats2.added == 0
        assert stats2.unchanged == 1
    finally:
        idx.close()


def test_rebuild_from_zenoh_handles_partial_index(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_partial.db'))
    try:
        existing = _mk_obs('already indexed', project='r')
        idx.upsert(existing)

        new_obs = _mk_obs('newly replicated', project='r')
        session = _FakeSession([existing, new_obs], [])

        stats = idx.rebuild_from_zenoh(session)

        assert stats.added == 1
        assert stats.unchanged == 1
        assert idx.row_count() == 2
        ids = {r.observation_id for r in idx.search(project='r')}
        assert existing.observation_id in ids
        assert new_obs.observation_id in ids
    finally:
        idx.close()


def test_rebuild_from_zenoh_shadows_missing_live_rows_instead_of_hard_deleting(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_shadow.db'))
    try:
        missing = _mk_obs('cached only', project='r')
        idx.upsert(missing)

        stats = idx.rebuild_from_zenoh(_FakeSession([], []))

        assert stats.added == 0
        assert stats.shadowed == 1
        assert idx.row_count() == 1
        assert idx.search(project='r') == []
        hit = idx.find_by_id(missing.observation_id, include_deleted=True)
        assert hit is not None
        assert hit.observation_id == missing.observation_id
    finally:
        idx.close()


def test_rebuild_from_zenoh_clears_shadow_when_obs_reappears(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_unshadow.db'))
    try:
        obs = _mk_obs('returns from zenoh', project='r')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, '2026-05-18T00:00:00.000000Z')

        stats = idx.rebuild_from_zenoh(_FakeSession([obs], []))

        assert stats.added == 0
        assert stats.unchanged == 0
        assert stats.shadowed == 0
        ids = {r.observation_id for r in idx.search(project='r')}
        assert obs.observation_id in ids
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Issue #32 — project-scoped tombstone enumeration + WAL checkpoint
# ---------------------------------------------------------------------------


def test_list_tombstoned_obs_in_project_isolates_by_project(tmp_path: Path) -> None:
    """Project filter restricts results to matching ``project`` column.

    Validates the query that drives ``store.gc_expired_tombstones`` fast
    path (#32) — wrong-project tombs must not appear in the result set.
    """
    idx = LocalIndex.connect(str(tmp_path / 'list_tomb.db'))
    try:
        live = _mk_obs('live in proj-A', project='proj-a')
        idx.upsert(live)

        tombed_a = _mk_obs('tombed in proj-A', project='proj-a')
        idx.upsert(tombed_a)
        idx.mark_deleted(tombed_a.observation_id, '2026-01-01T00:00:00.000000Z')

        tombed_b = _mk_obs('tombed in proj-B', project='proj-b')
        idx.upsert(tombed_b)
        idx.mark_deleted(tombed_b.observation_id, '2026-01-01T00:00:00.000000Z')

        cutoff = '2026-04-01T00:00:00.000000Z'

        a_rows = idx.list_tombstoned_obs_in_project('proj-a', cutoff)
        a_ids = {row[0] for row in a_rows}
        assert a_ids == {tombed_a.observation_id}, f'proj-a result must contain only tombed_a, got {a_ids}'

        b_rows = idx.list_tombstoned_obs_in_project('proj-b', cutoff)
        b_ids = {row[0] for row in b_rows}
        assert b_ids == {tombed_b.observation_id}

        empty = idx.list_tombstoned_obs_in_project('proj-c', cutoff)
        assert empty == []
    finally:
        idx.close()


def test_list_tombstoned_obs_in_project_respects_cutoff(tmp_path: Path) -> None:
    """Tombs newer than cutoff must NOT appear in the result set."""
    idx = LocalIndex.connect(str(tmp_path / 'list_tomb_cutoff.db'))
    try:
        old = _mk_obs('aged tomb', project='p')
        idx.upsert(old)
        idx.mark_deleted(old.observation_id, '2026-01-01T00:00:00.000000Z')

        fresh = _mk_obs('fresh tomb', project='p')
        idx.upsert(fresh)
        idx.mark_deleted(fresh.observation_id, '2026-05-01T00:00:00.000000Z')

        cutoff = '2026-03-01T00:00:00.000000Z'
        rows = idx.list_tombstoned_obs_in_project('p', cutoff)
        ids = {row[0] for row in rows}
        assert ids == {old.observation_id}, 'fresh tomb (after cutoff) must be excluded'
    finally:
        idx.close()


def test_list_tombstoned_obs_in_project_skips_live_rows(tmp_path: Path) -> None:
    """Live rows (``deleted_at IS NULL``) must not appear regardless of project."""
    idx = LocalIndex.connect(str(tmp_path / 'list_tomb_live.db'))
    try:
        live = _mk_obs('still live', project='p')
        idx.upsert(live)

        rows = idx.list_tombstoned_obs_in_project('p', '2099-01-01T00:00:00.000000Z')
        assert rows == []
    finally:
        idx.close()


def test_list_tombstoned_obs_in_project_skips_shadowed_rows(tmp_path: Path) -> None:
    """Rebuild-shadowed rows must not enter tombstone-driven GC queries."""
    idx = LocalIndex.connect(str(tmp_path / 'list_tomb_shadow.db'))
    try:
        shadowed = _mk_obs('missing in rebuild only', project='p')
        idx.upsert(shadowed)
        idx.mark_shadowed_missing(shadowed.observation_id, '2026-05-18T00:00:00.000000Z')

        rows = idx.list_tombstoned_obs_in_project('p', '2099-01-01T00:00:00.000000Z')
        assert rows == []
    finally:
        idx.close()


def test_list_tombstoned_obs_in_project_returns_empty_when_disabled() -> None:
    """Disabled instance returns empty list without touching SQLite."""
    idx = LocalIndex(db_path='', disabled=True)
    assert idx.list_tombstoned_obs_in_project('any', '2026-01-01T00:00:00.000000Z') == []


# ---------------------------------------------------------------------------
# Issue #70 — shadow retention sweep
# ---------------------------------------------------------------------------


def test_list_expired_shadowed_obs_respects_cutoff(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'shadow_expire.db'))
    try:
        aged = _mk_obs('aged shadow', project='p')
        idx.upsert(aged)
        idx.mark_shadowed_missing(aged.observation_id, '2026-01-01T00:00:00.000000Z')

        fresh = _mk_obs('fresh shadow', project='p')
        idx.upsert(fresh)
        idx.mark_shadowed_missing(fresh.observation_id, '2026-05-01T00:00:00.000000Z')

        cutoff = '2026-04-01T00:00:00.000000Z'
        ids = set(idx.list_expired_shadowed_obs(cutoff))
        assert ids == {aged.observation_id}
    finally:
        idx.close()


def test_list_expired_shadowed_obs_filters_by_project(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'shadow_project.db'))
    try:
        a = _mk_obs('proj-a shadow', project='proj-a')
        idx.upsert(a)
        idx.mark_shadowed_missing(a.observation_id, '2026-01-01T00:00:00.000000Z')

        b = _mk_obs('proj-b shadow', project='proj-b')
        idx.upsert(b)
        idx.mark_shadowed_missing(b.observation_id, '2026-01-01T00:00:00.000000Z')

        cutoff = '2026-04-01T00:00:00.000000Z'
        only_a = idx.list_expired_shadowed_obs(cutoff, project='proj-a')
        assert only_a == [a.observation_id]
        only_b = idx.list_expired_shadowed_obs(cutoff, project='proj-b')
        assert only_b == [b.observation_id]
        none = idx.list_expired_shadowed_obs(cutoff, project='proj-c')
        assert none == []
    finally:
        idx.close()


def test_list_expired_shadowed_obs_skips_tombstoned_rows(tmp_path: Path) -> None:
    """Rows with both deleted_at and an old shadowed_at must NOT appear here.

    Tombstones own the retention path for those rows; the shadow sweep
    deliberately filters ``deleted_at IS NULL`` so the same row is not
    accounted for twice.
    """
    idx = LocalIndex.connect(str(tmp_path / 'shadow_skip_tomb.db'))
    try:
        obs = _mk_obs('shadow first, then tomb', project='p')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, '2026-01-01T00:00:00.000000Z')
        # mark_deleted clears shadowed_at, so this branch tests the
        # belt-and-suspenders WHERE clause in case a future change ever
        # leaves shadowed_at set alongside deleted_at.
        idx.mark_deleted(obs.observation_id, '2026-02-01T00:00:00.000000Z')

        ids = idx.list_expired_shadowed_obs('2026-04-01T00:00:00.000000Z')
        assert ids == []
    finally:
        idx.close()


def test_list_expired_shadowed_obs_returns_empty_when_disabled() -> None:
    idx = LocalIndex(db_path='', disabled=True)
    assert idx.list_expired_shadowed_obs('2026-01-01T00:00:00.000000Z') == []


def test_close_runs_wal_checkpoint(tmp_path: Path) -> None:
    """``close()`` truncates the WAL so the file does not stay full on disk (#32).

    Drive enough upserts to grow the WAL, then close — afterwards the WAL
    file should be small (or absent), proving the explicit
    ``PRAGMA wal_checkpoint(TRUNCATE)`` ran.
    """
    db_path = tmp_path / 'wal_close.db'
    idx = LocalIndex.connect(str(db_path))
    for i in range(50):
        idx.upsert(_mk_obs(f'wal-fill-{i}', project='wal-test'))
    wal_path = tmp_path / 'wal_close.db-wal'
    # The WAL file may or may not exist mid-flight (auto-checkpoint can land
    # before our manual close); the assertion that matters is post-close.
    idx.close()
    if wal_path.exists():
        assert (
            wal_path.stat().st_size == 0
        ), f'WAL must be truncated to zero by close(), got {wal_path.stat().st_size} bytes'


def test_periodic_wal_checkpoint_resets_counter(tmp_path: Path) -> None:
    """The internal upsert counter rolls over after the configured cadence (#32).

    Drives more than ``_CHECKPOINT_EVERY_N_UPSERTS`` upserts and asserts
    the counter wrapped — proves the periodic checkpoint path actually
    fired without depending on filesystem timing of WAL truncation.
    """
    from mesh_mem.local_index import _CHECKPOINT_EVERY_N_UPSERTS

    idx = LocalIndex.connect(str(tmp_path / 'wal_cadence.db'))
    try:
        for i in range(_CHECKPOINT_EVERY_N_UPSERTS + 5):
            idx.upsert(_mk_obs(f'cadence-{i}', project='cadence-test'))
        # Counter resets to 0 at the checkpoint boundary, then increments
        # by the trailing upserts (5 here).
        assert idx._upserts_since_checkpoint == 5  # noqa: SLF001
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0021 — FTS5 (trigram) full-text search + supersedes-aware filtering
# ---------------------------------------------------------------------------


def test_fts_capable_on_modern_sqlite(tmp_path: Path) -> None:
    """The bundled CPython SQLite (≥3.34) supports FTS5 trigram; connect reports it."""
    idx = LocalIndex.connect(str(tmp_path / 'fts_cap.db'))
    try:
        assert idx.fts_capable is True, 'CI SQLite is expected to support FTS5 trigram'
    finally:
        idx.close()


def test_fts_search_excludes_identity_field_matches(tmp_path: Path) -> None:
    """ADR-0021 headline win: FTS indexes semantic fields only, not identity hex.

    The old LIKE path matched ``pc_id`` / ``session_id`` substrings inside
    ``payload_json``. FTS restricts the index to content/subject/summary/
    tags/project, so a query that only appears in an identity field returns
    nothing — eliminating the known UUID false-positive source.
    """
    idx = LocalIndex.connect(str(tmp_path / 'fts_identity.db'))
    if not idx.fts_capable:  # pragma: no cover
        idx.close()
        pytest.skip('FTS5 trigram not available in this SQLite build')
    try:
        # 'testpc' is the pc_id in _mk_obs; it must NOT leak into search.
        idx.upsert(_mk_obs('completely unrelated body text', project='proj'))
        hits = idx.search(query='testpc')
        assert hits == [], 'identity-field substring must not match via FTS'
    finally:
        idx.close()


def test_fts_search_multiword_and(tmp_path: Path) -> None:
    """FTS trigram supports multi-word phrase matching that LIKE could only do as one substring."""
    idx = LocalIndex.connect(str(tmp_path / 'fts_multiword.db'))
    if not idx.fts_capable:  # pragma: no cover
        idx.close()
        pytest.skip('FTS5 trigram not available in this SQLite build')
    try:
        idx.upsert(_mk_obs('Replication digest mismatch', project='ops'))
        idx.upsert(_mk_obs('zenoh hot era split brain', project='ops'))
        hits = idx.search(query='hot era')
        assert {r.content for r in hits} == {'zenoh hot era split brain'}
    finally:
        idx.close()


def test_fts_search_ranks_by_relevance(tmp_path: Path) -> None:
    """Queries route through bm25: the doc with the term in more fields ranks first.

    A doc mentioning the query term in subject AND content should outrank a
    doc that only mentions it once in a long unrelated body. We assert the
    strong match is first rather than pinning exact scores (bm25 values are
    implementation-defined).
    """
    idx = LocalIndex.connect(str(tmp_path / 'fts_rank.db'))
    if not idx.fts_capable:  # pragma: no cover
        idx.close()
        pytest.skip('FTS5 trigram not available in this SQLite build')
    try:
        weak = _mk_obs(
            'a long note about many things including a stray quorum mention near the end',
            project='ops',
        )
        weak.created_at = '2020-01-01T00:00:00.000000Z'  # older, so created_at tiebreak would put it last anyway
        strong = _mk_obs('quorum loss incident', project='ops', subject='quorum quorum quorum')
        strong.created_at = '2019-01-01T00:00:00.000000Z'  # older than weak: only bm25 can float it up
        idx.upsert(weak)
        idx.upsert(strong)

        hits = idx.search(query='quorum')
        assert len(hits) == 2
        assert hits[0].content == 'quorum loss incident', 'bm25 must rank the stronger match first'
    finally:
        idx.close()


def test_short_query_falls_back_to_like(tmp_path: Path) -> None:
    """Queries under 3 chars cannot use the trigram index and fall back to LIKE.

    Under LIKE the match runs against the full payload, so even a 2-char
    identity substring resolves — proving the fallback path is active rather
    than the FTS path (which would return nothing for identity fields).
    """
    idx = LocalIndex.connect(str(tmp_path / 'short_q.db'))
    try:
        idx.upsert(_mk_obs('ab unique marker body', project='proj'))
        # 2-char query against content substring works via LIKE.
        hits = idx.search(query='ab')
        assert {r.content for r in hits} == {'ab unique marker body'}
    finally:
        idx.close()


def test_supersedes_hidden_by_default(tmp_path: Path) -> None:
    """An observation superseded by a live newer one is hidden from default search."""
    idx = LocalIndex.connect(str(tmp_path / 'supersede_hide.db'))
    try:
        old = _mk_obs('decision v1', project='dec')
        idx.upsert(old)
        new = _mk_obs('decision v2', project='dec', supersedes=[old.observation_id])
        idx.upsert(new)

        hits = idx.search(project='dec')
        ids = {r.observation_id for r in hits}
        assert new.observation_id in ids
        assert old.observation_id not in ids, 'superseded entry must be hidden by default'
    finally:
        idx.close()


def test_supersedes_visible_with_include_flag(tmp_path: Path) -> None:
    """``include_superseded=True`` surfaces the older entry alongside the newer one."""
    idx = LocalIndex.connect(str(tmp_path / 'supersede_show.db'))
    try:
        old = _mk_obs('decision v1', project='dec')
        idx.upsert(old)
        new = _mk_obs('decision v2', project='dec', supersedes=[old.observation_id])
        idx.upsert(new)

        hits = idx.search(project='dec', include_superseded=True)
        ids = {r.observation_id for r in hits}
        assert old.observation_id in ids
        assert new.observation_id in ids
    finally:
        idx.close()


def test_supersedes_existence_based_reappears_when_superseder_deleted(tmp_path: Path) -> None:
    """Existence-based hiding: if the superseding obs is tombstoned, the old one returns.

    ADR-0021 §B: hiding is keyed on the *existence* of a live superseder, not
    a stored boolean, so deleting the newer version makes the older visible
    again without rewriting the older row.
    """
    idx = LocalIndex.connect(str(tmp_path / 'supersede_reappear.db'))
    try:
        old = _mk_obs('decision v1', project='dec')
        idx.upsert(old)
        new = _mk_obs('decision v2', project='dec', supersedes=[old.observation_id])
        idx.upsert(new)
        assert {r.observation_id for r in idx.search(project='dec')} == {new.observation_id}

        # Tombstone the superseder — the old decision should resurface.
        idx.mark_deleted(new.observation_id, '2026-06-01T00:00:00.000000Z')
        hits = idx.search(project='dec')
        assert {r.observation_id for r in hits} == {old.observation_id}
    finally:
        idx.close()


def test_supersedes_existence_based_reappears_when_superseder_shadowed(tmp_path: Path) -> None:
    """A rebuild-shadowed superseder also un-hides the older entry."""
    idx = LocalIndex.connect(str(tmp_path / 'supersede_shadow.db'))
    try:
        old = _mk_obs('decision v1', project='dec')
        idx.upsert(old)
        new = _mk_obs('decision v2', project='dec', supersedes=[old.observation_id])
        idx.upsert(new)
        idx.mark_shadowed_missing(new.observation_id, '2026-06-01T00:00:00.000000Z')

        hits = idx.search(project='dec')
        assert {r.observation_id for r in hits} == {old.observation_id}
    finally:
        idx.close()


def test_supersedes_filter_applies_on_fts_path(tmp_path: Path) -> None:
    """The supersedes filter must also apply when a ≥3-char query routes through FTS."""
    idx = LocalIndex.connect(str(tmp_path / 'supersede_fts.db'))
    if not idx.fts_capable:  # pragma: no cover
        idx.close()
        pytest.skip('FTS5 trigram not available in this SQLite build')
    try:
        old = _mk_obs('quorum strategy original', project='dec')
        idx.upsert(old)
        new = _mk_obs('quorum strategy revised', project='dec', supersedes=[old.observation_id])
        idx.upsert(new)

        hits = idx.search(query='quorum strategy')
        ids = {r.observation_id for r in hits}
        assert new.observation_id in ids
        assert old.observation_id not in ids, 'FTS path must honor supersedes hiding'

        hits_all = idx.search(query='quorum strategy', include_superseded=True)
        assert {r.observation_id for r in hits_all} == {old.observation_id, new.observation_id}
    finally:
        idx.close()


def test_find_superseded_by_returns_live_superseder(tmp_path: Path) -> None:
    """``find_superseded_by`` returns the forward link for get_memory chain display."""
    idx = LocalIndex.connect(str(tmp_path / 'find_super.db'))
    try:
        old = _mk_obs('v1', project='dec')
        idx.upsert(old)
        new = _mk_obs('v2', project='dec', supersedes=[old.observation_id])
        idx.upsert(new)

        assert idx.find_superseded_by(old.observation_id) == new.observation_id
        # The newer one is not superseded by anything.
        assert idx.find_superseded_by(new.observation_id) is None
        # Unknown id → None, no error.
        assert idx.find_superseded_by('00000000000000000000000000000000') is None
    finally:
        idx.close()


def test_physical_delete_purges_fts_entry(tmp_path: Path) -> None:
    """A hard delete must remove the obs_fts row so it stops matching searches."""
    idx = LocalIndex.connect(str(tmp_path / 'fts_phys.db'))
    if not idx.fts_capable:  # pragma: no cover
        idx.close()
        pytest.skip('FTS5 trigram not available in this SQLite build')
    try:
        obs = _mk_obs('searchable token alpha', project='p')
        idx.upsert(obs)
        assert {r.observation_id for r in idx.search(query='searchable token')} == {obs.observation_id}

        idx.physical_delete(obs.observation_id)
        assert idx.search(query='searchable token') == []
        with sqlite3.connect(str(tmp_path / 'fts_phys.db')) as raw:
            (fts_n,) = raw.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
            assert fts_n == 0
    finally:
        idx.close()


def test_migration_v2_to_v3_backfills_supersedes_and_fts(tmp_path: Path) -> None:
    """Opening a schema-v2 DB adds superseded_by + obs_fts and backfills both."""
    db = tmp_path / 'v2.db'
    # Build a v2-shaped DB with one row whose payload carries a supersedes edge.
    old = _mk_obs('older decision', project='m')
    newer = _mk_obs('newer decision', project='m', supersedes=[old.observation_id])
    with sqlite3.connect(str(db)) as raw:
        raw.executescript("""
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version(version) VALUES (2);
CREATE TABLE obs_index (
  observation_id TEXT PRIMARY KEY,
  project TEXT,
  created_at TEXT,
  memory_type TEXT,
  importance INTEGER,
  subject TEXT,
  summary TEXT,
  payload_json TEXT,
  deleted_at TEXT,
  shadowed_at TEXT
);
""")
        for o in (old, newer):
            raw.execute(
                'INSERT INTO obs_index '
                '(observation_id, project, created_at, memory_type, importance, subject, summary, payload_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (o.observation_id, o.project, o.created_at, o.memory_type, o.importance,
                 o.subject, o.summary, o.to_json()),
            )
        raw.commit()

    idx = LocalIndex.connect(str(db))
    try:
        with sqlite3.connect(str(db)) as raw:
            cols = {row[1] for row in raw.execute('PRAGMA table_info(obs_index)')}
            assert 'superseded_by' in cols
            (version,) = raw.execute('SELECT version FROM schema_version').fetchone()
            assert version == SCHEMA_VERSION
            # Backward edge backfilled from payload supersedes list.
            (sb,) = raw.execute(
                'SELECT superseded_by FROM obs_index WHERE observation_id = ?',
                (old.observation_id,),
            ).fetchone()
            assert sb == newer.observation_id
            # FTS backfilled — one entry per obs_index row.
            (fts_n,) = raw.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
            assert fts_n == 2

        # Superseded entry hidden by default after migration.
        hits = idx.search(project='m')
        assert {r.observation_id for r in hits} == {newer.observation_id}
        # And FTS query works on migrated data.
        if idx.fts_capable:
            assert {r.observation_id for r in idx.search(query='newer decision')} == {newer.observation_id}
    finally:
        idx.close()


def test_fts_resync_on_downgrade_upgrade_gap(tmp_path: Path) -> None:
    """obs_fts is rebuilt when it drifts from obs_index (old code wrote rows without FTS).

    Simulates the downgrade→upgrade gap: a row exists in obs_index but is
    missing from obs_fts. On the next connect, the count mismatch triggers a
    full FTS rebuild so the row becomes searchable again.
    """
    db = tmp_path / 'resync.db'
    idx = LocalIndex.connect(str(db))
    obs = _mk_obs('orphaned from fts index', project='p')
    idx.upsert(obs)
    assert idx.fts_capable
    idx.close()

    # Simulate old code: delete the FTS entry but keep the obs_index row.
    with sqlite3.connect(str(db)) as raw:
        raw.execute('DELETE FROM obs_fts WHERE observation_id = ?', (obs.observation_id,))
        raw.commit()
        (fts_n,) = raw.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
        assert fts_n == 0

    # Reconnect: the drift check should rebuild obs_fts.
    idx2 = LocalIndex.connect(str(db))
    try:
        with sqlite3.connect(str(db)) as raw:
            (fts_n,) = raw.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
            assert fts_n == 1, 'obs_fts must be rebuilt after drift'
        assert {r.observation_id for r in idx2.search(query='orphaned from')} == {obs.observation_id}
    finally:
        idx2.close()


def test_search_falls_back_to_like_when_fts_disabled(tmp_path: Path) -> None:
    """When fts_capable is False, search uses LIKE and still returns matches.

    Forces the non-FTS path by flipping the capability flag, mirroring an
    older SQLite build without FTS5.
    """
    idx = LocalIndex.connect(str(tmp_path / 'no_fts.db'))
    try:
        idx._fts_capable = False  # noqa: SLF001 — simulate FTS-less SQLite
        idx.upsert(_mk_obs('replication digest mismatch', project='ops'))
        hits = idx.search(query='replication')
        assert {r.content for r in hits} == {'replication digest mismatch'}
        # LIKE path still honors supersedes filtering.
        old = _mk_obs('legacy choice', project='ops')
        idx.upsert(old)
        new = _mk_obs('legacy choice updated', project='ops', supersedes=[old.observation_id])
        idx.upsert(new)
        hits2 = idx.search(query='legacy choice')
        assert old.observation_id not in {r.observation_id for r in hits2}
    finally:
        idx.close()


def test_rebuild_from_zenoh_populates_fts_and_supersedes(tmp_path: Path) -> None:
    """rebuild_from_zenoh writes FTS entries and supersedes edges in its transaction."""
    idx = LocalIndex.connect(str(tmp_path / 'rebuild_fts.db'))
    try:
        old = _mk_obs('rebuilt original', project='r')
        new = _mk_obs('rebuilt revision', project='r', supersedes=[old.observation_id])
        stats = idx.rebuild_from_zenoh(_FakeSession([old, new], []))
        assert stats.added == 2

        # Supersedes edge materialized → old hidden by default.
        assert {r.observation_id for r in idx.search(project='r')} == {new.observation_id}
        assert idx.find_superseded_by(old.observation_id) == new.observation_id
        # FTS populated for both rows.
        if idx.fts_capable:
            with sqlite3.connect(str(tmp_path / 'rebuild_fts.db')) as raw:
                (fts_n,) = raw.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
                assert fts_n == 2
            assert {r.observation_id for r in idx.search(query='rebuilt revision')} == {new.observation_id}
    finally:
        idx.close()

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
from typing import NoReturn

import pytest

from kioku_mesh.local_index import LocalIndex
from kioku_mesh.local_index import RebuildStats
from kioku_mesh.local_index import SCHEMA_VERSION
from kioku_mesh.models import Observation
from kioku_mesh.models import Tombstone


def _mk_obs(content: str, *, project: str = 'demo', tags: list[str] | None = None) -> Observation:
    return Observation(
        content=content,
        project=project,
        tags=list(tags or []),
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
        visibility='mesh',
    )


def _mk_legacy_obs(content: str, *, project: str = 'demo', tags: list[str] | None = None) -> Observation:
    """Create an obs with legacy visibility (for ADR-0019 Phase D gate tests only)."""
    return Observation(
        content=content,
        project=project,
        tags=list(tags or []),
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
        visibility='',
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


def test_local_index_migrates_legacy_db_and_rebuilds_fts(tmp_path: Path) -> None:
    """Legacy DBs without ``obs_fts`` must become searchable on first open."""
    db = tmp_path / 'legacy_fts.db'
    obs1 = _mk_obs('zenoh replicated memory search', project='legacy', tags=['zenoh'])
    obs2 = _mk_obs('another zenoh note', project='legacy')
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
        raw.executemany(
            'INSERT INTO obs_index '
            '(observation_id, project, created_at, memory_type, importance, subject, summary, payload_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (
                    obs.observation_id,
                    obs.project,
                    obs.created_at,
                    obs.memory_type,
                    obs.importance,
                    obs.subject,
                    obs.summary,
                    obs.to_json(),
                )
                for obs in (obs1, obs2)
            ],
        )
        raw.commit()

    idx = LocalIndex.connect(str(db))
    try:
        hits = idx.search(query='zenoh', project='legacy', include_superseded=True)
        assert {hit.observation_id for hit in hits} == {obs1.observation_id, obs2.observation_id}
        with sqlite3.connect(str(db)) as raw:
            if idx._fts_cap != 'like':  # noqa: SLF001
                tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                assert 'obs_fts' in tables
                (fts_count,) = raw.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
                assert fts_count == 2
    finally:
        idx.close()


def test_direct_constructor_opens_db_and_runs_migration(tmp_path: Path) -> None:
    """``LocalIndex(db_path=...)`` supports task verifier scripts."""
    db = tmp_path / 'direct.db'
    setup = LocalIndex.connect(str(db))
    obs = _mk_obs('zenoh direct constructor search', project='direct')
    try:
        setup.upsert(obs)
    finally:
        setup.close()

    idx = LocalIndex(db_path=db)
    try:
        hits = idx.search(query='zenoh', project='direct', include_superseded=True)
        assert [hit.observation_id for hit in hits] == [obs.observation_id]
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
    monkeypatch.setenv('KIOKU_MESH_DISABLE_INDEX', '1')
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

    def __init__(
        self,
        obs: list[Observation],
        tombs: list[Tombstone],
        *,
        legacy_tomb_keys: bool = False,
    ) -> None:
        self._obs_replies = [_FakeReply(o.to_json(), o.key_expr) for o in obs]
        if legacy_tomb_keys:
            tomb_key_fmt = 'mem/tomb/f/c/p/s/{id}'
        else:
            tomb_key_fmt = 'mem/mesh/tomb/f/c/p/s/{id}'
        self._tomb_replies = [_FakeReply(t.to_json(), tomb_key_fmt.format(id=t.observation_id)) for t in tombs]

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


def test_rebuild_from_zenoh_persists_orphan_tombstone(tmp_path: Path) -> None:
    """R5: a tomb whose obs is absent from both the index and zenoh must not vanish."""
    db_path = tmp_path / 'rebuild_orphan.db'
    idx = LocalIndex.connect(str(db_path))
    try:
        orphan_id = '0' * 32
        deleted_at = '2026-05-01T00:00:00.000000Z'
        tomb = Tombstone(observation_id=orphan_id, deleted_at=deleted_at)

        stats = idx.rebuild_from_zenoh(_FakeSession([], [tomb]))

        assert stats.orphaned == 1
        assert stats.marked_deleted == 0
        assert stats.added == 0
        assert idx.row_count() == 1
        assert idx.search(project='r') == []
        with sqlite3.connect(str(db_path)) as raw:
            got = raw.execute(
                'SELECT deleted_at, payload_json FROM obs_index WHERE observation_id = ?',
                (orphan_id,),
            ).fetchone()
            assert got == (deleted_at, None)
    finally:
        idx.close()


def test_rebuild_from_zenoh_merges_orphan_tombstone_when_obs_reappears(tmp_path: Path) -> None:
    """R5: once the orphan placeholder exists, a later obs arrival must not resurrect it as live."""
    db_path = tmp_path / 'rebuild_orphan_merge.db'
    idx = LocalIndex.connect(str(db_path))
    try:
        obs = _mk_obs('was orphan-tombstoned', project='r')
        deleted_at = '2026-05-01T00:00:00.000000Z'
        tomb = Tombstone(observation_id=obs.observation_id, deleted_at=deleted_at)

        first = idx.rebuild_from_zenoh(_FakeSession([], [tomb]))
        assert first.orphaned == 1

        # obs finally shows up in a later scan; the tombstone is no longer
        # returned this time (e.g. already gc'd upstream) — this is the
        # scenario that must not resurrect the row as live.
        second = idx.rebuild_from_zenoh(_FakeSession([obs], []))

        assert second.marked_deleted == 0
        assert second.orphaned == 0
        assert idx.search(project='r') == []
        with sqlite3.connect(str(db_path)) as raw:
            got = raw.execute(
                'SELECT deleted_at, payload_json FROM obs_index WHERE observation_id = ?',
                (obs.observation_id,),
            ).fetchone()
            assert got[0] == deleted_at
            assert got[1] is not None
        hit = idx.find_by_id(obs.observation_id, include_deleted=True)
        assert hit is not None
        assert hit.observation_id == obs.observation_id
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
        assert wal_path.stat().st_size == 0, (
            f'WAL must be truncated to zero by close(), got {wal_path.stat().st_size} bytes'
        )


def test_periodic_wal_checkpoint_resets_counter(tmp_path: Path) -> None:
    """The internal upsert counter rolls over after the configured cadence (#32).

    Drives more than ``_CHECKPOINT_EVERY_N_UPSERTS`` upserts and asserts
    the counter wrapped — proves the periodic checkpoint path actually
    fired without depending on filesystem timing of WAL truncation.
    """
    from kioku_mesh.local_index import _CHECKPOINT_EVERY_N_UPSERTS

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
# ADR-0021: FTS5 / supersedes-aware search (Issue #203)
# ---------------------------------------------------------------------------


def test_fts5_japanese_query_recall_matches_like(tmp_path: Path) -> None:
    """(a) Japanese query: FTS path recall >= LIKE path recall.

    Both paths must return the row containing the Japanese term. This
    verifies that the FTS5 trigram tokenizer covers the same content
    as LIKE for Japanese substrings.
    """
    idx = LocalIndex.connect(str(tmp_path / 'fts_jp.db'))
    try:
        obs = _mk_obs('kioku-mesh は分散メモリシステムです。', project='jp')
        idx.upsert(obs)

        query = '分散メモリ'
        results = idx.search(query=query, include_superseded=True)
        assert any(r.observation_id == obs.observation_id for r in results), (
            f'Japanese query {query!r} must match via search() (fts_cap={idx._fts_cap!r})'  # noqa: SLF001
        )
    finally:
        idx.close()


def test_short_query_falls_back_to_like(tmp_path: Path) -> None:
    """(b) Query < 3 chars falls back to LIKE even when trigram is available.

    The trigram tokenizer requires at least 3 characters; a 2-char query
    must still work via LIKE so short searches don't silently return nothing.
    """
    idx = LocalIndex.connect(str(tmp_path / 'short_query.db'))
    try:
        obs = _mk_obs('AI is useful for code generation.', project='ai')
        idx.upsert(obs)

        results = idx.search(query='AI', include_superseded=True)
        assert any(r.observation_id == obs.observation_id for r in results), (
            'Short query "AI" must still match via LIKE fallback'
        )
    finally:
        idx.close()


def test_multi_word_query_uses_and_semantics_for_non_contiguous_terms(tmp_path: Path) -> None:
    """Space-separated query terms match even when they are not adjacent."""
    idx = LocalIndex.connect(str(tmp_path / 'multi_word_and.db'))
    try:
        wanted = _mk_obs('branch cleanup notes describe a reusable パターン for rebases', project='and')
        branch_only = _mk_obs('branch cleanup notes without the other term', project='and')
        phrase_only = _mk_obs('a literal branch パターン phrase still works', project='and')
        idx.upsert(wanted)
        idx.upsert(branch_only)
        idx.upsert(phrase_only)

        results = idx.search(query='branch パターン', project='and', include_superseded=True)

        ids = {r.observation_id for r in results}
        assert wanted.observation_id in ids
        assert phrase_only.observation_id in ids
        assert branch_only.observation_id not in ids
    finally:
        idx.close()


def test_multi_word_query_mixes_fts_and_short_like_terms(tmp_path: Path) -> None:
    """Terms shorter than trigram length are enforced with LIKE alongside FTS terms.

    Uses non-hex short term ('qx') to avoid false-positive matches against
    hex observation_id UUID strings stored in payload_json.
    """
    idx = LocalIndex.connect(str(tmp_path / 'mixed_short_terms.db'))
    try:
        # 'qx' is 2 chars (non-hex: q, x ∉ [0-9a-f]) → LIKE path
        # 'migration' is ≥3 chars → FTS path
        wanted = _mk_obs('qx migration caused a tokenizer regression', project='mixed')
        long_only = _mk_obs('migration without the short abbreviation', project='mixed')
        short_only = _mk_obs('qx only mention', project='mixed')
        idx.upsert(wanted)
        idx.upsert(long_only)
        idx.upsert(short_only)

        results = idx.search(query='qx migration', project='mixed', include_superseded=True)

        assert [r.observation_id for r in results] == [wanted.observation_id]
    finally:
        idx.close()


def test_multi_word_query_quotes_fts_special_characters(tmp_path: Path) -> None:
    """FTS operators inside terms are treated as literal text."""
    idx = LocalIndex.connect(str(tmp_path / 'special_chars.db'))
    try:
        wanted = _mk_obs('rebase| notation appears far away from the パターン label', project='special')
        pattern_only = _mk_obs('this row has パターン but no operator token', project='special')
        idx.upsert(wanted)
        idx.upsert(pattern_only)

        results = idx.search(query='rebase| パターン', project='special', include_superseded=True)

        assert [r.observation_id for r in results] == [wanted.observation_id]
    finally:
        idx.close()


def test_fts5_cap_like_fallback_when_fts5_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) When FTS5 is unavailable, capability falls back to LIKE.

    Monkeypatches ``_detect_fts_cap`` to simulate a SQLite build without
    FTS5, then verifies that search still returns results via LIKE.
    """
    import kioku_mesh.local_index as li_mod

    monkeypatch.setattr(li_mod, '_detect_fts_cap', lambda _conn: li_mod._FTS_CAP_LIKE)  # noqa: SLF001

    idx = LocalIndex.connect(str(tmp_path / 'like_only.db'))
    try:
        assert idx._fts_cap == li_mod._FTS_CAP_LIKE  # noqa: SLF001
        obs = _mk_obs('hello world from LIKE path', project='test')
        idx.upsert(obs)
        results = idx.search(query='hello world', include_superseded=True)
        assert any(r.observation_id == obs.observation_id for r in results)
    finally:
        idx.close()


def test_rebuild_fts_normalizes_tags_like_lockstep_upsert(tmp_path: Path) -> None:
    """The rebuild path must store tags with the same space-joined representation as upsert."""
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415
    from kioku_mesh.local_index import _rebuild_fts_from_obs_index  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'fts_tags.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts is unavailable without FTS5 support')

        obs = _mk_obs('tag representation check', project='tags', tags=['zenoh', 'bug'])
        idx.upsert(obs)

        (lockstep_tags,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()

        _rebuild_fts_from_obs_index(idx._conn)  # noqa: SLF001
        (rebuilt_tags,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()

        assert lockstep_tags == 'zenoh bug'
        assert rebuilt_tags == lockstep_tags
    finally:
        idx.close()


def test_superseded_row_hidden_by_default(tmp_path: Path) -> None:
    """(d) A superseded row is hidden from search results by default.

    When obs B supersedes obs A, obs A must not appear in a default
    ``include_superseded=False`` search while obs B is alive.
    """
    idx = LocalIndex.connect(str(tmp_path / 'superseded_hidden.db'))
    try:
        obs_a = _mk_obs('old observation to be superseded', project='p')
        idx.upsert(obs_a)

        obs_b = Observation(
            content='new observation superseding A',
            project='p',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
        )
        idx.upsert(obs_b)

        results = idx.search(include_superseded=False)
        ids = {r.observation_id for r in results}
        assert obs_b.observation_id in ids, 'superseding obs B must be visible'
        assert obs_a.observation_id not in ids, 'superseded obs A must be hidden by default'

        results_with = idx.search(include_superseded=True)
        ids_with = {r.observation_id for r in results_with}
        assert obs_a.observation_id in ids_with, 'superseded obs A visible with include_superseded=True'
    finally:
        idx.close()


def test_superseded_row_restores_when_superseder_deleted(tmp_path: Path) -> None:
    """(e) Deleting the superseder makes the superseded row visible again (existence-based).

    The existence-based filter checks whether the superseder is still
    alive in obs_index. When obs B (the superseder) is tombstoned,
    obs A must reappear in default search results.
    """
    idx = LocalIndex.connect(str(tmp_path / 'superseded_restore.db'))
    try:
        obs_a = _mk_obs('obs A to be restored', project='p')
        idx.upsert(obs_a)

        obs_b = Observation(
            content='obs B supersedes A',
            project='p',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
        )
        idx.upsert(obs_b)

        # Verify obs_a is hidden while obs_b is alive.
        hidden = {r.observation_id for r in idx.search(include_superseded=False)}
        assert obs_a.observation_id not in hidden

        # Tombstone obs_b — obs_a should come back (existence-based).
        idx.mark_deleted(obs_b.observation_id, '2026-06-25T00:00:00.000000Z')

        restored = {r.observation_id for r in idx.search(include_superseded=False)}
        assert obs_a.observation_id in restored, 'obs A must reappear after its superseder is tombstoned'
    finally:
        idx.close()


def test_get_memory_output_contains_superseded_by(tmp_path: Path) -> None:
    """(f) ``find_by_id`` populates ``_extras['superseded_by']`` for use in get_memory.

    After obs B supersedes obs A, ``find_by_id(obs_a.observation_id, include_deleted=True)``
    must expose ``superseded_by`` in the observation's ``_extras`` dict.
    """
    idx = LocalIndex.connect(str(tmp_path / 'superseded_by_field.db'))
    try:
        obs_a = _mk_obs('original obs', project='p')
        idx.upsert(obs_a)

        obs_b = Observation(
            content='replacement obs',
            project='p',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
        )
        idx.upsert(obs_b)

        found = idx.find_by_id(obs_a.observation_id, include_deleted=True)
        assert found is not None
        assert hasattr(found, '_extras')
        assert found._extras.get('superseded_by') == obs_b.observation_id, (  # noqa: SLF001
            f'Expected superseded_by={obs_b.observation_id!r}, got extras={found._extras!r}'  # noqa: SLF001
        )
    finally:
        idx.close()


def test_fts_bm25_ranking_and_tiebreak(tmp_path: Path) -> None:
    """bm25 ranking: more-relevant match ranks first; created_at tie-break is stable.

    Inserts two observations where the second contains the query term more
    densely.  The FTS path must return them bm25-ranked (more relevant first).
    When relevance is equal, newer created_at must appear first (tie-break).
    """
    idx = LocalIndex.connect(str(tmp_path / 'bm25.db'))
    try:
        from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

        # Low-relevance: query term appears once in a long sentence.
        obs_low = _mk_obs(
            'This document is about many topics. The word kioku appears once here.',
            project='rank',
        )
        # High-relevance: query term is the entire content.
        obs_high = _mk_obs('kioku kioku kioku', project='rank')
        idx.upsert(obs_low)
        idx.upsert(obs_high)

        results = idx.search(query='kioku', project='rank', include_superseded=True)
        assert len(results) >= 2, 'Both obs must be returned'
        ids = [r.observation_id for r in results]
        if idx._fts_cap != _FTS_CAP_LIKE:  # noqa: SLF001
            # FTS path: high-relevance (dense) obs should rank first (lower bm25 rank value).
            assert ids.index(obs_high.observation_id) < ids.index(obs_low.observation_id), (
                'High-relevance obs must rank before low-relevance obs in FTS path'
            )

        # Tie-break: two obs with same content (same relevance), newer first.
        obs_older = _mk_obs('tiebreak content same words', project='tie')
        idx.upsert(obs_older)
        time.sleep(0.01)
        obs_newer = _mk_obs('tiebreak content same words', project='tie')
        idx.upsert(obs_newer)

        results_tie = idx.search(query='tiebreak', project='tie', include_superseded=True)
        assert len(results_tie) >= 2
        ids_tie = [r.observation_id for r in results_tie]
        if idx._fts_cap != _FTS_CAP_LIKE:  # noqa: SLF001
            # FTS path: ORDER BY rank, created_at DESC — newer first on tie.
            assert ids_tie.index(obs_newer.observation_id) < ids_tie.index(obs_older.observation_id), (
                'Newer obs must appear before older obs when bm25 rank is equal'
            )
    finally:
        idx.close()


def test_superseded_obs_resurfaces_when_superseder_shadowed(tmp_path: Path) -> None:
    """Shadowing the superseder must make the superseded row visible again.

    The existence-based filter must treat a shadowed superseder as non-live,
    so the superseded row resurfaces without needing a tombstone.
    """
    idx = LocalIndex.connect(str(tmp_path / 'shadow_resurface.db'))
    try:
        obs_a = _mk_obs('obs A to be superseded', project='p')
        idx.upsert(obs_a)

        obs_b = Observation(
            content='obs B supersedes A',
            project='p',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
        )
        idx.upsert(obs_b)

        # obs_a is hidden while obs_b is live.
        hidden = {r.observation_id for r in idx.search(include_superseded=False)}
        assert obs_a.observation_id not in hidden, 'obs_a must be hidden while superseder obs_b is live'

        # Shadow obs_b via mark_shadowed_missing (simulates rebuild not seeing it in Zenoh).
        idx.mark_shadowed_missing(obs_b.observation_id, '2026-06-25T00:00:00.000000Z')

        # obs_a must now resurface — shadowed superseder is no longer "live".
        visible = {r.observation_id for r in idx.search(include_superseded=False)}
        assert obs_a.observation_id in visible, 'obs_a must reappear after its superseder obs_b is shadowed'
    finally:
        idx.close()


def test_rebuild_from_zenoh_restores_fts_and_superseded(tmp_path: Path) -> None:
    """rebuild_from_zenoh() must restore obs_fts and superseded_by as a recovery path.

    Simulates a corrupt FTS index by deleting all obs_fts rows, then calls
    rebuild_from_zenoh() via a fake session.  After rebuild, FTS search and
    include_superseded filtering must both work correctly.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'rebuild_fts.db'))
    try:
        obs_a = _mk_obs('distributed memory system', project='r')
        idx.upsert(obs_a)

        obs_b = Observation(
            content='replacement for A',
            project='r',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
            visibility='mesh',
        )
        idx.upsert(obs_b)

        # Corrupt: wipe obs_fts and superseded_by to simulate a stale index.
        if idx._fts_cap != _FTS_CAP_LIKE:  # noqa: SLF001
            idx._conn.execute('DELETE FROM obs_fts')  # noqa: SLF001
        idx._conn.execute('UPDATE obs_index SET superseded_by = NULL')  # noqa: SLF001
        idx._conn.commit()  # noqa: SLF001

        # Fake session that returns both obs from "Zenoh".
        # Key format: mem/mesh/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{obs_id}
        class _FakeReply:
            def __init__(self, obs: Observation) -> None:
                af, ci, pi, si, oi = (obs.agent_family, obs.client_id, obs.pc_id, obs.session_id, obs.observation_id)
                key = f'mem/mesh/obs/{af}/{ci}/{pi}/{si}/{oi}'
                self.ok = type(
                    'Ok',
                    (),
                    {
                        'key_expr': key,
                        'payload': type('P', (), {'to_string': lambda self: obs.to_json()})(),
                    },
                )()

        class _FakeSession:
            def get(self, key_expr: str, timeout: float = 30.0) -> list:
                if 'tomb' in key_expr:
                    return []
                return [_FakeReply(obs_a), _FakeReply(obs_b)]

        idx.rebuild_from_zenoh(_FakeSession())

        # FTS search must find obs_b after rebuild.
        if idx._fts_cap != _FTS_CAP_LIKE:  # noqa: SLF001
            fts_results = idx.search(query='replacement', include_superseded=True)
            assert any(r.observation_id == obs_b.observation_id for r in fts_results), (
                'obs_b must be findable via FTS after rebuild'
            )

        # superseded_by must be reconstructed: obs_a hidden by default.
        results_default = idx.search(include_superseded=False)
        default_ids = {r.observation_id for r in results_default}
        assert obs_a.observation_id not in default_ids, 'obs_a must be hidden after rebuild restores superseded_by'
        assert obs_b.observation_id in default_ids, 'obs_b must be visible after rebuild'
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0025: incremental FTS rebuild + drift-detection guard (Issue #224)
# ---------------------------------------------------------------------------


def test_rebuild_incremental_fts_upsert(tmp_path: Path) -> None:
    """New observation appears in Zenoh → FTS row added (incremental upsert)."""
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'incr_upsert.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('incremental fts add', project='incr')
        stats = idx.rebuild_from_zenoh(_FakeSession([obs], []))

        assert stats.added == 1
        (fts_count,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert fts_count == 1, 'FTS must have exactly one row after incremental upsert'
        results = idx.search(query='incremental', project='incr')
        assert any(r.observation_id == obs.observation_id for r in results)
    finally:
        idx.close()


def test_rebuild_incremental_fts_tombstone(tmp_path: Path) -> None:
    """Tombstoned observation is removed from FTS (incremental delete)."""
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'incr_tomb.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('tombstone me', project='incr')
        idx.upsert(obs)
        (before,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert before == 1

        tomb = Tombstone(observation_id=obs.observation_id, deleted_at='2026-06-01T00:00:00.000000Z')
        idx.rebuild_from_zenoh(_FakeSession([obs], [tomb]))

        (after,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert after == 0, 'FTS must be empty after tombstone applied incrementally'
        assert idx.search(project='incr') == []
    finally:
        idx.close()


def test_rebuild_incremental_fts_shadow(tmp_path: Path) -> None:
    """Shadow-marked observation is removed from FTS (incremental delete)."""
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'incr_shadow.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('shadow me from fts', project='incr')
        idx.upsert(obs)
        (before,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert before == 1

        # Rebuild with empty Zenoh → obs not seen → shadow_rows applied
        stats = idx.rebuild_from_zenoh(_FakeSession([], []))

        assert stats.shadowed == 1
        (after,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert after == 0, 'FTS must be empty after shadow applied incrementally'
    finally:
        idx.close()


def test_rebuild_incremental_fts_drift_fallback(tmp_path: Path) -> None:
    """Count mismatch between obs_fts and live obs_index triggers full rebuild."""
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'incr_drift.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('drift guard content', project='incr')
        idx.upsert(obs)

        # Corrupt FTS by deleting all rows.
        idx._conn.execute('DELETE FROM obs_fts')  # noqa: SLF001
        idx._conn.commit()  # noqa: SLF001
        (before,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert before == 0

        # Rebuild with same obs (unchanged → no upsert_rows) → drift detected → full rebuild.
        idx.rebuild_from_zenoh(_FakeSession([obs], []))

        (after,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert after == 1, 'Drift guard must trigger full rebuild and restore FTS'
        results = idx.search(query='drift guard', project='incr')
        assert any(r.observation_id == obs.observation_id for r in results)
    finally:
        idx.close()


def test_rebuild_incremental_fts_unchanged_not_retokenized(tmp_path: Path) -> None:
    """Unchanged observations are not re-inserted into FTS during incremental rebuild."""
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'incr_unchanged.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('unchanged stays put', project='incr')
        idx.upsert(obs)
        (count_before,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert count_before == 1

        # First rebuild: obs unchanged → stats.unchanged == 1.
        stats = idx.rebuild_from_zenoh(_FakeSession([obs], []))
        assert stats.unchanged == 1

        # FTS must still have exactly one row (no duplicate from re-insert).
        (count_after,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert count_after == 1, 'Unchanged obs must not create duplicate FTS rows'

        # Second rebuild: still unchanged → still exactly one FTS row.
        stats2 = idx.rebuild_from_zenoh(_FakeSession([obs], []))
        assert stats2.unchanged == 1
        (count_after2,) = idx._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()  # noqa: SLF001
        assert count_after2 == 1, 'Repeated unchanged rebuild must not accumulate FTS rows'
    finally:
        idx.close()


def test_upsert_repeated_does_not_duplicate_fts_row(tmp_path: Path) -> None:
    """TASK-272: re-upserting the same observation must not grow ``obs_fts``.

    Before the fix, ``INSERT OR REPLACE INTO obs_fts`` never found a
    conflict to replace (FTS5 tables have no UNIQUE/PRIMARY KEY on
    ``observation_id``), so every repeated ``upsert()`` call for an
    already-indexed id (e.g. a self-echo of this peer's own PUT replayed
    back through the replication subscriber) appended a fresh FTS row.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'repeated_upsert.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('repeated upsert must not duplicate fts', project='dedupe')
        idx.upsert(obs)
        idx.upsert(obs)
        idx.upsert(obs)

        (fts_count,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()
        assert fts_count == 1, 'repeated upsert() must leave exactly one obs_fts row per observation_id'
    finally:
        idx.close()


def test_search_dedupes_observation_id_before_limit(tmp_path: Path) -> None:
    """TASK-272: a query-mode search must return each observation_id at most once.

    Simulates a pre-fix database where ``obs_fts`` already accumulated
    duplicate rows for one observation_id (the historical bug), and
    asserts the search-time dedupe absorbs it: the duplicate must not
    appear twice, and must not eat a ``limit`` slot that a distinct
    observation could have used.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415
    from kioku_mesh.local_index import _FTS_UPSERT_SQL  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'dedupe_search.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        dup_obs = _mk_obs('dedupe target keyword', project='dedupe')
        other_obs = _mk_obs('dedupe target keyword also matches', project='dedupe')
        idx.upsert(dup_obs)
        idx.upsert(other_obs)

        # Directly seed extra duplicate obs_fts rows for dup_obs to simulate
        # a database still carrying pre-fix bloat.
        with idx._lock:  # noqa: SLF001
            for _ in range(2):
                idx._conn.execute(  # noqa: SLF001
                    _FTS_UPSERT_SQL,
                    (
                        dup_obs.observation_id,
                        dup_obs.content,
                        dup_obs.subject or '',
                        dup_obs.summary or '',
                        '',
                        'dedupe',
                    ),
                )
            idx._conn.commit()  # noqa: SLF001
        (fts_count,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (dup_obs.observation_id,),
        ).fetchone()
        assert fts_count == 3, 'test setup must seed 3 duplicate obs_fts rows for dup_obs'

        results = idx.search(query='dedupe target keyword', project='dedupe', limit=2)
        result_ids = [r.observation_id for r in results]
        assert result_ids.count(dup_obs.observation_id) == 1, 'duplicate observation_id must appear at most once'
        assert other_obs.observation_id in result_ids, 'dedupe must not waste the limit slot on a duplicate row'
        assert len(results) == 2
    finally:
        idx.close()


def test_search_query_limit_not_capped_by_fixed_overfetch(tmp_path: Path) -> None:
    """PR #270 Codex review (R1): limit must not be silently capped below MAX_SEARCH.

    A prior fix used a fixed ``min(2000, limit * 5)`` over-fetch to dedupe
    in Python, which meant any query-mode search — even with zero
    duplicates — was silently truncated at 2000 rows regardless of the
    caller's requested ``limit``. This inserts more than 2000 unique
    observations and asserts a ``limit=10000`` query-mode search returns
    all of them.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'no_overfetch_cap.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        total = 2001
        for i in range(total):
            idx.upsert(_mk_obs(f'overfetch cap keyword item {i}', project='overfetch'))

        results = idx.search(query='overfetch cap keyword', project='overfetch', limit=10_000)
        assert len(results) == total, f'expected all {total} unique observations, got {len(results)}'
    finally:
        idx.close()


def test_search_dedupe_survives_duplicates_beyond_old_overfetch_factor(tmp_path: Path) -> None:
    """PR #270 Codex re-review: dedupe must not depend on a bounded duplicate count.

    The prior fix's over-fetch factor was a fixed 5x multiple: for
    ``limit=2`` it fetched only the top 10 raw SQL rows before deduping in
    Python. An earlier version of this test seeded 10 duplicate rows for
    one id but did not pin the SQL ordering, so ``other_obs`` could still
    land inside that top-10 window by ordering coincidence (e.g. via the
    ``created_at DESC`` tie-break) and the test passed against the old
    implementation too — it wasn't actually exercising the bug (Codex
    re-review on PR #270).

    Fixed by using ``importance`` as the top-priority sort key: the query
    path ranks ``importance DESC`` before bm25 relevance, so setting
    ``dup_obs`` to importance 5 and ``other_obs`` to importance 1
    deterministically forces every one of ``dup_obs``'s 11 duplicate
    ``obs_fts`` rows ahead of ``other_obs``'s single row in the raw SQL
    order — regardless of bm25 score or created_at. Under the old fixed
    top-10 fetch window, the fetched rows are therefore *guaranteed* to be
    11 copies of ``dup_obs`` and nothing else, so ``other_obs`` cannot
    reach the caller's ``limit`` slot at all: the old implementation must
    fail this test. The current ``ROW_NUMBER()``-based SQL dedupe joins
    the full match set (all 12 rows) before applying ``ORDER BY``/
    ``LIMIT``, so both ids survive regardless of raw-row ordering.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415
    from kioku_mesh.local_index import _FTS_UPSERT_SQL  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'beyond_overfetch_factor.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        dup_obs = Observation(
            content='heavy duplicate keyword target',
            project='heavydup',
            importance=5,
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            visibility='mesh',
        )
        other_obs = Observation(
            content='heavy duplicate keyword other',
            project='heavydup',
            importance=1,
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            visibility='mesh',
        )
        idx.upsert(dup_obs)
        idx.upsert(other_obs)

        with idx._lock:  # noqa: SLF001
            for _ in range(10):
                idx._conn.execute(  # noqa: SLF001
                    _FTS_UPSERT_SQL,
                    (dup_obs.observation_id, dup_obs.content, '', '', '', 'heavydup'),
                )
            idx._conn.commit()  # noqa: SLF001
        (dup_fts_count,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (dup_obs.observation_id,),
        ).fetchone()
        assert dup_fts_count == 11, 'test setup must seed 11 duplicate obs_fts rows for dup_obs (importance 5)'

        results = idx.search(query='heavy duplicate keyword', project='heavydup', limit=2)
        result_ids = [r.observation_id for r in results]
        assert result_ids.count(dup_obs.observation_id) == 1
        assert other_obs.observation_id in result_ids, (
            'a distinct observation must still fill the limit slot despite 11 duplicate, '
            'higher-importance rows for another id occupying the raw SQL ordering ahead of it'
        )
        assert len(results) == 2
    finally:
        idx.close()


def test_get_memory_status_truncated_reflects_actual_truncation(tmp_path: Path) -> None:
    """PR #270 Codex re-review: the browse-mode (no-query) path must also honor ``limit``.

    ``get_memory_status`` computes ``truncated = len(recent) >= MAX_SEARCH``
    from ``search_observations(limit=MAX_SEARCH)`` with no query, which
    hits the non-FTS browse branch. An earlier version of this test only
    requested ``limit=50`` against 75 rows, which the old fixed-multiple
    over-fetch (``min(2000, limit * 5)`` = ``min(2000, 250)`` = 250) could
    still satisfy — so it passed against the old implementation too and
    did not actually exercise the bug (Codex re-review on PR #270).

    The old over-fetch cap saturates at exactly 2000 once
    ``limit * 5 >= 2000`` (i.e. any ``limit >= 400``), independent of how
    many rows actually exist. This requests ``limit=2500`` (using an
    unbounded ``project`` filter so the SQL ``WHERE`` clause matches all
    seeded rows) against 2500 available unique rows: the old
    implementation can only ever return 2000 of them, silently truncating
    below the caller's real ``limit`` and making ``truncated`` look
    unreached even though far more rows exist. The current implementation
    reads ``obs_index`` (``observation_id`` PRIMARY KEY, so no join-
    induced duplicates) directly with ``LIMIT ?`` bound to the real
    ``limit``, and must return the full requested count.
    """
    idx = LocalIndex.connect(str(tmp_path / 'browse_limit_check.db'))
    try:
        total = 2500
        for i in range(total):
            idx.upsert(_mk_obs(f'browse mode row {i}', project='truncbrowse'))

        results = idx.search(project='truncbrowse', limit=2500)
        assert len(results) == total, (
            f'browse-mode search must honor the full requested limit ({total}), got {len(results)}'
        )
    finally:
        idx.close()


def test_upsert_rolls_back_on_fts_insert_failure(tmp_path: Path) -> None:
    """PR #270 Codex review (R2): a failed FTS INSERT must not leave torn state.

    ``upsert()`` runs an ``obs_fts`` DELETE followed by an INSERT inside
    sqlite3's implicit transaction. Before the fix, an exception raised by
    the INSERT (e.g. malformed FTS state) was caught and logged without a
    ``rollback()``, so the preceding DELETE stayed applied-but-uncommitted
    on this connection — same-connection reads would see the row as gone
    even though no new row replaced it, while the previously committed
    row was still intact for any other connection. Injects a failure by
    temporarily pointing the FTS insert statement at a nonexistent table
    and asserts the old row survives untouched afterward.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415
    import kioku_mesh.memory.local_index as local_index_module  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'rollback_injection.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('rollback must preserve prior fts row', project='rollback')
        idx.upsert(obs)
        (before,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()
        assert before == 1

        original_sql = local_index_module._FTS_UPSERT_SQL
        local_index_module._FTS_UPSERT_SQL = 'INSERT INTO obs_fts_nonexistent_table(observation_id) VALUES (?)'
        try:
            idx.upsert(obs)  # DELETE succeeds, INSERT fails -> must rollback
        finally:
            local_index_module._FTS_UPSERT_SQL = original_sql

        (after,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()
        assert after == 1, 'a failed upsert must roll back the preceding obs_fts DELETE, not leave 0 rows'

        (idx_count,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_index WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()
        assert idx_count == 1, 'obs_index upsert must also be rolled back alongside the failed obs_fts insert'

        # A subsequent successful upsert must still work normally (connection not left broken).
        idx.upsert(obs)
        (after2,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()
        assert after2 == 1
    finally:
        idx.close()


def test_rebuild_incremental_fts_net_delete(tmp_path: Path) -> None:
    """Same observation_id in both obs scan and tombstone scan nets to delete in FTS (C1).

    When a fresh reconcile sees an obs and its tombstone simultaneously, the
    incremental path upserts into FTS then deletes (upsert->delete order),
    so the row must not survive in obs_fts.
    """
    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'incr_net_delete.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts unavailable without FTS5')

        obs = _mk_obs('net delete target', project='incr')
        tomb = Tombstone(observation_id=obs.observation_id, deleted_at='2026-06-01T00:00:00.000000Z')
        # Fresh index: obs is new (upsert_rows) and has a tombstone (mark_rows).
        stats = idx.rebuild_from_zenoh(_FakeSession([obs], [tomb]))

        assert stats.added == 1
        assert stats.marked_deleted == 1
        (fts_count,) = idx._conn.execute(  # noqa: SLF001
            'SELECT COUNT(*) FROM obs_fts WHERE observation_id = ?',
            (obs.observation_id,),
        ).fetchone()
        assert fts_count == 0, 'FTS must have zero rows for net-deleted observation'
        assert idx.search(project='incr') == [], 'net-deleted obs must not appear in search'
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# R9: supersedes-aware hide x FTS full rebuild (PR #207 cross-review)
# ---------------------------------------------------------------------------


def _seed_legacy_supersedes_db(db: Path) -> tuple['Observation', 'Observation']:
    """Seed a legacy DB (schema v2, no superseded_by column, no obs_fts) with a supersedes pair.

    Returns (old_obs, new_obs) where new_obs.supersedes=[old_obs.observation_id].
    Used by R9 regression tests to simulate opening a legacy DB where the schema
    migration must backfill superseded_by and then trigger FTS full rebuild.
    """
    old_obs = _mk_obs('rebuild fts superseded content', project='r9')
    new_obs = Observation(
        content='rebuild fts superseder content',
        project='r9',
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
        supersedes=[old_obs.observation_id],
    )
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
        raw.executemany(
            'INSERT INTO obs_index '
            '(observation_id, project, created_at, memory_type, importance, subject, summary, payload_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (
                    o.observation_id,
                    o.project,
                    o.created_at,
                    o.memory_type,
                    o.importance,
                    o.subject,
                    o.summary,
                    o.to_json(),
                )
                for o in (old_obs, new_obs)
            ],
        )
        raw.commit()
    return old_obs, new_obs


def test_supersedes_aware_hide_after_fts_full_rebuild(tmp_path: Path) -> None:
    """(g) supersedes-aware 隠蔽が FTS full rebuild 直後でも効く (R9).

    Legacy DB (no obs_fts, no superseded_by column) is opened by
    LocalIndex.connect(); the migration backfills superseded_by from
    payload_json and rebuilds obs_fts. Even though both rows land in
    obs_fts, the existence-based superseded_by JOIN must hide old_obs
    from default search. Both FTS5 and LIKE paths apply the JOIN filter
    so this assertion holds regardless of _fts_cap.
    """
    db = tmp_path / 'r9_hide.db'
    old_obs, new_obs = _seed_legacy_supersedes_db(db)

    idx = LocalIndex.connect(str(db))
    try:
        results = idx.search(query='rebuild fts', include_superseded=False)
        result_ids = {r.observation_id for r in results}
        assert new_obs.observation_id in result_ids, (
            f'superseder new_obs must be visible after FTS rebuild (fts_cap={idx._fts_cap!r})'  # noqa: SLF001
        )
        assert old_obs.observation_id not in result_ids, (
            f'superseded old_obs must be hidden after FTS rebuild (fts_cap={idx._fts_cap!r})'  # noqa: SLF001
        )
    finally:
        idx.close()


def test_supersedes_aware_include_superseded_after_fts_full_rebuild(tmp_path: Path) -> None:
    """(h) include_superseded=True returns both rows after FTS full rebuild (R9).

    Boundary complement of test_supersedes_aware_hide_after_fts_full_rebuild:
    with include_superseded=True both old_obs and new_obs must appear even
    immediately after FTS full rebuild from a legacy DB.
    """
    db = tmp_path / 'r9_include.db'
    old_obs, new_obs = _seed_legacy_supersedes_db(db)

    idx = LocalIndex.connect(str(db))
    try:
        results = idx.search(query='rebuild fts', include_superseded=True)
        result_ids = {r.observation_id for r in results}
        assert new_obs.observation_id in result_ids, (
            f'superseder new_obs must appear with include_superseded=True (fts_cap={idx._fts_cap!r})'  # noqa: SLF001
        )
        assert old_obs.observation_id in result_ids, (
            f'superseded old_obs must appear with include_superseded=True (fts_cap={idx._fts_cap!r})'  # noqa: SLF001
        )
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# C1: FTS tags edge cases (PR #207/#208 follow-up)
# ---------------------------------------------------------------------------


def test_rebuild_fts_tags_edge_cases(tmp_path: Path) -> None:
    """C1: rebuild and lockstep upsert agree on obs_fts.tags for three edge cases.

    - tags=[] (empty array) → obs_fts.tags == ''
    - tags=['zenoh'] (single element) → obs_fts.tags == 'zenoh'
    - $.tags key absent in payload_json (legacy row) → obs_fts.tags == ''
    """
    import json  # noqa: PLC0415

    from kioku_mesh.local_index import _FTS_CAP_LIKE  # noqa: PLC0415
    from kioku_mesh.local_index import _FTS_UPSERT_SQL  # noqa: PLC0415
    from kioku_mesh.local_index import _rebuild_fts_from_obs_index  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'fts_tags_edge.db'))
    try:
        if idx._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
            pytest.skip('obs_fts is unavailable without FTS5 support')

        # Case 1: tags=[]
        obs_empty = _mk_obs('empty tags case', project='edge', tags=[])
        idx.upsert(obs_empty)
        (lockstep_empty,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs_empty.observation_id,),
        ).fetchone()

        # Case 2: tags=['zenoh']
        obs_single = _mk_obs('single tag case', project='edge', tags=['zenoh'])
        idx.upsert(obs_single)
        (lockstep_single,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs_single.observation_id,),
        ).fetchone()

        # Case 3: $.tags key absent in payload_json (legacy row).
        # Simulate lockstep behavior: lockstep produces '' for missing/empty tags.
        obs_notags = _mk_obs('no tags key case', project='edge', tags=[])
        payload_no_key = json.loads(obs_notags.to_json())
        payload_no_key.pop('tags', None)
        idx._conn.execute(  # noqa: SLF001
            'INSERT INTO obs_index '
            '(observation_id, project, created_at, memory_type, importance, subject, summary, payload_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                obs_notags.observation_id,
                obs_notags.project,
                obs_notags.created_at,
                obs_notags.memory_type,
                obs_notags.importance,
                obs_notags.subject,
                obs_notags.summary,
                json.dumps(payload_no_key),
            ),
        )
        idx._conn.execute(  # noqa: SLF001
            _FTS_UPSERT_SQL,
            (
                obs_notags.observation_id,
                obs_notags.content,
                obs_notags.subject or '',
                obs_notags.summary or '',
                '',  # lockstep produces '' for obs.tags == []
                obs_notags.project or '',
            ),
        )
        idx._conn.commit()  # noqa: SLF001
        (lockstep_notags,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs_notags.observation_id,),
        ).fetchone()

        # Delete all obs_fts rows to force a full rebuild (bypass R1 skip-guard).
        idx._conn.execute('DELETE FROM obs_fts')  # noqa: SLF001
        idx._conn.commit()  # noqa: SLF001

        _rebuild_fts_from_obs_index(idx._conn)  # noqa: SLF001

        (rebuilt_empty,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs_empty.observation_id,),
        ).fetchone()
        (rebuilt_single,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs_single.observation_id,),
        ).fetchone()
        (rebuilt_notags,) = idx._conn.execute(  # noqa: SLF001
            'SELECT tags FROM obs_fts WHERE observation_id = ?',
            (obs_notags.observation_id,),
        ).fetchone()

        assert lockstep_empty == ''
        assert rebuilt_empty == lockstep_empty

        assert lockstep_single == 'zenoh'
        assert rebuilt_single == lockstep_single

        assert lockstep_notags == ''
        assert rebuilt_notags == lockstep_notags

    finally:
        idx.close()


# ---------------------------------------------------------------------------
# R2 / R6: LIKE wildcard escape and query edge cases (PR #211 follow-up)
# ---------------------------------------------------------------------------


def test_search_like_escape_wildcards(tmp_path: Path) -> None:
    """R2: % and _ in short query terms are treated as literal chars via LIKE ESCAPE.

    Short terms (< 3 chars) always route through LIKE regardless of FTS
    capability, so using 2-char terms makes the escape behaviour deterministic.
    Without the ESCAPE clause, '_b' would match any single char + b (over-match).
    """
    idx = LocalIndex.connect(str(tmp_path / 'like_escape.db'))
    try:
        obs_underscore = _mk_obs('key_bar underscore match', project='esc')
        obs_no_underscore = _mk_obs('keyXbar no underscore here', project='esc')
        obs_percent = _mk_obs('rate a% percent match', project='esc')
        obs_no_percent = _mk_obs('rate ax no percent here', project='esc')
        idx.upsert(obs_underscore)
        idx.upsert(obs_no_underscore)
        idx.upsert(obs_percent)
        idx.upsert(obs_no_percent)

        # '_b' is 2 chars → always LIKE; must match literal underscore, not any-char + b.
        us_ids = {r.observation_id for r in idx.search(query='_b', project='esc', include_superseded=True)}
        assert obs_underscore.observation_id in us_ids, '_b must match literal underscore'
        assert obs_no_underscore.observation_id not in us_ids, '_b must not over-match Xb'

        # 'a%' is 2 chars → always LIKE; must match literal percent, not a + wildcard.
        pct_ids = {r.observation_id for r in idx.search(query='a%', project='esc', include_superseded=True)}
        assert obs_percent.observation_id in pct_ids, 'a% must match literal percent'
        assert obs_no_percent.observation_id not in pct_ids, 'a% must not over-match ax'
    finally:
        idx.close()


def test_search_all_short_terms_uses_like_and(tmp_path: Path) -> None:
    """R6: All terms shorter than the trigram threshold use LIKE AND semantics.

    When every term in the query is < 3 chars, no FTS term is produced and
    the query degrades to multiple LIKE conditions ANDed together.

    Uses non-hex terms ('qx', 'zy') to avoid false positive matches against
    the hex observation_id stored in payload_json.
    """
    idx = LocalIndex.connect(str(tmp_path / 'short_and.db'))
    try:
        # 'qx' and 'zy' contain only non-hex chars (q, x, y, z are outside [0-9a-f]),
        # preventing accidental matches against observation_id UUID strings.
        wanted = _mk_obs('qx and zy pair both present', project='shortand')
        has_qx_only = _mk_obs('qx only without the second token', project='shortand')
        has_zy_only = _mk_obs('zy only without the first token', project='shortand')
        idx.upsert(wanted)
        idx.upsert(has_qx_only)
        idx.upsert(has_zy_only)

        # 'qx' and 'zy' are both 2 chars → LIKE AND path regardless of FTS capability.
        results = idx.search(query='qx zy', project='shortand', include_superseded=True)
        ids = {r.observation_id for r in results}
        assert wanted.observation_id in ids, 'obs containing both qx and zy must match'
        assert has_qx_only.observation_id not in ids, 'obs with only qx must not match'
        assert has_zy_only.observation_id not in ids, 'obs with only zy must not match'
    finally:
        idx.close()


def test_search_term_with_double_quote_escape(tmp_path: Path) -> None:
    """R6: A query term containing a double-quote is sanitised by _quote_fts_term.

    _quote_fts_term replaces " with "" inside the FTS5 phrase literal so the
    query is accepted without raising a syntax error.
    """
    idx = LocalIndex.connect(str(tmp_path / 'dquote.db'))
    try:
        obs = _mk_obs('say hello greeting phrase', project='dquote')
        idx.upsert(obs)

        # 'say "hi"' contains an embedded double-quote; must not raise.
        results = idx.search(query='say "hi"', project='dquote', include_superseded=True)
        assert isinstance(results, list), 'search with embedded " must return a list without error'
    finally:
        idx.close()


def test_search_whitespace_only_returns_recency(tmp_path: Path) -> None:
    """R6: A whitespace-only query produces no terms and returns all live rows.

    str.split() on whitespace-only input yields an empty list, so no LIKE
    or FTS filter is appended and the query falls through to recency order.
    """
    idx = LocalIndex.connect(str(tmp_path / 'whitespace.db'))
    try:
        obs1 = _mk_obs('alpha observation', project='ws')
        obs2 = _mk_obs('beta observation', project='ws')
        idx.upsert(obs1)
        idx.upsert(obs2)

        results = idx.search(query='   ', project='ws', include_superseded=True)
        ids = {r.observation_id for r in results}
        assert obs1.observation_id in ids, 'whitespace-only query must return all live rows'
        assert obs2.observation_id in ids
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Issue #218: search_mode tests
# ---------------------------------------------------------------------------


def test_search_mode_backward_compat(tmp_path: Path) -> None:
    """search_mode unspecified and search_mode='and' return identical results."""
    from kioku_mesh.local_index import SEARCH_MODES  # noqa: PLC0415

    assert 'and' in SEARCH_MODES
    assert 'or' in SEARCH_MODES
    assert 'and_or' in SEARCH_MODES

    idx = LocalIndex.connect(str(tmp_path / 'compat.db'))
    try:
        both = _mk_obs('alpha beta content', project='compat')
        alpha_only = _mk_obs('alpha content only', project='compat')
        idx.upsert(both)
        idx.upsert(alpha_only)

        default_res = idx.search(query='alpha beta', project='compat', include_superseded=True)
        and_res = idx.search(query='alpha beta', project='compat', include_superseded=True, search_mode='and')
        assert [r.observation_id for r in default_res] == [r.observation_id for r in and_res]
    finally:
        idx.close()


def test_search_mode_or_recall(tmp_path: Path) -> None:
    """'or' mode returns rows containing alpha only, beta only, and both; unrelated excluded."""
    idx = LocalIndex.connect(str(tmp_path / 'or_recall.db'))
    try:
        both = _mk_obs('alpha beta present', project='orre')
        alpha_only = _mk_obs('alpha without the other word', project='orre')
        beta_only = _mk_obs('beta without the other word', project='orre')
        unrelated = _mk_obs('completely unrelated content', project='orre')
        for obs in (both, alpha_only, beta_only, unrelated):
            idx.upsert(obs)

        results = idx.search(query='alpha beta', project='orre', include_superseded=True, search_mode='or')
        ids = {r.observation_id for r in results}
        assert both.observation_id in ids
        assert alpha_only.observation_id in ids
        assert beta_only.observation_id in ids
        assert unrelated.observation_id not in ids
    finally:
        idx.close()


def test_search_mode_and_excludes_partial(tmp_path: Path) -> None:
    """'and' mode (default) excludes rows that only have one of the query terms."""
    idx = LocalIndex.connect(str(tmp_path / 'and_excl.db'))
    try:
        both = _mk_obs('alpha beta present', project='andex')
        alpha_only = _mk_obs('alpha without the other word', project='andex')
        for obs in (both, alpha_only):
            idx.upsert(obs)

        results = idx.search(query='alpha beta', project='andex', include_superseded=True, search_mode='and')
        ids = {r.observation_id for r in results}
        assert both.observation_id in ids
        assert alpha_only.observation_id not in ids
    finally:
        idx.close()


def test_search_mode_and_or_ordering_and_dedupe(tmp_path: Path) -> None:
    """'and_or' places AND hits before OR-only hits; no duplicate observation_ids; result <= limit."""
    idx = LocalIndex.connect(str(tmp_path / 'andor.db'))
    try:
        both = _mk_obs('alpha beta present', project='andor')
        alpha_only = _mk_obs('alpha without the other word', project='andor')
        beta_only = _mk_obs('beta without the other word', project='andor')
        for obs in (both, alpha_only, beta_only):
            idx.upsert(obs)

        results = idx.search(
            query='alpha beta', project='andor', include_superseded=True, search_mode='and_or', limit=10
        )
        ids = [r.observation_id for r in results]
        assert ids == list(dict.fromkeys(ids)), 'no duplicate observation_ids'
        assert len(ids) <= 10
        # AND hit (both) must come before OR-only hits
        if both.observation_id in ids and alpha_only.observation_id in ids:
            assert ids.index(both.observation_id) < ids.index(alpha_only.observation_id)
        if both.observation_id in ids and beta_only.observation_id in ids:
            assert ids.index(both.observation_id) < ids.index(beta_only.observation_id)
    finally:
        idx.close()


def test_search_mode_and_or_fill_from_or(tmp_path: Path) -> None:
    """'and_or' fills missing results from OR phase when AND returns fewer than limit."""
    idx = LocalIndex.connect(str(tmp_path / 'andor_fill.db'))
    try:
        alpha_only = _mk_obs('alpha without other', project='fill')
        beta_only = _mk_obs('beta without other', project='fill')
        for obs in (alpha_only, beta_only):
            idx.upsert(obs)

        # AND phase yields 0 hits; OR phase should fill both
        results = idx.search(query='alpha beta', project='fill', include_superseded=True, search_mode='and_or')
        ids = {r.observation_id for r in results}
        assert alpha_only.observation_id in ids
        assert beta_only.observation_id in ids
    finally:
        idx.close()


def test_search_mode_or_short_like_terms(tmp_path: Path) -> None:
    """All short terms (< 3 chars): 'or' uses LIKE OR, escaping %, _, backslash."""
    idx = LocalIndex.connect(str(tmp_path / 'short_or.db'))
    try:
        has_qx = _mk_obs('content with qx marker', project='short')
        has_zy = _mk_obs('content with zy marker', project='short')
        has_both = _mk_obs('content with qx and zy markers', project='short')
        unrelated = _mk_obs('no markers here', project='short')
        for obs in (has_qx, has_zy, has_both, unrelated):
            idx.upsert(obs)

        results = idx.search(query='qx zy', project='short', include_superseded=True, search_mode='or')
        ids = {r.observation_id for r in results}
        assert has_qx.observation_id in ids
        assert has_zy.observation_id in ids
        assert has_both.observation_id in ids
        assert unrelated.observation_id not in ids
    finally:
        idx.close()


def test_search_mode_or_mixed_fts_and_like(tmp_path: Path) -> None:
    """Mixed long+short terms: 'or' returns rows matching long-only, short-only, or both."""
    idx = LocalIndex.connect(str(tmp_path / 'mixed_or.db'))
    try:
        long_only = _mk_obs('migration happened here', project='mixed')
        short_only = _mk_obs('has qx short marker', project='mixed')
        both_terms = _mk_obs('migration and qx together', project='mixed')
        neither = _mk_obs('unrelated content entirely', project='mixed')
        for obs in (long_only, short_only, both_terms, neither):
            idx.upsert(obs)

        or_results = idx.search(query='migration qx', project='mixed', include_superseded=True, search_mode='or')
        or_ids = {r.observation_id for r in or_results}
        assert long_only.observation_id in or_ids
        assert short_only.observation_id in or_ids
        assert both_terms.observation_id in or_ids
        assert neither.observation_id not in or_ids

        and_results = idx.search(query='migration qx', project='mixed', include_superseded=True, search_mode='and')
        and_ids = {r.observation_id for r in and_results}
        assert both_terms.observation_id in and_ids
        assert long_only.observation_id not in and_ids
        assert short_only.observation_id not in and_ids
    finally:
        idx.close()


def test_search_mode_base_filter_superseded_not_leaked(tmp_path: Path) -> None:
    """include_superseded=False hides superseded rows in 'or' and 'and_or' modes."""
    idx = LocalIndex.connect(str(tmp_path / 'superseded_or.db'))
    try:
        old = _mk_obs('alpha beta old observation', project='sup')
        new = _mk_obs('alpha beta new observation supersedes old', project='sup')
        new.supersedes = [old.observation_id]
        idx.upsert(old)
        idx.upsert(new)

        for mode in ('or', 'and_or'):
            results = idx.search(query='alpha', project='sup', include_superseded=False, search_mode=mode)
            ids = {r.observation_id for r in results}
            assert new.observation_id in ids, f'{mode}: live superseder must appear'
            assert old.observation_id not in ids, f'{mode}: superseded row must be hidden'
    finally:
        idx.close()


def test_search_mode_base_filter_project_not_leaked(tmp_path: Path) -> None:
    """Project filter remains AND in 'or' mode — rows from other projects are excluded."""
    idx = LocalIndex.connect(str(tmp_path / 'proj_or.db'))
    try:
        in_proj = _mk_obs('alpha content', project='target')
        other_proj = _mk_obs('alpha content', project='other')
        idx.upsert(in_proj)
        idx.upsert(other_proj)

        results = idx.search(query='alpha', project='target', include_superseded=True, search_mode='or')
        ids = {r.observation_id for r in results}
        assert in_proj.observation_id in ids
        assert other_proj.observation_id not in ids
    finally:
        idx.close()


def test_search_mode_unknown_raises(tmp_path: Path) -> None:
    """Unknown search_mode raises ValueError."""
    idx = LocalIndex.connect(str(tmp_path / 'unknown_mode.db'))
    try:
        with pytest.raises(ValueError, match='search_mode'):
            idx.search(query='alpha', search_mode='fuzzy')
    finally:
        idx.close()


def test_search_mode_and_or_limit_respected(tmp_path: Path) -> None:
    """'and_or' final result length never exceeds limit."""
    idx = LocalIndex.connect(str(tmp_path / 'andor_limit.db'))
    try:
        for i in range(20):
            idx.upsert(_mk_obs(f'alpha content number {i}', project='lim'))
        for i in range(20):
            idx.upsert(_mk_obs(f'beta content number {i}', project='lim'))

        limit = 5
        results = idx.search(
            query='alpha beta', project='lim', include_superseded=True, search_mode='and_or', limit=limit
        )
        assert len(results) <= limit
    finally:
        idx.close()


def test_search_mode_or_multiword_no_and_hit(tmp_path: Path) -> None:
    """'or' recovers hits that 'and' misses when no row contains all terms."""
    idx = LocalIndex.connect(str(tmp_path / 'or_recall2.db'))
    try:
        has_foo = _mk_obs('foo is here', project='recall2')
        has_bar = _mk_obs('bar is here', project='recall2')
        idx.upsert(has_foo)
        idx.upsert(has_bar)

        and_results = idx.search(query='foo bar', project='recall2', include_superseded=True, search_mode='and')
        assert len(and_results) == 0, 'and mode must return 0 when no row has both terms'

        or_results = idx.search(query='foo bar', project='recall2', include_superseded=True, search_mode='or')
        or_ids = {r.observation_id for r in or_results}
        assert has_foo.observation_id in or_ids
        assert has_bar.observation_id in or_ids
    finally:
        idx.close()


def test_search_mode_or_whitespace_query_is_recency(tmp_path: Path) -> None:
    """'or' mode with whitespace-only query still returns recency results."""
    idx = LocalIndex.connect(str(tmp_path / 'or_ws.db'))
    try:
        obs1 = _mk_obs('alpha obs', project='orws')
        obs2 = _mk_obs('beta obs', project='orws')
        idx.upsert(obs1)
        idx.upsert(obs2)

        results = idx.search(query='   ', project='orws', include_superseded=True, search_mode='or')
        ids = {r.observation_id for r in results}
        assert obs1.observation_id in ids
        assert obs2.observation_id in ids
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0028 Phase 1 invariant tests
# ---------------------------------------------------------------------------


def test_rebuild_preserves_tombstone_state(tmp_path: Path) -> None:
    """[INV-2] rebuild_from_zenoh reconstructs tombstone state from Raw payloads.

    obs + tombstone are fed to _FakeSession; after rebuild from empty index,
    search must not return the tombstoned obs and find_by_id with
    include_deleted must locate it with deleted_at set.
    """
    idx = LocalIndex.connect(str(tmp_path / 'inv2_tomb.db'))
    try:
        obs = _mk_obs('will be tombstoned', project='inv2')
        tomb = Tombstone(observation_id=obs.observation_id, deleted_at='2026-06-01T00:00:00.000000Z')
        session = _FakeSession([obs], [tomb])

        stats = idx.rebuild_from_zenoh(session)

        assert stats.marked_deleted >= 1
        # Tombstoned row must not appear in live search.
        live_ids = {r.observation_id for r in idx.search(project='inv2')}
        assert obs.observation_id not in live_ids, 'tombstoned obs must be hidden from search'
        # find_by_id with include_deleted must return the row (tombstone not physically deleted).
        found = idx.find_by_id(obs.observation_id, include_deleted=True)
        assert found is not None
    finally:
        idx.close()


def test_rebuild_preserves_shadow_state(tmp_path: Path) -> None:
    """[INV-2/INV-3] rebuild marks obs absent from Zenoh as shadowed; FTS reflects only live rows.

    An obs present in the index but absent from the session's replies is
    shadowed by rebuild. After rebuild, search must hide it (INV-2 derived
    view matches live state) and the FTS table must not include it (INV-3
    FTS derived only from live rows).
    """
    idx = LocalIndex.connect(str(tmp_path / 'inv2_shadow.db'))
    try:
        live_obs = _mk_obs('live after rebuild', project='inv23')
        shadow_obs = _mk_obs('absent from zenoh', project='inv23')
        idx.upsert(live_obs)
        idx.upsert(shadow_obs)

        # Session only returns live_obs; shadow_obs will be rebuild-shadowed.
        stats = idx.rebuild_from_zenoh(_FakeSession([live_obs], []))

        assert stats.shadowed >= 1
        # search must not include the shadowed obs.
        live_ids = {r.observation_id for r in idx.search(project='inv23')}
        assert live_obs.observation_id in live_ids
        assert shadow_obs.observation_id not in live_ids, 'shadowed obs must be hidden from search'
        # FTS must not include the shadowed obs (INV-3).
        fts_results = idx.search(query='absent', project='inv23')
        fts_ids = {r.observation_id for r in fts_results}
        assert shadow_obs.observation_id not in fts_ids, 'shadowed obs must not appear in FTS results'
        # AC-4/INV-3: FTS must be rebuilt from live raw observations (positive assertion).
        # Searching for a token from live_obs's content must return live_obs — empty
        # obs_fts would make this fail, proving FTS is not merely empty.
        live_fts_results = idx.search(query='live', project='inv23')
        live_fts_ids = {r.observation_id for r in live_fts_results}
        assert live_obs.observation_id in live_fts_ids, (
            'rebuild_from_zenoh must reindex live raw observation in obs_fts'
        )
    finally:
        idx.close()


def test_rebuild_preserves_superseded_by(tmp_path: Path) -> None:
    """[INV-2/INV-4] rebuild reconstructs superseded_by from obs payloads.

    obs_B supersedes obs_A. After rebuilding an empty index from a session
    containing both, find_by_id on obs_A must return superseded_by set to
    obs_B.observation_id (INV-4: supersede is a distinct state with its own
    read semantics, and INV-2: derived view is reconstructable from raw obs).
    """
    idx = LocalIndex.connect(str(tmp_path / 'inv24_supersede.db'))
    try:
        obs_a = _mk_obs('original decision', project='inv24')
        obs_b = Observation(
            content='updated decision',
            project='inv24',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
            visibility='mesh',
        )
        # Rebuild from session containing both obs.
        idx.rebuild_from_zenoh(_FakeSession([obs_a, obs_b], []))

        found_a = idx.find_by_id(obs_a.observation_id, include_deleted=True)
        assert found_a is not None
        assert hasattr(found_a, '_extras')
        assert found_a._extras.get('superseded_by') == obs_b.observation_id, (  # noqa: SLF001
            f'superseded_by must be restored after rebuild; got extras={found_a._extras!r}'  # noqa: SLF001
        )
    finally:
        idx.close()


def test_gc_tombstones_does_not_evict_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[INV-5] gc_tombstones must not delete live observations regardless of age or importance.

    Seeds an old low-importance live obs (no deleted_at). Runs gc_tombstones
    with retention_days=0 (aggressive cutoff). The live obs must still be
    searchable afterwards — GC may only remove tombstoned rows.
    """
    from kioku_mesh.backend import LocalBackend  # noqa: PLC0415
    from kioku_mesh.backend import reset_backend  # noqa: PLC0415

    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    monkeypatch.setenv('KIOKU_MESH_STATE_DIR', str(tmp_path / 'state'))
    reset_backend()

    from kioku_mesh.memory.local_raw_store import LocalRawStore  # noqa: PLC0415

    backend = LocalBackend.__new__(LocalBackend)
    idx = LocalIndex.connect(str(tmp_path / 'inv5_tomb.db'))
    backend._idx = idx  # type: ignore[attr-defined]
    backend._raw_store = LocalRawStore(tmp_path / 'inv5_raw.db')  # type: ignore[attr-defined]

    live = Observation(
        content='ancient live obs',
        project='inv5',
        importance=1,
        created_at='2020-01-01T00:00:00.000000Z',
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
    )
    idx.upsert(live)

    backend.gc_tombstones(retention_days=0, project='inv5')

    ids = {r.observation_id for r in idx.search(project='inv5')}
    assert live.observation_id in ids, 'live obs must survive gc_tombstones regardless of age/importance'


def test_gc_shadows_does_not_evict_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[INV-5] gc_shadows must not delete live observations.

    Seeds a live obs with no shadowed_at. Runs gc_shadows with
    retention_days=0. The live obs must still be searchable afterwards.
    """
    from kioku_mesh.backend import LocalBackend  # noqa: PLC0415
    from kioku_mesh.backend import reset_backend  # noqa: PLC0415

    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    monkeypatch.setenv('KIOKU_MESH_STATE_DIR', str(tmp_path / 'state'))
    reset_backend()

    from kioku_mesh.memory.local_raw_store import LocalRawStore  # noqa: PLC0415

    backend = LocalBackend.__new__(LocalBackend)
    idx = LocalIndex.connect(str(tmp_path / 'inv5_shadow.db'))
    backend._idx = idx  # type: ignore[attr-defined]
    backend._raw_store = LocalRawStore(tmp_path / 'inv5_shadow_raw.db')  # type: ignore[attr-defined]

    live = Observation(
        content='live obs not shadowed',
        project='inv5s',
        importance=1,
        created_at='2020-01-01T00:00:00.000000Z',
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
    )
    idx.upsert(live)

    backend.gc_shadows(retention_days=0, project='inv5s')

    ids = {r.observation_id for r in idx.search(project='inv5s')}
    assert live.observation_id in ids, 'live obs must survive gc_shadows regardless of age/importance'


def test_list_shadowed_obs(tmp_path: Path) -> None:
    """list_shadowed_obs returns only shadowed rows, excluding live and tombstoned."""
    idx = LocalIndex.connect(str(tmp_path / 'list_shadow.db'))
    try:
        live = _mk_obs('live obs', project='ls')
        shadowed = _mk_obs('shadowed obs', project='ls')
        tombstoned = _mk_obs('tombstoned obs', project='ls')

        idx.upsert(live)
        idx.upsert(shadowed)
        idx.upsert(tombstoned)

        idx.mark_shadowed_missing(shadowed.observation_id, '2026-06-01T00:00:00.000000Z')
        idx.mark_deleted(tombstoned.observation_id, '2026-06-01T00:00:00.000000Z')

        rows = idx.list_shadowed_obs()

        shadow_ids = {row[0] for row in rows}
        assert shadowed.observation_id in shadow_ids, 'shadowed obs must appear in list_shadowed_obs'
        assert live.observation_id not in shadow_ids, 'live obs must not appear in list_shadowed_obs'
        assert tombstoned.observation_id not in shadow_ids, 'tombstoned obs must not appear in list_shadowed_obs'

        # Each row tuple must be (obs_id, project, created_at, shadowed_at, summary).
        for row in rows:
            assert len(row) == 5
            assert row[1] == 'ls'  # project
            assert row[3] != ''  # shadowed_at is non-empty
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0028 Phase3: inspect_by_id state inspection tests
# ---------------------------------------------------------------------------


def test_inspect_live(tmp_path: Path) -> None:
    """inspect_by_id returns state='live' for a normal observation."""
    idx = LocalIndex.connect(str(tmp_path / 'inspect_live.db'))
    try:
        obs = _mk_obs('live observation', project='p')
        idx.upsert(obs)
        result = idx.inspect_by_id(obs.observation_id)
        assert result is not None
        assert result['state'] == 'live'
        assert result['id'] == obs.observation_id
        assert result['deleted_at'] is None
        assert result['shadowed_at'] is None
        assert result['superseded_by'] is None
        assert result['created_at'] == obs.created_at
        assert result['payload'] is not None
        assert result['updated_at'] is None
    finally:
        idx.close()


def test_inspect_tombstoned(tmp_path: Path) -> None:
    """inspect_by_id returns state='tombstoned' after mark_deleted."""
    idx = LocalIndex.connect(str(tmp_path / 'inspect_tomb.db'))
    try:
        obs = _mk_obs('tombstoned observation', project='p')
        idx.upsert(obs)
        deleted_at = '2026-06-27T00:00:00.000000Z'
        idx.mark_deleted(obs.observation_id, deleted_at)
        result = idx.inspect_by_id(obs.observation_id)
        assert result is not None
        assert result['state'] == 'tombstoned'
        assert result['deleted_at'] == deleted_at
    finally:
        idx.close()


def test_inspect_shadowed(tmp_path: Path) -> None:
    """inspect_by_id returns state='shadowed' after mark_shadowed_missing."""
    idx = LocalIndex.connect(str(tmp_path / 'inspect_shadow.db'))
    try:
        obs = _mk_obs('shadowed observation', project='p')
        idx.upsert(obs)
        shadowed_at = '2026-06-27T00:00:00.000000Z'
        idx.mark_shadowed_missing(obs.observation_id, shadowed_at)
        result = idx.inspect_by_id(obs.observation_id)
        assert result is not None
        assert result['state'] == 'shadowed'
        assert result['shadowed_at'] == shadowed_at
        assert result['deleted_at'] is None
    finally:
        idx.close()


def test_inspect_superseded(tmp_path: Path) -> None:
    """inspect_by_id returns state='superseded' when superseded_by is set."""
    idx = LocalIndex.connect(str(tmp_path / 'inspect_super.db'))
    try:
        obs_a = _mk_obs('old observation', project='p')
        idx.upsert(obs_a)
        obs_b = Observation(
            content='new observation superseding A',
            project='p',
            agent_family='claude',
            client_id='test',
            pc_id='testpc',
            session_id='testsession',
            supersedes=[obs_a.observation_id],
        )
        idx.upsert(obs_b)
        result = idx.inspect_by_id(obs_a.observation_id)
        assert result is not None
        assert result['state'] == 'superseded'
        assert result['superseded_by'] == obs_b.observation_id
    finally:
        idx.close()


def test_inspect_physical_missing(tmp_path: Path) -> None:
    """inspect_by_id returns None for an observation_id not in the index."""
    idx = LocalIndex.connect(str(tmp_path / 'inspect_miss.db'))
    try:
        result = idx.inspect_by_id('00000000000000000000000000000000')
        assert result is None
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0028 Phase3 NB1: inspect_by_id state precedence tests (INV-4)
# ---------------------------------------------------------------------------


def test_inspect_precedence_deleted_over_shadowed(tmp_path: Path) -> None:
    """deleted_at takes priority over shadowed_at: state must be 'tombstoned'."""
    idx = LocalIndex.connect(str(tmp_path / 'prec_del_shd.db'))
    try:
        obs = _mk_obs('prec del shd', project='p')
        idx.upsert(obs)
        deleted_at = '2026-06-27T00:00:00.000000Z'
        shadowed_at = '2026-06-26T00:00:00.000000Z'
        # Set both flags directly — mark_deleted clears shadowed_at normally.
        idx._conn.execute(  # noqa: SLF001
            'UPDATE obs_index SET deleted_at = ?, shadowed_at = ? WHERE observation_id = ?',
            (deleted_at, shadowed_at, obs.observation_id),
        )
        idx._conn.commit()  # noqa: SLF001
        result = idx.inspect_by_id(obs.observation_id)
        assert result is not None
        assert result['state'] == 'tombstoned', f'deleted_at must beat shadowed_at, got {result["state"]}'
        assert result['deleted_at'] == deleted_at
        assert result['shadowed_at'] == shadowed_at
    finally:
        idx.close()


def test_inspect_precedence_deleted_over_superseded(tmp_path: Path) -> None:
    """deleted_at takes priority over superseded_by: state must be 'tombstoned'."""
    idx = LocalIndex.connect(str(tmp_path / 'prec_del_sup.db'))
    try:
        obs_a = _mk_obs('prec del sup old', project='p')
        obs_b = _mk_obs('prec del sup new', project='p')
        idx.upsert(obs_a)
        idx.upsert(obs_b)
        deleted_at = '2026-06-27T00:00:00.000000Z'
        # Set both deleted_at and superseded_by on obs_a simultaneously.
        idx._conn.execute(  # noqa: SLF001
            'UPDATE obs_index SET deleted_at = ?, superseded_by = ? WHERE observation_id = ?',
            (deleted_at, obs_b.observation_id, obs_a.observation_id),
        )
        idx._conn.commit()  # noqa: SLF001
        result = idx.inspect_by_id(obs_a.observation_id)
        assert result is not None
        assert result['state'] == 'tombstoned', f'deleted_at must beat superseded_by, got {result["state"]}'
        assert result['deleted_at'] == deleted_at
        assert result['superseded_by'] == obs_b.observation_id
    finally:
        idx.close()


def test_inspect_precedence_shadowed_over_superseded(tmp_path: Path) -> None:
    """shadowed_at takes priority over superseded_by: state must be 'shadowed'."""
    idx = LocalIndex.connect(str(tmp_path / 'prec_shd_sup.db'))
    try:
        obs_a = _mk_obs('prec shd sup old', project='p')
        obs_b = _mk_obs('prec shd sup new', project='p')
        idx.upsert(obs_a)
        idx.upsert(obs_b)
        shadowed_at = '2026-06-27T00:00:00.000000Z'
        # Set both shadowed_at and superseded_by on obs_a simultaneously.
        idx._conn.execute(  # noqa: SLF001
            'UPDATE obs_index SET shadowed_at = ?, superseded_by = ? WHERE observation_id = ?',
            (shadowed_at, obs_b.observation_id, obs_a.observation_id),
        )
        idx._conn.commit()  # noqa: SLF001
        result = idx.inspect_by_id(obs_a.observation_id)
        assert result is not None
        assert result['state'] == 'shadowed', f'shadowed_at must beat superseded_by, got {result["state"]}'
        assert result['shadowed_at'] == shadowed_at
        assert result['superseded_by'] == obs_b.observation_id
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0028 Phase4: additive filter tests (memory_types, source_files, references)
# ---------------------------------------------------------------------------


def test_search_memory_types_filter_matches_only_specified(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'mt_filter.db'))
    try:
        obs_decision = Observation(
            content='a design decision',
            project='p',
            memory_type='decision',
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs_note = Observation(
            content='a note',
            project='p',
            memory_type='note',
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        idx.upsert(obs_decision)
        idx.upsert(obs_note)
        hits = idx.search(memory_types=['decision'])
        ids = {h.observation_id for h in hits}
        assert obs_decision.observation_id in ids
        assert obs_note.observation_id not in ids
    finally:
        idx.close()


def test_search_memory_types_filter_none_returns_all(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'mt_none.db'))
    try:
        obs1 = Observation(
            content='c1',
            project='p',
            memory_type='decision',
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs2 = Observation(
            content='c2',
            project='p',
            memory_type='bug',
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        idx.upsert(obs1)
        idx.upsert(obs2)
        hits = idx.search(memory_types=None)
        ids = {h.observation_id for h in hits}
        assert obs1.observation_id in ids
        assert obs2.observation_id in ids
    finally:
        idx.close()


def test_search_memory_types_filter_empty_list_returns_all(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'mt_empty.db'))
    try:
        obs1 = Observation(
            content='c1',
            project='p',
            memory_type='decision',
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs2 = Observation(
            content='c2',
            project='p',
            memory_type='note',
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        idx.upsert(obs1)
        idx.upsert(obs2)
        hits = idx.search(memory_types=[])
        ids = {h.observation_id for h in hits}
        assert obs1.observation_id in ids
        assert obs2.observation_id in ids
    finally:
        idx.close()


def test_search_source_files_filter_exact(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'sf_filter.db'))
    try:
        obs_a = Observation(
            content='obs with src/a.py',
            project='p',
            source_files=['src/a.py'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs_b = Observation(
            content='obs with src/b.py',
            project='p',
            source_files=['src/b.py'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        idx.upsert(obs_a)
        idx.upsert(obs_b)
        hits = idx.search(source_files=['src/a.py'])
        ids = {h.observation_id for h in hits}
        assert obs_a.observation_id in ids
        assert obs_b.observation_id not in ids
    finally:
        idx.close()


def test_search_references_filter_exact(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'ref_filter.db'))
    try:
        obs1 = Observation(
            content='obs with ref #1',
            project='p',
            references=['#1'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs2 = Observation(
            content='obs with ref #2',
            project='p',
            references=['#2'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        idx.upsert(obs1)
        idx.upsert(obs2)
        hits = idx.search(references=['#2'])
        ids = {h.observation_id for h in hits}
        assert obs2.observation_id in ids
        assert obs1.observation_id not in ids
    finally:
        idx.close()


def test_search_composite_memory_types_and_source_files(tmp_path: Path) -> None:
    idx = LocalIndex.connect(str(tmp_path / 'composite.db'))
    try:
        obs_match = Observation(
            content='decision with src/a.py',
            project='p',
            memory_type='decision',
            source_files=['src/a.py'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs_wrong_type = Observation(
            content='note with src/a.py',
            project='p',
            memory_type='note',
            source_files=['src/a.py'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        obs_wrong_file = Observation(
            content='decision with src/b.py',
            project='p',
            memory_type='decision',
            source_files=['src/b.py'],
            agent_family='c',
            client_id='c',
            pc_id='p',
            session_id='s',
        )
        idx.upsert(obs_match)
        idx.upsert(obs_wrong_type)
        idx.upsert(obs_wrong_file)
        hits = idx.search(memory_types=['decision'], source_files=['src/a.py'])
        ids = {h.observation_id for h in hits}
        assert obs_match.observation_id in ids
        assert obs_wrong_type.observation_id not in ids
        assert obs_wrong_file.observation_id not in ids
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# ADR-0029 PR 3: legacy read is always skipped (KIOKU_MESH_LEGACY_READ_FALLBACK
# escape hatch removed in v1.0; the env var is now inert under any value).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('legacy_read_fallback_env', [None, 'on'])
def test_rebuild_from_zenoh_always_skips_legacy_obs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_read_fallback_env: str | None,
) -> None:
    """rebuild_from_zenoh must never ingest legacy obs, regardless of the (now-inert) env var."""
    if legacy_read_fallback_env is None:
        monkeypatch.delenv('KIOKU_MESH_LEGACY_READ_FALLBACK', raising=False)
    else:
        monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', legacy_read_fallback_env)
    obs = _mk_legacy_obs('should-be-skipped')
    session = _FakeSession([obs], [])
    idx = LocalIndex.connect(str(tmp_path / f'gate_{legacy_read_fallback_env}_obs.db'))
    try:
        stats = idx.rebuild_from_zenoh(session)
        assert stats.added == 0, 'legacy obs must never be ingested (v1.0 removed the read fallback)'
    finally:
        idx.close()


@pytest.mark.parametrize('legacy_read_fallback_env', [None, 'on'])
def test_rebuild_from_zenoh_always_skips_legacy_tomb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_read_fallback_env: str | None,
) -> None:
    """rebuild_from_zenoh must never ingest legacy tombs, regardless of the (now-inert) env var."""
    if legacy_read_fallback_env is None:
        monkeypatch.delenv('KIOKU_MESH_LEGACY_READ_FALLBACK', raising=False)
    else:
        monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', legacy_read_fallback_env)
    obs = _mk_legacy_obs('pre-existing')
    tomb = Tombstone(observation_id=obs.observation_id, deleted_at='2024-01-01T00:00:00Z')
    idx = LocalIndex.connect(str(tmp_path / f'gate_{legacy_read_fallback_env}_tomb.db'))
    try:
        # Pre-populate with a live row so the rebuild has something to tombstone.
        idx.upsert(obs)
        stats = idx.rebuild_from_zenoh(_FakeSession([], [tomb], legacy_tomb_keys=True))
        # The tomb is always skipped — marked_deleted stays 0 (no tombstone applied).
        assert stats.marked_deleted == 0, 'legacy tomb must never be ingested (v1.0 removed the read fallback)'
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# repair_from_zenoh tests (ADR-0035: repair-only realignment)
# ---------------------------------------------------------------------------


class _FakeErrReply:
    """Reply carrying an ``err`` (a storage/router refused the query)."""

    ok = None

    def __init__(self, message: str = 'boom') -> None:
        self.err = _FakeOk(message, '')


class _FailingSession(_FakeSession):
    """FakeSession whose obs or tomb scan fails in a chosen way."""

    def __init__(
        self,
        obs: list[Observation],
        tombs: list[Tombstone],
        *,
        fail_on: str,
        failure: str,
    ) -> None:
        super().__init__(obs, tombs)
        self._fail_on = fail_on
        self._failure = failure

    def get(self, key_expr: str, **kwargs: object) -> list:  # type: ignore[override]
        marker = 'obs' if '/obs/' in key_expr else 'tomb'
        if marker != self._fail_on:
            return super().get(key_expr, **kwargs)
        if self._failure == 'err':
            return [_FakeErrReply()]
        if self._failure == 'timeout':
            raise TimeoutError('scan timed out')
        if self._failure == 'malformed':
            # Canonical key with a corrupt payload — a record of ours that
            # cannot be parsed, not an off-namespace key the scan may skip.
            replies = self._obs_replies if marker == 'obs' else self._tomb_replies
            return [_FakeReply('{not json', str(replies[0].ok.key_expr))]
        raise AssertionError(f'unknown failure {self._failure}')


def _live_ids(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as raw:
        return {
            row[0]
            for row in raw.execute(
                'SELECT observation_id FROM obs_index WHERE deleted_at IS NULL AND shadowed_at IS NULL'
            ).fetchall()
        }


def test_repair_keeps_local_only_rows_live(tmp_path: Path) -> None:
    """The core safety property: absence from the scan means nothing."""
    db = tmp_path / 'repair_local_only.db'
    idx = LocalIndex.connect(str(db))
    try:
        local_only = _mk_obs('local only', project='r')
        remote = _mk_obs('remote', project='r')
        idx.upsert(local_only)

        stats = idx.repair_from_zenoh(_FakeSession([remote], []))

        assert stats.added == 1
        assert _live_ids(db) == {local_only.observation_id, remote.observation_id}
        assert idx.visibility_counts().shadowed == 0
    finally:
        idx.close()


def test_repair_upserts_and_marks_tombstones(tmp_path: Path) -> None:
    db = tmp_path / 'repair_apply.db'
    idx = LocalIndex.connect(str(db))
    try:
        deleted = _mk_obs('will be deleted', project='r')
        idx.upsert(deleted)
        fresh = _mk_obs('fresh', project='r')
        tomb = Tombstone(observation_id=deleted.observation_id, deleted_at='2026-04-30T00:00:00.000000Z')

        stats = idx.repair_from_zenoh(_FakeSession([fresh], [tomb]))

        assert (stats.added, stats.marked_deleted) == (1, 1)
        assert _live_ids(db) == {fresh.observation_id}
        state = idx.alignment_state()
        assert state.last_full_repair_completed_at
        assert state.last_success_mode == 'full'
        assert state.last_failure_at == ''
    finally:
        idx.close()


def test_repair_tombstones_mode_does_not_scan_observations(tmp_path: Path) -> None:
    db = tmp_path / 'repair_tomb_mode.db'
    idx = LocalIndex.connect(str(db))
    try:
        known = _mk_obs('known', project='r')
        idx.upsert(known)
        unseen = _mk_obs('not in index', project='r')
        tomb = Tombstone(observation_id=known.observation_id, deleted_at='2026-04-30T00:00:00.000000Z')

        stats = idx.repair_from_zenoh(_FakeSession([unseen], [tomb]), mode='tombstones')

        assert stats.scanned_obs == 0
        assert stats.added == 0
        assert stats.marked_deleted == 1
        assert _live_ids(db) == set()
        state = idx.alignment_state()
        assert state.last_tomb_repair_completed_at
        assert state.last_full_repair_completed_at == ''
    finally:
        idx.close()


@pytest.mark.parametrize('fail_on', ['obs', 'tomb'])
@pytest.mark.parametrize('failure', ['err', 'timeout', 'malformed'])
def test_repair_scan_failure_applies_nothing(tmp_path: Path, fail_on: str, failure: str) -> None:
    """Err / timeout / malformed: no shadow, no apply, no success timestamp."""
    db = tmp_path / f'repair_fail_{fail_on}_{failure}.db'
    idx = LocalIndex.connect(str(db))
    try:
        local_only = _mk_obs('local only', project='r')
        idx.upsert(local_only)
        remote = _mk_obs('remote', project='r')
        tomb = Tombstone(observation_id=local_only.observation_id, deleted_at='2026-04-30T00:00:00.000000Z')

        with pytest.raises(Exception):  # noqa: B017 — class varies by failure mode
            idx.repair_from_zenoh(_FailingSession([remote], [tomb], fail_on=fail_on, failure=failure))

        assert _live_ids(db) == {local_only.observation_id}, 'failed repair must not apply or hide anything'
        state = idx.alignment_state()
        assert state.last_full_repair_completed_at == ''
        assert state.last_failure_at
        assert state.last_failure_mode == 'full'
        assert state.last_failure_class
    finally:
        idx.close()


def test_repair_sqlite_failure_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / 'repair_sqlite_fail.db'
    idx = LocalIndex.connect(str(db))
    try:
        local_only = _mk_obs('local only', project='r')
        idx.upsert(local_only)
        remote = _mk_obs('remote', project='r')

        real_conn = idx._conn

        class _BrokenConn:
            def __getattr__(self, name: str) -> object:
                return getattr(real_conn, name)

            def executemany(self, *args: object, **kwargs: object) -> NoReturn:
                raise sqlite3.OperationalError('disk I/O error')

        idx._conn = _BrokenConn()  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            idx.repair_from_zenoh(_FakeSession([remote], []))
        idx._conn = real_conn

        assert _live_ids(db) == {local_only.observation_id}
        state = idx.alignment_state()
        assert state.last_full_repair_completed_at == ''
        assert state.last_failure_class == 'OperationalError'
    finally:
        idx.close()


def test_repair_persists_orphan_tombstone(tmp_path: Path) -> None:
    """N2: repair (full mode) must reuse R5's orphan placeholder, not discard it."""
    db_path = tmp_path / 'repair_orphan.db'
    idx = LocalIndex.connect(str(db_path))
    try:
        orphan_id = '0' * 32
        deleted_at = '2026-05-01T00:00:00.000000Z'
        tomb = Tombstone(observation_id=orphan_id, deleted_at=deleted_at)

        stats = idx.repair_from_zenoh(_FakeSession([], [tomb]))

        assert stats.orphaned == 1
        assert stats.marked_deleted == 0
        assert idx.row_count() == 1
        assert idx.search(project='r') == []
        with sqlite3.connect(str(db_path)) as raw:
            got = raw.execute(
                'SELECT deleted_at, payload_json FROM obs_index WHERE observation_id = ?',
                (orphan_id,),
            ).fetchone()
            assert got == (deleted_at, None)
    finally:
        idx.close()


def test_repair_tombstones_mode_does_not_treat_unscanned_obs_as_orphan(tmp_path: Path) -> None:
    """Tombstones-only mode never scans observations, so it must not persist orphans.

    "not seen this scan" only means "confirmed absent" when obs were actually scanned.
    """
    db_path = tmp_path / 'repair_tomb_mode_no_orphan.db'
    idx = LocalIndex.connect(str(db_path))
    try:
        unindexed_id = '0' * 32
        tomb = Tombstone(observation_id=unindexed_id, deleted_at='2026-05-01T00:00:00.000000Z')

        stats = idx.repair_from_zenoh(_FakeSession([], [tomb]), mode='tombstones')

        assert stats.orphaned == 0
        assert stats.marked_deleted == 0
        assert idx.row_count() == 0
    finally:
        idx.close()


def test_repair_does_not_change_rebuild_shadow_semantics(tmp_path: Path) -> None:
    """Rebuild still shadows absent rows; repair on the same index does not."""
    db = tmp_path / 'repair_vs_rebuild.db'
    idx = LocalIndex.connect(str(db))
    try:
        local_only = _mk_obs('local only', project='r')
        idx.upsert(local_only)
        idx.repair_from_zenoh(_FakeSession([], []))
        assert _live_ids(db) == {local_only.observation_id}

        idx.rebuild_from_zenoh(_FakeSession([], []))
        assert _live_ids(db) == set()
        assert idx.visibility_counts().shadowed == 1
    finally:
        idx.close()


def test_alignment_state_migration_preserves_existing_v4_db(tmp_path: Path) -> None:
    """Opening a pre-v5 database adds the state table without losing rows."""
    db = tmp_path / 'migrate_v5.db'
    idx = LocalIndex.connect(str(db))
    obs = _mk_obs('written at v4', project='r')
    idx.upsert(obs)
    idx.close()

    # Simulate a database written before schema v5.
    with sqlite3.connect(str(db)) as raw:
        raw.execute('DROP TABLE index_alignment_state')
        raw.execute('DELETE FROM schema_version')
        raw.execute('INSERT INTO schema_version(version) VALUES (4)')

    idx = LocalIndex.connect(str(db))
    try:
        assert {r.observation_id for r in idx.search(project='r')} == {obs.observation_id}
        assert idx.alignment_state() == type(idx.alignment_state())()
        with sqlite3.connect(str(db)) as raw:
            assert raw.execute('SELECT version FROM schema_version').fetchone()[0] == SCHEMA_VERSION
        idx.repair_from_zenoh(_FakeSession([obs], []))
        assert idx.alignment_state().last_full_repair_completed_at
    finally:
        idx.close()

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
from mesh_mem.local_index import SCHEMA_VERSION
from mesh_mem.models import Observation


def _mk_obs(content: str, *, project: str = 'demo', tags: list[str] | None = None) -> Observation:
    return Observation(
        content=content,
        project=project,
        tags=list(tags or []),
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
            indexes = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert 'idx_project_created' in indexes
            assert 'idx_created' in indexes
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

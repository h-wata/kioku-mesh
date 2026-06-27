"""Unit tests for LocalRawStore (ADR-0028 Phase 2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kioku_mesh.memory.local_raw_store import LocalRawStore
from kioku_mesh.models import Observation
from kioku_mesh.models import Tombstone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obs(content: str = 'test content', project: str = 'test') -> Observation:
    return Observation(content=content, project=project)


def _make_tomb(observation_id: str, deleted_at: str = '2026-01-01T00:00:00.000000Z') -> Tombstone:
    return Tombstone(observation_id=observation_id, reason='', deleted_at=deleted_at)


def _make_index_db(path: Path, rows: list[dict]) -> None:
    """Create a minimal obs_index SQLite file for migration tests."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
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
        )
    """)
    for row in rows:
        conn.execute(
            'INSERT INTO obs_index'
            ' (observation_id, project, created_at, memory_type, importance,'
            '  subject, summary, payload_json, deleted_at, shadowed_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                row['observation_id'],
                row.get('project', 'test'),
                row.get('created_at', '2026-01-01T00:00:00.000000Z'),
                row.get('memory_type', 'note'),
                row.get('importance', 2),
                row.get('subject', ''),
                row.get('summary', ''),
                row.get('payload_json', '{}'),
                row.get('deleted_at'),
                row.get('shadowed_at'),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_schema_tables_exist(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    conn = sqlite3.connect(str(tmp_path / 'raw.db'))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert 'raw_schema_version' in tables
    assert 'migration_meta' in tables
    assert 'local_obs' in tables
    assert 'local_tomb' in tables
    conn.close()
    store.close()


def test_schema_version(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    assert store.schema_version() == 1
    store.close()


def test_schema_columns_local_obs(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    conn = sqlite3.connect(str(tmp_path / 'raw.db'))
    cols = {row[1] for row in conn.execute('PRAGMA table_info(local_obs)').fetchall()}
    assert {'observation_id', 'payload_json', 'created_at'} <= cols
    conn.close()
    store.close()


def test_schema_columns_local_tomb(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    conn = sqlite3.connect(str(tmp_path / 'raw.db'))
    cols = {row[1] for row in conn.execute('PRAGMA table_info(local_tomb)').fetchall()}
    assert {'observation_id', 'tomb_json', 'deleted_at'} <= cols
    conn.close()
    store.close()


# ---------------------------------------------------------------------------
# Idempotent put tests
# ---------------------------------------------------------------------------


def test_put_obs_idempotent(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    obs = _make_obs()
    store.put_obs(obs)
    store.put_obs(obs)
    result = list(store.scan_obs())
    assert len(result) == 1
    assert result[0].observation_id == obs.observation_id
    store.close()


def test_put_tomb_idempotent(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    obs = _make_obs()
    tomb = _make_tomb(obs.observation_id)
    store.put_tomb(tomb)
    store.put_tomb(tomb)
    conn = sqlite3.connect(str(tmp_path / 'raw.db'))
    (count,) = conn.execute('SELECT COUNT(*) FROM local_tomb').fetchone()
    conn.close()
    assert count == 1
    store.close()


def test_put_obs_updates_payload(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    obs = _make_obs('original')
    store.put_obs(obs)
    obs2 = Observation(
        observation_id=obs.observation_id,
        content='updated',
        project='test',
    )
    store.put_obs(obs2)
    result = list(store.scan_obs())
    assert len(result) == 1
    assert result[0].content == 'updated'
    store.close()


# ---------------------------------------------------------------------------
# Scan ordering agnostic test
# ---------------------------------------------------------------------------


def test_scan_obs_ordering_agnostic(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    obs_a = _make_obs('alpha')
    obs_b = _make_obs('beta')
    obs_c = _make_obs('gamma')
    store.put_obs(obs_a)
    store.put_obs(obs_b)
    store.put_obs(obs_c)
    ids = {obs.observation_id for obs in store.scan_obs()}
    assert ids == {obs_a.observation_id, obs_b.observation_id, obs_c.observation_id}
    store.close()


def test_scan_tombs_ordering_agnostic(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    t1 = _make_tomb('a' * 32, '2026-01-01T00:00:00.000000Z')
    t2 = _make_tomb('b' * 32, '2026-01-02T00:00:00.000000Z')
    store.put_tomb(t1)
    store.put_tomb(t2)
    ids = {t.observation_id for t in store.scan_tombs()}
    assert ids == {'a' * 32, 'b' * 32}
    store.close()


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


def test_delete_obs(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    obs = _make_obs()
    store.put_obs(obs)
    store.delete_obs(obs.observation_id)
    result = list(store.scan_obs())
    assert not any(o.observation_id == obs.observation_id for o in result)
    store.close()


def test_delete_tomb(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path / 'raw.db')
    obs = _make_obs()
    tomb = _make_tomb(obs.observation_id)
    store.put_tomb(tomb)
    store.delete_tomb(obs.observation_id)
    result = list(store.scan_tombs())
    assert not any(t.observation_id == obs.observation_id for t in result)
    store.close()


# ---------------------------------------------------------------------------
# Migration marker tests
# ---------------------------------------------------------------------------


def test_migration_marker_idempotent(tmp_path: Path) -> None:
    """migrate_from_index called twice must not duplicate rows."""
    obs = _make_obs('migrated content')
    index_db = tmp_path / 'index.db'
    _make_index_db(
        index_db,
        [
            {
                'observation_id': obs.observation_id,
                'payload_json': obs.to_json(),
                'created_at': obs.created_at,
                'deleted_at': None,
                'shadowed_at': None,
            }
        ],
    )
    raw_db = tmp_path / 'raw.db'
    store = LocalRawStore(raw_db)
    store.migrate_from_index(index_db)
    store.migrate_from_index(index_db)
    obs_list = list(store.scan_obs())
    assert len(obs_list) == 1
    store.close()


def test_migration_tombstone_row(tmp_path: Path) -> None:
    """Tombstoned index rows are migrated to local_tomb, not local_obs."""
    obs = _make_obs('deleted content')
    index_db = tmp_path / 'index.db'
    _make_index_db(
        index_db,
        [
            {
                'observation_id': obs.observation_id,
                'payload_json': obs.to_json(),
                'created_at': obs.created_at,
                'deleted_at': '2026-01-01T00:00:00.000000Z',
                'shadowed_at': None,
            }
        ],
    )
    store = LocalRawStore(tmp_path / 'raw.db')
    store.migrate_from_index(index_db)
    obs_list = list(store.scan_obs())
    tomb_list = list(store.scan_tombs())
    assert len(obs_list) == 0
    assert len(tomb_list) == 1
    assert tomb_list[0].observation_id == obs.observation_id
    store.close()


def test_migration_index_row_count_unchanged(tmp_path: Path) -> None:
    """Migration must not delete rows from index.db."""
    obs = _make_obs('live content')
    index_db = tmp_path / 'index.db'
    _make_index_db(
        index_db,
        [
            {
                'observation_id': obs.observation_id,
                'payload_json': obs.to_json(),
                'created_at': obs.created_at,
                'deleted_at': None,
                'shadowed_at': None,
            }
        ],
    )
    store = LocalRawStore(tmp_path / 'raw.db')
    store.migrate_from_index(index_db)
    conn = sqlite3.connect(str(index_db))
    (count,) = conn.execute('SELECT COUNT(*) FROM obs_index').fetchone()
    conn.close()
    assert count == 1
    store.close()


# ---------------------------------------------------------------------------
# Shadowed row exclusion test
# ---------------------------------------------------------------------------


def test_shadowed_row_not_migrated(tmp_path: Path) -> None:
    """Shadowed rows (shadowed_at IS NOT NULL, deleted_at IS NULL) must not be copied."""
    obs_live = _make_obs('live row')
    obs_shadow = _make_obs('shadowed row')
    index_db = tmp_path / 'index.db'
    _make_index_db(
        index_db,
        [
            {
                'observation_id': obs_live.observation_id,
                'payload_json': obs_live.to_json(),
                'created_at': obs_live.created_at,
                'deleted_at': None,
                'shadowed_at': None,
            },
            {
                'observation_id': obs_shadow.observation_id,
                'payload_json': obs_shadow.to_json(),
                'created_at': obs_shadow.created_at,
                'deleted_at': None,
                'shadowed_at': '2026-01-01T00:00:00.000000Z',
            },
        ],
    )
    store = LocalRawStore(tmp_path / 'raw.db')
    store.migrate_from_index(index_db)
    obs_ids = {o.observation_id for o in store.scan_obs()}
    assert obs_live.observation_id in obs_ids
    assert obs_shadow.observation_id not in obs_ids
    store.close()


def test_migration_nonexistent_index_is_noop(tmp_path: Path) -> None:
    """If index.db does not exist, migrate_from_index is a silent no-op."""
    store = LocalRawStore(tmp_path / 'raw.db')
    store.migrate_from_index(tmp_path / 'nonexistent.db')
    assert list(store.scan_obs()) == []
    store.close()

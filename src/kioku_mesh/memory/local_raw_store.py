"""SQLite raw store for local backend (ADR-0028 Phase 2).

raw.db is the durable source-of-truth for local mode.  index.db is a
derived, best-effort view rebuilt from raw.db on open.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import threading
from typing import Iterator

from ..core.models import Observation
from ..core.models import Tombstone

log = logging.getLogger(__name__)

RAW_SCHEMA_VERSION = 1

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS raw_schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS migration_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_obs (
    observation_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS local_tomb (
    observation_id TEXT PRIMARY KEY,
    tomb_json TEXT NOT NULL,
    deleted_at TEXT
);
"""


def _open_raw_conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.executescript(_SCHEMA_SQL)
    conn.execute('DELETE FROM raw_schema_version')
    conn.execute('INSERT INTO raw_schema_version(version) VALUES (?)', (RAW_SCHEMA_VERSION,))
    conn.commit()
    return conn


class LocalRawStore:
    """SQLite raw store holding raw Observation and Tombstone payloads.

    raw.db has an independent schema version from index.db (ADR-0028).
    Thread-safe via a single lock per instance.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = _open_raw_conn(db_path)
        self._lock = threading.Lock()

    def put_obs(self, obs: Observation) -> None:
        """INSERT OR REPLACE an observation into local_obs."""
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO local_obs(observation_id, payload_json, created_at) VALUES (?, ?, ?)',
                (obs.observation_id, obs.to_json(), obs.created_at),
            )
            self._conn.commit()

    def put_tomb(self, tomb: Tombstone) -> None:
        """INSERT OR REPLACE a tombstone into local_tomb."""
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO local_tomb(observation_id, tomb_json, deleted_at) VALUES (?, ?, ?)',
                (tomb.observation_id, tomb.to_json(), tomb.deleted_at),
            )
            self._conn.commit()

    def scan_obs(self) -> Iterator[Observation]:
        """Yield all observations from local_obs; order is not guaranteed."""
        with self._lock:
            rows = self._conn.execute('SELECT payload_json FROM local_obs').fetchall()
        for (payload_json,) in rows:
            try:
                yield Observation.from_json(payload_json)
            except Exception as e:  # noqa: BLE001
                log.warning('LocalRawStore.scan_obs skip malformed payload: %s', e)

    def scan_tombs(self) -> Iterator[Tombstone]:
        """Yield all tombstones from local_tomb; order is not guaranteed."""
        with self._lock:
            rows = self._conn.execute('SELECT tomb_json FROM local_tomb').fetchall()
        for (tomb_json,) in rows:
            try:
                yield Tombstone.from_json(tomb_json)
            except Exception as e:  # noqa: BLE001
                log.warning('LocalRawStore.scan_tombs skip malformed payload: %s', e)

    def delete_obs(self, observation_id: str) -> None:
        """Hard-DELETE a row from local_obs."""
        with self._lock:
            self._conn.execute('DELETE FROM local_obs WHERE observation_id = ?', (observation_id,))
            self._conn.commit()

    def delete_tomb(self, observation_id: str) -> None:
        """Hard-DELETE a row from local_tomb."""
        with self._lock:
            self._conn.execute('DELETE FROM local_tomb WHERE observation_id = ?', (observation_id,))
            self._conn.commit()

    def migrate_from_index(self, index_db_path: Path) -> None:
        """Copy pre-Phase2 index.db rows to raw.db.

        Copy-only (index.db is never deleted) and idempotent (marker in
        migration_meta prevents double-copy when raw.db already has data).

        Steps follow ADR-0028 Phase2 copy_strategy exactly:
        - Live rows (deleted_at IS NULL, shadowed_at IS NULL) -> local_obs.
        - Tombstoned rows (deleted_at IS NOT NULL) -> local_tomb.
        - Shadowed-only rows (shadowed_at IS NOT NULL, deleted_at IS NULL) are skipped.
        - Copy + marker committed in a single raw.db transaction.
        """
        if not index_db_path.exists():
            return

        marker_key = f'index_db_migrated_from={index_db_path.resolve()}'

        with self._lock:
            marker_row = self._conn.execute('SELECT value FROM migration_meta WHERE key = ?', (marker_key,)).fetchone()
            if marker_row is not None:
                obs_count = self._conn.execute('SELECT COUNT(*) FROM local_obs').fetchone()[0]
                tomb_count = self._conn.execute('SELECT COUNT(*) FROM local_tomb').fetchone()[0]
                if obs_count > 0 or tomb_count > 0:
                    return

            try:
                idx_conn = sqlite3.connect(f'file:{index_db_path.resolve()}?mode=ro', uri=True, timeout=5.0)
            except sqlite3.OperationalError as e:
                log.warning('LocalRawStore.migrate_from_index cannot open index.db: %s', e)
                return

            try:
                col_names = {row[1] for row in idx_conn.execute('PRAGMA table_info(obs_index)').fetchall()}
                has_shadowed_at = 'shadowed_at' in col_names

                if has_shadowed_at:
                    raw_rows = idx_conn.execute(
                        'SELECT observation_id, payload_json, deleted_at, shadowed_at FROM obs_index'
                    ).fetchall()
                else:
                    raw_rows = [
                        (oid, pj, da, None)
                        for oid, pj, da in idx_conn.execute(
                            'SELECT observation_id, payload_json, deleted_at FROM obs_index'
                        ).fetchall()
                    ]
            except sqlite3.Error as e:
                log.warning('LocalRawStore.migrate_from_index scan failed: %s', e)
                idx_conn.close()
                return
            finally:
                idx_conn.close()

            obs_rows: list[tuple[str, str, str | None]] = []
            tomb_rows: list[tuple[str, str, str]] = []

            for observation_id, payload_json, deleted_at, shadowed_at in raw_rows:
                if shadowed_at is not None and deleted_at is None:
                    continue
                if deleted_at is not None:
                    tomb = Tombstone(observation_id=observation_id, reason='', deleted_at=deleted_at)
                    tomb_rows.append((observation_id, tomb.to_json(), deleted_at))
                else:
                    if not payload_json:
                        continue
                    try:
                        obs = Observation.from_json(payload_json)
                    except Exception:  # noqa: BLE001
                        continue
                    obs_rows.append((observation_id, payload_json, obs.created_at))

            from datetime import datetime, timezone  # noqa: PLC0415

            completed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            try:
                self._conn.execute('BEGIN')
                if obs_rows:
                    self._conn.executemany(
                        'INSERT OR REPLACE INTO local_obs(observation_id, payload_json, created_at) VALUES (?, ?, ?)',
                        obs_rows,
                    )
                if tomb_rows:
                    self._conn.executemany(
                        'INSERT OR REPLACE INTO local_tomb(observation_id, tomb_json, deleted_at) VALUES (?, ?, ?)',
                        tomb_rows,
                    )
                self._conn.execute(
                    'INSERT OR REPLACE INTO migration_meta(key, value) VALUES (?, ?)',
                    (marker_key, completed_at),
                )
                self._conn.execute('COMMIT')
            except sqlite3.Error as e:
                try:
                    self._conn.execute('ROLLBACK')
                except Exception:  # noqa: BLE001
                    pass
                log.warning('LocalRawStore.migrate_from_index transaction failed: %s', e)

    def schema_version(self) -> int:
        """Return the raw store schema version."""
        with self._lock:
            row = self._conn.execute('SELECT version FROM raw_schema_version').fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as e:
                log.warning('LocalRawStore.close failed: %s', e)

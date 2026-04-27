"""Local SQLite sidecar index for observation metadata.

Issue #7 / TASK-131 plan B. Zenoh-rocksdb stays the source of truth; this
file maintains a per-process SQLite mirror that the read path will switch
to in Phase 3. Phase 2 (this commit) only wires up the write side: every
``put_observation`` upserts into ``obs_index``, every ``put_tombstone``
stamps ``deleted_at``. Reads still go through the Zenoh full-scan path.

Failure semantics: SQLite is a sidecar. Zenoh write success is the contract;
SQLite errors are logged and swallowed so a corrupt index file cannot turn
a working put into a failure. The Phase 4 rebuild path (not yet implemented)
will repopulate the index from Zenoh on demand.

Disable: set ``MESH_MEM_DISABLE_INDEX=1`` (or ``MESH_MEM_INDEX_DB=:memory:``
for an ephemeral in-process DB) — the LocalIndex methods become no-ops in
the disabled case so callers don't branch.

Schema validated by TASK-134 spike at 50k rows: rebuild ~0.4s, query p99
~0.04ms, file size ~49MB. See docs/poc-reports/raw/TASK-134-spike-issue-7-result.yaml.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
import threading

from .identity import state_dir
from .models import Observation

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS obs_index (
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
CREATE INDEX IF NOT EXISTS idx_project_created ON obs_index(project, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_created ON obs_index(created_at DESC);
"""

_UPSERT_SQL = (
    'INSERT INTO obs_index '
    '(observation_id, project, created_at, memory_type, importance, subject, summary, payload_json) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
    'ON CONFLICT(observation_id) DO UPDATE SET '
    'project=excluded.project, '
    'created_at=excluded.created_at, '
    'memory_type=excluded.memory_type, '
    'importance=excluded.importance, '
    'subject=excluded.subject, '
    'summary=excluded.summary, '
    'payload_json=excluded.payload_json'
)

_MARK_DELETED_SQL = 'UPDATE obs_index SET deleted_at = ? WHERE observation_id = ?'


def _disabled_via_env() -> bool:
    return os.environ.get('MESH_MEM_DISABLE_INDEX', '').strip() == '1'


def _resolve_db_path() -> str:
    """Resolve the SQLite DB path from env or fall back to state_dir().

    Returns the literal ``:memory:`` if explicitly requested so callers can
    short-circuit to an in-process DB without touching disk.
    """
    override = os.environ.get('MESH_MEM_INDEX_DB', '').strip()
    if override:
        return override
    return str(state_dir() / 'index.db')


def _open_connection(path: str) -> sqlite3.Connection:
    """Open a SQLite connection at ``path``, applying PRAGMA + schema.

    ``check_same_thread=False`` because put_observation may run on a
    different thread than the MCP stdio handler (and a future Phase 4
    subscriber thread). Method-level locking in :class:`LocalIndex`
    serializes access.
    """
    if path != ':memory:':
        parent = Path(path).parent
        if str(parent) and parent != Path():
            parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.executescript(_SCHEMA_SQL)
    # Idempotent stamp; INSERT OR IGNORE so reopening an existing DB is fine.
    conn.execute('INSERT OR IGNORE INTO schema_version(version) VALUES (?)', (SCHEMA_VERSION,))
    conn.commit()
    return conn


class LocalIndex:
    """SQLite-backed sidecar index. Thread-safe via a single lock per instance.

    Construction is cheap (no I/O); ``connect`` opens the file, applies PRAGMA,
    runs ``CREATE IF NOT EXISTS``, and stamps schema_version. A disabled
    instance (``MESH_MEM_DISABLE_INDEX=1``) holds no connection and short-
    circuits every method.
    """

    def __init__(
        self,
        db_path: str,
        disabled: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._db_path = db_path
        self._disabled = disabled
        self._conn: sqlite3.Connection | None = conn
        self._lock = threading.Lock()

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def disabled(self) -> bool:
        return self._disabled

    @classmethod
    def connect(cls, db_path: str | None = None) -> 'LocalIndex':
        """Open (or no-op) and return a ready-to-use LocalIndex.

        ``db_path=None`` resolves from env (``MESH_MEM_INDEX_DB``) or
        ``state_dir()/index.db``. The returned instance is always non-None
        so callers can chain method calls without checking for disable.
        On open failure the returned instance silently falls back to
        disabled mode so a corrupt index file cannot block puts.
        """
        if _disabled_via_env():
            log.info('LocalIndex disabled via MESH_MEM_DISABLE_INDEX=1')
            return cls(db_path='', disabled=True)
        path = db_path if db_path is not None else _resolve_db_path()
        try:
            conn = _open_connection(path)
        except (sqlite3.Error, OSError) as e:
            log.warning('LocalIndex open failed (%s); falling back to disabled: %s', path, e)
            return cls(db_path=path, disabled=True)
        return cls(db_path=path, disabled=False, conn=conn)

    def upsert(self, obs: Observation) -> None:
        """Insert or replace ``obs`` in the index. No-op when disabled."""
        if self._disabled or self._conn is None:
            return
        row = (
            obs.observation_id,
            obs.project,
            obs.created_at,
            obs.memory_type,
            obs.importance,
            obs.subject,
            obs.summary,
            obs.to_json(),
        )
        with self._lock:
            try:
                self._conn.execute(_UPSERT_SQL, row)
                self._conn.commit()
            except sqlite3.Error as e:
                log.warning('LocalIndex.upsert failed for %s: %s', obs.observation_id, e)

    def mark_deleted(self, observation_id: str, deleted_at: str) -> None:
        """Stamp ``deleted_at`` on the row matching ``observation_id``.

        If no row exists yet (tombstone arrived before observation), the
        UPDATE is a silent no-op. Phase 4 subscriber will reconcile via
        rebuild — this matches the "zenoh is truth" policy in TASK-131 §3.4.
        """
        if self._disabled or self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(_MARK_DELETED_SQL, (deleted_at, observation_id))
                self._conn.commit()
            except sqlite3.Error as e:
                log.warning('LocalIndex.mark_deleted failed for %s: %s', observation_id, e)

    def search_by_project(self, project: str, limit: int = 50) -> list[Observation]:
        """Return live observations for ``project`` ordered by created_at DESC.

        Phase 3 will swap ``store.search_observations`` to call this (or a
        richer variant). Exposed in Phase 2 so tests can verify writes
        landed without going through Zenoh.
        """
        if self._disabled or self._conn is None:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    'SELECT payload_json FROM obs_index '
                    'WHERE project = ? AND deleted_at IS NULL '
                    'ORDER BY created_at DESC LIMIT ?',
                    (project, limit),
                ).fetchall()
            except sqlite3.Error as e:
                log.warning('LocalIndex.search_by_project failed: %s', e)
                return []
        out: list[Observation] = []
        for (payload,) in rows:
            try:
                out.append(Observation.from_json(payload))
            except Exception as e:  # noqa: BLE001 — malformed payload should not crash search
                log.warning('LocalIndex skip malformed payload: %s', e)
        return out

    def row_count(self) -> int:
        """Return the total number of rows. Returns 0 when disabled."""
        if self._disabled or self._conn is None:
            return 0
        with self._lock:
            try:
                (count,) = self._conn.execute('SELECT COUNT(*) FROM obs_index').fetchone()
            except sqlite3.Error as e:
                log.warning('LocalIndex.row_count failed: %s', e)
                return 0
        return int(count)

    def close(self) -> None:
        """Close the underlying connection. Safe to call multiple times."""
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as e:
                log.warning('LocalIndex.close failed: %s', e)
            finally:
                self._conn = None

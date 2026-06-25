"""Local SQLite sidecar index for observation metadata.

Issue #7 / TASK-131 plan B. Zenoh-rocksdb stays the source of truth; this
file maintains a per-process SQLite mirror that ``store.search_observations``
reads from since Phase 3. ``put_observation`` upserts into ``obs_index``,
``put_tombstone`` stamps ``deleted_at`` (no row delete). The Zenoh full-
scan path stays available behind ``MESH_MEM_DISABLE_INDEX=1`` as a fallback.

Failure semantics: SQLite is a sidecar. Zenoh write success is the contract;
SQLite errors are logged and swallowed so a corrupt index file cannot turn
a working put into a failure. The Phase 4 rebuild path (not yet implemented)
will repopulate the index from Zenoh on demand.

Disable: set ``MESH_MEM_DISABLE_INDEX=1`` (or ``MESH_MEM_INDEX_DB=:memory:``
for an ephemeral in-process DB) — the LocalIndex methods become no-ops in
the disabled case so callers don't branch.

Identity filters (``agent_family`` / ``client_id`` / ``pc_id`` / ``session_id``)
use ``json_extract(payload_json, ...)`` rather than dedicated columns. At
PoC scale this stays well under the sub-200ms target (TASK-134 spike). A
schema migration to lift these into indexable columns is deferred to a
later issue once 100k+ workloads or skewed identity distributions need it.

Schema validated by TASK-134 spike at 50k rows: rebuild ~0.4s, query p99
~0.04ms, file size ~49MB. See docs/poc-reports/raw/TASK-134-spike-issue-7-result.yaml.

ADR-0021: Schema version 3 adds FTS5 (trigram tokenizer) full-text search
and supersedes-aware filtering. The ``obs_fts`` virtual table indexes
content/subject/summary/tags/project (not identity fields) and is kept in
lockstep with ``obs_index``. ``superseded_by`` column on ``obs_index``
stores the reverse edge for existence-based supersession filtering.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading

from ..core.identity import state_dir
from ..core.keyspace import obs_id_from_key
from ..core.keyspace import OBS_READ_KEY_EXPR
from ..core.keyspace import TOMB_READ_KEY_EXPR
from ..core.models import Observation
from ..core.models import Tombstone

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

# Issue #32: long-running processes (kioku-mesh-mcp) keep the index connection
# open indefinitely, which blocks SQLite's automatic WAL checkpoint from
# completing the truncate phase. The WAL therefore grows unbounded — observed
# 130 MB on a host that had been writing for weeks. Issue an explicit
# ``PRAGMA wal_checkpoint(TRUNCATE)`` every N upserts and once at close so
# the WAL stays bounded without introducing a checkpoint thread.
_CHECKPOINT_EVERY_N_UPSERTS = 256


@dataclasses.dataclass
class RebuildStats:
    added: int = 0
    marked_deleted: int = 0
    shadowed: int = 0
    unchanged: int = 0


@dataclasses.dataclass(frozen=True)
class VisibilityCounts:
    live: int = 0
    tombstoned: int = 0
    shadowed: int = 0


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
  deleted_at TEXT,
  shadowed_at TEXT,
  superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_project_created ON obs_index(project, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_created ON obs_index(created_at DESC);
"""

# ADR-0021: FTS5 virtual table covering semantic fields only.
# observation_id is stored UNINDEXED so we can look up the obs_index row via
# JOIN; it is not part of the full-text index itself.
_FTS_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
  observation_id UNINDEXED,
  content, subject, summary, tags, project,
  tokenize = 'trigram'
);
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
    'payload_json=excluded.payload_json, '
    'shadowed_at=NULL'
)

_MARK_DELETED_SQL = 'UPDATE obs_index SET deleted_at = ?, shadowed_at = NULL WHERE observation_id = ?'
_MARK_SHADOWED_SQL = (
    'UPDATE obs_index SET shadowed_at = COALESCE(shadowed_at, ?) WHERE observation_id = ? AND deleted_at IS NULL'
)

# FTS upsert: delete the old entry (full scan, acceptable for write path)
# then re-insert so duplicate entries never accumulate.
_FTS_DELETE_SQL = 'DELETE FROM obs_fts WHERE observation_id = ?'
_FTS_INSERT_SQL = (
    'INSERT INTO obs_fts(observation_id, content, subject, summary, tags, project) '
    'VALUES (?, ?, ?, ?, ?, ?)'
)
# Backward edge: written when the superseding obs is upserted.
# Only sets superseded_by if it is not already set, so the first superseder wins.
_SET_SUPERSEDED_BY_SQL = (
    'UPDATE obs_index SET superseded_by = ? '
    'WHERE observation_id = ? AND superseded_by IS NULL'
)


def _disabled_via_env() -> bool:
    return os.environ.get('MESH_MEM_DISABLE_INDEX', '').strip() == '1'


def _shadow_now_iso() -> str:
    """Return the local timestamp used to mark rebuild-shadowed rows."""
    from datetime import datetime
    from datetime import timezone

    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _resolve_db_path() -> str:
    """Resolve the SQLite DB path from env or fall back to state_dir().

    Returns the literal ``:memory:`` if explicitly requested so callers can
    short-circuit to an in-process DB without touching disk.
    """
    override = os.environ.get('MESH_MEM_INDEX_DB', '').strip()
    if override:
        return override
    return str(state_dir() / 'index.db')


def _try_create_fts5(conn: sqlite3.Connection) -> bool:
    """Attempt to create the obs_fts FTS5 (trigram) table.

    Returns True if FTS5 with the trigram tokenizer is available in this
    SQLite build (requires SQLite ≥ 3.34). Returns False and logs a one-time
    INFO message otherwise; callers fall back to LIKE-based search.

    On an existing table, re-syncs obs_fts from obs_index when the two have
    drifted out of lockstep. This covers the downgrade→upgrade gap: old code
    that lacks FTS support upserts obs_index rows without touching obs_fts, so
    on the next upgrade those rows would be silently missing from FTS search.
    A cheap count comparison detects the drift and triggers a full backfill.
    """
    try:
        fts_existed = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='obs_fts'"
            ).fetchone()
        )
        conn.execute(_FTS_CREATE_SQL)
        if not fts_existed:
            _backfill_fts5(conn)
        elif _fts_out_of_sync(conn):
            log.info('obs_fts out of sync with obs_index; rebuilding FTS index')
            conn.execute('DELETE FROM obs_fts')
            _backfill_fts5(conn)
        return True
    except sqlite3.OperationalError as exc:
        log.info('FTS5 trigram not available (%s); search falls back to LIKE', exc)
        # Clean up the probe table if it got created before the error.
        try:
            conn.execute('DROP TABLE IF EXISTS obs_fts')
        except sqlite3.Error:
            pass
        return False


def _fts_out_of_sync(conn: sqlite3.Connection) -> bool:
    """Return True when obs_fts and obs_index row counts disagree.

    A cheap proxy for "old code mutated obs_index without maintaining
    obs_fts" (downgrade→upgrade). Exact per-row reconciliation is left to
    ``rebuild_from_zenoh``; this only needs to notice the index is stale.
    """
    try:
        (obs_n,) = conn.execute('SELECT COUNT(*) FROM obs_index').fetchone()
        (fts_n,) = conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
    except sqlite3.Error:
        return False
    return obs_n != fts_n


def _backfill_fts5(conn: sqlite3.Connection) -> None:
    """Populate obs_fts from all existing obs_index rows (called once on creation)."""
    rows = conn.execute(
        'SELECT observation_id, subject, summary, payload_json, project FROM obs_index'
    ).fetchall()
    fts_rows = []
    for obs_id, subject, summary, payload_json, project in rows:
        try:
            data = json.loads(payload_json)
            content = data.get('content', '')
            tags = ' '.join(data.get('tags', []) if isinstance(data.get('tags'), list) else [])
        except Exception:  # noqa: BLE001
            content = ''
            tags = ''
        fts_rows.append((obs_id, content, subject or '', summary or '', tags, project or ''))
    if fts_rows:
        conn.executemany(_FTS_INSERT_SQL, fts_rows)


def _backfill_superseded_by(conn: sqlite3.Connection) -> None:
    """Backfill the superseded_by column from payload_json supersedes lists.

    Only needed once when migrating from schema v2 → v3. Uses
    ``WHERE superseded_by IS NULL`` so re-running is safe.
    """
    rows = conn.execute('SELECT observation_id, payload_json FROM obs_index').fetchall()
    updates: list[tuple[str, str]] = []
    for obs_id, payload_json in rows:
        try:
            data = json.loads(payload_json)
            for superseded_id in data.get('supersedes', []):
                if isinstance(superseded_id, str):
                    updates.append((obs_id, superseded_id))
        except Exception:  # noqa: BLE001
            pass
    if updates:
        conn.executemany(_SET_SUPERSEDED_BY_SQL, updates)


def _ensure_schema(conn: sqlite3.Connection) -> bool:
    """Apply schema creation and forward-only migrations.

    Returns True if FTS5 with trigram tokenizer is available (ADR-0021).
    """
    conn.executescript(_SCHEMA_SQL)
    cols = {row[1] for row in conn.execute('PRAGMA table_info(obs_index)').fetchall()}
    if 'shadowed_at' not in cols:
        conn.execute('ALTER TABLE obs_index ADD COLUMN shadowed_at TEXT')
    if 'superseded_by' not in cols:
        conn.execute('ALTER TABLE obs_index ADD COLUMN superseded_by TEXT')
        _backfill_superseded_by(conn)

    fts_capable = _try_create_fts5(conn)

    conn.execute('DELETE FROM schema_version')
    conn.execute('INSERT INTO schema_version(version) VALUES (?)', (SCHEMA_VERSION,))
    return fts_capable


def _open_connection(path: str) -> tuple[sqlite3.Connection, bool]:
    """Open a SQLite connection at ``path``, applying PRAGMA + schema.

    Returns ``(conn, fts_capable)`` where ``fts_capable`` is True when FTS5
    with the trigram tokenizer is available.

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
    fts_capable = _ensure_schema(conn)
    conn.commit()
    return conn, fts_capable


def _fts_row(obs: Observation) -> tuple[str, str, str, str, str, str]:
    """Build the (observation_id, content, subject, summary, tags, project) tuple for obs_fts."""
    tags_str = ' '.join(obs.tags) if obs.tags else ''
    return (obs.observation_id, obs.content, obs.subject, obs.summary, tags_str, obs.project)


class LocalIndex:
    """SQLite-backed sidecar index. Thread-safe via a single lock per instance.

    Construction is cheap (no I/O); ``connect`` opens the file, applies PRAGMA,
    runs ``CREATE IF NOT EXISTS``, and stamps schema_version. A disabled
    instance (``MESH_MEM_DISABLE_INDEX=1``) holds no connection and short-
    circuits every method.

    ADR-0021: when ``fts_capable`` is True the ``obs_fts`` FTS5 table is
    available and ``search(query=...)`` uses bm25 ranking for queries ≥ 3
    characters. Shorter queries and FTS-disabled environments fall back to the
    LIKE-based path transparently.
    """

    def __init__(
        self,
        db_path: str,
        disabled: bool = False,
        conn: sqlite3.Connection | None = None,
        fts_capable: bool = False,
    ) -> None:
        self._db_path = db_path
        self._disabled = disabled
        self._conn: sqlite3.Connection | None = conn
        self._lock = threading.Lock()
        # Counter for the periodic ``PRAGMA wal_checkpoint(TRUNCATE)`` (#32).
        # Reset every checkpoint and on close.
        self._upserts_since_checkpoint = 0
        self._fts_capable = fts_capable

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def fts_capable(self) -> bool:
        """True when FTS5 trigram search is available (SQLite ≥ 3.34)."""
        return self._fts_capable

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
            conn, fts_capable = _open_connection(path)
        except (sqlite3.Error, OSError) as e:
            log.warning('LocalIndex open failed (%s); falling back to disabled: %s', path, e)
            return cls(db_path=path, disabled=True)
        return cls(db_path=path, disabled=False, conn=conn, fts_capable=fts_capable)

    def _maybe_checkpoint_locked(self) -> None:
        """Issue ``PRAGMA wal_checkpoint(TRUNCATE)`` every N upserts (#32).

        Caller must hold ``self._lock``. Errors are logged at DEBUG (the
        checkpoint is a housekeeping pragma, not a correctness step) so a
        transient failure cannot stall the write path.
        """
        if self._conn is None:
            return
        self._upserts_since_checkpoint += 1
        if self._upserts_since_checkpoint < _CHECKPOINT_EVERY_N_UPSERTS:
            return
        self._upserts_since_checkpoint = 0
        try:
            self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error as e:
            log.debug('LocalIndex.wal_checkpoint failed: %s', e)

    def upsert(self, obs: Observation) -> None:
        """Insert or replace ``obs`` in the index, clearing rebuild shadow state.

        ADR-0021: also writes backward supersedes edges and syncs obs_fts.
        """
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
                # Write backward supersedes edges (obs_index.superseded_by).
                for superseded_id in obs.supersedes:
                    self._conn.execute(_SET_SUPERSEDED_BY_SQL, (obs.observation_id, superseded_id))
                # Sync FTS5 (delete old entry then re-insert with fresh content).
                if self._fts_capable:
                    self._conn.execute(_FTS_DELETE_SQL, (obs.observation_id,))
                    self._conn.execute(_FTS_INSERT_SQL, _fts_row(obs))
                self._conn.commit()
                self._maybe_checkpoint_locked()
            except sqlite3.Error as e:
                log.warning('LocalIndex.upsert failed for %s: %s', obs.observation_id, e)

    def mark_deleted(self, observation_id: str, deleted_at: str) -> None:
        """Stamp ``deleted_at`` on the row matching ``observation_id``.

        If no row exists yet (tombstone arrived before observation), the
        UPDATE is a silent no-op. Phase 4 subscriber will reconcile via
        rebuild — this matches the "zenoh is truth" policy in TASK-131 §3.4.
        FTS entries are left in place; the JOIN filter in _search_via_fts
        excludes deleted rows naturally.
        """
        if self._disabled or self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(_MARK_DELETED_SQL, (deleted_at, observation_id))
                self._conn.commit()
            except sqlite3.Error as e:
                log.warning('LocalIndex.mark_deleted failed for %s: %s', observation_id, e)

    def mark_shadowed_missing(self, observation_id: str, shadowed_at: str) -> None:
        """Hide a live row because rebuild did not observe it in Zenoh.

        This state is distinct from tombstones: it hides the row from normal
        search but does not participate in retention GC and is cleared by any
        future observation upsert.
        """
        if self._disabled or self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(_MARK_SHADOWED_SQL, (shadowed_at, observation_id))
                self._conn.commit()
            except sqlite3.Error as e:
                log.warning('LocalIndex.mark_shadowed_missing failed for %s: %s', observation_id, e)

    def search_by_project(self, project: str, limit: int = 50) -> list[Observation]:
        """Return live observations for ``project`` ordered by created_at DESC.

        Phase 2 entry point retained for backward compatibility; Phase 3
        callers should prefer :meth:`search` for richer filters.
        """
        return self.search(project=project, limit=limit)

    def _search_via_fts(
        self,
        *,
        project: str,
        agent_family: str,
        client_id: str,
        pc_id: str,
        session_id: str,
        query: str,
        since_iso: str,
        until_iso: str,
        cursor_observation_id: str,
        limit: int,
        include_deleted: bool,
        include_superseded: bool,
    ) -> list[Observation]:
        """FTS5-backed search with bm25 ranking (ADR-0021 Scope A).

        Raises ``sqlite3.Error`` on failure so the caller can fall back to
        the LIKE path.
        """
        where: list[str] = ['obs_fts MATCH ?']
        params: list[object] = [query]

        if not include_deleted:
            where.append('o.deleted_at IS NULL')
            where.append('o.shadowed_at IS NULL')
        if not include_superseded:
            # Existence-based: hide only when the superseding obs is live.
            where.append(
                '(o.superseded_by IS NULL OR NOT EXISTS ('
                'SELECT 1 FROM obs_index AS _s '
                'WHERE _s.observation_id = o.superseded_by '
                'AND _s.deleted_at IS NULL AND _s.shadowed_at IS NULL'
                '))'
            )
        if project:
            where.append('o.project = ?')
            params.append(project)
        if agent_family:
            where.append("json_extract(o.payload_json, '$.agent_family') = ?")
            params.append(agent_family)
        if client_id:
            where.append("json_extract(o.payload_json, '$.client_id') = ?")
            params.append(client_id)
        if pc_id:
            where.append("json_extract(o.payload_json, '$.pc_id') = ?")
            params.append(pc_id)
        if session_id:
            where.append("json_extract(o.payload_json, '$.session_id') = ?")
            params.append(session_id)
        if since_iso:
            where.append('o.created_at >= ?')
            params.append(since_iso)
        if until_iso and cursor_observation_id:
            where.append('(o.created_at < ? OR (o.created_at = ? AND o.observation_id < ?))')
            params.extend([until_iso, until_iso, cursor_observation_id])
        elif until_iso:
            where.append('o.created_at <= ?')
            params.append(until_iso)

        sql = (
            'SELECT o.payload_json FROM obs_fts '
            'JOIN obs_index AS o ON o.observation_id = obs_fts.observation_id '
            'WHERE ' + ' AND '.join(where) +
            ' ORDER BY bm25(obs_fts) ASC, o.created_at DESC, o.observation_id DESC LIMIT ?'
        )
        params.append(max(1, limit))

        assert self._conn is not None  # caller checks
        rows = self._conn.execute(sql, params).fetchall()

        out: list[Observation] = []
        for (payload,) in rows:
            try:
                out.append(Observation.from_json(payload))
            except Exception as e:  # noqa: BLE001
                log.warning('LocalIndex skip malformed payload: %s', e)
        return out

    def search(
        self,
        *,
        project: str = '',
        agent_family: str = '',
        client_id: str = '',
        pc_id: str = '',
        session_id: str = '',
        query: str = '',
        since_iso: str = '',
        until_iso: str = '',
        cursor_observation_id: str = '',
        limit: int = 50,
        include_deleted: bool = False,
        include_superseded: bool = False,
    ) -> list[Observation]:
        """SQL-side search returning Observations ordered by created_at DESC.

        ADR-0021: when ``query`` has ≥ 3 characters and FTS5 is available,
        results are ranked by bm25 (most relevant first), then by
        ``created_at DESC`` as a tiebreaker. Shorter queries fall back to the
        LIKE path automatically; FTS errors also fall through to LIKE.

        ``include_superseded=False`` (default) hides observations whose
        ``superseded_by`` pointer references a live (non-deleted, non-shadowed)
        observation in the index. When the superseding observation is itself
        deleted or shadowed, the older entry becomes visible again
        (existence-based, per ADR-0021 §B).

        All other filter semantics are unchanged from the pre-ADR-0021 LIKE path.
        ``include_deleted=True`` returns both tombstoned and rebuild-shadowed
        rows. ``since_iso`` / ``until_iso`` are compared lexicographically
        against ``created_at``. When ``cursor_observation_id`` is paired with
        ``until_iso`` the bound switches to strict-tuple semantics for bulk-
        delete cursor pagination (#66).
        """
        if self._disabled or self._conn is None:
            return []

        # Route to FTS5 for queries of 3+ characters when the table is available.
        if self._fts_capable and query and len(query) >= 3:
            with self._lock:
                try:
                    return self._search_via_fts(
                        project=project,
                        agent_family=agent_family,
                        client_id=client_id,
                        pc_id=pc_id,
                        session_id=session_id,
                        query=query,
                        since_iso=since_iso,
                        until_iso=until_iso,
                        cursor_observation_id=cursor_observation_id,
                        limit=limit,
                        include_deleted=include_deleted,
                        include_superseded=include_superseded,
                    )
                except sqlite3.Error as e:
                    log.warning('LocalIndex FTS search failed, falling back to LIKE: %s', e)
                    # Fall through to LIKE path below.

        # LIKE path: used for short queries, FTS-disabled builds, or on FTS error.
        where: list[str] = []
        params: list[object] = []
        if not include_deleted:
            where.append('deleted_at IS NULL')
            where.append('shadowed_at IS NULL')
        if not include_superseded:
            where.append(
                '(superseded_by IS NULL OR NOT EXISTS ('
                'SELECT 1 FROM obs_index AS _s '
                'WHERE _s.observation_id = obs_index.superseded_by '
                'AND _s.deleted_at IS NULL AND _s.shadowed_at IS NULL'
                '))'
            )
        if project:
            where.append('project = ?')
            params.append(project)
        if agent_family:
            where.append("json_extract(payload_json, '$.agent_family') = ?")
            params.append(agent_family)
        if client_id:
            where.append("json_extract(payload_json, '$.client_id') = ?")
            params.append(client_id)
        if pc_id:
            where.append("json_extract(payload_json, '$.pc_id') = ?")
            params.append(pc_id)
        if session_id:
            where.append("json_extract(payload_json, '$.session_id') = ?")
            params.append(session_id)
        if since_iso:
            where.append('created_at >= ?')
            params.append(since_iso)
        if until_iso and cursor_observation_id:
            # Strict-tuple cursor for bulk-delete pagination (#66): exclude
            # the boundary row itself so iteration walks past timestamp
            # ties even when their count exceeds ``limit``.
            where.append('(created_at < ? OR (created_at = ? AND observation_id < ?))')
            params.extend([until_iso, until_iso, cursor_observation_id])
        elif until_iso:
            where.append('created_at <= ?')
            params.append(until_iso)
        if query:
            # Case-insensitive substring against the full payload (content /
            # project / tags / subject / summary). LIKE is fast enough at PoC
            # scale; FTS5 is the natural upgrade for 3+ char queries above.
            where.append('LOWER(payload_json) LIKE ?')
            params.append(f'%{query.lower()}%')

        sql = 'SELECT payload_json FROM obs_index'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        # ``observation_id`` is the PRIMARY KEY, so adding it as a secondary
        # sort key gives a total, stable order for cursor pagination over
        # rows that share the same ``created_at`` (#66).
        sql += ' ORDER BY created_at DESC, observation_id DESC LIMIT ?'
        params.append(max(1, limit))

        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                log.warning('LocalIndex.search failed: %s', e)
                return []
        out: list[Observation] = []
        for (payload,) in rows:
            try:
                out.append(Observation.from_json(payload))
            except Exception as e:  # noqa: BLE001 — malformed payload should not crash search
                log.warning('LocalIndex skip malformed payload: %s', e)
        return out

    def find_superseded_by(self, observation_id: str) -> str | None:
        """Return the id of the live observation that supersedes ``observation_id``, or None.

        Used by ``get_memory`` to show the forward chain link so agents that
        land on a superseded entry can follow 1 hop to the latest version.
        Returns None when the index is disabled, the row doesn't exist, or
        ``superseded_by`` is NULL / points to a non-live observation.
        """
        if self._disabled or self._conn is None:
            return None
        sql = (
            'SELECT superseded_by FROM obs_index WHERE observation_id = ? '
            'AND deleted_at IS NULL AND shadowed_at IS NULL'
        )
        with self._lock:
            try:
                row = self._conn.execute(sql, (observation_id,)).fetchone()
            except sqlite3.Error as e:
                log.warning('LocalIndex.find_superseded_by failed for %s: %s', observation_id, e)
                return None
        if row is None or row[0] is None:
            return None
        return row[0]

    def physical_delete(self, observation_id: str) -> None:
        """Hard-DELETE the row matching ``observation_id``. No-op on miss.

        Called by ``store.physical_delete_observation`` after the Zenoh
        key delete so the index does not leak rows that no longer exist
        upstream. The gc retention sweep also routes through here.
        ADR-0021: also purges the corresponding obs_fts entry.
        """
        if self._disabled or self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute('DELETE FROM obs_index WHERE observation_id = ?', (observation_id,))
                if self._fts_capable:
                    self._conn.execute(_FTS_DELETE_SQL, (observation_id,))
                self._conn.commit()
            except sqlite3.Error as e:
                log.warning('LocalIndex.physical_delete failed for %s: %s', observation_id, e)

    def list_tombstoned_obs_in_project(
        self,
        project: str,
        cutoff_iso: str,
    ) -> list[tuple[str, str]]:
        """Return ``(observation_id, payload_json)`` for project-scoped tombs older than cutoff.

        Drives the project-scoped gc fast path (#32): O(N) on the project
        subset via the ``(project, created_at)`` secondary index plus a
        deleted_at scan, instead of the legacy ``mem/tomb/**`` Zenoh full
        scan that paid O(M) on the global tombstone count. Returns the
        payload alongside the id so the caller can derive the exact key
        expression for surgical deletes without another Zenoh round-trip.
        """
        if self._disabled or self._conn is None:
            return []
        sql = (
            'SELECT observation_id, payload_json '
            'FROM obs_index '
            'WHERE project = ? AND deleted_at IS NOT NULL AND deleted_at < ?'
        )
        with self._lock:
            try:
                rows = self._conn.execute(sql, (project, cutoff_iso)).fetchall()
            except sqlite3.Error as e:
                log.warning('list_tombstoned_obs_in_project failed: %s', e)
                return []
        return [(row[0], row[1]) for row in rows]

    def list_expired_shadowed_obs(
        self,
        cutoff_iso: str,
        project: str = '',
    ) -> list[str]:
        """Return observation_ids whose ``shadowed_at`` predates ``cutoff_iso``.

        Drives the shadow retention sweep (Issue #70). Rows are scoped by
        ``project`` when non-empty to mirror the tombstone sweep behavior.
        Returns only ids — shadow rows have no remaining Zenoh state to
        coordinate with, so a payload round-trip is unnecessary.
        """
        if self._disabled or self._conn is None:
            return []
        sql = (
            'SELECT observation_id FROM obs_index '
            'WHERE shadowed_at IS NOT NULL AND deleted_at IS NULL AND shadowed_at < ?'
        )
        params: list[object] = [cutoff_iso]
        if project:
            sql += ' AND project = ?'
            params.append(project)
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                log.warning('list_expired_shadowed_obs failed: %s', e)
                return []
        return [row[0] for row in rows]

    def find_by_id(self, observation_id: str, include_deleted: bool = False) -> Observation | None:
        """Return the observation with id ``observation_id`` or None.

        Phase 3 caller is ``store.find_observation_by_id``. ``include_deleted``
        defaults to False so the lookup matches ``search`` semantics; the
        gc / delete paths in store call this with ``include_deleted=True``
        when they need to locate a tombstoned obs to physical-delete. The
        flag also includes rebuild-shadowed rows.
        """
        if self._disabled or self._conn is None:
            return None
        sql = 'SELECT payload_json FROM obs_index WHERE observation_id = ?'
        if not include_deleted:
            sql += ' AND deleted_at IS NULL AND shadowed_at IS NULL'
        with self._lock:
            try:
                row = self._conn.execute(sql, (observation_id,)).fetchone()
            except sqlite3.Error as e:
                log.warning('LocalIndex.find_by_id failed for %s: %s', observation_id, e)
                return None
        if row is None:
            return None
        try:
            return Observation.from_json(row[0])
        except Exception as e:  # noqa: BLE001
            log.warning('LocalIndex.find_by_id malformed payload for %s: %s', observation_id, e)
            return None

    def row_count(self) -> int:
        """Return the total row count, including tombstoned and shadowed rows."""
        if self._disabled or self._conn is None:
            return 0
        with self._lock:
            try:
                (count,) = self._conn.execute('SELECT COUNT(*) FROM obs_index').fetchone()
            except sqlite3.Error as e:
                log.warning('LocalIndex.row_count failed: %s', e)
                return 0
        return int(count)

    def distinct_projects(self) -> list[str]:
        """Return distinct non-empty ``project`` values present in the index.

        Used by shell completion (Issue #76) to suggest live ``--project``
        values. ``project`` is a real column, so the query is O(N) on a
        secondary index and safe to call from a tab-completion subshell.
        """
        if self._disabled or self._conn is None:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT project FROM obs_index WHERE project IS NOT NULL AND project != ''"
                ).fetchall()
            except sqlite3.Error as e:
                log.warning('LocalIndex.distinct_projects failed: %s', e)
                return []
        return sorted({row[0] for row in rows if row[0]})

    def distinct_pc_ids(self, scan_limit: int = 1000) -> list[str]:
        """Return distinct ``pc_id`` values from the ``scan_limit`` most-recent rows.

        ``pc_id`` lives inside ``payload_json``, so a full table scan would
        defeat completion latency budgets. Scanning the most-recent N rows
        keeps the result bounded while still surfacing currently-active
        peers (the typical completion target). Used by shell completion
        (Issue #76).
        """
        if self._disabled or self._conn is None:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT json_extract(payload_json, '$.pc_id') "
                    'FROM obs_index ORDER BY created_at DESC LIMIT ?',
                    (max(1, scan_limit),),
                ).fetchall()
            except sqlite3.Error as e:
                log.warning('LocalIndex.distinct_pc_ids failed: %s', e)
                return []
        return sorted({row[0] for row in rows if row[0]})

    def visibility_counts(self) -> VisibilityCounts:
        """Return live / tombstoned / shadowed row counts for status reporting."""
        if self._disabled or self._conn is None:
            return VisibilityCounts()
        sql = (
            'SELECT '
            'SUM(CASE WHEN deleted_at IS NULL AND shadowed_at IS NULL THEN 1 ELSE 0 END), '
            'SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END), '
            'SUM(CASE WHEN deleted_at IS NULL AND shadowed_at IS NOT NULL THEN 1 ELSE 0 END) '
            'FROM obs_index'
        )
        with self._lock:
            try:
                row = self._conn.execute(sql).fetchone()
            except sqlite3.Error as e:
                log.warning('LocalIndex.visibility_counts failed: %s', e)
                return VisibilityCounts()
        if row is None:
            return VisibilityCounts()
        return VisibilityCounts(live=int(row[0] or 0), tombstoned=int(row[1] or 0), shadowed=int(row[2] or 0))

    def rebuild_from_zenoh(self, session: object) -> RebuildStats:
        """Scan zenoh-rocksdb for all observations/tombstones and reconcile SQLite index.

        Idempotent: safe to call on every startup. Returns counts of rows
        added, tombstone marks applied, rebuild-shadow marks applied, and
        unchanged rows.

        ADR-0019 Phase A: the scan selectors cover both the legacy
        ``mem/{obs,tomb}/**`` namespace and the visibility-tiered ones
        (``mem/mesh/...``, ``mem/user/{id}/...``, ``mem/team/{id}/...``),
        so rows written by newer peers are indexed too.

        ADR-0021: writes FTS5 entries and backward supersedes edges for all
        upserted observations inside the same transaction.
        """
        if self._disabled or self._conn is None:
            return RebuildStats()

        obs_list: list[Observation] = []
        for reply in session.get(OBS_READ_KEY_EXPR, timeout=30.0):  # type: ignore[attr-defined]
            if reply.ok:
                # The broadened selector can match non-canonical keys; never
                # ingest a payload whose key is off-shape or disagrees with
                # the payload id (Codex review on PR #177).
                key_id = obs_id_from_key(str(reply.ok.key_expr))
                if key_id is None:
                    log.debug('rebuild_from_zenoh skip non-canonical obs key: %s', reply.ok.key_expr)
                    continue
                try:
                    obs = Observation.from_json(reply.ok.payload.to_string())
                except Exception as e:  # noqa: BLE001
                    log.warning('rebuild_from_zenoh skip malformed obs: %s', e)
                    continue
                if obs.observation_id != key_id:
                    log.warning(
                        'rebuild_from_zenoh skip key/payload id mismatch: key=%s payload_id=%s',
                        reply.ok.key_expr,
                        obs.observation_id,
                    )
                    continue
                obs_list.append(obs)

        tomb_ids: dict[str, str] = {}
        for reply in session.get(TOMB_READ_KEY_EXPR, timeout=30.0):  # type: ignore[attr-defined]
            if reply.ok:
                key_id = obs_id_from_key(str(reply.ok.key_expr))
                if key_id is None:
                    log.debug('rebuild_from_zenoh skip non-canonical tomb key: %s', reply.ok.key_expr)
                    continue
                try:
                    tomb = Tombstone.from_json(reply.ok.payload.to_string())
                except Exception as e:  # noqa: BLE001
                    log.warning('rebuild_from_zenoh skip malformed tomb: %s', e)
                    continue
                if tomb.observation_id != key_id:
                    log.warning(
                        'rebuild_from_zenoh skip key/payload id mismatch: key=%s payload_id=%s',
                        reply.ok.key_expr,
                        tomb.observation_id,
                    )
                    continue
                tomb_ids[tomb.observation_id] = tomb.deleted_at

        added = 0
        marked_deleted = 0
        shadowed = 0
        unchanged = 0

        with self._lock:
            try:
                existing: dict[str, tuple[str | None, str | None, str]] = {
                    row[0]: (row[1], row[2], row[3])
                    for row in self._conn.execute(
                        'SELECT observation_id, deleted_at, shadowed_at, payload_json FROM obs_index'
                    ).fetchall()
                }

                upsert_rows = []
                upsert_obs: list[Observation] = []
                seen_obs_ids: set[str] = set()
                for obs in obs_list:
                    seen_obs_ids.add(obs.observation_id)
                    payload_json = obs.to_json()
                    current = existing.get(obs.observation_id)
                    if current is None:
                        upsert_rows.append(
                            (
                                obs.observation_id,
                                obs.project,
                                obs.created_at,
                                obs.memory_type,
                                obs.importance,
                                obs.subject,
                                obs.summary,
                                payload_json,
                            )
                        )
                        upsert_obs.append(obs)
                        added += 1
                        continue
                    current_deleted, current_shadowed_at, current_payload_json = current
                    if current_shadowed_at is not None or current_payload_json != payload_json:
                        upsert_rows.append(
                            (
                                obs.observation_id,
                                obs.project,
                                obs.created_at,
                                obs.memory_type,
                                obs.importance,
                                obs.subject,
                                obs.summary,
                                payload_json,
                            )
                        )
                        upsert_obs.append(obs)
                    else:
                        unchanged += 1

                mark_rows = []
                for obs_id, del_at in tomb_ids.items():
                    # Rebuild can only update rows it has locally or is about
                    # to upsert from the observation scan; orphan tombstones are
                    # left to a later replay or rebuild where the obs appears.
                    if obs_id not in existing and obs_id not in seen_obs_ids:
                        continue
                    current_deleted = existing.get(obs_id, (None, None, ''))[0]
                    if current_deleted is not None:
                        continue
                    mark_rows.append((del_at, obs_id))
                    marked_deleted += 1

                shadow_rows = []
                shadowed_at = _shadow_now_iso()
                for obs_id, (deleted_at, existing_shadowed_at, _payload_json) in existing.items():
                    if deleted_at is None and existing_shadowed_at is None and obs_id not in seen_obs_ids:
                        shadow_rows.append((shadowed_at, obs_id))
                        shadowed += 1

                # Prepare FTS and supersedes edge data for upserted obs.
                fts_rows = [_fts_row(obs) for obs in upsert_obs] if self._fts_capable else []
                supersedes_updates: list[tuple[str, str]] = []
                for obs in obs_list:
                    for superseded_id in obs.supersedes:
                        supersedes_updates.append((obs.observation_id, superseded_id))

                self._conn.execute('BEGIN')
                if upsert_rows:
                    self._conn.executemany(_UPSERT_SQL, upsert_rows)
                if supersedes_updates:
                    self._conn.executemany(_SET_SUPERSEDED_BY_SQL, supersedes_updates)
                if mark_rows:
                    self._conn.executemany(_MARK_DELETED_SQL, mark_rows)
                if shadow_rows:
                    self._conn.executemany(_MARK_SHADOWED_SQL, shadow_rows)
                if fts_rows:
                    for (obs_id, *_) in fts_rows:
                        self._conn.execute(_FTS_DELETE_SQL, (obs_id,))
                    self._conn.executemany(_FTS_INSERT_SQL, fts_rows)
                self._conn.execute('COMMIT')
            except sqlite3.Error as e:
                try:
                    self._conn.execute('ROLLBACK')
                except Exception:  # noqa: BLE001
                    pass
                log.warning('rebuild_from_zenoh transaction failed: %s', e)
                raise

        return RebuildStats(added=added, marked_deleted=marked_deleted, shadowed=shadowed, unchanged=unchanged)

    def close(self) -> None:
        """Close the underlying connection. Safe to call multiple times.

        Issues a final ``PRAGMA wal_checkpoint(TRUNCATE)`` so the WAL does
        not survive the process at full size on disk (#32).
        """
        if self._conn is None:
            return
        with self._lock:
            try:
                try:
                    self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                except sqlite3.Error as e:
                    log.debug('LocalIndex.close wal_checkpoint failed: %s', e)
                self._conn.close()
            except sqlite3.Error as e:
                log.warning('LocalIndex.close failed: %s', e)
            finally:
                self._conn = None
                self._upserts_since_checkpoint = 0

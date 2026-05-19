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
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
import sqlite3
import threading

from .identity import state_dir
from .models import Observation
from .models import Tombstone

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Issue #32: long-running processes (mesh-mem-mcp) keep the index connection
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
  shadowed_at TEXT
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
    'payload_json=excluded.payload_json, '
    'shadowed_at=NULL'
)

_MARK_DELETED_SQL = 'UPDATE obs_index SET deleted_at = ?, shadowed_at = NULL WHERE observation_id = ?'
_MARK_SHADOWED_SQL = (
    'UPDATE obs_index SET shadowed_at = COALESCE(shadowed_at, ?) WHERE observation_id = ? AND deleted_at IS NULL'
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
    _ensure_schema(conn)
    conn.commit()
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply schema creation and forward-only migrations."""
    conn.executescript(_SCHEMA_SQL)
    cols = {row[1] for row in conn.execute('PRAGMA table_info(obs_index)').fetchall()}
    if 'shadowed_at' not in cols:
        conn.execute('ALTER TABLE obs_index ADD COLUMN shadowed_at TEXT')
    conn.execute('DELETE FROM schema_version')
    conn.execute('INSERT INTO schema_version(version) VALUES (?)', (SCHEMA_VERSION,))


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
        # Counter for the periodic ``PRAGMA wal_checkpoint(TRUNCATE)`` (#32).
        # Reset every checkpoint and on close.
        self._upserts_since_checkpoint = 0

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
        """Insert or replace ``obs`` in the index, clearing rebuild shadow state."""
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
                self._maybe_checkpoint_locked()
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
        limit: int = 50,
        include_deleted: bool = False,
    ) -> list[Observation]:
        """SQL-side search returning Observations ordered by created_at DESC.

        Filters compose with AND. Empty-string filters are skipped (matches
        ``store.search_observations`` semantics). ``query`` is a case-
        insensitive substring match against ``payload_json`` — this covers
        content / project / tags / subject / summary uniformly. False
        positives on identity-field substrings are accepted for PoC; the
        existing Zenoh-side semantic was content/project/tags only, so this
        is a slight broadening that no current test depends on negatively.

        ``include_deleted=True`` returns both tombstoned and rebuild-shadowed
        rows. The name is historical but the behavior is intentionally "show
        hidden rows" rather than only "show tombstoned rows".

        ``since_iso`` is compared lexicographically against ``created_at``;
        both are produced as 'Z'-suffixed UTC ISO 8601 strings by
        :meth:`mesh_mem.models._utc_now_iso`, so lex order matches time
        order. Rows whose created_at cannot be lex-compared (legacy bad
        writes) will sort, possibly incorrectly — same caveat as the
        existing Zenoh path.
        """
        if self._disabled or self._conn is None:
            return []
        where: list[str] = []
        params: list[object] = []
        if not include_deleted:
            where.append('deleted_at IS NULL')
            where.append('shadowed_at IS NULL')
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
        if query:
            # Case-insensitive substring against the full payload (content /
            # project / tags / subject / summary). LIKE is fast enough at PoC
            # scale; FTS5 is the natural upgrade if profiling demands it.
            where.append('LOWER(payload_json) LIKE ?')
            params.append(f'%{query.lower()}%')

        sql = 'SELECT payload_json FROM obs_index'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY created_at DESC LIMIT ?'
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

    def physical_delete(self, observation_id: str) -> None:
        """Hard-DELETE the row matching ``observation_id``. No-op on miss.

        Called by ``store.physical_delete_observation`` after the Zenoh
        key delete so the index does not leak rows that no longer exist
        upstream. The gc retention sweep also routes through here.
        """
        if self._disabled or self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute('DELETE FROM obs_index WHERE observation_id = ?', (observation_id,))
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
        """
        if self._disabled or self._conn is None:
            return RebuildStats()

        obs_list: list[Observation] = []
        for reply in session.get('mem/obs/**', timeout=30.0):  # type: ignore[attr-defined]
            if reply.ok:
                try:
                    obs_list.append(Observation.from_json(reply.ok.payload.to_string()))
                except Exception as e:  # noqa: BLE001
                    log.warning('rebuild_from_zenoh skip malformed obs: %s', e)

        tomb_ids: dict[str, str] = {}
        for reply in session.get('mem/tomb/**', timeout=30.0):  # type: ignore[attr-defined]
            if reply.ok:
                try:
                    tomb = Tombstone.from_json(reply.ok.payload.to_string())
                    tomb_ids[tomb.observation_id] = tomb.deleted_at
                except Exception as e:  # noqa: BLE001
                    log.warning('rebuild_from_zenoh skip malformed tomb: %s', e)

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

                self._conn.execute('BEGIN')
                if upsert_rows:
                    self._conn.executemany(_UPSERT_SQL, upsert_rows)
                if mark_rows:
                    self._conn.executemany(_MARK_DELETED_SQL, mark_rows)
                if shadow_rows:
                    self._conn.executemany(_MARK_SHADOWED_SQL, shadow_rows)
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

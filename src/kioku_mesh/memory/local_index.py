"""Local SQLite sidecar index for observation metadata.

Issue #7 / TASK-131 plan B. Zenoh-rocksdb stays the source of truth; this
file maintains a per-process SQLite mirror that ``store.search_observations``
reads from since Phase 3. ``put_observation`` upserts into ``obs_index``,
``put_tombstone`` stamps ``deleted_at`` (no row delete). The Zenoh full-
scan path stays available behind ``KIOKU_MESH_DISABLE_INDEX=1`` as a fallback.

Failure semantics: SQLite is a sidecar. Zenoh write success is the contract;
SQLite errors are logged and swallowed so a corrupt index file cannot turn
a working put into a failure. The Phase 4 rebuild path (not yet implemented)
will repopulate the index from Zenoh on demand.

Disable: set ``KIOKU_MESH_DISABLE_INDEX=1`` (or ``KIOKU_MESH_INDEX_DB=:memory:``
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
from pathlib import Path
import sqlite3
import threading
from typing import Iterator

from kioku_mesh.core._env_compat import get_env

from ..core.identity import state_dir
from ..core.keyspace import obs_id_from_key
from ..core.keyspace import OBS_READ_KEY_EXPR
from ..core.keyspace import TOMB_READ_KEY_EXPR
from ..core.models import Observation
from ..core.models import Tombstone

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

# ADR-0021: FTS5 capability levels (detected once per connection).
_FTS_CAP_TRIGRAM = 'trigram'  # FTS5 with trigram tokenizer (supports Japanese substring match)
_FTS_CAP_FTS5 = 'fts5'  # FTS5 without trigram
_FTS_CAP_LIKE = 'like'  # LIKE fallback (no FTS5)

# Issue #32: long-running processes (kioku-mesh-mcp) keep the index connection
# open indefinitely, which blocks SQLite's automatic WAL checkpoint from
# completing the truncate phase. The WAL therefore grows unbounded — observed
# 130 MB on a host that had been writing for weeks. Issue an explicit
# ``PRAGMA wal_checkpoint(TRUNCATE)`` every N upserts and once at close so
# the WAL stays bounded without introducing a checkpoint thread.
_CHECKPOINT_EVERY_N_UPSERTS = 256

# Issue #218: valid values for the search_mode parameter.
SEARCH_MODES = frozenset({'and', 'or', 'and_or'})


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

# ADR-0021: FTS5 lockstep sync SQL.
_FTS_UPSERT_SQL = (
    'INSERT OR REPLACE INTO obs_fts(observation_id, content, subject, summary, tags, project) '
    'VALUES (?, ?, ?, ?, ?, ?)'
)
_FTS_DELETE_SQL = 'DELETE FROM obs_fts WHERE observation_id = ?'
_PathLike = str | Path


def _quote_fts_term(term: str) -> str:
    """Return ``term`` as an FTS5 literal phrase."""
    return '"' + term.replace('"', '""') + '"'


def _escape_like(term: str) -> str:
    """Escape LIKE wildcard chars so term is treated as a literal substring."""
    term = term.replace('\\', '\\\\')
    term = term.replace('%', '\\%')
    term = term.replace('_', '\\_')
    return term


def _validate_search_mode(search_mode: str) -> str:
    if search_mode not in SEARCH_MODES:
        raise ValueError(f"search_mode must be one of 'and', 'or', 'and_or', got {search_mode!r}")
    return search_mode


def _disabled_via_env() -> bool:
    return get_env('KIOKU_MESH_DISABLE_INDEX', '').strip() == '1'


def _shadow_now_iso() -> str:
    """Return the local timestamp used to mark rebuild-shadowed rows."""
    from datetime import datetime
    from datetime import timezone

    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _detect_fts_cap(conn: sqlite3.Connection) -> str:
    """Detect FTS5 and trigram tokenizer availability on ``conn``.

    Probes by attempting to create/drop a temporary virtual table.
    Returns one of _FTS_CAP_TRIGRAM / _FTS_CAP_FTS5 / _FTS_CAP_LIKE.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_cap_probe USING fts5(x, tokenize='trigram')")
        conn.execute('DROP TABLE IF EXISTS _fts_cap_probe')
        return _FTS_CAP_TRIGRAM
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS _fts_cap_probe USING fts5(x)')
        conn.execute('DROP TABLE IF EXISTS _fts_cap_probe')
        return _FTS_CAP_FTS5
    except sqlite3.OperationalError:
        return _FTS_CAP_LIKE


def _resolve_db_path() -> str:
    """Resolve the SQLite DB path from env or fall back to state_dir().

    Returns the literal ``:memory:`` if explicitly requested so callers can
    short-circuit to an in-process DB without touching disk.
    """
    override = get_env('KIOKU_MESH_INDEX_DB', '').strip()
    if override:
        return override
    return str(state_dir() / 'index.db')


def _open_connection(path: str) -> tuple[sqlite3.Connection, str]:
    """Open a SQLite connection at ``path``, applying PRAGMA + schema.

    ``check_same_thread=False`` because put_observation may run on a
    different thread than the MCP stdio handler (and a future Phase 4
    subscriber thread). Method-level locking in :class:`LocalIndex`
    serializes access.

    Returns ``(conn, fts_cap)`` where ``fts_cap`` is one of the ``_FTS_CAP_*``
    constants detected during ``_ensure_schema``.
    """
    if path != ':memory:':
        parent = Path(path).parent
        if str(parent) and parent != Path():
            parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    fts_cap = _ensure_schema(conn)
    conn.commit()
    return conn, fts_cap


def _ensure_schema(conn: sqlite3.Connection) -> str:
    """Apply schema creation and forward-only migrations.

    Returns the detected FTS capability (one of ``_FTS_CAP_*``) so callers
    can store it without a second probe.

    ADR-0019 (visibility) 実装後は visibility scope と search filter の
    合成が必要。LocalIndex.search の filter 合成ロジックを参照のこと。
    """
    conn.executescript(_SCHEMA_SQL)
    cols = {row[1] for row in conn.execute('PRAGMA table_info(obs_index)').fetchall()}
    if 'shadowed_at' not in cols:
        conn.execute('ALTER TABLE obs_index ADD COLUMN shadowed_at TEXT')
    # ADR-0021: superseded_by tracks which observation superseded this row.
    superseded_by_is_new = 'superseded_by' not in cols
    if superseded_by_is_new:
        conn.execute('ALTER TABLE obs_index ADD COLUMN superseded_by TEXT')
        # R5: backfill superseded_by from payload_json.supersedes for existing rows.
        import json as _json  # noqa: PLC0415

        rows_with_supersedes = conn.execute(
            'SELECT observation_id, payload_json FROM obs_index '
            "WHERE json_extract(payload_json, '$.supersedes') IS NOT NULL"
        ).fetchall()
        for superseder_id, payload_json_str in rows_with_supersedes:
            try:
                payload = _json.loads(payload_json_str)
                supersedes_list = payload.get('supersedes') or []
                if supersedes_list:
                    placeholders = ','.join('?' for _ in supersedes_list)
                    conn.execute(
                        f'UPDATE obs_index SET superseded_by = ? '
                        f'WHERE observation_id IN ({placeholders}) AND superseded_by IS NULL',
                        [superseder_id, *supersedes_list],
                    )
            except Exception:  # noqa: BLE001
                pass
    # ADR-0021: FTS5 virtual table for full-text search (content/subject/summary/tags/project).
    fts_cap = _detect_fts_cap(conn)
    if fts_cap != _FTS_CAP_LIKE:
        # R3a: drop obs_fts if it was created without tags/project columns.
        fts_sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='obs_fts'").fetchone()
        if fts_sql_row and 'tags' not in (fts_sql_row[0] or ''):
            conn.execute('DROP TABLE IF EXISTS obs_fts')
        if fts_cap == _FTS_CAP_TRIGRAM:
            conn.execute(
                'CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5'
                "(observation_id UNINDEXED, content, subject, summary, tags, project, tokenize='trigram')"
            )
        else:
            conn.execute(
                'CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5'
                '(observation_id UNINDEXED, content, subject, summary, tags, project)'
            )
        _rebuild_fts_from_obs_index(conn)
    conn.execute('DELETE FROM schema_version')
    conn.execute('INSERT INTO schema_version(version) VALUES (?)', (SCHEMA_VERSION,))
    return fts_cap


def _rebuild_fts_from_obs_index(conn: sqlite3.Connection) -> None:
    """Rebuild ``obs_fts`` from live ``obs_index`` rows.

    ``obs_fts`` is a derived cache. A full delete/insert is deliberately
    idempotent and repairs old databases where the table is missing rows,
    contains stale rows, or was created before content/tags/project were
    populated correctly.

    Note on ``group_concat`` ordering: SQLite's ``json_each`` iterates array
    elements in index order, so ``group_concat(value, ' ')`` over
    ``json_each(payload_json, '$.tags')`` produces the same string as
    Python's ``' '.join(obs.tags)``. This relies on the current SQLite
    implementation; verify when upgrading SQLite major versions.
    """
    conn.execute('DELETE FROM obs_fts')
    conn.execute(
        'INSERT INTO obs_fts(observation_id, content, subject, summary, tags, project) '
        "SELECT observation_id, COALESCE(json_extract(payload_json, '$.content'), ''), "
        "COALESCE(subject, ''), COALESCE(summary, ''), "
        "COALESCE((SELECT group_concat(value, ' ') FROM json_each(obs_index.payload_json, '$.tags')), ''), "
        "COALESCE(project, '') "
        'FROM obs_index WHERE deleted_at IS NULL AND shadowed_at IS NULL'
    )


class LocalIndex:
    """SQLite-backed sidecar index. Thread-safe via a single lock per instance.

    Prefer ``connect`` in application code. Direct construction with a
    non-empty ``db_path`` also opens the file for compatibility with small
    scripts that instantiate ``LocalIndex(db_path=...)`` directly. A disabled
    instance (``KIOKU_MESH_DISABLE_INDEX=1``) holds no connection and short-
    circuits every method.
    """

    def __init__(
        self,
        db_path: _PathLike,
        disabled: bool = False,
        conn: sqlite3.Connection | None = None,
        fts_cap: str = _FTS_CAP_LIKE,
    ) -> None:
        path = str(db_path)
        if not disabled and conn is None and path:
            if _disabled_via_env():
                log.info('LocalIndex disabled via KIOKU_MESH_DISABLE_INDEX=1')
                disabled = True
            else:
                try:
                    conn, fts_cap = _open_connection(path)
                except (sqlite3.Error, OSError) as e:
                    log.warning('LocalIndex open failed (%s); falling back to disabled: %s', path, e)
                    disabled = True

        self._db_path = path
        self._disabled = disabled
        self._conn: sqlite3.Connection | None = conn
        self._fts_cap = fts_cap
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

        ``db_path=None`` resolves from env (``KIOKU_MESH_INDEX_DB``) or
        ``state_dir()/index.db``. The returned instance is always non-None
        so callers can chain method calls without checking for disable.
        On open failure the returned instance silently falls back to
        disabled mode so a corrupt index file cannot block puts.
        """
        if _disabled_via_env():
            log.info('LocalIndex disabled via KIOKU_MESH_DISABLE_INDEX=1')
            return cls(db_path='', disabled=True)
        path = db_path if db_path is not None else _resolve_db_path()
        try:
            conn, fts_cap = _open_connection(path)
        except (sqlite3.Error, OSError) as e:
            log.warning('LocalIndex open failed (%s); falling back to disabled: %s', path, e)
            return cls(db_path=path, disabled=True)
        return cls(db_path=path, disabled=False, conn=conn, fts_cap=fts_cap)

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
                # ADR-0021: update superseded_by on obs this one supersedes.
                if obs.supersedes:
                    placeholders = ','.join('?' for _ in obs.supersedes)
                    self._conn.execute(
                        f'UPDATE obs_index SET superseded_by = ? WHERE observation_id IN ({placeholders})',
                        [obs.observation_id, *obs.supersedes],
                    )
                # ADR-0021: FTS5 lockstep sync.
                if self._fts_cap != _FTS_CAP_LIKE:
                    self._conn.execute(
                        _FTS_UPSERT_SQL,
                        (
                            obs.observation_id,
                            obs.content,
                            obs.subject or '',
                            obs.summary or '',
                            ' '.join(obs.tags or []),
                            obs.project or '',
                        ),
                    )
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
                # ADR-0021: remove from FTS so deleted rows don't appear in FTS results.
                if self._fts_cap != _FTS_CAP_LIKE:
                    self._conn.execute(_FTS_DELETE_SQL, (observation_id,))
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
        memory_type: str = '',
        query: str = '',
        since_iso: str = '',
        until_iso: str = '',
        cursor_observation_id: str = '',
        limit: int = 50,
        include_deleted: bool = False,
        include_superseded: bool = False,
        search_mode: str = 'and',
    ) -> list[Observation]:
        """SQL-side search returning Observations ordered by created_at DESC.

        Filters compose with AND. Empty-string filters are skipped (matches
        ``store.search_observations`` semantics). ``query`` runs through a
        3-stage fallback (ADR-0021): space-separated terms use AND
        semantics by default, terms with 3+ chars use FTS5 when available,
        and shorter terms (or all terms when FTS5 is unavailable) use LIKE
        against ``payload_json``.

        ``search_mode`` controls how query terms are combined:
          'and' (default): all terms must match (existing behaviour).
          'or': any term matching is sufficient; base filters remain AND.
          'and_or': AND phase first; OR phase fills remaining limit slots.
        Base filters (deleted/shadowed/superseded/project/identity/since) are
        AND-combined in every mode. Unknown values raise ValueError.

        ``include_deleted=True`` returns both tombstoned and rebuild-shadowed
        rows. The name is historical but the behavior is intentionally "show
        hidden rows" rather than only "show tombstoned rows".

        ``include_superseded=False`` (default) hides rows whose superseder is
        still live in the index (existence-based, ADR-0021). Set to ``True``
        to include superseded rows.

        ``since_iso`` / ``until_iso`` are compared lexicographically against
        ``created_at``; both are produced as 'Z'-suffixed UTC ISO 8601
        strings by :meth:`mesh_mem.models._utc_now_iso`, so lex order
        matches time order. ``until_iso`` is inclusive (``<=``) by
        default to mirror ``since_iso`` semantics. When paired with
        ``cursor_observation_id`` the bound switches to the strict
        ``(created_at, observation_id) < (until_iso, cursor_observation_id)``
        tuple comparison used by bulk-delete cursor pagination so that
        ties on the boundary timestamp are walked correctly even when
        more rows share that timestamp than fit in a single page (#66).
        Rows whose created_at cannot be lex-compared (legacy bad writes)
        will sort, possibly incorrectly — same caveat as the existing
        Zenoh path.

        ADR-0019 (visibility) 実装後は visibility scope と search filter の
        合成が必要。LocalIndex.search の filter 合成ロジックを参照のこと。
        """
        if self._disabled or self._conn is None:
            return []
        search_mode = _validate_search_mode(search_mode)
        if search_mode == 'and_or':
            strict = self.search(
                project=project,
                agent_family=agent_family,
                client_id=client_id,
                pc_id=pc_id,
                session_id=session_id,
                memory_type=memory_type,
                query=query,
                since_iso=since_iso,
                until_iso=until_iso,
                cursor_observation_id=cursor_observation_id,
                limit=limit,
                include_deleted=include_deleted,
                include_superseded=include_superseded,
                search_mode='and',
            )
            if len(strict) >= limit:
                return strict[:limit]
            internal_or_limit = min(10_000, max(limit * 2, limit + 20))
            broad = self.search(
                project=project,
                agent_family=agent_family,
                client_id=client_id,
                pc_id=pc_id,
                session_id=session_id,
                memory_type=memory_type,
                query=query,
                since_iso=since_iso,
                until_iso=until_iso,
                cursor_observation_id=cursor_observation_id,
                limit=internal_or_limit,
                include_deleted=include_deleted,
                include_superseded=include_superseded,
                search_mode='or',
            )
            seen = {obs.observation_id for obs in strict}
            return [*strict, *(obs for obs in broad if obs.observation_id not in seen)][:limit]

        where: list[str] = []
        params: list[object] = []
        if not include_deleted:
            where.append('deleted_at IS NULL')
            where.append('shadowed_at IS NULL')
        # ADR-0021: existence-based supersedes filter. Superseder must be live
        # (not deleted AND not shadowed) to keep the superseded row hidden.
        if not include_superseded:
            where.append(
                '(superseded_by IS NULL OR superseded_by NOT IN '
                '(SELECT observation_id FROM obs_index WHERE deleted_at IS NULL AND shadowed_at IS NULL))'
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
        if memory_type:
            # ADR-0026: exact memory_type filter. ``memory_type`` is a real
            # column on obs_index, so this composes with the secondary index
            # cheaply (used by supersede-candidate detection).
            where.append('memory_type = ?')
            params.append(memory_type)
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

        # ADR-0021: 3-stage query fallback. Split multi-term queries into
        # AND semantics; trigram cannot match terms shorter than 3 chars, so
        # those terms are added as payload_json LIKE filters.
        query_terms = query.split()
        if self._fts_cap == _FTS_CAP_LIKE:
            fts_terms: list[str] = []
            like_terms = query_terms
        else:
            fts_terms = [term for term in query_terms if len(term) >= 3]
            like_terms = [term for term in query_terms if len(term) < 3]
        use_fts = bool(fts_terms)

        # ADR-0027: importance-aware ranking. When the caller expressed intent
        # with a query, importance is the PRIMARY ranking key and the relevance
        # signal (bm25 / recency) breaks ties within an importance level, so a
        # critical decision is not buried under a trivial note that merely
        # mentions the term. Importance-primary (rather than a weighted blend)
        # is deliberate: SQLite trigram bm25 ranks are ~1e-6 and corpus-
        # dependent, so no fixed additive weight is stable across stores — but
        # a 1-5 importance bucket is. Two deliberate exclusions:
        #   - no-query *browse* listings stay purely chronological (recency is
        #     the contract of a query-less search; reordering would surprise).
        #   - cursor pagination (bulk-delete, #66) must keep its
        #     (created_at, observation_id) ordering or the strict-tuple cursor
        #     walk skips/repeats rows — never inject importance there.
        rank_by_importance = bool(query_terms) and not cursor_observation_id
        imp_plain = 'importance DESC, ' if rank_by_importance else ''
        if rank_by_importance:
            # importance first, then bm25 relevance as the in-bucket tiebreak.
            fts_and_order = 'o.importance DESC, f.rank'
            fts_or_order = '(f.rank IS NULL), o.importance DESC, f.rank'
        else:
            fts_and_order = 'f.rank'
            fts_or_order = '(f.rank IS NULL), f.rank'

        if search_mode == 'and':
            for term in like_terms:
                where.append("LOWER(payload_json) LIKE ? ESCAPE '\\'")
                params.append(f'%{_escape_like(term.lower())}%')

            if use_fts:
                # FTS5 path: CTE join for bm25 ranking.
                fts_where = (' WHERE ' + ' AND '.join(where)) if where else ''
                match_expr = ' AND '.join(_quote_fts_term(term) for term in fts_terms)
                sql = (
                    'WITH fts_match AS (SELECT observation_id, rank FROM obs_fts WHERE obs_fts MATCH ?) '
                    'SELECT o.payload_json '
                    'FROM obs_index o '
                    'JOIN fts_match f ON f.observation_id = o.observation_id'
                    f'{fts_where} '
                    f'ORDER BY {fts_and_order}, o.created_at DESC, o.observation_id DESC LIMIT ?'
                )
                rows_params: list[object] = [match_expr, *params, max(1, limit)]
            else:
                sql = 'SELECT payload_json FROM obs_index'
                if where:
                    sql += ' WHERE ' + ' AND '.join(where)
                # ``observation_id`` is the PRIMARY KEY, so adding it as a secondary
                # sort key gives a total, stable order for cursor pagination over
                # rows that share the same ``created_at`` (#66). ``imp_plain`` is
                # empty on the cursor / browse paths (see rank_by_importance).
                sql += f' ORDER BY {imp_plain}created_at DESC, observation_id DESC LIMIT ?'
                rows_params = [*params, max(1, limit)]

        else:  # search_mode == 'or'
            # OR behavior: query terms combined with OR; base filters remain AND.
            like_or_preds: list[str] = ["LOWER(payload_json) LIKE ? ESCAPE '\\'" for _ in like_terms]
            like_or_params: list[object] = [f'%{_escape_like(t.lower())}%' for t in like_terms]

            if use_fts:
                match_expr = ' OR '.join(_quote_fts_term(term) for term in fts_terms)
                # LEFT JOIN so LIKE-only rows (not in obs_fts) are also returned.
                term_preds = ['f.observation_id IS NOT NULL', *like_or_preds]
                term_or = '(' + ' OR '.join(term_preds) + ')'
                if where:
                    combined_where = ' WHERE ' + ' AND '.join(where) + ' AND ' + term_or
                else:
                    combined_where = ' WHERE ' + term_or
                sql = (
                    'WITH fts_match AS (SELECT observation_id, rank FROM obs_fts WHERE obs_fts MATCH ?) '
                    'SELECT o.payload_json '
                    'FROM obs_index o '
                    'LEFT JOIN fts_match f ON f.observation_id = o.observation_id'
                    f'{combined_where} '
                    f'ORDER BY {fts_or_order}, o.created_at DESC, o.observation_id DESC LIMIT ?'
                )
                rows_params = [match_expr, *params, *like_or_params, max(1, limit)]
            else:
                if like_or_preds:
                    term_or = '(' + ' OR '.join(like_or_preds) + ')'
                    if where:
                        combined_where = ' WHERE ' + ' AND '.join(where) + ' AND ' + term_or
                    else:
                        combined_where = ' WHERE ' + term_or
                else:
                    # No query terms: recency search (same as 'and' with empty query).
                    combined_where = (' WHERE ' + ' AND '.join(where)) if where else ''
                sql = 'SELECT payload_json FROM obs_index'
                sql += combined_where
                sql += f' ORDER BY {imp_plain}created_at DESC, observation_id DESC LIMIT ?'
                rows_params = [*params, *like_or_params, max(1, limit)]

        with self._lock:
            try:
                rows = self._conn.execute(sql, rows_params).fetchall()
            except sqlite3.Error as e:
                if use_fts:
                    # FTS query failed (e.g. unsupported syntax); fall back to LIKE.
                    log.debug('LocalIndex.search FTS failed, falling back to LIKE: %s', e)
                    if search_mode == 'and':
                        # AND fallback: fts_terms become AND LIKE (where already has like_terms).
                        like_where = [*where]
                        like_params: list[object] = [*params]
                        for term in fts_terms:
                            like_where.append("LOWER(payload_json) LIKE ? ESCAPE '\\'")
                            like_params.append(f'%{_escape_like(term.lower())}%')
                        like_sql = 'SELECT payload_json FROM obs_index'
                        if like_where:
                            like_sql += ' WHERE ' + ' AND '.join(like_where)
                        like_sql += f' ORDER BY {imp_plain}created_at DESC, observation_id DESC LIMIT ?'
                        like_params.append(max(1, limit))
                    else:  # 'or' fallback: all terms become OR LIKE.
                        all_or_terms = [*fts_terms, *like_terms]
                        or_fallback_preds = ["LOWER(payload_json) LIKE ? ESCAPE '\\'" for _ in all_or_terms]
                        or_fallback_p: list[object] = [f'%{_escape_like(t.lower())}%' for t in all_or_terms]
                        like_sql = 'SELECT payload_json FROM obs_index'
                        if where and or_fallback_preds:
                            like_sql += (
                                ' WHERE ' + ' AND '.join(where) + ' AND (' + ' OR '.join(or_fallback_preds) + ')'
                            )
                        elif where:
                            like_sql += ' WHERE ' + ' AND '.join(where)
                        elif or_fallback_preds:
                            like_sql += ' WHERE (' + ' OR '.join(or_fallback_preds) + ')'
                        like_sql += f' ORDER BY {imp_plain}created_at DESC, observation_id DESC LIMIT ?'
                        like_params = [*params, *or_fallback_p, max(1, limit)]
                    try:
                        rows = self._conn.execute(like_sql, like_params).fetchall()
                    except sqlite3.Error as e2:
                        log.warning('LocalIndex.search LIKE fallback failed: %s', e2)
                        return []
                else:
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
                # ADR-0021: clear superseded_by on rows that pointed to this obs.
                self._conn.execute(
                    'UPDATE obs_index SET superseded_by = NULL WHERE superseded_by = ?',
                    (observation_id,),
                )
                if self._fts_cap != _FTS_CAP_LIKE:
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

    def list_shadowed_obs(
        self,
        project: str = '',
        limit: int = 50,
    ) -> list[tuple[str, str, str, str, str]]:
        """Return (observation_id, project, created_at, shadowed_at, summary) for shadowed rows.

        Drives ADR-0028 Phase 1 shadow inspect path. Rows with
        ``shadowed_at IS NOT NULL AND deleted_at IS NULL`` are rebuild-shadowed
        observations: they were not seen during the last Zenoh rebuild and are
        hidden from search, but have not been physically deleted yet.

        ``project`` narrows the scan when non-empty. Results are ordered
        ``shadowed_at DESC`` so the most-recently-shadowed rows appear first.
        """
        if self._disabled or self._conn is None:
            return []
        sql = (
            'SELECT observation_id, project, created_at, shadowed_at, summary '
            'FROM obs_index '
            'WHERE shadowed_at IS NOT NULL AND deleted_at IS NULL'
        )
        params: list[object] = []
        if project:
            sql += ' AND project = ?'
            params.append(project)
        sql += ' ORDER BY shadowed_at DESC LIMIT ?'
        params.append(max(1, limit))
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                log.warning('LocalIndex.list_shadowed_obs failed: %s', e)
                return []
        return [(row[0], row[1] or '', row[2] or '', row[3] or '', row[4] or '') for row in rows]

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
        # ADR-0021: also fetch superseded_by so get_memory can expose it.
        sql = 'SELECT payload_json, superseded_by FROM obs_index WHERE observation_id = ?'
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
            obs = Observation.from_json(row[0])
            # Stash superseded_by in _extras so callers (e.g. get_memory) can surface it.
            if row[1]:
                obs._extras['superseded_by'] = row[1]  # noqa: SLF001
            return obs
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

        return self._rebuild_from_observations(obs_list, tomb_ids, mark_missing_shadowed=True)

    def _rebuild_from_observations(
        self,
        obs_list: list[Observation],
        tomb_ids: dict[str, str],
        *,
        mark_missing_shadowed: bool,
    ) -> RebuildStats:
        """Apply obs_list and tomb_ids to the SQLite index in one transaction.

        mark_missing_shadowed=True (Zenoh path): index rows absent from
        obs_list are marked shadowed (may be restored if they reappear).
        mark_missing_shadowed=False (raw.db path): such rows are physically
        deleted because raw.db is the SoT and their absence means they are gone.
        """
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

                stale_rows: list[tuple[str, str]] = []
                shadowed_at = _shadow_now_iso()
                for obs_id, (deleted_at, existing_shadowed_at, _) in existing.items():
                    if deleted_at is None and existing_shadowed_at is None and obs_id not in seen_obs_ids:
                        stale_rows.append((shadowed_at, obs_id))
                        shadowed += 1

                self._conn.execute('BEGIN')
                if upsert_rows:
                    self._conn.executemany(_UPSERT_SQL, upsert_rows)
                if mark_rows:
                    self._conn.executemany(_MARK_DELETED_SQL, mark_rows)
                if mark_missing_shadowed:
                    if stale_rows:
                        self._conn.executemany(_MARK_SHADOWED_SQL, stale_rows)
                else:
                    for _, obs_id in stale_rows:
                        self._conn.execute('DELETE FROM obs_index WHERE observation_id = ?', (obs_id,))
                        self._conn.execute(
                            'UPDATE obs_index SET superseded_by = NULL WHERE superseded_by = ?',
                            (obs_id,),
                        )
                # ADR-0025: incremental FTS rebuild — reuse diff already computed for obs_index.
                # Order: upsert first, then delete (same-id upsert+tombstone nets to delete).
                if self._fts_cap != _FTS_CAP_LIKE:
                    obs_by_id = {obs.observation_id: obs for obs in obs_list}
                    for row in upsert_rows:
                        obs_id = row[0]
                        obs = obs_by_id[obs_id]
                        self._conn.execute(_FTS_DELETE_SQL, (obs_id,))
                        self._conn.execute(
                            _FTS_UPSERT_SQL,
                            (
                                obs_id,
                                obs.content,
                                obs.subject or '',
                                obs.summary or '',
                                ' '.join(obs.tags or []),
                                obs.project or '',
                            ),
                        )
                    for _, obs_id in mark_rows:
                        self._conn.execute(_FTS_DELETE_SQL, (obs_id,))
                    for _, obs_id in stale_rows:
                        self._conn.execute(_FTS_DELETE_SQL, (obs_id,))
                    # drift guard: count mismatch triggers full rebuild as self-heal.
                    (fts_count,) = self._conn.execute('SELECT COUNT(*) FROM obs_fts').fetchone()
                    (live_count,) = self._conn.execute(
                        'SELECT COUNT(*) FROM obs_index WHERE deleted_at IS NULL AND shadowed_at IS NULL'
                    ).fetchone()
                    if fts_count != live_count:
                        log.warning(
                            '_rebuild_from_observations FTS drift detected'
                            ' (%d vs %d live); falling back to full rebuild',
                            fts_count,
                            live_count,
                        )
                        _rebuild_fts_from_obs_index(self._conn)
                # Reconstruct superseded_by from obs_list (reset first for idempotency).
                self._conn.execute('UPDATE obs_index SET superseded_by = NULL')
                for obs in obs_list:
                    if obs.supersedes:
                        placeholders = ','.join('?' for _ in obs.supersedes)
                        self._conn.execute(
                            f'UPDATE obs_index SET superseded_by = ? WHERE observation_id IN ({placeholders})',
                            [obs.observation_id, *obs.supersedes],
                        )
                self._conn.execute('COMMIT')
            except sqlite3.Error as e:
                try:
                    self._conn.execute('ROLLBACK')
                except Exception:  # noqa: BLE001
                    pass
                log.warning('_rebuild_from_observations transaction failed: %s', e)
                raise

        return RebuildStats(added=added, marked_deleted=marked_deleted, shadowed=shadowed, unchanged=unchanged)

    def rebuild_from_raw_records(
        self,
        obs_iter: Iterator[Observation],
        tomb_iter: Iterator[Tombstone],
        *,
        mark_missing_shadowed: bool = False,
    ) -> RebuildStats:
        """Rebuild the SQLite index from LocalRawStore iterators.

        Intended for LocalBackend open: obs_iter/tomb_iter come from
        LocalRawStore.scan_obs()/scan_tombs().  raw.db is the SoT so
        mark_missing_shadowed defaults to False (stale index rows are
        physically deleted instead of shadowed).
        """
        if self._disabled or self._conn is None:
            return RebuildStats()
        obs_list = list(obs_iter)
        tomb_ids = {t.observation_id: t.deleted_at for t in tomb_iter}
        return self._rebuild_from_observations(obs_list, tomb_ids, mark_missing_shadowed=mark_missing_shadowed)

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

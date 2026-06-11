"""Local pending-puts queue for kioku-mesh (extracted from ``store.py``, #167).

When a Zenoh put fails with a retryable transport error, the serialized
payload is queued in a local SQLite DB (``pending_puts.db``) and replayed
after transport recovery (PR #50). Per ADR-0010, a save is complete only
once the Zenoh put succeeds — the queue is the explicit holding area for
writes that have not yet met that contract.

This module owns the queue storage, the drain logic, and the background
drain worker thread. Collaborators are resolved at call time via
:func:`_store`, so patching the *function itself* on ``store`` (for example
``store.get_session`` or ``store.get_index``) keeps affecting drain
behavior. Note the limit (#172): functions whose implementation lives in
``transport`` read their *own* module globals, so transport-owned internals
(``_open_session``, ``_session``, ``_RETRYABLE_EXC``, status recorders)
must be patched on ``mesh_mem.transport``, not on the ``store`` aliases.
``store`` re-exports this module's public surface, so
``store.drain_pending_puts`` and friends remain valid entry points.
"""

import logging
import sqlite3
import threading
from types import ModuleType

from .identity import state_dir
from .models import Observation
from .models import Tombstone

log = logging.getLogger(__name__)

_PENDING_PUTS_LIMIT = 1000
_PENDING_DRAIN_BATCH = 10
_PENDING_DRAIN_JOIN_TIMEOUT = 0.2

# Single-flight lock for drain execution (foreground and background share it).
_pending_drain_lock = threading.Lock()
# Guards the background worker's thread / stop-event pair. ``store.py``
# historically reused ``_transport_status_lock`` for these; the drain thread
# state now has its own lock with identical semantics.
_drain_state_lock = threading.Lock()
_pending_drain_thread: threading.Thread | None = None
_pending_drain_stop_event: threading.Event | None = None


def _store() -> ModuleType:
    """Return the ``store`` module, resolved lazily to avoid import cycles.

    Looked up at call time (not import time) so test monkeypatches on
    ``store``-level functions — ``get_session``, ``get_index``,
    ``_now_iso_utc`` — stay effective for queue and drain internals.
    This only covers the attribute this module reads off ``store``;
    internals *inside* a transport-owned function (e.g. which session
    factory ``get_session`` consults) live in ``mesh_mem.transport`` and
    must be patched there (#172).
    """
    from . import store

    return store


def _pending_puts_db_path() -> str:
    """Return the SQLite file path for locally queued failed puts."""
    return str(state_dir() / 'pending_puts.db')


def _open_pending_puts_db() -> sqlite3.Connection:
    """Open the local pending-puts SQLite DB, creating schema as needed."""
    conn = sqlite3.connect(_pending_puts_db_path(), timeout=5.0)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_puts (
          key_expr TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          observation_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          queued_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _trim_pending_puts_locked(conn: sqlite3.Connection) -> None:
    """Keep only the newest ``_PENDING_PUTS_LIMIT`` queued rows."""
    row_count = conn.execute('SELECT COUNT(*) FROM pending_puts').fetchone()[0]
    overflow = row_count - _PENDING_PUTS_LIMIT
    if overflow <= 0:
        return
    conn.execute(
        """
        DELETE FROM pending_puts
        WHERE key_expr IN (
          SELECT key_expr
          FROM pending_puts
          ORDER BY queued_at ASC, key_expr ASC
          LIMIT ?
        )
        """,
        (overflow,),
    )


def _enqueue_pending_put(kind: str, key_expr: str, observation_id: str, payload_json: str) -> None:
    """Persist a failed put locally so it can be retried after transport recovery."""
    try:
        conn = _open_pending_puts_db()
    except (OSError, sqlite3.Error) as e:
        log.warning('pending_puts open failed for %s: %s', key_expr, e)
        return
    try:
        conn.execute(
            """
            INSERT INTO pending_puts(key_expr, kind, observation_id, payload_json, queued_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key_expr) DO UPDATE SET
              kind=excluded.kind,
              observation_id=excluded.observation_id,
              payload_json=excluded.payload_json,
              queued_at=excluded.queued_at
            """,
            (key_expr, kind, observation_id, payload_json, _store()._now_iso_utc()),  # noqa: SLF001 — store collaborator, see _store()
        )
        _trim_pending_puts_locked(conn)
        conn.commit()
    except sqlite3.Error as e:
        log.warning('pending_puts enqueue failed for %s: %s', key_expr, e)
    finally:
        conn.close()


def _delete_pending_put(key_expr: str) -> None:
    """Drop a queued row once the corresponding mesh put succeeds."""
    try:
        conn = _open_pending_puts_db()
    except (OSError, sqlite3.Error):
        return
    try:
        conn.execute('DELETE FROM pending_puts WHERE key_expr = ?', (key_expr,))
        conn.commit()
    except sqlite3.Error as e:
        log.warning('pending_puts delete failed for %s: %s', key_expr, e)
    finally:
        conn.close()


def _count_pending_puts() -> int:
    """Return the number of queued failed puts on local disk."""
    try:
        conn = _open_pending_puts_db()
    except (OSError, sqlite3.Error):
        return 0
    try:
        return int(conn.execute('SELECT COUNT(*) FROM pending_puts').fetchone()[0])
    except sqlite3.Error as e:
        log.warning('pending_puts count failed: %s', e)
        return 0
    finally:
        conn.close()


def _deserialize_pending_put(kind: str, observation_id: str, payload_json: str) -> Observation | Tombstone:
    """Validate and deserialize a queued row before replaying it to the mesh."""
    if kind == 'observation':
        obs = Observation.from_json(payload_json)
        if obs.observation_id != observation_id:
            raise ValueError(
                f'observation_id mismatch in queued observation: {obs.observation_id} != {observation_id}'
            )
        return obs
    if kind == 'tombstone':
        tomb = Tombstone.from_json(payload_json)
        if tomb.observation_id != observation_id:
            raise ValueError(f'observation_id mismatch in queued tombstone: {tomb.observation_id} != {observation_id}')
        return tomb
    raise ValueError(f'unknown pending put kind: {kind!r}')


def _apply_replayed_put_to_index(replayed: Observation | Tombstone) -> None:
    """Mirror a replayed queued put into the local SQLite sidecar index."""
    if isinstance(replayed, Observation):
        _store().get_index().upsert(replayed)
        return
    _store().get_index().mark_deleted(replayed.observation_id, replayed.deleted_at)


def _drain_pending_puts_once_locked(limit: int | None = None) -> int:
    """Replay locally queued puts after a successful live write.

    Best-effort only: failures are logged and leave the remaining rows queued.
    Work is capped per call so the foreground save path does not block on a
    large backlog after transport recovery.
    """
    if limit is None:
        limit = _PENDING_DRAIN_BATCH
    if limit < 1:
        raise ValueError('limit must be >= 1')

    try:
        conn = _open_pending_puts_db()
    except (OSError, sqlite3.Error) as e:
        log.warning('pending_puts open failed during drain: %s', e)
        return 0
    try:
        total_rows = int(conn.execute('SELECT COUNT(*) FROM pending_puts').fetchone()[0])
        rows = conn.execute(
            """
            SELECT key_expr, kind, observation_id, payload_json
            FROM pending_puts
            ORDER BY queued_at ASC, key_expr ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as e:
        conn.close()
        log.warning('pending_puts load failed during drain: %s', e)
        return 0
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if not rows:
        return 0

    store = _store()
    drained = 0
    session = store.get_session()
    for key_expr, kind, observation_id, payload_json in rows:
        try:
            replayed = _deserialize_pending_put(kind, observation_id, payload_json)
            session.put(key_expr, payload_json)
            store._record_put_result('ok')  # noqa: SLF001 — store collaborator, see _store()
            _apply_replayed_put_to_index(replayed)
            _delete_pending_put(key_expr)
            drained += 1
        except store._RETRYABLE_EXC as e:  # noqa: SLF001 — store collaborator, see _store()
            store._record_put_result(f'error: {type(e).__name__}')  # noqa: SLF001 — store collaborator, see _store()
            log.warning('pending_puts drain stopped at %s: %s', key_expr, e)
            store._set_zenoh_session_state('disconnected')  # noqa: SLF001 — store collaborator, see _store()
            store._reset_session()  # noqa: SLF001 — store collaborator, see _store()
            break
        except Exception as e:  # noqa: BLE001
            log.warning('pending_puts drop malformed row for %s: %s', key_expr, e)
            _delete_pending_put(key_expr)
    remaining = _count_pending_puts()
    store._record_pending_drain_success(drained)  # noqa: SLF001 — store collaborator, see _store()
    if drained:
        log.info(
            'drained %d pending puts (%d remaining, total_succeeded=%d)',
            drained,
            remaining,
            store.get_transport_status().drain_total_succeeded,
        )
    if total_rows > len(rows):
        log.info('pending_puts drain capped at %d rows this call (%d still queued)', len(rows), remaining)
    return drained


def _drain_pending_puts_best_effort(limit: int | None = None) -> int:
    """Replay locally queued puts while holding the shared single-flight lock."""
    with _pending_drain_lock:
        return _drain_pending_puts_once_locked(limit=limit)


def drain_pending_puts(limit: int | None = None, *, wait: bool = True) -> int:
    """Drain queued failed puts, optionally skipping when another drain is active."""
    if wait:
        return _drain_pending_puts_best_effort(limit=limit)
    if not _pending_drain_lock.acquire(blocking=False):
        log.info('pending_puts drain skipped because another drain is already in progress')
        return 0
    try:
        return _drain_pending_puts_once_locked(limit=limit)
    finally:
        _pending_drain_lock.release()


def _run_pending_drain_background(limit: int | None, stop_event: threading.Event) -> None:
    """Drain queued puts in bounded batches until empty, blocked, or asked to stop."""
    store = _store()
    store._set_pending_drain_in_progress(True)  # noqa: SLF001 — store collaborator, see _store()
    try:
        log.info('pending_puts background drain started (%d queued)', _count_pending_puts())
        while not stop_event.is_set():
            drained = drain_pending_puts(limit=limit, wait=True)
            remaining = _count_pending_puts()
            if remaining == 0:
                log.info('pending_puts background drain finished (0 remaining)')
                return
            if drained == 0:
                log.info('pending_puts background drain paused (%d still queued)', remaining)
                return
        log.info('pending_puts background drain stop requested (%d remaining)', _count_pending_puts())
    finally:
        global _pending_drain_thread, _pending_drain_stop_event
        store._set_pending_drain_in_progress(False)  # noqa: SLF001 — store collaborator, see _store()
        with _drain_state_lock:
            if _pending_drain_thread is threading.current_thread():
                _pending_drain_thread = None
                _pending_drain_stop_event = None


def start_pending_drain_background(limit: int | None = None) -> bool:
    """Start a daemon worker that drains queued puts if transport is reachable."""
    global _pending_drain_thread, _pending_drain_stop_event
    if _count_pending_puts() <= 0:
        return False
    store = _store()
    try:
        store.get_session()
    except store._RETRYABLE_EXC as e:  # noqa: SLF001 — store collaborator, see _store()
        log.info('pending_puts background drain skipped (transport unreachable): %s', e)
        return False
    with _drain_state_lock:
        if _pending_drain_thread is not None and _pending_drain_thread.is_alive():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_run_pending_drain_background,
            args=(limit, stop_event),
            name='kioku-mesh-pending-drain',
            daemon=True,
        )
        _pending_drain_stop_event = stop_event
        _pending_drain_thread = thread
    thread.start()
    return True


def stop_pending_drain_background(join_timeout: float = _PENDING_DRAIN_JOIN_TIMEOUT) -> bool:
    """Request shutdown of the background drain worker without blocking long on exit."""
    global _pending_drain_thread, _pending_drain_stop_event
    with _drain_state_lock:
        thread = _pending_drain_thread
        stop_event = _pending_drain_stop_event
    if stop_event is not None:
        stop_event.set()
    if thread is None:
        return True
    if thread is threading.current_thread():
        return False
    thread.join(timeout=max(0.0, join_timeout))
    if thread.is_alive():
        log.info('pending_puts background drain still running after %.2fs; leaving daemon thread alive', join_timeout)
        return False
    with _drain_state_lock:
        if _pending_drain_thread is thread:
            _pending_drain_thread = None
            _pending_drain_stop_event = None
    return True

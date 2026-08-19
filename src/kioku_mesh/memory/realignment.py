"""Periodic repair-only realignment worker (ADR-0035, TASK-450 Phase 2).

A long-lived MCP server keeps its local SQLite index aligned with Zenoh by
running :meth:`LocalIndex.repair_from_zenoh` on a fixed cadence: tombstones
every 15 minutes, a full observation+tombstone repair every 6 hours. The
repair path only applies facts it *saw* in Zenoh, so a timed-out or partial
scan can never hide a local-only row (ADR-0035). ``rebuild_from_zenoh``,
which does shadow rows missing from the scan, is deliberately not scheduled.

Ownership is explicit and narrow:

- ``kioku-mesh-mcp`` calls :func:`enable_realignment` at startup (Zenoh
  backend only) and :func:`disable_realignment` in its ``finally``. One-shot
  CLI invocations never enable it, so they keep the #38 latency contract.
- Even when enabled, the thread only starts once ``store.get_index()`` has
  actually opened the index (:func:`start_realignment_worker`). An MCP
  process whose client never touched a memory tool therefore opens neither
  an index nor a session as a side effect.

Collaborators are resolved at call time via :func:`_store` (import cycle,
same pattern as ``pending_queue``).

Intervals are module constants rather than env vars on purpose (TASK-450):
they are tunable in tests but not part of the user-facing surface until the
cadence has real operating data behind it.
"""

import logging
import threading
import time
from types import ModuleType
from typing import Callable

from .local_index import REPAIR_MODE_FULL
from .local_index import REPAIR_MODE_TOMB

log = logging.getLogger(__name__)

#: Cheap tombstone-only repair cadence, seconds.
TOMB_INTERVAL_SEC = 15 * 60
#: Full observation+tombstone repair cadence, seconds.
FULL_INTERVAL_SEC = 6 * 60 * 60
#: Shutdown must not stall the MCP exit; the thread is a daemon anyway.
JOIN_TIMEOUT = 0.2

_state_lock = threading.Lock()
_enabled = False
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


def _store() -> ModuleType:
    """Return the ``store`` module, resolved lazily to avoid an import cycle."""
    from . import store

    return store


def enable_realignment() -> None:
    """Allow this process to own a realignment worker (MCP, Zenoh backend)."""
    global _enabled
    with _state_lock:
        _enabled = True


def disable_realignment() -> None:
    """Revoke ownership and stop any running worker."""
    global _enabled
    with _state_lock:
        _enabled = False
    stop_realignment_worker()


def realignment_status() -> dict[str, object]:
    """Diagnostics: whether this process owns and runs a worker."""
    with _state_lock:
        thread = _thread
        return {'enabled': _enabled, 'running': thread is not None and thread.is_alive()}


def start_realignment_worker(*, full_repair_due: bool = False) -> bool:
    """Start the worker if this process owns one and none is running.

    ``full_repair_due`` is set when the startup rebuild did not succeed: the
    first full repair then runs at the next tombstone tick instead of waiting
    six hours, and keeps retrying at that cadence until it succeeds.

    Returns True when a thread was started.
    """
    global _thread, _stop_event
    with _state_lock:
        if not _enabled:
            return False
        if _thread is not None and _thread.is_alive():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_run_worker,
            args=(stop_event,),
            kwargs={'full_repair_due': full_repair_due},
            name='kioku-mesh-realignment',
            daemon=True,
        )
        _stop_event = stop_event
        _thread = thread
    thread.start()
    log.info('index realignment worker started (tomb %ds / full %ds)', TOMB_INTERVAL_SEC, FULL_INTERVAL_SEC)
    return True


def stop_realignment_worker(join_timeout: float = JOIN_TIMEOUT) -> bool:
    """Ask the worker to stop; join briefly. True when it is gone."""
    global _thread, _stop_event
    with _state_lock:
        thread = _thread
        stop_event = _stop_event
    if stop_event is not None:
        stop_event.set()
    if thread is None:
        return True
    if thread is threading.current_thread():
        return False
    thread.join(timeout=max(0.0, join_timeout))
    if thread.is_alive():
        # A scan in flight is bounded by its own zenoh timeout; the daemon
        # thread dies with the process rather than delaying shutdown here.
        log.info('index realignment worker still running after %.2fs; leaving daemon thread alive', join_timeout)
        return False
    with _state_lock:
        if _thread is thread:
            _thread = None
            _stop_event = None
    return True


def _repair_once(mode: str) -> bool:
    """Run one repair scan. Returns True on success; never raises."""
    store = _store()
    try:
        session = store.get_session()
        stats = store.get_index().repair_from_zenoh(session, mode=mode)
    except Exception as e:  # noqa: BLE001 — a failed repair must not kill the worker
        # local_index already persisted the failure in index_alignment_state.
        log.warning('index realignment %s repair failed: %s: %s', mode, type(e).__name__, e)
        return False
    log.info('index realignment %s repair: %s', mode, stats)
    return True


def _run_worker(
    stop_event: threading.Event,
    *,
    full_repair_due: bool = False,
    repair: Callable[[str], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
    wait: Callable[[float], bool] | None = None,
) -> None:
    """Repair loop. ``now``/``wait``/``repair`` are injectable for tests.

    ``wait(delay)`` returns True when a stop was requested (the
    ``Event.wait`` contract), which is the only way out of the loop.
    """
    if wait is None:
        wait = stop_event.wait
    if repair is None:
        repair = _repair_once
    try:
        next_tomb = now() + TOMB_INTERVAL_SEC
        next_full = now() + FULL_INTERVAL_SEC
        while True:
            if wait(max(0.0, min(next_tomb, next_full) - now())):
                return
            tick = now()
            if full_repair_due or tick >= next_full:
                # A full repair covers tombstones too, so both clocks reset.
                full_repair_due = not repair(REPAIR_MODE_FULL)
                next_full = tick + FULL_INTERVAL_SEC
            else:
                repair(REPAIR_MODE_TOMB)
            next_tomb = tick + TOMB_INTERVAL_SEC
    finally:
        global _thread, _stop_event
        with _state_lock:
            if _thread is threading.current_thread():
                _thread = None
                _stop_event = None

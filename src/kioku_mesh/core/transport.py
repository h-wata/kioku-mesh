"""Zenoh transport layer for kioku-mesh (extracted from ``store.py``, #167).

Owns the Zenoh session lifecycle (open / cached reuse / reset), the
retry policy for transport-level failures, mesh-readiness probes, query
reply iteration, and the in-memory transport-health snapshot
(:class:`TransportStatus`).

``with_retry`` intentionally narrows retryable exceptions to
transport-layer errors (``zenoh.ZError``, ``ConnectionError``,
``TimeoutError``, ``QueryErrorReply``). Everything else propagates
unchanged so implementation bugs are not hidden by a retry loop.

``store`` re-exports this module's surface, so ``store.get_session``,
``store.with_retry``, ``store.get_transport_status`` and friends remain
valid entry points for *calling*. The re-exports are plain aliases,
though: assigning or monkeypatching them on ``store`` only shadows the
alias and does not reach this module (#172). To stub or inspect
transport internals — ``_open_session``, the cached ``_session``,
``_mesh_first_probe_success``, ``_RETRYABLE_EXC``, status recorders —
patch kioku_mesh.transport`` directly.
"""

from collections import deque
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
import functools
import logging
import os
import threading
import time
from typing import Any

import zenoh

log = logging.getLogger(__name__)

GET_TIMEOUT = 5.0
_PUT_HISTORY_LIMIT = 20

_session: zenoh.Session | None = None
_mesh_first_probe_success: float | None = None
_mesh_session_start_time: float | None = None
_transport_status_lock = threading.Lock()
_zenoh_session_state = 'unknown'
_last_put_at_iso = ''
_last_put_status = 'never'
_recent_put_results: deque[str] = deque(maxlen=_PUT_HISTORY_LIMIT)
_pending_drain_in_progress = False
_pending_drain_last_run_iso = ''
_pending_drain_total_succeeded = 0


# Callback for pending-put count — registered by memory.pending_queue at import time
# to break the core → memory circular dependency (ADR-0023).
def _default_pending_count() -> int:
    return 0


_pending_count_fn: Callable[[], int] = _default_pending_count


def register_pending_count(fn: Callable[[], int]) -> None:
    """Register a callback that returns the current pending-put count.

    Called by memory.pending_queue after it initialises, so that
    get_transport_status() can report pending_puts without importing memory.
    """
    global _pending_count_fn
    _pending_count_fn = fn


@dataclass(frozen=True)
class TransportStatus:
    """Ephemeral transport-health snapshot for status reporting."""

    zenoh_session: str
    last_put_at_iso: str
    last_put_status: str
    recent_put_ok: int
    recent_put_error: int
    recent_put_window: int
    pending_puts: int
    drain_in_progress: bool
    drain_last_run_iso: str
    drain_total_succeeded: int


def _now_iso_utc() -> str:
    """Return the current UTC timestamp in the project's compact ISO form."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _set_zenoh_session_state(state: str) -> None:
    """Update the last-known Zenoh session connectivity label."""
    global _zenoh_session_state
    with _transport_status_lock:
        _zenoh_session_state = state


def _record_put_result(status: str) -> None:
    """Record the final outcome of a high-level put operation."""
    global _last_put_at_iso, _last_put_status
    at_iso = _now_iso_utc()
    with _transport_status_lock:
        _last_put_at_iso = at_iso
        _last_put_status = status
        _recent_put_results.append(status)


def _set_pending_drain_in_progress(in_progress: bool) -> None:
    """Publish whether a background pending-drain worker is active."""
    global _pending_drain_in_progress
    with _transport_status_lock:
        _pending_drain_in_progress = in_progress


def _record_pending_drain_success(drained: int) -> None:
    """Accumulate successful replay counts for status reporting."""
    global _pending_drain_last_run_iso, _pending_drain_total_succeeded
    if drained <= 0:
        return
    with _transport_status_lock:
        _pending_drain_last_run_iso = _now_iso_utc()
        _pending_drain_total_succeeded += drained


def get_transport_status() -> TransportStatus:
    """Return lightweight in-memory transport / put health for diagnostics."""
    with _transport_status_lock:
        ok_count = sum(1 for status in _recent_put_results if status == 'ok')
        err_count = len(_recent_put_results) - ok_count
        return TransportStatus(
            zenoh_session=_zenoh_session_state,
            last_put_at_iso=_last_put_at_iso,
            last_put_status=_last_put_status,
            recent_put_ok=ok_count,
            recent_put_error=err_count,
            recent_put_window=len(_recent_put_results),
            pending_puts=_pending_count_fn(),
            drain_in_progress=_pending_drain_in_progress,
            drain_last_run_iso=_pending_drain_last_run_iso,
            drain_total_succeeded=_pending_drain_total_succeeded,
        )


def _reset_transport_status() -> None:
    """Clear in-memory transport diagnostics (test-only reset path)."""
    global _zenoh_session_state, _last_put_at_iso, _last_put_status
    global _pending_drain_in_progress, _pending_drain_last_run_iso, _pending_drain_total_succeeded
    with _transport_status_lock:
        _zenoh_session_state = 'unknown'
        _last_put_at_iso = ''
        _last_put_status = 'never'
        _recent_put_results.clear()
        _pending_drain_in_progress = False
        _pending_drain_last_run_iso = ''
        _pending_drain_total_succeeded = 0


class QueryErrorReply(Exception):
    """Raised when ``session.get()`` yields a reply carrying ``err`` instead of ``ok``.

    Treated as retryable by ``with_retry`` so a transient query failure gets
    a single automatic re-attempt.
    """


# zenoh-python 1.9.0 surfaces connection / get / put failures via ``zenoh.ZError``.
# Ref: https://zenoh-python.readthedocs.io/en/1.9.0/api_reference.html
_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    zenoh.ZError,
    ConnectionError,
    TimeoutError,
    QueryErrorReply,
)


def _open_session() -> zenoh.Session:
    """Open a new Zenoh client session using ``ZENOH_CONNECT`` env var."""
    endpoint = os.environ.get('ZENOH_CONNECT', 'tcp/localhost:7447')
    config = zenoh.Config()
    config.insert_json5('mode', '"client"')
    config.insert_json5('connect/endpoints', f'["{endpoint}"]')
    return zenoh.open(config)


def _reset_session() -> None:
    """Drop the cached session so the next call reopens it."""
    global _session, _mesh_first_probe_success, _mesh_session_start_time
    if _session is not None:
        try:
            _session.close()
        except Exception:  # noqa: BLE001
            pass
    _session = None
    _mesh_first_probe_success = None
    _mesh_session_start_time = None


def get_session() -> zenoh.Session:
    """Return the cached Zenoh session, opening it on first use or after reset."""
    global _session, _mesh_session_start_time
    if _session is None:
        try:
            _session = _open_session()
        except _RETRYABLE_EXC:
            _set_zenoh_session_state('disconnected')
            raise
        _mesh_session_start_time = time.monotonic()
        _set_zenoh_session_state('connected')
    return _session


def is_mesh_ready(min_ready_sec: float = 5.0) -> bool:
    """Return True if a probe completed without error and min_ready_sec has elapsed.

    A probe completing with zero replies is considered ready (e.g., empty store
    or fresh deployment). Previously required at least one reply, which caused
    permanent "waiting" status on empty stores.

    Informational only — search_observations never blocks on readiness.
    Probe result is cached; re-probes after _reset_session().
    """
    global _mesh_first_probe_success
    if _mesh_first_probe_success is None:
        try:
            session = get_session()
            list(session.get('mem/**', timeout=1.0))
            _mesh_first_probe_success = time.monotonic()
        except Exception:  # noqa: BLE001
            pass
    if _mesh_first_probe_success is None:
        return False
    return (time.monotonic() - _mesh_first_probe_success) >= min_ready_sec


def mesh_ready_label(min_ready_sec: float = 5.0) -> str:
    """Human-readable readiness string for ``kioku-mesh status``.

    Returns ``'yes'`` when ready, ``'waiting (Xs)'`` showing elapsed seconds
    since session start, or ``'waiting (no session)'`` before any session opens.
    """
    if is_mesh_ready(min_ready_sec):
        return 'yes'
    if _mesh_session_start_time is None:
        return 'waiting (no session)'
    elapsed = time.monotonic() - _mesh_session_start_time
    return f'waiting ({elapsed:.1f}s)'


def with_retry(func: Callable[..., Any]) -> Callable[..., Any]:
    """Retry transport-level failures exactly once; propagate other exceptions verbatim.

    On final failure, wraps the last retryable cause in ``RuntimeError`` with
    ``raise ... from last_exc`` so ``__cause__`` preserves the original error.
    """

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        last_exc: BaseException | None = None
        track_put = func.__name__ in {'put_observation', 'put_tombstone'}
        for attempt in range(2):
            try:
                result = func(*args, **kwargs)
                if track_put:
                    _set_zenoh_session_state('connected')
                    _record_put_result('ok')
                return result
            except _RETRYABLE_EXC as e:
                last_exc = e
                log.warning('%s retryable failure (attempt %d): %s', func.__name__, attempt + 1, e)
                if track_put:
                    _set_zenoh_session_state('disconnected')
                _reset_session()
                time.sleep(0.2 * (attempt + 1))
            except Exception as e:
                if track_put:
                    _record_put_result(f'error: {type(e).__name__}')
                raise
            # Any exception not in _RETRYABLE_EXC propagates untouched.
        assert last_exc is not None  # for type-checkers; loop always sets this on final failure
        if track_put:
            _record_put_result(f'error: {type(last_exc).__name__}')
        raise RuntimeError(f'{func.__name__} failed after retry: {type(last_exc).__name__}: {last_exc}') from last_exc

    return wrapped


def _iter_ok_replies(
    session: zenoh.Session,
    key_expr: str,
    timeout: float = GET_TIMEOUT,
) -> Iterator[Any]:
    """Yield only ``ok`` replies from ``session.get()``; raise on first ``err``.

    Usage contract:
        - **Local-accumulation only.** Consumers MUST collect yielded values into
          a list/set and process them AFTER the loop exits, never cause side
          effects inside the loop.
        - A mid-stream ``raise`` would re-enter via ``with_retry``, so any
          in-loop side effect would be partially applied and then duplicated.
        - For side-effecting workflows, collect first, then drive a separate loop,
          or restrict side effects to idempotent operations.
    """
    for reply in session.get(key_expr, timeout=timeout):
        if reply.ok:
            yield reply.ok
            continue
        payload = ''
        try:
            if reply.err is not None:
                payload = reply.err.payload.to_string()
        except Exception:  # noqa: BLE001
            pass
        raise QueryErrorReply(f'query error for {key_expr}: {payload or "unknown"}')

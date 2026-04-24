"""Zenoh session wrapper for mesh-mem.

Responsibilities:
    - open / close / retry the underlying ``zenoh.Session``
    - surface ``Reply.err`` as ``QueryErrorReply`` so errors do not get
      silently turned into empty result sets
    - clamp ``limit`` to ``MAX_SEARCH`` (return-count cap, not scan cap)
    - filter / sort search results with timezone-aware datetime compare

``with_retry`` intentionally narrows retryable exceptions to transport-layer
errors (``zenoh.ZError``, ``ConnectionError``, ``TimeoutError``,
``QueryErrorReply``). Everything else propagates unchanged so implementation
bugs are not hidden by a retry loop.
"""

from collections.abc import Callable
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import functools
import logging
import os
import time
from typing import Any

import zenoh

from .models import Observation
from .models import Tombstone

log = logging.getLogger(__name__)

# Return-count cap for search APIs. Does NOT bound the underlying
# ``session.get()`` scan; callers must narrow the key_expr for large spaces.
MAX_SEARCH = 10_000
GET_TIMEOUT = 5.0

_session: zenoh.Session | None = None


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
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:  # noqa: BLE001
            pass
    _session = None


def get_session() -> zenoh.Session:
    """Return the cached Zenoh session, opening it on first use or after reset."""
    global _session
    if _session is None:
        _session = _open_session()
    return _session


def with_retry(func: Callable[..., Any]) -> Callable[..., Any]:
    """Retry transport-level failures exactly once; propagate other exceptions verbatim.

    On final failure, wraps the last retryable cause in ``RuntimeError`` with
    ``raise ... from last_exc`` so ``__cause__`` preserves the original error.
    """

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        last_exc: BaseException | None = None
        for attempt in range(2):
            try:
                return func(*args, **kwargs)
            except _RETRYABLE_EXC as e:
                last_exc = e
                log.warning('%s retryable failure (attempt %d): %s', func.__name__, attempt + 1, e)
                _reset_session()
                time.sleep(0.2 * (attempt + 1))
            # Any exception not in _RETRYABLE_EXC propagates untouched.
        raise RuntimeError(f'{func.__name__} failed after retry') from last_exc

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


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO 8601 timestamp to a timezone-aware UTC datetime.

    ``None`` on parse failure. Accepts both ``+00:00`` and ``Z`` suffixes.
    Naive inputs are treated as UTC (documented, not enforced).
    """
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@with_retry
def put_observation(obs: Observation) -> None:
    """Publish an observation to its canonical ``mem/obs/...`` key."""
    get_session().put(obs.key_expr, obs.to_json())


@with_retry
def put_tombstone(obs: Observation, reason: str = '') -> None:
    """Publish a tombstone at the mirrored ``mem/tomb/...`` key for this observation."""
    tomb = Tombstone(observation_id=obs.observation_id, reason=reason)
    get_session().put(obs.tombstone_key_expr(), tomb.to_json())


@with_retry
def search_observations(
    query: str = '',
    agent_family: str = '',
    client_id: str = '',
    pc_id: str = '',
    session_id: str = '',
    project: str = '',
    since_iso: str = '',
    limit: int = 50,
) -> list[Observation]:
    """Search observations, narrowing by key_expr then filtering in Python.

    Tombstone keys for the same observation_id cause the entry to be hidden.
    """
    limit = max(1, min(limit, MAX_SEARCH))
    since_dt = _parse_iso(since_iso)

    parts = [
        'mem/obs',
        agent_family or '*',
        client_id or '*',
        pc_id or '*',
        session_id or '*',
        '**',
    ]
    key_expr = '/'.join(parts)
    tomb_expr = key_expr.replace('mem/obs/', 'mem/tomb/', 1)

    session = get_session()

    tombs: set[str] = set()
    for ok in _iter_ok_replies(session, tomb_expr):
        tombs.add(str(ok.key_expr).rsplit('/', 1)[-1])

    q = query.lower()
    results: list[Observation] = []
    for ok in _iter_ok_replies(session, key_expr):
        try:
            obs = Observation.from_json(ok.payload.to_string())
        except Exception as e:  # noqa: BLE001
            log.warning('skip malformed payload at %s: %s', ok.key_expr, e)
            continue
        if obs.observation_id in tombs:
            continue
        if project and obs.project != project:
            continue
        if since_dt:
            obs_dt = _parse_iso(obs.created_at)
            if obs_dt is None or obs_dt < since_dt:
                continue
        if (
            q
            and q not in obs.content.lower()
            and q not in obs.project.lower()
            and not any(q in t.lower() for t in obs.tags)
        ):
            continue
        results.append(obs)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    results.sort(key=lambda o: _parse_iso(o.created_at) or epoch, reverse=True)
    return results[:limit]


@with_retry
def find_observation_by_id(observation_id: str) -> Observation | None:
    """Locate a specific observation by full 32-char id, scanning ``mem/obs/**`` directly.

    Bypasses ``search_observations`` so that the delete path is not gated
    by ``MAX_SEARCH`` — required for reliable tombstone emission on older
    entries.
    """
    session = get_session()
    for ok in _iter_ok_replies(session, 'mem/obs/**'):
        try:
            obs = Observation.from_json(ok.payload.to_string())
        except Exception:  # noqa: BLE001
            continue
        if obs.observation_id == observation_id:
            return obs
    return None


@with_retry
def delete_key(key_expr: str) -> None:
    """Physically remove a key from the mesh via ``session.delete``.

    Callers pass an exact key expression, not a wildcard — storage-backend
    support for wildcard delete is inconsistent across Zenoh versions, so
    the gc paths enumerate first and then delete one key at a time.
    """
    get_session().delete(key_expr)


def _broadcast_delete_best_effort(key_expr: str) -> bool:
    """Send a wildcard ``session.delete`` as a best-effort broadcast.

    Unlike :func:`delete_key`, this helper accepts a wildcard key_expr and
    **does not raise** when the underlying backend rejects the pattern.
    Wildcard delete support varies by Zenoh version and storage plugin, so
    relying on it for correctness would regress the emergency-purge path
    on backends that refuse it. Failures are logged and swallowed; callers
    should treat the broadcast as additive insurance on top of exact-key
    deletes, not as a guaranteed purge.

    Returns ``True`` when the delete was issued without raising,
    ``False`` otherwise.
    """
    try:
        get_session().delete(key_expr)
    except _RETRYABLE_EXC as e:
        log.warning('broadcast delete %s failed (retryable, ignored): %s', key_expr, e)
        return False
    except Exception as e:  # noqa: BLE001 — wildcard support varies; never crash the purge
        log.warning('broadcast delete %s failed (non-retryable, ignored): %s', key_expr, e)
        return False
    return True


@with_retry
def _list_tombstones() -> list[tuple[str, Tombstone]]:
    """Return ``(tomb_key_expr, tombstone)`` for every tombstone in the mesh.

    Used by gc only; the normal search path does not materialize tomb bodies.
    Malformed tomb payloads are logged and skipped so one bad record cannot
    block the whole sweep.
    """
    session = get_session()
    out: list[tuple[str, Tombstone]] = []
    for ok in _iter_ok_replies(session, 'mem/tomb/**'):
        try:
            tomb = Tombstone.from_json(ok.payload.to_string())
        except Exception as e:  # noqa: BLE001
            log.warning('skip malformed tombstone at %s: %s', ok.key_expr, e)
            continue
        out.append((str(ok.key_expr), tomb))
    return out


def physical_delete_observation(observation_id: str) -> tuple[bool, bool]:
    """Physically purge an observation and any matching tombstones from the mesh.

    Locates the observation by full-id scan so the caller does not need to
    supply the identity fragments. Always sweeps ``mem/tomb/**`` for any
    tombstone carrying the same ``observation_id`` — covers the case where
    a tomb arrived on this router via replication but the obs never did,
    so the forensic trail is cleaned up as well.

    After the per-key deletes we additionally broadcast a wildcard-pattern
    ``session.delete`` for both ``mem/obs/*/*/*/*/{id}`` and
    ``mem/tomb/*/*/*/*/{id}`` via :func:`_broadcast_delete_best_effort`.
    This belt-and-suspenders step reaches any currently-connected storage
    replica whose copy we did not see via the live query (e.g. a peer
    whose replication lag meant our initial enumeration missed it). The
    broadcast is **best-effort** — wildcard delete support varies by
    Zenoh version and backend, so failures are logged and swallowed; we
    must not turn the emergency purge into a hard error on a backend that
    refuses the pattern.

    Returns:
        ``(obs_removed, tomb_removed)``. Reports what this router observed
        locally. A ``False`` return after broadcast still represents a
        best-effort purge across the mesh; callers should run the same
        command on every replica that might hold the key when a full
        split-brain guarantee is needed.
    """
    obs = find_observation_by_id(observation_id)
    obs_removed = False
    if obs is not None:
        delete_key(obs.key_expr)
        obs_removed = True

    # Enumerate-then-delete across tombs so we catch the orphan-tomb case.
    tomb_keys: list[str] = []
    for tomb_key, tomb in _list_tombstones():
        if tomb.observation_id == observation_id:
            tomb_keys.append(tomb_key)
    for k in tomb_keys:
        delete_key(k)

    # Broadcast wildcard deletes so reachable peers that didn't surface
    # via the live query above still drop matching keys. Key layout is
    # ``mem/{obs|tomb}/{agent}/{client}/{pc}/{session}/{observation_id}``,
    # so ``*/*/*/*`` matches the four identity segments below the prefix.
    # Best-effort: wildcard delete support varies by backend.
    _broadcast_delete_best_effort(f'mem/obs/*/*/*/*/{observation_id}')
    _broadcast_delete_best_effort(f'mem/tomb/*/*/*/*/{observation_id}')

    return (obs_removed, bool(tomb_keys))


def gc_expired_tombstones(
    retention_days: int = 30,
    now: datetime | None = None,
) -> int:
    """Physically purge tombstones older than ``retention_days`` along with their observations.

    Tombstones whose ``deleted_at`` cannot be parsed are left in place
    (conservative — never delete on ambiguity).

    Returns:
        Count of tombstones purged. The matching observation key is also
        deleted when present, but the return value counts tomb sweeps only.
    """
    if retention_days < 0:
        raise ValueError(f'retention_days must be >= 0, got {retention_days}')
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    purged = 0
    for tomb_key, tomb in _list_tombstones():
        deleted_dt = _parse_iso(tomb.deleted_at)
        if deleted_dt is None:
            log.warning('skip tomb with unparseable deleted_at: %s', tomb_key)
            continue
        if deleted_dt >= cutoff:
            continue
        # Defense against a misrouted / corrupted tombstone whose key suffix
        # disagrees with its body ``observation_id``: mirroring ``tomb_key``
        # to ``mem/obs/...`` would otherwise delete an unrelated obs. Skip
        # conservatively — the tomb stays, no obs is touched.
        key_suffix = tomb_key.rsplit('/', 1)[-1]
        if key_suffix != tomb.observation_id:
            log.warning(
                'skip tomb with key-suffix/body mismatch: key=%s body_id=%s',
                tomb_key,
                tomb.observation_id,
            )
            continue
        obs_key = tomb_key.replace('mem/tomb/', 'mem/obs/', 1)
        delete_key(tomb_key)
        delete_key(obs_key)
        purged += 1
    return purged

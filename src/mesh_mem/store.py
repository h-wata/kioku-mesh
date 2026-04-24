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

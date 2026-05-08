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
import json
import logging
import os
import time
from typing import Any

import zenoh

from .local_index import LocalIndex
from .models import Observation
from .models import Tombstone

log = logging.getLogger(__name__)

# Return-count cap for search APIs. Does NOT bound the underlying
# ``session.get()`` scan; callers must narrow the key_expr for large spaces.
MAX_SEARCH = 10_000
GET_TIMEOUT = 5.0

_session: zenoh.Session | None = None
_index: LocalIndex | None = None
_subscribers: list | None = None
_mesh_first_probe_success: float | None = None
_mesh_session_start_time: float | None = None

# Default rebuild-on-init policy. Long-lived processes (mesh-mem-mcp) keep
# the default ``True`` so the local SQLite index aligns with zenoh once at
# startup. One-shot CLI invocations call ``set_rebuild_on_init_default(False)``
# from ``__main__.main`` so each ``mesh-mem save/search/...`` does not pay
# the rebuild_from_zenoh cost on a populated mesh (#38). Env vars
# MESH_MEM_FORCE_REBUILD=1 and MESH_MEM_SKIP_REBUILD=1 override this default.
_rebuild_on_init_default: bool = True


def set_rebuild_on_init_default(rebuild: bool) -> None:
    """Override the default rebuild-on-first-init policy in this process.

    Reset by ``_reset_index()`` (test path); production CLI calls this once
    from ``main`` before any ``get_index()`` invocation.
    """
    global _rebuild_on_init_default
    _rebuild_on_init_default = rebuild


def _should_rebuild_on_init() -> bool:
    """Resolve the effective rebuild policy for the current process."""
    if os.environ.get('MESH_MEM_FORCE_REBUILD', '').strip() == '1':
        return True
    if os.environ.get('MESH_MEM_SKIP_REBUILD', '').strip() == '1':
        return False
    return _rebuild_on_init_default


def get_index() -> LocalIndex:
    """Return the cached LocalIndex sidecar, opening it on first use.

    Since Phase 3 the index is the default read path for
    ``search_observations`` / ``find_observation_by_id``; the legacy
    Zenoh full-scan stays available as a fallback when the index is
    disabled (``MESH_MEM_DISABLE_INDEX=1``) — in that case the returned
    instance is a no-op so callers do not need to branch.

    Phase 4: on first init, optionally runs ``rebuild_from_zenoh`` then
    starts the replication subscriber. The rebuild gate is the policy
    returned by :func:`_should_rebuild_on_init` — long-lived processes
    (default ``True``) align once at startup, while one-shot CLI invocations
    skip the ~15s scan on a populated mesh (#38). Set
    ``MESH_MEM_FORCE_REBUILD=1`` to opt back in for a CLI run, or pass
    ``--rebuild`` to ``mesh-mem``. Zenoh errors are logged and swallowed
    so a missing router cannot block reads/writes.
    """
    global _index, _subscribers
    if _index is None:
        _index = LocalIndex.connect()
        if not _index.disabled:
            try:
                session = get_session()
                if _should_rebuild_on_init():
                    try:
                        stats = _index.rebuild_from_zenoh(session)
                        log.info('LocalIndex rebuild: %s', stats)
                    except Exception as e:  # noqa: BLE001
                        log.warning('LocalIndex rebuild failed (partial index): %s', e)
                if _subscribers is None:
                    _subscribers = start_index_subscriber(session)
            except Exception as e:  # noqa: BLE001
                log.warning('LocalIndex zenoh init skipped (no session): %s', e)
    return _index


def _reset_subscribers() -> None:
    """Undeclare zenoh subscribers and clear the cache.

    Called by _reset_index so that subscriber callbacks stop referencing
    the about-to-be-closed LocalIndex instance.
    """
    global _subscribers
    if _subscribers:
        for sub in _subscribers:
            try:
                sub.undeclare()
            except Exception:  # noqa: BLE001
                pass
    _subscribers = None


def _reset_index() -> None:
    """Drop the cached index so the next call reopens it.

    Used by tests via ``conftest.py`` to ensure each test gets a fresh
    SQLite file under its own ``MESH_MEM_STATE_DIR`` tmp_path. Production
    code does not call this.

    Also restores ``_rebuild_on_init_default`` to its module default so a
    test that exercised the CLI (which flips the flag to False) does not
    leak that policy into the next test.
    """
    global _index, _rebuild_on_init_default
    _reset_subscribers()
    if _index is not None:
        try:
            _index.close()
        except Exception:  # noqa: BLE001
            pass
    _index = None
    _rebuild_on_init_default = True


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
        _session = _open_session()
        _mesh_session_start_time = time.monotonic()
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
    """Human-readable readiness string for ``mesh-mem status``.

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


def start_index_subscriber(session: zenoh.Session) -> list:
    """Subscribe to mem/obs/** and mem/tomb/** to keep SQLite in sync with replication.

    Callbacks are idempotent (upsert / mark_deleted) so overlap with the
    rebuild scan on startup is safe. The returned list holds the two
    zenoh.Subscriber objects; callers must call undeclare() on each when
    tearing down (handled by _reset_subscribers / _reset_index).
    """
    idx = get_index()
    if idx.disabled:
        return []

    def on_obs(sample: Any) -> None:
        try:
            obs = Observation.from_json(sample.payload.to_string())
            idx.upsert(obs)
        except json.JSONDecodeError as e:
            # Issue #31: gc broadcast-purge and other control payloads can
            # arrive on mem/obs/** with non-Observation bytes. Demote to
            # DEBUG so steady-state operation does not look pathological.
            log.debug('index subscriber on_obs non-JSON payload: %s', e)
        except Exception as e:  # noqa: BLE001
            log.warning('index subscriber on_obs error: %s', e)

    def on_tomb(sample: Any) -> None:
        try:
            tomb = Tombstone.from_json(sample.payload.to_string())
            idx.mark_deleted(tomb.observation_id, tomb.deleted_at)
        except json.JSONDecodeError as e:
            log.debug('index subscriber on_tomb non-JSON payload: %s', e)
        except Exception as e:  # noqa: BLE001
            log.warning('index subscriber on_tomb error: %s', e)

    sub_obs = session.declare_subscriber('mem/obs/**', on_obs)
    sub_tomb = session.declare_subscriber('mem/tomb/**', on_tomb)
    return [sub_obs, sub_tomb]


@with_retry
def put_observation(obs: Observation) -> None:
    """Publish an observation to its canonical ``mem/obs/...`` key.

    On zenoh success, also upsert into the local SQLite sidecar index
    (Issue #7 plan B). Index errors are best-effort and do not raise so
    a corrupt index file cannot break a working put.
    """
    get_session().put(obs.key_expr, obs.to_json())
    # Sidecar write happens AFTER the zenoh put so that a successful
    # zenoh-side write is the contract for callers. The index is a read-
    # acceleration layer; Phase 4 will reconcile it from zenoh on demand.
    get_index().upsert(obs)


@with_retry
def put_tombstone(obs: Observation, reason: str = '') -> None:
    """Publish a tombstone at the mirrored ``mem/tomb/...`` key for this observation.

    Also stamps ``deleted_at`` on the matching SQLite row when the index
    is enabled. A no-op on the index side when no row exists yet (the
    tombstone will be reconciled on Phase 4 rebuild).
    """
    tomb = Tombstone(observation_id=obs.observation_id, reason=reason)
    get_session().put(obs.tombstone_key_expr(), tomb.to_json())
    get_index().mark_deleted(obs.observation_id, tomb.deleted_at)


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
    """Search observations via the SQLite local index, falling back to Zenoh.

    Phase 3 of TASK-131: routes through :class:`LocalIndex.search` by
    default (sub-100ms at 50k per TASK-134 spike). The legacy Zenoh full-
    scan path stays available behind ``MESH_MEM_DISABLE_INDEX=1`` so
    operators can flip back if the index is unavailable.

    ``limit`` defaults to 50, max 10000 (``MAX_SEARCH``). Tombstones hide
    the matching observation in both paths.
    """
    limit = max(1, min(limit, MAX_SEARCH))
    idx = get_index()
    if not idx.disabled:
        return idx.search(
            project=project,
            agent_family=agent_family,
            client_id=client_id,
            pc_id=pc_id,
            session_id=session_id,
            query=query,
            since_iso=since_iso,
            limit=limit,
        )
    return _search_via_zenoh(
        query=query,
        agent_family=agent_family,
        client_id=client_id,
        pc_id=pc_id,
        session_id=session_id,
        project=project,
        since_iso=since_iso,
        limit=limit,
    )


@with_retry
def _search_via_zenoh(
    *,
    query: str,
    agent_family: str,
    client_id: str,
    pc_id: str,
    session_id: str,
    project: str,
    since_iso: str,
    limit: int,
) -> list[Observation]:
    """Legacy Zenoh full-scan search path, retained as a fallback.

    Identical semantics to the pre-Phase-3 ``search_observations`` body:
    narrow by key_expr, then filter project / since / query (substring
    on content / project / tags) in Python. Re-enabled by setting
    ``MESH_MEM_DISABLE_INDEX=1``.
    """
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
    # Use a dict keyed by observation_id so multiple Zenoh storages replying
    # with the same observation (multi-router / replication overlap) do not
    # produce duplicates (#12). Last-writer-wins within a single GET scan.
    results_by_id: dict[str, Observation] = {}
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
        results_by_id[obs.observation_id] = obs

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return sorted(results_by_id.values(), key=lambda o: _parse_iso(o.created_at) or epoch, reverse=True)[:limit]


def find_observation_by_id(observation_id: str) -> Observation | None:
    """Locate a specific observation by full 32-char id.

    Routes through the SQLite local index by default; on miss (or when
    the index is disabled / unavailable) falls back to a direct Zenoh
    ``mem/obs/**`` scan so the delete / gc paths can still locate
    pre-Phase-2 observations that were never indexed.

    The index lookup includes tombstoned rows because the gc / physical-
    delete callers need to find tombstoned observations to purge them.
    """
    idx = get_index()
    if not idx.disabled:
        hit = idx.find_by_id(observation_id, include_deleted=True)
        if hit is not None:
            return hit
        # Fall through: the index may legitimately not have the id (e.g.
        # an obs put on a peer before this PC's sidecar existed).
    return _find_by_id_via_zenoh(observation_id)


@with_retry
def _find_by_id_via_zenoh(observation_id: str) -> Observation | None:
    """Direct Zenoh ``mem/obs/**`` scan, retained for fallback / index-miss."""
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

    # Drop the local SQLite row too; otherwise readers would still see the
    # observation via the index even though Zenoh has dropped it.
    get_index().physical_delete(observation_id)

    return (obs_removed, bool(tomb_keys))


def gc_expired_tombstones(
    retention_days: int = 30,
    now: datetime | None = None,
    project: str = '',
) -> int:
    """Physically purge tombstones older than ``retention_days`` along with their observations.

    When ``project`` is non-empty, only tombstones whose corresponding
    observation has ``project == project`` are purged. Orphan tombstones
    (observation absent) are skipped conservatively when a project filter is
    active because the project cannot be determined. When ``project`` is empty
    (default) all expired tombstones are swept regardless of project.

    ``--force-id`` callers use :func:`physical_delete_observation` directly
    and are not affected by this filter.

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
        if project:
            obs = find_observation_by_id(tomb.observation_id)
            if obs is None:
                log.warning('skip orphan tomb (no obs found) under project filter: %s', tomb_key)
                continue
            if obs.project != project:
                continue
        obs_key = tomb_key.replace('mem/tomb/', 'mem/obs/', 1)
        delete_key(tomb_key)
        delete_key(obs_key)
        # Mirror the purge into the SQLite sidecar so the row does not
        # outlive the Zenoh delete.
        get_index().physical_delete(tomb.observation_id)
        purged += 1
    return purged

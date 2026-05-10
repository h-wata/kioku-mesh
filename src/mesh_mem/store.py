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

from collections import Counter
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
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
# MESH_MEM_FORCE_REBUILD=1 and MESH_MEM_SKIP_REBUILD=1 override this default,
# and an explicit ``set_rebuild_on_init_explicit(True/False)`` (the CLI's
# ``--rebuild`` flag) outranks both env vars.
_rebuild_on_init_default: bool = True
_rebuild_explicit_override: bool | None = None


def set_rebuild_on_init_default(rebuild: bool) -> None:
    """Override the default rebuild-on-first-init policy in this process.

    Lowest-priority signal: env vars and the explicit override outrank
    this. Reset by ``_reset_index()`` (test path); production CLI calls
    this once from ``main`` before any ``get_index()`` invocation.
    """
    global _rebuild_on_init_default
    _rebuild_on_init_default = rebuild


def set_rebuild_on_init_explicit(rebuild: bool | None) -> None:
    """Explicit user-level override for the rebuild policy (highest priority).

    Wins over both ``MESH_MEM_FORCE_REBUILD`` / ``MESH_MEM_SKIP_REBUILD``
    env vars and the module default. Direct user intent — for example, the
    CLI's ``--rebuild`` flag — must always take effect, even in environments
    that export ``MESH_MEM_SKIP_REBUILD=1`` from a shell profile or wrapper
    script. Pass ``None`` to clear the override.
    """
    global _rebuild_explicit_override
    _rebuild_explicit_override = rebuild


def _should_rebuild_on_init() -> bool:
    """Resolve the effective rebuild policy for the current process.

    Priority (highest to lowest):
      1. ``set_rebuild_on_init_explicit(True/False)`` — direct user intent.
      2. ``MESH_MEM_FORCE_REBUILD=1`` env var.
      3. ``MESH_MEM_SKIP_REBUILD=1`` env var.
      4. Module-level default (``True`` for long-lived processes; CLI flips
         to ``False`` so one-shot invocations stay sub-second).
    """
    if _rebuild_explicit_override is not None:
        return _rebuild_explicit_override
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

    Also restores ``_rebuild_on_init_default`` and the explicit override
    to their module defaults so a test that exercised the CLI (which
    flips the flag to False or sets the explicit override) does not leak
    that policy into the next test.
    """
    global _index, _rebuild_on_init_default, _rebuild_explicit_override
    _reset_subscribers()
    if _index is not None:
        try:
            _index.close()
        except Exception:  # noqa: BLE001
            pass
    _index = None
    _rebuild_on_init_default = True
    _rebuild_explicit_override = None


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


@dataclass
class BulkPurgeResult:
    """Outcome of :func:`bulk_purge_by_pc_id`.

    ``matches`` is always populated (used for the dry-run histogram); the
    delete counters are zero when ``executed=False``.
    """

    matches: list[tuple[str, str, str]] = field(default_factory=list)
    sessions: 'Counter[str]' = field(default_factory=Counter)
    purged: int = 0
    tombs_purged: int = 0
    failures: int = 0
    executed: bool = False


@with_retry
def _scan_obs_by_pc_id(
    pc_id: str,
    session_prefix: str,
) -> list[tuple[str, str, str]]:
    """Return ``(obs_key, observation_id, session_id)`` for matching obs.

    Reads ``mem/obs/**`` raw JSON instead of ``Observation.from_json`` so a
    payload with a now-invalid ``memory_type`` (legacy v0.2.2 enum) does not
    spam the per-record clamping WARNING during the scan — the bulk-purge
    callers do not need a parsed Observation, only the identity fields.
    """
    session = get_session()
    out: list[tuple[str, str, str]] = []
    for ok in _iter_ok_replies(session, 'mem/obs/**', timeout=30.0):
        try:
            payload = json.loads(ok.payload.to_string())
        except (json.JSONDecodeError, ValueError):
            continue
        if payload.get('pc_id') != pc_id:
            continue
        sid = payload.get('session_id', '')
        if session_prefix and not sid.startswith(session_prefix):
            continue
        obs_id = payload.get('observation_id')
        if not isinstance(obs_id, str) or len(obs_id) != 32:
            continue
        out.append((str(ok.key_expr), obs_id, sid))
    return out


def scan_obs_by_pc_id(
    pc_id: str,
    *,
    session_prefix: str = '',
) -> tuple[list[tuple[str, str, str]], 'Counter[str]']:
    """Return ``(matches, per-session counter)`` for a pc-scoped scan.

    Public scan-only entrypoint. The CLI calls this first (dry-run output +
    confirm prompt), then hands the same ``matches`` list to
    :func:`execute_bulk_purge` to avoid re-scanning ``mem/obs/**`` a second
    time on the destructive pass.
    """
    matches = _scan_obs_by_pc_id(pc_id, session_prefix)
    sessions: Counter[str] = Counter(sid for _, _, sid in matches)
    return matches, sessions


def execute_bulk_purge(
    matches: list[tuple[str, str, str]],
    *,
    progress_every: int = 500,
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> tuple[int, int, int]:
    """Delete every ``(obs_key, obs_id, _)`` in ``matches`` plus mirrored tombs.

    Returns ``(purged_obs, purged_tombs, failures)``. See
    :func:`bulk_purge_by_pc_id` for the rationale on skipping the global
    tomb sweep + wildcard broadcast and on the lexical obs→tomb mirror.
    """
    if not matches:
        return (0, 0, 0)
    session = get_session()
    idx = get_index()
    purged = 0
    tombs_purged = 0
    failures = 0
    total = len(matches)
    for i, (obs_key, obs_id, _sid) in enumerate(matches, start=1):
        try:
            session.delete(obs_key)
            if not idx.disabled:
                idx.physical_delete(obs_id)
            purged += 1
        except Exception as e:  # noqa: BLE001 — one bad key must not abort the sweep
            failures += 1
            log.warning('bulk purge failed for %s: %s', obs_id, e)
        # Mirror-delete the tomb slot. Lexical replace because keys share the
        # ``mem/{obs|tomb}/{ident}/{obs_id}`` layout. Counted separately and
        # wrapped in its own try so an obs-side success is never invalidated
        # by a tomb-side transport hiccup. The Zenoh ``delete`` is a no-op
        # when the tomb slot is absent (bench-obs case), so always safe.
        tomb_key = 'mem/tomb/' + obs_key[len('mem/obs/') :]
        try:
            session.delete(tomb_key)
            tombs_purged += 1
        except Exception as e:  # noqa: BLE001 — best-effort tomb cleanup
            log.debug('bulk purge tomb delete failed for %s: %s', tomb_key, e)
        if on_progress is not None and i % progress_every == 0:
            on_progress(i, total, purged, failures)
    return (purged, tombs_purged, failures)


def bulk_purge_by_pc_id(
    pc_id: str,
    *,
    session_prefix: str = '',
    execute: bool = False,
    progress_every: int = 500,
    on_progress: Callable[[int, int, int, int], None] | None = None,
) -> BulkPurgeResult:
    """Purge every obs that matches ``pc_id`` (and optionally a session prefix).

    Use case: a benchmark / smoke run on a peer host saved tens of thousands
    of synthetic observations under a throwaway ``session_id`` and they are
    now flooding the mesh. Operators scope the purge by ``pc_id`` and may
    further narrow with ``session_prefix`` so legitimate working memory on
    that host is not destroyed.

    Skips the global ``mem/tomb/**`` orphan sweep and the per-id wildcard
    broadcast that :func:`physical_delete_observation` performs. The global
    sweep stalls on ``GET_TIMEOUT`` past 30k tombstones, and the wildcard
    broadcast targets a single ``observation_id`` suffix — both are wrong
    primitives for a multi-id bulk path. Instead, for every matched obs we
    additionally exact-key delete the mirrored ``mem/tomb/...`` slot via
    :func:`execute_bulk_purge`, so any legitimate tombstones that happen to
    fall under the same ``pc_id`` are also cleaned up at O(1)/match.

    ``execute=False`` returns the match list + per-session histogram without
    issuing deletes (dry-run). ``progress_every`` / ``on_progress`` give the
    CLI a hook for streaming progress without coupling I/O into store.py.

    Convenience wrapper that runs both phases back-to-back. The CLI uses
    :func:`scan_obs_by_pc_id` + :func:`execute_bulk_purge` directly so it
    can interpose a user-confirm prompt between the two phases without
    paying for a second scan.
    """
    matches, sessions = scan_obs_by_pc_id(pc_id, session_prefix=session_prefix)
    if not execute or not matches:
        return BulkPurgeResult(matches=matches, sessions=sessions, executed=False)
    purged, tombs_purged, failures = execute_bulk_purge(
        matches,
        progress_every=progress_every,
        on_progress=on_progress,
    )
    return BulkPurgeResult(
        matches=matches,
        sessions=sessions,
        purged=purged,
        tombs_purged=tombs_purged,
        failures=failures,
        executed=True,
    )


def _gc_via_sqlite_index(idx: LocalIndex, cutoff_iso: str, project: str) -> int:
    """Project-scoped gc that drives deletes from the local SQLite index.

    Bypasses the global ``mem/tomb/**`` scan and the per-id Zenoh fallback
    that the legacy path pays. Each obs+tomb is removed by the exact key
    expression derived from the cached payload, so the cost is O(N) on
    the project-scoped subset (#32). Orphan tombstones (no obs row) are
    silently skipped — same semantic as the legacy path's
    ``log.warning('skip orphan tomb ... under project filter')`` branch.
    """
    purged = 0
    for obs_id, payload_json in idx.list_tombstoned_obs_in_project(project, cutoff_iso):
        try:
            obs = Observation.from_json(payload_json)
        except Exception as e:  # noqa: BLE001
            log.warning('gc fast-path skip malformed payload for %s: %s', obs_id, e)
            continue
        delete_key(obs.key_expr)
        delete_key(obs.tombstone_key_expr())
        idx.physical_delete(obs_id)
        purged += 1
    return purged


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

    Project-scoped path (``project != ''``) uses the local SQLite index
    (#32). gc is a batch operation, so correctness outranks the rebuild
    cost — we **always** ``rebuild_from_zenoh`` before the SQLite query
    rather than trusting that a non-empty sidecar reflects the full mesh.
    A previous ``row_count() == 0`` gate (codex review P1) silently
    missed older tombstones whenever the index had been partially
    populated by short-lived runs that did not align with zenoh. The
    rebuild is idempotent — repeat callers within the same process pay
    for two ``mem/{obs,tomb}/**`` scans, but no per-id Zenoh fallback,
    so total cost is still well below the legacy ``_list_tombstones``
    + per-id ``find_observation_by_id`` path on populated meshes.

    Returns:
        Count of tombstones purged. The matching observation key is also
        deleted when present, but the return value counts tomb sweeps only.
    """
    if retention_days < 0:
        raise ValueError(f'retention_days must be >= 0, got {retention_days}')
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    cutoff_iso = cutoff.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    if project:
        idx = get_index()
        if not idx.disabled:
            try:
                idx.rebuild_from_zenoh(get_session())
                return _gc_via_sqlite_index(idx, cutoff_iso, project)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    'gc project-scoped fast path failed (%s); falling through to global Zenoh scan',
                    e,
                )

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

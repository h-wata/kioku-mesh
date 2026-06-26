"""Observation store for kioku-mesh.

Responsibilities:
    - write path: ``put_observation`` / ``put_tombstone`` / delete broadcast
    - read path: ``search_observations`` / ``find_observation_by_id`` with the
      SQLite local index first and the legacy Zenoh scan as fallback
    - the local index handle lifecycle: ``get_index`` / ``_reset_index``
    - clamp ``limit`` to ``MAX_SEARCH`` (return-count cap, not scan cap)

Sibling modules extracted in #167, whose public surface this module
re-exports so ``store.<name>`` stays a valid entry point for callers and
tests:
    - ``transport.py``: session lifecycle, retry policy, transport status
    - ``pending_queue.py``: the failed-put queue and its drain worker
    - ``replication.py``: rebuild policy, key parsing, and the index
      subscriber that mirrors replicated PUT/DELETE into the local index
    - ``purge.py``: retention GC, shadow sweep, and pc-scoped bulk purge

Patching rule (#172): the re-exports are plain aliases, good for *calling*
only. Assigning or monkeypatching ``store.<name>`` does not propagate into
the owning module, so stub internals on the module that owns them
(kioku_mesh.transport`` for ``_open_session`` / ``_session`` /
``_RETRYABLE_EXC``, kioku_mesh.pending_queue`` for queue tunables, and so
on). Patching functions that still live here (``get_index``,
``find_observation_by_id``, ``_parse_iso``, ``get_session`` as an
attribute the siblings read) keeps working.
"""

from datetime import datetime
from datetime import timezone
import logging

from ..core.keyspace import find_by_id_selector
from ..core.keyspace import obs_id_from_key
from ..core.keyspace import obs_selector
from ..core.keyspace import tomb_selector
from ..core.models import Observation
from ..core.models import Tombstone
from ..core.transport import _iter_ok_replies
from ..core.transport import _now_iso_utc  # noqa: F401  (façade re-export, #167)
from ..core.transport import _open_session  # noqa: F401  (façade re-export, #167)
from ..core.transport import _record_pending_drain_success  # noqa: F401  (façade re-export, #167)
from ..core.transport import _record_put_result  # noqa: F401  (façade re-export, #167)
from ..core.transport import _reset_session  # noqa: F401  (façade re-export, #167)
from ..core.transport import _reset_transport_status
from ..core.transport import _RETRYABLE_EXC
from ..core.transport import _set_pending_drain_in_progress  # noqa: F401  (façade re-export, #167)
from ..core.transport import _set_zenoh_session_state  # noqa: F401  (façade re-export, #167)
from ..core.transport import get_session
from ..core.transport import GET_TIMEOUT  # noqa: F401  (façade re-export, #167)
from ..core.transport import get_transport_status  # noqa: F401  (façade re-export, #167)
from ..core.transport import is_mesh_ready  # noqa: F401  (façade re-export, #167)
from ..core.transport import mesh_ready_label  # noqa: F401  (façade re-export, #167)
from ..core.transport import QueryErrorReply  # noqa: F401  (façade re-export, #167)
from ..core.transport import TransportStatus  # noqa: F401  (façade re-export, #167)
from ..core.transport import with_retry
from .local_index import LocalIndex
from .pending_queue import _delete_pending_put
from .pending_queue import _enqueue_pending_put
from .pending_queue import _open_pending_puts_db  # noqa: F401  (façade re-export, #167)
from .pending_queue import drain_pending_puts
from .pending_queue import start_pending_drain_background  # noqa: F401  (façade re-export, #167)
from .pending_queue import stop_pending_drain_background  # noqa: F401  (façade re-export, #167)
from .purge import _broadcast_delete_best_effort  # noqa: F401  (façade re-export, #167)
from .purge import _gc_via_sqlite_index  # noqa: F401  (façade re-export, #167)
from .purge import _list_tombstones  # noqa: F401  (façade re-export, #167)
from .purge import _scan_obs_by_pc_id  # noqa: F401  (façade re-export, #167)
from .purge import bulk_purge_by_pc_id  # noqa: F401  (façade re-export, #167)
from .purge import BulkPurgeResult  # noqa: F401  (façade re-export, #167)
from .purge import delete_key  # noqa: F401  (façade re-export, #167)
from .purge import execute_bulk_purge  # noqa: F401  (façade re-export, #167)
from .purge import gc_expired_shadows  # noqa: F401  (façade re-export, #167)
from .purge import gc_expired_tombstones  # noqa: F401  (façade re-export, #167)
from .purge import physical_delete_observation  # noqa: F401  (façade re-export, #167)
from .purge import scan_obs_by_pc_id  # noqa: F401  (façade re-export, #167)
from .replication import _empty_index_rebuild_allowed
from .replication import _obs_id_from_key  # noqa: F401  (façade re-export, #167)
from .replication import _should_rebuild_on_init
from .replication import reset_rebuild_policy
from .replication import set_rebuild_on_init_default  # noqa: F401  (façade re-export, #167)
from .replication import set_rebuild_on_init_explicit  # noqa: F401  (façade re-export, #167)
from .replication import start_index_subscriber

log = logging.getLogger(__name__)

# Return-count cap for search APIs. Does NOT bound the underlying
# ``session.get()`` scan; callers must narrow the key_expr for large spaces.
MAX_SEARCH = 10_000

_index: LocalIndex | None = None
_subscribers: list | None = None


def get_index() -> LocalIndex:
    """Return the cached LocalIndex sidecar, opening it on first use.

    Since Phase 3 the index is the default read path for
    ``search_observations`` / ``find_observation_by_id``; the legacy
    Zenoh full-scan stays available as a fallback when the index is
    disabled (``KIOKU_MESH_DISABLE_INDEX=1``) — in that case the returned
    instance is a no-op so callers do not need to branch.

    Phase 4: on first init, optionally runs ``rebuild_from_zenoh`` then
    starts the replication subscriber. The rebuild gate is the policy
    returned by :func:`_should_rebuild_on_init` — long-lived processes
    (default ``True``) align once at startup, while one-shot CLI invocations
    skip the ~15s scan on a populated mesh (#38). Set
    ``KIOKU_MESH_FORCE_REBUILD=1`` to opt back in for a CLI run, or pass
    ``--rebuild`` to ``kioku-mesh``. Zenoh errors are logged and swallowed
    so a missing router cannot block reads/writes.

    Exception to the skip policy: an *empty* index always rebuilds, even when
    the policy says skip. A fresh spoke/install has nothing in SQLite yet while
    zenoh-rocksdb already holds replicated memories, so ``status``/``search``
    would otherwise report 0 until the next write. The #38 skip exists only to
    dodge the scan cost on a *populated* index — an empty one has nothing to
    lose — and this self-heals once: the next call sees rows and skips.
    """
    global _index, _subscribers
    if _index is None:
        _index = LocalIndex.connect()
        if not _index.disabled:
            try:
                session = get_session()
                rebuild = _should_rebuild_on_init()
                if not rebuild and _empty_index_rebuild_allowed():
                    counts = _index.visibility_counts()
                    if counts.live + counts.tombstoned + counts.shadowed == 0:
                        rebuild = True
                if rebuild:
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
    SQLite file under its own ``KIOKU_MESH_STATE_DIR`` tmp_path. Production
    code does not call this.

    Also restores ``_rebuild_on_init_default`` and the explicit override
    to their module defaults so a test that exercised the CLI (which
    flips the flag to False or sets the explicit override) does not leak
    that policy into the next test.
    """
    global _index
    stop_pending_drain_background()
    _reset_subscribers()
    if _index is not None:
        try:
            _index.close()
        except Exception:  # noqa: BLE001
            pass
    _index = None
    reset_rebuild_policy()
    _reset_transport_status()


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
    """Publish an observation to its canonical ``mem/obs/...`` key.

    On zenoh success, also upsert into the local SQLite sidecar index
    (Issue #7 plan B). Index errors are best-effort and do not raise so
    a corrupt index file cannot break a working put.

    On transport failure, the serialized observation is also queued in the
    local pending-puts SQLite DB so a later successful write can replay it.
    """
    payload_json = obs.to_json()
    try:
        get_session().put(obs.key_expr, payload_json)
    except _RETRYABLE_EXC:
        _enqueue_pending_put('observation', obs.key_expr, obs.observation_id, payload_json)
        raise
    _delete_pending_put(obs.key_expr)
    # Sidecar write happens AFTER the zenoh put so that a successful
    # zenoh-side write is the contract for callers. The index is a read-
    # acceleration layer; Phase 4 will reconcile it from zenoh on demand.
    get_index().upsert(obs)
    drain_pending_puts(wait=False)


@with_retry
def put_tombstone(obs: Observation, reason: str = '') -> None:
    """Publish a tombstone at the mirrored ``mem/tomb/...`` key for this observation.

    Also stamps ``deleted_at`` on the matching SQLite row when the index
    is enabled. A no-op on the index side when no row exists yet (the
    tombstone will be reconciled on Phase 4 rebuild).
    """
    tomb = Tombstone(observation_id=obs.observation_id, reason=reason)
    payload_json = tomb.to_json()
    key_expr = obs.tombstone_key_expr()
    try:
        get_session().put(key_expr, payload_json)
    except _RETRYABLE_EXC:
        _enqueue_pending_put('tombstone', key_expr, obs.observation_id, payload_json)
        raise
    _delete_pending_put(key_expr)
    # Keep the row live locally until the tombstone has actually reached the mesh.
    get_index().mark_deleted(obs.observation_id, tomb.deleted_at)
    drain_pending_puts(wait=False)


def search_observations(
    query: str = '',
    agent_family: str = '',
    client_id: str = '',
    pc_id: str = '',
    session_id: str = '',
    project: str = '',
    since_iso: str = '',
    until_iso: str = '',
    cursor_observation_id: str = '',
    limit: int = 50,
    include_superseded: bool = False,
    search_mode: str = 'and',
) -> list[Observation]:
    """Search observations via the SQLite local index, falling back to Zenoh.

    Phase 3 of TASK-131: routes through :class:`LocalIndex.search` by
    default (sub-100ms at 50k per TASK-134 spike). The legacy Zenoh full-
    scan path stays available behind ``KIOKU_MESH_DISABLE_INDEX=1`` so
    operators can flip back if the index is unavailable.

    ``limit`` defaults to 50, max 10000 (``MAX_SEARCH``). ``until_iso``
    is an inclusive upper bound on ``created_at``; pairing it with
    ``cursor_observation_id`` switches the bound to the strict
    ``(created_at, observation_id) < (until_iso, cursor_observation_id)``
    tuple comparison used by bulk-delete cursor pagination (#66).
    Tombstones hide the matching observation in both paths.
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
            until_iso=until_iso,
            cursor_observation_id=cursor_observation_id,
            limit=limit,
            include_superseded=include_superseded,
            search_mode=search_mode,
        )
    return _search_via_zenoh(
        query=query,
        agent_family=agent_family,
        client_id=client_id,
        pc_id=pc_id,
        session_id=session_id,
        project=project,
        since_iso=since_iso,
        until_iso=until_iso,
        cursor_observation_id=cursor_observation_id,
        limit=limit,
        search_mode=search_mode,
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
    until_iso: str = '',
    cursor_observation_id: str = '',
    limit: int,
    search_mode: str = 'and',
) -> list[Observation]:
    """Legacy Zenoh full-scan search path, retained as a fallback.

    Identical semantics to the pre-Phase-3 ``search_observations`` body:
    narrow by key_expr, then filter project / since / until / query
    (substring on content / project / tags) in Python. Re-enabled by
    setting ``KIOKU_MESH_DISABLE_INDEX=1``. ``cursor_observation_id``
    triggers the same strict-tuple cursor semantics as
    :meth:`LocalIndex.search` so bulk-delete pagination keeps working
    on the fallback path too (#66).
    ``search_mode`` is propagated: 'or'/'and_or' uses OR across content /
    project / tags substrings; 'and' uses AND (existing behaviour).
    """
    since_dt = _parse_iso(since_iso)
    until_dt = _parse_iso(until_iso)

    # ADR-0019 Phase A: selectors cover legacy + tiered namespaces.
    key_expr = obs_selector(agent_family, client_id, pc_id, session_id)
    tomb_expr = tomb_selector(agent_family, client_id, pc_id, session_id)

    session = get_session()

    tombs: set[str] = set()
    for ok in _iter_ok_replies(session, tomb_expr):
        # Canonical-key gate (Codex review on PR #177): the broadened
        # selector can match non-canonical keys; only a well-formed tomb
        # key may hide an observation.
        tomb_id = obs_id_from_key(str(ok.key_expr))
        if tomb_id is not None:
            tombs.add(tomb_id)

    q = query.lower()
    use_or = search_mode in ('or', 'and_or')
    # Use a dict keyed by observation_id so multiple Zenoh storages replying
    # with the same observation (multi-router / replication overlap) do not
    # produce duplicates (#12). Last-writer-wins within a single GET scan.
    results_by_id: dict[str, Observation] = {}
    for ok in _iter_ok_replies(session, key_expr):
        key_id = obs_id_from_key(str(ok.key_expr))
        if key_id is None:
            log.debug('skip non-canonical obs key in fallback scan: %s', ok.key_expr)
            continue
        try:
            obs = Observation.from_json(ok.payload.to_string())
        except Exception as e:  # noqa: BLE001
            log.warning('skip malformed payload at %s: %s', ok.key_expr, e)
            continue
        if obs.observation_id != key_id:
            log.debug('skip key/payload id mismatch in fallback scan: %s', ok.key_expr)
            continue
        if obs.observation_id in tombs:
            continue
        if project and obs.project != project:
            continue
        if since_dt or until_dt:
            obs_dt = _parse_iso(obs.created_at)
            if obs_dt is None:
                continue
            if since_dt and obs_dt < since_dt:
                continue
            if until_dt:
                if cursor_observation_id:
                    # Strict tuple cursor for paginated bulk-delete (#66).
                    if obs_dt > until_dt:
                        continue
                    if obs_dt == until_dt and obs.observation_id >= cursor_observation_id:
                        continue
                elif obs_dt > until_dt:
                    continue
        if q:
            if use_or:
                terms = q.split()
                if not any(
                    t in obs.content.lower() or t in obs.project.lower() or any(t in tag.lower() for tag in obs.tags)
                    for t in terms
                ):
                    continue
            elif (
                q not in obs.content.lower()
                and q not in obs.project.lower()
                and not any(q in t.lower() for t in obs.tags)
            ):
                continue
        results_by_id[obs.observation_id] = obs

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    # Sort by (created_at, observation_id) DESC tuple so the bulk-delete
    # cursor sees the same stable order as the SQLite path (#66). Without
    # the secondary key, ties on a boundary timestamp would shuffle
    # between calls and the next page could skip or re-emit rows.
    return sorted(
        results_by_id.values(),
        key=lambda o: (_parse_iso(o.created_at) or epoch, o.observation_id),
        reverse=True,
    )[:limit]


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


def _is_valid_observation_id(observation_id: str) -> bool:
    """Return True when ``observation_id`` is a 32-character hex string."""
    if len(observation_id) != 32:
        return False
    try:
        int(observation_id, 16)
    except ValueError:
        return False
    return True


@with_retry
def _find_by_id_via_zenoh(observation_id: str) -> Observation | None:
    """Query Zenoh by leaf id, retained for fallback / index-miss."""
    if not _is_valid_observation_id(observation_id):
        return None
    session = get_session()
    for ok in _iter_ok_replies(session, find_by_id_selector(observation_id)):
        if obs_id_from_key(str(ok.key_expr)) != observation_id:
            continue
        try:
            obs = Observation.from_json(ok.payload.to_string())
        except Exception:  # noqa: BLE001
            continue
        if obs.observation_id == observation_id:
            return obs
    return None

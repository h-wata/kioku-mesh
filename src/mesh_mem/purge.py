"""GC and bulk purge for kioku-mesh (extracted from ``store.py``, #167).

Named ``purge`` (not ``gc``) so the module does not shadow the stdlib
``gc`` module (ruff A005).

Owns every path that physically removes data from the mesh and the local
SQLite index:

    - ``delete_key`` / ``_broadcast_delete_best_effort``: the exact-key and
      best-effort wildcard Zenoh deletes the other paths build on
    - ``physical_delete_observation``: single-id emergency purge
      (``gc --force-id``)
    - ``scan_obs_by_pc_id`` / ``execute_bulk_purge`` /
      ``bulk_purge_by_pc_id``: pc-scoped mass cleanup (#66)
    - ``gc_expired_tombstones``: retention sweep, with the project-scoped
      SQLite fast path (#32) and the legacy global ``mem/tomb/**`` scan
    - ``gc_expired_shadows``: the rebuild-shadow → retention →
      physical-delete lifecycle (ADR-0011, Issue #70)

Collaborators that live in ``store`` (the session accessor, the index
handle, ``find_observation_by_id``, ``_parse_iso``) are resolved at call
time via :func:`_store` — the same lazy pattern as ``pending_queue`` /
``replication`` — so patching those *functions* on ``store`` keeps
affecting gc behavior and no sibling module is imported at load time.
Transport-owned internals (``_open_session``, ``_RETRYABLE_EXC``, the
``with_retry`` machinery) must be patched on ``mesh_mem.transport``
instead (#172). ``store`` re-exports this module's surface, so
``store.gc_expired_tombstones`` and friends remain valid entry points.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
from types import ModuleType

from .keyspace import OBS_READ_KEY_EXPR
from .local_index import LocalIndex
from .models import Observation
from .models import Tombstone
from .transport import _iter_ok_replies
from .transport import _RETRYABLE_EXC
from .transport import with_retry

log = logging.getLogger(__name__)


def _store() -> ModuleType:
    """Return the ``store`` module, resolved lazily to avoid import cycles.

    Looked up at call time (not import time) so test monkeypatches on
    ``store`` attributes — ``get_session`` / ``get_index`` /
    ``find_observation_by_id`` — stay effective for gc internals.
    """
    from . import store

    return store


@with_retry
def delete_key(key_expr: str) -> None:
    """Physically remove a key from the mesh via ``session.delete``.

    Callers pass an exact key expression, not a wildcard — storage-backend
    support for wildcard delete is inconsistent across Zenoh versions, so
    the gc paths enumerate first and then delete one key at a time.
    """
    _store().get_session().delete(key_expr)


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
        _store().get_session().delete(key_expr)
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
    session = _store().get_session()
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
    store = _store()
    obs = store.find_observation_by_id(observation_id)
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
    store.get_index().physical_delete(observation_id)

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
    session = _store().get_session()
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
    store = _store()
    session = store.get_session()
    idx = store.get_index()
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
    CLI a hook for streaming progress without coupling I/O into the gc path.

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

    store = _store()
    if project:
        idx = store.get_index()
        if not idx.disabled:
            try:
                idx.rebuild_from_zenoh(store.get_session())
                return _gc_via_sqlite_index(idx, cutoff_iso, project)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    'gc project-scoped fast path failed (%s); falling through to global Zenoh scan',
                    e,
                )

    purged = 0
    for tomb_key, tomb in _list_tombstones():
        deleted_dt = store._parse_iso(tomb.deleted_at)  # noqa: SLF001 — store collaborator, see _store()
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
            obs = store.find_observation_by_id(tomb.observation_id)
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
        store.get_index().physical_delete(tomb.observation_id)
        purged += 1
    return purged


def gc_expired_shadows(
    retention_days: int = 30,
    now: datetime | None = None,
    project: str = '',
) -> tuple[int, int]:
    """Sweep shadow rows older than the cutoff, re-verifying each against Zenoh.

    Shadows are rows that :meth:`LocalIndex.rebuild_from_zenoh` flagged
    because the row existed locally but did not appear in the Zenoh scan
    at that moment (ADR-0011, Issue #70). The shadow is a *guess about
    the past*, not a fact about now — a peer may have rejoined since,
    or a transient storage gap may have healed. So before physically
    deleting a long-shadowed row we re-query the live Zenoh state and
    branch:

    - ``obs_id`` still observable upstream → ``upsert`` the live obs,
      which clears ``shadowed_at`` (false-shadow recovery).
    - ``obs_id`` genuinely absent upstream → ``physical_delete`` the
      local row, completing the rebuild-shadow → retention →
      physical-delete lifecycle.

    This function operates only on rows that **already carry**
    ``shadowed_at``. Driving the *discovery* path — turning a stale-
    but-not-yet-shadowed live row into a shadow candidate — is the
    caller's responsibility, typically by running
    :meth:`LocalIndex.rebuild_from_zenoh` first. The CLI ``_cmd_gc``
    driver does that explicitly before invoking this function so that
    one-shot ``kioku-mesh gc`` (which skips startup rebuild per #38)
    still reaches the discovery branch.

    If the live Zenoh query fails the sweep is skipped entirely
    (returns ``(0, 0)``) — never delete on transport ambiguity.

    ``project`` mirrors the tombstone sweep filter.

    Returns ``(purged, revived)``.
    """
    if retention_days < 0:
        raise ValueError(f'retention_days must be >= 0, got {retention_days}')
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    cutoff_iso = cutoff.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    store = _store()
    idx = store.get_index()
    if idx.disabled:
        return (0, 0)
    candidate_ids = idx.list_expired_shadowed_obs(cutoff_iso, project=project)
    if not candidate_ids:
        return (0, 0)
    candidate_set = set(candidate_ids)

    # Live re-verify. Conservative on transport failure: skip the whole
    # sweep so we never destroy a row whose upstream state we did not
    # actually confirm to be absent.
    zenoh_obs_by_id: dict[str, Observation] = {}
    try:
        session = store.get_session()
        # ADR-0019 Phase A: re-verify against legacy + tiered namespaces so a
        # row that lives under mem/{mesh,user,team}/... is not wrongly purged.
        for reply in session.get(OBS_READ_KEY_EXPR, timeout=30.0):  # type: ignore[attr-defined]
            if not reply.ok:
                continue
            try:
                obs = Observation.from_json(reply.ok.payload.to_string())
            except Exception as e:  # noqa: BLE001
                log.warning('gc_expired_shadows skip malformed obs payload: %s', e)
                continue
            if obs.observation_id in candidate_set:
                zenoh_obs_by_id[obs.observation_id] = obs
    except Exception as e:  # noqa: BLE001
        log.warning('gc_expired_shadows Zenoh re-verify failed, skipping sweep: %s', e)
        return (0, 0)

    purged = 0
    revived = 0
    for obs_id in candidate_ids:
        obs = zenoh_obs_by_id.get(obs_id)
        if obs is not None:
            # Upsert revives the row: ON CONFLICT sets shadowed_at = NULL.
            idx.upsert(obs)
            revived += 1
        else:
            idx.physical_delete(obs_id)
            purged += 1
    return (purged, revived)

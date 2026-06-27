"""Backend abstraction for kioku-mesh.

``get_backend()`` returns either a LocalBackend (SQLite-only, no zenohd) or a
ZenohBackend (existing store.py) based on ``config.get_backend_mode()``.

LocalBackend is the on-ramp: single-machine persistent mode that works
without zenohd on PATH.  ZenohBackend wraps the existing store functions so
CLI and MCP server code paths are identical for both modes.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging
from typing import Protocol, runtime_checkable

from ..core.models import Observation
from ..core.models import Tombstone

log = logging.getLogger(__name__)

_backend_cache: 'MemoryBackend | None' = None


@dataclasses.dataclass(frozen=True)
class BackendStatus:
    mode: str
    live: int = 0
    tombstoned: int = 0
    shadowed: int = 0
    zenoh_session: str = 'n/a'
    last_put_at_iso: str = ''
    last_put_status: str = 'n/a'
    pending_puts: int = 0


@runtime_checkable
class MemoryBackend(Protocol):
    """Common surface for all kioku-mesh backends."""

    def put_observation(self, obs: Observation) -> None: ...

    def put_tombstone(self, obs: Observation, reason: str = '') -> None: ...

    def search_observations(
        self,
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
    ) -> list[Observation]: ...

    def find_observation_by_id(self, observation_id: str) -> Observation | None: ...

    def find_supersede_candidates(self, obs: Observation) -> list[Observation]: ...

    def physical_delete_observation(self, observation_id: str) -> tuple[bool, bool]: ...

    def get_status(self) -> BackendStatus: ...

    def drain_pending(self, limit: int | None = None, *, wait: bool = True) -> int: ...

    def gc_tombstones(self, retention_days: int = 30, project: str = '') -> int: ...

    def gc_shadows(self, retention_days: int = 30, project: str = '') -> tuple[int, int]: ...

    def close(self) -> None: ...


class LocalBackend:
    """SQLite-only backend. No zenohd required.

    Uses a dedicated index path (``state_dir()/local/index.db``) that is
    physically separate from the Zenoh sidecar index (``state_dir()/index.db``).
    This prevents ``rebuild_from_zenoh()`` from shadowing local-only rows when
    the user switches backend from ``local`` to ``zenoh`` (B2 fix).
    """

    def __init__(self) -> None:
        from ..core.identity import state_dir
        from .local_index import LocalIndex
        from .local_raw_store import LocalRawStore

        local_dir = state_dir() / 'local'
        local_dir.mkdir(parents=True, exist_ok=True)
        self._raw_store = LocalRawStore(local_dir / 'raw.db')
        self._raw_store.migrate_from_index(local_dir / 'index.db')
        self._idx = LocalIndex.connect(str(local_dir / 'index.db'))
        self._idx.rebuild_from_raw_records(self._raw_store.scan_obs(), self._raw_store.scan_tombs())

    def put_observation(self, obs: Observation) -> None:
        self._raw_store.put_obs(obs)  # SoT: raises on failure; index not touched
        self._idx.upsert(obs)  # best-effort (LocalIndex.upsert swallows errors)

    def put_tombstone(self, obs: Observation, reason: str = '') -> None:
        tomb = Tombstone(observation_id=obs.observation_id, reason=reason)
        self._raw_store.put_tomb(tomb)  # SoT: raises on failure; index not touched
        self._idx.mark_deleted(obs.observation_id, tomb.deleted_at)  # best-effort

    def search_observations(
        self,
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
        return self._idx.search(
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

    def find_observation_by_id(self, observation_id: str) -> Observation | None:
        return self._idx.find_by_id(observation_id, include_deleted=True)

    def find_supersede_candidates(self, obs: Observation) -> list[Observation]:
        from .supersede import find_candidates_in_index

        return find_candidates_in_index(self._idx, obs)

    def physical_delete_observation(self, observation_id: str) -> tuple[bool, bool]:
        # raw.db is authoritative: check it directly, independent of index state.
        raw_exists = self._raw_store.obs_exists(observation_id)
        idx_obs = self._idx.find_by_id(observation_id, include_deleted=True)
        if not raw_exists and idx_obs is None:
            return (False, False)
        self._raw_store.delete_obs(observation_id)  # SoT: raises on failure
        self._raw_store.delete_tomb(observation_id)  # SoT: raises on failure
        self._idx.physical_delete(observation_id)  # best-effort
        return (True, False)

    def get_status(self) -> BackendStatus:
        counts = self._idx.visibility_counts()
        return BackendStatus(
            mode='local',
            live=counts.live,
            tombstoned=counts.tombstoned,
            shadowed=counts.shadowed,
        )

    def drain_pending(self, limit: int | None = None, *, wait: bool = True) -> int:
        return 0

    def gc_tombstones(self, retention_days: int = 30, project: str = '') -> int:
        if retention_days < 0:
            raise ValueError(f'retention_days must be >= 0, got {retention_days}')
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        cutoff_iso = cutoff.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        purged = 0
        if project:
            # Enumerate from raw.db (authoritative) and index (best-effort); union.
            candidate_ids: set[str] = set()
            for obs_id, obs_project in self._raw_store.scan_expired_tomb_ids_with_project(cutoff_iso):
                if obs_project == project:
                    candidate_ids.add(obs_id)
            for obs_id, _ in self._idx.list_tombstoned_obs_in_project(project, cutoff_iso):
                candidate_ids.add(obs_id)
            for obs_id in candidate_ids:
                self._raw_store.delete_obs(obs_id)
                self._raw_store.delete_tomb(obs_id)
                self._idx.physical_delete(obs_id)
                purged += 1
        else:
            purged = self._gc_global_tombs(cutoff_iso)
        return purged

    def _gc_global_tombs(self, cutoff_iso: str) -> int:
        """Physical-delete tombstoned rows older than cutoff_iso across all projects."""
        import sqlite3

        # Collect from raw.db (authoritative) and index (best-effort); union.
        candidate_ids: set[str] = {
            obs_id for obs_id, _ in self._raw_store.scan_expired_tomb_ids_with_project(cutoff_iso)
        }
        if not (self._idx.disabled or self._idx._conn is None):  # noqa: SLF001
            with self._idx._lock:  # noqa: SLF001
                try:
                    rows = self._idx._conn.execute(  # noqa: SLF001
                        'SELECT observation_id FROM obs_index WHERE deleted_at IS NOT NULL AND deleted_at < ?',
                        (cutoff_iso,),
                    ).fetchall()
                    for (obs_id,) in rows:
                        candidate_ids.add(obs_id)
                except sqlite3.Error as e:
                    log.warning('LocalBackend._gc_global_tombs query failed: %s', e)
        purged = 0
        for obs_id in candidate_ids:
            self._raw_store.delete_obs(obs_id)
            self._raw_store.delete_tomb(obs_id)
            self._idx.physical_delete(obs_id)
            purged += 1
        return purged

    def gc_shadows(self, retention_days: int = 30, project: str = '') -> tuple[int, int]:
        if self._idx.disabled:
            return (0, 0)
        if retention_days < 0:
            raise ValueError(f'retention_days must be >= 0, got {retention_days}')
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        cutoff_iso = cutoff.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        candidate_ids = self._idx.list_expired_shadowed_obs(cutoff_iso, project=project)
        purged = 0
        for obs_id in candidate_ids:
            self._raw_store.delete_obs(obs_id)
            self._idx.physical_delete(obs_id)
            purged += 1
        return (purged, 0)

    def close(self) -> None:
        self._idx.close()
        self._raw_store.close()


class ZenohBackend:
    """Thin wrapper around the existing store.py functions."""

    def put_observation(self, obs: Observation) -> None:
        from . import store

        store.put_observation(obs)

    def put_tombstone(self, obs: Observation, reason: str = '') -> None:
        from . import store

        store.put_tombstone(obs, reason=reason)

    def search_observations(
        self,
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
        from . import store

        return store.search_observations(
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
            include_superseded=include_superseded,
            search_mode=search_mode,
        )

    def find_observation_by_id(self, observation_id: str) -> Observation | None:
        from . import store

        return store.find_observation_by_id(observation_id)

    def find_supersede_candidates(self, obs: Observation) -> list[Observation]:
        from . import store
        from .supersede import find_candidates_in_index

        return find_candidates_in_index(store.get_index(), obs)

    def physical_delete_observation(self, observation_id: str) -> tuple[bool, bool]:
        from . import store

        return store.physical_delete_observation(observation_id)

    def get_status(self) -> BackendStatus:
        from . import store
        from .local_index import VisibilityCounts

        t = store.get_transport_status()
        try:
            idx = store.get_index()
            counts = idx.visibility_counts()
        except Exception:  # noqa: BLE001
            counts = VisibilityCounts()
        return BackendStatus(
            mode='zenoh',
            live=counts.live,
            tombstoned=counts.tombstoned,
            shadowed=counts.shadowed,
            zenoh_session=t.zenoh_session,
            last_put_at_iso=t.last_put_at_iso,
            last_put_status=t.last_put_status,
            pending_puts=t.pending_puts,
        )

    def drain_pending(self, limit: int | None = None, *, wait: bool = True) -> int:
        from . import store

        return store.drain_pending_puts(limit=limit, wait=wait)

    def gc_tombstones(self, retention_days: int = 30, project: str = '') -> int:
        from . import store

        return store.gc_expired_tombstones(retention_days=retention_days, project=project)

    def gc_shadows(self, retention_days: int = 30, project: str = '') -> tuple[int, int]:
        from . import store

        return store.gc_expired_shadows(retention_days=retention_days, project=project)

    def close(self) -> None:
        from .store import _reset_session  # noqa: PLC2701

        _reset_session()


def get_backend() -> MemoryBackend:
    """Return the cached backend instance, creating it on first call."""
    global _backend_cache
    if _backend_cache is None:
        from ..core.config import get_backend_mode

        mode = get_backend_mode()
        if mode == 'local':
            _backend_cache = LocalBackend()
        else:
            _backend_cache = ZenohBackend()
    return _backend_cache


def reset_backend() -> None:
    """Drop the cached backend. Test-only helper."""
    global _backend_cache
    if _backend_cache is not None:
        try:
            _backend_cache.close()
        except Exception:  # noqa: BLE001
            pass
    _backend_cache = None

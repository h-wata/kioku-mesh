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
    ) -> list[Observation]: ...
    def find_observation_by_id(self, observation_id: str) -> Observation | None: ...
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

        local_dir = state_dir() / 'local'
        local_dir.mkdir(parents=True, exist_ok=True)
        self._idx = LocalIndex.connect(str(local_dir / 'index.db'))

    def put_observation(self, obs: Observation) -> None:
        self._idx.upsert(obs)

    def put_tombstone(self, obs: Observation, reason: str = '') -> None:
        tomb = Tombstone(observation_id=obs.observation_id, reason=reason)
        self._idx.mark_deleted(obs.observation_id, tomb.deleted_at)

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
        )

    def find_observation_by_id(self, observation_id: str) -> Observation | None:
        return self._idx.find_by_id(observation_id, include_deleted=True)

    def physical_delete_observation(self, observation_id: str) -> tuple[bool, bool]:
        obs = self._idx.find_by_id(observation_id, include_deleted=True)
        if obs is None:
            return (False, False)
        # Check if it was already tombstoned (has deleted_at in payload is not reliable;
        # use find_by_id include_deleted to detect existence, then physical_delete).
        self._idx.physical_delete(observation_id)
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
            for obs_id, _ in self._idx.list_tombstoned_obs_in_project(project, cutoff_iso):
                self._idx.physical_delete(obs_id)
                purged += 1
        else:
            # Global sweep: search with include_deleted and purge old tombs.
            rows = self._idx.search(include_deleted=True, limit=10_000)
            for obs in rows:
                # We can't directly query deleted_at from search; use the DB directly.
                # For now, fall back to project-scoped path with empty string not supported well.
                # Use the internal connection via list_tombstoned_obs_in_project with empty project.
                break
            # Use a direct query approach for the global case.
            purged = self._gc_global_tombs(cutoff_iso)
        return purged

    def _gc_global_tombs(self, cutoff_iso: str) -> int:
        """Physical-delete tombstoned rows older than cutoff_iso across all projects."""
        if self._idx.disabled or self._idx._conn is None:  # noqa: SLF001
            return 0
        import sqlite3

        with self._idx._lock:  # noqa: SLF001
            try:
                rows = self._idx._conn.execute(  # noqa: SLF001
                    'SELECT observation_id FROM obs_index WHERE deleted_at IS NOT NULL AND deleted_at < ?',
                    (cutoff_iso,),
                ).fetchall()
            except sqlite3.Error as e:
                log.warning('LocalBackend._gc_global_tombs query failed: %s', e)
                return 0
        purged = 0
        for (obs_id,) in rows:
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
            self._idx.physical_delete(obs_id)
            purged += 1
        return (purged, 0)

    def close(self) -> None:
        self._idx.close()


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
        )

    def find_observation_by_id(self, observation_id: str) -> Observation | None:
        from . import store

        return store.find_observation_by_id(observation_id)

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

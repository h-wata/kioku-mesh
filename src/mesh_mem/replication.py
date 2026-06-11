"""Replication ingest + rebuild policy for kioku-mesh (extracted from ``store.py``, #167).

Owns the pieces that keep the SQLite local index aligned with Zenoh
replication:

    - **rebuild policy**: when ``get_index`` first opens the sidecar, should
      it scan ``mem/**`` from Zenoh to backfill the index? Long-lived
      processes do; one-shot CLI invocations skip it on a populated mesh
      (#38). The resolved policy is computed here from explicit overrides,
      env vars, and a module default.
    - **key parsing**: ``_obs_id_from_key`` validates that a Zenoh key is a
      canonical ``mem/{obs|tomb}/.../{observation_id}`` shape before any
      DELETE is mirrored into the index (#64).
    - **replication subscriber**: ``start_index_subscriber`` declares the
      obs / tomb subscribers (legacy + ADR-0019 tiered namespaces) whose
      callbacks mirror replicated PUT/DELETE samples into the local index.

The index handle itself (``get_index`` / ``_index`` / ``_subscribers``) and
its reset path stay in ``store`` because they own that module state and the
test suite pokes it directly. ``start_index_subscriber`` resolves
``get_index`` at call time through :func:`_store` (the same lazy pattern as
``pending_queue``) so it does not import ``store`` at module load and test
monkeypatches on ``store.get_index`` still take effect. ``store`` re-exports
this module's surface, so ``store.set_rebuild_on_init_default`` /
``store.start_index_subscriber`` / ``store._obs_id_from_key`` remain valid.
"""

import json
import logging
import os
from types import ModuleType
from typing import Any

import zenoh

from .keyspace import obs_id_from_key
from .keyspace import OBS_READ_KEY_EXPR
from .keyspace import TOMB_READ_KEY_EXPR
from .models import Observation
from .models import Tombstone

log = logging.getLogger(__name__)

# Default rebuild-on-init policy. Long-lived processes (kioku-mesh-mcp) keep
# the default ``True`` so the local SQLite index aligns with zenoh once at
# startup. One-shot CLI invocations call ``set_rebuild_on_init_default(False)``
# from ``__main__.main`` so each ``kioku-mesh save/search/...`` does not pay
# the rebuild_from_zenoh cost on a populated mesh (#38). Env vars
# MESH_MEM_FORCE_REBUILD=1 and MESH_MEM_SKIP_REBUILD=1 override this default,
# and an explicit ``set_rebuild_on_init_explicit(True/False)`` (the CLI's
# ``--rebuild`` flag) outranks both env vars.
_rebuild_on_init_default: bool = True
_rebuild_explicit_override: bool | None = None


def _store() -> ModuleType:
    """Return the ``store`` module, resolved lazily to avoid import cycles.

    Looked up at call time (not import time) so ``start_index_subscriber``
    picks up the same ``get_index`` that callers and test monkeypatches see
    on the ``store`` module.
    """
    from . import store

    return store


def set_rebuild_on_init_default(rebuild: bool) -> None:
    """Override the default rebuild-on-first-init policy in this process.

    Lowest-priority signal: env vars and the explicit override outrank
    this. Reset by ``store._reset_index()`` (test path) via
    :func:`reset_rebuild_policy`; production CLI calls this once from
    ``main`` before any ``get_index()`` invocation.
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


def reset_rebuild_policy() -> None:
    """Restore the rebuild policy to its module defaults.

    Called by ``store._reset_index()`` (test path) so a test that exercised
    the CLI — which flips the default to False or sets the explicit override
    — does not leak that policy into the next test.
    """
    global _rebuild_on_init_default, _rebuild_explicit_override
    _rebuild_on_init_default = True
    _rebuild_explicit_override = None


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


def _empty_index_rebuild_allowed() -> bool:
    """Whether a fresh (zero-row) index may force a one-time rebuild.

    Even when :func:`_should_rebuild_on_init` resolved to ``False``, this
    backfills a newly provisioned spoke whose zenoh-rocksdb already holds
    replicated rows but whose SQLite index is still empty, so ``status`` /
    ``search`` do not report 0 until the next write. It overrides only the
    *implicit* CLI default-skip (#38); an explicit opt-out
    (``set_rebuild_on_init_explicit(False)`` or ``MESH_MEM_SKIP_REBUILD=1``)
    is honored and suppresses the auto-rebuild.
    """
    if _rebuild_explicit_override is False:
        return False
    if os.environ.get('MESH_MEM_SKIP_REBUILD', '').strip() == '1':
        return False
    return True


# Conservative canonical-key parser (Issue #64), now namespace-aware
# (ADR-0019 Phase A). Kept under its historical name so the store façade
# re-export and existing callers/tests stay valid.
_obs_id_from_key = obs_id_from_key


def start_index_subscriber(session: zenoh.Session) -> list:
    """Subscribe to obs / tomb keys (legacy + tiered) to keep SQLite in sync.

    ADR-0019 Phase A: the subscriptions use ``mem/**/obs/**`` /
    ``mem/**/tomb/**`` so PUT/DELETE samples published under the
    visibility-tiered namespaces (``mem/mesh/...``, ``mem/user/{id}/...``,
    ``mem/team/{id}/...``) by newer peers are mirrored into the local
    index exactly like legacy ``mem/obs/**`` traffic.

    Callbacks are idempotent (upsert / mark_deleted / physical_delete) so
    overlap with the rebuild scan on startup is safe. PUT-kind samples
    carry an Observation / Tombstone payload; DELETE-kind samples (issued
    by ``session.delete``) carry an empty payload and only the key, so the
    callbacks dispatch on ``sample.kind`` and mirror the upstream delete
    into the local SQLite index (Issue #64). The returned list holds the
    two zenoh.Subscriber objects; callers must call undeclare() on each
    when tearing down (handled by _reset_subscribers / _reset_index).
    """
    idx = _store().get_index()
    if idx.disabled:
        return []

    def on_obs(sample: Any) -> None:
        if sample.kind == zenoh.SampleKind.DELETE:
            obs_id = _obs_id_from_key(str(sample.key_expr))
            if obs_id is None:
                log.debug(
                    'index subscriber on_obs DELETE ignored (invalid obs_id in key %s)',
                    sample.key_expr,
                )
                return
            try:
                idx.physical_delete(obs_id)
            except Exception as e:  # noqa: BLE001
                log.warning('index subscriber on_obs DELETE failed for %s: %s', obs_id, e)
            return
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
        if sample.kind == zenoh.SampleKind.DELETE:
            # A tombstone DELETE upstream means the tomb (and its mirrored
            # obs) was physically purged — typically by retention gc or by
            # ``execute_bulk_purge``. Mirror that into the index so this
            # peer also drops the row instead of carrying a stale live or
            # tombstoned record.
            obs_id = _obs_id_from_key(str(sample.key_expr))
            if obs_id is None:
                log.debug(
                    'index subscriber on_tomb DELETE ignored (invalid obs_id in key %s)',
                    sample.key_expr,
                )
                return
            try:
                idx.physical_delete(obs_id)
            except Exception as e:  # noqa: BLE001
                log.warning('index subscriber on_tomb DELETE failed for %s: %s', obs_id, e)
            return
        try:
            tomb = Tombstone.from_json(sample.payload.to_string())
            idx.mark_deleted(tomb.observation_id, tomb.deleted_at)
        except json.JSONDecodeError as e:
            log.debug('index subscriber on_tomb non-JSON payload: %s', e)
        except Exception as e:  # noqa: BLE001
            log.warning('index subscriber on_tomb error: %s', e)

    sub_obs = session.declare_subscriber(OBS_READ_KEY_EXPR, on_obs)
    sub_tomb = session.declare_subscriber(TOMB_READ_KEY_EXPR, on_tomb)
    return [sub_obs, sub_tomb]

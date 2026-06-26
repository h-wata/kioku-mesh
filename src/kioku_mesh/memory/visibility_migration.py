"""Visibility migration tooling for ADR-0019 Phase C.

Moves legacy mem/obs/... and mem/tomb/... keys into explicit visibility
namespaces (user/team/mesh) using copy-verify-delete-repair ordering with
mandatory backup/checkpoint.

Migration sequence: backup -> PUT target -> verify -> DELETE legacy (exact
key) -> repair PUT target -> local index rebuild.

The subscriber's DELETE callback operates on observation_id scope, so a
plain put-old/delete-old sequence would delete the newly indexed row when
the source DELETE arrives. The post-delete repair PUT forces reindexing.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from ..core.config import get_team_id
from ..core.config import get_user_id
from ..core.keyspace import obs_id_from_key
from ..core.keyspace import tomb_key
from ..core.keyspace import validate_scope_slug
from ..core.models import Observation
from ..core.transport import _iter_ok_replies

log = logging.getLogger(__name__)

MAX_BATCH_SIZE = 10_000


@dataclass(frozen=True)
class MigrationTarget:
    visibility: str
    scope_id: str
    display: str


@dataclass
class RawLegacyRecord:
    kind: Literal['obs', 'tomb']
    key: str
    payload: str


@dataclass
class MigrationItem:
    kind: Literal['obs', 'tomb']
    observation_id: str
    old_key: str
    new_key: str
    original_payload: str
    new_payload: str


@dataclass
class MigrationConflict:
    kind: Literal['obs', 'tomb']
    observation_id: str
    old_key: str
    new_key: str
    existing_payload: str
    incoming_payload: str


@dataclass
class MigrationPlan:
    target: MigrationTarget
    items: list[MigrationItem] = field(default_factory=list)
    conflicts: list[MigrationConflict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class MigrationCheckpoint:
    version: int
    run_id: str
    params: dict[str, str]
    target: dict[str, str]
    started_at: str
    updated_at: str
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    params_hash: str = field(default='')


@dataclass
class MigrationResult:
    planned: int
    copied: int
    verified: int
    deleted: int
    repair_put: int
    conflicts: int
    failures: int
    backup_dir: Path
    checkpoint: Path


def parse_migration_target(raw: str) -> MigrationTarget:
    """Parse --to argument into a MigrationTarget.

    Accepts: mesh | user | team | team/<team_id>
    Rejects: user/<id> (user_id must be config-resolved), legacy, unknown.
    """
    raw = raw.strip()
    if raw == 'mesh':
        return MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    if raw == 'user':
        user_id = get_user_id()
        if not user_id:
            raise ValueError(
                "visibility 'user' requires user_id: set KIOKU_MESH_USER_ID or add "
                "'user_id: <slug>' to ~/.config/kioku-mesh/config.yaml"
            )
        validate_scope_slug('user', user_id)
        return MigrationTarget(visibility='user', scope_id=user_id, display=f'user/{user_id}')
    if raw == 'team':
        team_id = get_team_id()
        if not team_id:
            raise ValueError(
                "visibility 'team' requires team_id: set KIOKU_MESH_TEAM_ID or add "
                "'team_id: <slug>' to .kioku-mesh.yaml"
            )
        validate_scope_slug('team', team_id)
        return MigrationTarget(visibility='team', scope_id=team_id, display=f'team/{team_id}')
    if raw.startswith('team/'):
        explicit_id = raw[len('team/') :]
        if not explicit_id:
            raise ValueError(f'invalid --to {raw!r}: team_id segment cannot be empty')
        validate_scope_slug('team', explicit_id)
        return MigrationTarget(visibility='team', scope_id=explicit_id, display=raw)
    if raw.startswith('user/'):
        raise ValueError(
            f'--to {raw!r} is not accepted: user_id must be config-resolved '
            "(KIOKU_MESH_USER_ID or 'user_id:' in config.yaml). Use --to user instead."
        )
    if raw == 'legacy':
        raise ValueError("--to 'legacy' is not a valid migration target")
    raise ValueError(f'unknown --to {raw!r}: expected mesh | user | team | team/<team_id>')


def legacy_obs_selector(scope: str = '', key_prefix: str = '') -> str:
    """Zenoh selector for legacy obs keys only (mem/obs/...).

    --key-prefix: appends /** to the given prefix.
    --scope: expands 1-4 identity segments; missing segments become *.
    default: mem/obs/*/*/*/*/*  (exact legacy shape, 7-part key)
    """
    if key_prefix:
        return key_prefix.rstrip('/') + '/**'
    if not scope:
        return 'mem/obs/*/*/*/*/*'
    segments = scope.split('/')
    parts = [segments[i] if i < len(segments) else '*' for i in range(4)]
    return 'mem/obs/' + '/'.join(parts) + '/*'


def legacy_tomb_selector(scope: str = '', key_prefix: str = '') -> str:
    """Zenoh selector for legacy tomb keys only (mem/tomb/...).

    Mirrors the obs selector shape; for --key-prefix replaces /obs/ with /tomb/.
    """
    if key_prefix:
        prefix = key_prefix.rstrip('/')
        return prefix.replace('/obs/', '/tomb/', 1) + '/**'
    if not scope:
        return 'mem/tomb/*/*/*/*/*'
    segments = scope.split('/')
    parts = [segments[i] if i < len(segments) else '*' for i in range(4)]
    return 'mem/tomb/' + '/'.join(parts) + '/*'


def scan_legacy_visibility(
    session: Any,
    *,
    scope: str = '',
    key_prefix: str = '',
) -> list[RawLegacyRecord]:
    """Collect legacy obs/tomb records from Zenoh.

    Only keys with parts[1] == 'obs' or 'tomb' (legacy shape) are accepted.
    Tiered keys (mem/mesh/..., mem/user/..., mem/team/...) are excluded.
    All records are collected before returning — no side effects during iteration.
    """
    obs_sel = legacy_obs_selector(scope, key_prefix)
    tomb_sel = legacy_tomb_selector(scope, key_prefix)
    records: list[RawLegacyRecord] = []

    for sample in _iter_ok_replies(session, obs_sel):
        key = str(sample.key_expr)
        parts = key.split('/')
        if len(parts) < 2 or parts[1] != 'obs':
            log.debug('scan_legacy_visibility: skip tiered obs key: %s', key)
            continue
        if obs_id_from_key(key) is None:
            log.warning('scan_legacy_visibility: skip malformed obs key: %s', key)
            continue
        try:
            payload = sample.payload.to_string()
        except Exception as e:  # noqa: BLE001
            log.warning('scan_legacy_visibility: skip obs with unreadable payload %s: %s', key, e)
            continue
        records.append(RawLegacyRecord(kind='obs', key=key, payload=payload))

    for sample in _iter_ok_replies(session, tomb_sel):
        key = str(sample.key_expr)
        parts = key.split('/')
        if len(parts) < 2 or parts[1] != 'tomb':
            log.debug('scan_legacy_visibility: skip tiered tomb key: %s', key)
            continue
        if obs_id_from_key(key) is None:
            log.warning('scan_legacy_visibility: skip malformed tomb key: %s', key)
            continue
        try:
            payload = sample.payload.to_string()
        except Exception as e:  # noqa: BLE001
            log.warning('scan_legacy_visibility: skip tomb with unreadable payload %s: %s', key, e)
            continue
        records.append(RawLegacyRecord(kind='tomb', key=key, payload=payload))

    return records


def _get_key_payload(session: Any, key: str) -> str | None:
    """Return payload string at key, or None if absent or error."""
    try:
        samples = list(_iter_ok_replies(session, key, timeout=10.0))
    except Exception:  # noqa: BLE001
        return None
    if not samples:
        return None
    try:
        return samples[0].payload.to_string()
    except Exception:  # noqa: BLE001
        return None


def build_migration_plan(
    records: list[RawLegacyRecord],
    target: MigrationTarget,
    session: Any,
) -> MigrationPlan:
    """Build a migration plan from scanned records.

    obs: parse payload, set visibility/scope_id (preserves _extras), build new key.
    tomb: derive new key from identity segments in legacy key; payload unchanged.
    Conflict: target exists with different payload -> added to conflicts, not items.
    Idempotent: target exists with same payload -> added to items (already done).
    """
    plan = MigrationPlan(target=target)

    for record in records:
        parts = record.key.split('/')
        obs_id = obs_id_from_key(record.key)
        if obs_id is None:
            plan.skipped.append(record.key)
            continue

        if record.kind == 'obs':
            try:
                obs = Observation.from_json(record.payload)
            except Exception as e:  # noqa: BLE001
                log.warning('build_migration_plan: skip unparseable obs %s: %s', record.key, e)
                plan.skipped.append(record.key)
                continue
            if obs.observation_id != obs_id:
                log.warning(
                    'build_migration_plan: skip key/payload id mismatch: key=%s payload_id=%s',
                    record.key,
                    obs.observation_id,
                )
                plan.skipped.append(record.key)
                continue
            obs.visibility = target.visibility
            obs.scope_id = target.scope_id
            new_key = obs.key_expr
            new_payload = obs.to_json()

        else:  # tomb
            if len(parts) != 7:
                log.warning(
                    'build_migration_plan: skip tomb with unexpected part count=%d: %s',
                    len(parts),
                    record.key,
                )
                plan.skipped.append(record.key)
                continue
            agent_family, client_id, pc_id, session_id = parts[2], parts[3], parts[4], parts[5]
            new_key = tomb_key(
                target.visibility,
                target.scope_id,
                agent_family,
                client_id,
                pc_id,
                session_id,
                obs_id,
            )
            new_payload = record.payload

        existing = _get_key_payload(session, new_key)
        if existing is not None:
            if existing == new_payload:
                plan.items.append(
                    MigrationItem(
                        kind=record.kind,
                        observation_id=obs_id,
                        old_key=record.key,
                        new_key=new_key,
                        original_payload=record.payload,
                        new_payload=new_payload,
                    )
                )
            else:
                plan.conflicts.append(
                    MigrationConflict(
                        kind=record.kind,
                        observation_id=obs_id,
                        old_key=record.key,
                        new_key=new_key,
                        existing_payload=existing,
                        incoming_payload=new_payload,
                    )
                )
        else:
            plan.items.append(
                MigrationItem(
                    kind=record.kind,
                    observation_id=obs_id,
                    old_key=record.key,
                    new_key=new_key,
                    original_payload=record.payload,
                    new_payload=new_payload,
                )
            )

    return plan


def write_backup(plan: MigrationPlan, backup_dir: Path) -> None:
    """Write manifest.jsonl + per-item payload files before any deletion."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = backup_dir / 'manifest.jsonl'
    with manifest_path.open('w', encoding='utf-8') as f:
        for item in plan.items:
            payload_bytes = item.original_payload.encode('utf-8')
            sha = hashlib.sha256(payload_bytes).hexdigest()
            fname = f'{item.observation_id}.{item.kind}.json'
            (backup_dir / fname).write_bytes(payload_bytes)
            entry = {
                'kind': item.kind,
                'old_key': item.old_key,
                'new_key': item.new_key,
                'payload_file': fname,
                'sha256': sha,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def save_checkpoint_atomic(checkpoint: MigrationCheckpoint, path: Path) -> None:
    """Write checkpoint JSON atomically via tmp-file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    data = {
        'version': checkpoint.version,
        'run_id': checkpoint.run_id,
        'params': checkpoint.params,
        'params_hash': checkpoint.params_hash,
        'target': checkpoint.target,
        'started_at': checkpoint.started_at,
        'updated_at': checkpoint.updated_at,
        'items': checkpoint.items,
    }
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def load_checkpoint(path: Path) -> MigrationCheckpoint:
    """Load and deserialize a checkpoint JSON file."""
    data = json.loads(path.read_text(encoding='utf-8'))
    return MigrationCheckpoint(
        version=int(data['version']),
        run_id=str(data['run_id']),
        params=dict(data['params']),
        target=dict(data['target']),
        started_at=str(data['started_at']),
        updated_at=str(data['updated_at']),
        items=dict(data.get('items', {})),
        params_hash=str(data.get('params_hash', '')),
    )


def verify_key_payload(session: Any, key: str, expected_payload: str) -> bool:
    """Return True iff the key exists in Zenoh and its payload matches."""
    return _get_key_payload(session, key) == expected_payload


def compute_params_hash(params: dict[str, str]) -> str:
    """SHA-256 of sorted canonical JSON; used to detect mismatched --resume invocations."""
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def reconstruct_items_from_checkpoint(
    chk: MigrationCheckpoint,
    backup_dir: Path,
    target: MigrationTarget,
    existing_old_keys: set[str],
) -> list[MigrationItem]:
    """Rebuild MigrationItems for checkpoint entries where source is already deleted.

    Covers the case where repair PUT has not been performed and the legacy key is
    gone from Zenoh. Called on --resume when the fresh scan misses items whose
    source key was deleted in a prior interrupted run.
    """
    items: list[MigrationItem] = []
    for item_key, state in chk.items.items():
        if not state.get('source_deleted') or state.get('repair_put'):
            continue
        old_key = state.get('old_key', '')
        new_key = state.get('new_key', '')
        if not old_key or not new_key:
            log.warning('reconstruct_items_from_checkpoint: missing key in entry %s', item_key)
            continue
        if old_key in existing_old_keys:
            continue  # Source still present; handled by normal plan path
        try:
            obs_id, kind = item_key.rsplit(':', 1)
        except ValueError:
            log.warning('reconstruct_items_from_checkpoint: malformed item_key %s', item_key)
            continue
        if kind not in ('obs', 'tomb'):
            log.warning('reconstruct_items_from_checkpoint: unknown kind %r in %s', kind, item_key)
            continue
        payload_file = backup_dir / f'{obs_id}.{kind}.json'
        if not payload_file.exists():
            log.error(
                'reconstruct_items_from_checkpoint: backup not found for %s at %s; '
                'repair PUT cannot be performed — data may require manual recovery',
                item_key,
                payload_file,
            )
            continue
        original_payload = payload_file.read_text(encoding='utf-8')
        if kind == 'tomb':
            new_payload = original_payload
        else:
            try:
                obs = Observation.from_json(original_payload)
                obs.visibility = target.visibility
                obs.scope_id = target.scope_id
                new_payload = obs.to_json()
            except Exception as e:  # noqa: BLE001
                log.error(
                    'reconstruct_items_from_checkpoint: cannot rebuild obs payload for %s: %s',
                    item_key,
                    e,
                )
                continue
        items.append(
            MigrationItem(
                kind=kind,
                observation_id=obs_id,
                old_key=old_key,
                new_key=new_key,
                original_payload=original_payload,
                new_payload=new_payload,
            )
        )
    return items


def _execute_batch(
    batch: list[MigrationItem],
    chk: MigrationCheckpoint,
    result: MigrationResult,
    session: Any,
    checkpoint_path: Path,
    now_iso: str,
) -> None:
    """Execute one batch: PUT target, verify, DELETE source, repair PUT.

    Each phase is guarded by the checkpoint state so a crash mid-batch
    can be resumed without repeating completed steps.
    """
    # Phase: PUT target keys
    for item in batch:
        item_key = f'{item.observation_id}:{item.kind}'
        state = chk.items.setdefault(
            item_key,
            {
                'old_key': item.old_key,
                'new_key': item.new_key,
                'backed_up': True,
                'target_put': False,
                'target_verified': False,
                'source_deleted': False,
                'repair_put': False,
            },
        )
        if not state.get('target_put'):
            try:
                session.put(item.new_key, item.new_payload)
                state['target_put'] = True
                result.copied += 1
            except Exception as e:  # noqa: BLE001
                log.error('execute_migration: PUT failed for %s: %s', item.new_key, e)
                result.failures += 1

    chk.updated_at = now_iso
    save_checkpoint_atomic(chk, checkpoint_path)

    # Phase: verify target keys
    for item in batch:
        item_key = f'{item.observation_id}:{item.kind}'
        state = chk.items[item_key]
        if state.get('target_put') and not state.get('target_verified'):
            if verify_key_payload(session, item.new_key, item.new_payload):
                state['target_verified'] = True
                result.verified += 1
            else:
                log.error('execute_migration: verify failed for %s', item.new_key)
                result.failures += 1

    # Flush after verify so crash recovery knows whether verification succeeded
    chk.updated_at = now_iso
    save_checkpoint_atomic(chk, checkpoint_path)

    # Phase: DELETE source exact keys (only when target verified)
    for item in batch:
        item_key = f'{item.observation_id}:{item.kind}'
        state = chk.items[item_key]
        if state.get('target_verified') and not state.get('source_deleted'):
            try:
                session.delete(item.old_key)
                state['source_deleted'] = True
                result.deleted += 1
            except Exception as e:  # noqa: BLE001
                log.error('execute_migration: DELETE failed for %s: %s', item.old_key, e)
                result.failures += 1

    # Flush after delete so source deletion is durably recorded before repair PUT
    chk.updated_at = now_iso
    save_checkpoint_atomic(chk, checkpoint_path)

    # Phase: repair PUT target (subscriber DELETE may have cleaned the new key)
    for item in batch:
        item_key = f'{item.observation_id}:{item.kind}'
        state = chk.items[item_key]
        if state.get('source_deleted') and not state.get('repair_put'):
            try:
                session.put(item.new_key, item.new_payload)
                state['repair_put'] = True
                result.repair_put += 1
            except Exception as e:  # noqa: BLE001
                log.error('execute_migration: repair PUT failed for %s: %s', item.new_key, e)
                result.failures += 1

    chk.updated_at = now_iso
    save_checkpoint_atomic(chk, checkpoint_path)


def execute_migration(
    plan: MigrationPlan,
    *,
    session: Any,
    dry_run: bool,
    yes: bool,
    batch_size: int,
    checkpoint_path: Path,
    backup_dir: Path,
    now_iso: str,
    params_hash: str = '',
) -> MigrationResult:
    """Execute the migration plan.

    dry_run=True: print summary; no puts/deletes/backup/checkpoint writes.
    dry_run=False, not yes: interactive confirmation prompt.
    On completion, rebuilds the local SQLite index from Zenoh.
    Returns MigrationResult; non-zero conflicts indicates partial failure.
    """
    import sys

    from .store import get_index

    n_obs = sum(1 for i in plan.items if i.kind == 'obs')
    n_tomb = len(plan.items) - n_obs

    if dry_run:
        print('migrate-visibility dry-run')
        print('  from: legacy')
        print(f'  to:   {plan.target.display}')
        print(f'  observations: {n_obs}')
        print(f'  tombstones: {n_tomb}')
        print(f'  conflicts: {len(plan.conflicts)}')
        if plan.skipped:
            print(f'  skipped (malformed): {len(plan.skipped)}')
        if plan.items:
            sample_n = min(3, len(plan.items))
            print('  sample:')
            for item in plan.items[:sample_n]:
                print(f'    {item.old_key} -> {item.new_key}')
        print(f'  checkpoint (would be): {checkpoint_path}')
        print(f'  backup_dir (would be): {backup_dir}')
        return MigrationResult(
            planned=len(plan.items),
            copied=0,
            verified=0,
            deleted=0,
            repair_put=0,
            conflicts=len(plan.conflicts),
            failures=0,
            backup_dir=backup_dir,
            checkpoint=checkpoint_path,
        )

    if not yes:
        print(
            f'About to migrate {n_obs} legacy observations and {n_tomb} legacy tombstones'
            f' to {plan.target.display}.\n'
            f'This will PUT tiered copies, verify them, then DELETE legacy keys.\n'
            f'Backup: {backup_dir}\n'
            f'Checkpoint: {checkpoint_path}\n'
            f"Type 'migrate {n_obs}' to continue: ",
            end='',
            flush=True,
        )
        try:
            answer = input().strip()
        except (EOFError, KeyboardInterrupt):
            print('\ncancelled.', file=sys.stderr)
            sys.exit(1)
        if answer != f'migrate {n_obs}':
            print('cancelled.', file=sys.stderr)
            sys.exit(1)

    write_backup(plan, backup_dir)

    run_id = checkpoint_path.parent.name

    if checkpoint_path.exists():
        chk = load_checkpoint(checkpoint_path)
    else:
        chk = MigrationCheckpoint(
            version=1,
            run_id=run_id,
            params={'from': 'legacy', 'to': plan.target.display},
            target={'visibility': plan.target.visibility, 'scope_id': plan.target.scope_id},
            started_at=now_iso,
            updated_at=now_iso,
            params_hash=params_hash,
        )
        save_checkpoint_atomic(chk, checkpoint_path)

    result = MigrationResult(
        planned=len(plan.items),
        copied=0,
        verified=0,
        deleted=0,
        repair_put=0,
        conflicts=len(plan.conflicts),
        failures=0,
        backup_dir=backup_dir,
        checkpoint=checkpoint_path,
    )

    batch: list[MigrationItem] = []
    for item in plan.items:
        item_key = f'{item.observation_id}:{item.kind}'
        state = chk.items.get(item_key, {})
        if state.get('repair_put'):
            result.copied += 1
            result.verified += 1
            result.deleted += 1
            result.repair_put += 1
            continue
        batch.append(item)
        if len(batch) >= batch_size:
            _execute_batch(batch, chk, result, session, checkpoint_path, now_iso)
            batch = []

    if batch:
        _execute_batch(batch, chk, result, session, checkpoint_path, now_iso)

    try:
        get_index().rebuild_from_zenoh(session)
    except Exception as e:  # noqa: BLE001
        log.warning('execute_migration: index rebuild failed: %s', e)

    if plan.conflicts:
        print(f'WARNING: {len(plan.conflicts)} conflict(s) — source keys NOT deleted.', file=sys.stderr)
        for c in plan.conflicts:
            print(f'  conflict: {c.old_key} -> {c.new_key} (existing payload differs)', file=sys.stderr)

    return result

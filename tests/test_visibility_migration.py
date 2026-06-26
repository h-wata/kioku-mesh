"""Tests for ADR-0019 Phase C migrate-visibility module.

All Zenoh session interactions are mocked; no network access required.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import call
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from kioku_mesh.core.models import Observation
from kioku_mesh.memory.visibility_migration import build_migration_plan
from kioku_mesh.memory.visibility_migration import compute_params_hash
from kioku_mesh.memory.visibility_migration import execute_migration
from kioku_mesh.memory.visibility_migration import legacy_obs_selector
from kioku_mesh.memory.visibility_migration import legacy_tomb_selector
from kioku_mesh.memory.visibility_migration import load_checkpoint
from kioku_mesh.memory.visibility_migration import MigrationCheckpoint
from kioku_mesh.memory.visibility_migration import MigrationItem
from kioku_mesh.memory.visibility_migration import MigrationPlan
from kioku_mesh.memory.visibility_migration import MigrationTarget
from kioku_mesh.memory.visibility_migration import parse_migration_target
from kioku_mesh.memory.visibility_migration import RawLegacyRecord
from kioku_mesh.memory.visibility_migration import reconstruct_items_from_checkpoint
from kioku_mesh.memory.visibility_migration import save_checkpoint_atomic
from kioku_mesh.memory.visibility_migration import scan_legacy_visibility
from kioku_mesh.memory.visibility_migration import verify_key_payload
from kioku_mesh.memory.visibility_migration import write_backup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OBS_ID = 'a' * 32
_AGENT = 'claude'
_CLIENT = 'cc'
_PC = 'p' * 32
_SESSION = 's' * 32
_LEGACY_OBS_KEY = f'mem/obs/{_AGENT}/{_CLIENT}/{_PC}/{_SESSION}/{_OBS_ID}'
_LEGACY_TOMB_KEY = f'mem/tomb/{_AGENT}/{_CLIENT}/{_PC}/{_SESSION}/{_OBS_ID}'
_MESH_OBS_KEY = f'mem/mesh/obs/{_AGENT}/{_CLIENT}/{_PC}/{_SESSION}/{_OBS_ID}'
_MESH_TOMB_KEY = f'mem/mesh/tomb/{_AGENT}/{_CLIENT}/{_PC}/{_SESSION}/{_OBS_ID}'

_NOW_ISO = '2026-06-26T00:00:00Z'


def _make_obs(observation_id: str = _OBS_ID, visibility: str = '', scope_id: str = '') -> Observation:
    return Observation(
        content='test content',
        agent_family=_AGENT,
        client_id=_CLIENT,
        pc_id=_PC,
        session_id=_SESSION,
        observation_id=observation_id,
        visibility=visibility,
        scope_id=scope_id,
    )


def _make_sample(key: str, payload: str) -> MagicMock:
    """Build a mock Zenoh sample."""
    sample = MagicMock()
    sample.key_expr = key
    sample.payload.to_string.return_value = payload
    return sample


# ---------------------------------------------------------------------------
# parse_migration_target
# ---------------------------------------------------------------------------


def test_parse_target_mesh() -> None:
    t = parse_migration_target('mesh')
    assert t.visibility == 'mesh'
    assert t.scope_id == ''
    assert t.display == 'mesh'


def test_parse_target_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('kioku_mesh.memory.visibility_migration.get_user_id', lambda: 'hwata')
    t = parse_migration_target('user')
    assert t.visibility == 'user'
    assert t.scope_id == 'hwata'
    assert t.display == 'user/hwata'


def test_parse_target_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('kioku_mesh.memory.visibility_migration.get_team_id', lambda: 'myteam')
    t = parse_migration_target('team')
    assert t.visibility == 'team'
    assert t.scope_id == 'myteam'
    assert t.display == 'team/myteam'


def test_parse_target_team_explicit() -> None:
    t = parse_migration_target('team/alpha')
    assert t.visibility == 'team'
    assert t.scope_id == 'alpha'
    assert t.display == 'team/alpha'


def test_parse_target_user_slash_rejected() -> None:
    with pytest.raises(ValueError, match='not accepted'):
        parse_migration_target('user/hwata')


def test_parse_target_legacy_rejected() -> None:
    with pytest.raises(ValueError, match='not a valid migration target'):
        parse_migration_target('legacy')


def test_parse_target_unknown_rejected() -> None:
    with pytest.raises(ValueError, match='unknown --to'):
        parse_migration_target('bogus')


def test_parse_target_user_no_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('kioku_mesh.memory.visibility_migration.get_user_id', lambda: '')
    with pytest.raises(ValueError, match='requires user_id'):
        parse_migration_target('user')


def test_parse_target_team_no_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('kioku_mesh.memory.visibility_migration.get_team_id', lambda: '')
    with pytest.raises(ValueError, match='requires team_id'):
        parse_migration_target('team')


# ---------------------------------------------------------------------------
# Selector construction
# ---------------------------------------------------------------------------


def test_obs_selector_default() -> None:
    assert legacy_obs_selector() == 'mem/obs/*/*/*/*/*'


def test_obs_selector_scope_1() -> None:
    # 4 identity segs: claude/*/*/*, then obs_id /*  → 7-part key
    assert legacy_obs_selector(scope='claude') == 'mem/obs/claude/*/*/*/*'


def test_obs_selector_scope_2() -> None:
    assert legacy_obs_selector(scope='claude/cc') == 'mem/obs/claude/cc/*/*/*'


def test_obs_selector_scope_4() -> None:
    assert legacy_obs_selector(scope='claude/cc/pc/sess') == 'mem/obs/claude/cc/pc/sess/*'


def test_obs_selector_key_prefix() -> None:
    assert legacy_obs_selector(key_prefix='mem/obs/claude') == 'mem/obs/claude/**'


def test_tomb_selector_default() -> None:
    assert legacy_tomb_selector() == 'mem/tomb/*/*/*/*/*'


def test_tomb_selector_scope() -> None:
    assert legacy_tomb_selector(scope='claude') == 'mem/tomb/claude/*/*/*/*'


def test_tomb_selector_key_prefix() -> None:
    # /obs/ replaced with /tomb/
    assert legacy_tomb_selector(key_prefix='mem/obs/claude') == 'mem/tomb/claude/**'


# ---------------------------------------------------------------------------
# scan_legacy_visibility
# ---------------------------------------------------------------------------


def test_scan_legacy_visibility_basic() -> None:
    obs = _make_obs()
    obs_payload = obs.to_json()
    tomb_payload = json.dumps({'observation_id': _OBS_ID, 'reason': '', 'deleted_at': _NOW_ISO})
    obs_sample = _make_sample(_LEGACY_OBS_KEY, obs_payload)
    tomb_sample = _make_sample(_LEGACY_TOMB_KEY, tomb_payload)

    session = MagicMock()
    replies = {
        'mem/obs/*/*/*/*/*': [obs_sample],
        'mem/tomb/*/*/*/*/*': [tomb_sample],
    }

    with patch(
        'kioku_mesh.memory.visibility_migration._iter_ok_replies',
        side_effect=lambda s, k, **kw: iter(replies.get(k, [])),
    ):
        records = scan_legacy_visibility(session)

    assert len(records) == 2
    obs_rec = next(r for r in records if r.kind == 'obs')
    tomb_rec = next(r for r in records if r.kind == 'tomb')
    assert obs_rec.key == _LEGACY_OBS_KEY
    assert tomb_rec.key == _LEGACY_TOMB_KEY


def test_scan_skips_tiered_keys() -> None:
    """Keys in tiered namespaces (mem/mesh/...) must be excluded."""
    tiered_sample = _make_sample(_MESH_OBS_KEY, '{}')
    obs = _make_obs()
    legacy_sample = _make_sample(_LEGACY_OBS_KEY, obs.to_json())

    session = MagicMock()
    replies = {
        'mem/obs/*/*/*/*/*': [tiered_sample, legacy_sample],
        'mem/tomb/*/*/*/*/*': [],
    }

    with patch(
        'kioku_mesh.memory.visibility_migration._iter_ok_replies',
        side_effect=lambda s, k, **kw: iter(replies.get(k, [])),
    ):
        records = scan_legacy_visibility(session)

    keys = [r.key for r in records]
    assert _MESH_OBS_KEY not in keys
    assert _LEGACY_OBS_KEY in keys


# ---------------------------------------------------------------------------
# build_migration_plan — obs
# ---------------------------------------------------------------------------


def test_build_plan_obs_rewrites_visibility() -> None:
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    records = [RawLegacyRecord(kind='obs', key=_LEGACY_OBS_KEY, payload=obs.to_json())]

    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=None):
        plan = build_migration_plan(records, target, session)

    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.old_key == _LEGACY_OBS_KEY
    assert item.new_key == _MESH_OBS_KEY
    new_obs = Observation.from_json(item.new_payload)
    assert new_obs.visibility == 'mesh'
    assert new_obs.scope_id == ''
    assert new_obs.observation_id == _OBS_ID


def test_build_plan_obs_preserves_extras() -> None:
    """_extras must be carried through visibility rewrite."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    # inject an extra field
    raw = json.loads(obs.to_json())
    raw['_future_field'] = 'keep_me'
    payload = json.dumps(raw)
    records = [RawLegacyRecord(kind='obs', key=_LEGACY_OBS_KEY, payload=payload)]

    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=None):
        plan = build_migration_plan(records, target, session)

    assert '"_future_field"' in plan.items[0].new_payload


def test_build_plan_obs_preserves_created_at() -> None:
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    original_created_at = obs.created_at
    records = [RawLegacyRecord(kind='obs', key=_LEGACY_OBS_KEY, payload=obs.to_json())]

    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=None):
        plan = build_migration_plan(records, target, session)

    new_obs = Observation.from_json(plan.items[0].new_payload)
    assert new_obs.created_at == original_created_at
    assert new_obs.observation_id == _OBS_ID


# ---------------------------------------------------------------------------
# build_migration_plan — tomb
# ---------------------------------------------------------------------------


def test_build_plan_tomb_maps_key() -> None:
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    tomb_payload = json.dumps({'observation_id': _OBS_ID, 'reason': '', 'deleted_at': _NOW_ISO})
    records = [RawLegacyRecord(kind='tomb', key=_LEGACY_TOMB_KEY, payload=tomb_payload)]

    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=None):
        plan = build_migration_plan(records, target, session)

    assert len(plan.items) == 1
    assert plan.items[0].new_key == _MESH_TOMB_KEY
    assert plan.items[0].new_payload == tomb_payload


# ---------------------------------------------------------------------------
# Idempotency / conflict detection
# ---------------------------------------------------------------------------


def test_build_plan_idempotent_same_payload() -> None:
    """Target exists with same payload -> item added (already done), no conflict."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    # Use a single obs instance so created_at is identical in both payloads.
    obs = _make_obs()
    original_payload = obs.to_json()
    obs.visibility = 'mesh'
    obs.scope_id = ''
    new_payload = obs.to_json()
    records = [RawLegacyRecord(kind='obs', key=_LEGACY_OBS_KEY, payload=original_payload)]

    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=new_payload):
        plan = build_migration_plan(records, target, session)

    assert len(plan.items) == 1
    assert len(plan.conflicts) == 0


def test_build_plan_conflict_different_payload() -> None:
    """Target exists with different payload -> conflict, not item."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    records = [RawLegacyRecord(kind='obs', key=_LEGACY_OBS_KEY, payload=obs.to_json())]
    existing_different = json.dumps({'content': 'DIFFERENT', 'observation_id': _OBS_ID})

    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=existing_different):
        plan = build_migration_plan(records, target, session)

    assert len(plan.items) == 0
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].old_key == _LEGACY_OBS_KEY


# ---------------------------------------------------------------------------
# dry-run: no side effects
# ---------------------------------------------------------------------------


def test_execute_dry_run_no_side_effects(tmp_path: pathlib.Path) -> None:
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    obs_payload = obs.to_json()
    item = MigrationItem(
        kind='obs',
        observation_id=_OBS_ID,
        old_key=_LEGACY_OBS_KEY,
        new_key=_MESH_OBS_KEY,
        original_payload=obs_payload,
        new_payload=obs_payload,
    )
    plan = MigrationPlan(target=target, items=[item])
    session = MagicMock()
    checkpoint_path = tmp_path / 'chk.json'
    backup_dir = tmp_path / 'backup'

    with (
        patch('kioku_mesh.memory.visibility_migration.write_backup') as mock_backup,
        patch('kioku_mesh.memory.visibility_migration.save_checkpoint_atomic') as mock_chk,
    ):
        result = execute_migration(
            plan,
            session=session,
            dry_run=True,
            yes=True,
            batch_size=500,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            now_iso=_NOW_ISO,
        )

    session.put.assert_not_called()
    session.delete.assert_not_called()
    mock_backup.assert_not_called()
    mock_chk.assert_not_called()
    assert result.copied == 0
    assert result.deleted == 0
    assert not checkpoint_path.exists()
    assert not backup_dir.exists()


# ---------------------------------------------------------------------------
# execute_migration: ordering
# ---------------------------------------------------------------------------


def test_execute_order_put_verify_delete_repair(tmp_path: pathlib.Path) -> None:
    """Verify the exact sequence: PUT target -> verify -> DELETE source -> repair PUT."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    obs_payload = obs.to_json()
    obs.visibility = 'mesh'
    new_payload = obs.to_json()
    item = MigrationItem(
        kind='obs',
        observation_id=_OBS_ID,
        old_key=_LEGACY_OBS_KEY,
        new_key=_MESH_OBS_KEY,
        original_payload=obs_payload,
        new_payload=new_payload,
    )
    plan = MigrationPlan(target=target, items=[item])

    calls: list[str] = []
    session = MagicMock()
    session.put.side_effect = lambda key, payload: calls.append(f'PUT:{key}')
    session.delete.side_effect = lambda key: calls.append(f'DEL:{key}')

    checkpoint_path = tmp_path / 'chk.json'
    backup_dir = tmp_path / 'backup'

    with (
        patch('kioku_mesh.memory.visibility_migration.verify_key_payload', return_value=True),
        patch('kioku_mesh.memory.store.get_index') as mock_index,
    ):
        mock_index.return_value.rebuild_from_zenoh = MagicMock()
        execute_migration(
            plan,
            session=session,
            dry_run=False,
            yes=True,
            batch_size=500,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            now_iso=_NOW_ISO,
        )

    assert calls.index(f'PUT:{_MESH_OBS_KEY}') < calls.index(f'DEL:{_LEGACY_OBS_KEY}')
    # repair PUT comes after DELETE
    repair_indices = [i for i, c in enumerate(calls) if c == f'PUT:{_MESH_OBS_KEY}']
    del_idx = calls.index(f'DEL:{_LEGACY_OBS_KEY}')
    assert repair_indices[-1] > del_idx


# ---------------------------------------------------------------------------
# conflict: source NOT deleted
# ---------------------------------------------------------------------------


def test_execute_conflict_source_not_deleted(tmp_path: pathlib.Path) -> None:
    """Conflicts in plan -> source keys must not be deleted."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    plan = MigrationPlan(target=target, items=[])
    from kioku_mesh.memory.visibility_migration import MigrationConflict

    plan.conflicts.append(
        MigrationConflict(
            kind='obs',
            observation_id=_OBS_ID,
            old_key=_LEGACY_OBS_KEY,
            new_key=_MESH_OBS_KEY,
            existing_payload='{"content":"different"}',
            incoming_payload='{"content":"original"}',
        )
    )
    session = MagicMock()
    checkpoint_path = tmp_path / 'chk.json'
    backup_dir = tmp_path / 'backup'

    with patch('kioku_mesh.memory.store.get_index') as mock_index:
        mock_index.return_value.rebuild_from_zenoh = MagicMock()
        result = execute_migration(
            plan,
            session=session,
            dry_run=False,
            yes=True,
            batch_size=500,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            now_iso=_NOW_ISO,
        )

    session.delete.assert_not_called()
    assert result.conflicts == 1


# ---------------------------------------------------------------------------
# checkpoint resume: target_put done, source not deleted
# ---------------------------------------------------------------------------


def test_checkpoint_resume_target_put_no_source_deleted(tmp_path: pathlib.Path) -> None:
    """After target_put but before source_deleted, resume completes the rest."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    obs.visibility = 'mesh'
    new_payload = obs.to_json()
    item = MigrationItem(
        kind='obs',
        observation_id=_OBS_ID,
        old_key=_LEGACY_OBS_KEY,
        new_key=_MESH_OBS_KEY,
        original_payload=_make_obs().to_json(),
        new_payload=new_payload,
    )
    plan = MigrationPlan(target=target, items=[item])

    # pre-build checkpoint: target_put done, source not deleted
    chk = MigrationCheckpoint(
        version=1,
        run_id='run1',
        params={'from': 'legacy', 'to': 'mesh'},
        target={'visibility': 'mesh', 'scope_id': ''},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
    )
    chk.items[f'{_OBS_ID}:obs'] = {
        'old_key': _LEGACY_OBS_KEY,
        'new_key': _MESH_OBS_KEY,
        'backed_up': True,
        'target_put': True,
        'target_verified': False,
        'source_deleted': False,
        'repair_put': False,
    }
    checkpoint_path = tmp_path / 'chk.json'
    save_checkpoint_atomic(chk, checkpoint_path)

    backup_dir = tmp_path / 'backup'
    backup_dir.mkdir()
    (backup_dir / 'manifest.jsonl').write_text('', encoding='utf-8')

    session = MagicMock()

    with (
        patch('kioku_mesh.memory.visibility_migration.verify_key_payload', return_value=True),
        patch('kioku_mesh.memory.store.get_index') as mock_index,
    ):
        mock_index.return_value.rebuild_from_zenoh = MagicMock()
        result = execute_migration(
            plan,
            session=session,
            dry_run=False,
            yes=True,
            batch_size=500,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            now_iso=_NOW_ISO,
        )

    # target was already put; only verify, delete, repair PUT should happen
    assert result.deleted == 1
    assert result.repair_put == 1
    session.delete.assert_called_once_with(_LEGACY_OBS_KEY)
    # repair PUT must be called
    assert any(c == call(_MESH_OBS_KEY, new_payload) for c in session.put.call_args_list)


# ---------------------------------------------------------------------------
# checkpoint resume: source deleted but repair_put not done
# ---------------------------------------------------------------------------


def test_checkpoint_resume_source_deleted_repair_pending(tmp_path: pathlib.Path) -> None:
    """After source_deleted but before repair_put, resume performs repair PUT."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    obs.visibility = 'mesh'
    new_payload = obs.to_json()
    item = MigrationItem(
        kind='obs',
        observation_id=_OBS_ID,
        old_key=_LEGACY_OBS_KEY,
        new_key=_MESH_OBS_KEY,
        original_payload=_make_obs().to_json(),
        new_payload=new_payload,
    )
    plan = MigrationPlan(target=target, items=[item])

    chk = MigrationCheckpoint(
        version=1,
        run_id='run1',
        params={'from': 'legacy', 'to': 'mesh'},
        target={'visibility': 'mesh', 'scope_id': ''},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
    )
    chk.items[f'{_OBS_ID}:obs'] = {
        'old_key': _LEGACY_OBS_KEY,
        'new_key': _MESH_OBS_KEY,
        'backed_up': True,
        'target_put': True,
        'target_verified': True,
        'source_deleted': True,
        'repair_put': False,
    }
    checkpoint_path = tmp_path / 'chk.json'
    save_checkpoint_atomic(chk, checkpoint_path)

    backup_dir = tmp_path / 'backup'
    backup_dir.mkdir()
    (backup_dir / 'manifest.jsonl').write_text('', encoding='utf-8')

    session = MagicMock()

    with patch('kioku_mesh.memory.store.get_index') as mock_index:
        mock_index.return_value.rebuild_from_zenoh = MagicMock()
        result = execute_migration(
            plan,
            session=session,
            dry_run=False,
            yes=True,
            batch_size=500,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            now_iso=_NOW_ISO,
        )

    session.delete.assert_not_called()
    assert result.repair_put == 1
    assert any(c == call(_MESH_OBS_KEY, new_payload) for c in session.put.call_args_list)


# ---------------------------------------------------------------------------
# pending_puts check (via __main__)
# ---------------------------------------------------------------------------


def test_cmd_migrate_visibility_blocks_on_pending_puts(monkeypatch: pytest.MonkeyPatch) -> None:
    """migrate-visibility must return 2 when pending_puts > 0."""
    import argparse

    from kioku_mesh.__main__ import _cmd_migrate_visibility

    args = argparse.Namespace(
        from_source='legacy',
        to_visibility='mesh',
        dry_run=False,
        yes=True,
        scope='',
        key_prefix='',
        batch_size=500,
        resume='',
        checkpoint='',
        backup_dir='',
    )

    status_mock = MagicMock()
    status_mock.pending_puts = 3
    backend_mock = MagicMock()
    backend_mock.get_status.return_value = status_mock

    with patch('kioku_mesh.__main__.get_backend', return_value=backend_mock):
        rc = _cmd_migrate_visibility(args)

    assert rc == 2


# ---------------------------------------------------------------------------
# verify_key_payload
# ---------------------------------------------------------------------------


def test_verify_key_payload_match() -> None:
    session = MagicMock()
    payload = '{"x": 1}'
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=payload):
        assert verify_key_payload(session, 'some/key', payload) is True


def test_verify_key_payload_mismatch() -> None:
    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value='{"x": 2}'):
        assert verify_key_payload(session, 'some/key', '{"x": 1}') is False


def test_verify_key_payload_missing() -> None:
    session = MagicMock()
    with patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=None):
        assert verify_key_payload(session, 'some/key', '{}') is False


# ---------------------------------------------------------------------------
# write_backup
# ---------------------------------------------------------------------------


def test_write_backup_creates_files(tmp_path: pathlib.Path) -> None:
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    obs_payload = obs.to_json()
    item = MigrationItem(
        kind='obs',
        observation_id=_OBS_ID,
        old_key=_LEGACY_OBS_KEY,
        new_key=_MESH_OBS_KEY,
        original_payload=obs_payload,
        new_payload=obs_payload,
    )
    plan = MigrationPlan(target=target, items=[item])
    backup_dir = tmp_path / 'backup'

    write_backup(plan, backup_dir)

    assert (backup_dir / 'manifest.jsonl').exists()
    payload_file = backup_dir / f'{_OBS_ID}.obs.json'
    assert payload_file.exists()
    assert payload_file.read_text(encoding='utf-8') == obs_payload


# ---------------------------------------------------------------------------
# checkpoint round-trip
# ---------------------------------------------------------------------------


def test_checkpoint_round_trip(tmp_path: pathlib.Path) -> None:
    chk = MigrationCheckpoint(
        version=1,
        run_id='run42',
        params={'from': 'legacy', 'to': 'mesh'},
        target={'visibility': 'mesh', 'scope_id': ''},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
    )
    chk.items['abc:obs'] = {
        'target_put': True,
        'target_verified': False,
        'source_deleted': False,
        'repair_put': False,
    }
    path = tmp_path / 'checkpoint.json'
    save_checkpoint_atomic(chk, path)
    loaded = load_checkpoint(path)

    assert loaded.run_id == 'run42'
    assert loaded.items['abc:obs']['target_put'] is True
    assert loaded.items['abc:obs']['repair_put'] is False


# ---------------------------------------------------------------------------
# B2: checkpoint flushed at verify/delete phase boundaries
# ---------------------------------------------------------------------------


def test_execute_checkpoint_flushed_at_phase_boundaries(tmp_path: pathlib.Path) -> None:
    """_execute_batch must save checkpoint after PUT, verify, delete, and repair phases."""
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    obs = _make_obs()
    obs.visibility = 'mesh'
    new_payload = obs.to_json()
    item = MigrationItem(
        kind='obs',
        observation_id=_OBS_ID,
        old_key=_LEGACY_OBS_KEY,
        new_key=_MESH_OBS_KEY,
        original_payload=_make_obs().to_json(),
        new_payload=new_payload,
    )
    plan = MigrationPlan(target=target, items=[item])

    checkpoint_path = tmp_path / 'chk.json'
    backup_dir = tmp_path / 'backup'
    session = MagicMock()

    save_calls: list[str] = []

    def _track_save(chk: MigrationCheckpoint, path: pathlib.Path) -> None:
        # Record which states are true at each save
        state = chk.items.get(f'{_OBS_ID}:obs', {})
        save_calls.append(
            f'put={state.get("target_put")},ver={state.get("target_verified")},'
            f'del={state.get("source_deleted")},rep={state.get("repair_put")}'
        )

    with (
        patch(
            'kioku_mesh.memory.visibility_migration.save_checkpoint_atomic',
            side_effect=_track_save,
        ),
        patch('kioku_mesh.memory.visibility_migration.verify_key_payload', return_value=True),
        patch('kioku_mesh.memory.store.get_index') as mock_index,
    ):
        mock_index.return_value.rebuild_from_zenoh = MagicMock()
        execute_migration(
            plan,
            session=session,
            dry_run=False,
            yes=True,
            batch_size=500,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            now_iso=_NOW_ISO,
        )

    # Initial checkpoint creation (1) + 4 phase flushes per batch = 5 total saves
    assert len(save_calls) >= 5, f'expected >=5 saves, got {len(save_calls)}: {save_calls}'
    # Find the flush states corresponding to the 4 batch phases
    # After PUT phase: target_put=True, target_verified=False
    assert any('put=True,ver=False' in s for s in save_calls), 'no flush after PUT phase'
    # After verify phase: target_put=True, target_verified=True, source_deleted=False
    assert any('ver=True,del=False' in s for s in save_calls), 'no flush after verify phase'
    # After delete phase: source_deleted=True, repair_put=False
    assert any('del=True,rep=False' in s for s in save_calls), 'no flush after delete phase'
    # After repair phase: repair_put=True
    assert any('rep=True' in s for s in save_calls), 'no flush after repair phase'


# ---------------------------------------------------------------------------
# B3: params mismatch resume exits 2
# ---------------------------------------------------------------------------


def test_cmd_migrate_resume_params_mismatch_exits_2(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--resume with different --to than checkpoint must exit 2."""
    import argparse

    from kioku_mesh.__main__ import _cmd_migrate_visibility
    from kioku_mesh.memory.visibility_migration import compute_params_hash

    # Build a checkpoint with params_hash for 'mesh' target
    original_params = {
        'from': 'legacy',
        'to': 'mesh',
        'scope': '',
        'key_prefix': '',
        'visibility': 'mesh',
        'scope_id': '',
    }
    chk = MigrationCheckpoint(
        version=1,
        run_id='run1',
        params={'from': 'legacy', 'to': 'mesh'},
        target={'visibility': 'mesh', 'scope_id': ''},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
        params_hash=compute_params_hash(original_params),
    )
    chk_path = tmp_path / 'checkpoint.json'
    save_checkpoint_atomic(chk, chk_path)

    # Now try to resume with --to user (different target)
    monkeypatch.setattr('kioku_mesh.memory.visibility_migration.get_user_id', lambda: 'hwata')
    args = argparse.Namespace(
        from_source='legacy',
        to_visibility='user',
        dry_run=False,
        yes=True,
        scope='',
        key_prefix='',
        batch_size=500,
        resume=str(chk_path),
        checkpoint='',
        backup_dir='',
    )

    status_mock = MagicMock()
    status_mock.pending_puts = 0
    backend_mock = MagicMock()
    backend_mock.get_status.return_value = status_mock

    with patch('kioku_mesh.__main__.get_backend', return_value=backend_mock):
        rc = _cmd_migrate_visibility(args)

    assert rc == 2


# ---------------------------------------------------------------------------
# B1: CLI resume reconstructs pending items from checkpoint + backup
# ---------------------------------------------------------------------------


def test_cmd_migrate_resume_repairs_after_source_deleted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--resume must repair PUT even when legacy source key is absent from scan.

    Scenario: crash happened after source DELETE but before repair PUT.
    The checkpoint records source_deleted=True, repair_put=False.
    The fresh scan returns [] because the legacy key is gone.
    Resume must load the item from checkpoint+backup and execute repair PUT.
    """
    import argparse

    from kioku_mesh.__main__ import _cmd_migrate_visibility
    from kioku_mesh.memory.visibility_migration import compute_params_hash

    # --- Build checkpoint in a tmp migration run dir ---
    run_dir = tmp_path / 'run1'
    run_dir.mkdir()
    backup_dir = run_dir / 'backup'
    backup_dir.mkdir()

    original_payload = _make_obs().to_json()

    # Write backup payload file
    (backup_dir / f'{_OBS_ID}.obs.json').write_text(original_payload, encoding='utf-8')

    original_params = {
        'from': 'legacy',
        'to': 'mesh',
        'scope': '',
        'key_prefix': '',
        'visibility': 'mesh',
        'scope_id': '',
    }
    chk = MigrationCheckpoint(
        version=1,
        run_id='run1',
        params={'from': 'legacy', 'to': 'mesh'},
        target={'visibility': 'mesh', 'scope_id': ''},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
        params_hash=compute_params_hash(original_params),
    )
    chk.items[f'{_OBS_ID}:obs'] = {
        'old_key': _LEGACY_OBS_KEY,
        'new_key': _MESH_OBS_KEY,
        'backed_up': True,
        'target_put': True,
        'target_verified': True,
        'source_deleted': True,
        'repair_put': False,
    }
    chk_path = run_dir / 'checkpoint.json'
    save_checkpoint_atomic(chk, chk_path)

    # --- CLI args: --resume pointing to checkpoint ---
    args = argparse.Namespace(
        from_source='legacy',
        to_visibility='mesh',
        dry_run=False,
        yes=True,
        scope='',
        key_prefix='',
        batch_size=500,
        resume=str(chk_path),
        checkpoint='',
        backup_dir='',
    )

    status_mock = MagicMock()
    status_mock.pending_puts = 0
    backend_mock = MagicMock()
    backend_mock.get_status.return_value = status_mock

    session = MagicMock()
    # scan returns empty (legacy key already deleted)
    # verify returns True so the item (from checkpoint) can proceed to repair
    repair_put_calls: list[tuple[str, str]] = []
    session.put.side_effect = lambda key, payload: repair_put_calls.append((key, payload))

    with (
        patch('kioku_mesh.__main__.get_backend', return_value=backend_mock),
        patch('kioku_mesh.__main__.get_session', return_value=session),
        patch(
            'kioku_mesh.memory.visibility_migration._iter_ok_replies',
            return_value=iter([]),
        ),
        patch('kioku_mesh.memory.visibility_migration._get_key_payload', return_value=None),
        patch('kioku_mesh.memory.visibility_migration.verify_key_payload', return_value=True),
        patch('kioku_mesh.memory.store.get_index') as mock_index,
    ):
        mock_index.return_value.rebuild_from_zenoh = MagicMock()
        rc = _cmd_migrate_visibility(args)

    assert rc == 0, f'expected exit 0, got {rc}'
    # repair PUT must have been called for the mesh key
    assert any(
        key == _MESH_OBS_KEY for key, _ in repair_put_calls
    ), f'repair PUT for {_MESH_OBS_KEY!r} not found in {repair_put_calls}'


# ---------------------------------------------------------------------------
# compute_params_hash and reconstruct_items_from_checkpoint unit tests
# ---------------------------------------------------------------------------


def test_compute_params_hash_is_stable() -> None:
    params = {'from': 'legacy', 'to': 'mesh', 'scope': '', 'key_prefix': '', 'visibility': 'mesh', 'scope_id': ''}
    h1 = compute_params_hash(params)
    h2 = compute_params_hash(params)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_params_hash_differs_on_change() -> None:
    p1 = {'from': 'legacy', 'to': 'mesh', 'scope': '', 'key_prefix': '', 'visibility': 'mesh', 'scope_id': ''}
    p2 = dict(p1)
    p2['to'] = 'user/hwata'
    assert compute_params_hash(p1) != compute_params_hash(p2)


def test_reconstruct_items_skips_non_pending(tmp_path: pathlib.Path) -> None:
    """Items with repair_put=True must be skipped by reconstruct_items_from_checkpoint."""
    backup_dir = tmp_path / 'backup'
    backup_dir.mkdir()
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')

    chk = MigrationCheckpoint(
        version=1,
        run_id='r',
        params={},
        target={},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
    )
    chk.items[f'{_OBS_ID}:obs'] = {
        'old_key': _LEGACY_OBS_KEY,
        'new_key': _MESH_OBS_KEY,
        'backed_up': True,
        'target_put': True,
        'target_verified': True,
        'source_deleted': True,
        'repair_put': True,  # already done
    }

    items = reconstruct_items_from_checkpoint(chk, backup_dir, target, set())
    assert items == []


def test_reconstruct_items_from_checkpoint_obs(tmp_path: pathlib.Path) -> None:
    """reconstruct_items_from_checkpoint returns MigrationItem from backup when source gone."""
    backup_dir = tmp_path / 'backup'
    backup_dir.mkdir()
    target = MigrationTarget(visibility='mesh', scope_id='', display='mesh')
    original_payload = _make_obs().to_json()
    (backup_dir / f'{_OBS_ID}.obs.json').write_text(original_payload, encoding='utf-8')

    chk = MigrationCheckpoint(
        version=1,
        run_id='r',
        params={},
        target={},
        started_at=_NOW_ISO,
        updated_at=_NOW_ISO,
    )
    chk.items[f'{_OBS_ID}:obs'] = {
        'old_key': _LEGACY_OBS_KEY,
        'new_key': _MESH_OBS_KEY,
        'backed_up': True,
        'target_put': True,
        'target_verified': True,
        'source_deleted': True,
        'repair_put': False,
    }

    items = reconstruct_items_from_checkpoint(chk, backup_dir, target, set())
    assert len(items) == 1
    item = items[0]
    assert item.old_key == _LEGACY_OBS_KEY
    assert item.new_key == _MESH_OBS_KEY
    assert item.kind == 'obs'
    new_obs = Observation.from_json(item.new_payload)
    assert new_obs.visibility == 'mesh'

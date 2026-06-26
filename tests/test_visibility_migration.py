"""Tests for ADR-0019 Phase C migrate-visibility module.

All Zenoh session interactions are mocked; no network access required.
"""

from __future__ import annotations

import pathlib

import json
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch

import pytest

from kioku_mesh.memory.visibility_migration import (
    MigrationItem,
    MigrationPlan,
    MigrationTarget,
    MigrationCheckpoint,
    RawLegacyRecord,
    build_migration_plan,
    execute_migration,
    legacy_obs_selector,
    legacy_tomb_selector,
    load_checkpoint,
    parse_migration_target,
    save_checkpoint_atomic,
    scan_legacy_visibility,
    verify_key_payload,
    write_backup,
)
from kioku_mesh.core.models import Observation


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

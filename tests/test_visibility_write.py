"""ADR-0019 Phase B: visibility-aware write path.

Unit tests cover the config-side resolution (env / config.yaml precedence,
actionable errors for unconfigured scoped tiers). The e2e tests need a live
zenohd (``single_zenohd`` fixture) and verify that a tiered save actually
lands under the tiered key, that the tombstone mirrors it, and that
retention gc cleans tiered keys.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import time
from typing import Any

import pytest

from mesh_mem import config
from mesh_mem import store
from mesh_mem.models import Observation
from mesh_mem.models import Tombstone

_SETTLE = 0.4


# ---------------------------------------------------------------------------
# resolve_write_visibility (pure unit tests)
# ---------------------------------------------------------------------------


def test_resolve_defaults_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MESH_MEM_DEFAULT_VISIBILITY', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    assert config.resolve_write_visibility('') == ('', '')


def test_resolve_explicit_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MESH_MEM_DEFAULT_VISIBILITY', 'mesh')
    monkeypatch.setenv('MESH_MEM_USER_ID', 'hwata')
    assert config.resolve_write_visibility('user') == ('user', 'hwata')


def test_resolve_default_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MESH_MEM_DEFAULT_VISIBILITY', 'user')
    monkeypatch.setenv('MESH_MEM_USER_ID', 'hwata')
    assert config.resolve_write_visibility('') == ('user', 'hwata')


def test_resolve_mesh_needs_no_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MESH_MEM_USER_ID', raising=False)
    assert config.resolve_write_visibility('mesh') == ('mesh', '')


def test_resolve_user_without_id_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MESH_MEM_USER_ID', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    with pytest.raises(ValueError, match='MESH_MEM_USER_ID'):
        config.resolve_write_visibility('user')


def test_resolve_team_without_id_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MESH_MEM_TEAM_ID', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    with pytest.raises(ValueError, match='MESH_MEM_TEAM_ID'):
        config.resolve_write_visibility('team')


def test_resolve_rejects_unknown_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match='visibility'):
        config.resolve_write_visibility('org')


def test_legacy_payload_keeps_legacy_keys() -> None:
    """A payload without ``visibility`` must keep deriving legacy keys.

    This is the delete/gc-safety invariant: re-parsing an old observation
    must never re-home it into a tiered namespace.
    """
    obs = Observation(content='legacy', agent_family='f', client_id='c', pc_id='p', session_id='s')
    restored = Observation.from_json(obs.to_json())
    assert restored.visibility == ''
    assert restored.key_expr.startswith('mem/obs/')
    assert restored.tombstone_key_expr().startswith('mem/tomb/')


def test_tiered_payload_roundtrips_visibility() -> None:
    obs = Observation(
        content='tiered',
        agent_family='f',
        client_id='c',
        pc_id='p',
        session_id='s',
        visibility='user',
        scope_id='hwata',
    )
    restored = Observation.from_json(obs.to_json())
    assert (restored.visibility, restored.scope_id) == ('user', 'hwata')
    assert restored.key_expr == obs.key_expr
    assert restored.tombstone_key_expr() == obs.tombstone_key_expr()


# ---------------------------------------------------------------------------
# e2e against a live zenohd
# ---------------------------------------------------------------------------


def _mk_user_obs(content: str, *, project: str) -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test-client',
        pc_id='test-pc',
        session_id='test-session',
        visibility='user',
        scope_id='testuser',
    )


def test_put_observation_lands_under_user_namespace(single_zenohd: Any) -> None:
    obs = _mk_user_obs('user-tier write', project='visw-put')
    store.put_observation(obs)
    time.sleep(_SETTLE)

    sess = store.get_session()
    keys = [str(r.ok.key_expr) for r in sess.get('mem/user/testuser/obs/**', timeout=2.0) if r.ok]
    assert obs.key_expr in keys, 'tiered write must land under mem/user/{user_id}/obs/...'
    # And nothing under the legacy namespace for this id.
    legacy = [str(r.ok.key_expr) for r in sess.get(f'mem/obs/**/{obs.observation_id}', timeout=2.0) if r.ok]
    assert legacy == []
    # Readable through the normal search path (Phase A readers).
    found = {r.observation_id for r in store.search_observations(project='visw-put')}
    assert obs.observation_id in found


def test_put_tombstone_mirrors_user_namespace(single_zenohd: Any) -> None:
    obs = _mk_user_obs('user-tier delete target', project='visw-del')
    store.put_observation(obs)
    store.put_tombstone(obs, reason='test')
    time.sleep(_SETTLE)

    sess = store.get_session()
    tomb_keys = [str(r.ok.key_expr) for r in sess.get('mem/user/testuser/tomb/**', timeout=2.0) if r.ok]
    assert obs.tombstone_key_expr() in tomb_keys, 'tombstone must mirror the tiered obs key'
    assert store.search_observations(project='visw-del') == []


def test_gc_purges_tiered_keys(single_zenohd: Any) -> None:
    """Retention gc derives delete keys from the payload, so tiered keys are swept too."""
    obs = _mk_user_obs('user-tier gc target', project='visw-gc')
    store.put_observation(obs)
    aged = datetime.now(timezone.utc) - timedelta(days=60)
    tomb = Tombstone(
        observation_id=obs.observation_id,
        reason='aged',
        deleted_at=aged.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    )
    store.get_session().put(obs.tombstone_key_expr(), tomb.to_json())
    time.sleep(_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30)
    assert purged >= 1
    time.sleep(_SETTLE)

    sess = store.get_session()
    leftover = [
        str(r.ok.key_expr)
        for expr in (f'mem/**/obs/**/{obs.observation_id}', f'mem/**/tomb/**/{obs.observation_id}')
        for r in sess.get(expr, timeout=2.0)
        if r.ok
    ]
    assert leftover == [], f'gc must sweep tiered obs+tomb keys, leftover: {leftover}'

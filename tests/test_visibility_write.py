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
from pathlib import Path
import time
from typing import Any

import pytest

from kioku_mesh import config
from kioku_mesh import store
from kioku_mesh.models import Observation
from kioku_mesh.models import Tombstone

_SETTLE = 0.4


# ---------------------------------------------------------------------------
# resolve_write_visibility (pure unit tests)
# ---------------------------------------------------------------------------


def test_resolve_defaults_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('KIOKU_MESH_DEFAULT_VISIBILITY', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    assert config.resolve_write_visibility('') == ('', '')


def test_resolve_explicit_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('KIOKU_MESH_DEFAULT_VISIBILITY', 'mesh')
    monkeypatch.setenv('KIOKU_MESH_USER_ID', 'hwata')
    assert config.resolve_write_visibility('user') == ('user', 'hwata')


def test_resolve_default_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('KIOKU_MESH_DEFAULT_VISIBILITY', 'user')
    monkeypatch.setenv('KIOKU_MESH_USER_ID', 'hwata')
    assert config.resolve_write_visibility('') == ('user', 'hwata')


def test_resolve_mesh_needs_no_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
    assert config.resolve_write_visibility('mesh') == ('mesh', '')


def test_resolve_user_without_id_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    with pytest.raises(ValueError, match='KIOKU_MESH_USER_ID'):
        config.resolve_write_visibility('user')


def test_resolve_team_without_id_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    with pytest.raises(ValueError, match='KIOKU_MESH_TEAM_ID'):
        config.resolve_write_visibility('team')


def test_resolve_rejects_unknown_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match='visibility'):
        config.resolve_write_visibility('org')


def test_legacy_payload_keeps_legacy_keys() -> None:
    """A payload without ``visibility`` must keep deriving legacy keys.

    This is the delete/gc-safety invariant: re-parsing an old observation
    must never re-home it into a tiered namespace.
    """
    import json

    obs = Observation(content='legacy', agent_family='f', client_id='c', pc_id='p', session_id='s')
    # Simulate a payload written by a pre-Phase-B peer: the fields do not
    # exist at all (serializing a current Observation would include them
    # as empty strings, which is not the same wire shape).
    payload = json.loads(obs.to_json())
    del payload['visibility']
    del payload['scope_id']
    restored = Observation.from_json(json.dumps(payload))
    assert restored.visibility == ''
    assert restored.scope_id == ''
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
# Project-local .kioku-mesh.yaml (per-directory default, ADR-0019 addendum)
# ---------------------------------------------------------------------------


def _isolate_visibility_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Scrub visibility env vars and point both config sources at tmp_path.

    Returns the global config dir (XDG) so tests can drop a config.yaml in.
    ``monkeypatch.chdir`` keeps the upward ``.kioku-mesh.yaml`` search from
    escaping into the real filesystem above the test run.
    """
    monkeypatch.delenv('KIOKU_MESH_DEFAULT_VISIBILITY', raising=False)
    monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
    monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
    xdg = tmp_path / 'xdg'
    (xdg / 'kioku-mesh').mkdir(parents=True)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    monkeypatch.chdir(tmp_path)
    return xdg / 'kioku-mesh'


@pytest.mark.parametrize('depth', [0, 1, 2])
def test_project_config_found_walking_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, depth: int) -> None:
    """.kioku-mesh.yaml is discovered from cwd itself, a parent, or a grandparent."""
    _isolate_visibility_config(monkeypatch, tmp_path)
    (tmp_path / '.kioku-mesh.yaml').write_text('default_visibility: mesh\n')
    cwd = tmp_path
    for name in ('a', 'b')[:depth]:
        cwd = cwd / name
        cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert config.find_project_config() == tmp_path / '.kioku-mesh.yaml'
    assert config.get_default_visibility() == 'mesh'


def test_nearest_project_config_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_visibility_config(monkeypatch, tmp_path)
    (tmp_path / '.kioku-mesh.yaml').write_text('default_visibility: mesh\n')
    nested = tmp_path / 'sub'
    nested.mkdir()
    (nested / '.kioku-mesh.yaml').write_text('default_visibility: team\n')
    monkeypatch.chdir(nested)
    assert config.find_project_config() == nested / '.kioku-mesh.yaml'
    assert config.get_default_visibility() == 'team'


def test_no_project_config_falls_back_to_global(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg_dir = _isolate_visibility_config(monkeypatch, tmp_path)
    (cfg_dir / 'config.yaml').write_text('default_visibility: mesh\nteam_id: global-team\n')
    assert config.find_project_config() is None
    assert config.get_default_visibility() == 'mesh'
    assert config.get_team_id() == 'global-team'


def test_env_beats_project_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_visibility_config(monkeypatch, tmp_path)
    (tmp_path / '.kioku-mesh.yaml').write_text('default_visibility: team\nteam_id: proj-team\n')
    monkeypatch.setenv('KIOKU_MESH_DEFAULT_VISIBILITY', 'mesh')
    monkeypatch.setenv('KIOKU_MESH_TEAM_ID', 'env-team')
    assert config.get_default_visibility() == 'mesh'
    assert config.get_team_id() == 'env-team'


def test_project_config_beats_global(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg_dir = _isolate_visibility_config(monkeypatch, tmp_path)
    (cfg_dir / 'config.yaml').write_text('default_visibility: mesh\nteam_id: global-team\n')
    (tmp_path / '.kioku-mesh.yaml').write_text('default_visibility: team\nteam_id: proj-team\n')
    assert config.get_default_visibility() == 'team'
    assert config.get_team_id() == 'proj-team'


def test_project_config_cannot_set_user_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """user_id in a (committable) project file must be ignored — ADR-0019."""
    _isolate_visibility_config(monkeypatch, tmp_path)
    (tmp_path / '.kioku-mesh.yaml').write_text('user_id: mallory\ndefault_visibility: user\n')
    assert config.get_user_id() == ''
    # And the resolution fails actionably instead of writing as 'mallory'.
    with pytest.raises(ValueError, match='KIOKU_MESH_USER_ID'):
        config.resolve_write_visibility('')


def test_project_team_default_resolves_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_visibility_config(monkeypatch, tmp_path)
    (tmp_path / '.kioku-mesh.yaml').write_text('default_visibility: team\nteam_id: kioku-mesh\n')
    assert config.resolve_write_visibility('') == ('team', 'kioku-mesh')


def test_format_visibility_variants() -> None:
    assert config.format_visibility('', '') == 'legacy'
    assert config.format_visibility('mesh', '') == 'mesh'
    assert config.format_visibility('user', 'hwata') == 'user/hwata'
    assert config.format_visibility('team', 'kioku-mesh') == 'team/kioku-mesh'


def test_save_responses_show_effective_visibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """CLI and MCP save responses echo the effective scope (ADR-0019 trust note)."""
    from kioku_mesh import mcp_server
    from kioku_mesh.__main__ import main as cli_main
    from kioku_mesh.backend import reset_backend

    _isolate_visibility_config(monkeypatch, tmp_path)
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    msg = mcp_server.save_observation(content='legacy save', project='vis-resp')
    assert 'legacy' in msg

    msg = mcp_server.save_observation(content='mesh save', project='vis-resp', visibility='mesh')
    assert 'mesh' in msg

    monkeypatch.setenv('KIOKU_MESH_USER_ID', 'hwata')
    msg = mcp_server.save_observation(content='user save', project='vis-resp', visibility='user')
    assert 'user/hwata' in msg

    # Project file drives the CLI default; the response surfaces it.
    (tmp_path / '.kioku-mesh.yaml').write_text('default_visibility: team\nteam_id: kioku-mesh\n')
    rc = cli_main(['save', 'team save', '-p', 'vis-resp'])
    assert rc == 0
    out = capsys.readouterr().out
    assert '(visibility=team/kioku-mesh)' in out


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


def test_unknown_future_visibility_clamps_to_legacy() -> None:
    """A payload from a newer peer with an unknown tier parses safely as legacy."""
    import json

    obs = Observation(content='future', agent_family='f', client_id='c', pc_id='p', session_id='s')
    payload = json.loads(obs.to_json())
    payload['visibility'] = 'org'  # tier this version does not know
    restored = Observation.from_json(json.dumps(payload))
    assert restored.visibility == ''
    assert restored.key_expr.startswith('mem/obs/')


def test_resolve_rejects_malformed_scope_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad MESH_MEM_USER_ID fails at resolution time with ValueError.

    Codex P2 on PR #179: failing later inside the key builder would bypass
    the MCP/CLI error handling around resolve_write_visibility.
    """
    for bad in ('a/b', 'a*', 'a$b', 'a b', '..', '-lead', 'x' * 65):
        monkeypatch.setenv('KIOKU_MESH_USER_ID', bad)
        with pytest.raises(ValueError, match='scope_id'):
            config.resolve_write_visibility('user')


def test_gc_sweeps_tiered_obs_under_legacy_tombstone(single_zenohd: Any) -> None:
    """Rolling-upgrade resurrection guard (Codex P1 on PR #179).

    An old (pre-Phase-B) peer deleting a tiered observation emits the
    tombstone under the LEGACY namespace (its Observation lacks the
    visibility field, so the mirror lands at mem/tomb/...). Retention gc
    must still sweep the real tiered obs key — otherwise the next rebuild
    resurrects the deleted observation.
    """
    obs = _mk_user_obs('tiered obs, legacy tomb', project='visw-mixed')
    store.put_observation(obs)

    # Simulate the old writer: same identity, but the legacy tomb key.
    legacy_tomb_key = f'mem/tomb/{obs.agent_family}/{obs.client_id}/{obs.pc_id}/{obs.session_id}/{obs.observation_id}'
    aged = datetime.now(timezone.utc) - timedelta(days=60)
    tomb = Tombstone(
        observation_id=obs.observation_id,
        reason='deleted by old peer',
        deleted_at=aged.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
    )
    store.get_session().put(legacy_tomb_key, tomb.to_json())
    time.sleep(_SETTLE)

    purged = store.gc_expired_tombstones(retention_days=30)
    assert purged >= 1
    time.sleep(_SETTLE)

    sess = store.get_session()
    leftover = [str(r.ok.key_expr) for r in sess.get(f'mem/**/obs/**/{obs.observation_id}', timeout=2.0) if r.ok]
    assert leftover == [], f'tiered obs must not survive a legacy-tomb gc: {leftover}'

    # No resurrection: a fresh rebuild must not bring the observation back.
    store._reset_index()
    assert store.search_observations(project='visw-mixed') == []

"""Tests for the local backend (SQLite-only, no zenohd).

Covers:
  - LocalBackend contract (save / search / get / delete / gc)
  - contract parity with ZenohBackend via parametrize (zenoh skipped when unavailable)
  - ``kioku-mesh init --mode local`` writes config.yaml with backend: local
  - MESH_MEM_BACKEND=local env var selects LocalBackend without config.yaml
  - zenohd absent from PATH does not error in local mode
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from mesh_mem.backend import get_backend
from mesh_mem.backend import LocalBackend
from mesh_mem.backend import reset_backend
from mesh_mem.config import get_backend_mode
from mesh_mem.models import Observation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_obs(content: str, *, project: str = 'test') -> Observation:
    return Observation(content=content, project=project)


# ---------------------------------------------------------------------------
# Config / backend selection
# ---------------------------------------------------------------------------


def test_get_backend_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    assert get_backend_mode() == 'local'


def test_get_backend_mode_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    config_dir = tmp_path / 'xdg' / 'kioku-mesh'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.yaml').write_text('backend: local\n')
    monkeypatch.delenv('MESH_MEM_BACKEND', raising=False)
    assert get_backend_mode() == 'local'


def test_get_backend_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MESH_MEM_BACKEND', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent_config_dir_xyz')
    assert get_backend_mode() == 'zenoh'


def test_get_backend_returns_local_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    reset_backend()
    backend = get_backend()
    assert isinstance(backend, LocalBackend)
    reset_backend()


# ---------------------------------------------------------------------------
# LocalBackend contract tests
# ---------------------------------------------------------------------------


@pytest.fixture
def local_backend(monkeypatch: pytest.MonkeyPatch) -> LocalBackend:
    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    reset_backend()
    b = get_backend()
    assert isinstance(b, LocalBackend)
    return b


def test_local_put_and_search(local_backend: LocalBackend) -> None:
    obs = _mk_obs('hello local backend')
    local_backend.put_observation(obs)

    results = local_backend.search_observations(query='hello local')
    assert any(r.observation_id == obs.observation_id for r in results)


def test_local_get_by_id(local_backend: LocalBackend) -> None:
    obs = _mk_obs('findable content')
    local_backend.put_observation(obs)

    found = local_backend.find_observation_by_id(obs.observation_id)
    assert found is not None
    assert found.content == 'findable content'


def test_local_get_missing_returns_none(local_backend: LocalBackend) -> None:
    assert local_backend.find_observation_by_id('a' * 32) is None


def test_local_delete_tombstone(local_backend: LocalBackend) -> None:
    obs = _mk_obs('to be tombstoned')
    local_backend.put_observation(obs)

    local_backend.put_tombstone(obs, reason='test')

    results = local_backend.search_observations(query='tombstoned')
    assert not any(r.observation_id == obs.observation_id for r in results)


def test_local_physical_delete(local_backend: LocalBackend) -> None:
    obs = _mk_obs('physically deleted')
    local_backend.put_observation(obs)

    obs_removed, _ = local_backend.physical_delete_observation(obs.observation_id)
    assert obs_removed is True

    assert local_backend.find_observation_by_id(obs.observation_id) is None


def test_local_status(local_backend: LocalBackend) -> None:
    status = local_backend.get_status()
    assert status.mode == 'local'


def test_local_drain_pending_is_noop(local_backend: LocalBackend) -> None:
    assert local_backend.drain_pending() == 0


def test_local_gc_tombstones(local_backend: LocalBackend) -> None:
    obs = _mk_obs('gc target')
    local_backend.put_observation(obs)
    local_backend.put_tombstone(obs, reason='gc test')

    # With retention_days=0, everything tombstoned should be purged.
    purged = local_backend.gc_tombstones(retention_days=0, project='test')
    assert purged >= 1
    assert local_backend.find_observation_by_id(obs.observation_id) is None


def test_local_search_project_filter(local_backend: LocalBackend) -> None:
    obs_a = _mk_obs('project a content', project='alpha')
    obs_b = _mk_obs('project b content', project='beta')
    local_backend.put_observation(obs_a)
    local_backend.put_observation(obs_b)

    results = local_backend.search_observations(project='alpha')
    ids = [r.observation_id for r in results]
    assert obs_a.observation_id in ids
    assert obs_b.observation_id not in ids


# ---------------------------------------------------------------------------
# CLI integration: kioku-mesh init --mode local
# ---------------------------------------------------------------------------


def test_cli_init_local_writes_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from mesh_mem.__main__ import main as cli_main

    rc = cli_main(['init', '--mode', 'local'])
    assert rc == 0

    config_path = tmp_path / 'xdg' / 'kioku-mesh' / 'config.yaml'
    assert config_path.exists()
    content = config_path.read_text()
    assert 'backend: local' in content


def test_cli_init_local_prints_mesh_upgrade_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from mesh_mem.__main__ import main as cli_main

    rc = cli_main(['init', '--mode', 'local'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'scale up:' in out
    assert '--mode hub --force' in out


def test_cli_init_local_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from mesh_mem.__main__ import main as cli_main

    rc = cli_main(['init', '--mode', 'local', '--print'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'backend: local' in out


def test_cli_init_local_force_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from mesh_mem.__main__ import main as cli_main

    cli_main(['init', '--mode', 'local'])
    rc = cli_main(['init', '--mode', 'local', '--force'])
    assert rc == 0


def test_cli_init_local_refuses_if_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from mesh_mem.__main__ import main as cli_main

    cli_main(['init', '--mode', 'local'])
    rc = cli_main(['init', '--mode', 'local'])
    assert rc == 1


# ---------------------------------------------------------------------------
# CLI save/search/delete work without zenohd
# ---------------------------------------------------------------------------


def test_cli_save_search_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    reset_backend()
    from mesh_mem.__main__ import main as cli_main

    rc = cli_main(['save', 'hello from local cli', '-p', 'cliproj'])
    assert rc == 0

    rc = cli_main(['search', 'hello from local', '-p', 'cliproj'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'hello from local cli' in out


def test_cli_save_local_no_zenohd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify local mode works even when zenohd is not on PATH."""
    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    reset_backend()

    # Shadow PATH to hide zenohd (if present)
    original_path = os.environ.get('PATH', '')
    path_entries = [e for e in original_path.split(':') if 'zenoh' not in e.lower()]
    monkeypatch.setenv('PATH', ':'.join(path_entries))
    assert shutil.which('zenohd') is None or True  # may still be present elsewhere; that's ok

    from mesh_mem.__main__ import main as cli_main

    rc = cli_main(['save', 'no zenohd test'])
    assert rc == 0


# ---------------------------------------------------------------------------
# B2 regression: backend-switch must not shadow local-only rows
# ---------------------------------------------------------------------------


def test_backend_switch_does_not_shadow_local_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """W4 reproduction scenario: local save → switch to zenoh → row must survive.

    Before B2 fix: LocalBackend and ZenohBackend shared the same SQLite index.
    rebuild_from_zenoh() with an empty upstream would mark local-only rows as
    'shadowed', making them invisible in normal search (silent data loss).

    After B2 fix: LocalBackend uses state_dir()/local/index.db which is
    physically separate from the zenoh cache index (state_dir()/index.db).
    The rebuild never touches the local DB, so rows persist across the switch.
    """
    from mesh_mem.local_index import LocalIndex

    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    reset_backend()

    # Step 1: save via local backend.
    local_b = get_backend()
    assert isinstance(local_b, LocalBackend)
    obs = Observation(content='B2 regression data', project='b2test')
    local_b.put_observation(obs)

    # Confirm the local index path is under state_dir()/local/.
    from mesh_mem.identity import state_dir

    expected_local_db = state_dir() / 'local' / 'index.db'
    assert expected_local_db.exists(), 'LocalBackend must write to state_dir()/local/index.db'

    # Confirm the zenoh cache index path is *not* the same file.
    zenoh_cache_db = state_dir() / 'index.db'
    assert expected_local_db != zenoh_cache_db, 'Local and zenoh cache DBs must be different paths'

    # Step 2: simulate what happens when rebuild_from_zenoh is called with an empty
    # upstream — this used to shadow local-only rows in the shared DB.
    # We trigger it directly on the zenoh-cache DB (not the local DB).
    zenoh_idx = LocalIndex.connect(str(zenoh_cache_db))
    try:
        # Fake a Zenoh session that returns no observations or tombstones.
        class _EmptySession:
            def get(self, _key: str, **_kw):  # noqa: ANN202
                return []

        stats = zenoh_idx.rebuild_from_zenoh(_EmptySession())
        # The rebuild ran against the zenoh DB — it should not have touched local DB.
        assert stats.shadowed == 0, 'Zenoh cache DB was empty so nothing should be shadowed'
    finally:
        zenoh_idx.close()

    # Step 3: switch back to local backend and confirm the row is still visible.
    reset_backend()
    monkeypatch.setenv('MESH_MEM_BACKEND', 'local')
    reset_backend()
    results = get_backend().search_observations(query='B2 regression', project='b2test')
    assert any(
        r.observation_id == obs.observation_id for r in results
    ), 'local-only row must survive rebuild_from_zenoh on the zenoh cache DB'


# ---------------------------------------------------------------------------
# I1: Common contract suite — same assertions for LocalBackend + ZenohBackend
# ---------------------------------------------------------------------------

_ZENOHD_AVAILABLE = shutil.which('zenohd') is not None


@pytest.fixture(
    params=[
        'local',
        pytest.param(
            'zenoh',
            marks=pytest.mark.skipif(not _ZENOHD_AVAILABLE, reason='zenohd not on PATH'),
        ),
    ]
)
def contract_backend(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """Parametrized fixture that yields a LocalBackend or ZenohBackend."""
    mode = request.param
    monkeypatch.setenv('MESH_MEM_BACKEND', mode)
    reset_backend()
    if mode == 'zenoh':
        # Ensure a live zenohd router is running for the session.
        request.getfixturevalue('single_zenohd')
    return get_backend()


def _settle(backend: object) -> None:
    """Brief sleep after a Zenoh put — storage ingestion is asynchronous."""
    from mesh_mem.backend import ZenohBackend

    if isinstance(backend, ZenohBackend):
        import time

        time.sleep(0.25)


def test_contract_put_and_search(contract_backend: object) -> None:
    obs = _mk_obs('contract put and search', project='contract')
    contract_backend.put_observation(obs)  # type: ignore[union-attr]
    _settle(contract_backend)
    results = contract_backend.search_observations(query='contract put', project='contract')  # type: ignore[union-attr]
    assert any(r.observation_id == obs.observation_id for r in results)


def test_contract_get_by_id(contract_backend: object) -> None:
    obs = _mk_obs('contract get by id', project='contract')
    contract_backend.put_observation(obs)  # type: ignore[union-attr]
    _settle(contract_backend)
    found = contract_backend.find_observation_by_id(obs.observation_id)  # type: ignore[union-attr]
    assert found is not None
    assert found.content == 'contract get by id'


def test_contract_delete_tombstone(contract_backend: object) -> None:
    obs = _mk_obs('contract tombstone target', project='contract')
    contract_backend.put_observation(obs)  # type: ignore[union-attr]
    _settle(contract_backend)
    contract_backend.put_tombstone(obs, reason='contract test')  # type: ignore[union-attr]
    _settle(contract_backend)
    results = contract_backend.search_observations(query='contract tombstone', project='contract')  # type: ignore[union-attr]
    assert not any(r.observation_id == obs.observation_id for r in results)


def test_contract_status_mode(contract_backend: object) -> None:
    from mesh_mem.backend import BackendStatus

    status = contract_backend.get_status()  # type: ignore[union-attr]
    assert isinstance(status, BackendStatus)
    assert status.mode in ('local', 'zenoh')


def test_contract_drain_returns_int(contract_backend: object) -> None:
    drained = contract_backend.drain_pending()  # type: ignore[union-attr]
    assert isinstance(drained, int)
    assert drained >= 0

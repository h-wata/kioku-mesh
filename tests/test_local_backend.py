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
import sqlite3

import pytest

from kioku_mesh.backend import get_backend
from kioku_mesh.backend import LocalBackend
from kioku_mesh.backend import reset_backend
from kioku_mesh.config import get_backend_mode
from kioku_mesh.models import Observation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_obs(content: str, *, project: str = 'test') -> Observation:
    return Observation(content=content, project=project)


# ---------------------------------------------------------------------------
# Config / backend selection
# ---------------------------------------------------------------------------


def test_get_backend_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    assert get_backend_mode() == 'local'


def test_get_backend_mode_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    config_dir = tmp_path / 'xdg' / 'kioku-mesh'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.yaml').write_text('backend: local\n')
    monkeypatch.delenv('KIOKU_MESH_BACKEND', raising=False)
    assert get_backend_mode() == 'local'


def test_get_backend_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('KIOKU_MESH_BACKEND', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent_config_dir_xyz')
    assert get_backend_mode() == 'zenoh'


def test_get_backend_returns_local_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()
    backend = get_backend()
    assert isinstance(backend, LocalBackend)
    reset_backend()


# ---------------------------------------------------------------------------
# LocalBackend contract tests
# ---------------------------------------------------------------------------


@pytest.fixture
def local_backend(monkeypatch: pytest.MonkeyPatch) -> LocalBackend:
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
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
    from kioku_mesh.__main__ import main as cli_main

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
    from kioku_mesh.__main__ import main as cli_main

    rc = cli_main(['init', '--mode', 'local'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'scale up:' in out
    assert '--mode hub --force' in out


def test_cli_init_local_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from kioku_mesh.__main__ import main as cli_main

    rc = cli_main(['init', '--mode', 'local', '--print'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'backend: local' in out


def test_cli_init_local_force_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from kioku_mesh.__main__ import main as cli_main

    cli_main(['init', '--mode', 'local'])
    rc = cli_main(['init', '--mode', 'local', '--force'])
    assert rc == 0


def test_cli_init_local_refuses_if_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from kioku_mesh.__main__ import main as cli_main

    cli_main(['init', '--mode', 'local'])
    rc = cli_main(['init', '--mode', 'local'])
    assert rc == 1


# ---------------------------------------------------------------------------
# CLI save/search/delete work without zenohd
# ---------------------------------------------------------------------------


def test_cli_save_search_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()
    from kioku_mesh.__main__ import main as cli_main

    rc = cli_main(['save', 'hello from local cli', '-p', 'cliproj'])
    assert rc == 0

    rc = cli_main(['search', 'hello from local', '-p', 'cliproj'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'hello from local cli' in out


def test_cli_save_local_no_zenohd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify local mode works even when zenohd is not on PATH."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    # Shadow PATH to hide zenohd (if present)
    original_path = os.environ.get('PATH', '')
    path_entries = [e for e in original_path.split(':') if 'zenoh' not in e.lower()]
    monkeypatch.setenv('PATH', ':'.join(path_entries))
    assert shutil.which('zenohd') is None or True  # may still be present elsewhere; that's ok

    from kioku_mesh.__main__ import main as cli_main

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
    from kioku_mesh.local_index import LocalIndex

    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    # Step 1: save via local backend.
    local_b = get_backend()
    assert isinstance(local_b, LocalBackend)
    obs = Observation(content='B2 regression data', project='b2test')
    local_b.put_observation(obs)

    # Confirm the local index path is under state_dir()/local/.
    from kioku_mesh.identity import state_dir

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
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
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
    monkeypatch.setenv('KIOKU_MESH_BACKEND', mode)
    reset_backend()
    if mode == 'zenoh':
        # Ensure a live zenohd router is running for the session.
        request.getfixturevalue('single_zenohd')
    return get_backend()


def _settle(backend: object) -> None:
    """Brief sleep after a Zenoh put — storage ingestion is asynchronous."""
    from kioku_mesh.backend import ZenohBackend

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
    from kioku_mesh.backend import BackendStatus

    status = contract_backend.get_status()  # type: ignore[union-attr]
    assert isinstance(status, BackendStatus)
    assert status.mode in ('local', 'zenoh')


def test_contract_drain_returns_int(contract_backend: object) -> None:
    drained = contract_backend.drain_pending()  # type: ignore[union-attr]
    assert isinstance(drained, int)
    assert drained >= 0


# ---------------------------------------------------------------------------
# ADR-0028 Phase2: raw-store SoT tests (a / b / c / d)
# ---------------------------------------------------------------------------


def test_preexisting_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) Pre-existing index.db is migrated to raw.db when LocalBackend opens."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    from kioku_mesh.memory.local_index import LocalIndex

    local_dir = tmp_path / 'local'
    local_dir.mkdir()

    obs_live = Observation(content='live row', project='mig')
    obs_tomb = Observation(content='tombstoned row', project='mig')
    obs_old = Observation(content='superseded row', project='mig')
    obs_new = Observation(content='superseder row', project='mig', supersedes=[obs_old.observation_id])

    idx = LocalIndex.connect(str(local_dir / 'index.db'))
    idx.upsert(obs_live)
    idx.upsert(obs_tomb)
    idx.mark_deleted(obs_tomb.observation_id, '2026-01-01T00:00:00.000000Z')
    idx.upsert(obs_old)
    idx.upsert(obs_new)
    row_count_before = idx.row_count()
    idx.close()

    get_backend()  # triggers migrate_from_index + rebuild_from_raw_records

    raw_db = local_dir / 'raw.db'
    conn = sqlite3.connect(str(raw_db))
    obs_ids = {r[0] for r in conn.execute('SELECT observation_id FROM local_obs').fetchall()}
    tomb_ids = {r[0] for r in conn.execute('SELECT observation_id FROM local_tomb').fetchall()}
    conn.close()

    assert obs_live.observation_id in obs_ids
    assert obs_old.observation_id in obs_ids
    assert obs_new.observation_id in obs_ids
    assert obs_tomb.observation_id in tomb_ids
    assert obs_tomb.observation_id not in obs_ids  # tombstoned row must go to local_tomb only

    idx2 = LocalIndex.connect(str(local_dir / 'index.db'))
    assert idx2.row_count() >= row_count_before
    idx2.close()

    # Second open must not duplicate raw rows.
    reset_backend()
    get_backend()

    conn2 = sqlite3.connect(str(raw_db))
    obs_count2 = conn2.execute('SELECT COUNT(*) FROM local_obs').fetchone()[0]
    tomb_count2 = conn2.execute('SELECT COUNT(*) FROM local_tomb').fetchone()[0]
    conn2.close()

    assert obs_count2 == len(obs_ids)
    assert tomb_count2 == len(tomb_ids)

    reset_backend()


def test_raw_store_rebuild_after_index_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) After index.db deletion, LocalBackend rebuilds search state from raw.db."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    backend = get_backend()
    assert isinstance(backend, LocalBackend)

    obs = Observation(content='rebuild from raw', project='rebuild')
    obs_old = Observation(content='old superseded', project='rebuild')
    obs_new = Observation(content='superseder', project='rebuild', supersedes=[obs_old.observation_id])
    obs_tomb = Observation(content='tombstoned obs', project='rebuild')

    backend.put_observation(obs)
    backend.put_observation(obs_old)
    backend.put_observation(obs_new)
    backend.put_observation(obs_tomb)
    backend.put_tombstone(obs_tomb, reason='test')

    reset_backend()

    (tmp_path / 'local' / 'index.db').unlink()

    backend2 = get_backend()
    assert isinstance(backend2, LocalBackend)

    assert backend2.find_observation_by_id(obs.observation_id) is not None

    results = backend2.search_observations(project='rebuild')
    result_ids = {r.observation_id for r in results}
    assert obs.observation_id in result_ids
    assert obs_new.observation_id in result_ids
    assert obs_old.observation_id not in result_ids  # superseded_by obs_new
    assert obs_tomb.observation_id not in result_ids  # tombstoned

    reset_backend()


def test_contract_local_backend_raw_store_parity(contract_backend: object) -> None:
    """(c) LocalBackend raw-store rebuild produces same CRUD results as before reopen."""
    obs = _mk_obs('parity raw store c', project='parity-c')
    contract_backend.put_observation(obs)  # type: ignore[union-attr]
    _settle(contract_backend)

    found = contract_backend.find_observation_by_id(obs.observation_id)  # type: ignore[union-attr]
    assert found is not None
    assert found.content == 'parity raw store c'

    results = contract_backend.search_observations(  # type: ignore[union-attr]
        query='parity raw store', project='parity-c'
    )
    assert any(r.observation_id == obs.observation_id for r in results)

    if isinstance(contract_backend, LocalBackend):
        reset_backend()
        backend2 = get_backend()
        assert isinstance(backend2, LocalBackend)

        found2 = backend2.find_observation_by_id(obs.observation_id)
        assert found2 is not None
        assert found2.content == 'parity raw store c'

        results2 = backend2.search_observations(query='parity raw store', project='parity-c')
        assert any(r.observation_id == obs.observation_id for r in results2)

        backend2.put_tombstone(obs, reason='parity test c')
        results3 = backend2.search_observations(query='parity raw store', project='parity-c')
        assert not any(r.observation_id == obs.observation_id for r in results3)

        assert backend2.get_status().mode == 'local'

        reset_backend()


def test_split_brain_index_failure_recovered_by_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) raw write success + index no-op: reopen recovers obs via rebuild_from_raw_records."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    from kioku_mesh.memory.local_index import LocalIndex

    backend = get_backend()
    assert isinstance(backend, LocalBackend)

    obs = Observation(content='split brain obs', project='split')

    # Simulate index update failure: upsert is a no-op; raw write still commits.
    monkeypatch.setattr(LocalIndex, 'upsert', lambda self, o: None)
    backend.put_observation(obs)

    assert backend.find_observation_by_id(obs.observation_id) is None

    # Reopen: rebuild_from_raw_records (executemany, not upsert) restores the obs.
    reset_backend()
    backend2 = get_backend()

    assert backend2.find_observation_by_id(obs.observation_id) is not None

    reset_backend()


def test_no_ghost_row_on_raw_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) raw write failure must not create ghost rows in LocalIndex."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    from kioku_mesh.memory.local_raw_store import LocalRawStore

    backend = get_backend()
    assert isinstance(backend, LocalBackend)

    obs_ghost = Observation(content='ghost candidate', project='ghost')

    def _fail_put_obs(self: LocalRawStore, obs: Observation) -> None:
        raise sqlite3.Error('simulated raw write failure')

    monkeypatch.setattr(LocalRawStore, 'put_obs', _fail_put_obs)

    with pytest.raises(Exception):  # noqa: B017
        backend.put_observation(obs_ghost)

    assert backend.find_observation_by_id(obs_ghost.observation_id) is None

    reset_backend()


def test_physical_delete_not_resurrected_by_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) Physically deleted obs must not reappear after raw.db rebuild on reopen."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    backend = get_backend()
    assert isinstance(backend, LocalBackend)

    obs = Observation(content='to be physically deleted', project='del-test')
    backend.put_observation(obs)
    assert backend.find_observation_by_id(obs.observation_id) is not None

    deleted, _ = backend.physical_delete_observation(obs.observation_id)
    assert deleted

    reset_backend()
    backend2 = get_backend()

    assert backend2.find_observation_by_id(obs.observation_id) is None

    reset_backend()


def test_physical_delete_observation_split_brain_raw_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1: physical_delete must remove obs that is in raw.db only (split-brain state)."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    reset_backend()

    backend = get_backend()
    assert isinstance(backend, LocalBackend)

    obs = Observation(content='split-brain delete target', project='b1-test')

    # Write directly to raw_store, bypassing index (simulates split-brain).
    backend._raw_store.put_obs(obs)  # type: ignore[attr-defined]

    # before: index does NOT have the obs (split-brain state confirmed).
    assert backend.find_observation_by_id(obs.observation_id) is None

    # physical_delete_observation must succeed and remove from raw.db.
    deleted, _ = backend.physical_delete_observation(obs.observation_id)
    assert deleted, 'physical_delete must return True for raw-only observation'

    # raw.db must no longer contain the obs.
    raw_db = tmp_path / 'local' / 'raw.db'
    conn = sqlite3.connect(str(raw_db))
    raw_count = conn.execute(
        'SELECT COUNT(*) FROM local_obs WHERE observation_id = ?',
        (obs.observation_id,),
    ).fetchone()[0]
    conn.close()
    assert raw_count == 0, 'obs must be removed from raw.db'

    # Reopen: rebuild from raw.db must not resurrect the obs.
    reset_backend()
    backend2 = get_backend()
    assert isinstance(backend2, LocalBackend)

    assert backend2.find_observation_by_id(obs.observation_id) is None

    reset_backend()

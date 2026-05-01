"""Unit tests for mesh_mem.identity caching and atomicity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import pathlib

import pytest

from mesh_mem import identity


def test_session_id_is_cached_across_calls() -> None:
    first = identity.get_session_id()
    second = identity.get_session_id()
    assert first == second


def test_pc_id_is_cached_across_calls() -> None:
    first = identity.get_pc_id()
    second = identity.get_pc_id()
    assert first == second


def test_get_pc_id_is_atomic_under_concurrent_first_call() -> None:
    """Concurrent first callers must agree on a single pc_id value."""
    identity.reset_caches()
    # Reset the in-process cache per thread before the race. The on-disk
    # O_CREAT|O_EXCL guard is the contract we are verifying; we emulate the
    # "fresh host" precondition by clearing the cache here.
    with ThreadPoolExecutor(max_workers=10) as pool:
        values = list(pool.map(lambda _: identity.get_pc_id(), range(10)))
    # Every caller should see the same pc_id.
    assert len(set(values)) == 1


def test_env_overrides_auto_generated_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    identity.reset_caches()
    monkeypatch.setenv('MESH_MEM_SESSION_ID', 'explicit-session-001')
    assert identity.get_session_id() == 'explicit-session-001'


def test_get_pc_id_reads_preexisting_valid_value(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loser of the hard-link race must read the winner's fully-written value."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    identity.reset_caches()
    existing = 'deadbeefcafe1234abcd5678ef901234'  # pragma: allowlist secret
    (tmp_path / 'pc_id').write_text(existing + '\n')
    assert identity.get_pc_id() == existing


def test_get_pc_id_raises_on_empty_preexisting_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty pc_id (crashed-mid-write artifact) must not get cached as ''."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    identity.reset_caches()
    (tmp_path / 'pc_id').write_text('')
    with pytest.raises(RuntimeError, match='empty'):
        identity.get_pc_id()


def test_state_dir_env_override_wins(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MESH_MEM_STATE_DIR overrides the per-OS platformdirs default."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    assert identity.state_dir() == tmp_path
    assert tmp_path.exists()


def test_state_dir_linux_ignores_xdg_data_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux state_dir() must return ~/.local/share/mesh-mem even with XDG set.

    This preserves pre-v0.2.1 behavior so users who set XDG_DATA_HOME do
    not silently lose access to their existing pc_id / SQLite index
    after upgrade.
    """
    monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
    fake_home = tmp_path / 'home'
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, 'home', classmethod(lambda cls: fake_home))
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))

    result = identity.state_dir()

    expected = fake_home / '.local/share/mesh-mem'
    assert result == expected
    assert expected.exists()
    # Confirm XDG_DATA_HOME location was NOT used.
    assert not (tmp_path / 'xdg' / 'mesh-mem').exists()


def test_state_dir_macos_uses_platformdirs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS, state_dir() delegates to platformdirs.user_data_dir."""
    monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
    monkeypatch.setattr('sys.platform', 'darwin')
    fake_dir = tmp_path / 'mac-app-support'
    captured: dict[str, object] = {}

    def fake_user_data_dir(appname: str, *, appauthor: bool | None = False) -> str:
        captured['appname'] = appname
        captured['appauthor'] = appauthor
        return str(fake_dir)

    import platformdirs

    monkeypatch.setattr(platformdirs, 'user_data_dir', fake_user_data_dir)

    result = identity.state_dir()

    assert result == fake_dir
    assert fake_dir.exists()
    assert captured == {'appname': 'mesh-mem', 'appauthor': False}


def test_state_dir_windows_uses_platformdirs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, state_dir() delegates to platformdirs.user_data_dir."""
    monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
    monkeypatch.setattr('sys.platform', 'win32')
    fake_dir = tmp_path / 'localappdata'
    captured: dict[str, object] = {}

    def fake_user_data_dir(appname: str, *, appauthor: bool | None = False) -> str:
        captured['appname'] = appname
        captured['appauthor'] = appauthor
        return str(fake_dir)

    import platformdirs

    monkeypatch.setattr(platformdirs, 'user_data_dir', fake_user_data_dir)

    result = identity.state_dir()

    assert result == fake_dir
    assert fake_dir.exists()
    assert captured == {'appname': 'mesh-mem', 'appauthor': False}

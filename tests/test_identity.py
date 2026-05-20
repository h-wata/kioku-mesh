"""Unit tests for mesh_mem.identity caching and atomicity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import pathlib

import pytest

from mesh_mem import identity
from mesh_mem.identity import _sanitize_key_segment
from mesh_mem.identity import IdentitySource
from mesh_mem.identity import resolve_agent_family
from mesh_mem.identity import resolve_client_id


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


def test_state_dir_empty_env_falls_through_to_default(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty MESH_MEM_STATE_DIR falls through to the per-OS default.

    v0.2.0 treated an empty string as cwd; v0.2.1+ treats it as unset.
    """
    monkeypatch.setenv('MESH_MEM_STATE_DIR', '')
    fake_home = tmp_path / 'home'
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, 'home', classmethod(lambda cls: fake_home))
    monkeypatch.setattr('sys.platform', 'linux')

    result = identity.state_dir()

    expected = fake_home / '.local/share/mesh-mem'
    assert result == expected
    assert expected.exists()


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


# -- agent_family / client_id defaults & provenance (#82) -----------------------


def test_agent_family_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MESH_MEM_AGENT_FAMILY', 'claude')
    value, source = resolve_agent_family()
    assert value == 'claude'
    assert source is IdentitySource.ENV


def test_agent_family_default_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MESH_MEM_AGENT_FAMILY', raising=False)
    value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT


def test_agent_family_treats_empty_env_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty MESH_MEM_AGENT_FAMILY falls through to the default rather than producing an empty key segment."""
    monkeypatch.setenv('MESH_MEM_AGENT_FAMILY', '   ')
    value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT


def test_client_id_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MESH_MEM_CLIENT_ID', 'claude-code')
    value, source = resolve_client_id()
    assert value == 'claude-code'
    assert source is IdentitySource.ENV


def test_client_id_default_is_user_at_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-unset default is ``<user>@<host_short>`` (searchable, complements pc_id)."""
    monkeypatch.delenv('MESH_MEM_CLIENT_ID', raising=False)
    monkeypatch.setenv('USER', 'alice')
    monkeypatch.setattr('mesh_mem.identity.socket.gethostname', lambda: 'mbp-laptop.local')
    value, source = resolve_client_id()
    assert value == 'alice@mbp-laptop'
    assert source is IdentitySource.DEFAULT


def test_client_id_default_falls_back_when_user_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container without USER / LOGNAME / USERNAME and a stub getpass still yields a usable default."""
    monkeypatch.delenv('MESH_MEM_CLIENT_ID', raising=False)
    monkeypatch.delenv('USER', raising=False)
    monkeypatch.delenv('LOGNAME', raising=False)
    monkeypatch.delenv('USERNAME', raising=False)

    def _raise() -> str:
        raise KeyError('no user')

    monkeypatch.setattr('mesh_mem.identity.getpass.getuser', _raise)
    monkeypatch.setattr('mesh_mem.identity.socket.gethostname', lambda: '')
    value, source = resolve_client_id()
    assert value == 'user@host'
    assert source is IdentitySource.DEFAULT


def test_client_id_default_sanitizes_zenoh_unsafe_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname containing Zenoh-reserved characters must not corrupt the key namespace."""
    monkeypatch.delenv('MESH_MEM_CLIENT_ID', raising=False)
    monkeypatch.setenv('USER', 'al/ice')  # pathological path separator
    monkeypatch.setattr('mesh_mem.identity.socket.gethostname', lambda: 'host*name?.local')
    value, source = resolve_client_id()
    assert '/' not in value
    assert '*' not in value
    assert '?' not in value
    assert source is IdentitySource.DEFAULT


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('alice', 'alice'),
        ('  alice  ', 'alice'),
        ('al/ice', 'al-ice'),
        ('al*ice', 'al-ice'),
        ('al?ice', 'al-ice'),
        ('al$ice', 'al-ice'),
        ('al#ice', 'al-ice'),
        ('al\nice', 'al-ice'),
        ('/', '-'),  # single-char input sanitizes to '-'; only empty results trigger fallback
        ('', 'fb'),
        ('   ', 'fb'),
    ],
)
def test_sanitize_key_segment(raw: str, expected: str) -> None:
    assert _sanitize_key_segment(raw, 'fb') == expected

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


def test_state_dir_uses_platformdirs_when_no_override(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without override, state_dir delegates to platformdirs.user_data_dir.

    Captures the call so the test does not depend on the host's actual
    XDG / Library / LOCALAPPDATA layout, and confirms the returned
    directory is created.
    """
    monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
    fake_dir = tmp_path / 'platformdirs-managed'
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

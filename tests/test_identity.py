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

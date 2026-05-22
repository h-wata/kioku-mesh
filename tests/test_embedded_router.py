"""Tests for Tier 1 embedded zenoh router (mesh start / mesh join)."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

# Ensure subprocess calls resolve mesh_mem from this worktree, not another
# editable-installed worktree that may be active on the same interpreter.
_WORKTREE_SRC = str(Path(__file__).parent.parent / 'src')


def _wait_for_tcp(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll until TCP port accepts connections or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _subprocess_env(**extra: str) -> dict[str, str]:
    """Build subprocess env with PYTHONPATH pointing at this worktree's src."""
    env = os.environ.copy()
    env['PYTHONPATH'] = _WORKTREE_SRC
    env.update(extra)
    return env


def test_router_session_open_close() -> None:
    """In-process zenoh session opens and closes cleanly in mode=router."""
    import zenoh

    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"router"')
    cfg.insert_json5('listen/endpoints', '["tcp/127.0.0.1:17547"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    session = zenoh.open(cfg)
    assert session is not None
    assert not session.is_closed()
    session.close()


def test_inprocess_pubsub() -> None:
    """Same-process peer can pub/sub through an in-process router."""
    import zenoh

    router_cfg = zenoh.Config()
    router_cfg.insert_json5('mode', '"router"')
    router_cfg.insert_json5('listen/endpoints', '["tcp/127.0.0.1:17548"]')
    router_cfg.insert_json5('scouting/multicast/enabled', 'false')
    router = zenoh.open(router_cfg)

    peer_cfg = zenoh.Config()
    peer_cfg.insert_json5('mode', '"peer"')
    peer_cfg.insert_json5('connect/endpoints', '["tcp/127.0.0.1:17548"]')
    peer_cfg.insert_json5('scouting/multicast/enabled', 'false')
    peer = zenoh.open(peer_cfg)

    received: list[bytes] = []
    sub = router.declare_subscriber('test/topic', lambda s: received.append(bytes(s.payload)))
    time.sleep(0.2)
    peer.put('test/topic', b'hello')
    time.sleep(0.5)
    assert len(received) > 0
    assert received[0] == b'hello'

    sub.undeclare()
    peer.close()
    router.close()


def test_2process_save_search(tmp_path: Path) -> None:
    """Router in one process; client save/search in separate processes (no zenohd)."""
    port = 17549
    listen = f'tcp/127.0.0.1:{port}'
    state_dir = str(tmp_path / 'state')
    xdg_dir = str(tmp_path / 'xdg')

    router_proc = subprocess.Popen(
        [sys.executable, '-m', 'mesh_mem', 'mesh', 'start', '--listen', listen],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(
            MESH_MEM_STATE_DIR=state_dir,
            XDG_CONFIG_HOME=xdg_dir,
        ),
    )

    reachable = _wait_for_tcp('127.0.0.1', port, timeout=10.0)
    if not reachable:
        router_proc.terminate()
        router_proc.wait(timeout=3)
        pytest.fail(f'Embedded router did not start on {listen} within 10s')

    try:
        client_env = _subprocess_env(
            ZENOH_CONNECT=listen,
            MESH_MEM_BACKEND='zenoh',
            MESH_MEM_STATE_DIR=state_dir,
            XDG_CONFIG_HOME=xdg_dir,
        )

        save_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'save', 'test round-trip content'],
            env=client_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert save_result.returncode == 0, save_result.stderr

        # search uses the local SQLite index written by save (no --rebuild needed;
        # Tier 1 router has no storage plugin so Zenoh query returns empty)
        search_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'search', 'round-trip'],
            env=client_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert search_result.returncode == 0, search_result.stderr
        assert 'round-trip' in search_result.stdout, search_result.stdout

    finally:
        router_proc.terminate()
        try:
            router_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            router_proc.kill()

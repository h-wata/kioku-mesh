"""Tests for Tier 1 embedded zenoh router (mesh start / mesh join).

Key contract verified here:
  peer save via ZENOH_CONNECT → router's index subscriber receives it
  → router search returns peer-saved content (actual mesh exchange, not local SQLite only)
"""

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


def _free_port() -> int:
    """Pick an unused loopback TCP port (TOCTOU tolerable for tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


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

    port = _free_port()
    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"router"')
    cfg.insert_json5('listen/endpoints', f'["tcp/127.0.0.1:{port}"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    session = zenoh.open(cfg)
    assert session is not None
    assert not session.is_closed()
    session.close()


def test_inprocess_pubsub() -> None:
    """Same-process peer can pub/sub through an in-process router."""
    import zenoh

    port = _free_port()
    router_cfg = zenoh.Config()
    router_cfg.insert_json5('mode', '"router"')
    router_cfg.insert_json5('listen/endpoints', f'["tcp/127.0.0.1:{port}"]')
    router_cfg.insert_json5('scouting/multicast/enabled', 'false')
    router = zenoh.open(router_cfg)

    peer_cfg = zenoh.Config()
    peer_cfg.insert_json5('mode', '"peer"')
    peer_cfg.insert_json5('connect/endpoints', f'["tcp/127.0.0.1:{port}"]')
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


def test_actual_mesh_exchange(tmp_path: Path) -> None:
    """Peer save via ZENOH_CONNECT is visible from router search (actual mesh exchange).

    router_state and peer_state are distinct directories — this proves the
    router's search hits content that arrived via zenoh replication, not from
    the peer's own local SQLite.
    """
    port = _free_port()
    listen = f'tcp/127.0.0.1:{port}'
    router_state = str(tmp_path / 'router_state')
    peer_state = str(tmp_path / 'peer_state')
    xdg_dir = str(tmp_path / 'xdg')

    router_proc = subprocess.Popen(
        [sys.executable, '-m', 'mesh_mem', 'mesh', 'start', '--listen', listen],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(
            MESH_MEM_STATE_DIR=router_state,
            XDG_CONFIG_HOME=xdg_dir,
        ),
    )

    reachable = _wait_for_tcp('127.0.0.1', port, timeout=10.0)
    if not reachable:
        router_proc.terminate()
        router_proc.wait(timeout=3)
        pytest.fail(f'Embedded router did not start on {listen} within 10s')

    time.sleep(1.0)  # let index subscriber connect and stabilise

    unique_content = f'mesh-exchange-{port}-unique-content'
    try:
        # Peer save: DIFFERENT state_dir than router — proves mesh exchange
        peer_env = _subprocess_env(
            ZENOH_CONNECT=listen,
            MESH_MEM_BACKEND='zenoh',
            MESH_MEM_STATE_DIR=peer_state,
            XDG_CONFIG_HOME=xdg_dir,
        )
        save_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'save', unique_content],
            env=peer_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert save_result.returncode == 0, save_result.stderr

        time.sleep(1.5)  # wait for replication subscriber to write to router SQLite

        # Router search: uses ROUTER's state_dir — must hit via mesh replication
        router_search_env = _subprocess_env(
            ZENOH_CONNECT=listen,
            MESH_MEM_BACKEND='zenoh',
            MESH_MEM_STATE_DIR=router_state,
            XDG_CONFIG_HOME=xdg_dir,
        )
        search_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'search', 'mesh-exchange'],
            env=router_search_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert search_result.returncode == 0, search_result.stderr
        assert unique_content in search_result.stdout, (
            f'Peer-saved content not visible from router search.\n'
            f'Router state_dir: {router_state}\n'
            f'Peer state_dir: {peer_state}\n'
            f'Search stdout: {search_result.stdout!r}'
        )

    finally:
        router_proc.terminate()
        try:
            router_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            router_proc.kill()


def test_mesh_start_peer_hint_not_loopback_only(tmp_path: Path) -> None:
    """B3 regression: wildcard listen must show real-IP hint, not loopback-only for other hosts."""
    import signal as _signal
    import threading

    port = _free_port()
    listen = f'tcp/0.0.0.0:{port}'
    state_dir = str(tmp_path / 'state')

    # PYTHONUNBUFFERED=1 ensures print() flushes immediately so startup lines
    # are not held in the buffer when SIGINT is delivered (CI flaky fix: B4).
    router_proc = subprocess.Popen(
        [sys.executable, '-m', 'mesh_mem', 'mesh', 'start', '--listen', listen],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr so all output is in stdout
        bufsize=1,
        text=True,
        env=_subprocess_env(MESH_MEM_STATE_DIR=state_dir, PYTHONUNBUFFERED='1'),
    )

    collected: list[str] = []
    sentinel_event = threading.Event()

    def _reader() -> None:
        assert router_proc.stdout is not None
        for line in iter(router_proc.stdout.readline, ''):
            stripped = line.rstrip()
            collected.append(stripped)
            # startup is complete once 'Ctrl-C' appears (last startup print)
            if 'ctrl-c' in stripped.lower() or 'ctrl' in stripped.lower():
                sentinel_event.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Wait for startup sentinel before sending SIGINT
    if not sentinel_event.wait(timeout=15.0):
        router_proc.terminate()
        router_proc.wait(timeout=3)
        reader_thread.join(timeout=3)
        pytest.fail(f'Router startup sentinel not seen within 15s.\nCollected so far: {collected!r}')

    router_proc.send_signal(_signal.SIGINT)
    reader_thread.join(timeout=5)

    stdout_text = '\n'.join(collected)
    lines = collected

    # The "from other hosts" hint must NOT use loopback 127.0.0.1
    for line in lines:
        if 'other host' in line.lower():
            assert '127.0.0.1' not in line, (
                f'B3: other-host hint must not show loopback 127.0.0.1.\n'
                f'Offending line: {line!r}\n'
                f'Full output:\n{stdout_text}'
            )

    # Wildcard listen acknowledgement must appear
    assert any(
        '0.0.0.0' in ln or 'all interface' in ln.lower() for ln in lines
    ), f'Expected wildcard listen acknowledgement.\nOutput:\n{stdout_text}'


def test_mesh_join_long_running(tmp_path: Path) -> None:
    """I4: mesh join stays alive (foreground) and accumulates peer saves via replication."""
    import signal as _signal

    router_port = _free_port()
    router_listen = f'tcp/127.0.0.1:{router_port}'
    router_state = str(tmp_path / 'router_state')
    join_state = str(tmp_path / 'join_state')
    xdg_dir = str(tmp_path / 'xdg')

    router_proc = subprocess.Popen(
        [sys.executable, '-m', 'mesh_mem', 'mesh', 'start', '--listen', router_listen],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(MESH_MEM_STATE_DIR=router_state, XDG_CONFIG_HOME=xdg_dir),
    )

    reachable = _wait_for_tcp('127.0.0.1', router_port, timeout=10.0)
    if not reachable:
        router_proc.terminate()
        router_proc.wait(timeout=3)
        pytest.fail(f'Router did not start on {router_listen}')

    time.sleep(0.5)

    join_proc = subprocess.Popen(
        [sys.executable, '-m', 'mesh_mem', 'mesh', 'join', router_listen],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(MESH_MEM_STATE_DIR=join_state, XDG_CONFIG_HOME=xdg_dir),
    )

    time.sleep(1.0)  # let join subscriber connect

    try:
        # mesh join must still be running (foreground, not immediately exited)
        assert join_proc.poll() is None, (
            f'mesh join exited immediately (returncode={join_proc.returncode}). '
            'Expected foreground long-running process.'
        )

        unique_content = f'join-roundtrip-{router_port}'

        # Save via router endpoint — join process subscriber should receive it
        save_env = _subprocess_env(
            ZENOH_CONNECT=router_listen,
            MESH_MEM_BACKEND='zenoh',
            MESH_MEM_STATE_DIR=str(tmp_path / 'save_state'),
            XDG_CONFIG_HOME=xdg_dir,
        )
        save_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'save', unique_content],
            env=save_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert save_result.returncode == 0, save_result.stderr

        time.sleep(1.5)  # wait for replication to join's SQLite

        # join process's SQLite should now contain the save
        join_search_env = _subprocess_env(
            ZENOH_CONNECT=router_listen,
            MESH_MEM_BACKEND='zenoh',
            MESH_MEM_STATE_DIR=join_state,
            XDG_CONFIG_HOME=xdg_dir,
        )
        search_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'search', 'join-roundtrip'],
            env=join_search_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert search_result.returncode == 0, search_result.stderr
        assert unique_content in search_result.stdout, (
            f'join process did not accumulate peer save.\n'
            f'join_state: {join_state}\nSearch stdout: {search_result.stdout!r}'
        )

    finally:
        for proc in (join_proc, router_proc):
            if proc.poll() is None:
                proc.send_signal(_signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def test_doctor_embedded_router_status(tmp_path: Path) -> None:
    """Doctor reports embedded router as running when mesh start is active."""
    port = _free_port()
    listen = f'tcp/127.0.0.1:{port}'
    state_dir = str(tmp_path / 'state')

    router_proc = subprocess.Popen(
        [sys.executable, '-m', 'mesh_mem', 'mesh', 'start', '--listen', listen],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(MESH_MEM_STATE_DIR=state_dir),
    )

    reachable = _wait_for_tcp('127.0.0.1', port, timeout=10.0)
    if not reachable:
        router_proc.terminate()
        router_proc.wait(timeout=3)
        pytest.fail(f'Router did not start on {listen}')

    try:
        doctor_result = subprocess.run(
            [sys.executable, '-m', 'mesh_mem', 'doctor', '--json'],
            env=_subprocess_env(
                MESH_MEM_ROUTER_ENDPOINT=listen,
                MESH_MEM_STATE_DIR=state_dir,
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        import json

        data = json.loads(doctor_result.stdout)
        router_check = next((c for c in data['checks'] if c['name'] == 'embedded_router'), None)
        assert router_check is not None, 'embedded_router check not found in doctor output'
        assert router_check['status'] == 'pass', f'embedded_router status: {router_check}'
        assert router_check['details'].get('running') is True

    finally:
        router_proc.terminate()
        try:
            router_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            router_proc.kill()

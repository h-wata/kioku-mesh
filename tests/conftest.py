"""pytest fixtures for mesh-mem tests.

Layered fixtures:
    - ``isolated_state_dir``: redirects ``MESH_MEM_STATE_DIR`` at tmp path and
      resets identity / store caches. Always active.
    - ``single_zenohd`` (scope=session): launches one zenohd router on a random
      loopback port so integration tests share a single transport. Multicast
      scouting is disabled so tests never bleed out to the LAN.
    - ``dual_zenohd`` (scope=session): launches two linked zenohd routers for
      E2E sync tests (offline diff / tombstone propagation). Both sides
      configure ``replication`` with identical parameters; each side also
      exposes a ``stop()`` / ``start()`` hook so tests can simulate a split.

The zenohd fixtures are SKIPped if the ``zenohd`` binary is not on PATH so
the unit-only suite stays runnable without the native daemon installed.

Router config uses the ``memory`` volume (not ``rocksdb``) — we do not need
persistence across the test session and it removes the hard dependency on
the ``zenoh-backend-rocksdb`` plugin being installed on the test host.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from dataclasses import field
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

from mesh_mem import identity
from mesh_mem import store


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect MESH_MEM_STATE_DIR per test and reset identity / store / index caches."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    # MESH_MEM_INDEX_DB は state_dir() 配下に解決されるが、
    # 環境変数で別パスが指定されているとテストが本物の state_dir に書きに行ってしまうので削除。
    monkeypatch.delenv('MESH_MEM_INDEX_DB', raising=False)
    identity.reset_caches()
    # store._session / _index に他テストの残骸が残りうるので明示クリア。
    store._reset_session()
    store._reset_index()
    yield tmp_path
    identity.reset_caches()
    store._reset_session()
    store._reset_index()


def _purge_mem_keys() -> None:
    """Delete every key under ``mem/obs/**`` and ``mem/tomb/**`` on the current store.

    Enumerate-then-delete rather than wildcard-delete: storage-backend support
    for wildcard delete varies by Zenoh version, per-key delete is portable.
    """
    import time as _time

    sess = store.get_session()
    for prefix in ('mem/obs/**', 'mem/tomb/**'):
        keys = [str(r.ok.key_expr) for r in sess.get(prefix, timeout=2.0) if r.ok]
        for k in keys:
            sess.delete(k)
    # Storage absorbs deletes asynchronously — give it a beat before the
    # next test reads.
    _time.sleep(0.15)


@pytest.fixture(autouse=True)
def _mem_keys_clean_between_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    """Purge ``mem/**`` before any test that exercises a live zenohd router.

    Opt-in via ``single_zenohd`` being in the test's fixture closure. Tests
    that do not touch a router (pure unit tests) are untouched — opening a
    store session without a live endpoint would just raise.
    """
    if 'single_zenohd' in request.fixturenames:
        _purge_mem_keys()
    yield


def _zenohd_available() -> bool:
    return shutil.which('zenohd') is not None


def _free_port() -> int:
    """Pick an unused loopback TCP port. TOCTOU races are tolerable for tests."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _base_storage() -> dict:
    return {
        'key_expr': 'mem/**',
        'strip_prefix': 'mem',
        'volume': 'memory',
    }


def _router_config(
    port: int,
    peer_ports: list[int] | None = None,
    replication: dict | None = None,
) -> dict:
    """Build a zenohd JSON5 config dict. ``peer_ports`` drives the ``connect`` list."""
    storage = _base_storage()
    if replication is not None:
        storage['replication'] = replication
    config: dict = {
        'mode': 'router',
        'listen': {'endpoints': [f'tcp/127.0.0.1:{port}']},
        # Multicast scouting off — tests must not leak onto the developer LAN.
        'scouting': {'multicast': {'enabled': False}},
        'timestamping': {'enabled': {'router': True, 'peer': True, 'client': True}},
        'plugins': {
            'storage_manager': {
                'volumes': {'memory': {}},
                'storages': {'agent_mem': storage},
            },
        },
    }
    if peer_ports:
        config['connect'] = {'endpoints': [f'tcp/127.0.0.1:{p}' for p in peer_ports]}
    return config


def _wait_for_router(port: int, timeout: float = 10.0) -> None:
    """Block until a client session can connect to ``tcp/127.0.0.1:{port}``."""
    import zenoh

    deadline = time.monotonic() + timeout
    last_exc: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            cfg = zenoh.Config()
            cfg.insert_json5('mode', '"client"')
            cfg.insert_json5('connect/endpoints', f'["tcp/127.0.0.1:{port}"]')
            cfg.insert_json5('scouting/multicast/enabled', 'false')
            sess = zenoh.open(cfg)
            sess.close()
            return
        except Exception as e:  # noqa: BLE001 — probing a liveness endpoint
            last_exc = e
            time.sleep(0.2)
    raise RuntimeError(f'zenohd on port {port} not ready within {timeout:.1f}s: {last_exc}')


@dataclass
class _RouterHandle:
    port: int
    proc: subprocess.Popen[bytes] | None
    log_path: Path
    config_path: Path
    peer_ports: list[int] = field(default_factory=list)
    replication: dict | None = None

    @property
    def endpoint(self) -> str:
        return f'tcp/127.0.0.1:{self.port}'

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        """(Re-)start the router subprocess using the persisted config file."""
        if self.running:
            return
        logf = self.log_path.open('a')
        self.proc = subprocess.Popen(  # noqa: S603 — trusted args, test-only
            ['zenohd', '-c', str(self.config_path)],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_for_router(self.port)

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate the router; fall back to SIGKILL on timeout."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc = None


def _spawn_router(
    workdir: Path,
    tag: str,
    peer_ports: list[int] | None = None,
    replication: dict | None = None,
) -> _RouterHandle:
    port = _free_port()
    cfg_path = workdir / f'zenohd_{tag}_{port}.json5'
    log_path = workdir / f'zenohd_{tag}_{port}.log'
    cfg_path.write_text(json.dumps(_router_config(port, peer_ports, replication), indent=2))
    handle = _RouterHandle(
        port=port,
        proc=None,
        log_path=log_path,
        config_path=cfg_path,
        peer_ports=list(peer_ports or []),
        replication=replication,
    )
    handle.start()
    return handle


@pytest.fixture(scope='session')
def _zenohd_tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp('zenohd')


@pytest.fixture(scope='session')
def single_zenohd(_zenohd_tmp_root: Path) -> Iterator[_RouterHandle]:
    """Launch a single zenohd router for the whole test session."""
    if not _zenohd_available():
        pytest.skip('zenohd binary not found on PATH')
    handle = _spawn_router(_zenohd_tmp_root, 'single')
    old = os.environ.get('ZENOH_CONNECT')
    os.environ['ZENOH_CONNECT'] = handle.endpoint
    try:
        yield handle
    finally:
        if old is None:
            os.environ.pop('ZENOH_CONNECT', None)
        else:
            os.environ['ZENOH_CONNECT'] = old
        handle.stop()


@dataclass
class _DualHandle:
    a: _RouterHandle
    b: _RouterHandle


@pytest.fixture(scope='session')
def dual_zenohd(_zenohd_tmp_root: Path) -> Iterator[_DualHandle]:
    """Launch two peered zenohd routers with identical replication config."""
    if not _zenohd_available():
        pytest.skip('zenohd binary not found on PATH')
    # Replication numbers must match production configs under config/ byte-for-byte
    # so the test exercise matches real deployment.
    replication = {
        'interval': 2.0,  # shorter than prod's 10.0s to keep tests quick
        'sub_intervals': 5,
        'hot': 6,
        'warm': 30,
        'propagation_delay': 250,
    }
    # Allocate both ports up-front so each side's config lists the other.
    port_a = _free_port()
    port_b = _free_port()
    while port_b == port_a:
        port_b = _free_port()

    def _write(tag: str, port: int, peer_port: int) -> _RouterHandle:
        cfg_path = _zenohd_tmp_root / f'zenohd_{tag}_{port}.json5'
        log_path = _zenohd_tmp_root / f'zenohd_{tag}_{port}.log'
        cfg_path.write_text(
            json.dumps(_router_config(port, [peer_port], replication), indent=2),
        )
        return _RouterHandle(
            port=port,
            proc=None,
            log_path=log_path,
            config_path=cfg_path,
            peer_ports=[peer_port],
            replication=replication,
        )

    handle_a = _write('dualA', port_a, port_b)
    handle_b = _write('dualB', port_b, port_a)
    handle_a.start()
    handle_b.start()
    try:
        yield _DualHandle(a=handle_a, b=handle_b)
    finally:
        handle_b.stop()
        handle_a.stop()

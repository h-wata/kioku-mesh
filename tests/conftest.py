"""pytest fixtures for mesh-mem tests.

Layered fixtures:
    - ``isolated_state_dir``: redirects ``KIOKU_MESH_STATE_DIR`` at tmp path and
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

from kioku_mesh import identity
from kioku_mesh import store
from kioku_mesh.backend import reset_backend

from .wait_helpers import storage_missing
from .wait_helpers import wait_until

# Modules that must see the unpatched write gate. Everything that is itself a
# write sink belongs here; the bypass is only for tests whose subject is
# something else and whose stub sessions predate scope storages.
_REAL_SCOPE_GATE_MODULES = (
    'test_scope',
    'test_visibility_migration',
    'test_scope_migration',
    'test_two_node_scope_harness',
)


@pytest.fixture(autouse=True)
def scope_storages_rendered(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend this test host completed the scope storage cutover (design v3 task 1).

    The save preflight refuses any write whose scope has no exact live
    storage in the running zenohd. The test routers (and the many stub
    sessions in this suite) predate scope storages and serve a single broad
    ``mem/**`` — which does cover every key, legacy ones included — so
    without this fixture every write test would fail on the gate instead of
    on what it actually tests.

    The gate itself — declaration parsing, live-storage matching, and the
    fail-closed store / drain behavior — is exercised for real in
    ``tests/test_scope.py``, which is exempted here so it sees the unpatched
    functions.

    ``tests/test_visibility_migration.py`` is exempted too: migration is a
    write sink that DELETEs the legacy source right after its target PUT, so
    it has to be tested against the real gate — this bypass is what hid the
    missing migration gate (PR #316 review, B1). Those tests build
    gate-passing sessions of their own.

    ``tests/test_scope_migration.py`` and ``tests/test_two_node_scope_harness.py``
    are exempted for the same reason: the mesh re-PUT (task 5) publishes every
    manifest key, and its gate on live exact ``mesh`` storage is part of what
    those tests check.
    """
    if request.module.__name__.rsplit('.', 1)[-1] in _REAL_SCOPE_GATE_MODULES:
        return
    from kioku_mesh.core import scope as scope_mod

    def _every_key_has_storage(key_expr: str, session: object) -> scope_mod.PreflightVerdict:
        spec = scope_mod.scope_from_key(key_expr)
        return scope_mod.PreflightVerdict(True, key_expr, spec.label if spec else '')

    monkeypatch.setattr(scope_mod, 'evaluate_write_key', _every_key_has_storage)


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect KIOKU_MESH_STATE_DIR per test and reset identity / store / index caches."""
    monkeypatch.setenv('KIOKU_MESH_STATE_DIR', str(tmp_path))
    # KIOKU_MESH_INDEX_DB normally resolves under state_dir(), but if the env var
    # points elsewhere the test would write into the real state_dir — clear it.
    monkeypatch.delenv('KIOKU_MESH_INDEX_DB', raising=False)
    # Clear KIOKU_MESH_BACKEND so tests that don't set it use the default (zenoh).
    monkeypatch.delenv('KIOKU_MESH_BACKEND', raising=False)
    identity.reset_caches()
    # store._session / _index may carry stale state from previous tests — clear explicitly.
    store._reset_session()
    store._reset_index()
    reset_backend()
    yield tmp_path
    identity.reset_caches()
    store._reset_session()
    store._reset_index()
    reset_backend()


def _purge_mem_keys() -> None:
    """Delete every obs / tomb key (legacy + ADR-0019 tiered) on the current store.

    Enumerate-then-delete rather than wildcard-delete: storage-backend support
    for wildcard delete varies by Zenoh version, per-key delete is portable.
    """
    sess = store.get_session()
    prefixes = ('mem/**/obs/**', 'mem/**/tomb/**')
    for prefix in prefixes:
        keys = [str(r.ok.key_expr) for r in sess.get(prefix, timeout=2.0) if r.ok]
        for k in keys:
            sess.delete(k)
    # Storage absorbs deletes asynchronously. Wait for the keys to actually be
    # gone rather than for a duration: the next test's assertions are written
    # against an empty keyspace, and a fixed sleep that is occasionally too
    # short leaks a previous test's rows into them.
    for prefix in prefixes:
        wait_until(lambda p=prefix: storage_missing(sess, p), f'purge of {prefix} to be absorbed by storage')


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


def _mesh_scope_storage() -> dict:
    """Target-state storage for the mesh scope (design v3 task 1).

    ``mem/mesh/**`` with ``strip_prefix: mem`` and no broad storage beside
    it — what a host looks like after the storage cutover, which is what the
    save preflight requires.
    """
    return {
        'key_expr': 'mem/mesh/**',
        'strip_prefix': 'mem',
        'volume': 'memory',
    }


def _router_config(
    port: int,
    peer_ports: list[int] | None = None,
    replication: dict | None = None,
    storages: dict | None = None,
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
                'storages': storages if storages is not None else {'agent_mem': storage},
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
    storages: dict | None = None,
) -> _RouterHandle:
    port = _free_port()
    cfg_path = workdir / f'zenohd_{tag}_{port}.json5'
    log_path = workdir / f'zenohd_{tag}_{port}.log'
    cfg_path.write_text(json.dumps(_router_config(port, peer_ports, replication, storages), indent=2))
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
def _mesh_scope_router(_zenohd_tmp_root: Path) -> Iterator[_RouterHandle]:
    """Router configured the way a host looks *after* the scope storage cutover."""
    if not _zenohd_available():
        pytest.skip('zenohd binary not found on PATH')
    handle = _spawn_router(_zenohd_tmp_root, 'meshscope', storages={'mesh_store': _mesh_scope_storage()})
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture
def mesh_scope_zenohd(_mesh_scope_router: _RouterHandle, monkeypatch: pytest.MonkeyPatch) -> Iterator[_RouterHandle]:
    """Point this test (and any subprocess it spawns) at the post-cutover router.

    Needed by tests whose writes go through a *child process*, where the
    in-process preflight stub of ``scope_storages_rendered`` does not reach:
    the child runs the real gate, so it needs a router that really serves an
    exact ``mem/mesh/**`` storage.
    """
    monkeypatch.setenv('ZENOH_CONNECT', _mesh_scope_router.endpoint)
    store._reset_session()
    try:
        yield _mesh_scope_router
    finally:
        store._reset_session()


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

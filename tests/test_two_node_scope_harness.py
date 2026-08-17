"""Two-node Zenoh + RocksDB harness for the storage-scope cutover (design v3 task 4).

Five properties, one test each. Each runs real ``zenohd`` routers on isolated
loopback ports with their own ``ZENOH_BACKEND_ROCKSDB_ROOT`` and multicast /
gossip scouting disabled, so nothing here touches the production mesh:

1. :func:`test_alignment_ships_keys_outside_the_storage_key_expr` — replication
   alignment ships keys a storage's ``key_expr`` does not cover, as long as they
   are already in its directory. This is why the cutover may not reuse the old
   ``agent_mem`` directory.
2. :func:`test_differing_key_expr_makes_a_separate_replica_group` — a
   ``mem/**`` storage and a ``mem/mesh/**`` storage do not align with each
   other, which is what keeps the new mesh directory clean.
3. :func:`test_new_mesh_dir_stays_clean_after_cutover` — the design's step-6
   ``mem/**`` inventory probe on the cut-over peer returns mesh keys only, even
   though the same host's transitional legacy store was aligning legacy/user
   keys the whole time.
4. :func:`test_user_scope_never_reaches_a_peer_that_does_not_declare_it` — a
   ``user/`` observation reaches neither the RocksDB directory nor the
   ``LocalIndex`` of a peer that declares ``mesh`` only. Both are asserted:
   storage and subscriber are separate paths and either one alone would miss a
   regression in the other.
5. :func:`test_partial_cutover_leaves_the_stranded_host_unaligned` — a host left
   on the old broad config still receives live traffic but never gets the
   backlog the new mesh group already holds (N5). The lack of convergence is
   asserted against a live-traffic control so it cannot pass on a dead link.

Every directory inspection goes through :func:`_scan_dir`, which stops the node
and opens its RocksDB directory with a throwaway broad-storage router — a real
key scan of the directory, independent of what the node's own config serves.

These tests are marked ``integration`` because they spawn routers and wait for
replication (about a minute for the whole module). They stay in the default
``pytest tests/ -q`` run; to run only them::

    uv run pytest tests/test_two_node_scope_harness.py -q

The whole module SKIPs (with a reason, never silently) when ``zenohd`` or the
RocksDB backend plugin is missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

import pytest
import zenoh

from kioku_mesh import replication
from kioku_mesh import store
from kioku_mesh.core.scope import fetch_self_storages
from kioku_mesh.core.scope import parse_scope
from kioku_mesh.models import Observation

from .conftest import _free_port
from .conftest import _wait_for_router
from .wait_helpers import handshake
from .wait_helpers import storage_has
from .wait_helpers import wait_until

# pytest-timeout's global 60s (pyproject) is sized for unit tests; a router pair
# plus replication alignment needs more headroom than that.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

# Same shape as the production configs under config/, with a shorter interval:
# peers must agree on every field or they silently stop converging.
REPLICATION = {
    'interval': 1.0,
    'sub_intervals': 5,
    'hot': 6,
    'warm': 30,
    'propagation_delay': 250,
}

# Upper bounds, not expected durations — every positive wait below returns as
# soon as its condition holds.
ALIGN_TIMEOUT = 45.0
# For the one assertion that is about something NOT happening: long enough for
# many replication intervals, short enough to keep the suite usable.
NO_ALIGN_WINDOW = 15.0
NO_ALIGN_POLL = 0.5
GET_TIMEOUT = 1.0

_PROJECT = 'scope-harness'
_BARRIER_PROJECT = 'scope-harness-barrier'

_ROCKSDB_BACKEND_LIBS = (
    Path.home() / '.zenoh' / 'lib' / 'libzenoh_backend_rocksdb.so',
    Path('/usr/lib/libzenoh_backend_rocksdb.so'),
    Path('/usr/local/lib/libzenoh_backend_rocksdb.so'),
)


def _require_zenohd() -> None:
    """SKIP with a visible reason when the native pieces are missing.

    A silent pass would be worse than no test at all: this module's whole
    subject is the behavior of the real router.
    """
    if shutil.which('zenohd') is None:
        pytest.skip('zenohd binary not found on PATH — the two-node scope harness needs a real router')
    if not any(p.exists() for p in _ROCKSDB_BACKEND_LIBS):
        pytest.skip(
            'libzenoh_backend_rocksdb.so not found in '
            f'{[str(p) for p in _ROCKSDB_BACKEND_LIBS]} — the harness needs the RocksDB volume '
            '(the memory volume cannot be re-opened for a directory scan)'
        )


# -- router process ------------------------------------------------------------


def _storage(key_expr: str, strip_prefix: str, dir_name: str, *, replicated: bool = True) -> dict:
    entry: dict[str, Any] = {
        'key_expr': key_expr,
        'strip_prefix': strip_prefix,
        'volume': {'id': 'rocksdb', 'dir': dir_name, 'create_db': True},
    }
    if replicated:
        entry['replication'] = dict(REPLICATION)
    return entry


def _broad(dir_name: str) -> dict:
    """Return the pre-cutover storage: one broad ``mem/**`` over one directory."""
    return {'agent_mem': _storage('mem/**', 'mem', dir_name)}


def _scope_storages(*labels: str) -> dict:
    """Post-cutover storages, derived from the real scope contract.

    Going through :func:`kioku_mesh.core.scope.parse_scope` rather than
    hand-writing key expressions keeps the harness honest: if the contract's
    ``key_expr`` / ``strip_prefix`` / ``volume_dir`` ever change, these routers
    change with it.
    """
    specs = [parse_scope(label) for label in labels]
    return {spec.storage_name: _storage(spec.key_expr, spec.strip_prefix, spec.volume_dir) for spec in specs}


def _transitional(old_dir: str, mesh_dir: str) -> dict:
    """Design v3 step 3: read-only legacy source beside a clean, empty mesh store."""
    return {
        'legacy_source_store': _storage('mem/**', 'mem', old_dir),
        'mesh_store': _storage('mem/mesh/**', 'mem', mesh_dir),
    }


@dataclass
class _Node:
    """One zenohd process with its own RocksDB root, restartable with a new config."""

    name: str
    workdir: Path
    root: Path
    port: int
    proc: subprocess.Popen[bytes] | None = None
    _generation: int = 0

    @property
    def endpoint(self) -> str:
        return f'tcp/127.0.0.1:{self.port}'

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, storages: dict, connect: list[_Node] | None = None) -> None:
        """Start (or restart) this node serving ``storages``, connected to ``connect``."""
        if self.running:
            raise RuntimeError(f'node {self.name} is already running')
        self.root.mkdir(parents=True, exist_ok=True)
        self._generation += 1
        config = {
            'mode': 'router',
            'listen': {'endpoints': [self.endpoint]},
            'connect': {'endpoints': [peer.endpoint for peer in connect or []]},
            # Both scouting mechanisms off: this harness must never discover, or
            # be discovered by, the production mesh on the LAN.
            'scouting': {'multicast': {'enabled': False}, 'gossip': {'enabled': False}},
            'timestamping': {'enabled': {'router': True, 'peer': True, 'client': True}},
            'plugins': {'storage_manager': {'volumes': {'rocksdb': {}}, 'storages': storages}},
        }
        config_path = self.workdir / f'{self.name}-{self._generation}.json5'
        config_path.write_text(json.dumps(config, indent=2))
        log_path = self.workdir / f'{self.name}.log'
        with log_path.open('a') as logf:
            self.proc = subprocess.Popen(  # noqa: S603 — test-only, trusted args
                ['zenohd', '-c', str(config_path)],
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, 'ZENOH_BACKEND_ROCKSDB_ROOT': str(self.root)},
            )
        _wait_for_router(self.port)
        # The router accepts client sessions before storage_manager has opened
        # every backend, and a sample published into that window is simply not
        # stored by the storage that was still coming up — measured: seeding a
        # transitional node right after start left its mesh_store empty while
        # the already-loaded legacy store held everything, so the test's own
        # read-back said "stored" and the harness silently tested nothing.
        expected = set(storages)
        with _client(self) as session:
            wait_until(
                lambda: expected <= {s.name for s in fetch_self_storages(session)},
                f'storages {sorted(expected)} of node {self.name} to register in the admin space',
            )

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate the router; SIGKILL on timeout. Idempotent."""
        proc = self.proc
        self.proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


@contextmanager
def _client(node: _Node) -> Iterator[zenoh.Session]:
    config = zenoh.Config()
    config.insert_json5('mode', '"client"')
    config.insert_json5('connect/endpoints', json.dumps([node.endpoint]))
    config.insert_json5('scouting/multicast/enabled', 'false')
    session = zenoh.open(config)
    try:
        yield session
    finally:
        session.close()


def _local_keys(session: zenoh.Session, selector: str) -> set[str]:
    """Keys answered by the router this session is connected to, not by its peer.

    ``consolidation=NONE`` is required: the default collapses both sides' copies
    into one reply and the survivor is not necessarily the local one, so "the
    peer replicated it" and "the peer forwarded it" would be indistinguishable.
    """
    local_zids = {str(z) for z in session.info.routers_zid()}
    replies = session.get(selector, timeout=GET_TIMEOUT, consolidation=zenoh.ConsolidationMode.NONE)
    return {str(r.ok.key_expr) for r in replies if r.ok and str(r.replier_id.zid) in local_zids}


def _local_storages_holding(session: zenoh.Session, key: str) -> int:
    """How many storages of the *local* router answer ``key``.

    Replies carry ``(zid, eid)`` and each storage is its own entity, so on a
    transitional node — one broad store and one mesh store over different
    directories — a count of 2 means both hold the key and a count of 1 means
    only the broad one does. A plain "the key is readable on B" check cannot
    tell those apart, and would let a mesh-group test pass on a key that only
    ever arrived through the legacy group.
    """
    local_zids = {str(z) for z in session.info.routers_zid()}
    replies = session.get(key, timeout=GET_TIMEOUT, consolidation=zenoh.ConsolidationMode.NONE)
    return len({str(r.replier_id.eid) for r in replies if r.ok and str(r.replier_id.zid) in local_zids})


def _scan_dir(node: _Node, dir_name: str) -> set[str]:
    """Key scan of one RocksDB directory, with the owning node stopped.

    A throwaway router serves ``mem/**`` (``strip_prefix: mem``) over the
    directory and answers a single ``mem/**`` get, so the result is whatever the
    directory actually holds rather than what the node's own ``key_expr`` would
    admit today. It has no ``connect`` endpoints and no ``replication`` block,
    so it cannot pull anything in while it looks.
    """
    if node.running:
        raise RuntimeError(f'stop node {node.name} before scanning {dir_name}: RocksDB is single-writer')
    scanner = _Node(f'{node.name}-scan-{dir_name}', node.workdir, node.root, _free_port())
    scanner.start({'scan_store': _storage('mem/**', 'mem', dir_name, replicated=False)})
    try:
        with _client(scanner) as session:
            return _local_keys(session, 'mem/**')
    finally:
        scanner.stop()


def _obs(content: str, *, visibility: str = 'mesh', scope_id: str = '', project: str = _PROJECT) -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='scope-harness',
        pc_id='harness-pc',
        session_id='harness-session',
        visibility=visibility,
        scope_id=scope_id,
    )


def _seed(node: _Node, observations: list[Observation]) -> None:
    """Publish ``observations`` on ``node``, in order, on one session.

    ``handshake`` re-publishes until the key is readable, which closes the
    window where a freshly opened session's declarations have not reached the
    router yet and the sample is dropped outright.
    """
    with _client(node) as session:
        for obs in observations:
            handshake(
                lambda o=obs: session.put(o.key_expr, o.to_json()),
                lambda o=obs: storage_has(session, o.key_expr),
                f'{node.name} storage to hold {obs.key_expr}',
            )


@pytest.fixture
def two_nodes(tmp_path: Path) -> Iterator[tuple[_Node, _Node]]:
    """Yield a pair of un-started nodes with reserved ports and isolated RocksDB roots.

    Teardown stops both unconditionally, including on assertion failure, so no
    router outlives the test. ``tmp_path`` removal is pytest's (it keeps the
    last few runs, which is what makes a failure debuggable).
    """
    _require_zenohd()
    workdir = tmp_path / 'harness'
    workdir.mkdir()
    a = _Node('a', workdir, workdir / 'a-rocksdb', _free_port())
    b = _Node('b', workdir, workdir / 'b-rocksdb', _free_port())
    try:
        yield a, b
    finally:
        b.stop()
        a.stop()


# -- property 1 ----------------------------------------------------------------


def test_alignment_ships_keys_outside_the_storage_key_expr(two_nodes: tuple[_Node, _Node]) -> None:
    """Alignment copies a directory's existing keys even outside ``key_expr``.

    Narrowing the key expression over an existing directory only filters what
    the storage newly accepts; the replication log still offers everything the
    directory holds. Both nodes below declare ``mem/mesh/**``, so they are one
    replica group, and A's directory carries legacy and user keys from its broad
    past — which is exactly the state the cutover must not create.
    """
    a, b = two_nodes
    mesh = _obs('mesh tier key')
    legacy = _obs('legacy tier key', visibility='')
    user = _obs('user tier key', visibility='user', scope_id='hwata')

    a.start(_broad('old_a'))
    _seed(a, [mesh, legacy, user])
    a.stop()

    # The forbidden move: same directory, narrowed key_expr.
    a.start({'mesh_store': _storage('mem/mesh/**', 'mem', 'old_a')})
    b.start({'mesh_store': _storage('mem/mesh/**', 'mem', 'mesh_b')}, connect=[a])

    with _client(b) as session:
        wait_until(
            lambda: {legacy.key_expr, user.key_expr} <= _local_keys(session, 'mem/**'),
            "B's storage to receive the out-of-key_expr keys from A",
            timeout=ALIGN_TIMEOUT,
        )
    a.stop()
    b.stop()

    keys = _scan_dir(b, 'mesh_b')
    assert mesh.key_expr in keys, f'mesh key never aligned, so the scan proves nothing: {keys}'
    assert {legacy.key_expr, user.key_expr} <= keys, (
        'alignment was expected to ship the out-of-key_expr keys into a clean dir '
        f'(this is why the cutover uses a new directory); got {keys}'
    )


# -- properties 2 and 3 --------------------------------------------------------


def _converged_transitional_pair(a: _Node, b: _Node) -> tuple[Observation, Observation, Observation, Observation]:
    """Bring up the design's transitional state on both nodes and wait for it to settle.

    A holds a broad directory with all three tiers, then serves it read-only
    beside an empty ``mesh_store``; B starts with both directories empty. The
    coordinator's re-PUT then publishes the mesh manifest. Returns
    ``(mesh1, mesh2, legacy, user)``.
    """
    mesh1 = _obs('mesh key present before the split')
    legacy = _obs('legacy key that must stay behind', visibility='')
    user = _obs('user key that must stay behind', visibility='user', scope_id='hwata')
    mesh2 = _obs('mesh key added by the re-PUT')

    a.start(_broad('old_a'))
    _seed(a, [mesh1, legacy, user])
    a.stop()

    a.start(_transitional('old_a', 'mesh_a'))
    b.start(_transitional('old_b', 'mesh_b'), connect=[a])

    # Coordinator re-PUT of the manifest, exactly the design's step 4: the same
    # keys published again, which the broad group also re-absorbs harmlessly.
    _seed(a, [mesh1, mesh2])

    with _client(b) as session:
        wait_until(
            lambda: {legacy.key_expr, user.key_expr} <= _local_keys(session, 'mem/**'),
            "B's legacy source store to align the old directory (the control: alignment IS running)",
            timeout=ALIGN_TIMEOUT,
        )
        wait_until(
            lambda: all(_local_storages_holding(session, o.key_expr) == 2 for o in (mesh1, mesh2)),
            "B's mesh store (not just its broad legacy store) to hold the re-PUT manifest",
            timeout=ALIGN_TIMEOUT,
        )
    return mesh1, mesh2, legacy, user


def test_differing_key_expr_makes_a_separate_replica_group(two_nodes: tuple[_Node, _Node]) -> None:
    """``mem/**`` and ``mem/mesh/**`` storages do not align with each other.

    Both live on the same router here, and the broad one is busy replicating
    legacy and user keys across the link, yet none of that crosses into the
    mesh directory. That separation is what makes "new clean dir + re-PUT" a
    valid answer to B1.
    """
    a, b = two_nodes
    mesh1, mesh2, legacy, user = _converged_transitional_pair(a, b)

    a.stop()
    b.stop()
    old_b = _scan_dir(b, 'old_b')
    mesh_b = _scan_dir(b, 'mesh_b')

    assert {legacy.key_expr, user.key_expr} <= old_b, f"B's broad group did not converge: {old_b}"
    assert mesh_b == {
        mesh1.key_expr,
        mesh2.key_expr,
    }, f'the mesh replica group took keys from the broad group on the same host; mesh dir holds {mesh_b}'


def test_new_mesh_dir_stays_clean_after_cutover(two_nodes: tuple[_Node, _Node]) -> None:
    """The design's step-6 ``mem/**`` inventory probe returns mesh keys only.

    Run against the final config (mesh store alone, no peer), which is the check
    the runbook prescribes after the cutover.
    """
    a, b = two_nodes
    mesh1, mesh2, legacy, user = _converged_transitional_pair(a, b)

    a.stop()
    b.stop()
    b.start({'mesh_store': _storage('mem/mesh/**', 'mem', 'mesh_b')})
    with _client(b) as session:
        inventory = wait_until(
            lambda: _local_keys(session, 'mem/**') or set(),
            "B's final-config mesh store to answer the inventory probe",
        )
    b.stop()

    assert inventory == {
        mesh1.key_expr,
        mesh2.key_expr,
    }, f'non-mesh keys in the cut-over mesh dir: {sorted(inventory - {mesh1.key_expr, mesh2.key_expr})}'
    # The transitional legacy directory on the same host still holds them, so
    # the empty result above is separation, not an empty mesh.
    assert {legacy.key_expr, user.key_expr} <= _scan_dir(b, 'old_b')


# -- property 4 ----------------------------------------------------------------


def _indexed_ids(index: Any, project: str) -> set[str]:
    return {r.observation_id for r in index.search(project=project)}


def test_user_scope_never_reaches_a_peer_that_does_not_declare_it(
    two_nodes: tuple[_Node, _Node],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``user/`` observation reaches neither B's RocksDB dir nor B's LocalIndex.

    Storage scope and subscriber selectors are independent paths — a regression
    in either one alone would leak — so both are asserted. The mesh observation
    travelling the same two paths is the control that makes the two absences
    meaningful, and a barrier sample published last bounds the wait: it can only
    be indexed after the user sample ahead of it on the same session was handled.
    """
    a, b = two_nodes
    a.start(_scope_storages('mesh', 'user/hwata'))
    b.start(_scope_storages('mesh'), connect=[a])

    # This process now plays B's host: declares mesh only, read isolation on.
    config_home = tmp_path / 'b-config' / 'kioku-mesh'
    config_home.mkdir(parents=True)
    (config_home / 'config.yaml').write_text('storage_scopes:\n  - mesh\n')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'b-config'))
    monkeypatch.setenv('KIOKU_MESH_SCOPE_ISOLATION', 'enforce')
    monkeypatch.setenv('ZENOH_CONNECT', b.endpoint)
    store._reset_session()  # noqa: SLF001 — re-point the cached session at B

    mesh_obs = _obs('mesh observation B may hold')
    user_obs = _obs('user observation B must not hold', visibility='user', scope_id='hwata')
    barrier = _obs('barrier', project=_BARRIER_PROJECT)

    index = store.get_index()
    subscribers = replication.start_index_subscriber(store.get_session())
    try:
        _seed(a, [mesh_obs, user_obs, barrier])
        wait_until(
            lambda: barrier.observation_id in _indexed_ids(index, _BARRIER_PROJECT),
            'the barrier sample to be indexed on B',
            timeout=ALIGN_TIMEOUT,
        )
        indexed = _indexed_ids(index, _PROJECT)
        assert (
            mesh_obs.observation_id in indexed
        ), f'mesh obs never reached B index, so the absence proves nothing: {indexed}'
        assert user_obs.observation_id not in indexed, 'user-scope observation was indexed by a mesh-only peer'
    finally:
        for sub in subscribers:
            sub.undeclare()
        store._reset_session()  # noqa: SLF001

    a.stop()
    b.stop()
    mesh_dir_keys = _scan_dir(b, parse_scope('mesh').volume_dir)
    assert mesh_obs.key_expr in mesh_dir_keys, f'mesh obs never reached B storage: {mesh_dir_keys}'
    assert user_obs.key_expr not in mesh_dir_keys, 'user-scope key landed in a mesh-only peer directory'
    user_dir = b.root / parse_scope('user/hwata').volume_dir
    assert not user_dir.exists(), f'a user-scope directory was created on a peer that never declared it: {user_dir}'


# -- property 5 ----------------------------------------------------------------


def test_partial_cutover_leaves_the_stranded_host_unaligned(two_nodes: tuple[_Node, _Node]) -> None:
    """A host left on the old broad config never receives the new group's backlog (N5).

    Live traffic still flows — publications are routed regardless of replica
    groups — so the live key doubles as proof that the link is up while the
    backlog stays missing. Without that control, "did not converge" would also
    be what a broken link looks like.
    """
    a, b = two_nodes
    backlog = [_obs('published before B was linked'), _obs('second backlog key')]
    a.start(_scope_storages('mesh'))
    _seed(a, backlog)

    # B never ran the cutover: still one broad agent_mem.
    b.start(_broad('agent_mem'), connect=[a])
    live = _obs('published after the link came up')
    _seed(a, [live])

    backlog_keys = {o.key_expr for o in backlog}
    with _client(b) as session:
        wait_until(
            lambda: live.key_expr in _local_keys(session, 'mem/**'),
            'live traffic to reach the stranded host (control: the link is up)',
            timeout=ALIGN_TIMEOUT,
        )
        deadline = time.monotonic() + NO_ALIGN_WINDOW
        while time.monotonic() < deadline:
            aligned = backlog_keys & _local_keys(session, 'mem/**')
            assert not aligned, f'stranded host converged after all: {sorted(aligned)}'
            time.sleep(NO_ALIGN_POLL)

    a.stop()
    b.stop()
    keys = _scan_dir(b, 'agent_mem')
    assert live.key_expr in keys, f'live traffic never persisted on B, so the absence proves nothing: {keys}'
    assert not (backlog_keys & keys), f'backlog reached the stranded host: {sorted(backlog_keys & keys)}'

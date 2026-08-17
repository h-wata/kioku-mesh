"""Unit tests for the mesh re-PUT / inventory / purge tools (design v3 task 5).

This module is in ``conftest._REAL_SCOPE_GATE_MODULES``: ``replay_manifest`` is
a write sink (it PUTs every manifest key), so it has to be tested against the
unpatched write gate — the bypass fixture is what hid the missing migration
gate once already (PR #316 review B1). The sessions below therefore serve a
real admin-space storage list, built with ``test_scope``'s helpers.

The live two-node behavior (a real router pair, real RocksDB directories,
alignment) is covered in ``test_two_node_scope_harness.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from kioku_mesh.core import scope as scope_mod
from kioku_mesh.memory import scope_migration as sm

from .test_scope import FakeSession
from .test_scope import rendered_storages

_MESH_STORAGES = rendered_storages((scope_mod.ScopeSpec('mesh'),))


class _SourceSession(FakeSession):
    """Admin-space storage list plus a fake ``mem/mesh/**`` key space.

    ``data`` maps a key to the list of ``(zid, eid, payload)`` replies it gets,
    one per replying storage, which is what ``consolidation=NONE`` returns from
    a real mesh (review N3). Replies are only returned for selectors that
    intersect the key, matched on the selector's ``**`` prefix.
    """

    def __init__(
        self,
        data: dict[str, list[tuple[str, str, bytes]]] | None = None,
        storages: dict[str, dict[str, Any]] | None = None,
        *,
        fail_puts_after: int | None = None,
    ) -> None:
        super().__init__(storages if storages is not None else _MESH_STORAGES)
        self.data = data or {}
        self.fail_puts_after = fail_puts_after

    def get(self, selector: str, timeout: float = 0.0, consolidation: Any = None) -> list[Any]:
        if selector.startswith('@/'):
            return super().get(selector, timeout)
        self.selectors.append(selector)
        prefix = selector.removesuffix('**').rstrip('/')
        out: list[Any] = []
        for key, replies in self.data.items():
            if not key.startswith(prefix):
                continue
            for zid, eid, payload in replies:
                out.append(_DataReply(key, payload, zid, eid))
        return out

    def put(self, key_expr: str, payload: Any) -> None:
        if self.fail_puts_after is not None and len(self.puts) >= self.fail_puts_after:
            raise RuntimeError('simulated transport failure')
        self.puts.append((key_expr, payload))


class _DataReply:
    def __init__(self, key: str, payload: bytes, zid: str, eid: str) -> None:
        self.ok = _DataSample(key, payload)
        self.err = None
        self.replier_id = _ReplierId(zid, eid)


class _DataSample:
    def __init__(self, key: str, payload: bytes) -> None:
        self.key_expr = key
        self.payload = _Payload(payload)


class _Payload:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def to_bytes(self) -> bytes:
        return self._raw

    def to_string(self) -> str:
        return self._raw.decode()


class _ReplierId:
    def __init__(self, zid: str, eid: str) -> None:
        self.zid = zid
        self.eid = eid


@pytest.fixture(autouse=True)
def _mesh_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare mesh only and point at the local router, as a cut-over host does."""
    monkeypatch.setattr(scope_mod, 'get_storage_scopes', lambda: ['mesh'])
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/localhost:7447')


def _both_peers(payload: bytes) -> list[tuple[str, str, bytes]]:
    return [('ZID1', '1', payload), ('ZID2', '1', payload)]


def _two_peer_source(*, fail_puts_after: int | None = None) -> _SourceSession:
    return _SourceSession(
        {
            'mem/mesh/obs/proj/OBS1': _both_peers(b'{"observation_id":"OBS1"}'),
            'mem/mesh/tomb/claude/c/pc/s/OBS0': _both_peers(b'{"deleted_at":"x"}'),
            'mem/mesh/meta/whatever': _both_peers(b'raw-bytes'),
        },
        fail_puts_after=fail_puts_after,
    )


# -- manifest ------------------------------------------------------------------


def test_manifest_buckets_obs_tomb_and_other_separately() -> None:
    """Kinds stay in separate buckets so a stale tombstone cannot hide in obs (S5)."""
    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='2026-08-17T00:00:00Z')

    assert dict(manifest.kind_counts()) == {'obs': 1, 'tomb': 1, 'other': 1}
    assert manifest.peer_zids == ('ZID1', 'ZID2')
    # Per-peer attribution comes from replier_id, not from the key set (N3).
    assert [(r.zid, r.keys) for r in manifest.repliers] == [('ZID1', 3), ('ZID2', 3)]


def test_manifest_digest_covers_payload_bytes() -> None:
    """A changed payload changes the digest, so a checkpoint cannot follow it."""
    original = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')
    changed_source = _two_peer_source()
    changed_source.data['mem/mesh/obs/proj/OBS1'] = _both_peers(b'{"observation_id":"OBS1","edited":true}')
    changed = sm.build_manifest(changed_source, expected_peers=2, now_iso='t')

    assert original.digest != changed.digest


def test_manifest_refuses_conflicting_payloads_instead_of_picking_one() -> None:
    session = _two_peer_source()
    session.data['mem/mesh/obs/proj/OBS1'] = [('ZID1', '1', b'{"a":1}'), ('ZID2', '1', b'{"a":2}')]

    with pytest.raises(sm.ScopeMigrationError, match='conflicting payloads'):
        sm.build_manifest(session, expected_peers=2, now_iso='t')


def test_manifest_refuses_when_a_peer_did_not_answer() -> None:
    """Keys held only by an unreachable peer are absent from the union (N3)."""
    session = _two_peer_source()
    session.data = {k: [v[0]] for k, v in session.data.items()}  # only ZID1 answers

    with pytest.raises(sm.ScopeMigrationError, match='--expected-peers is 2'):
        sm.build_manifest(session, expected_peers=2, now_iso='t')


def test_manifest_round_trips_through_the_file(tmp_path: Path) -> None:
    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')
    path = tmp_path / 'manifest.json'
    sm.write_manifest(manifest, path)

    loaded = sm.read_manifest(path)
    assert loaded.digest == manifest.digest
    assert [e.payload for e in loaded.entries] == [e.payload for e in manifest.entries]


def test_read_manifest_rejects_a_tampered_file(tmp_path: Path) -> None:
    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')
    path = tmp_path / 'manifest.json'
    sm.write_manifest(manifest, path)
    data = json.loads(path.read_text())
    data['entries'][0]['sha256'] = '0' * 64
    path.write_text(json.dumps(data))

    with pytest.raises(sm.ScopeMigrationError, match='corrupt'):
        sm.read_manifest(path)


# -- checkpointed re-PUT -------------------------------------------------------


def test_replay_puts_every_key_and_verifies(tmp_path: Path) -> None:
    session = _two_peer_source()
    manifest = sm.build_manifest(session, expected_peers=2, now_iso='t')

    result = sm.replay_manifest(
        manifest, session=session, checkpoint_path=tmp_path / 'chk.json', now_iso='t', batch_size=2
    )

    assert result.put == 3
    assert {key for key, _ in session.puts} == {e.key for e in manifest.entries}
    assert sm.verify_reput(session, manifest).ok


def test_replay_refuses_a_batch_with_no_exact_mesh_storage(tmp_path: Path) -> None:
    """The pre-split broad storage must not carry the re-PUT (gate, not a warning)."""
    session = _two_peer_source()
    manifest = sm.build_manifest(session, expected_peers=2, now_iso='t')
    session.storages = {'agent_mem': {'key_expr': 'mem/**', 'strip_prefix': 'mem', 'volume': {'dir': 'agent_mem'}}}

    with pytest.raises(scope_mod.ScopePreflightError):
        sm.replay_manifest(manifest, session=session, checkpoint_path=tmp_path / 'chk.json', now_iso='t')

    assert session.puts == [], 'a refused gate must leave the target untouched'


def test_replay_resumes_where_it_stopped_and_gates_the_repeat(tmp_path: Path) -> None:
    """A resumed run re-PUTs only what is left, and still goes through the gate."""
    checkpoint = tmp_path / 'chk.json'
    interrupted = _two_peer_source(fail_puts_after=1)
    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')

    with pytest.raises(RuntimeError, match='simulated transport failure'):
        sm.replay_manifest(manifest, session=interrupted, checkpoint_path=checkpoint, now_iso='t', batch_size=1)
    assert len(interrupted.puts) == 1
    done_after_crash = sm.load_reput_checkpoint(checkpoint).done
    assert len(done_after_crash) == 1

    resumed = _two_peer_source()
    resumed.storages = {'agent_mem': {'key_expr': 'mem/**', 'strip_prefix': 'mem', 'volume': {'dir': 'agent_mem'}}}
    with pytest.raises(scope_mod.ScopePreflightError):
        sm.replay_manifest(manifest, session=resumed, checkpoint_path=checkpoint, now_iso='t')
    assert resumed.puts == [], 'the resumed repair PUT must pass the same gate'

    resumed.storages = _MESH_STORAGES
    result = sm.replay_manifest(manifest, session=resumed, checkpoint_path=checkpoint, now_iso='t')

    assert result.already_done == 1
    assert result.put == 2
    assert {key for key, _ in resumed.puts} == {e.key for e in manifest.entries if e.key not in done_after_crash}
    assert sorted(sm.load_reput_checkpoint(checkpoint).done) == sorted(e.key for e in manifest.entries)


def test_replay_refuses_a_checkpoint_from_another_manifest(tmp_path: Path) -> None:
    checkpoint = tmp_path / 'chk.json'
    sm.save_reput_checkpoint(sm.ReputCheckpoint(manifest_digest='deadbeef', done=[]), checkpoint)
    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')

    with pytest.raises(sm.ScopeMigrationError, match='belongs to manifest'):
        sm.replay_manifest(manifest, session=_two_peer_source(), checkpoint_path=checkpoint, now_iso='t')


def test_verify_reports_missing_and_mismatched_keys() -> None:
    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')
    live = _two_peer_source()
    del live.data['mem/mesh/meta/whatever']
    live.data['mem/mesh/obs/proj/OBS1'] = _both_peers(b'{"observation_id":"OBS1","drifted":true}')
    live.data['mem/mesh/obs/proj/UNEXPECTED'] = _both_peers(b'{}')

    report = sm.verify_reput(live, manifest)

    assert not report.ok
    assert report.missing == ('mem/mesh/meta/whatever',)
    assert report.digest_mismatch == ('mem/mesh/obs/proj/OBS1',)
    assert report.extra == ('mem/mesh/obs/proj/UNEXPECTED',)


# -- re-PUT --dry-run (CLI) ----------------------------------------------------


_BROAD_STORAGES = {'agent_mem': {'key_expr': 'mem/**', 'strip_prefix': 'mem', 'volume': {'dir': 'agent_mem'}}}


def _reput_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: _SourceSession, *, checkpoint: Path | None = None
) -> int:
    """Run ``scope-migrate re-put --dry-run`` against ``session``."""
    import argparse

    from kioku_mesh import __main__ as cli

    manifest = sm.build_manifest(_two_peer_source(), expected_peers=2, now_iso='t')
    manifest_path = tmp_path / 'manifest.json'
    sm.write_manifest(manifest, manifest_path)
    monkeypatch.setattr(cli, 'get_session', lambda: session)
    args = argparse.Namespace(
        manifest=str(manifest_path),
        checkpoint=str(checkpoint) if checkpoint else None,
        dry_run=True,
        yes=True,
        batch_size=100,
    )
    return cli._cmd_scope_migrate_reput(args)


def test_reput_dry_run_fails_on_a_transitional_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry-run must not report success where the real run would hit the gate (B1)."""
    session = _two_peer_source()
    session.storages = _BROAD_STORAGES

    rc = _reput_dry_run(tmp_path, monkeypatch, session)

    assert rc == 1
    assert 'migration refused' in capsys.readouterr().err
    assert session.puts == [], 'a dry-run never writes'


def test_reput_dry_run_passes_on_the_final_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _two_peer_source()

    rc = _reput_dry_run(tmp_path, monkeypatch, session)

    assert rc == 0
    assert 'would re-PUT 3 key(s)' in capsys.readouterr().out
    assert session.puts == []


def test_reput_dry_run_fails_on_a_checkpoint_from_another_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = tmp_path / 'chk.json'
    sm.save_reput_checkpoint(sm.ReputCheckpoint(manifest_digest='deadbeef', done=[]), checkpoint)

    rc = _reput_dry_run(tmp_path, monkeypatch, _two_peer_source(), checkpoint=checkpoint)

    assert rc == 1
    assert 'belongs to manifest' in capsys.readouterr().err


# -- inventory -----------------------------------------------------------------


def _index_db(path: Path, rows: list[tuple[str, str, str, str | None]]) -> Path:
    """Minimal ``obs_index`` with ``(id, visibility, scope_id, deleted_at)`` rows."""
    con = sqlite3.connect(path)
    con.execute(
        'CREATE TABLE obs_index (observation_id TEXT PRIMARY KEY, payload_json TEXT, '
        'deleted_at TEXT, shadowed_at TEXT)'
    )
    con.executemany(
        'INSERT INTO obs_index VALUES (?, ?, ?, NULL)',
        [
            (obs_id, json.dumps({'observation_id': obs_id, 'visibility': vis, 'scope_id': sid}), deleted)
            for obs_id, vis, sid, deleted in rows
        ],
    )
    con.commit()
    con.close()
    return path


def test_inventory_probe_labels_every_tier_and_stays_host_local() -> None:
    session = _SourceSession(
        {
            'mem/mesh/obs/proj/OBS1': [('ZID1', '1', b'{}')],
            'mem/mesh/tomb/claude/c/pc/s/OBS0': [('ZID1', '1', b'{}')],
            'mem/user/hwata/obs/proj/OBS2': [('ZID1', '1', b'{}')],
            'mem/obs/claude/c/pc/s/OBS3': [('ZID1', '1', b'{}')],
            'mem/mesh/obs/proj/PEER_ONLY': [('ZID2', '1', b'{}')],
        }
    )

    counts = sm.probe_inventory(session)

    assert dict(counts['mesh']) == {'obs': 1, 'tomb': 1}
    assert dict(counts['user/hwata']) == {'obs': 1}
    assert dict(counts['legacy']) == {'obs': 1}
    assert 'PEER_ONLY' not in str(counts), 'another router’s reply is not a host-local leftover'


def test_inventory_reports_sqlite_states_and_undeclared_scopes(tmp_path: Path) -> None:
    db = _index_db(
        tmp_path / 'index.db',
        [
            ('A', 'mesh', '', None),
            ('B', 'user', 'hwata', None),
            ('C', 'user', 'hwata', '2026-08-01T00:00:00Z'),
            ('D', '', '', None),
        ],
    )
    session = _SourceSession({'mem/user/hwata/obs/proj/B': [('ZID1', '1', b'{}')]})

    inventory = sm.scope_inventory(session, db_path=db)

    assert inventory.declared == ('mesh',)
    assert inventory.sqlite['user/hwata'] == {'live': 1, 'deleted': 1, 'shadowed': 0}
    assert inventory.sqlite['legacy'] == {'live': 1, 'deleted': 0, 'shadowed': 0}
    assert inventory.undeclared == ('user/hwata',), 'legacy is migrate-visibility’s business, not purge’s'


# -- host-local purge ----------------------------------------------------------


def test_purge_plan_targets_undeclared_scopes_only(tmp_path: Path) -> None:
    db = _index_db(
        tmp_path / 'index.db',
        [('A', 'mesh', '', None), ('B', 'user', 'hwata', None), ('C', 'team', 'sbgisen', None), ('D', '', '', None)],
    )
    root = tmp_path / 'rocksdb'
    for name in ('mesh', 'user_hwata', 'team_sbgisen', 'agent_mem', 'unrelated'):
        (root / name).mkdir(parents=True)
    # zenohd serves mesh and, say, team/sbgisen: the served directory is off limits.
    session = _SourceSession(
        storages=rendered_storages((scope_mod.ScopeSpec('mesh'), scope_mod.ScopeSpec('team', 'sbgisen')))
    )

    plan = sm.build_purge_plan(db_path=db, rocksdb_root=root, session=session)

    assert sorted(plan.rows) == ['team/sbgisen', 'user/hwata']
    assert [d.name for d in plan.dirs] == ['user_hwata']
    assert plan.row_count == 2


def test_purge_plan_keeps_every_directory_when_live_storages_are_unknown(tmp_path: Path) -> None:
    """No admin space is not evidence that nothing is served."""
    db = _index_db(tmp_path / 'index.db', [('B', 'user', 'hwata', None)])
    root = tmp_path / 'rocksdb'
    (root / 'user_hwata').mkdir(parents=True)

    plan = sm.build_purge_plan(db_path=db, rocksdb_root=root, session=None)

    assert plan.rows == {'user/hwata': ['B']}
    assert plan.dirs == ()


def test_execute_purge_deletes_rows_and_renames_the_directory_aside(tmp_path: Path) -> None:
    db = _index_db(tmp_path / 'index.db', [('B', 'user', 'hwata', None)])
    root = tmp_path / 'rocksdb'
    (root / 'user_hwata').mkdir(parents=True)
    (root / 'user_hwata' / 'CURRENT').write_text('x')
    session = _SourceSession()
    plan = sm.build_purge_plan(db_path=db, rocksdb_root=root, session=session)
    deleted: list[str] = []

    class _Index:
        def physical_delete(self, obs_id: str) -> None:
            deleted.append(obs_id)

    result = sm.execute_purge(plan, index=_Index(), now_stamp='20260817T000000Z')

    assert deleted == ['B']
    assert result.dirs_renamed == ((root / 'user_hwata', root / 'user_hwata.purged-20260817T000000Z'),)
    assert not (root / 'user_hwata').exists()
    assert (root / 'user_hwata.purged-20260817T000000Z' / 'CURRENT').read_text() == 'x', 'renamed, not deleted'


def test_purge_output_states_that_other_hosts_keep_their_copies() -> None:
    """The limit is invisible from a successful run, so it must be in the text."""
    assert 'other hosts' in sm.PURGE_LIMIT_NOTE
    assert 'No Zenoh delete' in sm.PURGE_LIMIT_NOTE

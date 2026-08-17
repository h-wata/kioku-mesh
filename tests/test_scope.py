"""Storage-scope contract, save preflight, and drain gating (design v3 task 1).

All tests drive a fake Zenoh session: they must never touch a real router
or the developer's own store. The central one is
``test_every_declared_write_lands_in_a_declared_storage`` — the invariant
that a write's resolved ``(visibility, scope_id)`` always has a live
storage on this host to hold it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kioku_mesh import config as config_mod
from kioku_mesh import doctor
from kioku_mesh import store
from kioku_mesh.core import keyspace
from kioku_mesh.core import scope
from kioku_mesh.core.keyspace import obs_key
from kioku_mesh.memory import pending_queue

# ---------------------------------------------------------------------------
# fake Zenoh session
# ---------------------------------------------------------------------------


class _FakeSample:
    def __init__(self, key_expr: str, payload: str) -> None:
        self.key_expr = key_expr
        self.payload = self
        self._payload = payload

    def to_string(self) -> str:
        return self._payload


class _FakeReply:
    def __init__(self, sample: _FakeSample) -> None:
        self.ok = sample


class _FakeInfo:
    def __init__(self, zid: str) -> None:
        self._zid = zid

    def routers_zid(self) -> list[str]:
        return [self._zid] if self._zid else []


class FakeSession:
    """Answers the admin-space get with a configurable storage list."""

    def __init__(
        self,
        storages: dict[str, dict[str, Any]] | None = None,
        zid: str = 'ZID1',
        *,
        serves_admin: bool = True,
    ) -> None:
        self.storages = storages if storages is not None else {}
        self.info = _FakeInfo(zid)
        self.selectors: list[str] = []
        self.puts: list[tuple[str, str]] = []
        # zenohd answers its admin base; the embedded router answers nothing.
        self.serves_admin = serves_admin

    def get(self, selector: str, timeout: float = 0.0) -> list[_FakeReply]:
        self.selectors.append(selector)
        if selector.endswith('/router'):
            return [_FakeReply(_FakeSample(selector, '{"version":"1.9.0"}'))] if self.serves_admin else []
        prefix = selector.split('/router/')[0]
        return [
            _FakeReply(
                _FakeSample(
                    f'{prefix}/router/status/plugins/storage_manager/storages/{name}',
                    json.dumps(body),
                )
            )
            for name, body in self.storages.items()
        ]

    def put(self, key_expr: str, payload: str) -> None:
        self.puts.append((key_expr, payload))


def storage_body(key_expr: str, strip_prefix: str, dir_name: str) -> dict[str, Any]:
    return {
        'key_expr': key_expr,
        'strip_prefix': strip_prefix,
        'volume': {'id': 'rocksdb', 'dir': dir_name, 'create_db': True},
    }


def rendered_storages(scopes: tuple[scope.ScopeSpec, ...]) -> dict[str, dict[str, Any]]:
    """Return the storage set a correct renderer would produce for ``scopes``."""
    return {s.storage_name: storage_body(s.key_expr, s.strip_prefix, s.volume_dir) for s in scopes}


@pytest.fixture(autouse=True)
def _local_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point at a local router so N6 does not fire in unrelated tests."""
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/localhost:7447')


def declare(monkeypatch: pytest.MonkeyPatch, entries: list[str] | None) -> None:
    monkeypatch.setattr(scope, 'get_storage_scopes', lambda: entries)


# ---------------------------------------------------------------------------
# scope parsing / resolution
# ---------------------------------------------------------------------------


def test_resolve_defaults_to_mesh_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, None)
    assert [s.label for s in scope.resolve_storage_scopes()] == ['mesh']


def test_resolve_keeps_declaration_order() -> None:
    scopes = scope.resolve_storage_scopes(['mesh', 'user/hwata', 'team/sbgisen'])
    assert [s.label for s in scopes] == ['mesh', 'user/hwata', 'team/sbgisen']


@pytest.mark.parametrize(
    'entries',
    [
        [],
        ['user/hwata'],  # mesh missing
        ['mesh', 'mesh'],  # duplicate
        ['mesh', 'user/*'],  # wildcard
        ['mesh', 'mem/**'],  # not a scope
        ['mesh', ''],  # empty entry
        ['mesh', 'user'],  # scoped tier without id
        ['mesh', 'mesh/extra'],  # mesh takes no id
        ['mesh', 'team/bad id'],  # invalid slug
        ['mesh', 'legacy'],
        ['mesh', 42],
        'mesh',  # not a list
    ],
)
def test_resolve_rejects_bad_declarations(entries: Any) -> None:
    with pytest.raises(scope.ScopeConfigError):
        scope.resolve_storage_scopes(entries)


def test_reads_storage_scopes_from_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip('yaml')
    cfg_dir = tmp_path / 'kioku-mesh'
    cfg_dir.mkdir()
    (cfg_dir / 'config.yaml').write_text('storage_scopes:\n  - mesh\n  - team/sbgisen\n', encoding='utf-8')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    assert config_mod.get_storage_scopes() == ['mesh', 'team/sbgisen']
    assert [s.label for s in scope.resolve_storage_scopes()] == ['mesh', 'team/sbgisen']


def test_project_config_cannot_declare_storage_scopes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """storage_scopes is host-global: a committed .kioku-mesh.yaml must not set it."""
    pytest.importorskip('yaml')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'empty-config'))
    project = tmp_path / 'proj'
    project.mkdir()
    (project / '.kioku-mesh.yaml').write_text('storage_scopes:\n  - mesh\n  - team/evil\n', encoding='utf-8')
    monkeypatch.chdir(project)
    assert config_mod.get_storage_scopes() is None


@pytest.mark.parametrize(
    ('label', 'key_expr', 'strip_prefix', 'volume_dir'),
    [
        ('mesh', 'mem/mesh/**', 'mem', 'mesh'),
        ('user/hwata', 'mem/user/hwata/**', 'mem/user/hwata', 'user_hwata'),
        ('team/sbgisen', 'mem/team/sbgisen/**', 'mem/team/sbgisen', 'team_sbgisen'),
    ],
)
def test_storage_spec_matches_design_table(label: str, key_expr: str, strip_prefix: str, volume_dir: str) -> None:
    spec = scope.parse_scope(label)
    assert (spec.key_expr, spec.strip_prefix, spec.volume_dir) == (key_expr, strip_prefix, volume_dir)
    assert spec.obs_read_key_expr == f'mem/{label}/obs/**'
    assert spec.tomb_read_key_expr == f'mem/{label}/tomb/**'


@pytest.mark.parametrize(
    ('key', 'expected'),
    [
        ('mem/mesh/obs/claude/c/pc/s/' + 'a' * 32, 'mesh'),
        ('mem/user/hwata/tomb/claude/c/pc/s/' + 'a' * 32, 'user/hwata'),
        ('mem/team/sbgisen/obs/claude/c/pc/s/' + 'a' * 32, 'team/sbgisen'),
        ('mem/obs/claude/c/pc/s/' + 'a' * 32, None),  # legacy
        ('msg/inbox/x', None),
        ('', None),
    ],
)
def test_scope_from_key(key: str, expected: str | None) -> None:
    spec = scope.scope_from_key(key)
    assert (spec.label if spec else None) == expected


# ---------------------------------------------------------------------------
# the write invariant
# ---------------------------------------------------------------------------


def test_every_declared_write_lands_in_a_declared_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the central write invariant.

    INVARIANT: the (visibility, scope_id) resolved for a write always has a
    live storage in this host's storage set that covers the resulting key.
    """
    entries = ['mesh', 'user/hwata', 'team/sbgisen']
    declare(monkeypatch, entries)
    scopes = scope.resolve_storage_scopes()
    session = FakeSession(rendered_storages(scopes))

    monkeypatch.setenv('XDG_CONFIG_HOME', '/nonexistent-kioku-test')
    monkeypatch.setenv('KIOKU_MESH_USER_ID', 'hwata')
    monkeypatch.setenv('KIOKU_MESH_TEAM_ID', 'sbgisen')
    for requested in ('mesh', 'user', 'team'):
        visibility, scope_id = config_mod.resolve_write_visibility(requested)
        spec = scope.scope_for_write(visibility, scope_id)
        assert spec.label in entries
        key = obs_key(visibility, scope_id, 'claude', 'cli', 'pc', 'sess', 'a' * 32)
        verdict = scope.evaluate_write_key(key, session)
        assert verdict.ok, verdict.message


def test_admin_selector_is_self_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """N1: the save path must never use the @/* wildcard selector."""
    declare(monkeypatch, ['mesh'])
    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()), zid='ABC123')
    scope.evaluate_write_key('mem/mesh/obs/claude/cli/pc/sess/' + 'a' * 32, session)
    assert session.selectors == ['@/ABC123/' + scope.ADMIN_STORAGES_SUFFIX]
    assert not any('@/*' in s for s in session.selectors)


def test_peer_selector_is_wildcard_for_doctor_only() -> None:
    session = FakeSession({'agent_mem': storage_body('mem/**', 'mem', 'agent_mem')}, zid='ABC123')
    peers = scope.fetch_peer_storages(session)
    assert session.selectors == ['@/*/' + scope.ADMIN_STORAGES_SUFFIX]
    assert [p.key_expr for p in peers] == ['mem/**']


# ---------------------------------------------------------------------------
# preflight rejection cases
# ---------------------------------------------------------------------------

MESH_KEY = 'mem/mesh/obs/claude/cli/pc/sess/' + 'a' * 32
TEAM_KEY = 'mem/team/other/obs/claude/cli/pc/sess/' + 'a' * 32


def test_refuses_scope_missing_from_storage_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh'])
    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()))
    verdict = scope.evaluate_write_key(TEAM_KEY, session)
    assert not verdict.ok
    assert 'storage_scopes' in verdict.reason
    assert 'render-storages' in verdict.hint


def test_refuses_when_storage_not_rendered_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared but zenohd still runs the old broad storage: no exact match."""
    declare(monkeypatch, ['mesh'])
    session = FakeSession({'agent_mem': storage_body('mem/**', 'mem', 'agent_mem')})
    verdict = scope.evaluate_write_key(MESH_KEY, session)
    assert not verdict.ok
    assert 'no exact mesh storage' in verdict.reason


def test_refuses_when_a_broad_storage_overlaps(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh'])
    storages = rendered_storages(scope.resolve_storage_scopes())
    storages['agent_mem'] = storage_body('mem/**', 'mem', 'agent_mem')
    verdict = scope.evaluate_write_key(MESH_KEY, FakeSession(storages))
    assert not verdict.ok
    assert 'overlapping broad storage' in verdict.reason


def test_refuses_legacy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh'])
    verdict = scope.evaluate_write_key('mem/obs/claude/cli/pc/sess/' + 'a' * 32, FakeSession())
    assert not verdict.ok
    assert 'visibility-scoped namespace' in verdict.reason


def test_refuses_when_admin_space_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh'])

    class Broken(FakeSession):
        def get(self, selector: str, timeout: float = 0.0) -> list[_FakeReply]:
            raise RuntimeError('router down')

    verdict = scope.evaluate_write_key(MESH_KEY, Broken())
    assert not verdict.ok
    assert 'live storage list' in verdict.reason


def test_refuses_when_endpoint_is_not_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """N6: a remote router's ZID would be mistaken for self."""
    declare(monkeypatch, ['mesh'])
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/192.168.128.12:7447')
    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()))
    verdict = scope.evaluate_write_key(MESH_KEY, session)
    assert not verdict.ok
    assert 'local router' in verdict.reason
    # the storage list may be read, but nothing about it is accepted as evidence


@pytest.mark.parametrize(
    ('endpoint', 'expected'),
    [
        ('tcp/localhost:7447', True),
        ('tcp/127.0.0.1:7447', True),
        ('tcp/[::1]:7447', True),
        ('tcp/192.168.128.12:7447', False),
        ('tls/hub.example.org:7447', False),
    ],
)
def test_local_router_endpoint_ok(endpoint: str, expected: bool) -> None:
    assert scope.local_router_endpoint_ok(endpoint) is expected


def test_preflight_raises_with_no_flag_to_downgrade_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed is unconditional — no env var turns a refusal into a warning."""
    declare(monkeypatch, ['mesh'])
    session = FakeSession({'agent_mem': storage_body('mem/**', 'mem', 'agent_mem')})
    monkeypatch.setenv('KIOKU_MESH_SCOPE_ISOLATION', 'off')  # read-path flag, must not weaken this
    with pytest.raises(scope.ScopePreflightError) as excinfo:
        scope.preflight_write_key(MESH_KEY, session)
    assert 'save refused' in str(excinfo.value)
    assert scope.preflight_write_key.__module__ == 'kioku_mesh.core.scope'


# ---------------------------------------------------------------------------
# store write path: fail-closed before put / upsert / enqueue
# ---------------------------------------------------------------------------


def _observation(visibility: str = 'team', scope_id: str = 'other') -> Any:
    from kioku_mesh.models import Observation

    return Observation(
        content='x',
        agent_family='claude',
        client_id='cli',
        pc_id='pc',
        session_id='sess',
        visibility=visibility,
        scope_id=scope_id,
        project='p',
        memory_type='pattern',
    )


def test_save_writes_nothing_when_preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """fail-closed: no zenoh put, no SQLite upsert, no pending-puts row."""
    declare(monkeypatch, ['mesh'])
    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()))
    monkeypatch.setattr(store, 'get_session', lambda: session)

    upserts: list[Any] = []
    monkeypatch.setattr(store, 'get_index', lambda: type('I', (), {'upsert': lambda _s, o: upserts.append(o)})())

    obs = _observation()
    with pytest.raises(scope.ScopePreflightError):
        store.put_observation(obs)
    assert session.puts == []
    assert upserts == []
    assert pending_queue._count_pending_puts() == 0


def test_save_succeeds_when_storage_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh', 'team/other'])
    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()))
    monkeypatch.setattr(store, 'get_session', lambda: session)
    monkeypatch.setattr(store, 'get_index', lambda: type('I', (), {'upsert': lambda _s, _o: None})())

    obs = _observation()
    store.put_observation(obs)
    assert [k for k, _ in session.puts] == [obs.key_expr]


# ---------------------------------------------------------------------------
# N4: drain must not delete rows the preflight refuses
# ---------------------------------------------------------------------------


def test_drain_keeps_entries_that_fail_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh'])
    obs = _observation()  # team/other — not declared
    pending_queue._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())
    assert pending_queue._count_pending_puts() == 1

    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()))
    monkeypatch.setattr(store, 'get_session', lambda: session)

    assert pending_queue.drain_pending_puts() == 0
    assert session.puts == []
    assert pending_queue._count_pending_puts() == 1  # row kept, not dropped

    blocked = pending_queue.scope_blocked_pending_puts()
    assert [k for k, _ in blocked] == [obs.key_expr]


def test_drain_replays_entries_that_pass_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh', 'team/other'])
    obs = _observation()
    pending_queue._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())

    session = FakeSession(rendered_storages(scope.resolve_storage_scopes()))
    monkeypatch.setattr(store, 'get_session', lambda: session)
    monkeypatch.setattr(store, 'get_index', lambda: type('I', (), {'upsert': lambda _s, _o: None})())

    assert pending_queue.drain_pending_puts() == 1
    assert pending_queue._count_pending_puts() == 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_passes_on_matching_storages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    declare(monkeypatch, ['mesh', 'user/hwata'])
    live = scope.fetch_self_storages(FakeSession(rendered_storages(scope.resolve_storage_scopes())))
    result = doctor.check_storage_scopes(live=live, config_path=tmp_path / 'zenohd.json5', blocked_pending=[])
    assert result.status is doctor.CheckStatus.PASS


def test_doctor_fails_when_scope_has_no_live_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    declare(monkeypatch, ['mesh', 'team/sbgisen'])
    live = scope.fetch_self_storages(FakeSession(rendered_storages((scope.parse_scope('mesh'),))))
    result = doctor.check_storage_scopes(live=live, config_path=tmp_path / 'zenohd.json5', blocked_pending=[])
    assert result.status is doctor.CheckStatus.FAIL
    assert any('team/sbgisen' in p for p in result.details['problems'])


def test_doctor_fails_on_leftover_broad_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    declare(monkeypatch, ['mesh'])
    storages = rendered_storages(scope.resolve_storage_scopes())
    storages['agent_mem'] = storage_body('mem/**', 'mem', 'agent_mem')
    live = scope.fetch_self_storages(FakeSession(storages))
    result = doctor.check_storage_scopes(live=live, config_path=tmp_path / 'zenohd.json5', blocked_pending=[])
    assert result.status is doctor.CheckStatus.FAIL
    assert any('agent_mem' in p for p in result.details['problems'])


def test_doctor_fails_on_wrong_volume_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    declare(monkeypatch, ['mesh'])
    live = scope.fetch_self_storages(FakeSession({'mesh_store': storage_body('mem/mesh/**', 'mem', 'agent_mem')}))
    result = doctor.check_storage_scopes(live=live, config_path=tmp_path / 'zenohd.json5', blocked_pending=[])
    assert result.status is doctor.CheckStatus.FAIL
    assert any('volume dir' in p for p in result.details['problems'])


def test_doctor_warns_when_router_is_not_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """N6: matching storages are not trustworthy when 'self' may be another host."""
    declare(monkeypatch, ['mesh'])
    live = scope.fetch_self_storages(FakeSession(rendered_storages(scope.resolve_storage_scopes())))
    result = doctor.check_storage_scopes(
        live=live,
        config_path=tmp_path / 'zenohd.json5',
        endpoint='tcp/192.168.128.12:7447',
        blocked_pending=[],
    )
    assert result.status is doctor.CheckStatus.WARN
    assert result.details['local_router_endpoint'] is False


def test_doctor_reports_blocked_pending_puts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    declare(monkeypatch, ['mesh'])
    live = scope.fetch_self_storages(FakeSession(rendered_storages(scope.resolve_storage_scopes())))
    result = doctor.check_storage_scopes(
        live=live,
        config_path=tmp_path / 'zenohd.json5',
        blocked_pending=[(TEAM_KEY, 'not in storage_scopes')],
    )
    assert result.status is doctor.CheckStatus.FAIL
    assert result.details['scope_blocked_pending_puts'][0]['key_expr'] == TEAM_KEY


def test_doctor_reads_replication_from_config_not_admin_space(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """N2: the admin space has no replication block, so the file is the source."""
    declare(monkeypatch, ['mesh'])
    cfg = tmp_path / 'zenohd.json5'
    cfg.write_text(
        """
        {
          mode: "router",
          plugins: { storage_manager: { volumes: { rocksdb: {} }, storages: {
            mesh_store: {
              key_expr: "mem/mesh/**",
              strip_prefix: "mem",
              replication: { interval: 10.0, sub_intervals: 5, hot: 6, warm: 30, propagation_delay: 250 },
              volume: { id: "rocksdb", dir: "mesh", create_db: true },
            },
          } } },
        }
        """,
        encoding='utf-8',
    )
    live = scope.fetch_self_storages(FakeSession(rendered_storages(scope.resolve_storage_scopes())))
    result = doctor.check_storage_scopes(live=live, config_path=cfg, blocked_pending=[])
    assert result.status is doctor.CheckStatus.PASS
    assert result.details['replication_from_config']['mesh_store']['interval'] == 10.0
    assert 'admin space does not expose replication' in result.details['replication_source']
    # the live definitions the admin space returned carry no replication field
    assert not any(hasattr(s, 'replication') for s in live)


def test_storage_scopes_check_is_registered() -> None:
    assert 'check_storage_scopes' in doctor._CHECK_ORDER


# ---------------------------------------------------------------------------
# read path selectors (design v3 task 2)
# ---------------------------------------------------------------------------


def _includes(outer: str, inner: str) -> bool:
    import zenoh

    return zenoh.KeyExpr(outer).includes(zenoh.KeyExpr(inner))


def test_read_selectors_stay_global_while_isolation_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (staged) behavior: read everything, exactly as before."""
    declare(monkeypatch, ['mesh', 'team/sbgisen'])
    monkeypatch.delenv('KIOKU_MESH_SCOPE_ISOLATION', raising=False)
    assert scope.obs_read_selectors() == (keyspace.OBS_READ_KEY_EXPR,)
    assert scope.tomb_read_selectors() == (keyspace.TOMB_READ_KEY_EXPR,)


def test_read_selectors_are_inside_declared_scopes_when_enforcing(monkeypatch: pytest.MonkeyPatch) -> None:
    """INVARIANT: every selector read under enforcement is covered by a declared scope.

    Nothing outside this host's storage_scopes may reach the subscriber, the
    rebuild scan, or the purge sweep — that is what read-path isolation means.
    """
    declare(monkeypatch, ['mesh', 'user/hwata', 'team/sbgisen'])
    monkeypatch.setenv('KIOKU_MESH_SCOPE_ISOLATION', 'enforce')
    declared = scope.resolve_storage_scopes()

    selectors = scope.obs_read_selectors() + scope.tomb_read_selectors()
    assert selectors  # never empty: an empty selector set would silently index nothing
    for selector in selectors:
        assert any(_includes(spec.key_expr, selector) for spec in declared), selector
    # and every declared scope is actually covered — isolation must not drop a scope
    for spec in declared:
        assert spec.obs_read_key_expr in selectors
        assert spec.tomb_read_key_expr in selectors


def test_enforced_selectors_exclude_foreign_and_legacy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh', 'team/sbgisen'])
    monkeypatch.setenv('KIOKU_MESH_SCOPE_ISOLATION', 'enforce')
    selectors = scope.obs_read_selectors() + scope.tomb_read_selectors()
    foreign = (
        'mem/team/other/obs/claude/c/pc/s/' + 'a' * 32,
        'mem/user/someone/obs/claude/c/pc/s/' + 'a' * 32,
        'mem/obs/claude/c/pc/s/' + 'a' * 32,
    )
    for key in foreign:
        assert not any(_includes(selector, key) for selector in selectors), key


def test_read_isolation_rejects_invalid_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken storage_scopes must not silently fall back to reading everything."""
    declare(monkeypatch, ['user/hwata'])  # mesh missing
    monkeypatch.setenv('KIOKU_MESH_SCOPE_ISOLATION', 'enforce')
    with pytest.raises(scope.ScopeConfigError):
        scope.obs_read_selectors()


def test_subscriber_declares_one_subscription_per_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replication subscriber must go through the scope API, not the constants."""
    declare(monkeypatch, ['mesh', 'team/sbgisen'])
    monkeypatch.setenv('KIOKU_MESH_SCOPE_ISOLATION', 'enforce')

    declared: list[str] = []

    class _SubSession:
        def declare_subscriber(self, key_expr: str, handler: object) -> object:  # noqa: ARG002
            declared.append(key_expr)
            return object()

    from kioku_mesh.memory import replication

    monkeypatch.setattr(
        replication,
        '_store',
        lambda: type('S', (), {'get_index': staticmethod(lambda: type('I', (), {'disabled': False})())}),
    )
    subs = replication.start_index_subscriber(_SubSession())
    assert len(subs) == len(declared)
    assert sorted(declared) == sorted(scope.obs_read_selectors() + scope.tomb_read_selectors())


# ---------------------------------------------------------------------------
# Tier 1 embedded router exception (mesh-only)
# ---------------------------------------------------------------------------


def embedded_session() -> FakeSession:
    """Return a `mesh start` router: no admin space, no storages."""
    return FakeSession(storages={}, serves_admin=False)


def test_detects_embedded_router_by_absent_admin_space() -> None:
    assert scope.is_storageless_embedded_router(embedded_session()) is True
    # a real zenohd answers its admin base even when no storage is configured
    assert scope.is_storageless_embedded_router(FakeSession(storages={})) is False


def test_mesh_save_passes_through_embedded_router(monkeypatch: pytest.MonkeyPatch) -> None:
    declare(monkeypatch, ['mesh'])
    verdict = scope.evaluate_write_key(MESH_KEY, embedded_session())
    assert verdict.ok
    assert 'Tier 1 exception' in verdict.note


def test_embedded_router_still_refuses_scoped_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exception is mesh-only: user/team must not leak through a storage-less router."""
    declare(monkeypatch, ['mesh', 'team/other', 'user/hwata'])
    for key in (TEAM_KEY, 'mem/user/hwata/obs/claude/cli/pc/sess/' + 'a' * 32):
        verdict = scope.evaluate_write_key(key, embedded_session())
        assert not verdict.ok, key


def test_embedded_exception_needs_a_mesh_only_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that also stores team data is not in the Tier 1 quickstart case."""
    declare(monkeypatch, ['mesh', 'team/sbgisen'])
    verdict = scope.evaluate_write_key(MESH_KEY, embedded_session())
    assert not verdict.ok


def test_zenohd_without_storages_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent storages alone must never open the gate — only a storage-less embedded router does."""
    declare(monkeypatch, ['mesh'])
    verdict = scope.evaluate_write_key(MESH_KEY, FakeSession(storages={}))
    assert not verdict.ok
    assert 'no exact mesh storage' in verdict.reason


def test_exception_is_reported_to_the_user_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    declare(monkeypatch, ['mesh'])
    scope.reset_exception_notice()
    session = embedded_session()
    with caplog.at_level('INFO', logger='kioku_mesh.core.scope'):
        scope.preflight_write_key(MESH_KEY, session)
        scope.preflight_write_key(MESH_KEY, session)
    notices = [r for r in caplog.records if 'Tier 1 exception' in r.getMessage()]
    assert len(notices) == 1


def test_remote_embedded_router_is_refused_before_the_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """N6 precedes Tier 1 (B2): a remote embedded router is not this host's mesh start."""
    declare(monkeypatch, ['mesh'])
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/192.168.128.12:7447')
    session = embedded_session()
    verdict = scope.evaluate_write_key(MESH_KEY, session)
    assert not verdict.ok
    assert not verdict.note
    assert 'local router' in verdict.reason
    assert session.puts == []


def test_remote_embedded_router_save_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing lands anywhere: no zenoh put, no SQLite upsert, no pending-puts row."""
    declare(monkeypatch, ['mesh'])
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/192.168.128.12:7447')
    session = embedded_session()
    monkeypatch.setattr(store, 'get_session', lambda: session)
    upserts: list[Any] = []
    monkeypatch.setattr(store, 'get_index', lambda: type('I', (), {'upsert': lambda _s, o: upserts.append(o)})())

    obs = _observation(visibility='mesh', scope_id='')
    with pytest.raises(scope.ScopePreflightError):
        store.put_observation(obs)
    assert session.puts == []
    assert upserts == []
    assert pending_queue._count_pending_puts() == 0


def test_doctor_does_not_claim_the_exception_for_a_remote_embedded_router(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Doctor must not advertise the Tier 1 exception the write gate refuses (B2)."""
    declare(monkeypatch, ['mesh'])
    result = doctor.check_storage_scopes(
        live=[],
        embedded_router=True,
        endpoint='tcp/192.168.128.12:7447',
        config_path=tmp_path / 'zenohd.json5',
        blocked_pending=[],
    )
    assert result.status is doctor.CheckStatus.FAIL
    assert 'not stored durably' not in result.summary
    assert result.details['local_router_endpoint'] is False


def test_doctor_reports_the_embedded_router_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    declare(monkeypatch, ['mesh'])
    result = doctor.check_storage_scopes(
        live=[],
        embedded_router=True,
        config_path=tmp_path / 'zenohd.json5',
        blocked_pending=[],
    )
    assert result.status is doctor.CheckStatus.WARN
    assert 'not stored durably' in result.summary
    assert result.details['embedded_router'] is True

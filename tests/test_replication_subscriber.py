"""Tests for Phase 4: startup rebuild and replication subscriber.

test_startup_rebuild_runs_when_index_empty and the two subscriber tests
require a live zenohd (single_zenohd fixture). The env-var skip test
is a pure unit test and does not need a router.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
import zenoh

from mesh_mem import replication
from mesh_mem import store
from mesh_mem.models import Observation
from mesh_mem.models import Tombstone

_SETTLE = 0.4  # seconds to wait for async subscriber delivery


def _mk_obs(content: str, *, project: str = 'sub-test') -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test-client',
        pc_id='test-pc',
        session_id='test-session',
    )


def _remote_session(endpoint: str) -> zenoh.Session:
    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"client"')
    cfg.insert_json5('connect/endpoints', f'["{endpoint}"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    return zenoh.open(cfg)


def test_subscriber_picks_up_remote_put_into_index(single_zenohd: Any) -> None:
    """A put from a remote session lands in the local index via the subscriber."""
    idx = store.get_index()
    assert not idx.disabled

    obs = _mk_obs('replicated content', project='sub-obs')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, obs.to_json())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    ids = {r.observation_id for r in idx.search(project='sub-obs')}
    assert obs.observation_id in ids, 'subscriber must upsert replicated obs into index'


def test_subscriber_preserves_extras_end_to_end(single_zenohd: Any) -> None:
    """E2E for Issue #107 / ADR-0012: unknown fields survive the replication path.

    A remote peer running a NEWER schema publishes an observation whose
    payload carries fields this build does not know. The local subscriber
    must round-trip them: from_json (stash in ``_extras``) -> SQLite upsert
    (``to_json`` re-merges extras into payload_json) -> search re-parse.
    A second upsert of the restored object simulates the next replication
    hop and must not decay the extras either.
    """
    idx = store.get_index()
    assert not idx.disabled

    obs = _mk_obs('forward-compat payload', project='sub-extras')
    newer = json.loads(obs.to_json())
    newer['visibility'] = 'pub'  # plausible future scalar (ADR-0019)
    newer['routing_hints'] = {'hub': 'tokyo', 'prio': 3}  # nested unknown
    extras_expected = {'visibility': 'pub', 'routing_hints': {'hub': 'tokyo', 'prio': 3}}

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, json.dumps(newer))
        time.sleep(_SETTLE)
    finally:
        remote.close()

    # Public search API routes through LocalIndex.search -> from_json.
    hits = [r for r in store.search_observations(project='sub-extras') if r.observation_id == obs.observation_id]
    assert hits, 'subscriber must upsert the newer-schema obs into the index'
    restored = hits[0]
    assert getattr(restored, '_extras', {}) == extras_expected
    # Known fields are intact alongside the extras.
    assert restored.content == 'forward-compat payload'
    # Re-emission puts the unknown fields back into the wire payload.
    reemitted = json.loads(restored.to_json())
    assert reemitted['visibility'] == 'pub'
    assert reemitted['routing_hints'] == {'hub': 'tokyo', 'prio': 3}

    # Second hop: upsert the restored object again (store-and-forward) and re-search.
    idx.upsert(restored)
    second = [r for r in idx.search(project='sub-extras') if r.observation_id == obs.observation_id]
    assert second and getattr(second[0], '_extras', {}) == extras_expected, '_extras must survive repeated hops'


def test_rebuild_preserves_extras_from_zenoh_storage(single_zenohd: Any) -> None:
    """E2E for Issue #107: the startup rebuild scan must not strip unknown fields.

    Covers the cold-start path: the newer-schema payload already sits in
    Zenoh storage, the local index is reset (fresh spoke / restart), and
    ``rebuild_from_zenoh`` re-populates SQLite from the stored payloads.
    """
    obs = _mk_obs('forward-compat via rebuild', project='rebuild-extras')
    newer = json.loads(obs.to_json())
    newer['visibility'] = 'team'
    newer['team_id'] = 'kioku-mesh'

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, json.dumps(newer))
        time.sleep(_SETTLE)
    finally:
        remote.close()

    # Simulate restart: drop the index so get_index() rebuilds from Zenoh.
    store._reset_index()

    hits = [r for r in store.search_observations(project='rebuild-extras') if r.observation_id == obs.observation_id]
    assert hits, 'rebuild must repopulate the newer-schema obs from zenoh storage'
    assert getattr(hits[0], '_extras', {}) == {'visibility': 'team', 'team_id': 'kioku-mesh'}
    reemitted = json.loads(hits[0].to_json())
    assert reemitted['visibility'] == 'team'
    assert reemitted['team_id'] == 'kioku-mesh'


def test_subscriber_picks_up_remote_tombstone(single_zenohd: Any) -> None:
    """A tombstone published by a remote session marks the index row deleted."""
    idx = store.get_index()
    obs = _mk_obs('will be remote-deleted', project='sub-tomb')
    idx.upsert(obs)

    tomb = Tombstone(observation_id=obs.observation_id)
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.tombstone_key_expr(), tomb.to_json())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert idx.search(project='sub-tomb') == [], 'subscriber must mark row deleted'


def test_subscriber_mirrors_remote_obs_delete_into_index(single_zenohd: Any) -> None:
    """Issue #64: a remote ``session.delete(obs.key_expr)`` must purge the local index row.

    Pre-fix, the subscriber only parsed payloads and silently swallowed
    DELETE-kind samples (empty payload → JSONDecodeError → DEBUG log),
    leaving ghost rows on every peer that did not run the delete itself.
    """
    idx = store.get_index()
    obs = _mk_obs('about to be remote-deleted', project='sub-obs-delete')
    idx.upsert(obs)
    assert obs.observation_id in {r.observation_id for r in idx.search(project='sub-obs-delete')}

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.delete(obs.key_expr)
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert (
        idx.search(project='sub-obs-delete') == []
    ), 'subscriber must physical-delete the index row when a remote peer deletes the obs key'


def test_subscriber_mirrors_remote_tomb_delete_into_index(single_zenohd: Any) -> None:
    """A remote ``session.delete(tomb_key)`` must drop the index row too.

    Mirrors the retention-gc / execute_bulk_purge path that issues a Zenoh
    delete on ``mem/tomb/...`` after the obs has already been purged on
    the originating PC.
    """
    idx = store.get_index()
    obs = _mk_obs('tomb side will be remote-deleted', project='sub-tomb-delete')
    idx.upsert(obs)

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.delete(obs.tombstone_key_expr())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert (
        idx.search(project='sub-tomb-delete', include_deleted=True) == []
    ), 'subscriber must physical-delete the index row when a remote peer deletes the tomb key'


def test_subscriber_ignores_delete_with_invalid_obs_id(
    single_zenohd: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE on a key whose trailing segment is not a 32-hex obs_id is a no-op.

    Guards against a malformed control key accidentally physical-deleting
    an unrelated row whose id happens to fall in the same shard. The
    callback must hit the DEBUG branch and never call ``physical_delete``.
    """
    idx = store.get_index()
    obs = _mk_obs('untouched by malformed delete', project='sub-bad-key')
    idx.upsert(obs)

    physical_delete_calls: list[str] = []
    orig_physical = idx.physical_delete

    def tracked_physical(observation_id: str) -> None:
        physical_delete_calls.append(observation_id)
        orig_physical(observation_id)

    monkeypatch.setattr(idx, 'physical_delete', tracked_physical)

    remote = _remote_session(single_zenohd.endpoint)
    try:
        # Trailing segment is not 32 hex → must be ignored.
        remote.delete('mem/obs/a/b/c/sess/not-a-real-obs-id')
        remote.delete('mem/tomb/a/b/c/sess/short-id')
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert (
        physical_delete_calls == []
    ), f'malformed DELETE keys must not trigger physical_delete; got {physical_delete_calls}'
    # Real row untouched.
    assert obs.observation_id in {r.observation_id for r in idx.search(project='sub-bad-key')}


def test_obs_id_from_key_extracts_only_32_hex() -> None:
    """Unit test for the conservative obs_id extractor used by DELETE handlers."""
    from mesh_mem.store import _obs_id_from_key

    valid = 'a' * 32
    # Canonical 7-segment shape under each accepted prefix.
    assert _obs_id_from_key(f'mem/obs/fam/cli/pc/sess/{valid}') == valid
    assert _obs_id_from_key(f'mem/tomb/fam/cli/pc/sess/{valid}') == valid
    # Mixed-case hex must be rejected (canonical obs_ids are lowercase).
    assert _obs_id_from_key('mem/obs/fam/cli/pc/sess/' + 'A' * 32) is None
    # Wrong obs_id length / non-hex chars / trailing slash → None.
    assert _obs_id_from_key('mem/obs/fam/cli/pc/sess/short') is None
    assert _obs_id_from_key('mem/obs/fam/cli/pc/sess/' + 'g' * 32) is None
    assert _obs_id_from_key('mem/obs/fam/cli/pc/sess/') is None
    # Wrong prefix → None (subscriber should never see these, but the
    # helper must not lean on the declare_subscriber filter for safety).
    assert _obs_id_from_key(f'other/ns/fam/cli/pc/sess/{valid}') is None
    assert _obs_id_from_key(f'mem/control/fam/cli/pc/sess/{valid}') is None
    assert _obs_id_from_key(f'/mem/obs/fam/cli/pc/sess/{valid}') is None
    # Wrong segment count → None (too few or too many slashes).
    assert _obs_id_from_key(f'mem/obs/fam/cli/{valid}') is None
    assert _obs_id_from_key(f'mem/obs/fam/cli/pc/sess/extra/{valid}') is None


def test_subscriber_demotes_non_json_payload_to_debug(
    single_zenohd: Any,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #31: non-JSON payloads must log DEBUG, not WARNING.

    gc broadcast-purge and other control payloads can land on mem/obs/**
    with non-Observation bytes. The subscriber must absorb those without
    emitting WARNING-level noise — DEBUG is the new contract.
    """
    debug_msgs: list[str] = []
    warning_msgs: list[str] = []

    def _debug(msg: str, *args: object) -> None:
        debug_msgs.append(msg % args if args else msg)

    def _warning(msg: str, *args: object) -> None:
        warning_msgs.append(msg % args if args else msg)

    # The subscriber callbacks log via replication.log now (#167).
    monkeypatch.setattr(replication.log, 'debug', _debug)
    monkeypatch.setattr(replication.log, 'warning', _warning)

    # Make sure the subscriber is registered.
    store.get_index()

    remote = _remote_session(single_zenohd.endpoint)
    try:
        # Publish gibberish under both keyspaces the subscriber watches.
        # Keys must be canonical (32-hex leaf) so the samples reach the
        # JSON-parse branch instead of the non-canonical-key gate.
        remote.put('mem/obs/x/y/z/sess/' + 'a' * 32, 'not json at all')
        remote.put('mem/tomb/x/y/z/sess/' + 'b' * 32, '{not json either')
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert any(
        'non-JSON payload' in m for m in debug_msgs
    ), 'expected DEBUG log for non-JSON payload (one of on_obs/on_tomb)'
    assert not warning_msgs, f'non-JSON payloads must NOT log WARNING; got {warning_msgs}'


def test_startup_rebuild_runs_when_index_empty(single_zenohd: Any) -> None:  # noqa: ARG001
    """After index reset, get_index triggers rebuild from zenoh."""
    obs = _mk_obs('pre-existing in zenoh', project='rebuild-start')
    store.put_observation(obs)
    time.sleep(0.25)

    # Simulate restart: clear the index (and subscriber).
    store._reset_index()

    # Next call to get_index should trigger rebuild from zenoh.
    results = store.search_observations(project='rebuild-start')
    ids = {r.observation_id for r in results}
    assert obs.observation_id in ids, 'rebuild must repopulate index from zenoh'


def test_rebuild_shadows_remote_delete_missed_while_subscriber_stopped(single_zenohd: Any) -> None:  # noqa: ARG001
    """If subscriber downtime misses an upstream delete, rebuild must shadow the stale row.

    Models the Issue #67 edge: the local SQLite cache still has a row, the
    upstream obs key was deleted while no subscriber callback was active, and
    the next rebuild must hide the stale row without hard-deleting it.
    """
    idx = store.get_index()
    obs = _mk_obs('stale after missed delete', project='rebuild-shadow-after-miss')
    store.put_observation(obs)
    time.sleep(_SETTLE)

    store._reset_subscribers()
    try:
        remote = _remote_session(single_zenohd.endpoint)
        try:
            remote.delete(obs.key_expr)
            time.sleep(_SETTLE)
        finally:
            remote.close()

        assert obs.observation_id in {r.observation_id for r in idx.search(project='rebuild-shadow-after-miss')}

        stats = idx.rebuild_from_zenoh(store.get_session())
        assert stats.shadowed == 1
        assert idx.search(project='rebuild-shadow-after-miss') == []
        assert idx.find_by_id(obs.observation_id, include_deleted=True) is not None
    finally:
        # Re-arm the subscriber cache so later tests see the normal steady-state wiring.
        store._subscribers = store.start_index_subscriber(store.get_session())  # noqa: SLF001


def test_startup_rebuild_skipped_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """MESH_MEM_SKIP_REBUILD=1 prevents rebuild_from_zenoh from running on init."""
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []
    orig = LocalIndex.rebuild_from_zenoh

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return orig(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('MESH_MEM_SKIP_REBUILD', '1')

    store._reset_index()  # force re-init on next get_index() call
    store.get_index()  # triggers startup logic; session is available via single_zenohd

    assert not rebuild_calls, 'rebuild_from_zenoh must not be called when MESH_MEM_SKIP_REBUILD=1'


# ---------------------------------------------------------------------------
# Issue #38 — rebuild policy: CLI default skip + env override + reset semantics
# ---------------------------------------------------------------------------


def test_set_rebuild_on_init_default_false_skips_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """``set_rebuild_on_init_default(False)`` causes get_index to skip rebuild.

    Mirrors the path the CLI takes: ``mesh-mem ...`` without ``--rebuild``
    flips the module default before the first ``get_index`` so a one-shot
    invocation does not pay the ~15s rebuild on a populated mesh (#38).
    """
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    # Ensure neither env var is set so the module default is the only signal.
    monkeypatch.delenv('MESH_MEM_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('MESH_MEM_FORCE_REBUILD', raising=False)

    # Seed a row directly (bypassing get_index's rebuild path) so the index is
    # NON-empty: the empty-index auto-rebuild override only backfills a fresh
    # spoke, so the default-skip path (#38) is asserted on a populated index.
    seed = LocalIndex.connect()
    seed.upsert(_mk_obs('seed', project='skip-default'))
    seed.close()

    store._reset_index()
    store.set_rebuild_on_init_default(False)
    store.get_index()

    assert not rebuild_calls, 'rebuild must be skipped when default policy is False on a populated index'


def test_empty_index_rebuilds_despite_default_false(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """Empty index backfills via rebuild even when the default policy is False (spoke-onboarding self-heal)."""
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []
    orig = LocalIndex.rebuild_from_zenoh

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return orig(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('MESH_MEM_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('MESH_MEM_FORCE_REBUILD', raising=False)

    store._reset_index()
    store.set_rebuild_on_init_default(False)
    store.get_index()  # empty index -> override forces a one-time rebuild

    assert rebuild_calls, 'empty index must rebuild even when the default policy is False'


def test_force_rebuild_env_overrides_module_default(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """MESH_MEM_FORCE_REBUILD=1 wins over set_rebuild_on_init_default(False).

    Models the ``--rebuild`` (or env-level opt-in) escape hatch on top of
    the CLI's default-False policy.
    """
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('MESH_MEM_SKIP_REBUILD', raising=False)
    monkeypatch.setenv('MESH_MEM_FORCE_REBUILD', '1')

    store._reset_index()
    store.set_rebuild_on_init_default(False)  # CLI default
    store.get_index()

    assert rebuild_calls, 'MESH_MEM_FORCE_REBUILD=1 must force rebuild even when default is False'


def test_skip_rebuild_env_overrides_force_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """MESH_MEM_FORCE_REBUILD=1 wins over MESH_MEM_SKIP_REBUILD=1 when both set.

    Pin the precedence (FORCE > SKIP) so future readers do not have to
    reverse-engineer the resolution order from the implementation.
    """
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('MESH_MEM_SKIP_REBUILD', '1')
    monkeypatch.setenv('MESH_MEM_FORCE_REBUILD', '1')

    store._reset_index()
    store.get_index()

    assert rebuild_calls, 'FORCE must outrank SKIP when both env vars set'


def test_reset_index_restores_rebuild_default() -> None:
    """``_reset_index()`` resets ``_rebuild_on_init_default`` back to True.

    Tests rely on this so a CLI test (which flips the policy False) does
    not leak that policy into a subsequent non-CLI test.
    """
    store.set_rebuild_on_init_default(False)
    assert replication._rebuild_on_init_default is False  # noqa: SLF001
    store._reset_index()
    assert replication._rebuild_on_init_default is True  # noqa: SLF001


def test_cli_main_sets_rebuild_default_false(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
    tmp_path: Any,  # noqa: ARG001
) -> None:
    """Invoking ``mesh-mem save ...`` without ``--rebuild`` skips rebuild_from_zenoh."""
    from mesh_mem.__main__ import main as cli_main
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('MESH_MEM_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('MESH_MEM_FORCE_REBUILD', raising=False)

    # Seed a row so the index is non-empty: the CLI default-skip (#38) applies
    # to a populated index. (An empty index would intentionally rebuild once to
    # backfill a fresh spoke — covered by test_empty_index_rebuilds_despite_default_false.)
    seed = LocalIndex.connect()
    seed.upsert(_mk_obs('seed', project='rebuild-policy'))
    seed.close()

    rc = cli_main(['save', 'cli-rebuild-skip-test', '-p', 'rebuild-policy'])
    assert rc == 0
    assert not rebuild_calls, 'CLI default must skip rebuild_from_zenoh on a populated index'


def test_cli_main_with_rebuild_flag_runs_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """``mesh-mem --rebuild save ...`` opts back into the startup rebuild scan."""
    from mesh_mem.__main__ import main as cli_main
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('MESH_MEM_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('MESH_MEM_FORCE_REBUILD', raising=False)

    rc = cli_main(['--rebuild', 'save', 'cli-rebuild-on-test', '-p', 'rebuild-policy'])
    assert rc == 0
    assert rebuild_calls, '--rebuild must trigger rebuild_from_zenoh on first init'


def test_cli_rebuild_flag_overrides_skip_env(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """``mesh-mem --rebuild`` must win over ambient ``MESH_MEM_SKIP_REBUILD=1``.

    Codex review P2: a shell profile or wrapper script that exports
    ``MESH_MEM_SKIP_REBUILD=1`` previously blocked ``--rebuild`` because
    the env var won the policy resolution. Direct user intent on this
    invocation (the typed flag) must outrank ambient env config.
    """
    from mesh_mem.__main__ import main as cli_main
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('MESH_MEM_SKIP_REBUILD', '1')
    monkeypatch.delenv('MESH_MEM_FORCE_REBUILD', raising=False)

    rc = cli_main(['--rebuild', 'save', 'cli-rebuild-vs-skip', '-p', 'rebuild-policy'])
    assert rc == 0
    assert rebuild_calls, '--rebuild must outrank MESH_MEM_SKIP_REBUILD=1 (codex P2)'


def test_explicit_override_outranks_force_env(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """An explicit ``set_rebuild_on_init_explicit(False)`` outranks ``MESH_MEM_FORCE_REBUILD=1``.

    Pin the highest-priority slot in the policy resolver: when a caller
    deliberately sets the explicit override, env vars must not flip it
    back. Symmetric to the ``--rebuild`` vs SKIP_REBUILD test above.
    """
    from mesh_mem.local_index import LocalIndex
    from mesh_mem.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('MESH_MEM_FORCE_REBUILD', '1')

    store._reset_index()
    store.set_rebuild_on_init_explicit(False)
    store.get_index()

    assert not rebuild_calls, 'explicit override(False) must beat MESH_MEM_FORCE_REBUILD=1'


def test_reset_index_clears_explicit_override() -> None:
    """``_reset_index()`` clears ``_rebuild_explicit_override`` along with the default.

    Tests rely on this so a CLI test that flipped the explicit override
    does not leak that policy into a subsequent test.
    """
    store.set_rebuild_on_init_explicit(True)
    assert replication._rebuild_explicit_override is True  # noqa: SLF001
    store._reset_index()
    assert replication._rebuild_explicit_override is None  # noqa: SLF001


def _tiered_obs_key(prefix: str, obs: Observation) -> str:
    """Build the ADR-0019 tiered key for ``obs`` under ``prefix`` (e.g. ``mem/user/hwata``)."""
    return f'{prefix}/obs/{obs.agent_family}/{obs.client_id}/{obs.pc_id}/{obs.session_id}/{obs.observation_id}'


def test_subscriber_picks_up_tiered_namespace_puts(single_zenohd: Any) -> None:
    """ADR-0019 Phase A: obs PUT under mesh/user/team namespaces land in the index."""
    idx = store.get_index()
    assert not idx.disabled

    cases = [
        ('mem/mesh', _mk_obs('tiered mesh obs', project='sub-tiered')),
        ('mem/user/hwata', _mk_obs('tiered user obs', project='sub-tiered')),
        ('mem/team/kioku-mesh', _mk_obs('tiered team obs', project='sub-tiered')),
    ]
    remote = _remote_session(single_zenohd.endpoint)
    try:
        for prefix, obs in cases:
            remote.put(_tiered_obs_key(prefix, obs), obs.to_json())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    ids = {r.observation_id for r in idx.search(project='sub-tiered')}
    for prefix, obs in cases:
        assert obs.observation_id in ids, f'subscriber must index obs replicated under {prefix}'


def test_subscriber_mirrors_tiered_namespace_delete(single_zenohd: Any) -> None:
    """ADR-0019 Phase A: a DELETE on a tiered key purges the matching index row."""
    idx = store.get_index()
    obs = _mk_obs('tiered delete target', project='sub-tiered-del')
    idx.upsert(obs)
    assert obs.observation_id in {r.observation_id for r in idx.search(project='sub-tiered-del')}

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.delete(_tiered_obs_key('mem/user/hwata', obs))
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert idx.search(project='sub-tiered-del') == [], 'tiered-namespace DELETE must mirror into the index'


def test_rebuild_indexes_tiered_namespace_rows(single_zenohd: Any) -> None:
    """ADR-0019 Phase A: the startup rebuild scan ingests tiered-namespace rows."""
    obs = _mk_obs('tiered rebuild obs', project='rebuild-tiered')
    tombed = _mk_obs('tiered rebuild tombed', project='rebuild-tiered')
    tomb = Tombstone(observation_id=tombed.observation_id)

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(_tiered_obs_key('mem/team/kioku-mesh', obs), obs.to_json())
        remote.put(_tiered_obs_key('mem/team/kioku-mesh', tombed), tombed.to_json())
        remote.put(
            _tiered_obs_key('mem/team/kioku-mesh', tombed).replace('/obs/', '/tomb/', 1),
            tomb.to_json(),
        )
        time.sleep(_SETTLE)
    finally:
        remote.close()

    # Simulate restart: drop the index so get_index() rebuilds from Zenoh.
    store._reset_index()

    hits = {r.observation_id for r in store.search_observations(project='rebuild-tiered')}
    assert obs.observation_id in hits, 'rebuild must ingest tiered-namespace obs'
    assert tombed.observation_id not in hits, 'rebuild must apply tiered-namespace tombstones'


def test_subscriber_rejects_payload_under_non_canonical_key(single_zenohd: Any) -> None:
    """Codex review (PR #177): a valid Observation payload under an off-shape key must not be indexed."""
    idx = store.get_index()
    assert not idx.disabled

    smuggled = _mk_obs('smuggled via control namespace', project='sub-noncanon')
    mismatched = _mk_obs('id mismatch with key leaf', project='sub-noncanon')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        # Off-shape namespaces that still match the broadened mem/**/obs/** selector.
        remote.put(f'mem/control/obs/f/c/p/s/{smuggled.observation_id}', smuggled.to_json())
        remote.put('mem/obs/x/y/z/sess/not-a-hex-id', smuggled.to_json())
        # Canonical shape but the key leaf disagrees with the payload id.
        remote.put('mem/obs/f/c/p/s/' + 'c' * 32, mismatched.to_json())
        time.sleep(_SETTLE)
    finally:
        remote.close()

    assert idx.search(project='sub-noncanon') == [], 'non-canonical or mismatched keys must never reach the index'


def test_rebuild_rejects_payload_under_non_canonical_key(single_zenohd: Any) -> None:
    """Codex review (PR #177): the rebuild scan applies the same canonical-key gate."""
    smuggled = _mk_obs('smuggled for rebuild', project='rebuild-noncanon')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(f'mem/control/obs/f/c/p/s/{smuggled.observation_id}', smuggled.to_json())
        remote.put('mem/obs/f/c/p/s/' + 'd' * 32, smuggled.to_json())  # id mismatch
        time.sleep(_SETTLE)
    finally:
        remote.close()

    store._reset_index()

    assert store.search_observations(project='rebuild-noncanon') == []

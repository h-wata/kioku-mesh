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

from kioku_mesh import replication
from kioku_mesh import store
from kioku_mesh.models import Observation
from kioku_mesh.models import Tombstone

from .wait_helpers import handshake as _handshake_until
from .wait_helpers import POLL_INTERVAL as _POLL_INTERVAL
from .wait_helpers import storage_has
from .wait_helpers import WAIT_TIMEOUT as _WAIT_TIMEOUT
from .wait_helpers import wait_until as _wait_until

# Repeat count for the one-shot CLI test below: the shape it exercises is a
# race, so a single pass would only sample it once. Ten open -> put -> close
# cycles cost well under a second in total on a live path.
_ONE_SHOT_CYCLES = 10


def _indexed_ids(idx: Any, project: str, **kwargs: Any) -> set[str]:
    return {r.observation_id for r in idx.search(project=project, **kwargs)}


def _wait_for_indexed(idx: Any, project: str, observation_id: str, what: str) -> None:
    _wait_until(lambda: observation_id in _indexed_ids(idx, project), what)


def _wait_for_gone(idx: Any, project: str, what: str, **kwargs: Any) -> None:
    _wait_until(lambda: idx.search(project=project, **kwargs) == [], what)


def _barrier(remote: zenoh.Session, idx: Any, label: str) -> None:
    """Publish a sentinel obs on ``remote`` and wait until the subscriber indexes it.

    The negative tests assert that something did *not* reach the index, which
    no amount of waiting can establish on its own. Publishing a sentinel after
    the samples under test and waiting for that sentinel gives a concrete
    point in time by which the earlier samples have been delivered and handled:
    they travel the same session -> router -> subscriber path, in order, ahead
    of the sentinel. That is strictly stronger than the fixed sleep it replaces.

    This relies on Zenoh preserving publish order across *different* key
    expressions — the sentinel lives under its own ``_barrier-{label}``
    project, distinct from whatever key(s) the samples under test use — as
    long as both are put on the same session with the same priority and
    congestion-control settings. Ordering here is a property of the
    session's transport link, not of any single key expression, so it holds
    for every put in this file today; it would silently stop holding if a
    future put on this session used a different priority.
    """
    sentinel = _mk_obs(f'barrier {label}', project=f'_barrier-{label}')
    remote.put(sentinel.key_expr, sentinel.to_json())
    _wait_for_indexed(idx, f'_barrier-{label}', sentinel.observation_id, f'barrier sample for {label}')


def _storage_has(key: str) -> bool:
    """Whether Zenoh storage currently answers a get on ``key``.

    The rebuild tests scan storage rather than relying on the subscriber, so
    they have to wait for the *storage* to hold the sample, which is a
    different condition from "the local index saw it".
    """
    return storage_has(store.get_session(), key)


def _mk_obs(content: str, *, project: str = 'sub-test') -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test-client',
        pc_id='test-pc',
        session_id='test-session',
        visibility='mesh',
    )


def _mk_legacy_obs(content: str, *, project: str = 'sub-test') -> Observation:
    """Create an obs with legacy visibility (for ADR-0019 Phase D gate tests only)."""
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test-client',
        pc_id='test-pc',
        session_id='test-session',
        visibility='',
    )


def _handshake(sess: zenoh.Session, *, via: str) -> None:
    """Publish a canary on ``sess`` until it is observed, proving the path is live.

    ``zenoh.open`` returns before the new session's declarations have been
    exchanged with the router, and a sample published in that window is
    routed against a routing table that does not know every destination yet.
    The sample itself is not lost — the router's storage still holds it, and a
    query or a later index rebuild reads it back. What is lost is the *live
    delivery to subscribers*: the notification is never re-sent, so a
    subscriber that was not routed to at publish time never sees that sample.
    Measured under the conditions that first triggered this fix (this
    author's machine, single-host loopback zenohd, contended load — see PR
    #298): the local index still had not seen such a sample 10s later while
    the same key answered a storage query, so no amount of extra waiting
    recovered it there. Whether that symptom reproduces is machine-dependent:
    an independent cross-review on a quieter, higher-core-count host
    (nproc=16) could not reproduce it at all after disabling this handshake
    (30 runs, 0 failures) — the drop appears to depend on how contended
    declaration propagation is, which this suite has no way to control or
    detect from inside a single run.

    Re-publishing the canary until it is observed is what makes the tests
    deterministic: nothing under test is published until the session ->
    router -> (subscriber | storage) path has actually delivered something.
    Re-publishing is side-effect free — the subscriber upserts by
    observation_id and the storage overwrites by key — and once the path is
    established it stays established for the life of the session. Kept
    unconditionally rather than only on hosts known to need it: the cost is
    negligible (idempotent re-publish, single-digit milliseconds on a live
    path) against the downside of a suite that goes flaky again on whichever
    machine happens to be under load at the time.

    ``via='index'`` proves delivery all the way to the local index subscriber;
    ``via='storage'`` proves only that the router's storage took the sample,
    for the one test that deliberately runs with its subscriber stopped.
    """
    canary = _mk_obs('handshake canary', project='_handshake')
    if via == 'index':
        idx = store.get_index()

        def arrived() -> bool:
            return canary.observation_id in _indexed_ids(idx, '_handshake')
    else:

        def arrived() -> bool:
            return _storage_has(canary.key_expr)

    _handshake_until(
        lambda: sess.put(canary.key_expr, canary.to_json()),
        arrived,
        f'the remote -> {via} path',
    )


def _remote_session(endpoint: str, *, handshake_via: str = 'index') -> zenoh.Session:
    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"client"')
    cfg.insert_json5('connect/endpoints', f'["{endpoint}"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    sess = zenoh.open(cfg)
    _handshake(sess, via=handshake_via)
    return sess


def _warm_peer_subscriber(endpoint: str) -> tuple[zenoh.Session, Any, set[str]]:
    """Declare an independent ``mem/**`` subscriber and prove it is live.

    Stands in for another peer's replication subscriber: it lives on its own
    session, so it survives the store-session resets the one-shot CLI test
    performs. Returns the session, the subscriber handle and the mutable set
    of key expressions it has observed so far.
    """
    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"client"')
    cfg.insert_json5('connect/endpoints', f'["{endpoint}"]')
    cfg.insert_json5('scouting/multicast/enabled', 'false')
    peer = zenoh.open(cfg)
    seen: set[str] = set()
    sub = peer.declare_subscriber('mem/**', lambda sample: seen.add(str(sample.key_expr)))

    # The peer's own declaration has to reach the router before it can be
    # routed to, so publish a canary from an already-established session until
    # the peer observes it — same reasoning as _handshake, one hop further.
    warm = _remote_session(endpoint, handshake_via='storage')
    try:
        canary = _mk_obs('peer canary', project='_peer-canary')
        deadline = time.monotonic() + _WAIT_TIMEOUT
        while canary.key_expr not in seen:
            warm.put(canary.key_expr, canary.to_json())
            attempt_end = min(time.monotonic() + 0.1, deadline)
            while canary.key_expr not in seen and time.monotonic() < attempt_end:
                time.sleep(_POLL_INTERVAL)
            if canary.key_expr not in seen and time.monotonic() >= deadline:
                raise AssertionError(f'timed out after {_WAIT_TIMEOUT:.1f}s establishing the peer subscriber')
    finally:
        warm.close()
    return peer, sub, seen


def test_one_shot_cli_put_reaches_peer_subscriber_and_storage(single_zenohd: Any) -> None:
    """The one-shot CLI shape (lazy-open -> put -> close) still reaches a peer.

    ``kioku-mesh save`` is a one-shot process: ``store.put_observation`` opens
    the Zenoh session on demand and ``main``'s ``finally`` closes it right
    after, so production does publish from a just-opened session — the same
    shape the tests in this file used to hit. This pins what that path
    guarantees for an *already established* peer: the sample reaches both the
    peer's live subscriber and the router's storage. It is the production
    counterpart of ``_handshake``'s reasoning, which is about a peer whose own
    declaration has not propagated yet.
    """
    peer, sub, seen = _warm_peer_subscriber(single_zenohd.endpoint)
    try:
        for i in range(_ONE_SHOT_CYCLES):
            obs = _mk_obs(f'one-shot cli put {i}', project='sub-oneshot')
            # A fresh process: no cached session, no cached index.
            store._reset_index()
            store._reset_session()
            store.put_observation(obs)
            # What __main__.main does in its finally clause.
            store._reset_index()
            store._reset_session()

            _wait_until(lambda: obs.key_expr in seen, f'peer subscriber to receive one-shot put {i}')
            _wait_until(
                lambda: any(r.ok for r in peer.get(obs.key_expr, timeout=2.0)),
                f'router storage to hold one-shot put {i}',
            )
    finally:
        sub.undeclare()
        peer.close()


def test_subscriber_picks_up_remote_put_into_index(single_zenohd: Any) -> None:
    """A put from a remote session lands in the local index via the subscriber."""
    idx = store.get_index()
    assert not idx.disabled

    obs = _mk_obs('replicated content', project='sub-obs')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, obs.to_json())
        _wait_for_indexed(idx, 'sub-obs', obs.observation_id, 'subscriber to upsert replicated obs into index')
    finally:
        remote.close()


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
    # 'visibility' graduated to a known field in Phase B — use fields that
    # are still unknown to this schema as the forward-compat probes.
    newer['priority_hint'] = 'high'  # plausible future scalar
    newer['routing_hints'] = {'hub': 'tokyo', 'prio': 3}  # nested unknown
    extras_expected = {'priority_hint': 'high', 'routing_hints': {'hub': 'tokyo', 'prio': 3}}

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, json.dumps(newer))
        # Public search API routes through LocalIndex.search -> from_json.
        hits = _wait_until(
            lambda: [
                r for r in store.search_observations(project='sub-extras') if r.observation_id == obs.observation_id
            ],
            'subscriber to upsert the newer-schema obs into the index',
        )
    finally:
        remote.close()

    restored = hits[0]
    assert getattr(restored, '_extras', {}) == extras_expected
    # Known fields are intact alongside the extras.
    assert restored.content == 'forward-compat payload'
    # Re-emission puts the unknown fields back into the wire payload.
    reemitted = json.loads(restored.to_json())
    assert reemitted['priority_hint'] == 'high'
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
    # Unknown-to-this-schema fields ('visibility' is known since Phase B).
    newer['org_id'] = 'kioku-mesh'
    newer['retention_class'] = 'gold'

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, json.dumps(newer))
        _wait_until(lambda: _storage_has(obs.key_expr), 'zenoh storage to hold the newer-schema obs')
    finally:
        remote.close()

    # Simulate restart: drop the index so get_index() rebuilds from Zenoh.
    store._reset_index()

    hits = [r for r in store.search_observations(project='rebuild-extras') if r.observation_id == obs.observation_id]
    assert hits, 'rebuild must repopulate the newer-schema obs from zenoh storage'
    assert getattr(hits[0], '_extras', {}) == {'org_id': 'kioku-mesh', 'retention_class': 'gold'}
    reemitted = json.loads(hits[0].to_json())
    assert reemitted['org_id'] == 'kioku-mesh'
    assert reemitted['retention_class'] == 'gold'


def test_subscriber_picks_up_remote_tombstone(single_zenohd: Any) -> None:
    """A tombstone published by a remote session marks the index row deleted."""
    idx = store.get_index()
    obs = _mk_obs('will be remote-deleted', project='sub-tomb')
    idx.upsert(obs)

    tomb = Tombstone(observation_id=obs.observation_id)
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.tombstone_key_expr(), tomb.to_json())
        _wait_for_gone(idx, 'sub-tomb', 'subscriber to mark the row deleted')
    finally:
        remote.close()


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
        _wait_for_gone(
            idx,
            'sub-obs-delete',
            'subscriber to physical-delete the index row after a remote peer deletes the obs key',
        )
    finally:
        remote.close()


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
        _wait_for_gone(
            idx,
            'sub-tomb-delete',
            'subscriber to physical-delete the index row after a remote peer deletes the tomb key',
            include_deleted=True,
        )
    finally:
        remote.close()


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
        _barrier(remote, idx, 'invalid-delete')
    finally:
        remote.close()

    assert (
        physical_delete_calls == []
    ), f'malformed DELETE keys must not trigger physical_delete; got {physical_delete_calls}'
    # Real row untouched.
    assert obs.observation_id in {r.observation_id for r in idx.search(project='sub-bad-key')}


def test_obs_id_from_key_extracts_only_32_hex() -> None:
    """Unit test for the conservative obs_id extractor used by DELETE handlers."""
    from kioku_mesh.store import _obs_id_from_key

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
        # Keys must be canonical (32-hex leaf) and non-legacy so they reach
        # the JSON-parse branch instead of the legacy-gate or non-canonical-key gate.
        remote.put('mem/mesh/obs/x/y/z/sess/' + 'a' * 32, 'not json at all')
        remote.put('mem/mesh/tomb/x/y/z/sess/' + 'b' * 32, '{not json either')
        # Wait for *both* callbacks to have handled their sample: that is the
        # point at which "and no WARNING was emitted" is a real assertion
        # rather than a statement about how fast the box happened to be.
        _wait_until(
            lambda: sum('non-JSON payload' in m for m in debug_msgs) >= 2,
            'DEBUG logs for the non-JSON payloads from both on_obs and on_tomb',
        )
    finally:
        remote.close()

    assert not warning_msgs, f'non-JSON payloads must NOT log WARNING; got {warning_msgs}'


def test_startup_rebuild_runs_when_index_empty(single_zenohd: Any) -> None:  # noqa: ARG001
    """After index reset, get_index triggers rebuild from zenoh."""
    obs = _mk_obs('pre-existing in zenoh', project='rebuild-start')
    store.put_observation(obs)
    _wait_until(lambda: _storage_has(obs.key_expr), 'zenoh storage to hold the pre-existing obs')

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
    _wait_until(lambda: _storage_has(obs.key_expr), 'zenoh storage to hold the obs before the missed delete')

    store._reset_subscribers()
    try:
        # The subscriber is deliberately stopped here, so the handshake can
        # only prove the path as far as the router's storage.
        remote = _remote_session(single_zenohd.endpoint, handshake_via='storage')
        try:
            remote.delete(obs.key_expr)
            # The rebuild scan reads storage, so the delete has to have landed
            # there — the subscriber is deliberately stopped and cannot tell us.
            _wait_until(lambda: not _storage_has(obs.key_expr), 'zenoh storage to drop the deleted obs key')
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
    """KIOKU_MESH_SKIP_REBUILD=1 prevents rebuild_from_zenoh from running on init."""
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []
    orig = LocalIndex.rebuild_from_zenoh

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return orig(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('KIOKU_MESH_SKIP_REBUILD', '1')

    store._reset_index()  # force re-init on next get_index() call
    store.get_index()  # triggers startup logic; session is available via single_zenohd

    assert not rebuild_calls, 'rebuild_from_zenoh must not be called when KIOKU_MESH_SKIP_REBUILD=1'


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
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    # Ensure neither env var is set so the module default is the only signal.
    monkeypatch.delenv('KIOKU_MESH_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('KIOKU_MESH_FORCE_REBUILD', raising=False)

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
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []
    orig = LocalIndex.rebuild_from_zenoh

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return orig(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('KIOKU_MESH_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('KIOKU_MESH_FORCE_REBUILD', raising=False)

    store._reset_index()
    store.set_rebuild_on_init_default(False)
    store.get_index()  # empty index -> override forces a one-time rebuild

    assert rebuild_calls, 'empty index must rebuild even when the default policy is False'


def test_force_rebuild_env_overrides_module_default(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """KIOKU_MESH_FORCE_REBUILD=1 wins over set_rebuild_on_init_default(False).

    Models the ``--rebuild`` (or env-level opt-in) escape hatch on top of
    the CLI's default-False policy.
    """
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('KIOKU_MESH_SKIP_REBUILD', raising=False)
    monkeypatch.setenv('KIOKU_MESH_FORCE_REBUILD', '1')

    store._reset_index()
    store.set_rebuild_on_init_default(False)  # CLI default
    store.get_index()

    assert rebuild_calls, 'KIOKU_MESH_FORCE_REBUILD=1 must force rebuild even when default is False'


def test_skip_rebuild_env_overrides_force_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """KIOKU_MESH_FORCE_REBUILD=1 wins over KIOKU_MESH_SKIP_REBUILD=1 when both set.

    Pin the precedence (FORCE > SKIP) so future readers do not have to
    reverse-engineer the resolution order from the implementation.
    """
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('KIOKU_MESH_SKIP_REBUILD', '1')
    monkeypatch.setenv('KIOKU_MESH_FORCE_REBUILD', '1')

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
    from kioku_mesh.__main__ import main as cli_main
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('KIOKU_MESH_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('KIOKU_MESH_FORCE_REBUILD', raising=False)

    # Seed a row so the index is non-empty: the CLI default-skip (#38) applies
    # to a populated index. (An empty index would intentionally rebuild once to
    # backfill a fresh spoke — covered by test_empty_index_rebuilds_despite_default_false.)
    seed = LocalIndex.connect()
    seed.upsert(_mk_obs('seed', project='rebuild-policy'))
    seed.close()

    rc = cli_main(
        ['save', 'cli-rebuild-skip-test', '-p', 'rebuild-policy', '--subject', 'rebuild skip', '--summary', 'skip']
    )
    assert rc == 0
    assert not rebuild_calls, 'CLI default must skip rebuild_from_zenoh on a populated index'


def test_cli_main_with_rebuild_flag_runs_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """``mesh-mem --rebuild save ...`` opts back into the startup rebuild scan."""
    from kioku_mesh.__main__ import main as cli_main
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.delenv('KIOKU_MESH_SKIP_REBUILD', raising=False)
    monkeypatch.delenv('KIOKU_MESH_FORCE_REBUILD', raising=False)

    rc = cli_main(
        [
            '--rebuild',
            'save',
            'cli-rebuild-on-test',
            '-p',
            'rebuild-policy',
            '--subject',
            'rebuild on',
            '--summary',
            'rebuild flag runs rebuild',
        ]
    )
    assert rc == 0
    assert rebuild_calls, '--rebuild must trigger rebuild_from_zenoh on first init'


def test_cli_rebuild_flag_overrides_skip_env(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """``mesh-mem --rebuild`` must win over ambient ``KIOKU_MESH_SKIP_REBUILD=1``.

    Codex review P2: a shell profile or wrapper script that exports
    ``KIOKU_MESH_SKIP_REBUILD=1`` previously blocked ``--rebuild`` because
    the env var won the policy resolution. Direct user intent on this
    invocation (the typed flag) must outrank ambient env config.
    """
    from kioku_mesh.__main__ import main as cli_main
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('KIOKU_MESH_SKIP_REBUILD', '1')
    monkeypatch.delenv('KIOKU_MESH_FORCE_REBUILD', raising=False)

    rc = cli_main(
        [
            '--rebuild',
            'save',
            'cli-rebuild-vs-skip',
            '-p',
            'rebuild-policy',
            '--subject',
            'flag vs skip',
            '--summary',
            'flag beats skip env',
        ]
    )
    assert rc == 0
    assert rebuild_calls, '--rebuild must outrank KIOKU_MESH_SKIP_REBUILD=1 (codex P2)'


def test_explicit_override_outranks_force_env(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,  # noqa: ARG001
) -> None:
    """An explicit ``set_rebuild_on_init_explicit(False)`` outranks ``KIOKU_MESH_FORCE_REBUILD=1``.

    Pin the highest-priority slot in the policy resolver: when a caller
    deliberately sets the explicit override, env vars must not flip it
    back. Symmetric to the ``--rebuild`` vs SKIP_REBUILD test above.
    """
    from kioku_mesh.local_index import LocalIndex
    from kioku_mesh.local_index import RebuildStats

    rebuild_calls: list[bool] = []

    def tracking_rebuild(self: LocalIndex, session: object) -> RebuildStats:
        rebuild_calls.append(True)
        return RebuildStats()

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', tracking_rebuild)
    monkeypatch.setenv('KIOKU_MESH_FORCE_REBUILD', '1')

    store._reset_index()
    store.set_rebuild_on_init_explicit(False)
    store.get_index()

    assert not rebuild_calls, 'explicit override(False) must beat KIOKU_MESH_FORCE_REBUILD=1'


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
        for prefix, obs in cases:
            _wait_for_indexed(idx, 'sub-tiered', obs.observation_id, f'obs replicated under {prefix} to be indexed')
    finally:
        remote.close()


def test_subscriber_mirrors_tiered_namespace_delete(single_zenohd: Any) -> None:
    """ADR-0019 Phase A: a DELETE on a tiered key purges the matching index row."""
    idx = store.get_index()
    obs = _mk_obs('tiered delete target', project='sub-tiered-del')
    idx.upsert(obs)
    assert obs.observation_id in {r.observation_id for r in idx.search(project='sub-tiered-del')}

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.delete(_tiered_obs_key('mem/user/hwata', obs))
        _wait_for_gone(idx, 'sub-tiered-del', 'tiered-namespace DELETE to mirror into the index')
    finally:
        remote.close()


def test_rebuild_indexes_tiered_namespace_rows(single_zenohd: Any) -> None:
    """ADR-0019 Phase A: the startup rebuild scan ingests tiered-namespace rows."""
    obs = _mk_obs('tiered rebuild obs', project='rebuild-tiered')
    tombed = _mk_obs('tiered rebuild tombed', project='rebuild-tiered')
    tomb = Tombstone(observation_id=tombed.observation_id)

    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(_tiered_obs_key('mem/team/kioku-mesh', obs), obs.to_json())
        remote.put(_tiered_obs_key('mem/team/kioku-mesh', tombed), tombed.to_json())
        tomb_key = _tiered_obs_key('mem/team/kioku-mesh', tombed).replace('/obs/', '/tomb/', 1)
        remote.put(tomb_key, tomb.to_json())
        # The rebuild scan reads storage, so every sample has to be stored first.
        for key in (
            _tiered_obs_key('mem/team/kioku-mesh', obs),
            _tiered_obs_key('mem/team/kioku-mesh', tombed),
            tomb_key,
        ):
            _wait_until(lambda k=key: _storage_has(k), f'zenoh storage to hold {key}')
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
        _barrier(remote, idx, 'noncanonical-put')
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
        # The rebuild scan reads storage, so wait for the samples to be stored.
        for key in (f'mem/control/obs/f/c/p/s/{smuggled.observation_id}', 'mem/obs/f/c/p/s/' + 'd' * 32):
            _wait_until(lambda k=key: _storage_has(k), f'zenoh storage to hold {key}')
    finally:
        remote.close()

    store._reset_index()

    assert store.search_observations(project='rebuild-noncanon') == []


# ---------------------------------------------------------------------------
# ADR-0029 PR 3: legacy read is always skipped (KIOKU_MESH_LEGACY_READ_FALLBACK
# escape hatch removed in v1.0; the env var is now inert under any value).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('legacy_read_fallback_env', [None, 'on'])
def test_subscriber_always_skips_legacy_put(
    single_zenohd: Any,
    monkeypatch: pytest.MonkeyPatch,
    legacy_read_fallback_env: str | None,
) -> None:
    """Subscriber must never index a legacy-key obs, regardless of the (now-inert) env var."""
    if legacy_read_fallback_env is None:
        monkeypatch.delenv('KIOKU_MESH_LEGACY_READ_FALLBACK', raising=False)
    else:
        monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', legacy_read_fallback_env)
    idx = store.get_index()
    assert not idx.disabled

    obs = _mk_legacy_obs('should-be-skipped', project='sub-gate-off')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, obs.to_json())  # key_expr is legacy mem/obs/...
        _barrier(remote, idx, 'legacy-put')
    finally:
        remote.close()

    ids = {r.observation_id for r in idx.search(project='sub-gate-off')}
    assert obs.observation_id not in ids, 'subscriber must never index legacy obs (v1.0 removed the read fallback)'


@pytest.mark.parametrize('legacy_read_fallback_env', [None, 'on'])
def test_rebuild_always_skips_legacy_obs(
    single_zenohd: Any,
    monkeypatch: pytest.MonkeyPatch,
    legacy_read_fallback_env: str | None,
) -> None:
    """rebuild_from_zenoh must never ingest legacy-key obs, regardless of the (now-inert) env var."""
    if legacy_read_fallback_env is None:
        monkeypatch.delenv('KIOKU_MESH_LEGACY_READ_FALLBACK', raising=False)
    else:
        monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', legacy_read_fallback_env)
    obs = _mk_legacy_obs('rebuild-skip-legacy', project='rebuild-gate-off')
    remote = _remote_session(single_zenohd.endpoint)
    try:
        remote.put(obs.key_expr, obs.to_json())  # legacy key
        # The rebuild scan reads storage, so wait for the sample to be stored.
        _wait_until(lambda: _storage_has(obs.key_expr), 'zenoh storage to hold the legacy-key obs')
    finally:
        remote.close()

    store._reset_index()

    hits = store.search_observations(project='rebuild-gate-off')
    assert not hits, 'rebuild must never ingest legacy obs (v1.0 removed the read fallback)'


# ---------------------------------------------------------------------------
# Issue #323 — subscriber lifetime is bound to the zenoh session
# ---------------------------------------------------------------------------


def test_subscriber_rebinds_after_session_reset(single_zenohd: Any) -> None:
    """A put published *after* a session reset still reaches the local index.

    ``with_retry`` drops the session on any retryable transport error
    (``transport._reset_session``), which kills the subscribers declared on it.
    Pre-fix they were never re-declared — ``put``/``search`` kept working on the
    new session while the index went permanently deaf, with no error and no log.

    This exercises the real path end to end: real router, real session reset,
    real remote publish, real SQLite index. Asserting that a hook fired would
    not have caught the bug, because the hole was in the wiring, not the call.
    """
    idx = store.get_index()
    assert not idx.disabled

    # What a single retryable put failure does to the transport.
    store._reset_session()
    assert store._subscribers is None, 'subscribers declared on the closed session must be dropped'
    # The next operation reopens the session; the subscribers must come back with it.
    store.get_session()
    assert store._subscribers, 'a reopened session must carry re-declared index subscribers'

    obs = _mk_obs('published after session reset', project='sub-rebind')
    remote = _remote_session(single_zenohd.endpoint, handshake_via='storage')
    try:
        _handshake_until(
            lambda: remote.put(obs.key_expr, obs.to_json()),
            lambda: obs.observation_id in _indexed_ids(idx, 'sub-rebind'),
            'the index subscriber re-bound to the post-reset session',
        )
    finally:
        remote.close()


def test_subscriber_is_declared_before_the_startup_rebuild(
    single_zenohd: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sample published *during* the startup rebuild scan is not lost (R2).

    The scan takes up to 60s in production (obs + tomb, ``timeout=30.0`` each).
    While it ran, the subscriber did not exist yet and the scan's snapshot was
    already taken, so anything published in that window landed in neither path.
    The publish here happens inside a stand-in for the scan, so the assertion
    does not depend on the real scan being slow enough to race.
    """
    from kioku_mesh.local_index import LocalIndex

    obs = _mk_obs('published during the rebuild scan', project='sub-during-rebuild')
    remote = _remote_session(single_zenohd.endpoint, handshake_via='storage')
    orig_rebuild = LocalIndex.rebuild_from_zenoh
    indexed_during_scan: list[bool] = []

    def publishing_rebuild(self: LocalIndex, session: object) -> Any:
        # Stands where the real scan's ~60s window is: publish, then wait for
        # the subscriber to mirror it while the "scan" is still running.
        _handshake_until(
            lambda: remote.put(obs.key_expr, obs.to_json()),
            lambda: obs.observation_id in {r.observation_id for r in self.search(project='sub-during-rebuild')},
            'the subscriber to be live during the rebuild scan',
        )
        indexed_during_scan.append(True)
        return orig_rebuild(self, session)

    monkeypatch.setattr(LocalIndex, 'rebuild_from_zenoh', publishing_rebuild)
    monkeypatch.setenv('KIOKU_MESH_FORCE_REBUILD', '1')
    try:
        store._reset_index()  # simulate a fresh process: next get_index() rebuilds
        idx = store.get_index()
        assert indexed_during_scan, 'the rebuild scan must have run'
        assert obs.observation_id in _indexed_ids(idx, 'sub-during-rebuild')
    finally:
        remote.close()


def test_doctor_reports_index_subscriber_bound_to_current_session(single_zenohd: Any) -> None:  # noqa: ARG001
    """``doctor``'s index_subscriber check PASSes on a live session and WARNs after a reset."""
    from kioku_mesh import doctor

    store.get_index()
    bound = doctor.check_index_subscriber()
    assert bound.status is doctor.CheckStatus.PASS, bound.summary
    assert bound.details['declared'] > 0

    store._reset_session()
    # No get_session() here: this is the muted state the check exists to surface.
    muted = doctor.check_index_subscriber()
    assert muted.status is doctor.CheckStatus.WARN
    assert muted.details['bound_to_current_session'] is False

"""Reply.err / retry error-path tests using a mock zenoh session.

Covers the three scenarios Codex flagged as required for the
QueryErrorReply / _iter_ok_replies / with_retry path:

1. ok, ok, err      -> QueryErrorReply raised, search_observations fails
                       (does NOT silently return an empty / partial list)
2. err -> retry ok  -> one retryable failure is re-attempted and succeeds
3. err -> retry err -> final RuntimeError with __cause__ == QueryErrorReply

Phase 3 routes ``search_observations`` through the SQLite local index by
default; the zenoh retry path that these tests exercise is only entered
when ``MESH_MEM_DISABLE_INDEX=1``. Tests below force that env var so the
retry semantics under test are actually reachable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mesh_mem import store
from mesh_mem.models import Observation
from mesh_mem.store import QueryErrorReply


def _ok_reply(obs: Observation) -> SimpleNamespace:
    """Build a fake ``ok`` reply holding a serialized observation."""
    ok = SimpleNamespace(
        key_expr=SimpleNamespace(as_str=lambda o=obs: f'mem/obs/fake/k/p/s/{o.observation_id}'),
        payload=SimpleNamespace(to_string=lambda o=obs: o.to_json()),
    )
    return SimpleNamespace(ok=ok, err=None)


def _err_reply(message: str = 'fake error') -> SimpleNamespace:
    err = SimpleNamespace(payload=SimpleNamespace(to_string=lambda: message))
    return SimpleNamespace(ok=None, err=err)


class _FakeSession:
    """Return a canned sequence of replies per call to ``get``."""

    def __init__(self, reply_batches: list[list[Any]]) -> None:
        self._batches = list(reply_batches)
        self.calls: list[str] = []

    def get(self, key_expr: str, timeout: float = 0.0) -> list[Any]:  # noqa: ARG002
        self.calls.append(key_expr)
        if not self._batches:
            return []
        return self._batches.pop(0)

    def close(self) -> None:
        pass


def _install_fake_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    """Redirect ``_open_session`` so ``get_session`` / ``_reset_session`` cycle uses the fake.

    Also forces ``MESH_MEM_DISABLE_INDEX=1`` so ``search_observations`` falls
    back to the legacy zenoh path being exercised by these tests, and resets
    the cached LocalIndex so the env var takes effect on the next call.
    """
    monkeypatch.setenv('MESH_MEM_DISABLE_INDEX', '1')
    store._reset_index()
    monkeypatch.setattr(store, '_open_session', lambda: session)
    store._reset_session()


def test_search_does_not_silently_return_partial_on_err(monkeypatch: pytest.MonkeyPatch) -> None:
    """ok, ok, err from attempt 1 must NOT bleed partial data into the retry's clean result."""
    obs1 = Observation(content='a')  # first attempt, before err
    obs2 = Observation(content='b')  # first attempt, before err
    obs3 = Observation(content='c')  # second attempt (after retry)
    fake = _FakeSession(
        [
            [],  # attempt 1: tombstones empty
            [_ok_reply(obs1), _ok_reply(obs2), _err_reply('boom')],  # attempt 1: obs with trailing err
            [],  # attempt 2: tombstones empty
            [_ok_reply(obs3)],  # attempt 2: single clean ok
        ]
    )
    _install_fake_session(monkeypatch, fake)

    results = store.search_observations()
    ids = {o.observation_id for o in results}
    # 1件目・2件目は err でロールバックされ、retry 後の結果にだけ観測されるべき。
    assert obs1.observation_id not in ids
    assert obs2.observation_id not in ids
    assert obs3.observation_id in ids


def test_search_recovers_when_err_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """First attempt errors on tombstone query; retry succeeds with clean replies."""
    obs = Observation(content='hi')
    # Attempt 1: tombstone fetch errors out.
    # Attempt 2 (after retry): tombstone empty, observations has one ok.
    fake = _FakeSession(
        [
            [_err_reply('transient')],
            [],
            [_ok_reply(obs)],
        ]
    )
    _install_fake_session(monkeypatch, fake)

    results = store.search_observations()
    assert len(results) == 1
    assert results[0].observation_id == obs.observation_id


def test_search_final_failure_preserves_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated err replies must end as RuntimeError with QueryErrorReply as __cause__."""
    # Both attempts: tombstone query errors.
    fake = _FakeSession([[_err_reply('always broken')], [_err_reply('still broken')]])
    _install_fake_session(monkeypatch, fake)

    with pytest.raises(RuntimeError) as ei:
        store.search_observations()
    message = str(ei.value)
    assert 'failed after retry' in message
    assert 'QueryErrorReply' in message
    assert 'still broken' in message
    assert ei.value.__cause__ is not None
    assert isinstance(ei.value.__cause__, QueryErrorReply)


def test_find_by_id_via_zenoh_uses_leaf_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #40 stage 1: fallback lookup must query by leaf observation_id."""
    obs = Observation(content='needle')
    fake = _FakeSession([[_ok_reply(obs)]])
    _install_fake_session(monkeypatch, fake)

    hit = store.find_observation_by_id(obs.observation_id)

    assert hit is not None
    assert hit.observation_id == obs.observation_id
    assert fake.calls == [f'mem/obs/**/{obs.observation_id}']


def test_find_by_id_via_zenoh_rejects_invalid_observation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid ids must not be interpolated into a Zenoh key expression."""
    fake = _FakeSession([])
    _install_fake_session(monkeypatch, fake)

    hit = store.find_observation_by_id('../not-a-hex-id')

    assert hit is None
    assert fake.calls == []


def test_put_failure_updates_transport_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed put must surface as disconnected transport health (#48)."""

    class _BrokenSession:
        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            raise ConnectionError('router down')

        def close(self) -> None:
            pass

    dummy_index = SimpleNamespace(
        upsert=lambda obs: None,
        mark_deleted=lambda observation_id, deleted_at: None,
    )
    monkeypatch.setattr(store, '_open_session', lambda: _BrokenSession())
    monkeypatch.setattr(store, 'get_index', lambda: dummy_index)
    store._reset_session()
    store._reset_index()

    with pytest.raises(RuntimeError) as ei:
        store.put_observation(Observation(content='fails'))

    assert 'ConnectionError' in str(ei.value)
    status = store.get_transport_status()
    assert status.zenoh_session == 'disconnected'
    assert status.last_put_status == 'error: ConnectionError'
    assert status.last_put_at_iso.endswith('Z')
    assert status.recent_put_ok == 0
    assert status.recent_put_error == 1
    assert status.recent_put_window == 1
    assert status.pending_puts == 1


def test_failed_put_is_queued_and_replayed_on_next_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #50: failed puts survive transport loss and drain on later success."""

    class _BrokenSession:
        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            raise ConnectionError('router down')

        def close(self) -> None:
            pass

    class _WorkingSession:
        def __init__(self) -> None:
            self.put_calls: list[str] = []

        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            self.put_calls.append(key_expr)

        def close(self) -> None:
            pass

    dummy_index = SimpleNamespace(
        upsert=lambda obs: None,
        mark_deleted=lambda observation_id, deleted_at: None,
    )
    monkeypatch.setattr(store, 'get_index', lambda: dummy_index)
    monkeypatch.setattr(store, '_open_session', lambda: _BrokenSession())
    store._reset_session()
    store._reset_index()

    queued = Observation(content='queued')
    with pytest.raises(RuntimeError):
        store.put_observation(queued)
    assert store.get_transport_status().pending_puts == 1

    working = _WorkingSession()
    monkeypatch.setattr(store, '_open_session', lambda: working)
    store._reset_session()

    fresh = Observation(content='fresh')
    store.put_observation(fresh)

    assert working.put_calls == [fresh.key_expr, queued.key_expr]
    status = store.get_transport_status()
    assert status.pending_puts == 0
    assert status.recent_put_ok == 2
    assert status.recent_put_error == 1


def test_pending_drain_is_capped_per_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drain replays only a bounded number of queued rows per successful put."""

    class _WorkingSession:
        def __init__(self) -> None:
            self.put_calls: list[str] = []

        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            self.put_calls.append(key_expr)

        def close(self) -> None:
            pass

    ticks = {'n': 0}

    def _fake_now() -> str:
        ticks['n'] += 1
        return f'2026-05-17T00:00:{ticks["n"]:02d}.000000Z'

    monkeypatch.setattr(store, '_PENDING_DRAIN_BATCH', 2)
    monkeypatch.setattr(store, '_now_iso_utc', _fake_now)
    dummy_index = SimpleNamespace(
        upsert=lambda obs: None,
        mark_deleted=lambda observation_id, deleted_at: None,
    )
    monkeypatch.setattr(store, 'get_index', lambda: dummy_index)
    working = _WorkingSession()
    monkeypatch.setattr(store, '_open_session', lambda: working)
    store._reset_session()
    store._reset_index()

    queued = [Observation(content=f'queued-{i}') for i in range(3)]
    for obs in queued:
        store._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())

    fresh = Observation(content='fresh-cap')
    store.put_observation(fresh)

    assert working.put_calls == [fresh.key_expr, queued[0].key_expr, queued[1].key_expr]
    assert store.get_transport_status().pending_puts == 1


def test_pending_drain_retryable_failure_keeps_remaining_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drain transport failure stops replay and leaves queued rows intact."""

    class _FlakyDrainSession:
        def __init__(self, fail_key: str) -> None:
            self.fail_key = fail_key
            self.put_calls: list[str] = []

        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            self.put_calls.append(key_expr)
            if key_expr == self.fail_key:
                raise ConnectionError('drain flap')

        def close(self) -> None:
            pass

    ticks = {'n': 0}

    def _fake_now() -> str:
        ticks['n'] += 1
        return f'2026-05-17T00:10:{ticks["n"]:02d}.000000Z'

    monkeypatch.setattr(store, '_now_iso_utc', _fake_now)
    dummy_index = SimpleNamespace(
        upsert=lambda obs: None,
        mark_deleted=lambda observation_id, deleted_at: None,
    )
    monkeypatch.setattr(store, 'get_index', lambda: dummy_index)
    queued = [Observation(content=f'queued-{i}') for i in range(2)]
    for obs in queued:
        store._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())

    flaky = _FlakyDrainSession(queued[0].key_expr)
    monkeypatch.setattr(store, '_open_session', lambda: flaky)
    store._reset_session()
    store._reset_index()

    fresh = Observation(content='fresh-retryable')
    store.put_observation(fresh)

    assert flaky.put_calls == [fresh.key_expr, queued[0].key_expr]
    status = store.get_transport_status()
    assert status.pending_puts == 2
    assert status.recent_put_ok == 1
    assert status.recent_put_error == 1


def test_malformed_pending_row_is_dropped_before_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed queued payloads must be discarded without publishing garbage."""

    class _WorkingSession:
        def __init__(self) -> None:
            self.put_calls: list[str] = []

        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            self.put_calls.append(key_expr)

        def close(self) -> None:
            pass

    dummy_index = SimpleNamespace(
        upsert=lambda obs: None,
        mark_deleted=lambda observation_id, deleted_at: None,
    )
    monkeypatch.setattr(store, 'get_index', lambda: dummy_index)
    bad_id = 'a' * 32
    store._enqueue_pending_put('observation', f'mem/obs/x/y/z/s/{bad_id}', bad_id, '{not-json')

    working = _WorkingSession()
    monkeypatch.setattr(store, '_open_session', lambda: working)
    store._reset_session()
    store._reset_index()

    fresh = Observation(content='fresh-malformed')
    store.put_observation(fresh)

    assert working.put_calls == [fresh.key_expr]
    assert store.get_transport_status().pending_puts == 0


def test_pending_puts_limit_trims_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue size is capped by dropping the oldest queued rows."""
    ticks = {'n': 0}

    def _fake_now() -> str:
        ticks['n'] += 1
        return f'2026-05-17T00:20:{ticks["n"]:02d}.000000Z'

    monkeypatch.setattr(store, '_PENDING_PUTS_LIMIT', 3)
    monkeypatch.setattr(store, '_now_iso_utc', _fake_now)
    store._reset_session()
    store._reset_index()

    queued = [Observation(content=f'queued-{i}') for i in range(5)]
    for obs in queued:
        store._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())

    assert store.get_transport_status().pending_puts == 3
    conn = store._open_pending_puts_db()
    try:
        keys = [
            row[0] for row in conn.execute('SELECT key_expr FROM pending_puts ORDER BY queued_at ASC, key_expr ASC')
        ]
    finally:
        conn.close()
    assert keys == [queued[2].key_expr, queued[3].key_expr, queued[4].key_expr]


def test_search_via_zenoh_deduplicates_by_observation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate obs replies (multi-router replication overlap) collapse to one result (#12)."""
    obs = Observation(content='deduplicated', project='dedup-test')
    # Simulate two Zenoh storages both replying with the same observation.
    fake = _FakeSession(
        [
            [],  # tombstones empty
            [_ok_reply(obs), _ok_reply(obs)],  # same obs from two storages
        ]
    )
    _install_fake_session(monkeypatch, fake)

    results = store.search_observations(project='dedup-test')
    assert len(results) == 1
    assert results[0].observation_id == obs.observation_id


def test_search_via_zenoh_filter_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both project and keyword filters drop non-matching items in the FINAL result set (#8).

    Final-state contract only: an obs matching project but failing keyword
    must be absent, and an obs matching keyword but failing project must
    also be absent. Evaluation order is NOT pinned here — the final result
    set is identical whether project or keyword is checked first.
    Order is locked at the internal-state level by
    ``test_search_via_zenoh_project_filter_short_circuits_before_keyword``.
    """
    obs_match_project = Observation(content='no-keyword-here', project='keep')
    obs_wrong_project = Observation(content='the-keyword', project='drop')
    fake = _FakeSession(
        [
            [],  # tombstones empty
            [_ok_reply(obs_match_project), _ok_reply(obs_wrong_project)],
        ]
    )
    _install_fake_session(monkeypatch, fake)

    results = store.search_observations(query='the-keyword', project='keep')
    ids = {o.observation_id for o in results}
    # Correct project, wrong keyword — dropped by keyword filter.
    assert obs_match_project.observation_id not in ids
    # Correct keyword, wrong project — dropped by project filter.
    assert obs_wrong_project.observation_id not in ids


def test_search_via_zenoh_project_filter_short_circuits_before_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project filter MUST short-circuit before the keyword filter runs (#13).

    The pre-existing ``test_search_via_zenoh_filter_order`` test only checks
    the FINAL result set, which is identical regardless of evaluation order
    (both filters reject the same items either way). To pin the actual
    order at the internal-state level we spy on ``obs.content.lower()``
    — the heavy call invoked by the keyword filter — and assert:

      * the project-mismatch obs is NEVER subjected to that call,
        proving project is checked first and the obs never reaches the
        ``results_by_id`` registration step.
      * the project-match obs DOES reach the keyword filter (sanity that
        the spy is wired correctly).

    Equivalently: an item that matches keyword but fails project never
    incurs keyword-evaluation cost.
    """
    real_from_json = Observation.from_json
    lower_calls: dict[str, int] = {}

    class _TrackedStr(str):
        """``str`` subclass logging ``.lower()`` invocations keyed by observation_id.

        Attached only to ``Observation.content`` after JSON parsing so we
        can detect whether the keyword filter (``obs.content.lower()``)
        ran on a given obs without having to refactor production code.
        """

        def __new__(cls, value: str, key: str) -> '_TrackedStr':
            inst = super().__new__(cls, value)
            inst._key = key  # noqa: SLF001 — test-local marker on a str subclass
            return inst

        def lower(self) -> str:
            lower_calls[self._key] = lower_calls.get(self._key, 0) + 1  # noqa: SLF001
            return super().lower()

    def spy_from_json(data: str) -> Observation:
        obs = real_from_json(data)
        obs.content = _TrackedStr(obs.content, obs.observation_id)
        return obs

    monkeypatch.setattr(Observation, 'from_json', spy_from_json)

    obs_match = Observation(content='the-keyword stays', project='keep')
    obs_mismatch = Observation(content='the-keyword leaves', project='drop')
    fake = _FakeSession(
        [
            [],  # tombstones empty
            [_ok_reply(obs_match), _ok_reply(obs_mismatch)],
        ]
    )
    _install_fake_session(monkeypatch, fake)

    store.search_observations(query='the-keyword', project='keep')

    # If filter order is flipped (keyword before project), content.lower()
    # is invoked on every parsed obs — including project-mismatches — and
    # this assertion fails. With the documented order
    # (tombstone -> project -> since -> keyword) the project-mismatch obs
    # short-circuits before content.lower() is ever called.
    assert lower_calls.get(obs_mismatch.observation_id, 0) == 0, (
        f'project-mismatch obs reached the keyword filter — order regressed '
        f'(content.lower() called {lower_calls.get(obs_mismatch.observation_id, 0)} times '
        f'on a project-mismatch row that should have been skipped earlier)'
    )
    # Sanity check that the spy itself is wired correctly: the project-match
    # obs DOES go through the keyword filter at least once.
    assert (
        lower_calls.get(obs_match.observation_id, 0) >= 1
    ), 'project-match obs never reached the keyword filter — spy is not wired correctly'


def test_search_via_zenoh_filters_skip_non_matching_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observations not matching project are excluded from results_by_id, not just from output.

    Verify that a non-matching project obs is completely absent from results
    even when no keyword filter is applied — i.e. project filter acts as an
    early-exit guard before dict registration.
    """
    obs_keep = Observation(content='stays', project='target')
    obs_skip = Observation(content='filtered-out', project='other')
    fake = _FakeSession(
        [
            [],  # tombstones empty
            [_ok_reply(obs_keep), _ok_reply(obs_skip)],
        ]
    )
    _install_fake_session(monkeypatch, fake)

    results = store.search_observations(project='target')
    ids = {o.observation_id for o in results}
    assert obs_keep.observation_id in ids
    assert obs_skip.observation_id not in ids

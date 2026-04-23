"""Reply.err / retry error-path tests using a mock zenoh session.

Covers the three scenarios Codex flagged as required for the
QueryErrorReply / _iter_ok_replies / with_retry path:

1. ok, ok, err      -> QueryErrorReply raised, search_observations fails
                       (does NOT silently return an empty / partial list)
2. err -> retry ok  -> one retryable failure is re-attempted and succeeds
3. err -> retry err -> final RuntimeError with __cause__ == QueryErrorReply
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
    """Redirect ``_open_session`` so ``get_session`` / ``_reset_session`` cycle uses the fake."""
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
    assert ei.value.__cause__ is not None
    assert isinstance(ei.value.__cause__, QueryErrorReply)

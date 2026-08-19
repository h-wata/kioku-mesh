"""Realignment worker lifecycle and cadence (ADR-0035, TASK-450 Phase 2).

The cadence tests drive :func:`_run_worker` with a fake clock and a fake
``wait`` so a 15-minute / 6-hour schedule is exercised in microseconds. The
lifecycle tests instead run the *real* thread and assert it starts and stops,
because "the thread is alive" is exactly what a mock-only test cannot show.
"""

import threading
import time
from typing import Any

import pytest

from kioku_mesh.memory import realignment
from kioku_mesh.memory import store


class _FakeSchedule:
    """A monotonic clock advanced only by ``wait``; stops after N ticks."""

    def __init__(self, ticks: int) -> None:
        self.t = 0.0
        self.remaining = ticks

    def now(self) -> float:
        return self.t

    def wait(self, delay: float) -> bool:
        if self.remaining <= 0:
            return True  # stop requested
        self.remaining -= 1
        self.t += delay
        return False


def _run(ticks: int, *, full_repair_due: bool = False, fail_full: int = 0) -> list[tuple[float, str]]:
    """Run the loop for ``ticks`` wakeups; return (elapsed_sec, mode) per repair."""
    sched = _FakeSchedule(ticks)
    calls: list[tuple[float, str]] = []
    failures = {'left': fail_full}

    def repair(mode: str) -> bool:
        calls.append((sched.t, mode))
        if mode == realignment.REPAIR_MODE_FULL and failures['left'] > 0:
            failures['left'] -= 1
            return False
        return True

    realignment._run_worker(
        threading.Event(),
        full_repair_due=full_repair_due,
        repair=repair,
        now=sched.now,
        wait=sched.wait,
    )
    return calls


def test_cadence_is_tomb_every_15min_and_full_every_6h() -> None:
    calls = _run(25)

    tomb = realignment.TOMB_INTERVAL_SEC
    # 24 tombstone slots per 6h; the 24th wakeup is the full repair instead.
    assert [t for t, mode in calls if mode == realignment.REPAIR_MODE_FULL] == [
        realignment.FULL_INTERVAL_SEC,
    ]
    assert [t for t, mode in calls if mode == realignment.REPAIR_MODE_TOMB] == [tomb * i for i in range(1, 24)] + [
        realignment.FULL_INTERVAL_SEC + tomb
    ]


def test_first_full_repair_waits_6h_when_startup_rebuild_succeeded() -> None:
    calls = _run(3, full_repair_due=False)

    assert [mode for _t, mode in calls] == [realignment.REPAIR_MODE_TOMB] * 3


def test_failed_startup_rebuild_runs_full_at_the_next_tomb_tick() -> None:
    calls = _run(2, full_repair_due=True)

    assert calls[0] == (realignment.TOMB_INTERVAL_SEC, realignment.REPAIR_MODE_FULL)
    # Success returns to the normal cadence: the next tick is tombstone-only.
    assert calls[1][1] == realignment.REPAIR_MODE_TOMB


def test_failing_full_repair_retries_every_15min_until_it_succeeds() -> None:
    calls = _run(4, full_repair_due=True, fail_full=2)

    tomb = realignment.TOMB_INTERVAL_SEC
    assert calls[:3] == [(tomb * i, realignment.REPAIR_MODE_FULL) for i in (1, 2, 3)]
    # Third full repair succeeded -> back to tombstone cadence.
    assert calls[3] == (tomb * 4, realignment.REPAIR_MODE_TOMB)


def test_stop_event_ends_the_loop_without_repairing() -> None:
    stop = threading.Event()
    stop.set()
    calls: list[str] = []

    realignment._run_worker(stop, repair=lambda mode: calls.append(mode) or True)

    assert calls == []


# -- real thread lifecycle -----------------------------------------------------


@pytest.fixture
def owned() -> Any:
    """Grant realignment ownership for the test and always release it."""
    realignment.enable_realignment()
    yield
    realignment.disable_realignment()


def test_worker_thread_starts_and_stops(owned: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    entered = threading.Event()

    def _slow_wait(_delay: float) -> bool:
        entered.set()
        return realignment._stop_event.wait(5.0)

    monkeypatch.setattr(realignment, '_repair_once', lambda mode: True)
    # The real loop's first act is a wait; make it observable and interruptible.
    real_run = realignment._run_worker
    monkeypatch.setattr(
        realignment,
        '_run_worker',
        lambda stop, **kw: real_run(stop, **{**kw, 'wait': _slow_wait}),
    )

    assert realignment.start_realignment_worker() is True
    assert entered.wait(5.0)
    assert realignment.realignment_status()['running'] is True

    assert realignment.stop_realignment_worker(join_timeout=5.0) is True
    assert realignment.realignment_status()['running'] is False


def test_single_worker_guard_refuses_a_second_thread(owned: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    started: list[threading.Thread] = []
    real_thread = threading.Thread

    class _Recording(real_thread):  # type: ignore[misc, valid-type]
        def start(self) -> None:
            started.append(self)
            super().start()

    monkeypatch.setattr(realignment.threading, 'Thread', _Recording)
    monkeypatch.setattr(realignment, '_repair_once', lambda mode: True)
    real_run = realignment._run_worker
    monkeypatch.setattr(
        realignment,
        '_run_worker',
        lambda stop, **kw: real_run(stop, **{**kw, 'wait': lambda _d: stop.wait(5.0)}),
    )

    assert realignment.start_realignment_worker() is True
    assert realignment.start_realignment_worker() is False
    assert len(started) == 1
    assert realignment.stop_realignment_worker(join_timeout=5.0) is True


def test_worker_does_not_start_without_ownership() -> None:
    assert realignment.realignment_status()['enabled'] is False
    assert realignment.start_realignment_worker() is False
    assert realignment.realignment_status()['running'] is False


def test_repair_once_reports_failure_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def repair_from_zenoh(self, session: object, *, mode: str, should_stop: object = None) -> None:
            raise TimeoutError('scan timed out')

    monkeypatch.setattr(store, 'get_session', lambda: object())
    monkeypatch.setattr(store, 'get_index', lambda: _Boom())

    assert realignment._repair_once(realignment.REPAIR_MODE_TOMB) is False


def test_repair_once_uses_repair_not_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0035: the worker must never call the shadowing rebuild path."""
    calls: list[str] = []

    class _Idx:
        def repair_from_zenoh(self, session: object, *, mode: str, should_stop: object = None) -> str:
            calls.append(f'repair:{mode}')
            return 'stats'

        def rebuild_from_zenoh(self, session: object) -> None:
            raise AssertionError('the realignment worker must not rebuild')

    monkeypatch.setattr(store, 'get_session', lambda: object())
    monkeypatch.setattr(store, 'get_index', lambda: _Idx())

    assert realignment._repair_once(realignment.REPAIR_MODE_FULL) is True
    assert calls == ['repair:full']


# -- ownership: who may run a worker at all ------------------------------------


def test_get_index_starts_the_worker_only_after_the_index_is_open(
    owned: Any,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership alone starts nothing; opening the index is what starts it."""
    monkeypatch.setattr(realignment, '_run_worker', lambda stop, **kw: stop.wait(5.0))
    assert realignment.realignment_status()['running'] is False

    store.get_index()

    assert realignment.realignment_status()['running'] is True
    assert realignment.stop_realignment_worker(join_timeout=5.0) is True


def test_no_worker_without_ownership_even_when_the_index_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI opens indexes all day and must never grow a repair worker."""
    monkeypatch.setattr(realignment, '_run_worker', lambda stop, **kw: stop.wait(5.0))

    store.get_index()

    assert realignment.realignment_status() == {'enabled': False, 'running': False}


def test_no_worker_when_the_index_is_disabled(owned: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setenv('KIOKU_MESH_DISABLE_INDEX', '1')
    monkeypatch.setattr(realignment, '_run_worker', lambda stop, **kw: stop.wait(5.0))

    assert store.get_index().disabled is True
    assert realignment.realignment_status()['running'] is False


def test_cli_run_leaves_realignment_off(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Regression: a one-shot CLI invocation must not own or start a worker."""
    from kioku_mesh.__main__ import main as cli_main

    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    from kioku_mesh.backend import reset_backend

    reset_backend()

    assert cli_main(['status']) == 0
    capsys.readouterr()
    # ...and opening the index afterwards (what a zenoh-backend CLI command
    # does on every run) still must not produce a worker.
    store.get_index()

    assert realignment.realignment_status() == {'enabled': False, 'running': False}


# -- shutdown with an in-flight repair (PR #328 B1) ----------------------------


class _BlockingSession:
    """A zenoh-shaped session whose scan keeps streaming replies until released.

    This is the shape of a real scan that is still collecting from peers when
    shutdown arrives: without a cancellation check at the reply boundary the
    scan runs until ``release``, and shutdown has to abandon the thread.
    """

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.replies_yielded = 0

    def get(self, key_expr: str, **kwargs: Any) -> Any:  # noqa: ARG002
        def _stream() -> Any:
            self.entered.set()
            while not self.release.wait(0.005):
                self.replies_yielded += 1
                yield _NoOkReply()

        return _stream()


class _SkippedOk:
    """An ok reply on a non-canonical key: the repair scan skips it."""

    key_expr = 'mem/mesh/not-a-canonical-key'
    payload = ''


class _NoOkReply:
    """A reply carrying the ok above (the scan sees one reply, applies nothing)."""

    ok = _SkippedOk()
    err = None


def _start_worker_in_repair(monkeypatch: pytest.MonkeyPatch, idx: Any) -> _BlockingSession:
    """Start a real worker that is already inside a repair scan."""
    session = _BlockingSession()
    monkeypatch.setattr(store, 'get_session', lambda: session)
    monkeypatch.setattr(store, 'get_index', lambda: idx)
    real_run = realignment._run_worker
    # Skip the 15-minute wait: go straight into the first repair.
    monkeypatch.setattr(
        realignment,
        '_run_worker',
        lambda stop, **kw: real_run(stop, **{**kw, 'wait': lambda _d: stop.is_set()}),
    )
    assert realignment.start_realignment_worker() is True
    assert session.entered.wait(5.0)
    return session


def test_shutdown_stops_a_worker_that_is_inside_a_repair(
    owned: Any,  # noqa: ARG001
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: the thread must be gone when shutdown returns, not left as a daemon."""
    from kioku_mesh.memory.local_index import LocalIndex

    idx = LocalIndex.connect(str(tmp_path / 'shutdown.db'))
    try:
        session = _start_worker_in_repair(monkeypatch, idx)
        started = time.monotonic()

        stopped = realignment.disable_realignment()
        elapsed = time.monotonic() - started

        assert stopped is True
        assert realignment.realignment_status()['running'] is False
        # Shutdown must not have waited out the blocked scan.
        assert elapsed < 5.0
        assert session.release.is_set() is False

        state = idx.alignment_state()
        assert state.last_tomb_repair_completed_at == ''
        assert state.last_full_repair_completed_at == ''
        # A cancellation is not a mesh failure, so it is not recorded as one.
        assert state.last_failure_at == ''
    finally:
        idx.close()


def test_cancelled_scan_raises_instead_of_returning_partial_results() -> None:
    """A cancelled scan must not look like a completed one to its caller."""
    from kioku_mesh.core.transport import collect_ok_replies_over
    from kioku_mesh.core.transport import ScanCancelled

    session = _BlockingSession()
    session.release.set()

    with pytest.raises(ScanCancelled):
        collect_ok_replies_over(session, ['mem/obs/**'], timeout=0.1, should_stop=lambda: True)


def test_repair_cancellation_applies_nothing_and_advances_no_timestamp(tmp_path: Any) -> None:
    from kioku_mesh.core.transport import ScanCancelled
    from kioku_mesh.memory.local_index import LocalIndex

    idx = LocalIndex.connect(str(tmp_path / 'cancel.db'))
    try:
        session = _BlockingSession()
        session.release.set()

        with pytest.raises(ScanCancelled):
            idx.repair_from_zenoh(session, mode=realignment.REPAIR_MODE_TOMB, should_stop=lambda: True)

        state = idx.alignment_state()
        assert state.last_tomb_repair_completed_at == ''
        assert state.last_failure_at == ''
    finally:
        idx.close()


class _SilentSession(_BlockingSession):
    """A scan that accepted the query and never answers — the residual case.

    Cancellation is cooperative, so a scan with no reply boundary to check on
    cannot be interrupted; shutdown must report that instead of hanging.
    """

    def get(self, key_expr: str, **kwargs: Any) -> Any:  # noqa: ARG002
        def _stream() -> Any:
            self.entered.set()
            self.release.wait(10.0)
            return
            yield  # pragma: no cover — makes this a generator

        return _stream()


def test_shutdown_reports_failure_instead_of_waiting_out_a_silent_scan(
    owned: Any,  # noqa: ARG001
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kioku_mesh.memory.local_index import LocalIndex

    idx = LocalIndex.connect(str(tmp_path / 'silent.db'))
    session = _SilentSession()
    try:
        monkeypatch.setattr(store, 'get_session', lambda: session)
        monkeypatch.setattr(store, 'get_index', lambda: idx)
        real_run = realignment._run_worker
        monkeypatch.setattr(
            realignment,
            '_run_worker',
            lambda stop, **kw: real_run(stop, **{**kw, 'wait': lambda _d: stop.is_set()}),
        )
        assert realignment.start_realignment_worker() is True
        assert session.entered.wait(5.0)

        started = time.monotonic()
        stopped = realignment.stop_realignment_worker(join_timeout=0.2)
        elapsed = time.monotonic() - started

        assert stopped is False
        assert elapsed < 2.0
    finally:
        session.release.set()
        realignment.stop_realignment_worker(join_timeout=5.0)
        idx.close()


# -- B2: a selector that ends with zero replies -------------------------------


class _SilentTombSession:
    """obs scan answers normally; the tomb selector accepts and never answers.

    The shape that produced PR #328 B2: the per-reply cancellation check never
    runs for a selector with no replies, so a stop arriving while the last
    selector waits must still be caught after that selector finishes.
    """

    def __init__(self, obs: Any) -> None:
        self._obs = obs
        self.entered_tomb = threading.Event()
        self.release = threading.Event()

    def get(self, key_expr: str, **kwargs: Any) -> Any:  # noqa: ARG002
        if '/tomb/' in key_expr:

            def _silent() -> Any:
                self.entered_tomb.set()
                self.release.wait(10.0)
                return
                yield  # pragma: no cover — makes this a generator

            return _silent()
        return [_ObsReply(self._obs)]


class _ObsReply:
    def __init__(self, obs: Any) -> None:
        self.ok = _ObsOk(obs)
        self.err = None


class _ObsOk:
    def __init__(self, obs: Any) -> None:
        self.key_expr = obs.key_expr
        self.payload = _ObsPayload(obs.to_json())


class _ObsPayload:
    def __init__(self, text: str) -> None:
        self._text = text

    def to_string(self) -> str:
        return self._text


def _mk_remote_obs() -> Any:
    from kioku_mesh.core.models import Observation

    return Observation(
        content='remote observation',
        project='b2',
        agent_family='claude',
        client_id='test',
        pc_id='testpc',
        session_id='testsession',
        visibility='mesh',
    )


def test_stop_during_a_silent_final_selector_applies_nothing(tmp_path: Any) -> None:
    """B2: obs scan done, final tomb selector silent, stop requested."""
    from kioku_mesh.core.transport import ScanCancelled
    from kioku_mesh.memory.local_index import LocalIndex

    obs = _mk_remote_obs()
    session = _SilentTombSession(obs)
    idx = LocalIndex.connect(str(tmp_path / 'b2.db'))
    stop = threading.Event()
    result: dict[str, Any] = {}

    def _repair() -> None:
        try:
            result['stats'] = idx.repair_from_zenoh(
                session, mode=realignment.REPAIR_MODE_FULL, should_stop=stop.is_set
            )
        except BaseException as e:  # noqa: BLE001 — the test asserts on what came out
            result['exc'] = e

    worker = threading.Thread(target=_repair, daemon=True)
    try:
        worker.start()
        assert session.entered_tomb.wait(5.0)
        stop.set()
        session.release.set()
        worker.join(timeout=5.0)

        assert not worker.is_alive()
        assert 'stats' not in result, f'a cancelled scan must not report success: {result.get("stats")}'
        assert isinstance(result.get('exc'), ScanCancelled)
        assert idx.find_by_id(obs.observation_id, include_deleted=True) is None
        state = idx.alignment_state()
        assert state.last_full_repair_completed_at == ''
        assert state.last_failure_at == ''
    finally:
        session.release.set()
        worker.join(timeout=5.0)
        idx.close()


def test_scan_cancelled_after_a_selector_finishes_with_zero_replies() -> None:
    """The unit-level shape of B2, independent of repair."""
    from kioku_mesh.core.transport import collect_ok_replies_over
    from kioku_mesh.core.transport import ScanCancelled

    stop = threading.Event()

    class _StopOnSecondSelector:
        def get(self, key_expr: str, **kwargs: Any) -> Any:  # noqa: ARG002
            if '/tomb/' in key_expr:
                stop.set()  # a stop arrives while this empty selector runs
                return []
            return []

    with pytest.raises(ScanCancelled):
        collect_ok_replies_over(
            _StopOnSecondSelector(),
            ['mem/**/obs/**', 'mem/**/tomb/**'],
            timeout=0.1,
            should_stop=stop.is_set,
        )

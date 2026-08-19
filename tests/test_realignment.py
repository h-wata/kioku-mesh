"""Realignment worker lifecycle and cadence (ADR-0035, TASK-450 Phase 2).

The cadence tests drive :func:`_run_worker` with a fake clock and a fake
``wait`` so a 15-minute / 6-hour schedule is exercised in microseconds. The
lifecycle tests instead run the *real* thread and assert it starts and stops,
because "the thread is alive" is exactly what a mock-only test cannot show.
"""

import threading
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
        def repair_from_zenoh(self, session: object, *, mode: str) -> None:
            raise TimeoutError('scan timed out')

    monkeypatch.setattr(store, 'get_session', lambda: object())
    monkeypatch.setattr(store, 'get_index', lambda: _Boom())

    assert realignment._repair_once(realignment.REPAIR_MODE_TOMB) is False


def test_repair_once_uses_repair_not_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0035: the worker must never call the shadowing rebuild path."""
    calls: list[str] = []

    class _Idx:
        def repair_from_zenoh(self, session: object, *, mode: str) -> str:
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

"""Deterministic waits for tests that go through a live Zenoh router.

Zenoh delivery is asynchronous in two distinct ways, and the two need
different treatment:

* **Steady state.** A ``put`` on an established session reaches the router
  storage / the local index subscriber in single-digit milliseconds, but not
  synchronously. :func:`wait_until` polls the condition the test actually
  cares about instead of sleeping for a duration picked to be "usually
  enough".
* **Session startup.** ``zenoh.open`` returns *before* the new session's
  declarations have been exchanged with the router. A sample published in
  that window is routed against a routing table that does not know the
  destination yet and is dropped outright — it is not queued, so waiting
  longer never recovers it (measured on this suite: still missing 10s
  later). :func:`handshake` closes that window by re-publishing a canary
  until it is observed.

For assertions of the "X must *not* arrive" kind, no amount of waiting is
evidence on its own; use :func:`barrier` to obtain a concrete point in time
by which the earlier samples must have been handled.

These helpers were extracted from ``test_replication_subscriber.py``
(TASK-295 / PR #298) so the rest of the suite can reuse them.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any, TypeVar

_T = TypeVar('_T')

# Upper bound for the waits below, not an expected duration: every wait
# returns as soon as its condition holds.
WAIT_TIMEOUT = 10.0
POLL_INTERVAL = 0.01


def wait_until(predicate: Callable[[], _T], description: str, *, timeout: float = WAIT_TIMEOUT) -> _T:
    """Poll ``predicate`` until it returns a truthy value, or fail with ``description``."""
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f'timed out after {timeout:.1f}s waiting for {description}')
        time.sleep(POLL_INTERVAL)


def storage_has(session: Any, key: str) -> bool:
    """Whether the router storage currently answers a get on ``key``."""
    return any(r.ok for r in session.get(key, timeout=2.0))


def storage_missing(session: Any, key_expr: str) -> bool:
    """Whether a get on ``key_expr`` (may be a wildcard) yields no sample at all."""
    return not any(r.ok for r in session.get(key_expr, timeout=2.0))


def handshake(publish: Callable[[], None], arrived: Callable[[], bool], what: str) -> None:
    """Re-publish via ``publish`` until ``arrived`` observes it, proving the path is live.

    Call this once per freshly opened session, before publishing anything the
    test depends on. Re-publishing must be side-effect free for the caller
    (the storage overwrites by key and the index subscriber upserts by
    observation id, so a canary re-put is); once the path is established it
    stays established for the life of the session.
    """
    deadline = time.monotonic() + WAIT_TIMEOUT
    while True:
        publish()
        # Give this attempt a short window before re-publishing: on a live
        # path the canary lands in single-digit milliseconds.
        attempt_end = min(time.monotonic() + 0.1, deadline)
        while time.monotonic() < attempt_end:
            if arrived():
                return
            time.sleep(POLL_INTERVAL)
        if arrived():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f'timed out after {WAIT_TIMEOUT:.1f}s establishing {what}')

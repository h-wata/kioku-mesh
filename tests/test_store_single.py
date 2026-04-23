"""Integration tests against a single local zenohd router.

Skipped by default (the ``single_zenohd`` fixture is a placeholder until
the subprocess launcher lands). Happy-path save/search/delete coverage
and tombstone exclusion live here once the fixture is wired up.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason='single_zenohd fixture not implemented yet')
def test_save_then_search_roundtrip() -> None:
    pass


@pytest.mark.skip(reason='single_zenohd fixture not implemented yet')
def test_tombstone_hides_observation() -> None:
    pass


@pytest.mark.skip(reason='single_zenohd fixture not implemented yet')
def test_reconnect_after_session_reset() -> None:
    pass

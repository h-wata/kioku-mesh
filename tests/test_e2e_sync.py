"""End-to-end tests across two linked zenohd routers.

Acceptance criteria for PoC Step 4/5:

* offline diff sync: a put made while peer-B is down is visible on peer-B
  within ``replication.interval * 2 + propagation_delay`` after peer-B restarts.
* tombstone propagation: a delete issued during a split is observed to hide
  the target observation on both sides after link recovery.

Skipped until the ``dual_zenohd`` fixture lands.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason='dual_zenohd fixture not implemented yet')
def test_offline_diff_sync() -> None:
    pass


@pytest.mark.skip(reason='dual_zenohd fixture not implemented yet')
def test_tombstone_propagates_across_split_brain() -> None:
    pass

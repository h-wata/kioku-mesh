"""pytest fixtures for mesh-mem tests.

Layered fixtures:
    - ``isolated_state_dir``: redirects ``MESH_MEM_STATE_DIR`` at tmp path and
      resets identity caches. Always active.
    - ``single_zenohd`` (scope=session): launches one zenohd router on a random
      port so integration tests share a single transport.
    - ``dual_zenohd`` (scope=session): launches two linked zenohd routers for
      E2E sync tests (offline diff / tombstone propagation).

The zenohd fixtures are SKIPped if the ``zenohd`` binary is not on PATH so
the unit-only suite stays runnable without the native daemon installed.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from mesh_mem import identity
from mesh_mem import store


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect MESH_MEM_STATE_DIR per test and reset identity caches."""
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(tmp_path))
    identity.reset_caches()
    # store._session は他テストの残骸が残りうるので明示クリア
    store._reset_session()
    yield tmp_path
    identity.reset_caches()
    store._reset_session()


def _zenohd_available() -> bool:
    return shutil.which('zenohd') is not None


@pytest.fixture(scope='session')
def single_zenohd() -> None:
    """Hold a single-router session fixture slot until the subprocess launcher lands."""
    if not _zenohd_available():
        pytest.skip('zenohd binary not found on PATH')
    pytest.skip('single_zenohd fixture not yet implemented')


@pytest.fixture(scope='session')
def dual_zenohd() -> None:
    """Hold a dual-router session fixture slot for future E2E sync tests."""
    if not _zenohd_available():
        pytest.skip('zenohd binary not found on PATH')
    pytest.skip('dual_zenohd fixture not yet implemented')

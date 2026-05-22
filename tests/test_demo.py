"""Tests for the ``mesh-mem demo`` subcommand."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from mesh_mem.backend import reset_backend

# Ordered list of phrases that must appear in demo output — mirrors Issue #108 mockup.
_EXPECTED_PHRASES = [
    'Setting up local memory (no zenohd needed)',
    'Saving 3 example observations:',
    '[decision]',
    'Chose Postgres over SQLite for the analytics service',
    '[bug]',
    'Session refresh race — fixed by adding mutex around token swap',
    '[pattern]',
    'Tests mirror src/ layout, prefix with test_',
    'Try this now:',
    'mesh-mem search "race"',
    'mesh-mem search "Postgres"',
    'Then close this terminal',
    'mesh-mem mcp install',
]


def _assert_output_order(out: str, phrases: list[str]) -> None:
    """Assert every phrase is present and appears in the given order."""
    positions = []
    for phrase in phrases:
        pos = out.find(phrase)
        assert pos >= 0, f'{phrase!r} not found in output'
        positions.append(pos)
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1], f'{phrases[i]!r} should appear before {phrases[i + 1]!r}'


def test_demo_first_run_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    reset_backend()

    from mesh_mem.__main__ import main as cli_main

    rc = cli_main(['demo'])
    assert rc == 0

    out = capsys.readouterr().out
    _assert_output_order(out, _EXPECTED_PHRASES)


def test_demo_idempotent_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    reset_backend()

    from mesh_mem.__main__ import main as cli_main

    assert cli_main(['demo']) == 0
    reset_backend()
    assert cli_main(['demo']) == 0

    # --help must document that re-running adds duplicates
    help_result = subprocess.run(
        [sys.executable, '-m', 'mesh_mem', 'demo', '--help'],
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert 'duplicate' in help_result.stdout.lower() or 'Re-running' in help_result.stdout


def test_demo_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / 'state'
    xdg_dir = tmp_path / 'xdg'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg_dir))
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(state_dir))
    reset_backend()

    from mesh_mem.__main__ import main as cli_main

    assert cli_main(['demo']) == 0
    reset_backend()

    env = os.environ.copy()
    env['XDG_CONFIG_HOME'] = str(xdg_dir)
    env['MESH_MEM_STATE_DIR'] = str(state_dir)
    result = subprocess.run(
        [sys.executable, '-m', 'mesh_mem', 'search', 'Postgres'],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert 'Postgres' in result.stdout


def test_demo_ignores_mesh_mem_backend_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MESH_MEM_BACKEND=zenoh でも demo は local backend を強制使用する (B1 regression)."""
    state_dir = tmp_path / 'state'
    xdg_dir = tmp_path / 'xdg'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg_dir))
    monkeypatch.setenv('MESH_MEM_STATE_DIR', str(state_dir))
    monkeypatch.setenv('MESH_MEM_BACKEND', 'zenoh')
    reset_backend()

    from mesh_mem.__main__ import main as cli_main

    assert cli_main(['demo']) == 0
    reset_backend()

    env = os.environ.copy()
    env['XDG_CONFIG_HOME'] = str(xdg_dir)
    env['MESH_MEM_STATE_DIR'] = str(state_dir)
    env['MESH_MEM_BACKEND'] = 'local'

    search_result = subprocess.run(
        [sys.executable, '-m', 'mesh_mem', 'search', 'Postgres'],
        env=env,
        capture_output=True,
        text=True,
    )
    assert search_result.returncode == 0
    assert (
        'Postgres' in search_result.stdout
    ), 'Seeded observations not found in local index — demo may have used zenoh backend'

    status_result = subprocess.run(
        [sys.executable, '-m', 'mesh_mem', 'status'],
        env=env,
        capture_output=True,
        text=True,
    )
    assert 'backend: local' in status_result.stdout

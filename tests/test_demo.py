"""Tests for the ``mesh-mem demo`` subcommand."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from mesh_mem.backend import reset_backend


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
    assert 'Postgres' in out
    assert 'race' in out
    assert 'test_' in out
    assert 'mesh-mem search' in out
    assert 'mesh-mem mcp install' in out


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

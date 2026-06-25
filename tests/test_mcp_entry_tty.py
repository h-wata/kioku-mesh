"""Tests for TTY misinvocation detection in kioku-mesh-mcp entry point.

Verifies that ``kioku-mesh-mcp`` exits with code 2 when stdin is a TTY,
passes through when stdin is redirected, and respects KIOKU_MESH_MCP_ALLOW_TTY.
Linux-only (pty module).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='pty not available on Windows')


def _find_kioku_mesh_mcp() -> str | None:
    """Locate the installed ``kioku-mesh-mcp`` script for subprocess tests."""
    candidate = Path(sys.executable).parent / 'kioku-mesh-mcp'
    if candidate.exists():
        return str(candidate)
    return shutil.which('kioku-mesh-mcp')


KIOKU_MESH_MCP = _find_kioku_mesh_mcp()
_MCP_ENTRY = [KIOKU_MESH_MCP] if KIOKU_MESH_MCP is not None else None


def _run_via_pty(extra_env: dict[str, str] | None = None, timeout: float = 5.0) -> subprocess.CompletedProcess[bytes]:
    """Launch kioku-mesh-mcp with a real PTY as stdin."""
    import pty

    assert _MCP_ENTRY is not None

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            _MCP_ENTRY,
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
        )
        # slave end is now owned by the child; close our copy so EOF propagates
        os.close(slave_fd)
        slave_fd = -1
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(_MCP_ENTRY, proc.returncode, stdout, stderr)
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)


@pytest.mark.skipif(
    _MCP_ENTRY is None,
    reason='kioku-mesh-mcp console script not installed — run `pip install -e .[dev]` to enable',
)
def test_tty_exits_with_code_2() -> None:
    """TTY stdin → exit 2 with usage message on stderr."""
    result = _run_via_pty()
    assert result.returncode == 2, f'expected exit 2, got {result.returncode}\nstderr={result.stderr!r}'
    assert b'stdio MCP server' in result.stderr, f'expected usage in stderr, got {result.stderr!r}'


@pytest.mark.skipif(
    _MCP_ENTRY is None,
    reason='kioku-mesh-mcp console script not installed — run `pip install -e .[dev]` to enable',
)
def test_stdin_redirect_does_not_exit_2() -> None:
    """Non-TTY stdin (pipe) → no immediate sys.exit(2); stdio loop entered."""
    assert _MCP_ENTRY is not None
    proc = subprocess.Popen(
        _MCP_ENTRY,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Close stdin immediately to let the process see EOF and decide what to do.
    # We don't care whether it exits cleanly or with an MCP error; we only
    # assert it did NOT exit with code 2 (which is the TTY-guard code).
    proc.stdin.close()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    assert proc.returncode != 2, f'unexpected TTY-guard exit on pipe stdin (returncode={proc.returncode})'


@pytest.mark.skipif(
    _MCP_ENTRY is None,
    reason='kioku-mesh-mcp console script not installed — run `pip install -e .[dev]` to enable',
)
def test_allow_tty_env_bypasses_check() -> None:
    """KIOKU_MESH_MCP_ALLOW_TTY=1 with TTY stdin → stdio loop entered, no exit 2."""
    result = _run_via_pty(extra_env={'KIOKU_MESH_MCP_ALLOW_TTY': '1'}, timeout=2.0)
    assert result.returncode != 2, (
        f'KIOKU_MESH_MCP_ALLOW_TTY=1 should bypass the TTY guard, got returncode={result.returncode}\n'
        f'stderr={result.stderr!r}'
    )

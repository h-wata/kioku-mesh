"""Tests for TTY misinvocation detection in mesh-mem-mcp entry point.

Verifies that ``mesh-mem-mcp`` exits with code 2 when stdin is a TTY,
passes through when stdin is redirected, and respects MESH_MEM_MCP_ALLOW_TTY.
Linux-only (pty module).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='pty not available on Windows')

_MCP_ENTRY = [sys.executable, '-m', 'mesh_mem.mcp_server']


def _run_via_pty(extra_env: dict[str, str] | None = None, timeout: float = 5.0) -> subprocess.CompletedProcess[bytes]:
    """Launch mesh-mem-mcp with a real PTY as stdin."""
    import pty

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


def test_tty_exits_with_code_2() -> None:
    """TTY stdin → exit 2 with usage message on stderr."""
    result = _run_via_pty()
    assert result.returncode == 2, f'expected exit 2, got {result.returncode}\nstderr={result.stderr!r}'
    assert b'stdio MCP server' in result.stderr, f'expected usage in stderr, got {result.stderr!r}'


def test_stdin_redirect_does_not_exit_2() -> None:
    """Non-TTY stdin (pipe) → no immediate sys.exit(2); stdio loop entered."""
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


def test_allow_tty_env_bypasses_check() -> None:
    """MESH_MEM_MCP_ALLOW_TTY=1 with TTY stdin → stdio loop entered, no exit 2."""
    result = _run_via_pty(extra_env={'MESH_MEM_MCP_ALLOW_TTY': '1'}, timeout=2.0)
    assert result.returncode != 2, (
        f'MESH_MEM_MCP_ALLOW_TTY=1 should bypass the TTY guard, got returncode={result.returncode}\n'
        f'stderr={result.stderr!r}'
    )

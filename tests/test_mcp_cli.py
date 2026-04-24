"""Subprocess smoke test for the ``mesh-mem-mcp`` console script.

Spawns the installed entry-point binary, speaks MCP over stdio through
``fastmcp.client.transports.StdioTransport``, and exercises both the
low-latency path (``list_tools``) and a full round-trip against the
live ``single_zenohd`` router (``save_observation`` → store lookup).

Guarded by a skip when the binary is absent: running the unit suite in
an environment without ``pip install -e .`` should not fail here.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import pytest

# Skip this whole module when fastmcp is missing rather than failing
# collection with ``ModuleNotFoundError`` on the top-level import.
pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402 — must follow importorskip
from fastmcp.client.transports import StdioTransport  # noqa: E402

from mesh_mem import store  # noqa: E402

_INGEST_SETTLE = 0.25


def _find_mesh_mem_mcp() -> str | None:
    """Locate the ``mesh-mem-mcp`` console script, preferring the active interpreter's venv."""
    # Prefer the binary right next to the interpreter running the test — this
    # matches the venv layout that `pip install -e .[dev]` produces and avoids
    # picking up a stale system-wide install.
    candidate = Path(sys.executable).parent / 'mesh-mem-mcp'
    if candidate.exists():
        return str(candidate)
    return shutil.which('mesh-mem-mcp')


MESH_MEM_MCP = _find_mesh_mem_mcp()


@pytest.mark.skipif(
    MESH_MEM_MCP is None,
    reason='mesh-mem-mcp console script not installed — run `pip install -e .[dev]` to enable',
)
def test_subprocess_list_tools() -> None:
    """``mesh-mem-mcp`` spawns cleanly and exposes the four tool definitions.

    Deliberately does NOT depend on ``single_zenohd`` — ``list_tools`` only
    exercises MCP startup and the decorator-based tool registry, never
    opening a Zenoh session. Skipping this on unit-only hosts (no zenohd
    binary) would needlessly weaken the console-script coverage.
    """

    async def _go() -> list[str]:
        env = os.environ.copy()
        transport = StdioTransport(command=MESH_MEM_MCP, args=[], env=env)
        async with Client(transport) as client:
            tools = await client.list_tools()
            return [t.name for t in tools]

    names = asyncio.run(_go())
    assert set(names) >= {
        'save_observation',
        'search_memory',
        'delete_memory',
        'get_memory_status',
    }


@pytest.mark.skipif(
    MESH_MEM_MCP is None,
    reason='mesh-mem-mcp console script not installed',
)
def test_subprocess_save_roundtrip_via_live_router(single_zenohd: Any) -> None:  # noqa: ARG001
    """End-to-end smoke: subprocess saves, the parent process reads it back from the router."""

    async def _go() -> str:
        env = os.environ.copy()
        transport = StdioTransport(command=MESH_MEM_MCP, args=[], env=env)
        async with Client(transport) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'saved-through-subprocess',
                    'project': 'mcp-cli',
                    'tags': ['subproc'],
                },
            )
            assert not result.is_error
            return result.data

    msg = asyncio.run(_go())
    assert '保存完了' in msg
    obs_id = msg.split()[-1]
    assert len(obs_id) == 32

    time.sleep(_INGEST_SETTLE)
    # The subprocess MCP server and this test share the same zenohd instance
    # via the inherited ZENOH_CONNECT — so a store lookup here must surface
    # the observation that the subprocess just published.
    found = store.find_observation_by_id(obs_id)
    assert found is not None
    assert found.content == 'saved-through-subprocess'
    assert found.project == 'mcp-cli'

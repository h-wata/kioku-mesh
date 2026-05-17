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
import mesh_mem.__main__ as cli_module  # noqa: E402
from mesh_mem.models import Observation  # noqa: E402

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


# ---------------------------------------------------------------------------
# Phase 3 CLI tests — exercise ``mesh_mem.__main__.main()`` directly
# ---------------------------------------------------------------------------

from mesh_mem.__main__ import main as cli_main  # noqa: E402


def test_cli_save_with_new_fields(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """All new options are accepted and persisted."""
    rc = cli_main(
        [
            'save',
            'test-content-phase3',
            '--memory-type',
            'decision',
            '--importance',
            '4',
            '--subject',
            'test-subject',
            '--summary',
            'test-summary',
            '--source-files',
            'a.py,b.py',
            '--supersedes',
            '',
            '-p',
            'proj-phase3',
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '保存完了' in out
    obs_id = out.strip().split()[-1]
    assert len(obs_id) == 32

    time.sleep(_INGEST_SETTLE)
    obs = store.find_observation_by_id(obs_id)
    assert obs is not None
    assert obs.memory_type == 'decision'
    assert obs.importance == 4
    assert obs.subject == 'test-subject'
    assert obs.summary == 'test-summary'
    assert obs.source_files == ['a.py', 'b.py']


def test_cli_get_memory(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """Save then get-memory returns all structured fields."""
    rc = cli_main(
        [
            'save',
            'get-memory-content',
            '--memory-type',
            'bug',
            '--importance',
            '3',
            '--subject',
            'root-cause',
            '--summary',
            'one-line-summary',
            '--source-files',
            'x.py',
        ]
    )
    assert rc == 0
    obs_id = capsys.readouterr().out.strip().split()[-1]

    time.sleep(_INGEST_SETTLE)
    rc2 = cli_main(['get-memory', obs_id])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert f'id: {obs_id}' in out
    assert 'memory_type: bug' in out
    assert 'importance: 3' in out
    assert 'subject: root-cause' in out
    assert 'summary: one-line-summary' in out
    assert 'source_files: x.py' in out
    assert '---' in out
    assert 'get-memory-content' in out


def test_cli_search_summary_priority(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """Search output shows summary when present, falls back to content[:80]."""
    rc = cli_main(
        [
            'save',
            'full-body-content-that-is-long',
            '--summary',
            'short-summary',
            '-p',
            'proj-search',
        ]
    )
    assert rc == 0
    obs_id = capsys.readouterr().out.strip().split()[-1]

    time.sleep(_INGEST_SETTLE)
    rc2 = cli_main(['search', '--project', 'proj-search'])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert 'short-summary' in out
    assert f'<id={obs_id}>' in out
    assert '[note][2]' in out


def test_cli_status_reports_transport_and_pending_puts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli_module, 'search_observations', lambda limit=store.MAX_SEARCH: [])
    monkeypatch.setattr(cli_module, 'get_pc_id', lambda: 'p' * 32)
    monkeypatch.setattr(cli_module, 'get_session_id', lambda: 'test-session')
    monkeypatch.setattr(cli_module, 'mesh_ready_label', lambda: 'yes')
    monkeypatch.setattr(
        cli_module,
        'get_transport_status',
        lambda: store.TransportStatus(
            zenoh_session='disconnected',
            last_put_at_iso='2026-05-17T00:00:00.000000Z',
            last_put_status='error: ConnectionError',
            recent_put_ok=4,
            recent_put_error=1,
            recent_put_window=5,
            pending_puts=2,
        ),
    )

    rc = cli_main(['status'])

    assert rc == 0
    out = capsys.readouterr().out
    assert 'zenoh_session: disconnected' in out
    assert 'last_put_at_iso: 2026-05-17T00:00:00.000000Z' in out
    assert 'last_put_status: error: ConnectionError' in out
    assert 'recent_puts: 4 ok / 1 error' in out
    assert 'pending_puts: 2' in out


def test_cli_save_importance_out_of_range(capsys: pytest.CaptureFixture) -> None:
    """Importance outside 1-5 causes argparse error (exit code 2)."""
    with pytest.raises(SystemExit) as exc:
        cli_main(['save', 'content', '--importance', '6'])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc2:
        cli_main(['save', 'content', '--importance', '0'])
    assert exc2.value.code == 2


def test_cli_source_files_csv(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """--source-files a.py,b.py is parsed into list[str]."""
    rc = cli_main(['save', 'csv-test', '--source-files', 'a.py,b.py'])
    assert rc == 0
    obs_id = capsys.readouterr().out.strip().split()[-1]

    time.sleep(_INGEST_SETTLE)
    obs = store.find_observation_by_id(obs_id)
    assert obs is not None
    assert obs.source_files == ['a.py', 'b.py']


def test_cli_bulk_delete_dry_run_by_project(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """Bulk delete dry-run reports the count and leaves matching rows visible."""
    target_a = Observation(content='bulk-dry-a', project='bulk-dry')
    target_b = Observation(content='bulk-dry-b', project='bulk-dry')
    other = Observation(content='bulk-dry-other', project='other-project')
    store.put_observation(target_a)
    store.put_observation(target_b)
    store.put_observation(other)
    time.sleep(_INGEST_SETTLE)

    rc = cli_main(['delete', '--project', 'bulk-dry', '--dry-run'])
    assert rc == 0
    captured = capsys.readouterr()
    assert "bulk delete 対象: 2 件 (project='bulk-dry')" in captured.err
    assert 'Dry run' in captured.out

    visible = {obs.observation_id for obs in store.search_observations(project='bulk-dry')}
    assert target_a.observation_id in visible
    assert target_b.observation_id in visible
    assert other.observation_id not in visible


def test_cli_bulk_delete_requires_yes_in_noninteractive(
    single_zenohd: Any,  # noqa: ARG001
    capsys: pytest.CaptureFixture,
) -> None:
    """Non-interactive bulk delete must require ``--yes`` before mutating state."""
    target = Observation(content='bulk-needs-yes', project='bulk-needs-yes')
    store.put_observation(target)
    time.sleep(_INGEST_SETTLE)

    rc = cli_main(['delete', '--project', 'bulk-needs-yes'])
    assert rc == 2
    captured = capsys.readouterr()
    assert '非対話環境では --yes を併用してください。' in captured.err

    visible = {obs.observation_id for obs in store.search_observations(project='bulk-needs-yes')}
    assert target.observation_id in visible


def test_cli_bulk_delete_executes_with_until_filter(
    single_zenohd: Any,  # noqa: ARG001
    capsys: pytest.CaptureFixture,
) -> None:
    """Bulk delete honors ``--until`` and tombstones only matching rows."""
    old_obs = Observation(
        content='bulk-until-old',
        project='bulk-until',
        created_at='2026-05-01T00:00:00.000000Z',
    )
    new_obs = Observation(
        content='bulk-until-new',
        project='bulk-until',
        created_at='2026-05-20T00:00:00.000000Z',
    )
    store.put_observation(old_obs)
    store.put_observation(new_obs)
    time.sleep(_INGEST_SETTLE)

    rc = cli_main(
        [
            'delete',
            '--project',
            'bulk-until',
            '--until',
            '2026-05-10T00:00:00Z',
            '--yes',
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "bulk delete 対象: 1 件 (project='bulk-until', until='2026-05-10T00:00:00Z')" in captured.err
    assert '削除（tombstone）完了: 1 件' in captured.out
    time.sleep(_INGEST_SETTLE)

    visible = {obs.observation_id for obs in store.search_observations(project='bulk-until')}
    assert old_obs.observation_id not in visible
    assert new_obs.observation_id in visible


def test_cli_bulk_delete_requires_selector(capsys: pytest.CaptureFixture) -> None:
    """Bulk mode without id or narrowing selector is rejected."""
    rc = cli_main(['delete'])
    assert rc == 2
    captured = capsys.readouterr()
    assert 'bulk delete では --project/--pc-id/--since/--until のいずれかが必要です。' in captured.err

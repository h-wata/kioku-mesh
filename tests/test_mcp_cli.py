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
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
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
    """``mesh-mem-mcp`` spawns cleanly and exposes the registered tool definitions.

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
        'get_memory',
        'drain_pending_puts',
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
    assert 'saved' in msg
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
            '--references',
            'h-wata/mesh-mem#73,PR#68',
            '--supersedes',
            '',
            '-p',
            'proj-phase3',
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert 'saved' in out
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
    assert obs.references == ['h-wata/mesh-mem#73', 'PR#68']


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
            '--references',
            'h-wata/mesh-mem#73',
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
    assert 'references: h-wata/mesh-mem#73' in out
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
            '--references',
            '#73,PR#68',
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
    assert '(refs: #73, PR#68)' in out
    assert f'<id={obs_id}>' in out
    assert '[note][2]' in out


def test_cli_search_markdown_fallbacks(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """Markdown output is one bullet per row with subject/summary/content fallback."""
    with_subject = Observation(
        content='body-subject',
        project='proj-search-md',
        subject='issue-58',
        summary='accepted plan',
        references=['#58'],
    )
    with_summary = Observation(
        content='body-summary',
        project='proj-search-md',
        summary='summary-only',
    )
    with_content = Observation(
        content='x' * 90,
        project='proj-search-md',
    )
    store.put_observation(with_subject)
    store.put_observation(with_summary)
    store.put_observation(with_content)
    time.sleep(_INGEST_SETTLE)

    rc = cli_main(['search', '--project', 'proj-search-md', '--format', 'markdown'])

    assert rc == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 3
    assert all(line.startswith('- **[') for line in lines)
    assert f'issue-58 — accepted plan (refs: #58) <id={with_subject.observation_id}>' in out
    assert f'summary-only <id={with_summary.observation_id}>' in out
    assert f'{"x" * 80}… <id={with_content.observation_id}>' in out


def test_cli_search_json_includes_full_fields(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """JSON output returns full observation objects."""
    rc = cli_main(
        [
            'save',
            'json-body',
            '--memory-type',
            'decision',
            '--importance',
            '4',
            '--subject',
            'json-subject',
            '--summary',
            'json-summary',
            '--source-files',
            'a.py,b.py',
            '--references',
            'h-wata/mesh-mem#73,PR#68',
            '--supersedes',
            'a' * 32,
            '--tags',
            'x,y',
            '-p',
            'proj-search-json',
        ]
    )
    assert rc == 0
    obs_id = capsys.readouterr().out.strip().split()[-1]

    time.sleep(_INGEST_SETTLE)
    rc2 = cli_main(['search', '--project', 'proj-search-json', '--format', 'json'])

    assert rc2 == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert set(row) >= {
        'content',
        'agent_family',
        'client_id',
        'pc_id',
        'session_id',
        'project',
        'tags',
        'observation_id',
        'created_at',
        'memory_type',
        'importance',
        'subject',
        'summary',
        'source_files',
        'references',
        'supersedes',
    }
    assert row['observation_id'] == obs_id
    assert row['memory_type'] == 'decision'
    assert row['importance'] == 4
    assert row['subject'] == 'json-subject'
    assert row['summary'] == 'json-summary'
    assert row['source_files'] == ['a.py', 'b.py']
    assert row['references'] == ['h-wata/mesh-mem#73', 'PR#68']
    assert row['supersedes'] == ['a' * 32]
    assert row['tags'] == ['x', 'y']


def test_cli_search_empty_format_variants(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Empty search results follow the per-format output contract."""
    monkeypatch.setattr(cli_module, 'search_observations', lambda **kwargs: [])

    assert cli_main(['search']) == 0
    assert capsys.readouterr().out == 'No matching memories.\n'

    assert cli_main(['search', '--format', 'markdown']) == 0
    assert capsys.readouterr().out == ''

    assert cli_main(['search', '--format', 'json']) == 0
    assert capsys.readouterr().out == '[]\n'


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
            drain_in_progress=True,
            drain_last_run_iso='2026-05-17T00:00:02.000000Z',
            drain_total_succeeded=7,
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
    assert 'drain_in_progress: yes' in out
    assert 'drain_last_run_iso: 2026-05-17T00:00:02.000000Z' in out
    assert 'drain_total_succeeded: 7' in out


def test_cli_drain_pending_replays_queued_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    class _WorkingSession:
        def __init__(self) -> None:
            self.put_calls: list[str] = []

        def put(self, key_expr: str, payload: str) -> None:  # noqa: ARG002
            self.put_calls.append(key_expr)

        def close(self) -> None:
            pass

    dummy_index = SimpleNamespace(
        upsert=lambda obs: None,
        mark_deleted=lambda observation_id, deleted_at: None,
    )
    monkeypatch.setattr(store, 'get_index', lambda: dummy_index)
    working = _WorkingSession()
    monkeypatch.setattr(store, '_open_session', lambda: working)
    store._reset_session()
    store._reset_index()

    queued = [Observation(content=f'cli-drain-{i}') for i in range(3)]
    for obs in queued:
        store._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())

    rc = cli_main(['drain', '--pending', '--limit', '2'])

    assert rc == 0
    out = capsys.readouterr().out
    assert 'pending_puts drain complete: drained=2, remaining=1' in out
    assert working.put_calls == [queued[0].key_expr, queued[1].key_expr]


def test_cli_main_requests_background_drain_shutdown_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli_module, 'search_observations', lambda limit=store.MAX_SEARCH: [])
    monkeypatch.setattr(cli_module, 'get_pc_id', lambda: 'p' * 32)
    monkeypatch.setattr(cli_module, 'get_session_id', lambda: 'test-session')
    monkeypatch.setattr(cli_module, 'mesh_ready_label', lambda: 'yes')
    monkeypatch.setattr(
        cli_module,
        'get_transport_status',
        lambda: store.TransportStatus(
            zenoh_session='connected',
            last_put_at_iso='',
            last_put_status='never',
            recent_put_ok=0,
            recent_put_error=0,
            recent_put_window=0,
            pending_puts=0,
            drain_in_progress=False,
            drain_last_run_iso='',
            drain_total_succeeded=0,
        ),
    )
    monkeypatch.setattr(cli_module, 'stop_pending_drain_background', lambda: calls.append('stop'))
    monkeypatch.setattr(cli_module, '_reset_session', lambda: calls.append('reset'))

    assert cli_main(['status']) == 0
    capsys.readouterr()
    assert calls == ['stop', 'reset']


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


def test_cli_references_csv(single_zenohd: Any, capsys: pytest.CaptureFixture) -> None:  # noqa: ARG001
    """--references #73,PR#68 is parsed into list[str]."""
    rc = cli_main(['save', 'refs-test', '--references', '#73,PR#68'])
    assert rc == 0
    obs_id = capsys.readouterr().out.strip().split()[-1]

    time.sleep(_INGEST_SETTLE)
    obs = store.find_observation_by_id(obs_id)
    assert obs is not None
    assert obs.references == ['#73', 'PR#68']


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
    assert "bulk delete target: 2 entries (project='bulk-dry')" in captured.err
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
    assert 'Pass --yes for non-interactive use.' in captured.err

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
    assert "bulk delete target: 1 entries (project='bulk-until', until='2026-05-10T00:00:00Z')" in captured.err
    assert 'deleted (tombstone): 1 entries' in captured.out
    time.sleep(_INGEST_SETTLE)

    visible = {obs.observation_id for obs in store.search_observations(project='bulk-until')}
    assert old_obs.observation_id not in visible
    assert new_obs.observation_id in visible


def test_cli_bulk_delete_requires_selector(capsys: pytest.CaptureFixture) -> None:
    """Bulk mode without id or narrowing selector is rejected."""
    rc = cli_main(['delete'])
    assert rc == 2
    captured = capsys.readouterr()
    assert 'bulk delete requires one of --project/--pc-id/--since/--until.' in captured.err


def test_cli_bulk_delete_pages_past_batch_size(
    single_zenohd: Any,  # noqa: ARG001
    capsys: pytest.CaptureFixture,
) -> None:
    """``--batch-size`` < total forces cursor pagination; all rows tombstone (#66).

    The pre-#66 code aborted at ``len(matches) >= MAX_SEARCH``; the new
    cursor loop pages via ``until_iso`` and must reach the end even when
    the per-call page is much smaller than the target set.
    """
    project = 'bulk-chunk'
    targets = []
    base = '2026-04-01T00:00:'
    # 10 rows, each at a distinct seconds-resolution timestamp so the
    # cursor advances cleanly between pages.
    for i in range(10):
        obs = Observation(
            content=f'chunk-{i:02d}',
            project=project,
            created_at=f'{base}{i:02d}.000000Z',
        )
        store.put_observation(obs)
        targets.append(obs)
    time.sleep(_INGEST_SETTLE)

    rc = cli_main(['delete', '--project', project, '--yes', '--batch-size', '3'])
    assert rc == 0
    captured = capsys.readouterr()
    assert f'bulk delete target: 10 entries (project={project!r})' in captured.err
    assert 'deleted (tombstone): 10 entries' in captured.out
    time.sleep(_INGEST_SETTLE)

    visible = {obs.observation_id for obs in store.search_observations(project=project)}
    for obs in targets:
        assert obs.observation_id not in visible, f'{obs.observation_id} should be tombstoned'


def test_cli_bulk_delete_continues_on_failure(
    single_zenohd: Any,  # noqa: ARG001
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``put_tombstone`` failure must not abort the rest (#66)."""
    project = 'bulk-fail'
    targets = []
    for i in range(4):
        obs = Observation(
            content=f'fail-{i}',
            project=project,
            created_at=f'2026-04-02T00:00:{i:02d}.000000Z',
        )
        store.put_observation(obs)
        targets.append(obs)
    time.sleep(_INGEST_SETTLE)

    failing_id = targets[1].observation_id
    real_put_tombstone = store.put_tombstone

    def flaky(obs: Observation, reason: str = '') -> None:
        if obs.observation_id == failing_id:
            raise RuntimeError('simulated transport hiccup')
        real_put_tombstone(obs, reason=reason)

    monkeypatch.setattr(cli_module, 'put_tombstone', flaky)

    rc = cli_main(['delete', '--project', project, '--yes', '--batch-size', '2'])
    assert rc == 1, 'non-zero exit when at least one row failed'
    captured = capsys.readouterr()
    assert 'deleted (tombstone): 3 entries (1 failures)' in captured.out
    assert f'put_tombstone failed for {failing_id}' in captured.err


def test_cli_bulk_delete_emits_local_index_hint(
    single_zenohd: Any,  # noqa: ARG001
    capsys: pytest.CaptureFixture,
) -> None:
    """Completion of bulk delete always emits the local-index escape-hatch hint (#66)."""
    obs = Observation(content='hint-target', project='bulk-hint')
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    rc = cli_main(['delete', '--project', 'bulk-hint', '--yes'])
    assert rc == 0
    captured = capsys.readouterr()
    assert 'mesh-mem --rebuild gc --retention-days 0 --project <name>' in captured.err

"""Smoke tests for the FastMCP server exposed by :mod:`mesh_mem.mcp_server`.

Uses FastMCP's in-process ``Client(FastMCP)`` pattern so we exercise tool
registration + argument binding + return-value marshalling without spawning
a subprocess. The subprocess launch path is covered separately in
``test_mcp_cli.py``.

Each test body is sync; we wrap the async MCP client in ``asyncio.run``.
``_INGEST_SETTLE`` matches the sibling store / gc tests and absorbs the
async ingest lag between a put and the next query.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

# Skip this whole module — and every collected test in it — when fastmcp is
# not installed, instead of letting pytest's collection abort with
# ``ModuleNotFoundError`` on the top-level import.
pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402 — must follow importorskip

from mesh_mem import store  # noqa: E402
from mesh_mem import transport  # noqa: E402
from mesh_mem.mcp_server import mcp  # noqa: E402
import mesh_mem.mcp_server as mcp_server_module  # noqa: E402
from mesh_mem.models import Observation  # noqa: E402

_INGEST_SETTLE = 0.25


def _mk_obs(content: str, project: str = 'mcp-test') -> Observation:
    return Observation(
        content=content,
        agent_family='claude',
        client_id='claude-code',
        pc_id='mcp-pc',
        session_id='mcp-sess',
        project=project,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_list_tools_registers_all_tools(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> list[str]:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return [t.name for t in tools]

    names = _run(_go())
    assert set(names) >= {
        'save_observation',
        'search_memory',
        'delete_memory',
        'get_memory_status',
        'get_memory',
        'drain_pending_puts',
    }


def test_server_advertises_proactive_instructions(single_zenohd: Any) -> None:  # noqa: ARG001
    """Verify the MCP server ships a PROACTIVE SAVE protocol.

    Coding agents must auto-trigger ``save_observation`` without per-project
    CLAUDE.md tweaks. Without this, dogfooding fell back to manual saves only.
    """

    async def _go() -> str | None:
        async with Client(mcp) as client:
            init_result = client.initialize_result
            if init_result is None:
                return None
            return init_result.instructions

    instructions = _run(_go())
    assert instructions, 'FastMCP must expose initialize().instructions'
    assert 'PROACTIVE SAVE' in instructions
    assert 'save_observation' in instructions
    assert 'search_memory' in instructions
    assert 'SKIP saving when the entry would mostly duplicate another source of truth' in instructions
    assert 'PR / Issue lifecycle ticks' in instructions
    assert 'Prefer decision / bug / pattern / config over summary' in instructions
    # Issue #158: approval triggers must be framed as a language-agnostic
    # semantic act, anchored by multilingual examples so non-English users
    # are not silently dropped.
    assert 'semantic act of approval' in instructions
    assert 'regardless of phrasing or language' in instructions
    for lang_tag in ('EN:', 'JA:', 'ZH:', 'KO:'):
        assert lang_tag in instructions, f'missing multilingual anchor: {lang_tag}'
    # Issue #158: SoR SKIP rule must explicitly carve out the rationale
    # (alternatives / constraints / preferences) so the why is not lost.
    assert 'SKIP exception' in instructions
    assert 'save the WHY' in instructions
    assert 'Alternatives that were considered and rejected' in instructions


def test_save_observation_persists_to_store(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {'content': 'hello from mcp smoke', 'project': 'mcp-smoke', 'tags': ['a', 'b']},
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    assert 'saved' in msg
    # Extract the 32-char id (last whitespace-separated token of the success message).
    obs_id = msg.split()[-1]
    assert len(obs_id) == 32

    time.sleep(_INGEST_SETTLE)
    found = store.find_observation_by_id(obs_id)
    assert found is not None
    assert found.content == 'hello from mcp smoke'
    assert found.project == 'mcp-smoke'
    assert set(found.tags) == {'a', 'b'}


def test_search_memory_finds_saved_entry(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('needle for mcp search', project='mcp-search')
    obs.references = ['#73', 'PR#68']
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'needle', 'project': 'mcp-search', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text
    assert 'needle for mcp search' in text
    assert '(refs: #73, PR#68)' in text


def test_search_memory_empty_reports_none(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'project': 'project-that-has-nothing'},
            )
            return result.data

    text = _run(_go())
    assert 'No matching memories' in text


def test_delete_memory_emits_tombstone(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('soon to be tombstoned via mcp', project='mcp-delete')
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'delete_memory',
                {'observation_id': obs.observation_id, 'reason': 'smoke'},
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    assert 'deleted' in msg
    assert obs.observation_id in msg

    time.sleep(_INGEST_SETTLE)
    remaining = store.search_observations(project='mcp-delete')
    assert obs.observation_id not in [r.observation_id for r in remaining]


def test_delete_memory_rejects_short_id(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'delete_memory',
                {'observation_id': 'deadbeef'},  # 8 chars — rejected before any scan
            )
            # Tool returns an error string in data (not is_error) to stay LLM-friendly.
            return result.data

    msg = _run(_go())
    assert '32-character match' in msg


def test_delete_memory_reports_missing_id(single_zenohd: Any) -> None:  # noqa: ARG001
    phantom_id = 'a' * 32

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'delete_memory',
                {'observation_id': phantom_id},
            )
            return result.data

    msg = _run(_go())
    assert 'not found' in msg
    assert phantom_id in msg


def test_get_memory_status_reports_version_and_counts(single_zenohd: Any) -> None:  # noqa: ARG001
    store.put_observation(_mk_obs('status obs 1', project='mcp-status'))
    store.put_observation(_mk_obs('status obs 2', project='mcp-status'))
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'kioku-mesh version' in text
    assert 'pc_id' in text
    assert 'session_id' in text
    assert 'zenoh_session: connected' in text
    assert 'last_put_status: ok' in text
    assert 'pending_puts: 0' in text
    assert 'index_rows: live=2 / tomb=0 / shadow=0' in text
    # At least the 2 we put show up in the count summary.
    assert 'count (within limit' in text


def test_get_memory_status_reports_shadow_rows(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('shadowed for status', project='mcp-status-shadow')
    store.get_index().upsert(obs)
    store.get_index().mark_shadowed_missing(obs.observation_id, '2026-05-18T00:00:00.000000Z')

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'index_rows: live=0 / tomb=0 / shadow=1' in text


def test_get_memory_status_reports_disconnected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_mem import backend as backend_module
    from mesh_mem.backend import BackendStatus

    mock_status = BackendStatus(
        mode='zenoh',
        live=0,
        tombstoned=0,
        shadowed=0,
        zenoh_session='disconnected',
        last_put_at_iso='2026-05-16T00:00:00.000000Z',
        last_put_status='error: ZError',
        pending_puts=3,
    )

    class _MockBackend:
        def search_observations(self, **kwargs):  # noqa: ANN202
            return []

        def get_status(self) -> BackendStatus:
            return mock_status

        def close(self) -> None:
            pass

    monkeypatch.setattr(backend_module, '_backend_cache', _MockBackend())

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'zenoh_session: disconnected' in text
    assert 'last_put_at_iso: 2026-05-16T00:00:00.000000Z' in text
    assert 'last_put_status: error: ZError' in text
    assert 'pending_puts: 3' in text


def test_drain_pending_puts_tool_replays_queued_rows(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(transport, '_open_session', lambda: working)
    store._reset_session()
    store._reset_index()

    queued = [Observation(content=f'mcp-drain-{i}', project='mcp-drain') for i in range(2)]
    for obs in queued:
        store._enqueue_pending_put('observation', obs.key_expr, obs.observation_id, obs.to_json())

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('drain_pending_puts', {'limit': 1})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'pending_puts drain complete: drained=1, remaining=1' in text
    assert working.put_calls == [queued[0].key_expr]


def test_main_starts_and_stops_pending_drain_around_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.delenv('ZENOH_CONNECT', raising=False)
    monkeypatch.setattr(mcp_server_module, 'start_pending_drain_background', lambda: calls.append('start'))
    monkeypatch.setattr(mcp_server_module, 'stop_pending_drain_background', lambda: calls.append('stop'))
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: calls.append('run'))

    mcp_server_module.main()

    assert calls == ['start', 'run', 'stop']


def test_main_warns_when_zenoh_connect_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/127.0.0.1:65534')
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: None)

    def _fail(addr: tuple[str, int], timeout: float = 0.5) -> Any:  # noqa: ARG001
        raise ConnectionRefusedError('connect refused')

    monkeypatch.setattr(mcp_server_module.socket, 'create_connection', _fail)

    mcp_server_module.main()
    err = capsys.readouterr().err
    assert 'WARNING: ZENOH_CONNECT=tcp/127.0.0.1:65534 is unreachable' in err
    assert 'connect refused' in err


def test_main_skips_warning_when_any_zenoh_endpoint_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/127.0.0.1:1,tcp/127.0.0.1:7447')
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: None)

    class _DummySocket:
        def close(self) -> None:
            pass

    calls: list[tuple[str, int]] = []

    def _probe(addr: tuple[str, int], timeout: float = 0.5) -> Any:  # noqa: ARG001
        calls.append(addr)
        if addr[1] == 1:
            raise ConnectionRefusedError('first down')
        return _DummySocket()

    monkeypatch.setattr(mcp_server_module.socket, 'create_connection', _probe)

    mcp_server_module.main()
    captured = capsys.readouterr()
    assert captured.err == ''
    assert calls == [('127.0.0.1', 1), ('127.0.0.1', 7447)]


def test_main_skips_warning_when_zenoh_connect_is_unset(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.delenv('ZENOH_CONNECT', raising=False)
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: None)

    mcp_server_module.main()
    captured = capsys.readouterr()
    assert captured.err == ''


def test_save_observation_with_all_new_fields(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'full field observation',
                    'project': 'mcp-phase2',
                    'tags': ['phase2'],
                    'memory_type': 'decision',
                    'importance': 4,
                    'subject': 'test subject',
                    'summary': 'test summary line',
                    'source_files': ['src/mesh_mem/mcp_server.py'],
                    'references': ['h-wata/mesh-mem#73'],
                    'supersedes': [],
                },
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    assert 'saved' in msg
    obs_id = msg.split()[-1]
    time.sleep(_INGEST_SETTLE)
    found = store.find_observation_by_id(obs_id)
    assert found is not None
    assert found.memory_type == 'decision'
    assert found.importance == 4
    assert found.subject == 'test subject'
    assert found.summary == 'test summary line'
    assert found.source_files == ['src/mesh_mem/mcp_server.py']
    assert found.references == ['h-wata/mesh-mem#73']


def test_save_observation_rejects_invalid_memory_type(single_zenohd: Any) -> None:  # noqa: ARG001
    """Reject invalid memory_type at the MCP boundary.

    The tool must return a friendly error string (not raise) when an LLM
    passes a memory_type outside the documented enum, and must not persist
    a partial observation.
    """

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'should not persist',
                    'project': 'mcp-mt-validate',
                    'memory_type': 'feature',  # invalid
                },
            )
            return result.data

    msg = _run(_go())
    assert 'memory_type' in msg
    assert 'feature' in msg

    time.sleep(_INGEST_SETTLE)
    leaked = store.search_observations(project='mcp-mt-validate', limit=10)
    assert leaked == [], 'invalid memory_type must not produce a stored obs'


def test_save_observation_backward_compat(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {'content': 'backward compat obs', 'project': 'mcp-compat'},
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    assert 'saved' in msg
    obs_id = msg.split()[-1]
    time.sleep(_INGEST_SETTLE)
    found = store.find_observation_by_id(obs_id)
    assert found is not None
    assert found.memory_type == 'note'
    assert found.importance == 2
    assert found.subject == ''
    assert found.summary == ''


def test_search_memory_summary_priority(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = Observation(
        content='long content that should be truncated in display',
        agent_family='claude',
        client_id='claude-code',
        pc_id='mcp-pc',
        session_id='mcp-sess',
        project='mcp-summary',
        memory_type='decision',
        importance=3,
        summary='short summary wins',
        references=['#73'],
    )
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'project': 'mcp-summary', 'limit': 5},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'short summary wins' in text
    assert '[decision][3]' in text
    assert '(refs: #73)' in text
    assert obs.observation_id in text


def test_get_memory_returns_full_metadata(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = Observation(
        content='full content for get_memory test',
        agent_family='claude',
        client_id='claude-code',
        pc_id='mcp-pc',
        session_id='mcp-sess',
        project='mcp-get',
        memory_type='bug',
        importance=5,
        subject='critical bug',
        summary='bug summary',
        source_files=['src/store.py'],
        references=['h-wata/mesh-mem#73'],
        supersedes=['a' * 32],
    )
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text
    assert 'memory_type: bug' in text
    assert 'importance: 5' in text
    assert 'subject: critical bug' in text
    assert 'summary: bug summary' in text
    assert 'source_files: src/store.py' in text
    assert 'references: h-wata/mesh-mem#73' in text
    assert 'full content for get_memory test' in text


def test_tool_descriptions_contain_proactive_hint() -> None:
    """Tool docstrings must carry per-tool proactive save reminders.

    These docstrings become the MCP tool descriptions seen by the LLM.
    Distributing PROACTIVELY across key tools reinforces the protocol in
    long sessions where the server instructions may have been pushed out of
    the context window.
    """
    assert mcp_server_module.save_observation.__doc__ is not None
    assert 'PROACTIVELY' in mcp_server_module.save_observation.__doc__
    assert mcp_server_module.search_memory.__doc__ is not None
    assert 'PROACTIVELY' in mcp_server_module.search_memory.__doc__
    assert mcp_server_module.get_memory_status.__doc__ is not None
    assert 'PROACTIVELY' in mcp_server_module.get_memory_status.__doc__


def test_get_memory_status_includes_last_save_at(single_zenohd: Any) -> None:  # noqa: ARG001
    """get_memory_status output contains last_save_at for proactive save nudging."""
    store.put_observation(_mk_obs('entry for last_save_at test', project='mcp-last-save'))
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'last_save_at:' in text


# Issue #158 Phase 2: session-scoped save count + nudge.


def test_get_memory_status_reports_session_save_block(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,
) -> None:  # noqa: ARG001
    """`this_session_*` + `session_age` fields appear and reflect saves for the current session."""
    from mesh_mem import identity

    identity.reset_caches()
    monkeypatch.setenv('MESH_MEM_SESSION_ID', '20260604T000000Z-nudgetst')
    current_sid = identity.get_session_id()
    obs = Observation(
        content='session-scoped entry',
        agent_family='claude',
        client_id='claude-code',
        pc_id='mcp-pc',
        session_id=current_sid,
        project='mcp-session-nudge',
    )
    store.put_observation(obs)
    time.sleep(_INGEST_SETTLE)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'this_session_saves: 1' in text
    assert 'this_session_last_save_age:' in text
    assert 'session_age:' in text
    # Recent save → no nudge expected (well under the 20-minute stale threshold).
    assert 'nudge:' not in text
    identity.reset_caches()


def test_get_memory_status_emits_nudge_for_stale_empty_session(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,
) -> None:  # noqa: ARG001
    """A long-running session with zero saves triggers the consider-saving nudge."""
    from mesh_mem import identity

    identity.reset_caches()
    # Session id timestamp prefix maps to 2024 → session_age is enormous,
    # well past the 10-minute no-saves threshold.
    monkeypatch.setenv('MESH_MEM_SESSION_ID', '20240101T000000Z-emptysess')
    identity.get_session_id()

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'this_session_saves: 0' in text
    assert 'nudge:' in text
    assert 'No save_observation calls in this session yet' in text
    identity.reset_caches()


def test_get_memory_status_session_age_dash_for_unparseable_id(
    monkeypatch: pytest.MonkeyPatch,
    single_zenohd: Any,
) -> None:  # noqa: ARG001
    """A custom session_id with no timestamp prefix shows session_age '-' and skips the nudge."""
    from mesh_mem import identity

    identity.reset_caches()
    monkeypatch.setenv('MESH_MEM_SESSION_ID', 'custom-handle-no-timestamp')
    identity.get_session_id()

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'session_age: -' in text
    # Unparseable timestamp → cannot prove the session is "stale" → no nudge.
    assert 'nudge:' not in text
    identity.reset_caches()

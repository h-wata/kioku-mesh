"""Smoke tests for the FastMCP server exposed by :mod:`mesh_mem.mcp_server`.

Uses FastMCP's in-process ``Client(FastMCP)`` pattern so we exercise tool
registration + argument binding + return-value marshalling without spawning
a subprocess. The subprocess launch path is covered separately in
``test_mcp_cli.py``.

Each test body is sync; we wrap the async MCP client in ``asyncio.run``.
Zenoh ``put`` is asynchronous — the storage plugin ingests on its own
background thread, so a query issued right after a put can miss it. Every
site that depends on a prior put waits for the condition it actually cares
about via ``wait_until`` (see ``tests/wait_helpers.py``) instead of sleeping
for a duration picked to be "usually enough".
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import re
from types import SimpleNamespace
from typing import Any

import pytest

# Skip this whole module — and every collected test in it — when fastmcp is
# not installed, instead of letting pytest's collection abort with
# ``ModuleNotFoundError`` on the top-level import.
pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402 — must follow importorskip

from kioku_mesh import store  # noqa: E402
from kioku_mesh import transport  # noqa: E402
from kioku_mesh.mcp_server import mcp  # noqa: E402
import kioku_mesh.mcp_server as mcp_server_module  # noqa: E402
from kioku_mesh.models import Observation  # noqa: E402

from .wait_helpers import wait_until  # noqa: E402


def _saved_id(text: str) -> str:
    """Extract observation id from save_observation response (JSON or legacy text)."""
    import json as _json

    try:
        data = _json.loads(text)
        if isinstance(data, dict) and 'observation_id' in data:
            return data['observation_id']
    except (ValueError, TypeError):
        pass
    return text.strip().split()[1]


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
                {
                    'content': 'hello from mcp smoke',
                    'project': 'mcp-smoke',
                    'tags': ['a', 'b'],
                    'subject': 'mcp smoke',
                    'summary': 'save reaches the store through the MCP tool',
                },
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    assert 'saved' in msg
    # Extract the 32-char id (last whitespace-separated token of the success message).
    obs_id = _saved_id(msg)
    assert len(obs_id) == 32

    found = wait_until(
        lambda: store.find_observation_by_id(obs_id),
        f'{obs_id} to be readable from the router',
    )
    assert found.content == 'hello from mcp smoke'
    assert found.project == 'mcp-smoke'
    assert set(found.tags) == {'a', 'b'}


def test_search_memory_finds_saved_entry(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('needle for mcp search', project='mcp-search')
    obs.references = ['#73', 'PR#68']
    store.put_observation(obs)
    wait_until(
        lambda: obs.observation_id in {r.observation_id for r in store.search_observations(project='mcp-search')},
        f'{obs.observation_id} to appear in search',
    )

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


# Issue #278: project alias read-side resolution (mesh-mem -> kioku-mesh).
def test_search_memory_alias_resolves_to_canonical_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Querying the legacy alias hits observations stored under the canonical name."""
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('canonical project content', project='kioku-mesh')
    backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'canonical', 'project': 'mesh-mem', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text


def test_search_memory_alias_query_returns_both_eras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy name is an equivalent label, not a redirect.

    Querying ``mesh-mem`` must return rows stored under *both* labels; a plain
    legacy->canonical rewrite would silently drop the legacy-stored rows.
    """
    backend = _mk_local_backend(monkeypatch)
    legacy = _mk_obs_full('both eras content legacy', project='mesh-mem')
    canonical = _mk_obs_full('both eras content canonical', project='kioku-mesh')
    for obs in (legacy, canonical):
        backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'both eras', 'project': 'mesh-mem', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert text.count(legacy.observation_id) == 1
    assert text.count(canonical.observation_id) == 1


def test_save_observation_with_legacy_project_is_not_rewritten(single_zenohd: Any) -> None:  # noqa: ARG001
    """Write side keeps the literal project value, and the read side still finds it.

    Round trip for the Issue #278 contract: nothing is rewritten on save
    (ADR-0028, append-only), so a row written under the legacy label has to be
    reachable through the canonical label at search time instead.
    """

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'legacy project write path',
                    'project': 'mesh-mem',
                    'subject': 'legacy project write path',
                    'summary': 'write side must not resolve aliases',
                },
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    obs_id = _saved_id(msg)
    stored = wait_until(
        lambda: store.find_observation_by_id(obs_id),
        f'{obs_id} to be readable from the router',
    )
    assert stored.project == 'mesh-mem'

    async def _search() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'legacy project write path', 'project': 'kioku-mesh', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    assert obs_id in _run(_search())


def test_search_memory_unknown_project_still_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown project stays empty while an aliased one still expands.

    The empty half alone is insensitive to alias resolution, so it is paired
    with the positive half here: expansion must apply to alias members only.
    """
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('unrelated content', project='mesh-mem')
    backend.put_observation(obs)

    async def _go() -> tuple[str, str]:
        async with Client(mcp) as client:
            unknown = await client.call_tool(
                'search_memory',
                {'project': 'totally-unknown-project'},
            )
            aliased = await client.call_tool(
                'search_memory',
                {'project': 'kioku-mesh', 'limit': 20},
            )
            return unknown.data, aliased.data

    unknown_text, aliased_text = _run(_go())
    assert 'No matching memories' in unknown_text
    assert obs.observation_id in aliased_text


def test_search_memory_canonical_project_finds_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the Issue #278 symptom itself.

    History saved under the legacy name must be reachable when searching by
    the canonical name.

    Before the fix, ``project='kioku-mesh'`` matched only rows literally stored
    as ``kioku-mesh`` and the ``mesh-mem`` era was invisible.
    """
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('legacy era content', project='mesh-mem')
    backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'legacy era', 'project': 'kioku-mesh', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text


def test_search_memory_canonical_project_merges_legacy_and_canonical_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical search returns both eras exactly once each (dedupe contract)."""
    backend = _mk_local_backend(monkeypatch)
    legacy = _mk_obs_full('merged era content legacy', project='mesh-mem')
    canonical = _mk_obs_full('merged era content canonical', project='kioku-mesh')
    other = _mk_obs_full('merged era content elsewhere', project='some-other-project')
    for obs in (legacy, canonical, other):
        backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'merged era', 'project': 'kioku-mesh', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert text.count(legacy.observation_id) == 1
    assert text.count(canonical.observation_id) == 1
    assert other.observation_id not in text


def test_search_memory_or_fallback_still_resolves_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #285's AND->OR fallback must reuse the same alias-expanded project.

    ``search_memory`` expands ``project`` to its alias set exactly once, near
    the top of the function, and rebinds the parameter so every query issued
    afterwards — including the AND->OR retry added by #285 — inherits the
    expansion (PR #288 review B3). This pins that contract down for the
    fallback call specifically: a row saved under the legacy ``mesh-mem``
    project must still be reachable when searching the canonical
    ``kioku-mesh`` name via the fallback path, not just the first AND search.

    The query terms are chosen so the initial AND search misses (the content
    lacks 'FTS5') and only the OR retry finds it, so a green result here
    proves the *fallback* call carried the expansion, not just the first one.
    """
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('alias fallback regression content', project='mesh-mem')
    backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                # 'FTS5' never appears in the content, so AND (all terms
                # required) misses and only the OR retry can find it.
                {'query': 'alias fallback FTS5', 'project': 'kioku-mesh', 'limit': 20},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert '(no AND match; fell back to OR)' in text
    assert obs.observation_id in text


def test_delete_memory_emits_tombstone(single_zenohd: Any) -> None:  # noqa: ARG001
    obs = _mk_obs('soon to be tombstoned via mcp', project='mcp-delete')
    store.put_observation(obs)
    wait_until(
        lambda: obs.observation_id in {r.observation_id for r in store.search_observations(project='mcp-delete')},
        f'{obs.observation_id} to appear in search',
    )

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

    wait_until(
        lambda: obs.observation_id not in {r.observation_id for r in store.search_observations(project='mcp-delete')},
        f'{obs.observation_id} to drop out of search after tombstone',
    )


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
    wait_until(
        lambda: len(store.search_observations(project='mcp-status')) >= 2,
        'both status obs to appear in search',
    )

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


def _iso_days_ago(days: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def test_get_memory_status_reports_last_7d_family_counts(single_zenohd: Any) -> None:  # noqa: ARG001
    """The last-7d family breakdown counts only entries within the 7-day window.

    Boundary decision: ``created_at >= now - 7 days`` is INCLUDED. This is
    exercised with a small safety margin (a few seconds either side of the
    cutoff) rather than an exact 7.000...-day offset, because the tool
    itself computes its own ``now`` after the observations are constructed
    and the store round-trip settles — an exact offset would flip sides of
    the boundary depending on real wall-clock jitter between test setup and
    the tool call.
    """
    recent = Observation(
        content='recent claude save',
        agent_family='claude',
        project='mcp-status-7d',
        created_at=_iso_days_ago(1),
    )
    just_inside_boundary = Observation(
        content='just under 7 days ago codex save',
        agent_family='codex',
        project='mcp-status-7d',
        created_at=_iso_days_ago(7 - 60 / 86400),
    )
    too_old = Observation(
        content='8 days ago codex save, must not count',
        agent_family='codex',
        project='mcp-status-7d',
        created_at=_iso_days_ago(8),
    )
    for obs in (recent, just_inside_boundary, too_old):
        store.put_observation(obs)
    wait_until(
        lambda: len(store.search_observations(project='mcp-status-7d')) >= 3,
        'all mcp-status-7d observations to appear in search',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'family (last 7d):' in text
    # All-time breakdown still reflects all 3 saves.
    assert '  family claude: 1' in text
    assert '  family codex: 2' in text
    # 7-day breakdown includes the boundary entry but excludes the 8-day-old one.
    assert '  family_7d claude: 1' in text
    assert '  family_7d codex: 1' in text


def test_get_memory_status_last_7d_section_present_when_empty(single_zenohd: Any) -> None:  # noqa: ARG001
    """The last-7d section header is emitted even when no save falls in the window."""
    obs = Observation(
        content='stale save well outside the 7-day window',
        agent_family='claude',
        project='mcp-status-7d-empty',
        created_at=_iso_days_ago(30),
    )
    store.put_observation(obs)
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id),
        f'{obs.observation_id} to be readable from the router',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'family (last 7d):' in text
    assert 'family_7d' not in text


def test_get_memory_status_last_7d_excludes_tombstoned(single_zenohd: Any) -> None:  # noqa: ARG001
    """A deleted (tombstoned) recent observation must not count toward the 7-day breakdown."""
    obs = Observation(
        content='recent save that gets deleted',
        agent_family='claude',
        project='mcp-status-7d-tomb',
        created_at=_iso_days_ago(1),
    )
    store.put_observation(obs)
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id),
        f'{obs.observation_id} to be readable from the router',
    )

    async def _delete() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'delete_memory',
                {'observation_id': obs.observation_id, 'reason': 'smoke'},
            )
            assert not result.is_error

    _run(_delete())
    wait_until(
        lambda: obs.observation_id
        not in {r.observation_id for r in store.search_observations(project='mcp-status-7d-tomb')},
        f'{obs.observation_id} to drop out of search after tombstone',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'family (last 7d):' in text
    assert 'family_7d' not in text


# --- last-7d contract tests against a mock backend + frozen clock -----------
#
# The store-backed tests above cannot pin the exact `now - 7d` boundary (real
# wall-clock jitter between test setup and the tool's own `now` flips it) and
# cannot afford MAX_SEARCH observations. These use a mock backend returning
# lightweight rows plus a frozen `_utcnow`, so the boundary operator and the
# search-limit behaviour are fixed exactly (Issue #280 cross-review B1/B2/B3).

_FROZEN_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _row(created_at: str, agent_family: str = 'claude') -> SimpleNamespace:
    """Build a minimal stand-in for the Observation fields get_memory_status reads."""
    return SimpleNamespace(agent_family=agent_family, pc_id='mock-pc', created_at=created_at)


def _status_text(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[SimpleNamespace],
    *,
    now: datetime = _FROZEN_NOW,
) -> str:
    from kioku_mesh import backend as backend_module
    from kioku_mesh.backend import BackendStatus

    mock_status = BackendStatus(
        mode='local',
        live=len(rows),
        tombstoned=0,
        shadowed=0,
        zenoh_session='n/a',
        last_put_at_iso=None,
        last_put_status='ok',
        pending_puts=0,
    )

    class _MockBackend:
        def search_observations(self, **kwargs):  # noqa: ANN003, ANN202, ARG002
            return list(rows)

        def get_status(self) -> BackendStatus:
            return mock_status

        def close(self) -> None:
            pass

    monkeypatch.setattr(backend_module, '_backend_cache', _MockBackend())
    # Per-session counts re-query the real store; stub it out so these stay
    # hermetic and fast.
    monkeypatch.setattr(mcp_server_module, 'search_observations', lambda **kwargs: [])  # noqa: ARG005
    monkeypatch.setattr(mcp_server_module, '_utcnow', lambda: now)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory_status', {})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'failed to read shared memory' not in text
    return text


def test_get_memory_status_last_7d_includes_exact_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """``created_at == now - 7d`` is INCLUDED; one microsecond older is not.

    With the clock frozen this pins the boundary operator: flipping the
    implementation from ``>=`` to ``>`` turns the claude count into 0 and
    fails this test.
    """
    cutoff = _FROZEN_NOW - timedelta(days=7)
    text = _status_text(
        monkeypatch,
        [
            _row(cutoff.isoformat(), 'claude'),
            _row((cutoff - timedelta(microseconds=1)).isoformat(), 'codex'),
        ],
    )
    assert 'family (last 7d):' in text
    assert '  family_7d claude: 1' in text
    assert 'family_7d codex' not in text


def test_get_memory_status_last_7d_is_partial_when_search_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAX_SEARCH rows all inside the window ⇒ counts are lower bounds, not exact.

    The true population here is MAX_SEARCH + 1; the backend can only return
    MAX_SEARCH, so the 7d section must say so instead of printing a
    confident-looking exact number.
    """
    one_day_ago = (_FROZEN_NOW - timedelta(days=1)).isoformat()
    rows = [_row(one_day_ago, 'claude') for _ in range(store.MAX_SEARCH)]
    text = _status_text(monkeypatch, rows)
    assert f'family (last 7d) [PARTIAL: search limit {store.MAX_SEARCH} reached' in text
    assert f'  family_7d claude: >={store.MAX_SEARCH}' in text
    assert f'  family_7d claude: {store.MAX_SEARCH}' not in text


def test_get_memory_status_last_7d_is_exact_when_limit_reached_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hitting MAX_SEARCH is not partial if the returned rows already reach past the cutoff."""
    rows = [_row((_FROZEN_NOW - timedelta(days=1)).isoformat(), 'claude')]
    rows += [_row((_FROZEN_NOW - timedelta(days=30)).isoformat(), 'codex') for _ in range(store.MAX_SEARCH - 1)]
    text = _status_text(monkeypatch, rows)
    assert 'family (last 7d):' in text
    assert 'PARTIAL' not in text
    assert '  family_7d claude: 1' in text


def test_get_memory_status_survives_naive_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """An offset-less timestamp must not TypeError the whole status; it is read as UTC."""
    naive = (_FROZEN_NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
    text = _status_text(
        monkeypatch,
        [
            _row(naive, 'claude'),
            _row((_FROZEN_NOW - timedelta(days=2)).isoformat(), 'claude'),
        ],
    )
    assert '  family_7d claude: 2' in text


def test_get_memory_status_last_7d_excludes_future_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """The window is [now-7d, now]: a future-dated row is excluded and reported."""
    text = _status_text(
        monkeypatch,
        [
            _row((_FROZEN_NOW + timedelta(days=1)).isoformat(), 'codex'),
            _row((_FROZEN_NOW - timedelta(days=1)).isoformat(), 'claude'),
        ],
    )
    assert '  family_7d claude: 1' in text
    assert 'family_7d codex' not in text
    assert '  family_7d skipped (created_at in the future): 1' in text


def test_get_memory_status_skips_invalid_and_missing_created_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparsable / empty created_at values are skipped individually, not fatal."""
    text = _status_text(
        monkeypatch,
        [
            _row('not-a-timestamp', 'codex'),
            _row('', 'codex'),
            _row((_FROZEN_NOW - timedelta(days=1)).isoformat(), 'claude'),
        ],
    )
    assert '  family_7d claude: 1' in text
    assert 'family_7d codex' not in text
    assert '  family_7d skipped (missing/unparsable created_at): 2' in text


def test_get_memory_status_reports_disconnected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import backend as backend_module
    from kioku_mesh.backend import BackendStatus

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


def test_main_owns_realignment_around_run_without_touching_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0035: MCP grants ownership, and revokes it on exit.

    The index and the zenoh session stay untouched: an MCP process whose
    client never calls a memory tool must not grow either as a side effect.
    """
    from kioku_mesh.memory import realignment
    from kioku_mesh.memory import store as store_mod

    seen: list[dict] = []
    monkeypatch.delenv('ZENOH_CONNECT', raising=False)
    monkeypatch.setattr(mcp_server_module, 'start_pending_drain_background', lambda: None)
    monkeypatch.setattr(mcp_server_module, 'stop_pending_drain_background', lambda: None)
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: seen.append(realignment.realignment_status()))

    mcp_server_module.main()

    assert seen == [{'enabled': True, 'running': False}]
    assert realignment.realignment_status() == {'enabled': False, 'running': False}
    assert store_mod._index is None


def test_main_warns_when_the_realignment_worker_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The shutdown result is checked, not discarded (PR #328 B1)."""
    monkeypatch.delenv('ZENOH_CONNECT', raising=False)
    monkeypatch.setattr(mcp_server_module, 'start_pending_drain_background', lambda: None)
    monkeypatch.setattr(mcp_server_module, 'stop_pending_drain_background', lambda: None)
    monkeypatch.setattr(mcp_server_module, 'disable_realignment', lambda: False)
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: None)

    mcp_server_module.main()

    assert 'index realignment worker did not stop before shutdown' in capsys.readouterr().err


def test_main_does_not_own_realignment_on_the_local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh.memory import realignment

    seen: list[dict] = []
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    monkeypatch.setattr(mcp_server_module.mcp, 'run', lambda: seen.append(realignment.realignment_status()))

    mcp_server_module.main()

    assert seen == [{'enabled': False, 'running': False}]


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
    obs_id = _saved_id(msg)
    found = wait_until(
        lambda: store.find_observation_by_id(obs_id),
        f'{obs_id} to be readable from the router',
    )
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
                    'subject': 'invalid memory type',
                    'summary': 'must be rejected before persistence',
                },
            )
            return result.data

    msg = _run(_go())
    assert 'memory_type' in msg
    assert 'feature' in msg

    # Negative assertion: prove the invalid save never landed rather than
    # sleeping and hoping. A sentinel put after the rejected call travels the
    # same session -> router -> storage path; once it is readable, an
    # erroneous persist of the earlier save would already have landed too.
    sentinel = _mk_obs('barrier for invalid memory_type', project='mcp-mt-validate')
    store.put_observation(sentinel)
    wait_until(
        lambda: sentinel.observation_id
        in {r.observation_id for r in store.search_observations(project='mcp-mt-validate', limit=10)},
        f'barrier {sentinel.observation_id} to appear in search',
    )
    leaked = store.search_observations(project='mcp-mt-validate', limit=10)
    assert [r.observation_id for r in leaked] == [sentinel.observation_id], (
        'invalid memory_type must not produce a stored obs'
    )


def test_save_observation_backward_compat(single_zenohd: Any) -> None:  # noqa: ARG001
    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'backward compat obs',
                    'project': 'mcp-compat',
                    'subject': 'optional field defaults',
                    'summary': 'memory_type and importance keep their defaults',
                },
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    assert 'saved' in msg
    obs_id = _saved_id(msg)
    found = wait_until(
        lambda: store.find_observation_by_id(obs_id),
        f'{obs_id} to be readable from the router',
    )
    assert found.memory_type == 'note'
    assert found.importance == 2
    assert found.subject == 'optional field defaults'
    assert found.summary == 'memory_type and importance keep their defaults'


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
    wait_until(
        lambda: obs.observation_id in {r.observation_id for r in store.search_observations(project='mcp-summary')},
        f'{obs.observation_id} to appear in search',
    )

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
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id) is not None,
        f'{obs.observation_id} to be readable from the router',
    )

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
    obs = _mk_obs('entry for last_save_at test', project='mcp-last-save')
    store.put_observation(obs)
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id) is not None,
        f'{obs.observation_id} to be readable from the router',
    )

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
    from kioku_mesh import identity

    identity.reset_caches()
    monkeypatch.setenv('KIOKU_MESH_SESSION_ID', '20260604T000000Z-nudgetst')
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
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id) is not None,
        f'{obs.observation_id} to be readable from the router',
    )

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
    from kioku_mesh import identity

    identity.reset_caches()
    # Session id timestamp prefix maps to 2024 → session_age is enormous,
    # well past the 10-minute no-saves threshold.
    monkeypatch.setenv('KIOKU_MESH_SESSION_ID', '20240101T000000Z-emptysess')
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
    from kioku_mesh import identity

    identity.reset_caches()
    monkeypatch.setenv('KIOKU_MESH_SESSION_ID', 'custom-handle-no-timestamp')
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


def test_search_memory_with_search_mode_or(single_zenohd: Any) -> None:  # noqa: ARG001
    """search_memory accepts search_mode='or' and returns a valid result."""
    obs = _mk_obs('modesmoke alpha observation', project='mcp-mode-smoke')
    store.put_observation(obs)
    wait_until(
        lambda: obs.observation_id in {r.observation_id for r in store.search_observations(project='mcp-mode-smoke')},
        f'{obs.observation_id} to appear in search',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'modesmoke', 'project': 'mcp-mode-smoke', 'search_mode': 'or'},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text


def test_search_memory_with_search_mode_and_or(single_zenohd: Any) -> None:  # noqa: ARG001
    """search_memory accepts search_mode='and_or' and returns a valid result."""
    obs = _mk_obs('andorsmoke content', project='mcp-andor-smoke')
    store.put_observation(obs)
    wait_until(
        lambda: obs.observation_id in {r.observation_id for r in store.search_observations(project='mcp-andor-smoke')},
        f'{obs.observation_id} to appear in search',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'andorsmoke', 'project': 'mcp-andor-smoke', 'search_mode': 'and_or'},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text


def test_search_memory_default_and_falls_back_to_or_when_empty(single_zenohd: Any) -> None:  # noqa: ARG001
    """Issue #276: default 'and' search that misses falls back to 'or' and says so."""
    obs = _mk_obs('deduplicate search results content', project='mcp-and-or-fallback')
    store.put_observation(obs)
    wait_until(
        lambda: obs.observation_id
        in {r.observation_id for r in store.search_observations(project='mcp-and-or-fallback')},
        f'{obs.observation_id} to appear in search',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                # 'FTS5' never appears in the saved content, so AND (all terms
                # required) misses while OR (any term) still finds 'deduplicate'.
                {'query': 'deduplicate FTS5', 'project': 'mcp-and-or-fallback'},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert '(no AND match; fell back to OR)' in text
    assert obs.observation_id in text


def test_search_memory_and_hit_has_no_fallback_marker(single_zenohd: Any) -> None:  # noqa: ARG001
    """When AND already hits, no fallback marker is added (no false-positive fallback)."""
    obs = _mk_obs('deduplicate search results FTS5 content', project='mcp-and-no-fallback')
    store.put_observation(obs)
    wait_until(
        lambda: obs.observation_id
        in {r.observation_id for r in store.search_observations(project='mcp-and-no-fallback')},
        f'{obs.observation_id} to appear in search',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'deduplicate FTS5', 'project': 'mcp-and-no-fallback'},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert '(no AND match; fell back to OR)' not in text
    assert obs.observation_id in text


def test_search_memory_and_or_both_empty_reports_none_without_marker(single_zenohd: Any) -> None:  # noqa: ARG001
    """When both AND and OR miss, the plain 'no matches' message is returned unmarked."""

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'nonexistent-term-xyz', 'project': 'mcp-and-or-both-empty'},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert text == 'No matching memories.'
    assert '(no AND match; fell back to OR)' not in text


def test_search_memory_unknown_search_mode_returns_error(single_zenohd: Any) -> None:  # noqa: ARG001
    """search_memory with an unknown search_mode returns a user-visible error string."""

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory',
                {'query': 'anything', 'search_mode': 'fuzzy'},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'search_mode' in text.lower()


# ---------------------------------------------------------------------------
# Issue #277: search_memory output byte cap + truncated display
# ---------------------------------------------------------------------------


def test_cap_search_output_under_limit_is_unchanged() -> None:
    entries = ['first entry', 'second entry']
    expected = '\n---\n'.join(entries)
    result = mcp_server_module._cap_search_output(entries, max_bytes=10_000)
    assert result == expected
    assert 'truncated' not in result


def test_cap_search_output_exact_boundary_is_unchanged() -> None:
    entries = ['abc', 'defgh']
    joined = '\n---\n'.join(entries)
    exact_cap = len(joined.encode('utf-8'))
    result = mcp_server_module._cap_search_output(entries, max_bytes=exact_cap)
    assert result == joined
    assert 'truncated' not in result


def test_cap_search_output_one_byte_over_boundary_truncates() -> None:
    entries = ['a' * 100, 'b' * 100]
    joined = '\n---\n'.join(entries)
    exact_cap = len(joined.encode('utf-8'))
    result = mcp_server_module._cap_search_output(entries, max_bytes=exact_cap - 1)
    assert result != joined
    assert 'truncated: showing 1 of 2 result(s)' in result
    assert result.startswith('a' * 100)
    assert 'b' * 100 not in result
    assert len(result.encode('utf-8')) <= exact_cap - 1


def test_cap_search_output_multibyte_cut_does_not_corrupt_bytes() -> None:
    # 'あ' is 3 UTF-8 bytes, so any budget that is not a multiple of 3 lands
    # mid-character. The cut must never emit a partial code point.
    entry = 'あ' * 50
    result = mcp_server_module._cap_search_output([entry], max_bytes=100)
    shown = result.split('\n[truncated')[0]
    assert set(shown) == {'あ'}
    assert shown.encode('utf-8').decode('utf-8') == shown
    assert 'truncated: showing 1 of 1 result(s)' in result
    assert len(result.encode('utf-8')) <= 100


def test_cap_search_output_single_entry_over_cap_shows_partial() -> None:
    entry = 'x' * 500
    result = mcp_server_module._cap_search_output([entry], max_bytes=100)
    shown = result.split('\n[truncated')[0]
    assert shown
    assert set(shown) == {'x'}
    assert 'truncated: showing 1 of 1 result(s)' in result
    assert len(result.encode('utf-8')) <= 100


def test_cap_search_output_prefix_marker_survives_and_counts_toward_budget() -> None:
    marker = '(no AND match; fell back to OR)'
    entries = ['first entry here'.ljust(100), 'second entry here'.ljust(100), 'third entry here'.ljust(100)]
    joined = '\n---\n'.join(entries)
    full_with_marker = f'{marker}\n{joined}'
    exact_cap = len(full_with_marker.encode('utf-8'))

    # Exactly enough budget for marker + all entries: unchanged, no truncation.
    unchanged = mcp_server_module._cap_search_output(entries, max_bytes=exact_cap, prefix=marker)
    assert unchanged == full_with_marker
    assert 'truncated' not in unchanged

    # One byte short: marker must still be present and intact; an entry is dropped instead.
    truncated = mcp_server_module._cap_search_output(entries, max_bytes=exact_cap - 1, prefix=marker)
    assert truncated.startswith(marker + '\n')
    assert 'truncated: showing 2 of 3 result(s)' in truncated
    assert 'third entry here' not in truncated


_ENTRY_HEADER = '[pattern][4] 2026-08-11T00:00:00 (kioku-mesh) some subject'


def _production_shaped_entry(observation_id: str, body: str) -> str:
    """Build an entry in the exact shape search_memory formats (header + body + <id=...>)."""
    return f'{_ENTRY_HEADER}\n{body} <id={observation_id}>'


# Review B1: the truncation notice is part of the returned text, so the *final*
# string — prefix, entries, separators and notice — must fit the byte cap.
@pytest.mark.parametrize(
    ('name', 'entries', 'max_bytes', 'prefix'),
    [
        ('ascii_one_byte_over', ['a' * 13], 12, ''),
        ('utf8_mid_character', ['あ' * 50], 10, ''),
        ('production_single_plus_one', ['q' * 20_001], 20_000, ''),
        ('prefix_one_short', ['w' * 60], 90, '(no AND match; fell back to OR)'),
        ('prefix_exact', ['w' * 59], 91, '(no AND match; fell back to OR)'),
        ('notice_larger_than_cap', ['a' * 100], 5, ''),
    ],
)
def test_cap_search_output_final_text_never_exceeds_cap(
    name: str,  # noqa: ARG001 — id only
    entries: list[str],
    max_bytes: int,
    prefix: str,
) -> None:
    result = mcp_server_module._cap_search_output(entries, max_bytes=max_bytes, prefix=prefix)
    assert len(result.encode('utf-8')) <= max_bytes
    # Never emit a partial code point.
    assert result.encode('utf-8').decode('utf-8') == result


def test_cap_search_output_production_cap_with_one_byte_over_entry() -> None:
    """The Issue #277 headline contract: 20,001 bytes in, <= 20,000 bytes out."""
    result = mcp_server_module._cap_search_output(['q' * 20_001], max_bytes=20_000)
    assert len(result.encode('utf-8')) <= 20_000
    assert 'truncated: showing 1 of 1 result(s)' in result


# Review B2: a partially displayed entry must stay actionable — the caller has
# to be able to feed the id straight into get_memory / delete_memory.
def test_cap_search_output_partial_entry_keeps_full_observation_id() -> None:
    observation_id = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'
    entry = _production_shaped_entry(observation_id, 'z' * 30_000)
    result = mcp_server_module._cap_search_output([entry], max_bytes=20_000)
    assert len(result.encode('utf-8')) <= 20_000
    assert f'<id={observation_id}>' in result
    assert result.startswith(_ENTRY_HEADER + '\n')
    assert 'truncated: showing 1 of 1 result(s)' in result


def test_cap_search_output_partial_entry_keeps_id_with_multibyte_body() -> None:
    observation_id = '0f1e2d3c4b5a69788796a5b4c3d2e1f0'
    entry = _production_shaped_entry(observation_id, 'あ' * 9_000)
    result = mcp_server_module._cap_search_output([entry], max_bytes=20_000)
    assert len(result.encode('utf-8')) <= 20_000
    assert f'<id={observation_id}>' in result
    assert result.encode('utf-8').decode('utf-8') == result


# Review B3: when PR #285's fallback marker is passed as ``prefix`` (not as an
# entry), the N/M counts must reflect observations only.
def test_cap_search_output_prefix_marker_is_not_counted_as_a_result() -> None:
    marker = '(no AND match; fell back to OR)'
    entries = [_production_shaped_entry(f'{i:032x}', 'z' * 300) for i in range(4)]
    result = mcp_server_module._cap_search_output(entries, max_bytes=800, prefix=marker)
    assert result.startswith(marker + '\n')
    assert 'of 4 result(s)' in result
    assert 'of 5 result(s)' not in result
    assert len(result.encode('utf-8')) <= 800


def test_search_memory_single_huge_observation_keeps_id_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single oversized observation still returns its full 32-char id, within the cap."""
    from kioku_mesh import backend as backend_module

    obs = Observation(
        content='c' * 100,
        summary='s' * 60_000,
        agent_family='claude',
        client_id='claude-code',
        pc_id='mcp-pc',
        session_id='mcp-sess',
        project='mcp-bytecap-huge',
    )

    class _HugeResultBackend:
        def search_observations(self, **kwargs):  # noqa: ANN202, ARG002
            return [obs]

    monkeypatch.setattr(backend_module, '_backend_cache', _HugeResultBackend())

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('search_memory', {'project': 'mcp-bytecap-huge'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert len(text.encode('utf-8')) <= mcp_server_module.SEARCH_OUTPUT_MAX_BYTES
    assert len(obs.observation_id) == 32
    assert f'<id={obs.observation_id}>' in text
    assert 'truncated: showing 1 of 1 result(s)' in text


def test_search_memory_output_truncated_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_memory truncates and reports it when results exceed the byte cap."""
    from kioku_mesh import backend as backend_module

    big_obs = [
        Observation(
            content='x' * 5_000,
            summary='y' * 5_000,
            agent_family='claude',
            client_id='claude-code',
            pc_id='mcp-pc',
            session_id='mcp-sess',
            project='mcp-bytecap',
        )
        for _ in range(5)
    ]

    class _BigResultsBackend:
        def search_observations(self, **kwargs):  # noqa: ANN202, ARG002
            return big_obs

    monkeypatch.setattr(backend_module, '_backend_cache', _BigResultsBackend())
    monkeypatch.setattr(mcp_server_module, 'SEARCH_OUTPUT_MAX_BYTES', 4_000)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('search_memory', {'project': 'mcp-bytecap'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    # The whole returned text — notice included — must fit the cap, not merely be "smaller".
    assert len(text.encode('utf-8')) <= 4_000
    assert 'truncated: showing' in text
    assert 'result(s); output capped at 4000 bytes' in text


def test_search_memory_fallback_marker_not_counted_in_capped_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration bug (B): the #285 AND->OR fallback marker vs. #277 byte cap.

    The marker must not inflate the byte-cap's ``showing N of M`` total, and
    must survive truncation (never dropped, never overflow the cap) since it
    is passed as ``prefix=`` rather than appended into the result entries.
    """
    from kioku_mesh import backend as backend_module

    big_obs = [
        Observation(
            content='x' * 5_000,
            summary='y' * 5_000,
            agent_family='claude',
            client_id='claude-code',
            pc_id='mcp-pc',
            session_id='mcp-sess',
            project='mcp-fallback-bytecap',
        )
        for _ in range(5)
    ]

    class _FallbackBigResultsBackend:
        def search_observations(self, **kwargs):  # noqa: ANN202, ARG002
            # AND misses (empty); OR retry finds the oversized result set,
            # triggering the #276 fallback marker together with the #277 cap.
            return [] if kwargs.get('search_mode') == 'and' else big_obs

    monkeypatch.setattr(backend_module, '_backend_cache', _FallbackBigResultsBackend())
    monkeypatch.setattr(mcp_server_module, 'SEARCH_OUTPUT_MAX_BYTES', 4_000)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('search_memory', {'project': 'mcp-fallback-bytecap'})
            assert not result.is_error
            return result.data

    text = _run(_go())

    # (a) final output never exceeds the byte cap, marker bytes included.
    assert len(text.encode('utf-8')) <= 4_000
    # (b) the marker itself survives truncation intact.
    assert '(no AND match; fell back to OR)' in text
    # (c) showing N of M counts only real result entries, never the marker.
    match = re.search(r'showing (\d+) of (\d+) result', text)
    assert match is not None, text
    shown, total = int(match.group(1)), int(match.group(2))
    assert total == len(big_obs), f'expected total={len(big_obs)} (entries only), got {total}'
    assert 0 < shown <= total


def test_search_memory_output_under_cap_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_memory returns results verbatim (no truncation notice) when under the byte cap."""
    from kioku_mesh import backend as backend_module

    small_obs = [
        Observation(
            content='small content',
            summary='small summary',
            agent_family='claude',
            client_id='claude-code',
            pc_id='mcp-pc',
            session_id='mcp-sess',
            project='mcp-bytecap-small',
        )
    ]

    class _SmallResultsBackend:
        def search_observations(self, **kwargs):  # noqa: ANN202, ARG002
            return small_obs

    monkeypatch.setattr(backend_module, '_backend_cache', _SmallResultsBackend())

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('search_memory', {'project': 'mcp-bytecap-small'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'truncated' not in text
    assert 'small summary' in text


# ---------------------------------------------------------------------------
# ADR-0028 Phase3: get_memory state field tests
# ---------------------------------------------------------------------------


def test_get_memory_state_field(single_zenohd: Any) -> None:  # noqa: ARG001
    """get_memory response includes a 'state:' line."""
    obs = _mk_obs('state field test', project='mcp-state')
    store.put_observation(obs)
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id) is not None,
        f'{obs.observation_id} to be readable from the router',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'state:' in text


def test_get_memory_state_live(single_zenohd: Any) -> None:  # noqa: ARG001
    """A freshly saved observation returns state: live."""
    obs = _mk_obs('live state test', project='mcp-state-live')
    store.put_observation(obs)
    wait_until(
        lambda: store.find_observation_by_id(obs.observation_id) is not None,
        f'{obs.observation_id} to be readable from the router',
    )

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'state: live' in text


@pytest.mark.skip(
    reason=(
        'Zenoh mode: get_memory uses find_observation_by_id which filters tombstoned rows. '
        'Tombstoned obs return "not found" before inspect_by_id is reached in Zenoh mode. '
        'Local backend tombstoned state is covered in test_get_memory_state_local_tombstoned.'
    )
)
def test_get_memory_state_tombstoned(single_zenohd: Any) -> None:  # noqa: ARG001
    """Zenoh mode tombstoned obs: not retrievable via get_memory (see local backend test for coverage)."""


# ---------------------------------------------------------------------------
# ADR-0028 Phase3 B1 regression: local backend state reporting
# ---------------------------------------------------------------------------


def test_get_memory_state_local_tombstoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """LocalBackend: tombstoned obs must return state: tombstoned, not state: live (B1 guard).

    Regression test for the B1 blocker reported in worker4_review.yaml:
    CLI/MCP called store.get_index() (Zenoh sidecar index) instead of
    backend._idx (LocalBackend index). In local mode the sidecar index is
    empty, so inspect_by_id returned None and state defaulted to 'live'
    even when the local row was tombstoned.
    """
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    from kioku_mesh.backend import get_backend as _get_backend  # noqa: PLC0415

    backend = _get_backend()
    obs = _mk_obs('local tombstone b1 test', project='mcp-local-b1')
    backend.put_observation(obs)
    backend.put_tombstone(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            return result.data

    text = _run(_go())
    assert 'state: tombstoned' in text, f'Expected "state: tombstoned", got: {text!r}'


def test_get_memory_state_local_shadowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """LocalBackend: shadowed obs must return state: shadowed, not state: live."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    from kioku_mesh.backend import get_backend as _get_backend  # noqa: PLC0415

    backend = _get_backend()
    obs = _mk_obs('local shadowed b1 test', project='mcp-local-shadow')
    backend.put_observation(obs)
    # Mark shadowed directly via backend._idx.
    shadowed_at = '2026-06-27T00:00:00.000000Z'
    backend._idx.mark_shadowed_missing(obs.observation_id, shadowed_at)  # noqa: SLF001

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            return result.data

    text = _run(_go())
    assert 'state: shadowed' in text, f'Expected "state: shadowed", got: {text!r}'


# ---------------------------------------------------------------------------
# ADR-0028 Phase4: recall_context MCP tool tests (9 cases)
# ---------------------------------------------------------------------------


def _mk_local_backend(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    """Switch to local backend mode and return the backend instance."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    from kioku_mesh.backend import reset_backend as _reset  # noqa: PLC0415

    _reset()
    from kioku_mesh.backend import get_backend as _get_backend  # noqa: PLC0415

    return _get_backend()


def _mk_obs_full(
    content: str,
    *,
    project: str = 'rc-test',
    memory_type: str = 'note',
    importance: int = 3,
    source_files: list[str] | None = None,
    references: list[str] | None = None,
) -> Observation:
    return Observation(
        content=content,
        agent_family='claude',
        client_id='claude-code',
        pc_id='mcp-pc',
        session_id='mcp-sess',
        project=project,
        memory_type=memory_type,
        importance=importance,
        source_files=source_files or [],
        references=references or [],
    )


# Case 1: tool registered and existing tools unchanged
def test_recall_context_tool_registered_and_signature_additive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recall_context is registered; existing tools are still present and callable."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')

    async def _go() -> tuple[list[str], str, str]:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = [t.name for t in tools]
            # search_memory backward compat call
            r_sm = await client.call_tool('search_memory', {'project': 'no-match-rc-sig'})
            # recall_context no-args call
            r_rc = await client.call_tool('recall_context', {})
            return names, r_sm.data, r_rc.data

    names, sm_out, rc_out = _run(_go())
    assert 'recall_context' in names
    assert 'search_memory' in names
    assert 'get_memory' in names
    assert 'save_observation' in names
    assert 'No matching memories' in sm_out
    # Empty index → no results
    assert 'No matching current context.' in rc_out or 'recall_context:' in rc_out


# Case 2: grouping by project and memory_type
def test_recall_context_groups_project_and_memory_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output is grouped by (project, memory_type) in first-hit order."""
    backend = _mk_local_backend(monkeypatch)
    obs_d = _mk_obs_full('decision content', project='grp-a', memory_type='decision')
    obs_b = _mk_obs_full('bug content', project='grp-a', memory_type='bug')
    obs_d2 = _mk_obs_full('decision in b', project='grp-b', memory_type='decision')
    backend.put_observation(obs_d)
    backend.put_observation(obs_b)
    backend.put_observation(obs_d2)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {'project': 'grp-a'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'recall_context:' in text
    assert 'project=grp-a' in text
    assert 'decision content' in text
    assert 'bug content' in text
    # group headers appear
    assert 'memory_type=decision' in text
    assert 'memory_type=bug' in text
    # full content is in output
    assert obs_d.observation_id in text
    assert obs_b.observation_id in text


# Case 3: memory_types filter (valid + invalid)
def test_recall_context_memory_types_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory_types=['decision'] returns only decisions; invalid type returns error."""
    backend = _mk_local_backend(monkeypatch)
    obs_d = _mk_obs_full('decision only', project='mt-test', memory_type='decision')
    obs_n = _mk_obs_full('note only', project='mt-test', memory_type='note')
    backend.put_observation(obs_d)
    backend.put_observation(obs_n)

    async def _go_valid() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'mt-test', 'memory_types': ['decision']},
            )
            return result.data

    async def _go_invalid() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'mt-test', 'memory_types': ['invalid_type']},
            )
            return result.data

    valid_out = _run(_go_valid())
    assert obs_d.observation_id in valid_out
    assert obs_n.observation_id not in valid_out

    invalid_out = _run(_go_invalid())
    assert 'invalid' in invalid_out.lower() or 'memory_types' in invalid_out


# Issue #278: project alias read-side resolution (mesh-mem -> kioku-mesh).
def test_recall_context_alias_resolves_to_canonical_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recall_context with the legacy alias surfaces observations saved under the canonical name."""
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('recall alias content', project='kioku-mesh')
    backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {'project': 'mesh-mem'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text


def test_recall_context_unknown_project_still_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recall_context stays empty for an unknown project but still expands aliases.

    Same pairing as the search_memory case: the negative half on its own does
    not exercise alias resolution at all.
    """
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('unrelated recall content', project='mesh-mem')
    backend.put_observation(obs)

    async def _go() -> tuple[str, str]:
        async with Client(mcp) as client:
            unknown = await client.call_tool('recall_context', {'project': 'totally-unknown-project'})
            aliased = await client.call_tool('recall_context', {'project': 'kioku-mesh'})
            return unknown.data, aliased.data

    unknown_text, aliased_text = _run(_go())
    assert obs.observation_id not in unknown_text
    assert obs.observation_id in aliased_text


def test_recall_context_canonical_project_finds_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #278 symptom on the recall path: canonical project must surface legacy-era rows."""
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('recall legacy era content', project='mesh-mem')
    backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {'project': 'kioku-mesh'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert obs.observation_id in text
    # Observability: the filters line names what the project filter expanded to.
    assert "project='kioku-mesh' (also matching '/home/gisen/work/mesh-mem', 'mesh-mem')" in text


def test_recall_context_canonical_project_merges_legacy_and_canonical_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recall_context returns both eras exactly once each when they coexist."""
    backend = _mk_local_backend(monkeypatch)
    legacy = _mk_obs_full('recall merged legacy', project='mesh-mem')
    canonical = _mk_obs_full('recall merged canonical', project='kioku-mesh')
    other = _mk_obs_full('recall merged elsewhere', project='some-other-project')
    for obs in (legacy, canonical, other):
        backend.put_observation(obs)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {'project': 'kioku-mesh'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert text.count(legacy.observation_id) == 1
    assert text.count(canonical.observation_id) == 1
    assert other.observation_id not in text


# Case 4: source_files exact-match filter
def test_recall_context_source_files_filter_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source_files=['src/a.py'] returns only obs whose source_files include 'src/a.py'."""
    backend = _mk_local_backend(monkeypatch)
    obs_a = _mk_obs_full('has src/a.py', project='sf-test', source_files=['src/a.py'])
    obs_b = _mk_obs_full('has src/b.py', project='sf-test', source_files=['src/b.py'])
    backend.put_observation(obs_a)
    backend.put_observation(obs_b)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'sf-test', 'source_files': ['src/a.py']},
            )
            return result.data

    text = _run(_go())
    assert obs_a.observation_id in text
    assert obs_b.observation_id not in text


# Case 5: references exact-match filter
def test_recall_context_references_filter_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """references=['#2'] returns only obs whose references include '#2'."""
    backend = _mk_local_backend(monkeypatch)
    obs1 = _mk_obs_full('ref #1', project='ref-test', references=['#1'])
    obs2 = _mk_obs_full('ref #2', project='ref-test', references=['#2'])
    backend.put_observation(obs1)
    backend.put_observation(obs2)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'ref-test', 'references': ['#2']},
            )
            return result.data

    text = _run(_go())
    assert obs2.observation_id in text
    assert obs1.observation_id not in text


# Case 6: hidden states excluded by default
def test_recall_context_hidden_states_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tombstoned and shadowed observations are excluded by default."""
    backend = _mk_local_backend(monkeypatch)
    obs_live = _mk_obs_full('live obs', project='hidden-test')
    obs_tomb = _mk_obs_full('tombstoned obs', project='hidden-test')
    obs_shadow = _mk_obs_full('shadowed obs', project='hidden-test')
    obs_super_old = _mk_obs_full('superseded old', project='hidden-test')
    obs_super_new = _mk_obs_full('supersedes old', project='hidden-test')
    backend.put_observation(obs_live)
    backend.put_observation(obs_tomb)
    backend.put_observation(obs_shadow)
    backend.put_observation(obs_super_old)
    # supersede chain
    obs_super_new.supersedes = [obs_super_old.observation_id]
    backend.put_observation(obs_super_new)
    # tombstone obs_tomb
    backend.put_tombstone(obs_tomb)
    # shadow obs_shadow directly via _idx
    backend._idx.mark_shadowed_missing(obs_shadow.observation_id, '2026-01-01T00:00:00.000000Z')  # noqa: SLF001

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'hidden-test'},
            )
            return result.data

    text = _run(_go())
    assert obs_live.observation_id in text
    assert obs_tomb.observation_id not in text
    assert obs_shadow.observation_id not in text
    # superseded old is hidden as a top-level entry; superseder is visible.
    # obs_super_old.observation_id may appear in the supersedes: field of the
    # superseder entry but must NOT appear as its own id: line.
    assert f'id: {obs_super_old.observation_id}' not in text
    assert obs_super_new.observation_id in text


# Case 7: importance ordering preserved
def test_recall_context_importance_ordering_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty query orders higher importance first; empty query stays chronological."""
    backend = _mk_local_backend(monkeypatch)
    obs_low = _mk_obs_full('ordering keyword', project='ord-test', importance=1)
    obs_high = _mk_obs_full('ordering keyword', project='ord-test', importance=5)
    backend.put_observation(obs_low)
    backend.put_observation(obs_high)

    async def _go_query() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'ord-test', 'query': 'ordering'},
            )
            return result.data

    text = _run(_go_query())
    pos_high = text.find(obs_high.observation_id)
    pos_low = text.find(obs_low.observation_id)
    assert pos_high != -1
    assert pos_low != -1
    assert pos_high < pos_low, 'higher importance should appear first when query is non-empty'


# Case 8: limit clamp (1000→100, 0→1)
def test_recall_context_limit_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limit=1000 returns at most 100; limit=0 returns at least 1."""
    backend = _mk_local_backend(monkeypatch)
    for i in range(5):
        backend.put_observation(_mk_obs_full(f'clamp obs {i}', project='clamp-test'))

    async def _go_large() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'clamp-test', 'limit': 1000},
            )
            return result.data

    async def _go_zero() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'clamp-test', 'limit': 0},
            )
            return result.data

    # Should not error and return up to 100 results (we have 5)
    text_large = _run(_go_large())
    assert 'recall_context:' in text_large

    # limit=0 clamped to 1, should return exactly 1 result
    text_zero = _run(_go_zero())
    assert 'recall_context: 1 result(s)' in text_zero


# Case 9: index disabled message
def test_recall_context_index_disabled_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the local index is disabled, recall_context returns a clear error, not a Zenoh scan."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    monkeypatch.setenv('KIOKU_MESH_DISABLE_INDEX', '1')
    from kioku_mesh.backend import reset_backend as _reset  # noqa: PLC0415

    _reset()

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {})
            return result.data

    text = _run(_go())
    assert 'recall_context requires the local index' in text
    assert 'KIOKU_MESH_DISABLE_INDEX' in text


# Issue #356/A1: recall_context byte cap (reuses search_memory's _cap_search_output).
def test_recall_context_output_capped_at_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large set of big observations never exceeds RECALL_OUTPUT_MAX_BYTES."""
    backend = _mk_local_backend(monkeypatch)
    monkeypatch.setattr(mcp_server_module, 'RECALL_OUTPUT_MAX_BYTES', 4_000)
    for i in range(10):
        backend.put_observation(_mk_obs_full('x' * 2_000, project='recall-bytecap', memory_type='note'))

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'recall-bytecap', 'limit': 10},
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert len(text.encode('utf-8')) <= 4_000
    assert 'truncated: showing' in text


def test_recall_context_truncation_notice_visible_to_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When truncated, the caller sees a 'showing N of M' notice, not silently-dropped data."""
    backend = _mk_local_backend(monkeypatch)
    monkeypatch.setattr(mcp_server_module, 'RECALL_OUTPUT_MAX_BYTES', 3_000)
    for i in range(8):
        backend.put_observation(_mk_obs_full('y' * 1_500, project='recall-notice', memory_type='note'))

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'recall-notice', 'limit': 8},
            )
            return result.data

    text = _run(_go())
    assert 'recall_context: 8 result(s)' in text  # total reflects the real hit count
    assert 'truncated: showing' in text
    assert 'result(s); output capped at 3000 bytes' in text


def test_recall_context_truncation_drops_whole_entries_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With multiple observations, truncation falls on boundaries: a shown entry is never cut mid-way.

    (This guarantee holds when more than one observation is in play, so a
    whole one can be dropped from the tail. See
    test_recall_context_single_oversized_observation_returns_partial for the
    single-observation exception, PR #308 review B1.)
    """
    backend = _mk_local_backend(monkeypatch)
    monkeypatch.setattr(mcp_server_module, 'RECALL_OUTPUT_MAX_BYTES', 3_000)
    n = 6
    for i in range(n):
        content = f'STARTMARK{i}_' + 'z' * 780 + f'_ENDMARK{i}'
        backend.put_observation(_mk_obs_full(content, project='recall-boundary', memory_type='note'))

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'recall-boundary', 'limit': n},
            )
            return result.data

    text = _run(_go())
    assert 'truncated: showing' in text
    # Every entry is either wholly present (both its start and end marker show up)
    # or wholly dropped (neither does) — a mismatch means a half-written entry
    # slipped through the tail-truncation boundary.
    presence = [(f'STARTMARK{i}_' in text, f'_ENDMARK{i}' in text) for i in range(n)]
    for start_present, end_present in presence:
        assert start_present == end_present
    assert any(start_present for start_present, _ in presence)  # at least one full entry survived
    assert not all(start_present for start_present, _ in presence)  # and at least one was dropped


def test_recall_context_single_oversized_observation_returns_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single observation that alone exceeds the budget is shown truncated, not dropped to zero results.

    PR #308 review B1: recall entries lack search_memory's ``<id=...>`` suffix,
    so ``_shrink_entry`` can't preserve entry identity the way it does for
    search_memory — it falls back to a plain byte cut of the one entry. This
    is a deliberate exception to the whole-observation-boundary rule: a
    partial result is more useful than an empty one, and the truncated notice
    still tells the caller data was cut.
    """
    backend = _mk_local_backend(monkeypatch)
    monkeypatch.setattr(mcp_server_module, 'RECALL_OUTPUT_MAX_BYTES', 3_000)
    head_marker = 'CONTENT_HEAD_MARKER'
    backend.put_observation(_mk_obs_full(head_marker + 'w' * 30_000, project='recall-oversized', memory_type='note'))

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'recall-oversized', 'limit': 1},
            )
            return result.data

    text = _run(_go())
    assert 'No matching current context.' not in text
    assert 'recall_context: 1 result(s)' in text  # not dropped to zero results
    # The observation's own content — not just some incidental character that
    # happens to also appear in the truncation notice's wording — survived the cut.
    assert head_marker in text
    assert 'truncated: showing' in text
    assert len(text.encode('utf-8')) <= 3_000


def test_recall_context_single_oversized_observation_utf8_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial cut of a single oversized observation never splits a multibyte character."""
    backend = _mk_local_backend(monkeypatch)
    monkeypatch.setattr(mcp_server_module, 'RECALL_OUTPUT_MAX_BYTES', 3_000)
    backend.put_observation(_mk_obs_full('あ' * 30_000, project='recall-oversized-mb', memory_type='note'))

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'recall_context',
                {'project': 'recall-oversized-mb', 'limit': 1},
            )
            return result.data

    text = _run(_go())
    encoded = text.encode('utf-8')  # must not raise UnicodeEncodeError on a split code point
    assert '�' not in text  # no replacement character from a mis-cut code point
    assert encoded.decode('utf-8') == text  # round-trip is lossless
    assert 'truncated: showing' in text

    # Directly pin the boundary-cut contract for both a 3-byte (Japanese) and
    # a 4-byte (surrogate-pair emoji) code point, at a byte offset that lands
    # squarely inside the character — not just "whatever offset the full
    # recall_context pipeline happens to produce".
    ja_text = 'あ' * 10  # each 'あ' is 3 UTF-8 bytes
    ja_cut = mcp_server_module._cut_utf8(ja_text, 3 * 3 + 1)  # 1 byte into the 4th char
    assert '�' not in ja_cut
    assert ja_cut.encode('utf-8').decode('utf-8') == ja_cut
    assert ja_cut == 'あ' * 3  # the partial 4th character is dropped whole, not mangled

    emoji_text = '\U0001f600' * 10  # each emoji is 4 UTF-8 bytes, a surrogate pair in UTF-16
    emoji_cut = mcp_server_module._cut_utf8(emoji_text, 4 * 3 + 2)  # 2 bytes into the 4th char
    assert '�' not in emoji_cut
    assert emoji_cut.encode('utf-8').decode('utf-8') == emoji_cut
    assert emoji_cut == '\U0001f600' * 3


def test_recall_context_under_cap_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal-sized results are byte-identical to the pre-cap grouped markdown (no truncation)."""
    backend = _mk_local_backend(monkeypatch)
    obs_d = _mk_obs_full('small decision content', project='recall-nocap', memory_type='decision')
    obs_b = _mk_obs_full('small bug content', project='recall-nocap', memory_type='bug')
    backend.put_observation(obs_d)
    backend.put_observation(obs_b)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {'project': 'recall-nocap'})
            return result.data

    text = _run(_go())
    assert 'truncated' not in text
    assert 'recall_context: 2 result(s)' in text
    assert 'memory_type=decision' in text
    assert 'memory_type=bug' in text
    assert obs_d.observation_id in text
    assert obs_b.observation_id in text
    assert 'small decision content' in text
    assert 'small bug content' in text


def test_format_recall_entries_groups_non_contiguous_hits() -> None:
    """A group heading appears exactly once even when hits alternate between groups.

    ``idx.search`` does not guarantee hits are sorted by (project, memory_type),
    so two hits from the same group can be non-adjacent in ``hits``. The old
    dict-based grouping collected all hits per key before rendering; a naive
    prev-key comparison would instead emit the same heading twice.
    """
    obs_d1 = _mk_obs_full('decision one', project='p', memory_type='decision')
    obs_b1 = _mk_obs_full('bug one', project='p', memory_type='bug')
    obs_d2 = _mk_obs_full('decision two', project='p', memory_type='decision')
    hits = [
        {'obs': obs_d1, 'state': 'live'},
        {'obs': obs_b1, 'state': 'live'},
        {'obs': obs_d2, 'state': 'live'},
    ]

    entries = mcp_server_module._format_recall_entries(hits)  # noqa: SLF001

    joined = '\n---\n'.join(entries)
    assert joined.count('### project=p / memory_type=decision') == 1
    assert joined.count('### project=p / memory_type=bug') == 1
    assert obs_d1.observation_id in joined
    assert obs_b1.observation_id in joined
    assert obs_d2.observation_id in joined


# ---------------------------------------------------------------------------
# ADR-0028 Phase5: save-lint warn-only guardrails
# ---------------------------------------------------------------------------


def _mock_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch get_backend to avoid real storage; sufficient for lint-only tests."""
    import kioku_mesh.mcp_server as _mcp_mod  # noqa: PLC0415

    class _NoopBackend:
        def put_observation(self, obs: Any) -> None:  # noqa: ANN401
            pass

        def find_supersede_candidates(self, obs: Any) -> list:  # noqa: ANN401
            return []

        def close(self) -> None:
            pass

    noop = _NoopBackend()
    monkeypatch.setattr(_mcp_mod, 'get_backend', lambda: noop)


def test_save_lint_warnings_field_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_observation always returns a 'warnings' key in the JSON response."""
    import json as _json

    _mock_backend(monkeypatch)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'A detailed analysis of the write path with mutex lock added',
                    'memory_type': 'bug',
                    'subject': 'write_path_race',
                    'summary': 'mutex added around the write path',
                },
            )
            return result.data

    msg = _run(_go())
    data = _json.loads(msg)
    assert 'warnings' in data
    assert isinstance(data['warnings'], list)
    assert 'observation_id' in data
    assert data['status'] == 'saved'


def test_save_lint_generic_noise_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic progress content triggers GENERIC_NOISE warning."""
    import json as _json

    _mock_backend(monkeypatch)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {'content': 'tests pass', 'subject': 'generic noise', 'summary': 'bare status tick'},
            )
            return result.data

    msg = _run(_go())
    data = _json.loads(msg)
    codes = [w['code'] for w in data['warnings']]
    assert 'GENERIC_NOISE' in codes


def test_save_lint_secret_pattern_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content with an API key pattern triggers SECRET_PATTERN warning."""
    import json as _json

    _mock_backend(monkeypatch)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'config uses sk-ABCDEFGHIJKLMNOPQRST for auth',  # pragma: allowlist secret
                    'subject': 'secret pattern',
                    'summary': 'config sample embeds an API-key-shaped token',
                },
            )
            return result.data

    msg = _run(_go())
    data = _json.loads(msg)
    codes = [w['code'] for w in data['warnings']]
    assert 'SECRET_PATTERN' in codes


def test_save_rejects_missing_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty subject is rejected outright, not merely lint-warned.

    Supersedes the MISSING_SUBJECT warn-only assertion: the lint rule still
    exists in save_lint.py, but the MCP boundary now rejects such a save
    before linting, so that warning is unreachable from this path. Both the
    omitted argument (schema-level) and the blank one (validation-level) come
    back as tool errors, never as a successful-looking string.
    """
    _mock_backend(monkeypatch)

    async def _go() -> tuple[Any, Any]:
        async with Client(mcp) as client:
            omitted = await client.call_tool(
                'save_observation',
                {'content': 'A non-trivial decision content here', 'memory_type': 'decision', 'subject': ''},
                raise_on_error=False,
            )
            blank = await client.call_tool(
                'save_observation',
                {
                    'content': 'A non-trivial decision content here',
                    'memory_type': 'decision',
                    'subject': '',
                    'summary': '',
                },
                raise_on_error=False,
            )
            return omitted, blank

    omitted, blank = _run(_go())
    assert omitted.is_error is True
    assert 'summary' in ' '.join(getattr(block, 'text', '') for block in omitted.content)
    assert blank.is_error is True
    blank_message = ' '.join(getattr(block, 'text', '') for block in blank.content)
    assert 'subject' in blank_message
    assert 'required' in blank_message


def test_save_lint_no_warning_for_normal_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal, detailed content with proper fields produces no lint warnings."""
    import json as _json

    _mock_backend(monkeypatch)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'Root cause: race condition in flush path when two sessions write simultaneously.',
                    'memory_type': 'bug',
                    'subject': 'flush_race_condition',
                    'summary': 'concurrent flushes raced on the same buffer',
                },
            )
            return result.data

    msg = _run(_go())
    data = _json.loads(msg)
    assert data['warnings'] == []


def test_save_lint_warn_only_save_succeeds(single_zenohd: Any) -> None:  # noqa: ARG001
    """Warnings do not block saves; observation_id is returned and observation is persisted."""
    import json as _json

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'done',
                    'project': 'lint-warn-only',
                    'subject': 'warn only',
                    'summary': 'lint warnings do not block the save',
                },
            )
            assert not result.is_error
            return result.data

    msg = _run(_go())
    data = _json.loads(msg)
    obs_id = data['observation_id']
    assert len(obs_id) == 32
    assert 'GENERIC_NOISE' in [w['code'] for w in data['warnings']]

    found = wait_until(
        lambda: store.find_observation_by_id(obs_id),
        f'{obs_id} to be readable from the router',
    )
    assert found.content == 'done'


# ---------------------------------------------------------------------------
# Cross-PC origin markers (#: host-local details must be attributable)
# ---------------------------------------------------------------------------


def test_instructions_document_cross_pc_origin(single_zenohd: Any) -> None:  # noqa: ARG001
    """Instructions must warn that other-pc entries carry host-local details."""

    async def _go() -> str | None:
        async with Client(mcp) as client:
            init_result = client.initialize_result
            return init_result.instructions if init_result else None

    instructions = _run(_go())
    assert instructions
    assert 'CROSS-PC ORIGIN' in instructions
    assert 'tmux pane' in instructions


def test_get_memory_marks_other_pc_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry saved under a different pc_id is labeled (other pc)."""
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('written on the office machine', project='origin-get')
    backend.put_observation(obs)
    monkeypatch.setattr(mcp_server_module, 'get_pc_id', lambda: 'another-pc')

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'origin: claude-code (other pc)' in text


def test_get_memory_marks_this_pc_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry whose pc_id matches the current host is labeled (this pc)."""
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('written locally', project='origin-get-local')
    backend.put_observation(obs)
    monkeypatch.setattr(mcp_server_module, 'get_pc_id', lambda: 'mcp-pc')

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('get_memory', {'observation_id': obs.observation_id})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'origin: claude-code (this pc)' in text


def test_search_memory_origin_suffix_only_for_other_pc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_memory appends [origin: ...] only when the entry came from another pc."""
    backend = _mk_local_backend(monkeypatch)
    local = _mk_obs_full('local origin needle', project='origin-search')
    remote = Observation(
        content='remote origin needle',
        agent_family='claude',
        client_id='gisen@office',
        pc_id='remote-pc',
        session_id='mcp-sess',
        project='origin-search',
    )
    backend.put_observation(local)
    backend.put_observation(remote)
    monkeypatch.setattr(mcp_server_module, 'get_pc_id', lambda: 'mcp-pc')

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'search_memory', {'query': 'needle', 'project': 'origin-search', 'limit': 20}
            )
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert '[origin: gisen@office, other pc]' in text
    local_block = next(b for b in text.split('---') if local.observation_id in b)
    assert '[origin:' not in local_block


def test_recall_context_shows_origin_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recall_context output carries an origin line for every entry."""
    backend = _mk_local_backend(monkeypatch)
    obs = _mk_obs_full('recalled with origin', project='origin-recall')
    backend.put_observation(obs)
    monkeypatch.setattr(mcp_server_module, 'get_pc_id', lambda: 'another-pc')

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool('recall_context', {'project': 'origin-recall'})
            assert not result.is_error
            return result.data

    text = _run(_go())
    assert 'origin: claude-code (other pc)' in text


# ---------------------------------------------------------------------------
# ADR-0026 supersede suggestion: MCP renderer-error path (#236 TEST-2)
# ---------------------------------------------------------------------------


def test_save_swallows_supersede_renderer_error_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_observation returns saved even when candidate rendering raises, with a debug breadcrumb."""
    import json as _json

    class _BoomCandidate:
        observation_id = 'c' * 32
        summary = 'old summary'
        subject = 'db'

        @property
        def created_at(self) -> str:
            raise RuntimeError('render boom')

    class _Backend:
        def put_observation(self, obs: Any) -> None:  # noqa: ANN401
            pass

        def find_supersede_candidates(self, obs: Any) -> list:  # noqa: ANN401
            return [_BoomCandidate()]

        def close(self) -> None:
            pass

    backend = _Backend()
    monkeypatch.setattr(mcp_server_module, 'get_backend', lambda: backend)

    async def _go() -> str:
        async with Client(mcp) as client:
            result = await client.call_tool(
                'save_observation',
                {
                    'content': 'use PostgreSQL going forward',
                    'project': 'sup-render',
                    'subject': 'db',
                    'summary': 'switching primary datastore to PostgreSQL',
                },
            )
            assert not result.is_error
            return result.data

    debug_calls: list[str] = []
    monkeypatch.setattr(
        mcp_server_module.log,
        'debug',
        lambda msg, *args, **kw: debug_calls.append(msg % args if args else msg),
    )

    msg = _run(_go())

    data = _json.loads(msg)
    assert data['status'] == 'saved'
    assert 'supersede_candidates' not in data
    assert any('supersede suggestion failed' in m for m in debug_calls)

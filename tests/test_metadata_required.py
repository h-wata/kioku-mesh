"""Required subject/summary enforcement and the family-unknown fallback fix."""

from __future__ import annotations

import json

import pytest

from kioku_mesh.core.identity import IdentitySource
from kioku_mesh.core.identity import resolve_agent_family
from kioku_mesh.core.identity import resolve_client_id
from kioku_mesh.memory.metadata import derive_subject
from kioku_mesh.memory.metadata import derive_summary
from kioku_mesh.memory.metadata import is_missing
from kioku_mesh.memory.metadata import MetadataRequiredError
from kioku_mesh.memory.metadata import validate_required_metadata

# -- is_missing / validate_required_metadata ------------------------------------


@pytest.mark.parametrize('value', ['', '   ', '-', '--', '...', 'N/A', 'n/a', 'TBD', 'なし', None])
def test_is_missing_treats_placeholders_as_absent(value: str | None) -> None:
    """'-' was stored in production as a stand-in for a real subject; it must not pass."""
    assert is_missing(value) is True


@pytest.mark.parametrize('value', ['recall latency', '-1 の扱い', 'a'])
def test_is_missing_accepts_real_values(value: str) -> None:
    assert is_missing(value) is False


def test_validate_required_metadata_accepts_both_filled() -> None:
    validate_required_metadata('subject', 'summary')


def test_validate_required_metadata_reports_both_missing_at_once() -> None:
    with pytest.raises(MetadataRequiredError) as excinfo:
        validate_required_metadata('-', '')
    message = str(excinfo.value)
    assert 'subject' in message
    assert 'summary' in message


def test_validate_required_metadata_rejects_missing_summary_only() -> None:
    with pytest.raises(MetadataRequiredError) as excinfo:
        validate_required_metadata('real subject', '   ')
    assert 'summary' in str(excinfo.value)


# -- backfill derivation --------------------------------------------------------


def test_derive_subject_uses_first_non_empty_line() -> None:
    assert derive_subject('\n\n# 見出し\n本文\n') == '見出し'


def test_derive_subject_truncates() -> None:
    assert derive_subject('x' * 200, limit=10) == 'x' * 9 + '…'


def test_derive_summary_uses_first_sentence() -> None:
    assert derive_summary('これは概要です。次の文は含まない。') == 'これは概要です。'


def test_derive_summary_of_empty_content_is_empty() -> None:
    assert derive_summary('   \n  ') == ''


@pytest.mark.parametrize(
    ('content', 'token'),
    [
        # The four shapes measured in production: a backfill dry-run over 269
        # derivable entries cut 21.6% of them inside a dotted token like these.
        ('v0.8.0 リリース後の migration plan を承認した。次の文は含まない。', 'v0.8.0'),
        ('watch.sh が停止していると report が Dispatcher に届かない。次の文は含まない。', 'watch.sh'),
        ('handyscanner(192.168.3.44) の Pico ボタン検証が完了した。次の文は含まない。', '192.168.3.44'),
        ('cube-webapp frontend useMultiMap.ts の runFloorId が自己参照する。次の文は含まない。', 'useMultiMap.ts'),
        # Same defect, other everyday shapes.
        ('kioku_mesh.memory.metadata の derive_summary を直した。次の文は含まない。', 'kioku_mesh.memory.metadata'),
        ('store.state.mapInfo.floorId を obj.method() 経由で読む。次の文は含まない。', 'obj.method()'),
        ('recall の閾値は 0.7、レイテンシは 15.6 ms だった。次の文は含まない。', '15.6 ms'),
    ],
)
def test_derive_summary_does_not_split_inside_dotted_tokens(content: str, token: str) -> None:
    """A period glued to a following non-space character is part of a token, not a sentence end."""
    summary = derive_summary(content)
    assert token in summary
    assert '次の文' not in summary


def test_derive_summary_still_splits_english_sentences() -> None:
    assert derive_summary('This is a sentence. Next one.') == 'This is a sentence.'


def test_derive_summary_splits_after_a_trailing_version_number() -> None:
    """The dotted-token rule must not swallow a real sentence end that follows one."""
    assert derive_summary('Bumped to v0.8.0. Next one.') == 'Bumped to v0.8.0.'


def test_derive_summary_keeps_single_letter_abbreviations() -> None:
    """'e.g.' ends in '. ' but is not a sentence end — splitting there yields a 4-char summary."""
    assert derive_summary('Use e.g. this form. Next one.') == 'Use e.g. this form.'


def test_derive_summary_keeps_japanese_sentence_splitting() -> None:
    assert derive_summary('これは概要です。次の文は含まない。') == 'これは概要です。'


def test_derive_subject_is_unaffected_by_dotted_tokens() -> None:
    """Subject derivation (first line + truncate) was already healthy; pin that it stays."""
    assert derive_subject('v0.8.0 リリース後の migration plan\n本文') == 'v0.8.0 リリース後の migration plan'


# -- agent_family / client_id resolution (family=unknown fix) -------------------


def _clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        'KIOKU_MESH_AGENT_FAMILY',
        'KIOKU_MESH_CLIENT_ID',
        'MESH_MEM_AGENT_FAMILY',
        'MESH_MEM_CLIENT_ID',
        'CLAUDECODE',
        'CLAUDE_CODE_ENTRYPOINT',
        'CODEX_SANDBOX',
        'CODEX_HOME',
        'GEMINI_CLI',
        'GEMINI_SANDBOX',
    ):
        monkeypatch.delenv(name, raising=False)


def test_legacy_mesh_mem_env_is_not_read(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """ADR-0029 removed MESH_MEM_* in v1.0.0 and that removal stands.

    A client config still exporting the old name resolves to 'unknown' (with
    the warning) so the operator repairs the config instead of the old name
    quietly keeping a removed contract alive.
    """
    from kioku_mesh.core import identity

    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('MESH_MEM_AGENT_FAMILY', 'codex')
    identity.reset_caches()
    with caplog.at_level('WARNING'):
        value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT
    assert any('agent_family' in record.getMessage() for record in caplog.records)


def test_current_env_outranks_launcher_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-written value beats a marker that may have leaked from a parent process."""
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('KIOKU_MESH_AGENT_FAMILY', 'claude')
    monkeypatch.setenv('CODEX_SANDBOX', '1')
    value, source = resolve_agent_family()
    assert value == 'claude'
    assert source is IdentitySource.ENV


def test_launcher_marker_detects_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code passes CLAUDECODE=1 to the MCP servers it spawns."""
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('CLAUDECODE', '1')
    value, source = resolve_agent_family()
    assert value == 'claude'
    assert source is IdentitySource.DETECTED


def test_launcher_detection_outranks_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy name carries no weight at all — detection is consulted as if it were unset."""
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('MESH_MEM_AGENT_FAMILY', 'codex')
    monkeypatch.setenv('CLAUDECODE', '1')
    value, source = resolve_agent_family()
    assert value == 'claude'
    assert source is IdentitySource.DETECTED


def test_nested_claude_then_codex_markers_resolve_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex launched from Claude Code inherits CLAUDECODE alongside its own marker.

    Table order used to hand that process to 'claude' with no warning. A wrong
    family is trusted silently, so ambiguity must fall back to 'unknown'.
    """
    from kioku_mesh.core import identity

    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CODEX_SANDBOX', '1')
    identity.reset_caches()
    with caplog.at_level('WARNING'):
        value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT
    assert any('multiple agent families' in record.getMessage() for record in caplog.records)


def test_nested_codex_then_claude_markers_resolve_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reverse nesting (Claude Code launched from Codex) is equally unattributable.

    Same env set as the previous test — the point is that neither direction is
    distinguishable from the markers, so neither may win.
    """
    from kioku_mesh.core import identity

    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('CODEX_SANDBOX', '1')
    monkeypatch.setenv('CLAUDE_CODE_ENTRYPOINT', 'cli')
    identity.reset_caches()
    with caplog.at_level('WARNING'):
        value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT
    assert any('multiple agent families' in record.getMessage() for record in caplog.records)


def test_user_exported_codex_home_does_not_attribute_to_codex(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CODEX_HOME is a user-configurable path, not a launcher-owned marker.

    Codex CLI does not pass it to the MCP servers it spawns, so honouring it
    could only ever mislabel a shell that exports it from a profile.
    """
    from kioku_mesh.core import identity

    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('CODEX_HOME', '/home/someone/.codex')
    identity.reset_caches()
    with caplog.at_level('WARNING'):
        value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT


def test_explicit_family_env_survives_conflicting_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambiguity handling must not weaken the explicit-config precedence."""
    from kioku_mesh.core import identity

    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('KIOKU_MESH_AGENT_FAMILY', 'codex')
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CODEX_SANDBOX', '1')
    identity.reset_caches()
    value, source = resolve_agent_family()
    assert value == 'codex'
    assert source is IdentitySource.ENV


def test_ambiguous_family_warning_is_emitted_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Identity resolves on every Observation construction; the warning must not repeat."""
    from kioku_mesh.core import identity

    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CODEX_SANDBOX', '1')
    identity.reset_caches()
    with caplog.at_level('WARNING'):
        resolve_agent_family()
        resolve_agent_family()
    ambiguous = [r for r in caplog.records if 'multiple agent families' in r.getMessage()]
    assert len(ambiguous) == 1


def test_unresolved_family_warns(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Falling back to 'unknown' means the identity config is broken — say so."""
    _clear_identity_env(monkeypatch)
    from kioku_mesh.core import identity

    identity.reset_caches()
    with caplog.at_level('WARNING'):
        value, source = resolve_agent_family()
    assert value == 'unknown'
    assert source is IdentitySource.DEFAULT
    assert any('agent_family' in record.message for record in caplog.records)


def test_legacy_client_id_env_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same removal as agent_family: the old name falls through to the <user>@<host> default."""
    _clear_identity_env(monkeypatch)
    monkeypatch.setenv('MESH_MEM_CLIENT_ID', 'codex-cli')
    value, source = resolve_client_id()
    assert value != 'codex-cli'
    assert source is IdentitySource.DEFAULT


# -- MCP save_observation entry point ------------------------------------------


def test_mcp_save_observation_rejects_missing_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp.exceptions import ToolError

    from kioku_mesh import mcp_server

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))
    with pytest.raises(ToolError) as excinfo:
        mcp_server.save_observation(content='body text', subject='', summary='')
    assert 'subject' in str(excinfo.value)
    assert 'summary' in str(excinfo.value)
    assert saved == []


def test_mcp_save_observation_rejects_placeholder_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp.exceptions import ToolError

    from kioku_mesh import mcp_server

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))
    with pytest.raises(ToolError) as excinfo:
        mcp_server.save_observation(content='body text', subject='-', summary='-')
    assert 'subject' in str(excinfo.value)
    assert saved == []


def test_mcp_input_schema_marks_subject_and_summary_required() -> None:
    """The published schema is what an MCP client generates arguments from.

    A docstring saying REQUIRED is invisible to it: only ``required`` in the
    tool's inputSchema keeps a client from omitting subject / summary.
    """
    import asyncio

    pytest.importorskip('fastmcp')
    from fastmcp import Client

    from kioku_mesh.mcp_server import mcp

    async def _go() -> list[str]:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool = next(t for t in tools if t.name == 'save_observation')
            return list(tool.inputSchema.get('required', []))

    required = asyncio.run(_go())
    assert set(required) >= {'content', 'subject', 'summary'}


def test_mcp_call_without_metadata_is_a_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal returned as a normal string reads as a successful save on the wire.

    The caller must see ``is_error=true`` so it retries with real metadata
    instead of believing the entry was stored.
    """
    import asyncio

    pytest.importorskip('fastmcp')
    from fastmcp import Client

    from kioku_mesh import mcp_server

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))

    async def _go():  # noqa: ANN202 — fastmcp CallToolResult
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(
                'save_observation',
                {'content': 'body text', 'subject': '', 'summary': ''},
                raise_on_error=False,
            )

    result = asyncio.run(_go())
    assert result.is_error is True
    message = ' '.join(getattr(block, 'text', '') for block in result.content)
    assert 'subject' in message
    assert 'summary' in message
    assert saved == []


def test_mcp_save_observation_accepts_filled_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import mcp_server

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))
    result = mcp_server.save_observation(
        content='body text',
        subject='real subject',
        summary='real summary',
    )
    assert json.loads(result)['status'] == 'saved'
    assert len(saved) == 1


class _RecordingBackend:
    """Minimal backend double capturing puts without touching zenoh / sqlite."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def put_observation(self, obs) -> None:  # noqa: ANN001 — test double
        self._sink.append(obs)

    def find_supersede_candidates(self, obs) -> list:  # noqa: ANN001, ARG002 — test double
        return []


# -- CLI save entry point -------------------------------------------------------


def test_cli_save_rejects_missing_metadata(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from kioku_mesh import __main__ as cli

    saved: list = []
    monkeypatch.setattr(cli, 'get_backend', lambda: _RecordingBackend(saved))
    rc = cli.main(['save', 'body text'])
    assert rc == 2
    assert 'subject' in capsys.readouterr().err
    assert saved == []


def test_backfill_metadata_is_dry_run_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A destructive repair must not write until the caller opts in."""
    from kioku_mesh import __main__ as cli

    backend = _BackfillBackend([_observation(subject='-', summary='', content='壊れた行\n詳細')])
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    rc = cli.main(['backfill-metadata'])
    out = capsys.readouterr().out
    assert rc == 0
    assert backend.written == []
    assert 'dry-run' in out
    assert '壊れた行' in out


def test_backfill_metadata_apply_appends_new_observation_superseding_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0002 / ADR-0028: the original payload is never written back over.

    The repair is a new observation carrying the derived metadata and a
    supersedes link, so the raw layer stays append-only and the historical
    payload is still there to read.
    """
    from kioku_mesh import __main__ as cli

    original = _observation(subject='', summary='', content='見出し行。本文の続き。')
    backend = _BackfillBackend([original])
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    rc = cli.main(['backfill-metadata', '--apply'])
    assert rc == 0
    assert len(backend.written) == 1
    repaired = backend.written[0]
    assert repaired.subject == '見出し行。本文の続き。'
    assert repaired.summary == '見出し行。'
    assert repaired.observation_id != original.observation_id
    assert repaired.supersedes == [original.observation_id]
    # Provenance rides along: a repair must not re-attribute the entry to the
    # host running the command, nor move it to the top of recency ordering.
    assert repaired.content == original.content
    assert repaired.created_at == original.created_at
    assert repaired.agent_family == original.agent_family
    assert repaired.client_id == original.client_id
    assert repaired.pc_id == original.pc_id
    assert repaired.session_id == original.session_id
    assert repaired.visibility == original.visibility


def test_backfill_metadata_partial_failure_keeps_successes_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure mid-batch leaves the appended repairs in place and reports failure."""
    from kioku_mesh import __main__ as cli

    observations = [_observation(subject='', summary='', content=f'行{i}。本文。') for i in range(3)]
    backend = _BackfillBackend(observations, fail_on_index=1)
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    rc = cli.main(['backfill-metadata', '--apply'])
    assert rc == 1
    assert len(backend.written) == 2
    superseded = {old for obs in backend.written for old in obs.supersedes}
    assert superseded == {observations[0].observation_id, observations[2].observation_id}


def test_backfill_metadata_rerun_does_not_supersede_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running after a partial failure retries only what is still unrepaired.

    The store keeps both the original and its superseder, so a naive second
    pass would append a second repair for an already-repaired entry.
    """
    from kioku_mesh import __main__ as cli

    observations = [_observation(subject='', summary='', content=f'行{i}。本文。') for i in range(3)]
    backend = _BackfillBackend(observations, fail_on_index=1)
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    assert cli.main(['backfill-metadata', '--apply']) == 1

    # The superseders are now part of the stored set, as a real backend would
    # return them (unfiltered) on the next scan.
    backend.observations.extend(backend.written)
    first_pass = list(backend.written)
    backend.fail_on_index = None
    backend.written.clear()
    assert cli.main(['backfill-metadata', '--apply']) == 0

    assert len(backend.written) == 1
    assert backend.written[0].supersedes == [observations[1].observation_id]
    already = {old for obs in first_pass for old in obs.supersedes}
    assert not already & set(backend.written[0].supersedes)


def test_backfill_metadata_skips_complete_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import __main__ as cli

    backend = _BackfillBackend([_observation(subject='ok', summary='ok summary', content='body')])
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    assert cli.main(['backfill-metadata', '--apply']) == 0
    assert backend.written == []


def test_backfill_metadata_reports_unknown_family_without_rewriting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """agent_family is part of the key; a payload rewrite must not pretend to fix it."""
    from kioku_mesh import __main__ as cli

    obs = _observation(subject='ok', summary='ok', content='body')
    obs.agent_family = 'unknown'
    backend = _BackfillBackend([obs])
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    cli.main(['backfill-metadata', '--apply'])
    out = capsys.readouterr().out
    assert 'agent_family unknown: 1' in out
    assert backend.written == []


def _observation(*, subject: str, summary: str, content: str):  # noqa: ANN202 — test helper
    from kioku_mesh.models import Observation

    return Observation(content=content, subject=subject, summary=summary, visibility='mesh')


class _BackfillBackend:
    """Backend double returning a fixed observation set and recording appends.

    ``fail_on_index`` injects a failure on the Nth put so partial-batch
    behaviour can be asserted without a live backend.
    """

    def __init__(self, observations: list, fail_on_index: int | None = None) -> None:
        self.observations = observations
        self.fail_on_index = fail_on_index
        self.written: list = []
        self._puts = 0

    def search_observations(self, **kwargs) -> list:  # noqa: ANN003, ARG002 — test double
        return list(self.observations)

    def put_observation(self, obs) -> None:  # noqa: ANN001 — test double
        index = self._puts
        self._puts += 1
        if self.fail_on_index is not None and index == self.fail_on_index:
            raise RuntimeError('injected put failure')
        self.written.append(obs)


def test_cli_save_accepts_filled_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import __main__ as cli

    saved: list = []
    monkeypatch.setattr(cli, 'get_backend', lambda: _RecordingBackend(saved))
    rc = cli.main(['save', 'body text', '--subject', 'real subject', '--summary', 'real summary'])
    assert rc == 0
    assert len(saved) == 1


# -- required metadata x observation expiry (merge of PR #275 and PR #273) -----


def test_mcp_required_metadata_and_expiry_hold_in_one_tool_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both features share one ``save_observation`` schema; pin them together.

    PR #275 made ``subject`` / ``summary`` required, PR #273 added the optional
    ``expires_at`` / ``ttl_sec``. Resolving that merge by hand can silently
    push the expiry arguments into ``required`` (breaking every durable save)
    or drop the expiry normalization (silently storing entries that never
    expire). One FastMCP call exercises both halves so neither regresses
    alone.
    """
    import asyncio

    pytest.importorskip('fastmcp')
    from datetime import datetime
    from datetime import timedelta

    from fastmcp import Client

    from kioku_mesh import mcp_server

    def _parse_ts(value: str) -> datetime:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))

    async def _go():  # noqa: ANN202 — fastmcp CallToolResult tuple
        async with Client(mcp_server.mcp) as client:
            tool = next(t for t in await client.list_tools() if t.name == 'save_observation')
            missing = await client.call_tool(
                'save_observation',
                {'content': 'body text', 'subject': '', 'summary': ''},
                raise_on_error=False,
            )
            explicit = await client.call_tool(
                'save_observation',
                {
                    'content': 'body text',
                    'subject': 'real subject',
                    'summary': 'real summary',
                    'expires_at': '2026-08-08T21:00:00+09:00',
                    'ttl_sec': 999999,
                },
                raise_on_error=False,
            )
            ttl = await client.call_tool(
                'save_observation',
                {
                    'content': 'body text',
                    'subject': 'real subject',
                    'summary': 'real summary',
                    'ttl_sec': 3600,
                },
                raise_on_error=False,
            )
            return tool.inputSchema, missing, explicit, ttl

    schema, missing, explicit, ttl = asyncio.run(_go())

    # The published contract: only the three metadata fields are required, and
    # the expiry arguments stay optional.
    assert set(schema.get('required', [])) == {'content', 'subject', 'summary'}
    assert {'expires_at', 'ttl_sec'} <= set(schema.get('properties', {}))

    # Missing metadata is still a protocol-level error, and nothing is stored.
    assert missing.is_error is True
    assert len(saved) == 2

    # expires_at wins over ttl_sec and is normalized to UTC.
    assert explicit.is_error is False
    assert saved[0].expires_at == '2026-08-08T12:00:00.000000Z'
    assert json.loads(explicit.content[0].text)['expires_at'] == '2026-08-08T12:00:00.000000Z'

    # ttl_sec alone lands roughly ttl seconds ahead: the server resolves the
    # instant just before the observation stamps its own created_at, so the
    # gap is a hair under the full ttl rather than exactly it.
    assert ttl.is_error is False
    assert saved[1].expires_at > saved[1].created_at
    delta = _parse_ts(saved[1].expires_at) - _parse_ts(saved[1].created_at)
    assert timedelta(seconds=3599) <= delta <= timedelta(seconds=3600)

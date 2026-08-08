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
    from kioku_mesh import mcp_server

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))
    result = mcp_server.save_observation(content='body text', subject='', summary='')
    assert 'subject' in result
    assert 'summary' in result
    assert saved == []


def test_mcp_save_observation_rejects_placeholder_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import mcp_server

    saved: list = []
    monkeypatch.setattr(mcp_server, 'get_backend', lambda: _RecordingBackend(saved))
    result = mcp_server.save_observation(content='body text', subject='-', summary='-')
    assert 'subject' in result
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


def test_backfill_metadata_apply_rewrites_derived_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import __main__ as cli

    backend = _BackfillBackend([_observation(subject='', summary='', content='見出し行。本文の続き。')])
    monkeypatch.setattr(cli, 'get_backend', lambda: backend)
    rc = cli.main(['backfill-metadata', '--apply'])
    assert rc == 0
    assert len(backend.written) == 1
    assert backend.written[0].subject == '見出し行。本文の続き。'
    assert backend.written[0].summary == '見出し行。'


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
    """Backend double returning a fixed observation set and recording rewrites."""

    def __init__(self, observations: list) -> None:
        self._observations = observations
        self.written: list = []

    def search_observations(self, **kwargs) -> list:  # noqa: ANN003, ARG002 — test double
        return list(self._observations)

    def put_observation(self, obs) -> None:  # noqa: ANN001 — test double
        self.written.append(obs)


def test_cli_save_accepts_filled_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from kioku_mesh import __main__ as cli

    saved: list = []
    monkeypatch.setattr(cli, 'get_backend', lambda: _RecordingBackend(saved))
    rc = cli.main(['save', 'body text', '--subject', 'real subject', '--summary', 'real summary'])
    assert rc == 0
    assert len(saved) == 1

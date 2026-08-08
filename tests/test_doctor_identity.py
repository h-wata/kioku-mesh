"""Unit tests for :func:`kioku_mesh.doctor.check_identity`.

Every test injects both inputs (config paths built under ``tmp_path`` and an
explicit observation list), so the suite never reads the developer's real
``~/.claude.json`` / ``~/.codex/config.toml`` or the host's memory store —
which on a broken host would otherwise make these tests pass or fail for
reasons that have nothing to do with the code under test.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from kioku_mesh import doctor
from kioku_mesh.doctor import _collect_legacy_env_keys
from kioku_mesh.doctor import _UNKNOWN_DOMINANCE_RATIO
from kioku_mesh.doctor import check_identity
from kioku_mesh.doctor import CheckResult
from kioku_mesh.doctor import CheckStatus


@dataclass
class _FakeObs:
    """Minimal stand-in for Observation: the check only reads agent_family."""

    agent_family: str


def _observations(unknown: int, known: int) -> list[_FakeObs]:
    return [_FakeObs('unknown')] * unknown + [_FakeObs('claude')] * known


def _write_claude_config(path: Path, env: dict[str, str]) -> Path:
    """Write a ~/.claude.json-shaped file with ``env`` under an MCP server."""
    path.write_text(
        json.dumps({'mcpServers': {'kioku_mesh': {'command': 'kioku-mesh-mcp', 'env': env}}}),
        encoding='utf-8',
    )
    return path


def _write_codex_config(path: Path, env: dict[str, str]) -> Path:
    """Write a ~/.codex/config.toml-shaped file with ``env`` under an MCP server."""
    lines = ['[mcp_servers.kioku_mesh]', 'command = "kioku-mesh-mcp"', '', '[mcp_servers.kioku_mesh.env]']
    lines += [f'{k} = "{v}"' for k, v in env.items()]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


# -- Deprecated MESH_MEM_* prefix (WARN) ---------------------------------------


def test_legacy_prefix_in_claude_config_warns(tmp_path: Path) -> None:
    cfg = _write_claude_config(
        tmp_path / '.claude.json',
        {'MESH_MEM_AGENT_FAMILY': 'claude', 'MESH_MEM_CLIENT_ID': 'claude-code'},
    )
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.WARN
    assert result.name == 'identity'
    assert 'MESH_MEM_AGENT_FAMILY' in result.summary
    assert result.details['legacy_hits'][0]['keys'] == ['MESH_MEM_AGENT_FAMILY', 'MESH_MEM_CLIENT_ID']


def test_legacy_prefix_never_fails(tmp_path: Path) -> None:
    """#275 reinstates the prefix as deprecated-but-working: FAIL would be wrong."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_AGENT_FAMILY': 'claude'})
    result = check_identity([cfg], observations=_observations(unknown=50, known=0))
    assert result.status is not CheckStatus.FAIL


def test_legacy_prefix_in_codex_toml_warns(tmp_path: Path) -> None:
    cfg = _write_codex_config(tmp_path / 'config.toml', {'MESH_MEM_AGENT_FAMILY': 'codex'})
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.WARN
    assert str(cfg) in result.summary


def test_legacy_prefix_hint_names_the_current_env_vars(tmp_path: Path) -> None:
    """The hint has to be actionable without opening the docs."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_CLIENT_ID': 'claude-code'})
    result = check_identity([cfg], observations=[])
    assert 'KIOKU_MESH_AGENT_FAMILY' in result.hint
    assert 'KIOKU_MESH_CLIENT_ID' in result.hint


def test_legacy_prefix_outranks_unknown_dominance_in_the_summary(tmp_path: Path) -> None:
    """Both WARN: the headline goes to the one naming the file to edit."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_AGENT_FAMILY': 'claude'})
    result = check_identity([cfg], observations=_observations(unknown=50, known=0))
    assert result.status is CheckStatus.WARN
    assert 'MESH_MEM_AGENT_FAMILY' in result.summary
    # The unknown-dominance finding is still reported, just not in the headline.
    assert result.details['unknown_ratio'] == 1.0


def test_both_prefixes_present_is_not_a_finding(tmp_path: Path) -> None:
    """KIOKU_MESH_* wins with or without #275's fallback, so the old key is inert."""
    cfg = _write_claude_config(
        tmp_path / '.claude.json',
        {
            'KIOKU_MESH_AGENT_FAMILY': 'claude',
            'MESH_MEM_AGENT_FAMILY': 'claude',
            'KIOKU_MESH_CLIENT_ID': 'claude-code',
            'MESH_MEM_CLIENT_ID': 'claude-code',
        },
    )
    result = check_identity([cfg], observations=_observations(unknown=0, known=10))
    assert result.status is CheckStatus.PASS
    assert result.details['legacy_hits'] == []


def test_partial_migration_reports_only_the_unmigrated_key(tmp_path: Path) -> None:
    cfg = _write_claude_config(
        tmp_path / '.claude.json',
        {'KIOKU_MESH_AGENT_FAMILY': 'claude', 'MESH_MEM_AGENT_FAMILY': 'claude', 'MESH_MEM_CLIENT_ID': 'claude-code'},
    )
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.WARN
    assert result.details['legacy_hits'][0]['keys'] == ['MESH_MEM_CLIENT_ID']


def test_current_prefix_only_passes(tmp_path: Path) -> None:
    cfg = _write_claude_config(
        tmp_path / '.claude.json',
        {'KIOKU_MESH_AGENT_FAMILY': 'claude', 'KIOKU_MESH_CLIENT_ID': 'claude-code'},
    )
    result = check_identity([cfg], observations=_observations(unknown=0, known=10))
    assert result.status is CheckStatus.PASS
    assert result.details['legacy_hits'] == []
    assert result.details['inspected_configs'] == [str(cfg)]


def test_current_prefix_is_not_matched_as_legacy() -> None:
    """KIOKU_MESH_* must not be mistaken for MESH_MEM_* by a sloppy match."""
    assert _collect_legacy_env_keys({'env': {'KIOKU_MESH_AGENT_FAMILY': 'claude'}}) == []


def test_counterpart_must_be_in_the_same_mapping() -> None:
    """A KIOKU_MESH_* key in an unrelated server block doesn't cover this one."""
    data = {
        'mcpServers': {
            'a': {'env': {'MESH_MEM_AGENT_FAMILY': 'claude'}},
            'b': {'env': {'KIOKU_MESH_AGENT_FAMILY': 'codex'}},
        }
    }
    assert _collect_legacy_env_keys(data) == ['MESH_MEM_AGENT_FAMILY']


def test_legacy_key_is_found_at_any_nesting_depth() -> None:
    data = {'a': [{'b': {'env': {'MESH_MEM_STATE_DIR': '/tmp/x'}}}]}
    assert _collect_legacy_env_keys(data) == ['MESH_MEM_STATE_DIR']


def test_legacy_name_in_a_value_is_not_a_hit(tmp_path: Path) -> None:
    """Only keys count — a path or prose that mentions the old name is fine."""
    cfg = _write_claude_config(
        tmp_path / '.claude.json',
        {'KIOKU_MESH_STATE_DIR': '/home/u/.local/share/MESH_MEM_backup'},
    )
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.PASS


# -- Missing / malformed configs (skip, never FAIL) ----------------------------


def test_missing_config_file_passes(tmp_path: Path) -> None:
    result = check_identity([tmp_path / 'absent.json'], observations=[])
    assert result.status is CheckStatus.PASS
    assert result.details['inspected_configs'] == []
    assert 'no MCP client config found' in result.summary


def test_malformed_json_is_skipped_not_failed(tmp_path: Path) -> None:
    cfg = tmp_path / '.claude.json'
    cfg.write_text('{ this is not json', encoding='utf-8')
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.PASS
    # Unparseable files are not counted as inspected: the check must not
    # claim coverage of a file it could not read.
    assert result.details['inspected_configs'] == []


def test_malformed_toml_is_skipped_not_failed(tmp_path: Path) -> None:
    cfg = tmp_path / 'config.toml'
    cfg.write_text('[mcp_servers.kioku_mesh\ncommand = ', encoding='utf-8')
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.PASS
    assert result.details['inspected_configs'] == []


def test_check_never_writes_to_the_config(tmp_path: Path) -> None:
    """Doctor diagnoses; it does not edit the user's editor-managed config."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_AGENT_FAMILY': 'claude'})
    before = cfg.read_bytes()
    mtime_before = cfg.stat().st_mtime_ns
    check_identity([cfg], observations=[])
    assert cfg.read_bytes() == before
    assert cfg.stat().st_mtime_ns == mtime_before
    assert list(tmp_path.iterdir()) == [cfg]


# -- Unknown dominance (WARN) --------------------------------------------------


def test_unknown_dominance_warns(tmp_path: Path) -> None:
    result = check_identity([tmp_path / 'absent.json'], observations=_observations(unknown=50, known=0))
    assert result.status is CheckStatus.WARN
    assert '100%' in result.summary
    assert result.details['unknown_ratio'] == 1.0


def test_unknown_minority_does_not_warn(tmp_path: Path) -> None:
    result = check_identity([tmp_path / 'absent.json'], observations=_observations(unknown=10, known=40))
    assert result.status is CheckStatus.PASS
    assert result.details['unknown_ratio'] == 0.2


def test_unknown_ratio_exactly_at_threshold_warns(tmp_path: Path) -> None:
    """The threshold is inclusive (>=), matching the docstring."""
    unknown = int(_UNKNOWN_DOMINANCE_RATIO * 100)
    result = check_identity(
        [tmp_path / 'absent.json'],
        observations=_observations(unknown=unknown, known=100 - unknown),
    )
    assert result.status is CheckStatus.WARN


def test_unknown_ratio_just_below_threshold_passes(tmp_path: Path) -> None:
    unknown = int(_UNKNOWN_DOMINANCE_RATIO * 100) - 1
    result = check_identity(
        [tmp_path / 'absent.json'],
        observations=_observations(unknown=unknown, known=100 - unknown),
    )
    assert result.status is CheckStatus.PASS


def test_empty_agent_family_counts_as_unknown(tmp_path: Path) -> None:
    result = check_identity([tmp_path / 'absent.json'], observations=[_FakeObs('')] * 10)
    assert result.status is CheckStatus.WARN


def test_no_observations_does_not_warn(tmp_path: Path) -> None:
    """A fresh install with an empty store is not a broken identity."""
    result = check_identity([tmp_path / 'absent.json'], observations=[])
    assert result.status is CheckStatus.PASS
    assert result.details['unknown_ratio'] is None
    assert result.details['sampled_observations'] == 0


def test_backend_failure_is_skipped_not_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable backend is check_zenohd_reachable's problem, not this one."""
    import kioku_mesh.memory.backend as backend_mod

    def boom() -> object:
        raise RuntimeError('backend down')

    monkeypatch.setattr(backend_mod, 'get_backend', boom)
    result = check_identity([tmp_path / 'absent.json'])
    assert result.status is CheckStatus.PASS
    assert result.details['unknown_ratio'] is None


# -- Wiring --------------------------------------------------------------------


def test_run_all_checks_includes_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        'check_identity',
        lambda: CheckResult(name='identity', status=CheckStatus.PASS, summary='stub'),
    )
    names = [r.name for r in doctor.run_all_checks()]
    assert 'identity' in names


def test_default_config_paths_cover_both_clients() -> None:
    paths = [p.name for p in doctor._default_identity_config_paths()]
    assert '.claude.json' in paths
    assert 'config.toml' in paths


def test_identity_warn_maps_to_warning_exit() -> None:
    """A deprecated prefix shows up in the exit code, but does not fail the run."""
    warned = CheckResult(name='identity', status=CheckStatus.WARN, summary='stub')
    assert doctor.exit_code_for(doctor.worst_status([warned])) == 1

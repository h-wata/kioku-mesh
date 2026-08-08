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


# -- Retired MESH_MEM_* identity keys (FAIL) -----------------------------------


def test_legacy_prefix_in_claude_config_fails(tmp_path: Path) -> None:
    cfg = _write_claude_config(
        tmp_path / '.claude.json',
        {'MESH_MEM_AGENT_FAMILY': 'claude', 'MESH_MEM_CLIENT_ID': 'claude-code'},
    )
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.FAIL
    assert result.name == 'identity'
    assert 'MESH_MEM_AGENT_FAMILY' in result.summary
    assert result.details['legacy_hits'][0]['keys'] == ['MESH_MEM_AGENT_FAMILY', 'MESH_MEM_CLIENT_ID']


def test_legacy_identity_key_fails_because_nothing_reads_it(tmp_path: Path) -> None:
    """Resolution is KIOKU_MESH_* -> launcher -> unknown: the old key is inert."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_AGENT_FAMILY': 'claude'})
    result = check_identity([cfg], observations=_observations(unknown=50, known=0))
    assert result.status is CheckStatus.FAIL


def test_legacy_prefix_in_codex_toml_fails(tmp_path: Path) -> None:
    cfg = _write_codex_config(tmp_path / 'config.toml', {'MESH_MEM_AGENT_FAMILY': 'codex'})
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.FAIL
    assert str(cfg) in result.summary


def test_legacy_prefix_hint_names_the_current_env_vars(tmp_path: Path) -> None:
    """The hint has to be actionable without opening the docs."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_CLIENT_ID': 'claude-code'})
    result = check_identity([cfg], observations=[])
    assert 'KIOKU_MESH_AGENT_FAMILY' in result.hint
    assert 'KIOKU_MESH_CLIENT_ID' in result.hint


def test_legacy_prefix_outranks_unknown_dominance_in_the_summary(tmp_path: Path) -> None:
    """The retired key is the more severe finding and names the file to edit."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_AGENT_FAMILY': 'claude'})
    result = check_identity([cfg], observations=_observations(unknown=50, known=0))
    assert result.status is CheckStatus.FAIL
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
    assert result.status is CheckStatus.FAIL
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
    data = {'a': [{'b': {'env': {'MESH_MEM_AGENT_FAMILY': 'claude'}}}]}
    assert _collect_legacy_env_keys(data) == ['MESH_MEM_AGENT_FAMILY']


def test_non_identity_legacy_key_is_not_a_finding(tmp_path: Path) -> None:
    """Only the two identity keys are in scope — the hint fits nothing else."""
    assert _collect_legacy_env_keys({'env': {'MESH_MEM_STATE_DIR': '/tmp/x'}}) == []
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_STATE_DIR': '/tmp/x'})
    result = check_identity([cfg], observations=[])
    assert result.status is CheckStatus.PASS


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


def test_unreadable_index_is_skipped_not_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store we cannot sample is check_zenohd_reachable's problem, not this one."""
    monkeypatch.setattr(doctor, '_readonly_index_db_path', lambda: tmp_path / 'nonexistent-index.db')
    result = check_identity([tmp_path / 'absent.json'])
    assert result.status is CheckStatus.PASS
    assert result.details['unknown_ratio'] is None
    assert result.details['sampled_observations'] == 0


# -- Read-only sampling (no writes to the user's store) ------------------------


def _write_index_db(path: Path, families: list[str]) -> Path:
    """Build a minimal obs_index the check can sample, as the real index does."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE obs_index (observation_id TEXT PRIMARY KEY, created_at TEXT, '
        'payload_json TEXT, deleted_at TEXT, shadowed_at TEXT)'
    )
    conn.executemany(
        'INSERT INTO obs_index (observation_id, created_at, payload_json) VALUES (?, ?, ?)',
        [
            (f'obs{i}', f'2026-08-08T00:00:{i:02d}Z', json.dumps({'agent_family': fam}))
            for i, fam in enumerate(families)
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every state lookup at a directory that does not exist yet."""
    state = tmp_path / 'state'
    monkeypatch.setenv('KIOKU_MESH_STATE_DIR', str(state))
    monkeypatch.delenv('KIOKU_MESH_INDEX_DB', raising=False)
    return state


@pytest.mark.parametrize('backend_mode', ['local', 'zenoh'])
def test_fresh_state_is_not_created_by_the_check(
    _isolated_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_mode: str
) -> None:
    """Diagnosing a host must not set that host up: no dir, no DB, no schema.

    Reading observations through get_backend() created state_dir/local/raw.db
    and index.db on a fresh host merely because doctor ran.
    """
    monkeypatch.setenv('KIOKU_MESH_BACKEND', backend_mode)
    result = check_identity([tmp_path / 'absent.json'])
    assert result.status is CheckStatus.PASS
    assert result.details['unknown_ratio'] is None
    assert not _isolated_state.exists()


def test_existing_index_is_sampled_without_being_modified(
    _isolated_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sample is real (so the test above isn't vacuous) and read-only."""
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    db = _write_index_db(_isolated_state / 'local' / 'index.db', ['unknown'] * 9 + ['claude'])
    before = db.read_bytes()
    mtime_before = db.stat().st_mtime_ns
    sidecars_before = sorted(p.name for p in db.parent.iterdir())

    result = check_identity([tmp_path / 'absent.json'])

    assert result.details['sampled_observations'] == 10
    assert result.details['unknown_ratio'] == 0.9
    assert result.status is CheckStatus.WARN
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime_before
    # No -wal / -shm / -journal left behind either.
    assert sorted(p.name for p in db.parent.iterdir()) == sidecars_before


def test_deleted_and_shadowed_rows_are_excluded_from_the_sample(
    _isolated_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sample the rows a search would return, not every row ever written."""
    import sqlite3

    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'local')
    db = _write_index_db(_isolated_state / 'local' / 'index.db', ['claude', 'unknown', 'unknown'])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE obs_index SET deleted_at = '2026-08-08T01:00:00Z' WHERE observation_id = 'obs1'")
    conn.execute("UPDATE obs_index SET shadowed_at = '2026-08-08T01:00:00Z' WHERE observation_id = 'obs2'")
    conn.commit()
    conn.close()

    result = check_identity([tmp_path / 'absent.json'])
    assert result.details['sampled_observations'] == 1
    assert result.details['unknown_ratio'] == 0.0


def test_index_db_env_override_is_honored_in_zenoh_mode(
    _isolated_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'zenoh')
    db = _write_index_db(tmp_path / 'elsewhere' / 'index.db', ['unknown'] * 10)
    monkeypatch.setenv('KIOKU_MESH_INDEX_DB', str(db))
    result = check_identity([tmp_path / 'absent.json'])
    assert result.status is CheckStatus.WARN
    assert result.details['sampled_observations'] == 10


def test_in_memory_index_has_nothing_to_sample(
    _isolated_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('KIOKU_MESH_BACKEND', 'zenoh')
    monkeypatch.setenv('KIOKU_MESH_INDEX_DB', ':memory:')
    assert doctor._readonly_index_db_path() is None
    result = check_identity([tmp_path / 'absent.json'])
    assert result.details['unknown_ratio'] is None


def test_sampling_never_imports_a_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_backend is what created the files; it must not be on this path."""
    import kioku_mesh.memory.backend as backend_mod

    def boom() -> object:
        raise AssertionError('check_identity must not construct a backend')

    monkeypatch.setattr(backend_mod, 'get_backend', boom)
    result = check_identity([tmp_path / 'absent.json'])
    assert result.status in (CheckStatus.PASS, CheckStatus.WARN)


# -- Wiring --------------------------------------------------------------------


def test_identity_is_registered_last_in_the_check_order() -> None:
    """Registry assertions read the order as data, so nothing has to run.

    Executing run_all_checks() to read back the names would run the other ten
    checks for real, and those open the developer's ~/.config, TLS certs and
    memory store — inputs this module promises not to touch.
    """
    assert doctor._CHECK_ORDER[-1] == 'check_identity'
    assert doctor._CHECK_ORDER.index('check_identity') > doctor._CHECK_ORDER.index('check_legacy_namespace')


def test_check_order_names_resolve_to_callables() -> None:
    """The registry is names, so a typo must not go unnoticed until runtime."""
    for name in doctor._CHECK_ORDER:
        assert callable(getattr(doctor, name)), name


def test_run_all_checks_runs_every_registered_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """All checks stubbed: this exercises the wiring without touching the host."""
    for name in doctor._CHECK_ORDER:
        monkeypatch.setattr(
            doctor,
            name,
            lambda n=name: CheckResult(name=n.removeprefix('check_'), status=CheckStatus.PASS, summary='stub'),
        )
    names = [r.name for r in doctor.run_all_checks()]
    assert names == [n.removeprefix('check_') for n in doctor._CHECK_ORDER]
    assert 'identity' in names


def test_default_config_paths_cover_both_clients() -> None:
    paths = [p.name for p in doctor._default_identity_config_paths()]
    assert '.claude.json' in paths
    assert 'config.toml' in paths


def test_identity_warn_maps_to_warning_exit() -> None:
    """Unknown dominance alone is a warning: exit 1, not a failed run."""
    warned = CheckResult(name='identity', status=CheckStatus.WARN, summary='stub')
    assert doctor.exit_code_for(doctor.worst_status([warned])) == 1


def test_identity_fail_maps_to_failure_exit() -> None:
    """A retired identity key means identity is unset: exit 2."""
    failed = CheckResult(name='identity', status=CheckStatus.FAIL, summary='stub')
    assert doctor.exit_code_for(doctor.worst_status([failed])) == 2


def test_legacy_identity_config_exits_two_end_to_end(tmp_path: Path) -> None:
    """The FAIL has to survive the fold into worst_status/exit code."""
    cfg = _write_claude_config(tmp_path / '.claude.json', {'MESH_MEM_AGENT_FAMILY': 'claude'})
    result = check_identity([cfg], observations=[])
    assert doctor.exit_code_for(doctor.worst_status([result])) == 2

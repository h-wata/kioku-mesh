"""Tests for ADR-0026 supersede-candidate detection and the doctor check.

Covers:
  - ``normalize_subject`` (casefold + whitespace collapse)
  - ``find_candidates_in_index`` matching / exclusion rules
  - ``LocalIndex.search`` exact ``memory_type`` filter (the SQL the detector
    relies on)
  - ``doctor.check_conflicting_latest`` grouping with injected observations
  - the CLI ``_format_supersede_hint`` renderer

These are pure SQLite / function tests — no zenohd, no backend wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from kioku_mesh.core.models import Observation
from kioku_mesh.doctor import check_conflicting_latest
from kioku_mesh.doctor import CheckStatus
from kioku_mesh.memory.local_index import LocalIndex
from kioku_mesh.memory.supersede import find_candidates_in_index
from kioku_mesh.memory.supersede import normalize_subject


def _mk(
    content: str,
    *,
    memory_type: str = 'decision',
    subject: str = 'db',
    project: str = 'demo',
    supersedes: list[str] | None = None,
    visibility: str = '',
    scope_id: str = '',
) -> Observation:
    return Observation(
        content=content,
        project=project,
        memory_type=memory_type,
        subject=subject,
        supersedes=list(supersedes or []),
        visibility=visibility,
        scope_id=scope_id,
    )


@pytest.fixture
def idx(tmp_path: Path) -> LocalIndex:
    index = LocalIndex.connect(str(tmp_path / 'index.db'))
    yield index
    index.close()


# -- normalize_subject ---------------------------------------------------------


def test_normalize_subject_casefolds_and_collapses_whitespace() -> None:
    assert normalize_subject('  DB   Choice ') == 'db choice'
    assert normalize_subject('DB') == normalize_subject('db')
    assert normalize_subject('') == ''


# -- find_candidates_in_index --------------------------------------------------


def test_finds_same_subject_type_project(idx: LocalIndex) -> None:
    old = _mk('use SQLite', subject='db')
    idx.upsert(old)
    new = _mk('use PostgreSQL', subject='DB')  # different casing on purpose
    candidates = find_candidates_in_index(idx, new)
    assert [c.observation_id for c in candidates] == [old.observation_id]


def test_excludes_self(idx: LocalIndex) -> None:
    obs = _mk('use SQLite', subject='db')
    idx.upsert(obs)
    # Passing the same already-saved obs must not return itself.
    assert find_candidates_in_index(idx, obs) == []


def test_non_revisable_type_returns_empty(idx: LocalIndex) -> None:
    idx.upsert(_mk('a note', memory_type='note', subject='db'))
    new = _mk('another note', memory_type='note', subject='db')
    assert find_candidates_in_index(idx, new) == []


def test_empty_subject_returns_empty(idx: LocalIndex) -> None:
    idx.upsert(_mk('use SQLite', subject=''))
    assert find_candidates_in_index(idx, _mk('use PostgreSQL', subject='')) == []


def test_different_subject_not_matched(idx: LocalIndex) -> None:
    idx.upsert(_mk('use SQLite', subject='db'))
    assert find_candidates_in_index(idx, _mk('cache layer', subject='cache')) == []


def test_different_project_not_matched(idx: LocalIndex) -> None:
    idx.upsert(_mk('use SQLite', subject='db', project='alpha'))
    assert find_candidates_in_index(idx, _mk('use PostgreSQL', subject='db', project='beta')) == []


def test_different_memory_type_not_matched(idx: LocalIndex) -> None:
    # A config entry should not surface a decision candidate, even on the
    # same subject — the SQL memory_type filter keeps them separate.
    idx.upsert(_mk('decided X', memory_type='decision', subject='db'))
    assert find_candidates_in_index(idx, _mk('configured X', memory_type='config', subject='db')) == []


def test_superseded_candidate_is_excluded(idx: LocalIndex) -> None:
    a = _mk('use SQLite', subject='db')
    idx.upsert(a)
    b = _mk('use PostgreSQL', subject='db', supersedes=[a.observation_id])
    idx.upsert(b)
    # A new save on the same subject should see only the live B, not the
    # already-superseded A.
    c = _mk('use CockroachDB', subject='db')
    candidates = find_candidates_in_index(idx, c)
    assert [x.observation_id for x in candidates] == [b.observation_id]


def test_tombstoned_candidate_is_excluded(idx: LocalIndex) -> None:
    a = _mk('use SQLite', subject='db')
    idx.upsert(a)
    idx.mark_deleted(a.observation_id, a.created_at)
    assert find_candidates_in_index(idx, _mk('use PostgreSQL', subject='db')) == []


def test_scope_mismatch_not_matched(idx: LocalIndex) -> None:
    idx.upsert(_mk('team decision', subject='db', visibility='team', scope_id='alpha'))
    new = _mk('other team decision', subject='db', visibility='team', scope_id='beta')
    assert find_candidates_in_index(idx, new) == []


def test_candidate_limit_is_capped(idx: LocalIndex) -> None:
    for i in range(8):
        idx.upsert(_mk(f'old {i}', subject='db'))
    candidates = find_candidates_in_index(idx, _mk('new one', subject='db'), limit=3)
    assert len(candidates) == 3


# -- LocalIndex.search memory_type filter --------------------------------------


def test_search_memory_type_filter(idx: LocalIndex) -> None:
    idx.upsert(_mk('a decision', memory_type='decision', subject='x'))
    idx.upsert(_mk('a note', memory_type='note', subject='x'))
    hits = idx.search(memory_type='decision')
    assert [h.memory_type for h in hits] == ['decision']


# C1: memory_type AND is maintained under search_mode='or' and 'and_or'


def test_memory_type_filter_ands_with_or_search_mode(idx: LocalIndex) -> None:
    """memory_type filter stays ANDed even when search_mode='or'/'and_or'.

    Both observations contain the same query term so the OR expansion
    would return them both if memory_type were ORed in.  The note must
    be excluded in all modes.
    """
    term = 'postgresql'
    idx.upsert(_mk(f'use {term} for prod', memory_type='decision', subject='db'))
    idx.upsert(_mk(f'{term} note entry', memory_type='note', subject='db'))

    for mode in ('or', 'and_or'):
        hits = idx.search(memory_type='decision', query=term, search_mode=mode)
        types = [h.memory_type for h in hits]
        assert types == ['decision'], f"search_mode={mode!r}: expected only 'decision', got {types}"


# -- doctor.check_conflicting_latest -------------------------------------------


def test_conflicting_latest_pass_when_unique() -> None:
    obs = [_mk('only one', subject='db'), _mk('other', subject='cache')]
    result = check_conflicting_latest(observations=obs)
    assert result.status is CheckStatus.PASS


def test_conflicting_latest_warns_on_duplicate_subject() -> None:
    obs = [_mk('use SQLite', subject='db'), _mk('use PostgreSQL', subject='DB')]
    result = check_conflicting_latest(observations=obs)
    assert result.status is CheckStatus.WARN
    assert result.details['conflicts'] == 1


def test_conflicting_latest_ignores_non_revisable_types() -> None:
    obs = [_mk('n1', memory_type='note', subject='db'), _mk('n2', memory_type='note', subject='db')]
    result = check_conflicting_latest(observations=obs)
    assert result.status is CheckStatus.PASS


def test_conflicting_latest_separates_scope() -> None:
    obs = [
        _mk('a', subject='db', visibility='team', scope_id='alpha'),
        _mk('b', subject='db', visibility='team', scope_id='beta'),
    ]
    result = check_conflicting_latest(observations=obs)
    assert result.status is CheckStatus.PASS


# -- CLI hint renderer ---------------------------------------------------------


def test_format_supersede_hint_empty() -> None:
    from kioku_mesh.__main__ import _format_supersede_hint

    assert _format_supersede_hint([]) == []


def test_format_supersede_hint_lists_ids_and_advice() -> None:
    from kioku_mesh.__main__ import _format_supersede_hint

    old = _mk('use SQLite', subject='db')
    lines = _format_supersede_hint([old])
    text = '\n'.join(lines)
    assert old.observation_id in text
    assert '--supersedes' in text
    assert 'delete' in text


# -- C2 / C3 regression tests --------------------------------------------------


def test_c2_cmd_save_swallows_renderer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_save must return 0 even when _format_supersede_hint raises (C2)."""
    import argparse
    from unittest.mock import MagicMock

    import kioku_mesh.__main__ as cli_module

    args = argparse.Namespace(
        tags='',
        source_files=None,
        references=None,
        supersedes=None,
        visibility='',
        content='use PostgreSQL',
        project='demo',
        memory_type='decision',
        importance=3,
        subject='db',
        summary='',
    )

    mock_backend = MagicMock()
    mock_backend.find_supersede_candidates.return_value = [_mk('old decision', subject='db')]
    monkeypatch.setattr(cli_module, 'get_backend', lambda: mock_backend)
    monkeypatch.setattr(cli_module, 'resolve_write_visibility', lambda _: ('', ''))
    monkeypatch.setattr(cli_module, 'format_visibility', lambda v, s: 'local')

    def _raiser(_: object) -> NoReturn:
        raise RuntimeError('render boom')

    monkeypatch.setattr(cli_module, '_format_supersede_hint', _raiser)

    result = cli_module._cmd_save(args)

    assert result == 0
    mock_backend.put_observation.assert_called_once()


def test_pool_limit_triggers_debug_log(
    idx: LocalIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_candidates_in_index emits a debug log when pool hits _POOL_LIMIT (C3)."""
    from kioku_mesh.memory.supersede import _POOL_LIMIT
    import kioku_mesh.memory.supersede as supersede_module

    obs = _mk('use PostgreSQL', subject='db')
    fake_pool = [_mk('old', subject='other')] * _POOL_LIMIT
    monkeypatch.setattr(LocalIndex, 'search', lambda self, **kw: fake_pool)

    debug_calls: list[str] = []
    monkeypatch.setattr(
        supersede_module.logger,
        'debug',
        lambda msg, *args: debug_calls.append(msg % args if args else msg),
    )

    find_candidates_in_index(idx, obs)

    assert any('_POOL_LIMIT' in m for m in debug_calls)


# -- doctor conflicting-latest scan truncation ---------------------------------


def _patch_backend(monkeypatch: pytest.MonkeyPatch, rows: list[Observation]) -> None:
    """Make the doctor's lazily-imported get_backend return ``rows``."""
    import kioku_mesh.memory.backend as backend_module

    class _StubBackend:
        def search_observations(self, **_kw: object) -> list[Observation]:
            return rows

    monkeypatch.setattr(backend_module, 'get_backend', lambda: _StubBackend())


def test_conflicting_latest_truncation_warns_when_no_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated scan with no conflicts is WARN (inconclusive), not PASS."""
    import kioku_mesh.doctor as doctor_module

    monkeypatch.setattr(doctor_module, '_CONFLICT_SCAN_LIMIT', 3)
    # Distinct subjects → no conflict, but len == patched cap → truncated.
    _patch_backend(monkeypatch, [_mk(f'd{i}', subject=f's{i}') for i in range(3)])

    result = check_conflicting_latest()
    assert result.status is CheckStatus.WARN
    assert result.details['truncated'] is True
    assert result.details['conflicts'] == 0


def test_conflicting_latest_truncation_noted_alongside_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """When truncated AND conflicts exist, the summary flags the truncation."""
    import kioku_mesh.doctor as doctor_module

    monkeypatch.setattr(doctor_module, '_CONFLICT_SCAN_LIMIT', 3)
    _patch_backend(monkeypatch, [_mk('a', subject='db'), _mk('b', subject='DB'), _mk('c', subject='cache')])

    result = check_conflicting_latest()
    assert result.status is CheckStatus.WARN
    assert result.details['conflicts'] == 1
    assert result.details['truncated'] is True
    assert 'truncated' in result.summary


def test_conflicting_latest_injected_list_never_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected observations list is taken as complete, ignoring the cap."""
    import kioku_mesh.doctor as doctor_module

    monkeypatch.setattr(doctor_module, '_CONFLICT_SCAN_LIMIT', 1)
    result = check_conflicting_latest(observations=[_mk('only', subject='db')])
    assert result.status is CheckStatus.PASS
    assert result.details['truncated'] is False

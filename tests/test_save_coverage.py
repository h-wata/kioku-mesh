"""Unit tests for the ``scripts/save_coverage.py`` opportunity-coverage prototype (#105).

The script lives under ``scripts/`` (not in the ``mesh_mem`` package) because it
is client / analysis tooling, not part of the MCP server. It is loaded here by
path so the test can exercise it without putting ``scripts/`` on ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'save_coverage.py'
_spec = importlib.util.spec_from_file_location('save_coverage', _SCRIPT)
assert _spec is not None and _spec.loader is not None
save_coverage = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve the module via sys.modules.
sys.modules['save_coverage'] = save_coverage
_spec.loader.exec_module(save_coverage)

_EXAMPLE = Path(__file__).resolve().parent.parent / 'scripts' / 'save_coverage_example.jsonl'


def test_parse_events_skips_blank_and_preserves_order() -> None:
    events = save_coverage.parse_events(
        [
            '{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug"}',
            '',
            '{"ts": "2026-05-28T10:01:00Z", "type": "save", "memory_type": "bug"}',
        ]
    )
    assert [e.event_type for e in events] == ['opportunity', 'save']
    assert events[0].kind == 'bug'


def test_parse_events_rejects_missing_fields() -> None:
    with pytest.raises(ValueError, match='line 1'):
        save_coverage.parse_events(['{"type": "save"}'])


def test_parse_events_rejects_bad_json() -> None:
    with pytest.raises(ValueError, match='invalid JSON'):
        save_coverage.parse_events(['{not json'])


def test_full_coverage() -> None:
    events = save_coverage.parse_events(
        [
            '{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug"}',
            '{"ts": "2026-05-28T10:02:00Z", "type": "save", "memory_type": "bug"}',
        ]
    )
    report = save_coverage.compute_coverage(events)
    assert report.total == 1
    assert report.covered == 1
    assert report.coverage == 1.0
    assert report.missed == []
    assert report.orphan_saves == []


def test_save_outside_window_misses_and_orphans() -> None:
    events = save_coverage.parse_events(
        [
            '{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug"}',
            '{"ts": "2026-05-28T11:00:00Z", "type": "save", "memory_type": "bug"}',
        ]
    )
    report = save_coverage.compute_coverage(events, window_seconds=600)
    assert report.covered == 0
    assert len(report.missed) == 1
    assert len(report.orphan_saves) == 1


def test_one_to_one_matching() -> None:
    events = save_coverage.parse_events(
        [
            '{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug"}',
            '{"ts": "2026-05-28T10:00:30Z", "type": "opportunity", "kind": "decision"}',
            '{"ts": "2026-05-28T10:01:00Z", "type": "save", "memory_type": "bug"}',
        ]
    )
    report = save_coverage.compute_coverage(events)
    assert report.total == 2
    assert report.covered == 1
    assert report.coverage == 0.5


def test_require_type_match_gates_mismatched_save() -> None:
    events = save_coverage.parse_events(
        [
            '{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug"}',
            '{"ts": "2026-05-28T10:01:00Z", "type": "save", "memory_type": "config"}',
        ]
    )
    lenient = save_coverage.compute_coverage(events, require_type_match=False)
    strict = save_coverage.compute_coverage(events, require_type_match=True)
    assert lenient.covered == 1
    assert strict.covered == 0


def test_coverage_with_no_opportunities_is_zero() -> None:
    events = save_coverage.parse_events(['{"ts": "2026-05-28T10:00:00Z", "type": "save", "memory_type": "bug"}'])
    report = save_coverage.compute_coverage(events)
    assert report.total == 0
    assert report.coverage == 0.0
    assert len(report.orphan_saves) == 1


def test_format_report_json_surfaces_missed() -> None:
    events = save_coverage.parse_events(
        [
            '{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug", "label": "x"}',
            '{"ts": "2026-05-28T11:00:00Z", "type": "save", "memory_type": "bug"}',
        ]
    )
    report = save_coverage.compute_coverage(events, window_seconds=600)
    payload = json.loads(save_coverage.format_report(report, as_json=True))
    assert payload['total'] == 1
    assert payload['covered'] == 0
    assert payload['missed'][0]['kind'] == 'bug'


def test_main_reports_example_trace(capsys: pytest.CaptureFixture[str]) -> None:
    code = save_coverage.main([str(_EXAMPLE), '--json'])
    assert code == 0
    out = capsys.readouterr().out
    assert '"coverage": 0.75' in out


def test_main_min_coverage_gate(capsys: pytest.CaptureFixture[str]) -> None:
    assert save_coverage.main([str(_EXAMPLE), '--min-coverage', '0.9']) == 1
    capsys.readouterr()
    assert save_coverage.main([str(_EXAMPLE), '--min-coverage', '0.5']) == 0


@pytest.mark.parametrize('bad', ['0', '-1', '-0.5', 'abc', 'inf', '-inf', 'nan', '1e309'])
def test_main_rejects_invalid_window_seconds(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        save_coverage.main([str(_EXAMPLE), '--window-seconds', bad])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert '--window-seconds' in err


@pytest.mark.parametrize('bad', ['-0.1', '1.1', '2', 'abc'])
def test_main_rejects_invalid_min_coverage(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        save_coverage.main([str(_EXAMPLE), '--min-coverage', bad])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert '--min-coverage' in err


@pytest.mark.parametrize('ok', ['0.0', '1.0', '0.75'])
def test_main_accepts_boundary_min_coverage(ok: str, capsys: pytest.CaptureFixture[str]) -> None:
    # All values are valid argparse-wise; exit code depends only on whether the
    # example trace's 0.75 coverage clears the bar.
    rc = save_coverage.main([str(_EXAMPLE), '--min-coverage', ok])
    capsys.readouterr()
    assert rc in (0, 1)

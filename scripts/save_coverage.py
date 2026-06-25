"""Prototype: measure proactive-save *opportunity coverage* from an event trace.

Issue #105 — kioku-mesh ships a PROACTIVE SAVE protocol (ADR-0009, PR #103) but
its acceptance has so far been qualitative (dogfooding). This script turns a
flat JSONL trace of two event kinds into a single objective number::

    opportunity coverage = (opportunities followed by a save) / (opportunities)

It is deliberately small and transport-agnostic: the trace is whatever a hook,
log scraper, or manual annotation produces, one JSON object per line. See
``docs/design/issue-105-proactive-save-opportunity-coverage.md`` for the data
sources and the pipeline this is the first iteration of.

Event schema (one JSON object per line)::

    {"ts": "2026-05-28T10:00:00Z", "type": "opportunity",
     "kind": "bug", "label": "fixed null deref in parser"}
    {"ts": "2026-05-28T10:02:00Z", "type": "save",
     "memory_type": "bug", "observation_id": "..."}

``ts`` (ISO8601) and ``type`` are required; the rest are optional.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import math
import sys

# Opportunity ``kind`` -> the ``memory_type`` a faithful save would carry. Used
# only when --require-type-match is on; kept lenient so the metric can be
# iterated without committing to a rigid taxonomy.
OPPORTUNITY_TO_MEMORY_TYPE = {
    'bug': 'bug',
    'decision': 'decision',
    'pattern': 'pattern',
    'config': 'config',
    'summary': 'summary',
    'discovery': 'note',
    'note': 'note',
}

DEFAULT_WINDOW_SECONDS = 1800.0


@dataclass
class Event:
    """A single trace event: an opportunity to save, or an actual save."""

    ts: datetime
    event_type: str
    kind: str = ''
    memory_type: str = ''
    label: str = ''
    order: int = 0


@dataclass
class CoverageReport:
    """Result of matching saves to opportunities within a time window."""

    total: int
    matches: list[tuple[Event, Event]]
    missed: list[Event]
    orphan_saves: list[Event]
    window_seconds: float
    require_type_match: bool

    @property
    def covered(self) -> int:
        """Number of opportunities matched to a save."""
        return len(self.matches)

    @property
    def coverage(self) -> float:
        """Covered/total in [0, 1]; 0.0 when there are no opportunities."""
        return self.covered / self.total if self.total else 0.0


def _parse_iso(value: str) -> datetime:
    """Parse an ISO8601 timestamp, tolerating a trailing ``Z``, into UTC."""
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_events(lines: Iterable[str]) -> list[Event]:
    """Parse a JSONL trace into ``Event`` objects, preserving input order.

    Blank lines are ignored. A malformed line (bad JSON, missing ``ts`` /
    ``type``, unparseable timestamp) raises ``ValueError`` naming the line
    number, so a broken trace fails loudly rather than silently skewing the
    metric.
    """
    events: list[Event] = []
    order = 0
    for lineno, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f'line {lineno}: invalid JSON: {e}') from e
        if not isinstance(obj, dict):
            raise ValueError(f'line {lineno}: expected a JSON object, got {type(obj).__name__}')
        if 'ts' not in obj or 'type' not in obj:
            raise ValueError(f'line {lineno}: each event needs "ts" and "type"')
        try:
            ts = _parse_iso(str(obj['ts']))
        except ValueError as e:
            raise ValueError(f'line {lineno}: bad timestamp {obj["ts"]!r}: {e}') from e
        event_type = str(obj['type'])
        if event_type not in ('opportunity', 'save'):
            raise ValueError(f'line {lineno}: type must be "opportunity" or "save", got {event_type!r}')
        events.append(
            Event(
                ts=ts,
                event_type=event_type,
                kind=str(obj.get('kind', '')),
                memory_type=str(obj.get('memory_type', '')),
                label=str(obj.get('label', obj.get('detail', ''))),
                order=order,
            )
        )
        order += 1
    return events


def _types_compatible(kind: str, memory_type: str) -> bool:
    """Return True when a save's ``memory_type`` plausibly satisfies ``kind``.

    Missing data on either side is treated as compatible — the metric should
    not punish a trace that omits the optional type fields.
    """
    if not kind or not memory_type:
        return True
    return OPPORTUNITY_TO_MEMORY_TYPE.get(kind, kind) == memory_type


def compute_coverage(
    events: Sequence[Event],
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    require_type_match: bool = False,
) -> CoverageReport:
    """Greedily match each save to the oldest eligible preceding opportunity.

    An opportunity is *covered* when a save occurs at or after it and within
    ``window_seconds``. Matching is one-to-one: a save consumes a single
    opportunity, so two saves are needed to cover two opportunities. Saves with
    no eligible opportunity are reported as ``orphan_saves`` (proactive saves
    beyond the logged opportunities, or noise); opportunities that no save ever
    reaches are ``missed``.
    """
    window = timedelta(seconds=window_seconds)
    ordered = sorted(events, key=lambda e: (e.ts, 0 if e.event_type == 'opportunity' else 1, e.order))

    total = sum(1 for e in ordered if e.event_type == 'opportunity')
    pending: list[Event] = []
    matches: list[tuple[Event, Event]] = []
    missed: list[Event] = []
    orphans: list[Event] = []

    for ev in ordered:
        if ev.event_type == 'opportunity':
            pending.append(ev)
            continue
        # ev is a save: opportunities older than the window can never be
        # covered now, so retire them as missed before trying to match.
        cutoff = ev.ts - window
        live: list[Event] = []
        for opp in pending:
            if opp.ts < cutoff:
                missed.append(opp)
            else:
                live.append(opp)
        pending = live
        match_idx: int | None = None
        for i, opp in enumerate(pending):
            if opp.ts <= ev.ts and (not require_type_match or _types_compatible(opp.kind, ev.memory_type)):
                match_idx = i
                break
        if match_idx is None:
            orphans.append(ev)
        else:
            matches.append((pending.pop(match_idx), ev))

    missed.extend(pending)
    missed.sort(key=lambda e: (e.ts, e.order))
    return CoverageReport(
        total=total,
        matches=matches,
        missed=missed,
        orphan_saves=orphans,
        window_seconds=window_seconds,
        require_type_match=require_type_match,
    )


def _fmt_event(ev: Event) -> str:
    tag = ev.kind or ev.memory_type or '-'
    return f'  {ev.ts.isoformat()} [{tag}] {ev.label or "-"}'


def format_report(report: CoverageReport, as_json: bool = False) -> str:
    """Render a ``CoverageReport`` as human-readable text or a JSON document."""
    if as_json:
        payload = {
            'coverage': round(report.coverage, 4),
            'covered': report.covered,
            'total': report.total,
            'window_seconds': report.window_seconds,
            'require_type_match': report.require_type_match,
            'missed': [{'ts': e.ts.isoformat(), 'kind': e.kind, 'label': e.label} for e in report.missed],
            'orphan_saves': [
                {'ts': e.ts.isoformat(), 'memory_type': e.memory_type, 'label': e.label} for e in report.orphan_saves
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    match_state = 'on' if report.require_type_match else 'off'
    lines = [
        f'opportunity coverage: {report.coverage * 100:.1f}% ({report.covered}/{report.total})',
        f'window: {report.window_seconds:g}s, type-match: {match_state}',
    ]
    if report.missed:
        lines.append(f'missed opportunities ({len(report.missed)}):')
        lines.extend(_fmt_event(e) for e in report.missed)
    if report.orphan_saves:
        lines.append(f'orphan saves ({len(report.orphan_saves)}):')
        lines.extend(_fmt_event(e) for e in report.orphan_saves)
    return '\n'.join(lines)


def _read_lines(path: str) -> list[str]:
    if path == '-':
        return sys.stdin.read().splitlines()
    with open(path, encoding='utf-8') as fh:
        return fh.read().splitlines()


def _positive_seconds(value: str) -> float:
    """Argparse type: finite float strictly greater than 0.

    Reject inf / NaN explicitly so a caller passing ``--window-seconds inf``
    fails with a clean argparse error instead of an OverflowError much later
    when the value is fed into ``timedelta``.
    """
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'must be a number, got {value!r}') from exc
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError(f'must be finite, got {seconds}')
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f'must be > 0, got {seconds}')
    return seconds


def _coverage_fraction(value: str) -> float:
    """Argparse type: float in the inclusive range [0.0, 1.0]."""
    try:
        fraction = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'must be a number, got {value!r}') from exc
    if not 0.0 <= fraction <= 1.0:
        raise argparse.ArgumentTypeError(f'must be between 0.0 and 1.0 inclusive, got {fraction}')
    return fraction


def main(argv: Sequence[str] | None = None) -> int:
    """Run the coverage report as a CLI and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog='save_coverage',
        description='Measure proactive-save opportunity coverage from a JSONL event trace.',
    )
    parser.add_argument('trace', help="path to a JSONL trace, or '-' for stdin")
    parser.add_argument(
        '--window-seconds',
        type=_positive_seconds,
        default=DEFAULT_WINDOW_SECONDS,
        help='max seconds a save may trail an opportunity and still cover it; must be > 0 (default: %(default)s)',
    )
    parser.add_argument(
        '--require-type-match',
        action='store_true',
        help='only let a save cover an opportunity when their kind / memory_type agree',
    )
    parser.add_argument('--json', action='store_true', help='emit a JSON document instead of text')
    parser.add_argument(
        '--min-coverage',
        type=_coverage_fraction,
        default=None,
        help='exit non-zero when coverage is below this fraction; must be in [0.0, 1.0]; for CI gating',
    )
    args = parser.parse_args(argv)

    events = parse_events(_read_lines(args.trace))
    report = compute_coverage(events, args.window_seconds, args.require_type_match)
    print(format_report(report, as_json=args.json))
    if args.min_coverage is not None and report.coverage < args.min_coverage:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

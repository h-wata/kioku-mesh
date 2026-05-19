"""Command-line interface for mesh-mem.

Thin wrapper over the same store primitives the MCP server uses.
``gc`` performs physical delete via ``session.delete`` — the Zenoh storage
backend is expected to propagate the removal through replication so both
sides converge.
"""

import argparse
from datetime import datetime
from datetime import timezone
import json
import sys

from . import __version__
from .identity import get_pc_id
from .identity import get_session_id
from .models import Observation
from .models import VALID_MEMORY_TYPES
from .store import _reset_session
from .store import drain_pending_puts
from .store import execute_bulk_purge
from .store import find_observation_by_id
from .store import gc_expired_tombstones
from .store import get_transport_status
from .store import MAX_SEARCH
from .store import mesh_ready_label
from .store import physical_delete_observation
from .store import put_observation
from .store import put_tombstone
from .store import scan_obs_by_pc_id
from .store import search_observations
from .store import set_rebuild_on_init_default
from .store import set_rebuild_on_init_explicit
from .store import stop_pending_drain_background

_SEARCH_FORMATS = ('text', 'markdown', 'json')


def _parse_csv(value: str) -> list[str]:
    return [s.strip() for s in value.split(',') if s.strip()]


def _parse_iso_or_none(value: str) -> datetime | None:
    """Parse an ISO8601 timestamp, treating naive datetimes as UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _delete_has_bulk_selector(args: argparse.Namespace) -> bool:
    """Return True when any bulk-delete narrowing flag is present."""
    return bool(args.project or args.pc_id or args.since or args.until)


def _positive_int(value: str) -> int:
    """Argparse type that rejects zero / negative integers."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer (1 or greater).')
    return parsed


def _select_delete_targets(args: argparse.Namespace) -> tuple[list[Observation], str | None]:
    """Resolve bulk-delete targets from search filters.

    Returns ``(matches, error_message)`` where ``error_message`` is suitable
    for stderr output and an exit-code-2 caller path.
    """
    until_dt = _parse_iso_or_none(args.until or '')
    if args.until and until_dt is None:
        return [], '--until must be in ISO8601 format.'

    matches = search_observations(
        project=args.project or '',
        pc_id=args.pc_id or '',
        since_iso=args.since or '',
        limit=MAX_SEARCH,
    )
    if len(matches) >= MAX_SEARCH:
        return [], (
            f'bulk delete hit the upper limit ({MAX_SEARCH} entries).'
            ' Narrow further with --project/--pc-id/--since/--until.'
        )
    if until_dt is not None:
        matches = [obs for obs in matches if (_parse_iso_or_none(obs.created_at) or until_dt) <= until_dt]
    return matches, None


def _cmd_save(args: argparse.Namespace) -> int:
    tag_list = [t.strip() for t in (args.tags or '').split(',') if t.strip()]
    source_files = _parse_csv(args.source_files) if args.source_files else []
    supersedes = _parse_csv(args.supersedes) if args.supersedes else []
    obs = Observation(
        content=args.content,
        project=args.project or '',
        tags=tag_list,
        memory_type=args.memory_type,
        importance=args.importance,
        subject=args.subject or '',
        summary=args.summary or '',
        source_files=source_files,
        supersedes=supersedes,
    )
    put_observation(obs)
    print(f'saved: {obs.observation_id}')
    return 0


def _truncate_search_content(content: str, limit: int = 80) -> str:
    """Return ``content`` truncated for compact search-style summaries."""
    if len(content) <= limit:
        return content
    return content[:limit] + '…'


def _format_search_text_entry(obs: Observation) -> str:
    """Render one search result in the legacy human-readable text format."""
    body = obs.summary if obs.summary else obs.content[:80]
    subject_part = f' {obs.subject}' if obs.subject else ''
    project_part = f' ({obs.project})' if obs.project else ''
    return (
        f'[{obs.memory_type}][{obs.importance}] {obs.created_at[:19]}'
        f'{project_part}{subject_part}\n'
        f'{body} <id={obs.observation_id}>'
    )


def _format_search_markdown_body(obs: Observation) -> str:
    """Render the markdown-mode body with subject/summary/content fallback."""
    if obs.subject:
        if obs.summary:
            return f'{obs.subject} — {obs.summary}'
        return obs.subject
    if obs.summary:
        return obs.summary
    return _truncate_search_content(obs.content)


def _format_search_markdown_entry(obs: Observation) -> str:
    """Render one search result as a single markdown bullet."""
    project_part = f' ({obs.project})' if obs.project else ''
    body = _format_search_markdown_body(obs)
    return (
        f'- **[{obs.memory_type}][{obs.importance}]** '
        f'{obs.created_at[:16]}{project_part} '
        f'{body} <id={obs.observation_id}>'
    )


def _format_search_json(results: list[Observation]) -> str:
    """Render search results as a JSON array using the full observation schema."""
    return json.dumps([json.loads(obs.to_json()) for obs in results], ensure_ascii=False)


def _cmd_search(args: argparse.Namespace) -> int:
    results = search_observations(
        query=args.query or '',
        agent_family=args.agent_family or '',
        client_id=args.client_id or '',
        pc_id=args.pc_id or '',
        session_id=args.session_id or '',
        project=args.project or '',
        since_iso=args.since or '',
        limit=args.limit,
    )
    if not results:
        if args.format == 'text':
            print('No matching memories.')
        elif args.format == 'json':
            print('[]')
        return 0
    if args.format == 'text':
        print('\n---\n'.join(_format_search_text_entry(obs) for obs in results))
        return 0
    if args.format == 'markdown':
        print('\n'.join(_format_search_markdown_entry(obs) for obs in results))
        return 0
    print(_format_search_json(results))
    return 0


def _cmd_get_memory(args: argparse.Namespace) -> int:
    if len(args.observation_id) != 32:
        print('observation_id must be a full 32-character match.', file=sys.stderr)
        return 2
    obs = find_observation_by_id(args.observation_id)
    if obs is None:
        print(f'observation_id {args.observation_id} not found.', file=sys.stderr)
        return 1
    lines = [
        f'id: {obs.observation_id}',
        f'memory_type: {obs.memory_type}',
        f'importance: {obs.importance}',
        f'created_at: {obs.created_at}',
        f'project: {obs.project or "-"}',
        f'subject: {obs.subject or "-"}',
        f'summary: {obs.summary or "-"}',
        f'agent: {obs.agent_family}/{obs.client_id}',
        f'tags: {", ".join(obs.tags) if obs.tags else "-"}',
        f'source_files: {", ".join(obs.source_files) if obs.source_files else "-"}',
        f'supersedes: {", ".join(obs.supersedes) if obs.supersedes else "-"}',
        '---',
        obs.content,
    ]
    print('\n'.join(lines))
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    if args.observation_id and _delete_has_bulk_selector(args):
        print(
            'observation_id and bulk selector (--project/--pc-id/--since/--until) cannot be combined.',
            file=sys.stderr,
        )
        return 2
    if args.observation_id and args.dry_run:
        print('--dry-run is only valid for bulk delete.', file=sys.stderr)
        return 2
    if args.observation_id and args.yes:
        print('--yes is only valid for bulk delete.', file=sys.stderr)
        return 2

    if not args.observation_id:
        if not _delete_has_bulk_selector(args):
            print(
                'bulk delete requires one of --project/--pc-id/--since/--until.',
                file=sys.stderr,
            )
            return 2
        matches, error = _select_delete_targets(args)
        if error:
            print(error, file=sys.stderr)
            return 2

        selector_parts = []
        if args.project:
            selector_parts.append(f'project={args.project!r}')
        if args.pc_id:
            selector_parts.append(f'pc_id={args.pc_id!r}')
        if args.since:
            selector_parts.append(f'since={args.since!r}')
        if args.until:
            selector_parts.append(f'until={args.until!r}')
        selector_text = ', '.join(selector_parts)
        print(f'bulk delete target: {len(matches)} entries ({selector_text})', file=sys.stderr)
        if not matches:
            print('no targets — no observations matched the selector.')
            return 0
        if args.dry_run:
            print('Dry run — pass --yes to actually delete.')
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    'bulk delete requires interactive confirmation. Pass --yes for non-interactive use.',
                    file=sys.stderr,
                )
                return 2
            prompt = f"Tombstone {len(matches)} entries? type 'yes' to confirm: "
            try:
                answer = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print('\ncancelled.', file=sys.stderr)
                return 1
            if answer != 'yes':
                print('cancelled.', file=sys.stderr)
                return 1

        for obs in matches:
            put_tombstone(obs, reason=args.reason or '')
        print(f'deleted (tombstone): {len(matches)} entries')
        return 0

    if len(args.observation_id) != 32:
        print('observation_id must be a full 32-character match.', file=sys.stderr)
        return 2
    obs = find_observation_by_id(args.observation_id)
    if obs is None:
        print(f'observation_id {args.observation_id} not found.', file=sys.stderr)
        return 1
    put_tombstone(obs, reason=args.reason or '')
    print(f'deleted (tombstone): {args.observation_id}')
    return 0


def _cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    try:
        recent = search_observations(limit=MAX_SEARCH)
    except Exception as e:  # noqa: BLE001
        print(f'failed to read shared memory [{type(e).__name__}]: {e}', file=sys.stderr)
        return 1
    transport = get_transport_status()
    by_family: dict[str, int] = {}
    by_pc: dict[str, int] = {}
    for obs in recent:
        by_family[obs.agent_family] = by_family.get(obs.agent_family, 0) + 1
        by_pc[obs.pc_id] = by_pc.get(obs.pc_id, 0) + 1
    truncated = len(recent) >= MAX_SEARCH
    print(f'mesh-mem version: {__version__}')
    print(f'pc_id: {get_pc_id()}')
    print(f'session_id: {get_session_id()}')
    print(f'zenoh_session: {transport.zenoh_session}')
    print(f'last_put_at_iso: {transport.last_put_at_iso or "-"}')
    print(f'last_put_status: {transport.last_put_status}')
    print(f'recent_puts: {transport.recent_put_ok} ok / {transport.recent_put_error} error')
    print(f'pending_puts: {transport.pending_puts}')
    print(f'drain_in_progress: {"yes" if transport.drain_in_progress else "no"}')
    print(f'drain_last_run_iso: {transport.drain_last_run_iso or "-"}')
    print(f'drain_total_succeeded: {transport.drain_total_succeeded}')
    print(f'count (within limit {MAX_SEARCH}): {len(recent)}{" (limit may be reached)" if truncated else ""}')
    for family, count in sorted(by_family.items()):
        print(f'  family {family}: {count}')
    for pc, count in sorted(by_pc.items()):
        print(f'  pc {pc[:8]}: {count}')
    label = mesh_ready_label()
    print(f'mesh_ready: {label}')
    if label != 'yes':
        print(
            'WARNING: peer alignment not yet complete. Search counts may be low right after restart.',
            file=sys.stderr,
        )
    return 0


def _cmd_drain(args: argparse.Namespace) -> int:
    if not args.pending:
        print('drain currently only supports --pending.', file=sys.stderr)
        return 2
    drained = drain_pending_puts(limit=args.limit, wait=True)
    remaining = get_transport_status().pending_puts
    print(f'pending_puts drain complete: drained={drained}, remaining={remaining}')
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    if args.force_id:
        if len(args.force_id) != 32:
            print('--force-id requires a full 32-character observation_id.', file=sys.stderr)
            return 2
        obs_removed, tomb_removed = physical_delete_observation(args.force_id)
        parts = []
        if obs_removed:
            parts.append('obs')
        if tomb_removed:
            parts.append('tomb')
        if parts:
            print(f'physically deleted ({", ".join(parts)}) + broadcast purge: {args.force_id}')
        else:
            # No local match, but the broadcast wildcard delete may still have
            # purged a reachable peer's copy — treat as success so scripts do
            # not retry or misinterpret a completed emergency purge as failure.
            print(
                f'observation_id {args.force_id} not present on this replica. '
                'broadcast purge already sent (best-effort). '
                'For full coverage, run the same command on other peers.',
            )
        return 0
    if args.by_pc_id:
        return _cmd_gc_by_pc_id(args)
    purged = gc_expired_tombstones(retention_days=args.retention_days, project=args.project or '')
    project_note = f' (project={args.project})' if args.project else ''
    print(f'retention {args.retention_days}-day tombstones{project_note}: physically deleted {purged} entries')
    return 0


def _cmd_gc_by_pc_id(args: argparse.Namespace) -> int:
    if len(args.by_pc_id) != 32:
        print('--by-pc-id requires a 32-character pc_id.', file=sys.stderr)
        return 2

    print(
        f'scanning mem/obs/** pc_id={args.by_pc_id!r}'
        + (f' session_prefix={args.session_prefix!r}' if args.session_prefix else ''),
        file=sys.stderr,
    )
    matches, sessions = scan_obs_by_pc_id(args.by_pc_id, session_prefix=args.session_prefix or '')
    print(f'matched obs: {len(matches)} entries', file=sys.stderr)
    if not matches:
        print('no targets — no observations matched the pc_id.')
        return 0
    print('session breakdown:', file=sys.stderr)
    for sid, count in sessions.most_common():
        print(f'  {sid!r:>40}: {count}', file=sys.stderr)
    if not args.execute:
        print('Dry run — pass --execute to actually delete.')
        return 0

    # Interactive confirm gate before any destructive call. ``--yes`` skips
    # it for non-interactive ops (CI, scripted bulk purges where the
    # operator already audited the dry-run output upstream).
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                '--execute requires interactive confirmation. Pass --yes for non-interactive use.',
                file=sys.stderr,
            )
            return 2
        prompt = f"Physically delete {len(matches)} obs for pc_id={args.by_pc_id}? type 'yes' to confirm: "
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print('\ncancelled.', file=sys.stderr)
            return 1
        if answer != 'yes':
            print('cancelled.', file=sys.stderr)
            return 1

    def _on_progress(i: int, total: int, purged: int, failures: int) -> None:
        print(f'  progress: {i}/{total} (purged={purged}, fail={failures})', file=sys.stderr)

    purged, tombs_purged, failures = execute_bulk_purge(matches, on_progress=_on_progress)
    print(
        f'physically deleted: obs={purged}, tombs={tombs_purged}, failures={failures}'
        ' (tomb sweep / broadcast skipped)',
    )
    return 0 if failures == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='mesh-mem', description='mesh-mem CLI')
    parser.add_argument('--version', action='version', version=f'mesh-mem {__version__}')
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help=(
            'Rebuild the SQLite index from zenoh on first startup. '
            'CLI is one-shot and skips by default (#38); '
            'pass this when the index is empty and you want search to work, or during CI verification. '
            'MESH_MEM_FORCE_REBUILD=1 has the same effect.'
        ),
    )
    sub = parser.add_subparsers(dest='command', required=True)

    _MEMORY_TYPES = sorted(VALID_MEMORY_TYPES)  # noqa: N806

    p_save = sub.add_parser('save', help='Save an observation')
    p_save.add_argument('content', help='content to save')
    p_save.add_argument('-p', '--project', default='')
    p_save.add_argument('-t', '--tags', default='', help='comma-separated tags')
    p_save.add_argument(
        '--memory-type',
        dest='memory_type',
        default='note',
        choices=_MEMORY_TYPES,
        help='memory category (default: note)',
    )
    p_save.add_argument(
        '--importance',
        type=int,
        default=2,
        choices=range(1, 6),
        metavar='1-5',
        help='importance 1-5 (default: 2)',
    )
    p_save.add_argument('--subject', default='', help='short topic name')
    p_save.add_argument('--summary', default='', help='one-line summary shown in search results')
    p_save.add_argument(
        '--source-files',
        dest='source_files',
        default='',
        help='related file paths (comma-separated)',
    )
    p_save.add_argument(
        '--supersedes',
        default='',
        help='observation_ids this entry replaces (comma-separated, 32-char hex)',
    )
    p_save.set_defaults(func=_cmd_save)

    p_search = sub.add_parser('search', help='Search memories')
    p_search.add_argument('query', nargs='?', default='', help='search keyword (optional)')
    p_search.add_argument('--agent-family', dest='agent_family', default='')
    p_search.add_argument('--client-id', dest='client_id', default='')
    p_search.add_argument('--pc-id', dest='pc_id', default='')
    p_search.add_argument('--session-id', dest='session_id', default='')
    p_search.add_argument('-p', '--project', default='')
    p_search.add_argument('--since', default='', help='limit to ISO8601 timestamp and later')
    p_search.add_argument('-n', '--limit', type=int, default=50, help='max results (default: 50)')
    p_search.add_argument('--format', choices=_SEARCH_FORMATS, default='text', help='output format (default: text)')
    p_search.set_defaults(func=_cmd_search)

    p_delete = sub.add_parser('delete', help='Soft-delete an observation (tombstone)')
    p_delete.add_argument('observation_id', nargs='?', default='', help='full 32-character observation_id')
    p_delete.add_argument('-p', '--project', default='', help='tombstone observations in the given project')
    p_delete.add_argument('--pc-id', dest='pc_id', default='', help='tombstone observations from the given pc_id')
    p_delete.add_argument('--since', default='', help='limit to ISO8601 timestamp and later')
    p_delete.add_argument('--until', default='', help='limit to ISO8601 timestamp and earlier')
    p_delete.add_argument('--dry-run', action='store_true', help='show count only, do not delete')
    p_delete.add_argument('--yes', action='store_true', help='skip interactive confirmation for bulk delete')
    p_delete.add_argument('-r', '--reason', default='')
    p_delete.set_defaults(func=_cmd_delete)

    p_status = sub.add_parser('status', help='Show memory status')
    p_status.set_defaults(func=_cmd_status)

    p_drain = sub.add_parser('drain', help='Drain pending_puts')
    p_drain.add_argument('--pending', action='store_true', help='replay queued rows in pending_puts.db')
    p_drain.add_argument('--limit', type=_positive_int, default=None, help='max rows drained per invocation')
    p_drain.set_defaults(func=_cmd_drain)

    p_gc = sub.add_parser('gc', help='Physically delete tombstoned entries (retention / --force-id / --by-pc-id)')
    p_gc.add_argument(
        '--force-id',
        dest='force_id',
        default='',
        help=(
            'physically delete observation_id by 32-char exact match '
            '(emergency-purge for sensitive data; --project is ignored)'
        ),
    )
    p_gc.add_argument(
        '--retention-days',
        dest='retention_days',
        type=int,
        default=30,
        help='retention in days (default 30). Tombstones and their obs older than this are physically deleted.',
    )
    p_gc.add_argument(
        '-p',
        '--project',
        default='',
        help='only delete tombstones for the given project (default: all projects)',
    )
    p_gc.add_argument(
        '--by-pc-id',
        dest='by_pc_id',
        default='',
        help=(
            'bulk-physically-delete obs matching a 32-char pc_id (for cleaning up bench/spam). '
            'Default is dry-run; pass --execute to actually delete. Skips tomb sweep / broadcast.'
        ),
    )
    p_gc.add_argument(
        '--session-prefix',
        dest='session_prefix',
        default='',
        help='used with --by-pc-id; narrow further by session_id prefix (e.g. "bench")',
    )
    p_gc.add_argument(
        '--execute',
        action='store_true',
        help='disable --by-pc-id dry-run and actually delete',
    )
    p_gc.add_argument(
        '--yes',
        action='store_true',
        help='skip interactive confirmation for --by-pc-id --execute (for CI / automation)',
    )
    p_gc.set_defaults(func=_cmd_gc)

    p_get = sub.add_parser('get-memory', help='Get a single observation by observation_id')
    p_get.add_argument('observation_id', help='full 32-character observation_id')
    p_get.set_defaults(func=_cmd_get_memory)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch to the matching subcommand handler.

    The CLI is a one-shot process; the per-startup ``rebuild_from_zenoh``
    scan adds ~15s on a populated mesh (#38) and the local SQLite index
    converges via the replication subscriber anyway. Default to skipping
    that rebuild and only opt back in via ``--rebuild`` (or
    ``MESH_MEM_FORCE_REBUILD=1`` at the env layer).

    ``--rebuild`` uses the explicit-override channel so it wins over
    ambient ``MESH_MEM_SKIP_REBUILD=1`` in shell profiles / wrappers —
    a flag the user typed on this exact invocation must beat env-level
    ambient config (codex review P2).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.rebuild:
        set_rebuild_on_init_explicit(True)
    else:
        set_rebuild_on_init_default(False)
    try:
        return args.func(args)
    finally:
        stop_pending_drain_background()
        # Explicitly close the cached Zenoh session before sys.exit. Without
        # this, the CLI hangs after printing output because the session's
        # replication subscriber thread keeps the interpreter alive past
        # the command's return; users had to ctrl-c to escape.
        _reset_session()


if __name__ == '__main__':
    sys.exit(main())

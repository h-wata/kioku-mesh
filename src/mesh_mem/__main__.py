"""Command-line interface for mesh-mem.

Thin wrapper over the same store primitives the MCP server uses.
``gc`` performs physical delete via ``session.delete`` — the Zenoh storage
backend is expected to propagate the removal through replication so both
sides converge.
"""

import argparse
from collections.abc import Callable
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import socket
import sys

try:
    import argcomplete
except ImportError:  # argcomplete is an optional extra (`pip install mesh-mem[completion]`).
    argcomplete = None  # type: ignore[assignment]

from . import __version__
from . import doctor as doctor_module
from .identity import get_pc_id
from .identity import get_session_id
from .local_index import LocalIndex
from .models import Observation
from .models import VALID_MEMORY_TYPES
from .store import _reset_session
from .store import drain_pending_puts
from .store import execute_bulk_purge
from .store import find_observation_by_id
from .store import gc_expired_shadows
from .store import gc_expired_tombstones
from .store import get_index
from .store import get_session
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


def _iter_delete_targets(args: argparse.Namespace, *, batch_size: int) -> Iterator[Observation]:
    """Yield bulk-delete targets in (created_at, observation_id) DESC order.

    Pages over :func:`search_observations` using the strict
    ``(created_at, observation_id)`` tuple cursor so the 10k
    :data:`MAX_SEARCH` per-call cap does not abort large bulk deletes
    (#66). Each page is at most ``min(batch_size, MAX_SEARCH)`` rows; the
    next call passes the last yielded row's ``(created_at,
    observation_id)`` as ``(until_iso, cursor_observation_id)``, which
    :meth:`LocalIndex.search` translates into
    ``(created_at, observation_id) < cursor`` — so ties on the boundary
    timestamp walk correctly even when more rows share the timestamp
    than fit in a single batch (#66 review).
    """
    page_size = max(1, min(batch_size, MAX_SEARCH))
    until_cursor = args.until or ''
    cursor_obs_id = ''
    while True:
        page = search_observations(
            project=args.project or '',
            pc_id=args.pc_id or '',
            since_iso=args.since or '',
            until_iso=until_cursor,
            cursor_observation_id=cursor_obs_id,
            limit=page_size,
        )
        if not page:
            return
        for obs in page:
            yield obs
        if len(page) < page_size:
            return
        last = page[-1]
        until_cursor = last.created_at
        cursor_obs_id = last.observation_id


def _count_delete_targets(args: argparse.Namespace, *, batch_size: int) -> tuple[int, str | None]:
    """Return ``(total_count, error_message)`` for bulk-delete preflight.

    Streams via :func:`_iter_delete_targets` so the count is uncapped
    (no :data:`MAX_SEARCH` abort). Used by ``--dry-run`` and to size the
    interactive confirmation prompt.
    """
    if args.until and _parse_iso_or_none(args.until or '') is None:
        return 0, '--until must be in ISO8601 format.'
    total = 0
    for _ in _iter_delete_targets(args, batch_size=batch_size):
        total += 1
    return total, None


def _cmd_save(args: argparse.Namespace) -> int:
    tag_list = [t.strip() for t in (args.tags or '').split(',') if t.strip()]
    source_files = _parse_csv(args.source_files) if args.source_files else []
    references = _parse_csv(args.references) if args.references else []
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
        references=references,
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
    refs_part = f' (refs: {", ".join(obs.references)})' if obs.references else ''
    return (
        f'[{obs.memory_type}][{obs.importance}] {obs.created_at[:19]}'
        f'{project_part}{subject_part}{refs_part}\n'
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
    refs_part = f' (refs: {", ".join(obs.references)})' if obs.references else ''
    body = _format_search_markdown_body(obs)
    return (
        f'- **[{obs.memory_type}][{obs.importance}]** '
        f'{obs.created_at[:16]}{project_part} '
        f'{body}{refs_part} <id={obs.observation_id}>'
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
        f'references: {", ".join(obs.references) if obs.references else "-"}',
        f'supersedes: {", ".join(obs.supersedes) if obs.supersedes else "-"}',
        '---',
        obs.content,
    ]
    print('\n'.join(lines))
    return 0


_LOCAL_INDEX_HINT = (
    'hint: rows visible only in the local index (gc --by-pc-id matches 0) are\n'
    '      better cleaned with: mesh-mem --rebuild gc --retention-days 0 --project <name>'
)


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
        batch_size = args.batch_size
        total, error = _count_delete_targets(args, batch_size=batch_size)
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
        print(f'bulk delete target: {total} entries ({selector_text})', file=sys.stderr)
        if total == 0:
            print('no targets — no observations matched the selector.')
            print(_LOCAL_INDEX_HINT, file=sys.stderr)
            return 0
        if args.dry_run:
            print('Dry run — pass --yes to actually delete.')
            print(_LOCAL_INDEX_HINT, file=sys.stderr)
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    'bulk delete requires interactive confirmation. Pass --yes for non-interactive use.',
                    file=sys.stderr,
                )
                return 2
            if total > MAX_SEARCH:
                # The pre-#66 hard cap is gone; surface the same "large set"
                # warning so an operator can't tombstone 100k rows by hitting
                # enter once. The local-index hint is the cheap-out path for
                # the common "Zenoh already aged it out" case.
                print(
                    f'NOTE: large bulk delete ({total} > {MAX_SEARCH} entries). '
                    'If targets exist only in the local index, '
                    '`mesh-mem --rebuild gc --retention-days 0 --project ...` is faster.',
                    file=sys.stderr,
                )
            prompt = f"Tombstone {total} entries? type 'yes' to confirm: "
            try:
                answer = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print('\ncancelled.', file=sys.stderr)
                return 1
            if answer != 'yes':
                print('cancelled.', file=sys.stderr)
                return 1

        # Stream a second pass; the page contents may shift slightly under
        # concurrent writes but cursor pagination keeps progress monotone.
        deleted = 0
        failures = 0
        next_progress = batch_size
        for obs in _iter_delete_targets(args, batch_size=batch_size):
            try:
                put_tombstone(obs, reason=args.reason or '')
                deleted += 1
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the sweep (#66)
                failures += 1
                print(f'  put_tombstone failed for {obs.observation_id}: {e}', file=sys.stderr)
            if deleted + failures >= next_progress:
                print(
                    f'  progress: {deleted + failures}/{total} (ok={deleted}, fail={failures})',
                    file=sys.stderr,
                )
                next_progress += batch_size
        suffix = f' ({failures} failures)' if failures else ''
        print(f'deleted (tombstone): {deleted} entries{suffix}')
        print(_LOCAL_INDEX_HINT, file=sys.stderr)
        return 0 if failures == 0 else 1

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
    # Shadow sweep depends on ``shadowed_at`` being current. CLI startup skips
    # ``rebuild_from_zenoh`` by default (#38), so a one-shot ``mesh-mem gc``
    # would otherwise miss "stale-but-not-yet-shadowed" local rows that the
    # subscriber never had a chance to reconcile. Run reconcile explicitly
    # before the sweep when shadow prune is on so the discovery path is
    # closed (#70). On reconcile failure we fall through conservatively:
    # the sweep then only sees pre-existing shadow rows, which is no worse
    # than the previous behavior.
    if not args.no_shadow_prune:
        try:
            get_index().rebuild_from_zenoh(get_session())
        except Exception as e:  # noqa: BLE001
            print(
                f'rebuild_from_zenoh skipped before gc shadow sweep: {type(e).__name__}: {e}',
                file=sys.stderr,
            )
    purged_tomb = gc_expired_tombstones(retention_days=args.retention_days, project=args.project or '')
    project_note = f' (project={args.project})' if args.project else ''
    if args.no_shadow_prune:
        print(
            f'retention {args.retention_days}-day sweep{project_note}: '
            f'physically deleted {purged_tomb} tombstones (shadow prune skipped)'
        )
        return 0
    purged_shadow, revived_shadow = gc_expired_shadows(retention_days=args.retention_days, project=args.project or '')
    print(
        f'retention {args.retention_days}-day sweep{project_note}: '
        f'physically deleted {purged_tomb} tombstones / {purged_shadow} shadows '
        f'(revived {revived_shadow})'
    )
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


def _distinct_values_from_local_index(method_name: str) -> list[str]:
    """Open the local SQLite index, call ``method_name``, and close cleanly.

    Completion runs inside a shell subprocess — touching Zenoh or triggering
    ``rebuild_from_zenoh`` would blow the latency budget. ``LocalIndex.connect``
    is pure SQLite and degrades to a disabled instance on open failure, so a
    missing / unreadable index just yields no suggestions instead of crashing
    the completer.
    """
    try:
        idx = LocalIndex.connect()
        try:
            return list(getattr(idx, method_name)())
        finally:
            idx.close()
    except Exception:  # noqa: BLE001 — completion must never raise
        return []


def _complete_project(prefix: str, **_kwargs) -> list[str]:
    """Argcomplete callback: suggest ``--project`` values from the local index."""
    return [v for v in _distinct_values_from_local_index('distinct_projects') if v.startswith(prefix)]


def _complete_pc_id(prefix: str, **_kwargs) -> list[str]:
    """Argcomplete callback: suggest ``--pc-id`` / ``--by-pc-id`` values."""
    return [v for v in _distinct_values_from_local_index('distinct_pc_ids') if v.startswith(prefix)]


def _attach_completer(action: argparse.Action, completer: Callable[..., list[str]]) -> None:
    """Attach an argcomplete completer to an argparse Action, no-op if argcomplete is absent."""
    if argcomplete is None:
        return
    action.completer = completer  # type: ignore[attr-defined]


_INIT_MODES = ('localhost', 'hub', 'spoke')
_DEFAULT_ZENOH_PORT = 7447


def _default_init_path() -> Path:
    """Return the XDG-aware default output path for ``mesh-mem init``."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'mesh-mem' / 'zenohd.json5'


# Probe destinations span: public internet (default route), the three RFC1918
# blocks (LAN), and the CGNAT 100.64/10 range (Tailscale and similar overlay
# networks). UDP connect() resolves the kernel's preferred source IP for each
# destination without sending a packet — so a host with eth0/wlan0/tailscale0
# gets one source IP per probe hit. Offline hosts return [], and the
# interactive picker still offers 127.0.0.1 + 0.0.0.0 + custom.
_LOCAL_IPV4_PROBES: tuple[str, ...] = (
    '8.8.8.8',
    '192.168.1.1',
    '10.0.0.1',
    '172.16.0.1',
    '100.64.0.1',
)


def _detect_local_ipv4() -> list[str]:
    """Return non-loopback IPv4 addresses on this host (best-effort, stdlib-only).

    Limitation: each probe yields the source IP for ONE route. Hosts with two
    interfaces in the same subnet (rare) only surface one of them — users can
    fall back to ``custom`` in the picker or pass ``--listen`` explicitly.
    """
    found: list[str] = []

    def _add(ip: str) -> None:
        if ip.startswith('127.') or ip in found:
            return
        found.append(ip)

    for dest in _LOCAL_IPV4_PROBES:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((dest, 80))
                _add(s.getsockname()[0])
        except OSError:
            continue
    # Hostname resolution catches VPN / tunnel interfaces that don't match any
    # probe subnet (e.g. site-to-site IPSec on a custom prefix).
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            ip = entry[4][0]
            if isinstance(ip, str):
                _add(ip)
    except (socket.gaierror, OSError):
        pass
    return found


def _normalize_endpoint(spec: str, default_port: int = _DEFAULT_ZENOH_PORT) -> str:
    """Normalize a user-supplied endpoint to ``tcp/<host>:<port>``."""
    spec = spec.strip()
    if not spec:
        raise ValueError('endpoint must not be empty')
    if spec.startswith(('tcp/', 'udp/')):
        return spec
    if ':' not in spec:
        return f'tcp/{spec}:{default_port}'
    return f'tcp/{spec}'


def _dedupe_endpoints(endpoints: list[str]) -> list[str]:
    """Drop duplicate endpoints while preserving the user's ordering.

    zenohd refuses to start if ``listen.endpoints`` binds the same socket twice
    (and the error surfaces well after init). Catch it here so the generated
    config is always startable.
    """
    seen: set[str] = set()
    result: list[str] = []
    for ep in endpoints:
        if ep in seen:
            continue
        seen.add(ep)
        result.append(ep)
    return result


def _prompt_listen_endpoints(detected: list[str]) -> list[str]:
    """Interactive listen-endpoint picker. Returns normalized, deduplicated ``tcp/...`` strings."""
    options: list[tuple[str, str]] = [('127.0.0.1', 'loopback only — single-host testing')]
    for ip in detected:
        options.append((ip, 'detected interface'))
    options.append(('0.0.0.0', 'all interfaces — firewall recommended'))
    options.append(('custom', 'enter manually'))
    print('\nSelect listen endpoint(s) (comma-separated for multiple, e.g. "1,2"):', file=sys.stderr)
    for idx, (ip, note) in enumerate(options, 1):
        print(f'  {idx}) {ip:<20} ({note})', file=sys.stderr)
    raw = input('> ').strip()
    if not raw:
        raise ValueError('no selection made')
    picks: list[str] = []
    for token in raw.split(','):
        token = token.strip()
        if not token:
            continue
        try:
            idx = int(token)
        except ValueError as e:
            raise ValueError(f'invalid selection: {token!r}') from e
        if idx < 1 or idx > len(options):
            raise ValueError(f'selection {idx} out of range')
        ip, _ = options[idx - 1]
        if ip == 'custom':
            ip = input('  custom endpoint (ip[:port] or tcp/ip:port): ').strip()
            if not ip:
                raise ValueError('custom endpoint must not be empty')
        picks.append(_normalize_endpoint(ip))
    picks = _dedupe_endpoints(picks)
    if not picks:
        raise ValueError('no listen endpoint selected')
    return picks


def _format_endpoint_list(endpoints: list[str], indent: str = '      ') -> str:
    """Format endpoints as JSON5 array body lines."""
    return (',\n' + indent).join(f'"{ep}"' for ep in endpoints)


def _render_localhost_config(listen_endpoints: list[str]) -> str:
    """Render a single-host (loopback) zenohd config in JSON5 form."""
    listen_lines = _format_endpoint_list(listen_endpoints)
    return (
        '// Generated by `mesh-mem init` — single-host (loopback) config.\n'
        '// memory volume: data does NOT survive a zenohd restart.\n'
        '// Multicast scouting disabled so nothing leaks onto the LAN.\n'
        '\n'
        '{\n'
        '  mode: "router",\n'
        '\n'
        '  listen: {\n'
        f'    endpoints: [\n      {listen_lines},\n    ],\n'
        '  },\n'
        '\n'
        '  scouting: {\n'
        '    multicast: { enabled: false },\n'
        '  },\n'
        '\n'
        '  timestamping: {\n'
        '    enabled: { router: true, peer: true, client: true },\n'
        '  },\n'
        '\n'
        '  plugins: {\n'
        '    storage_manager: {\n'
        '      volumes: {\n'
        '        memory: {},\n'
        '      },\n'
        '      storages: {\n'
        '        agent_mem: {\n'
        '          key_expr: "mem/**",\n'
        '          strip_prefix: "mem",\n'
        '          volume: "memory",\n'
        '        },\n'
        '      },\n'
        '    },\n'
        '  },\n'
        '}\n'
    )


def _render_mesh_config(mode: str, listen_endpoints: list[str], connect_endpoints: list[str]) -> str:
    """Render a hub-or-spoke zenohd config with rocksdb + replication."""
    listen_lines = _format_endpoint_list(listen_endpoints)
    if connect_endpoints:
        connect_lines = _format_endpoint_list(connect_endpoints)
        connect_block = f'    endpoints: [\n      {connect_lines},\n    ],'
    else:
        connect_block = '    endpoints: [],'
    role_note = 'hub: spokes dial in' if mode == 'hub' else 'spoke: dials the hub'
    return (
        f'// Generated by `mesh-mem init --mode {mode}` — {role_note}.\n'
        '// rocksdb volume + replication. Requires `zenoh-backend-rocksdb` plugin and\n'
        '// ZENOH_BACKEND_ROCKSDB_ROOT pointing at a writable directory.\n'
        '// Replication block must match byte-for-byte across all peers.\n'
        '\n'
        '{\n'
        '  mode: "router",\n'
        '\n'
        '  listen: {\n'
        f'    endpoints: [\n      {listen_lines},\n    ],\n'
        '  },\n'
        '\n'
        '  connect: {\n'
        f'{connect_block}\n'
        '  },\n'
        '\n'
        '  timestamping: {\n'
        '    enabled: { router: true, peer: true, client: true },\n'
        '  },\n'
        '\n'
        '  plugins: {\n'
        '    storage_manager: {\n'
        '      volumes: {\n'
        '        rocksdb: {},\n'
        '      },\n'
        '      storages: {\n'
        '        agent_mem: {\n'
        '          key_expr: "mem/**",\n'
        '          strip_prefix: "mem",\n'
        '          replication: {\n'
        '            interval: 10.0,\n'
        '            sub_intervals: 5,\n'
        '            hot: 6,\n'
        '            warm: 30,\n'
        '            propagation_delay: 250,\n'
        '          },\n'
        '          volume: {\n'
        '            id: "rocksdb",\n'
        '            dir: "agent_mem",\n'
        '            create_db: true,\n'
        '          },\n'
        '        },\n'
        '      },\n'
        '    },\n'
        '  },\n'
        '}\n'
    )


def _resolve_listen_endpoints(args: argparse.Namespace, detected: list[str]) -> list[str]:
    """Resolve listen endpoints from flags / interactive picker / mode default."""
    if args.listen:
        return _dedupe_endpoints([_normalize_endpoint(spec) for spec in args.listen])
    if args.mode == 'localhost':
        return [_normalize_endpoint('127.0.0.1')]
    if sys.stdin.isatty():
        return _prompt_listen_endpoints(detected)
    raise ValueError(f'--listen required for --mode {args.mode} in non-interactive use')


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run diagnostic checks and report PASS / WARN / FAIL with hints."""
    results = doctor_module.run_all_checks()
    if args.json:
        print(doctor_module.to_json(results))
    else:
        print(doctor_module.format_text(results))
    return doctor_module.exit_code_for(doctor_module.worst_status(results))


def _cmd_init(args: argparse.Namespace) -> int:
    detected = _detect_local_ipv4()
    try:
        listen = _resolve_listen_endpoints(args, detected)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    connect = _dedupe_endpoints([_normalize_endpoint(spec) for spec in (args.connect or [])])
    if args.mode == 'spoke' and not connect:
        print('error: --connect required for --mode spoke (the hub endpoint to dial).', file=sys.stderr)
        return 2
    if args.mode == 'localhost' and connect:
        print('error: --connect is not used with --mode localhost.', file=sys.stderr)
        return 2

    # Loopback in listen is what makes local MCP clients reach this router on the
    # default ``ZENOH_CONNECT=tcp/127.0.0.1:7447``. Don't add it silently; surface
    # the gap so the user knows ZENOH_CONNECT will need to point elsewhere.
    if args.mode in ('hub', 'spoke') and not any('127.0.0.1' in ep for ep in listen):
        print(
            'note: 127.0.0.1 is not in --listen; local MCP clients will need '
            'ZENOH_CONNECT pointed at one of the listed endpoints.',
            file=sys.stderr,
        )

    if args.mode == 'localhost':
        body = _render_localhost_config(listen)
    else:
        body = _render_mesh_config(args.mode, listen, connect)

    if args.to_stdout:
        sys.stdout.write(body)
        return 0

    out = Path(args.out) if args.out else _default_init_path()
    if out.exists() and not args.force:
        print(f'error: {out} already exists. Use --force to overwrite.', file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding='utf-8')
    print(f'wrote {out}')
    print(f'next: zenohd -c {out}')
    if args.mode in ('hub', 'spoke'):
        print('also: export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"')
    return 0


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
    _attach_completer(p_save.add_argument('-p', '--project', default=''), _complete_project)
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
        '--references',
        default='',
        help='related PR / Issue ids (comma-separated, e.g. "#67,PR#68,org/repo#42")',
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
    _attach_completer(p_search.add_argument('--pc-id', dest='pc_id', default=''), _complete_pc_id)
    p_search.add_argument('--session-id', dest='session_id', default='')
    _attach_completer(p_search.add_argument('-p', '--project', default=''), _complete_project)
    p_search.add_argument('--since', default='', help='limit to ISO8601 timestamp and later')
    p_search.add_argument('-n', '--limit', type=int, default=50, help='max results (default: 50)')
    p_search.add_argument('--format', choices=_SEARCH_FORMATS, default='text', help='output format (default: text)')
    p_search.set_defaults(func=_cmd_search)

    p_delete = sub.add_parser('delete', help='Soft-delete an observation (tombstone)')
    p_delete.add_argument('observation_id', nargs='?', default='', help='full 32-character observation_id')
    _attach_completer(
        p_delete.add_argument('-p', '--project', default='', help='tombstone observations in the given project'),
        _complete_project,
    )
    _attach_completer(
        p_delete.add_argument('--pc-id', dest='pc_id', default='', help='tombstone observations from the given pc_id'),
        _complete_pc_id,
    )
    p_delete.add_argument('--since', default='', help='limit to ISO8601 timestamp and later')
    p_delete.add_argument('--until', default='', help='limit to ISO8601 timestamp and earlier')
    p_delete.add_argument('--dry-run', action='store_true', help='show count only, do not delete')
    p_delete.add_argument('--yes', action='store_true', help='skip interactive confirmation for bulk delete')
    p_delete.add_argument(
        '--batch-size',
        dest='batch_size',
        type=_positive_int,
        default=1000,
        help='bulk delete page / progress granularity (default 1000, max 10000)',
    )
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
    _attach_completer(
        p_gc.add_argument(
            '-p',
            '--project',
            default='',
            help='only delete tombstones for the given project (default: all projects)',
        ),
        _complete_project,
    )
    _attach_completer(
        p_gc.add_argument(
            '--by-pc-id',
            dest='by_pc_id',
            default='',
            help=(
                'bulk-physically-delete obs matching a 32-char pc_id (for cleaning up bench/spam). '
                'Default is dry-run; pass --execute to actually delete. Skips tomb sweep / broadcast.'
            ),
        ),
        _complete_pc_id,
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
    p_gc.add_argument(
        '--no-shadow-prune',
        dest='no_shadow_prune',
        action='store_true',
        help='Skip physical deletion of shadow rows during retention sweep (tombstones only).',
    )
    p_gc.set_defaults(func=_cmd_gc)

    p_get = sub.add_parser('get-memory', help='Get a single observation by observation_id')
    p_get.add_argument('observation_id', help='full 32-character observation_id')
    p_get.set_defaults(func=_cmd_get_memory)

    p_init = sub.add_parser(
        'init',
        help='Generate a starter zenohd config under ~/.config/mesh-mem/',
    )
    p_init.add_argument(
        '--mode',
        default='localhost',
        choices=_INIT_MODES,
        help='localhost (loopback+memory, default) / hub (LAN listener) / spoke (dials a hub)',
    )
    p_init.add_argument(
        '--listen',
        action='append',
        default=[],
        metavar='ENDPOINT',
        help='listen endpoint (repeatable, e.g. tcp/0.0.0.0:7447). Interactive picker if omitted.',
    )
    p_init.add_argument(
        '--connect',
        action='append',
        default=[],
        metavar='ENDPOINT',
        help='connect endpoint (repeatable). Required for --mode spoke.',
    )
    p_init.add_argument(
        '--out',
        default='',
        help=f'output path (default: {_default_init_path()})',
    )
    p_init.add_argument('--force', action='store_true', help='overwrite if the target file exists')
    p_init.add_argument(
        '--print',
        dest='to_stdout',
        action='store_true',
        help='write to stdout instead of file',
    )
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser(
        'doctor',
        help='Run diagnostic checks (zenohd reachable, config present, state dir healthy)',
    )
    p_doctor.add_argument(
        '--json',
        action='store_true',
        help='emit machine-readable JSON instead of human-readable text',
    )
    p_doctor.set_defaults(func=_cmd_doctor)

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
    if argcomplete is not None:
        # Must run before parse_args. When the shell is asking for completion
        # candidates (``_ARGCOMPLETE`` in env) argcomplete writes results and
        # exits the process; in normal invocation it is a no-op.
        argcomplete.autocomplete(parser)
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

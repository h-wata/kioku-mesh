"""Command-line interface for kioku-mesh.

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
import shlex
import shutil
import socket
import subprocess
import sys

try:
    import argcomplete
except ImportError:  # argcomplete is an optional extra (`pip install kioku-mesh[completion]`).
    argcomplete = None  # type: ignore[assignment]

from . import __version__
from . import doctor as doctor_module
from . import mcp_install as mcp_install_module
from . import tls as tls_module
from . import zenohd_install as zenohd_install_module
from .backend import get_backend
from .backend import reset_backend
from .config import format_visibility
from .config import resolve_write_visibility
from .config import write_local_config
from .identity import get_pc_id
from .identity import get_session_id
from .identity import IdentitySource
from .identity import resolve_agent_family
from .identity import resolve_client_id
from .local_index import LocalIndex
from .mcp_install import MCPClient
from .models import Observation
from .models import VALID_MEMORY_TYPES
from .paths import data_share_leaf
from .paths import resolve_app_dir
from .store import _reset_session
from .store import execute_bulk_purge
from .store import get_index
from .store import get_session
from .store import MAX_SEARCH
from .store import mesh_ready_label
from .store import scan_obs_by_pc_id
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
        page = get_backend().search_observations(
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
    try:
        visibility, scope_id = resolve_write_visibility(getattr(args, 'visibility', '') or '')
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
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
        visibility=visibility,
        scope_id=scope_id,
    )
    backend = get_backend()
    backend.put_observation(obs)
    print(f'saved: {obs.observation_id} (visibility={format_visibility(visibility, scope_id)})')
    # ADR-0026 §A: suggest-first supersede detection. Only when the caller
    # did not already declare what this entry replaces — if they passed
    # --supersedes they have handled it explicitly.
    if not supersedes:
        try:
            candidates = backend.find_supersede_candidates(obs)
            for line in _format_supersede_hint(candidates):
                print(line)
        except Exception:  # noqa: BLE001 — detection is best-effort, never fail a save
            pass
    return 0


def _format_supersede_hint(candidates: list[Observation]) -> list[str]:
    """Render the ADR-0026 supersede-candidate hint, or [] when there are none.

    Shared by the CLI ``save`` text output; the MCP tool builds an
    equivalent structured payload instead.
    """
    if not candidates:
        return []
    lines = [
        f'supersede candidates ({len(candidates)} live entr'
        f'{"y" if len(candidates) == 1 else "ies"} with the same subject/type):'
    ]
    for c in candidates:
        body = c.summary or _truncate_search_content(c.content, 60)
        lines.append(f'  - {c.observation_id}  {c.created_at[:10]}  {body}')
    ids = ','.join(c.observation_id for c in candidates)
    lines.append(
        '  hint: if this revises them, save with --supersedes '
        f'{ids} so the older one is hidden, or `kioku-mesh delete <id>` to retire it.'
    )
    return lines


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
    results = get_backend().search_observations(
        query=args.query or '',
        agent_family=args.agent_family or '',
        client_id=args.client_id or '',
        pc_id=args.pc_id or '',
        session_id=args.session_id or '',
        project=args.project or '',
        since_iso=args.since or '',
        limit=args.limit,
        search_mode=args.search_mode,
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
    obs = get_backend().find_observation_by_id(args.observation_id)
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
    '      better cleaned with: kioku-mesh --rebuild gc --retention-days 0 --project <name>'
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
                    '`kioku-mesh --rebuild gc --retention-days 0 --project ...` is faster.',
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
                get_backend().put_tombstone(obs, reason=args.reason or '')
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
    obs = get_backend().find_observation_by_id(args.observation_id)
    if obs is None:
        print(f'observation_id {args.observation_id} not found.', file=sys.stderr)
        return 1
    get_backend().put_tombstone(obs, reason=args.reason or '')
    print(f'deleted (tombstone): {args.observation_id}')
    return 0


def _format_identity_source(source: IdentitySource, env_var: str) -> str:
    """Format the provenance suffix shown after an identity value in `status`."""
    if source is IdentitySource.ENV:
        return f'(from {env_var})'
    if source is IdentitySource.DETECTED:
        return '(auto-detected)'
    return f'(default — set {env_var} to override)'


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        recent = get_backend().search_observations(limit=MAX_SEARCH)
    except Exception as e:  # noqa: BLE001
        print(f'failed to read shared memory [{type(e).__name__}]: {e}', file=sys.stderr)
        return 1
    status = get_backend().get_status()
    by_family: dict[str, int] = {}
    by_pc: dict[str, int] = {}
    for obs in recent:
        by_family[obs.agent_family] = by_family.get(obs.agent_family, 0) + 1
        by_pc[obs.pc_id] = by_pc.get(obs.pc_id, 0) + 1
    truncated = len(recent) >= MAX_SEARCH
    af_value, af_source = resolve_agent_family()
    cid_value, cid_source = resolve_client_id()
    print(f'kioku-mesh version: {__version__}')
    print(f'backend: {status.mode}')
    print(f'pc_id: {get_pc_id()}')
    print(f'session_id: {get_session_id()}')
    print(f'agent_family: {af_value} {_format_identity_source(af_source, "KIOKU_MESH_AGENT_FAMILY")}')
    print(f'client_id: {cid_value} {_format_identity_source(cid_source, "KIOKU_MESH_CLIENT_ID")}')
    print(f'zenoh_session: {status.zenoh_session}')
    print(f'last_put_at_iso: {status.last_put_at_iso or "-"}')
    print(f'last_put_status: {status.last_put_status}')
    print(f'pending_puts: {status.pending_puts}')
    print(f'observations_live: {status.live}')
    print(f'observations_tombstoned: {status.tombstoned}')
    print(f'observations_shadowed: {status.shadowed}')
    if status.shadowed > 0:
        print(
            'hint: shadowed observations exist (covered by newer obs, hidden from search but not deleted).'
            ' Run `kioku-mesh doctor` for details or `kioku-mesh status --show-shadows` to list them.'
        )
    print(f'count (within limit {MAX_SEARCH}): {len(recent)}{" (limit may be reached)" if truncated else ""}')
    for family, count in sorted(by_family.items()):
        print(f'  family {family}: {count}')
    for pc, count in sorted(by_pc.items()):
        print(f'  pc {pc[:8]}: {count}')
    if status.mode == 'zenoh':
        label = mesh_ready_label()
        print(f'mesh_ready: {label}')
        if label != 'yes':
            print(
                'WARNING: peer alignment not yet complete. Search counts may be low right after restart.',
                file=sys.stderr,
            )
    if args.show_shadows:
        backend = get_backend()
        idx = getattr(backend, '_idx', None) or get_index()
        shadows = idx.list_shadowed_obs(limit=50)
        if shadows:
            print('shadowed observations:')
            for obs_id, proj, _created_at, shadowed_at, summary in shadows:
                print(f'  {obs_id}  project={proj}  shadowed_at={shadowed_at}  summary={summary[:60]}')
        else:
            print('no shadowed observations.')
    return 0


def _cmd_drain(args: argparse.Namespace) -> int:
    if not args.pending:
        print('drain currently only supports --pending.', file=sys.stderr)
        return 2
    drained = get_backend().drain_pending(limit=args.limit, wait=True)
    remaining = get_backend().get_status().pending_puts
    print(f'pending_puts drain complete: drained={drained}, remaining={remaining}')
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    if args.force_id:
        if len(args.force_id) != 32:
            print('--force-id requires a full 32-character observation_id.', file=sys.stderr)
            return 2
        obs_removed, tomb_removed = get_backend().physical_delete_observation(args.force_id)
        parts = []
        if obs_removed:
            parts.append('obs')
        if tomb_removed:
            parts.append('tomb')
        if parts:
            print(f'physically deleted ({", ".join(parts)}): {args.force_id}')
        else:
            print(
                f'observation_id {args.force_id} not present on this replica.',
            )
        return 0
    if args.by_pc_id:
        if get_backend().get_status().mode == 'local':
            print('--by-pc-id is not supported in local mode.', file=sys.stderr)
            return 2
        return _cmd_gc_by_pc_id(args)
    # Shadow sweep for zenoh: rebuild index from zenoh first so discovery path is current.
    if not args.no_shadow_prune and get_backend().get_status().mode == 'zenoh':
        try:
            get_index().rebuild_from_zenoh(get_session())
        except Exception as e:  # noqa: BLE001
            print(
                f'rebuild_from_zenoh skipped before gc shadow sweep: {type(e).__name__}: {e}',
                file=sys.stderr,
            )
    purged_tomb = get_backend().gc_tombstones(retention_days=args.retention_days, project=args.project or '')
    project_note = f' (project={args.project})' if args.project else ''
    if args.no_shadow_prune:
        print(
            f'retention {args.retention_days}-day sweep{project_note}: '
            f'physically deleted {purged_tomb} tombstones (shadow prune skipped)'
        )
        return 0
    purged_shadow, revived_shadow = get_backend().gc_shadows(
        retention_days=args.retention_days, project=args.project or ''
    )
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


_INIT_MODES = ('local', 'hub', 'spoke')
_DEFAULT_ZENOH_PORT = 7447


def _default_init_path() -> Path:
    """Return the XDG-aware default output path for ``kioku-mesh init``."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return resolve_app_dir(Path(base)) / 'zenohd.json5'


# zenohd is invoked from systemd, which does not necessarily inherit a shell's
# PATH — resolve the binary to an absolute path at unit-generation time. Falls
# back to the most common location only when ``which`` fails, with a warning.
_SYSTEMD_ZENOHD_FALLBACK = '/usr/bin/zenohd'

_SYSTEMD_UNIT_NAME = 'kioku-mesh-zenohd.service'


def _default_systemd_user_unit_path() -> Path:
    """Return the XDG-aware path for the user-scope systemd unit."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'systemd' / 'user' / _SYSTEMD_UNIT_NAME


def _detect_systemd_user(
    run: Callable[[list[str]], 'subprocess.CompletedProcess[str]'] | None = None,
) -> tuple[bool, str]:
    """Return ``(supported, reason)`` for systemd-user availability on this host.

    Two-stage check:
    1. Platform gate + ``systemctl`` binary on PATH (cheap, catches macOS /
       Windows / non-systemd Linux).
    2. ``systemctl --user show-environment`` probe (cheap subprocess, catches
       hosts where the binary exists but the user manager is not reachable —
       WSL distros with `WSL_INTEROP` enabled, non-systemd containers, broken
       user buses). Required by #86 acceptance: hosts without working
       systemd-user must get a clear refusal rather than a broken unit file.

    The subprocess runner is injectable so tests can simulate both reachable
    and unreachable user managers without monkeypatching ``subprocess`` globally.
    """
    if sys.platform == 'darwin':
        return False, 'macOS uses launchd; systemd is not available. Run zenohd manually or via launchctl.'
    if sys.platform == 'win32':
        return False, 'Windows uses Task Scheduler; systemd is not available. Run zenohd manually or via NSSM.'
    systemctl = shutil.which('systemctl')
    if systemctl is None:
        return False, 'systemctl is not on PATH — host is likely not running systemd.'
    runner = run or _default_systemctl_probe
    try:
        result = runner([systemctl, '--user', 'show-environment'])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, (
            f'systemctl --user probe failed ({type(e).__name__}: {e}). '
            'Run inside a logged-in user session with an active systemd --user manager.'
        )
    if result.returncode != 0:
        stderr = (result.stderr or '').strip() or '(no stderr)'
        return False, (
            f'systemctl --user show-environment failed (rc={result.returncode}): {stderr}. '
            'The user manager is not reachable on this host '
            '(WSL without systemd, non-systemd container, or no active user bus).'
        )
    return True, ''


def _default_systemctl_probe(argv: list[str]) -> 'subprocess.CompletedProcess[str]':
    """Run a ``systemctl --user`` probe with a short timeout. Raises on timeout / OS error."""
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=2.0)


def _quote_systemd_value(value: str) -> str:
    r"""Quote a value for a systemd unit ExecStart line.

    systemd's command-line parser is POSIX-shell-like
    (https://www.freedesktop.org/software/systemd/man/systemd.syntax.html):
    unquoted whitespace splits arguments, double-quoted strings group an
    argument, and ``\\`` / ``"`` are backslash-escaped inside double quotes.
    Quoting ExecStart values prevents a path with spaces (e.g.
    ``--out '/home/u/My Configs/zenohd.json5'``) from getting split into
    multiple arguments at unit-load time.
    """
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _render_systemd_unit(config_path: Path, zenohd_binary: str, rocksdb_root: str) -> str:
    """Render a systemd --user unit pointing at the given kioku-mesh config.

    Mirrors the manual recipe documented in README §"systemd unit (zenohd)";
    the absolute ``zenohd`` path is baked in because the user manager does
    not inherit an interactive shell's PATH on every distro. Both the binary
    and config path are quoted so paths containing whitespace survive
    systemd's unquoted-whitespace splitter.

    ``rocksdb_root`` is the ``ZENOH_BACKEND_ROCKSDB_ROOT`` the unit sets (e.g.
    ``%h/.local/share/kioku-mesh``). The caller resolves it with the legacy
    fallback so an existing ``mesh-mem`` store is not orphaned (#128).
    """
    return (
        '# Generated by `kioku-mesh init --install-systemd`. Safe to edit; re-running\n'
        '# with --force overwrites this file.\n'
        '[Unit]\n'
        'Description=kioku-mesh zenohd router\n'
        'After=network-online.target\n'
        'Wants=network-online.target\n'
        '\n'
        '[Service]\n'
        'Type=simple\n'
        f'Environment=ZENOH_BACKEND_ROCKSDB_ROOT={rocksdb_root}\n'
        f'ExecStartPre=/usr/bin/install -d {rocksdb_root}\n'
        f'ExecStart={_quote_systemd_value(zenohd_binary)} -c {_quote_systemd_value(str(config_path))}\n'
        'Restart=on-failure\n'
        'RestartSec=5s\n'
        '\n'
        '[Install]\n'
        'WantedBy=default.target\n'
    )


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


def _endpoint_host(ep: str) -> str:
    """Extract the host from a ``scheme/HOST:PORT`` endpoint (IPv6-bracket aware)."""
    return ep.split('/', 1)[-1].rsplit(':', 1)[0]


def _is_loopback_endpoint(ep: str) -> bool:
    """Return True when an endpoint targets loopback (never leaves the host)."""
    host = _endpoint_host(ep).strip('[]')
    return host == 'localhost' or host == '::1' or host.startswith('127.')


def _to_tls_endpoints(endpoints: list[str]) -> list[str]:
    """Swap ``tcp/`` for ``tls/`` on cross-host endpoints, leaving loopback plaintext.

    mTLS protects links that traverse the network. The loopback hop from a local
    CLI / MCP client to the local zenohd never goes on the wire, so it stays
    plaintext ``tcp/`` — otherwise those local clients (which connect over
    ``tcp/127.0.0.1`` without a cert) could not reach their own router. UDP
    endpoints are left untouched (TLS rides on TCP).
    """
    out: list[str] = []
    for ep in endpoints:
        if ep.startswith('tcp/') and not _is_loopback_endpoint(ep):
            out.append('tls/' + ep[len('tcp/') :])
        else:
            out.append(ep)
    return out


def _render_tls_block() -> str:
    """Render the ``transport.link.tls`` block pointing at the local cert store.

    Both ``listen_*`` (server side) and ``connect_*`` (client side) reference the
    same peer identity because a zenoh router both accepts and dials links.
    ``enable_mtls`` makes the far side present a cert too; ``verify_name_on_connect``
    checks that cert's SAN against the dialed address.
    """
    ca = tls_module.ca_cert_path()
    cert = tls_module.peer_cert_path()
    key = tls_module.peer_key_path()
    return (
        '  transport: {\n'
        '    link: {\n'
        '      tls: {\n'
        f'        root_ca_certificate: "{ca}",\n'
        '        enable_mtls: true,\n'
        '        verify_name_on_connect: true,\n'
        f'        listen_private_key: "{key}",\n'
        f'        listen_certificate: "{cert}",\n'
        f'        connect_private_key: "{key}",\n'
        f'        connect_certificate: "{cert}",\n'
        '      },\n'
        '    },\n'
        '  },\n'
        '\n'
    )


def _render_mesh_config(
    mode: str, listen_endpoints: list[str], connect_endpoints: list[str], tls: bool = False
) -> str:
    """Render a hub-or-spoke zenohd config with rocksdb + replication.

    When ``tls`` is set, endpoints use the ``tls/`` scheme and a
    ``transport.link.tls`` mTLS block is emitted; the cert paths it references
    must already exist (the caller validates this).
    """
    if tls:
        listen_endpoints = _to_tls_endpoints(listen_endpoints)
        connect_endpoints = _to_tls_endpoints(connect_endpoints)
    listen_lines = _format_endpoint_list(listen_endpoints)
    if connect_endpoints:
        connect_lines = _format_endpoint_list(connect_endpoints)
        connect_block = f'    endpoints: [\n      {connect_lines},\n    ],'
    else:
        connect_block = '    endpoints: [],'
    role_note = 'hub: spokes dial in' if mode == 'hub' else 'spoke: dials the hub'
    tls_note = ' (mTLS enabled)' if tls else ''
    tls_block = _render_tls_block() if tls else ''
    return (
        f'// Generated by `kioku-mesh init --mode {mode}` — {role_note}.{tls_note}\n'
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
        f'{tls_block}'
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


def _cmd_mcp_install(args: argparse.Namespace) -> int:
    """Register ``kioku-mesh-mcp`` with the chosen MCP client."""
    try:
        client = MCPClient(args.client)
    except ValueError:
        print(f'error: unknown client {args.client!r}', file=sys.stderr)
        return 2
    try:
        extra_env = mcp_install_module.parse_env_pairs(args.env or [])
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    try:
        message = mcp_install_module.install(
            client,
            name=args.name,
            extra_env=extra_env,
            force=args.force,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    # ``install`` returns a human message either way; for the "already
    # registered, refuse without --force" branch the message starts with
    # ``error:`` and reflects exit code 1, matching the file-overwrite
    # idiom used by ``kioku-mesh init``.
    if message.startswith('error:'):
        print(message, file=sys.stderr)
        return 1
    print(message)
    return 0


_MESH_DEFAULT_LISTEN = 'tcp/0.0.0.0:17447'


def _cmd_mesh_start(args: argparse.Namespace) -> int:
    """Open an in-process zenoh router, start index subscriber, and wait for Ctrl-C."""
    import signal as _signal

    import zenoh

    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"router"')
    cfg.insert_json5('listen/endpoints', f'["{args.listen}"]')
    try:
        session = zenoh.open(cfg)
    except Exception as e:  # noqa: BLE001
        print(f'error: failed to open router session: {e}', file=sys.stderr)
        return 1

    # Internal self-connect endpoint (loopback substitution — router process only).
    self_connect_ep = args.listen.replace('0.0.0.0', '127.0.0.1')

    # User-facing peer hints: separate from self_connect_ep so other-host guidance
    # is correct when listen is wildcard (B3 fix).
    _port = args.listen.rsplit(':', 1)[-1]
    _is_wildcard = '0.0.0.0' in args.listen or '[::]' in args.listen
    if _is_wildcard:
        _detected = _detect_local_ipv4()
        print(f'Router listening on {args.listen} (all interfaces).')
        print(f'  from this host:   ZENOH_CONNECT=tcp/127.0.0.1:{_port} KIOKU_MESH_BACKEND=zenoh kioku-mesh save ...')
        if _detected:
            _hint_ip = _detected[0]
            _all_ips = ', '.join(_detected)
            print(
                f'  from other hosts: ZENOH_CONNECT=tcp/{_hint_ip}:{_port} KIOKU_MESH_BACKEND=zenoh '
                f'kioku-mesh save ...'
            )
            print(f'                    ^ auto-detected IPs: {_all_ips}')
        else:
            print(
                f'  from other hosts: '
                f'ZENOH_CONNECT=tcp/<this-host-ip>:{_port} KIOKU_MESH_BACKEND=zenoh kioku-mesh save ...'
            )
            print("                    ^ replace <this-host-ip> with this machine's LAN IP")
    else:
        print(f'Router listening on {args.listen}.')
        print(f'  connect with: ZENOH_CONNECT={args.listen} KIOKU_MESH_BACKEND=zenoh kioku-mesh save ...')
    print('Subscribing to peer observations (saves will be visible from this process)...')
    print('Press Ctrl-C to stop.')

    # Start index subscriber so peer saves are visible from router search.
    # _open_session() uses ZENOH_CONNECT, so we point it at our own router.
    os.environ['ZENOH_CONNECT'] = self_connect_ep
    os.environ['KIOKU_MESH_BACKEND'] = 'zenoh'
    set_rebuild_on_init_default(False)
    get_index()  # opens client session to our router + starts replication subscriber

    def _shutdown(sig: int, frame: object) -> None:
        stop_pending_drain_background()
        reset_backend()
        session.close()
        sys.exit(0)

    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.pause()
    return 0  # unreachable


def _cmd_mesh_join(args: argparse.Namespace) -> int:
    """Connect to a mesh peer, start index subscriber, and wait for Ctrl-C."""
    import signal as _signal

    import zenoh

    cfg = zenoh.Config()
    cfg.insert_json5('mode', '"peer"')
    cfg.insert_json5('connect/endpoints', f'["{args.peer}"]')
    try:
        session = zenoh.open(cfg)
    except Exception as e:  # noqa: BLE001
        print(f'error: failed to connect to {args.peer}: {e}', file=sys.stderr)
        return 1

    print(f'Connected to peer {args.peer}')
    print(f'In another terminal: ZENOH_CONNECT={args.peer} KIOKU_MESH_BACKEND=zenoh kioku-mesh save ...')
    print('Subscribing to mesh observations (saves from peers will be stored locally)...')
    print('Press Ctrl-C to stop.')

    # Start index subscriber to receive saves from the mesh.
    os.environ['ZENOH_CONNECT'] = args.peer
    os.environ['KIOKU_MESH_BACKEND'] = 'zenoh'
    set_rebuild_on_init_default(False)
    get_index()  # opens client session + starts replication subscriber

    def _shutdown(sig: int, frame: object) -> None:
        stop_pending_drain_background()
        reset_backend()
        session.close()
        sys.exit(0)

    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    _signal.pause()
    return 0  # unreachable


def _cmd_init_local(args: argparse.Namespace) -> int:
    """Provision a local-only config that does NOT require zenohd on PATH."""
    from .config import _config_path

    config_path = _config_path()
    if config_path.exists() and not args.force:
        print(f'error: {config_path} already exists. Use --force to overwrite.', file=sys.stderr)
        return 1
    if args.to_stdout:
        sys.stdout.write('backend: local\n')
        return 0
    written = write_local_config()
    print(f'wrote {written}')
    print('local backend ready — no zenohd required.')
    print('next: kioku-mesh save "hello local"')
    print(
        'scale up: for multi-host mesh, install zenohd then '
        '`kioku-mesh init --mode hub --force` (see README §Power users).'
    )
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    if args.mode == 'local':
        if getattr(args, 'tls', False):
            print('error: --tls applies only to --mode hub / spoke.', file=sys.stderr)
            return 2
        return _cmd_init_local(args)

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
    if args.mode == 'hub' and connect:
        print('error: --connect is not used with --mode hub (hubs only listen).', file=sys.stderr)
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

    use_tls = getattr(args, 'tls', False)
    if use_tls:
        if args.mode not in ('hub', 'spoke'):
            print('error: --tls applies only to --mode hub / spoke.', file=sys.stderr)
            return 2
        # mTLS rides on TCP; cross-host udp/ endpoints can't be wrapped in TLS, so
        # they'd stay plaintext + unauthenticated while the config advertises mTLS.
        # Refuse rather than silently emit an unprotected cross-host link.
        bad_udp = [ep for ep in (*listen, *connect) if ep.startswith('udp/') and not _is_loopback_endpoint(ep)]
        if bad_udp:
            print(
                'error: --tls cannot secure cross-host UDP endpoints (mTLS rides on TCP). '
                'Use tcp/ for these, or drop --tls:\n  ' + '\n  '.join(bad_udp),
                file=sys.stderr,
            )
            return 2
        cert_paths = (tls_module.ca_cert_path(), tls_module.peer_cert_path(), tls_module.peer_key_path())
        missing = [p for p in cert_paths if not p.is_file()]
        if missing:
            print(
                'error: --tls needs certificates that are not present yet:\n  '
                + '\n  '.join(str(p) for p in missing)
                + '\nProvision them first: `kioku-mesh tls init-ca` (CA host), then on each peer '
                '`kioku-mesh tls enroll <ca-host> --san <addr>` (with SSH), or the copy-paste flow '
                '`tls request` -> `tls sign` -> `tls install`.',
                file=sys.stderr,
            )
            return 2
        # Under --tls the local client hop matters more than usual: local
        # save/search/MCP connect over plaintext tcp/127.0.0.1 with no cert, so a
        # TLS-only router (e.g. listen 0.0.0.0 or LAN-IP only) locks them out.
        if not any(_is_loopback_endpoint(ep) for ep in listen):
            print(
                'warning: --tls with no loopback listen endpoint. Local save / search / MCP '
                'clients connect over plaintext tcp/127.0.0.1:7447 and cannot reach a TLS-only '
                'router. Add `--listen 127.0.0.1` so the local hop stays reachable; only '
                'cross-host links are encrypted.',
                file=sys.stderr,
            )

    body = _render_mesh_config(args.mode, listen, connect, tls=use_tls)

    config_path = Path(args.out) if args.out else _default_init_path()
    # zenohd's RocksDB root must point at the same dir the Python state dir
    # resolves to, honoring the legacy mesh-mem fallback so an existing store
    # is not orphaned on a partially-migrated host (#128).
    rocksdb_leaf = data_share_leaf()
    unit_path = _default_systemd_user_unit_path() if args.install_systemd else None
    unit_body: str | None = None
    if args.install_systemd:
        supported, reason = _detect_systemd_user()
        if not supported:
            print(f'error: --install-systemd is not supported here. {reason}', file=sys.stderr)
            return 2
        zenohd_binary = shutil.which('zenohd')
        if zenohd_binary is None:
            print(
                f'warning: zenohd not on PATH; baking fallback ExecStart={_SYSTEMD_ZENOHD_FALLBACK} '
                'into the unit. Install zenohd or edit the unit before enabling.',
                file=sys.stderr,
            )
            zenohd_binary = _SYSTEMD_ZENOHD_FALLBACK
        unit_body = _render_systemd_unit(config_path, zenohd_binary, f'%h/.local/share/{rocksdb_leaf}')

    if args.to_stdout:
        sys.stdout.write(body)
        if unit_body is not None:
            sys.stdout.write('\n# --- systemd unit (' + str(unit_path) + ') ---\n')
            sys.stdout.write(unit_body)
        return 0

    # Installing the systemd unit against a config that already exists is a
    # pure add-on: keep the user's existing config untouched (don't demand
    # --force, don't rewrite it) and only generate the unit. --force still
    # overwrites the config when the user explicitly asks.
    reuse_existing_config = config_path.exists() and not args.force and args.install_systemd
    if config_path.exists() and not args.force and not reuse_existing_config:
        print(f'error: {config_path} already exists. Use --force to overwrite.', file=sys.stderr)
        return 1
    if unit_path is not None and unit_path.exists() and not args.force:
        print(f'error: {unit_path} already exists. Use --force to overwrite.', file=sys.stderr)
        return 1

    if reuse_existing_config:
        print(f'using existing config {config_path} (unchanged)')
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(body, encoding='utf-8')
        print(f'wrote {config_path}')
    if unit_body is not None and unit_path is not None:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit_body, encoding='utf-8')
        print(f'wrote {unit_path}')
        print('enable: systemctl --user daemon-reload && systemctl --user enable --now kioku-mesh-zenohd')
    else:
        print(f'next: zenohd -c {config_path}')
    if args.mode in ('hub', 'spoke'):
        print(f'also: export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/{rocksdb_leaf}"')
    _print_mode_followup(args.mode, listen)
    return 0


def _print_mode_followup(mode: str, listen_endpoints: list[str]) -> None:
    """Print mode-specific scale-up / cross-peer guidance after init."""
    if mode == 'hub':
        hint_ip = _first_lan_ip_from_listen(listen_endpoints) or '<this-host-ip>'
        print(
            f'on each spoke: kioku-mesh init --mode spoke --listen 127.0.0.1 '
            f'--listen <spoke-lan-ip> --connect {hint_ip}:7447'
        )
    elif mode == 'spoke':
        print('then (once zenohd is running and synced with the hub): kioku-mesh --rebuild status')
        print(
            '  populates the local search index from memories already on the hub; '
            'until you run it once, status/search show 0 even though replication succeeded.'
        )
        print("on the hub: confirm --listen includes this spoke's reachable IP, then restart zenohd.")


def _first_lan_ip_from_listen(listen_endpoints: list[str]) -> str | None:
    """Return the first non-loopback IP from a list of `tcp/HOST:PORT` endpoints."""
    for ep in listen_endpoints:
        host = ep.split('/', 1)[-1].rsplit(':', 1)[0]
        if host and not host.startswith('127.') and host != '0.0.0.0':
            return host
    return None


def _cmd_tls_init_ca(args: argparse.Namespace) -> int:
    """Create the mesh CA (run once, on the host that will hold the CA key)."""
    ca_key = tls_module.ca_key_path()
    if ca_key.exists() and not args.force:
        print(f'error: {ca_key} already exists. Use --force to replace the CA.', file=sys.stderr)
        print('  warning: replacing the CA invalidates every peer cert it signed.', file=sys.stderr)
        return 1
    tls_module.create_ca(common_name=args.name, days=args.days)
    print(f'wrote {tls_module.ca_key_path()} (keep this secret — never copy it to another host)')
    print(f'wrote {tls_module.ca_cert_path()} (public — distribute to every peer)')
    print('next on each peer: kioku-mesh tls enroll <this-host> --san <address-peers-dial> (with SSH),')
    print('             or copy-paste: kioku-mesh tls request --san <address-peers-dial>')
    return 0


def _read_pasted_blob() -> str:
    """Read an armored blob from stdin, stopping at its ``-----END`` line.

    Stopping at the end marker means an interactive paste doesn't need a
    trailing Ctrl-D; piped input (no marker, or extra data) still reads through
    to EOF. Either way the text is handed to a tolerant ``_dearmor``.
    """
    lines: list[str] = []
    for line in sys.stdin:
        lines.append(line)
        if line.startswith('-----END KIOKU-MESH'):
            break
    return ''.join(lines)


def _cmd_tls_request(args: argparse.Namespace) -> int:
    """Generate this peer's key (stays local) + a CSR blob to hand to the CA host."""
    try:
        _key_pem, csr_pem = tls_module.generate_key_and_csr(args.san, common_name=args.cn or None)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    blob = tls_module.encode_csr_blob(csr_pem)
    # Guidance goes to stderr so stdout is a clean blob you can pipe or copy
    # without slicing prose out of it.
    print(f'wrote {tls_module.peer_key_path()} (private — stays on this host)', file=sys.stderr)
    if args.out:
        Path(args.out).write_text(blob + '\n')
        print(f'wrote CSR request to {args.out} — send it to the CA host', file=sys.stderr)
    else:
        print('copy the block below to the CA host and run `kioku-mesh tls sign`:', file=sys.stderr)
        print(blob)
    print(
        'then paste the bundle it returns into `kioku-mesh tls install` here.\n'
        '(have SSH to the CA host? `kioku-mesh tls enroll <ca-host> --san ...` does all three steps.)',
        file=sys.stderr,
    )
    return 0


def _resolve_csr_input(args: argparse.Namespace) -> str | None:
    """Return the raw CSR text from the file arg or a pasted/piped blob, or None on error."""
    if args.csr:
        src = Path(args.csr)
        if not src.is_file():
            print(f'error: CSR not found: {src}', file=sys.stderr)
            return None
        return src.read_text()
    if sys.stdin.isatty():
        print('paste the CSR block from `kioku-mesh tls request` (reads to its -----END line):', file=sys.stderr)
    return _read_pasted_blob()


def _cmd_tls_sign(args: argparse.Namespace) -> int:
    """Sign a peer's CSR with the local CA (run on the CA host); emit a cert bundle."""
    ca_key = tls_module.ca_key_path()
    if not ca_key.is_file():
        print(f'error: no CA at {ca_key}. Run `kioku-mesh tls init-ca` on this host first.', file=sys.stderr)
        return 2
    raw = _resolve_csr_input(args)
    if raw is None:
        return 2
    try:
        csr_pem = tls_module.decode_csr_blob(raw)
        cert_pem = tls_module.sign_csr(csr_pem, days=args.days)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    bundle = tls_module.encode_cert_bundle(cert_pem, tls_module.ca_cert_path().read_bytes())
    if args.out:
        Path(args.out).write_text(bundle + '\n')
        print(f'wrote signed bundle to {args.out} — send it back to the requesting peer', file=sys.stderr)
    else:
        print('copy the block below back to the requesting peer for `kioku-mesh tls install`:', file=sys.stderr)
        print(bundle)
    return 0


def _resolve_install_material(args: argparse.Namespace) -> tuple[bytes, bytes] | None:
    """Return ``(cert_pem, ca_pem)`` from --cert/--ca files or a pasted bundle blob, or None on error."""
    if args.cert or args.ca:
        if not (args.cert and args.ca):
            print('error: --cert and --ca must be given together (or omit both to paste a bundle).', file=sys.stderr)
            return None
        cert_path, ca_path = Path(args.cert), Path(args.ca)
        for label, p in (('--cert', cert_path), ('--ca', ca_path)):
            if not p.is_file():
                print(f'error: {label} file not found: {p}', file=sys.stderr)
                return None
        return cert_path.read_bytes(), ca_path.read_bytes()
    if args.bundle:
        src = Path(args.bundle)
        if not src.is_file():
            print(f'error: bundle file not found: {src}', file=sys.stderr)
            return None
        raw = src.read_text()
    else:
        if sys.stdin.isatty():
            print('paste the bundle block from `kioku-mesh tls sign` (reads to its -----END line):', file=sys.stderr)
        raw = _read_pasted_blob()
    try:
        return tls_module.decode_cert_bundle(raw)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return None


def _cmd_tls_install(args: argparse.Namespace) -> int:
    """Place a signed cert + CA cert into this host's TLS store (from a bundle blob or files)."""
    if not tls_module.peer_key_path().is_file():
        print(
            f'error: no private key at {tls_module.peer_key_path()}. Run `kioku-mesh tls request` on this '
            'host first so the key that matches this cert exists locally.',
            file=sys.stderr,
        )
        return 2
    material = _resolve_install_material(args)
    if material is None:
        return 2
    cert_pem, ca_pem = material
    try:
        tls_module.install(cert_pem, ca_pem)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    print(f'installed {tls_module.peer_cert_path()}')
    print(f'installed {tls_module.ca_cert_path()}')
    print('next: kioku-mesh init --mode <hub|spoke> --tls --listen ... --force')
    return 0


def _cmd_tls_enroll(args: argparse.Namespace) -> int:
    """One-shot SSH enrollment: request locally, sign on the CA host over SSH, install.

    The opt-in upgrade for anyone who already has SSH to the CA host: it folds
    request -> sign -> install into a single command. The peer key is still
    generated locally and never leaves; only the public CSR/cert cross the link
    (piped through SSH's own encrypted channel).

    The freshly built key is held in memory and only committed to disk after the
    returned bundle has been decoded and validated, so a failed enroll (bad CA
    host, timeout, remote error, malformed bundle) never overwrites an
    already-enrolled peer's key with one that no longer matches its cert.
    """
    try:
        key_pem, csr_pem = tls_module.build_key_and_csr(args.san, common_name=args.cn or None)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    csr_blob = tls_module.encode_csr_blob(csr_pem)
    ssh_cmd = ['ssh']
    if args.ssh_port:
        ssh_cmd += ['-p', str(args.ssh_port)]
    for opt in args.ssh_opt:
        ssh_cmd += ['-o', opt]
    # shlex.join so a --remote-mesh path with spaces stays a single argument and
    # any shell metacharacters are quoted rather than interpreted by the remote
    # shell the command is handed to.
    remote_cmd = shlex.join([args.remote_mesh, 'tls', 'sign', '--days', str(args.days)])
    ssh_cmd += [args.ca_host, remote_cmd]
    print(f'signing on {args.ca_host} over SSH ...', file=sys.stderr)
    try:
        proc = subprocess.run(  # noqa: S603 - args are explicit, not a shell string
            ssh_cmd, input=csr_blob, capture_output=True, text=True, timeout=args.timeout
        )
    except FileNotFoundError:
        print(
            'error: `ssh` not found. Use the copy-paste flow instead: `tls request` -> `tls sign` -> `tls install`.',
            file=sys.stderr,
        )
        return 2
    except subprocess.TimeoutExpired:
        print(f'error: SSH to {args.ca_host} timed out after {args.timeout}s.', file=sys.stderr)
        return 2
    if proc.returncode != 0:
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr if proc.stderr.endswith('\n') else proc.stderr + '\n')
        print(
            f'error: remote `tls sign` failed on {args.ca_host} (exit {proc.returncode}). '
            f'Is `{args.remote_mesh}` on its PATH and the CA initialized there?',
            file=sys.stderr,
        )
        return 2
    # Decode + validate against the in-memory key before touching disk, so a
    # malformed bundle leaves the existing peer untouched.
    try:
        cert_pem, ca_pem = tls_module.decode_cert_bundle(proc.stdout)
        tls_module.validate_bundle(cert_pem, ca_pem, key_pem)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    # The bundle is good: commit the key/CSR, then install (re-verifies + writes
    # cert + ca). install's checks are guaranteed to pass after validate_bundle.
    tls_module.write_peer_material(key_pem, csr_pem)
    tls_module.install(cert_pem, ca_pem)
    print(f'enrolled via {args.ca_host}: installed {tls_module.peer_cert_path()} + {tls_module.ca_cert_path()}')
    print('next: kioku-mesh init --mode <hub|spoke> --tls --listen ... --force')
    return 0


def _cmd_tls_info(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Show the local CA / peer certificate details and expiry."""
    found = False
    for label, path in (('CA', tls_module.ca_cert_path()), ('peer', tls_module.peer_cert_path())):
        if not path.is_file():
            continue
        found = True
        try:
            info = tls_module.inspect_cert(path.read_bytes())
        except Exception as e:  # noqa: BLE001
            print(f'{label}: {path} — unreadable ({type(e).__name__})')
            continue
        state = 'EXPIRED' if info.expired else f'{info.days_remaining}d left'
        print(f'{label}: {path}')
        print(f'  subject: {info.subject}')
        if info.sans:
            print(f'  SAN:     {", ".join(info.sans)}')
        print(f'  expires: {info.not_valid_after:%Y-%m-%d} ({state})')
    if not found:
        print('no certificates found. Run `kioku-mesh tls init-ca` / `tls request` to provision them.')
        return 1
    return 0


def _cmd_migrate_visibility(args: argparse.Namespace) -> int:
    from datetime import timezone

    from .memory.visibility_migration import build_migration_plan
    from .memory.visibility_migration import compute_params_hash
    from .memory.visibility_migration import execute_migration
    from .memory.visibility_migration import load_checkpoint
    from .memory.visibility_migration import parse_migration_target
    from .memory.visibility_migration import reconstruct_items_from_checkpoint
    from .memory.visibility_migration import scan_legacy_visibility

    status = get_backend().get_status()
    if status.pending_puts > 0:
        print(
            f'error: pending_puts={status.pending_puts} > 0. '
            'Run `kioku-mesh drain --pending` first to prevent legacy key recreation.',
            file=sys.stderr,
        )
        return 2

    if args.from_source != 'legacy':
        print(f'error: --from {args.from_source!r}: only "legacy" is supported in Phase C.', file=sys.stderr)
        return 2

    try:
        target = parse_migration_target(args.to_visibility)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    scope = args.scope or ''
    key_prefix = args.key_prefix or ''

    # C1: validate --key-prefix before scanning
    if key_prefix and not (key_prefix.startswith('mem/obs') or key_prefix.startswith('mem/tomb')):
        print(
            f'error: --key-prefix {key_prefix!r} must start with mem/obs or mem/tomb.',
            file=sys.stderr,
        )
        return 2

    batch_size = min(max(1, args.batch_size), 10_000)

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    run_id = now_dt.strftime('%Y%m%dT%H%M%SZ')

    state_base = Path.home() / '.local' / 'share' / 'kioku-mesh' / 'migrations' / run_id
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else state_base / 'checkpoint.json'
    backup_dir = Path(args.backup_dir) if args.backup_dir else state_base / 'backup'

    if args.resume:
        resume_path = Path(args.resume)
        if not resume_path.is_file():
            print(f'error: --resume {resume_path}: file not found.', file=sys.stderr)
            return 2
        checkpoint_path = resume_path
        # Derive backup_dir from checkpoint parent unless explicitly given
        if not args.backup_dir:
            backup_dir = resume_path.parent / 'backup'

    # Compute params hash for this run (used to detect mismatched --resume)
    run_params = {
        'from': args.from_source,
        'to': target.display,
        'scope': scope,
        'key_prefix': key_prefix,
        'visibility': target.visibility,
        'scope_id': target.scope_id,
    }
    current_params_hash = compute_params_hash(run_params)

    # B3: On resume, load checkpoint and validate params before any mutation
    chk_for_resume = None
    if args.resume and checkpoint_path.is_file():
        try:
            chk_for_resume = load_checkpoint(checkpoint_path)
        except Exception as e:  # noqa: BLE001
            print(f'error: cannot load checkpoint {checkpoint_path}: {e}', file=sys.stderr)
            return 2
        if chk_for_resume.params_hash and chk_for_resume.params_hash != current_params_hash:
            print(
                f'error: --resume checkpoint params do not match current arguments.\n'
                f'  checkpoint: {chk_for_resume.params}\n'
                f'  current:    {run_params}\n'
                f'Use the exact same --from/--to/--scope/--key-prefix as the original run.',
                file=sys.stderr,
            )
            return 2

    session = get_session()

    print('Scanning legacy keys...', file=sys.stderr)
    try:
        records = scan_legacy_visibility(session, scope=scope, key_prefix=key_prefix)
    except Exception as e:  # noqa: BLE001
        print(f'error: scan failed: {e}', file=sys.stderr)
        return 1

    print(f'Found {len(records)} legacy records. Building migration plan...', file=sys.stderr)
    try:
        plan = build_migration_plan(records, target, session)
    except Exception as e:  # noqa: BLE001
        print(f'error: plan build failed: {e}', file=sys.stderr)
        return 1

    # B1: On resume, merge checkpoint items whose source key is already deleted
    if chk_for_resume is not None:
        existing_old_keys = {item.old_key for item in plan.items}
        extra_items = reconstruct_items_from_checkpoint(chk_for_resume, backup_dir, target, existing_old_keys)
        if extra_items:
            print(
                f'--resume: adding {len(extra_items)} pending item(s) from checkpoint (source key already deleted).',
                file=sys.stderr,
            )
            plan.items.extend(extra_items)

    result = execute_migration(
        plan,
        session=session,
        dry_run=args.dry_run,
        yes=args.yes,
        batch_size=batch_size,
        checkpoint_path=checkpoint_path,
        backup_dir=backup_dir,
        now_iso=now_iso,
        params_hash=current_params_hash,
    )

    if not args.dry_run:
        print(
            f'migrate-visibility complete: '
            f'planned={result.planned} copied={result.copied} verified={result.verified} '
            f'deleted={result.deleted} repair_put={result.repair_put} '
            f'conflicts={result.conflicts} failures={result.failures}'
        )

    if result.failures > 0:
        return 1
    if result.conflicts > 0:
        return 1
    return 0


def _cmd_zenohd_install(args: argparse.Namespace) -> int:
    bin_dir = Path(args.bin_dir) if args.bin_dir else zenohd_install_module.default_bin_dir()
    try:
        installed = zenohd_install_module.install(
            version=args.version,
            bin_dir=bin_dir,
            verbose=args.verbose,
        )
    except Exception as exc:  # noqa: BLE001
        print(f'error: {exc}', file=sys.stderr)
        return 1
    for name, path in installed.items():
        print(f'installed {name}: {path}')
    print()
    print(f'add to PATH: export PATH="{bin_dir}:$PATH"')
    if bin_dir.as_posix() not in os.environ.get('PATH', '').split(':'):
        print('  (not yet on PATH — add the line above to your shell profile)')
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='kioku-mesh', description='kioku-mesh CLI')
    parser.add_argument('--version', action='version', version=f'kioku-mesh {__version__}')
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help=(
            'Rebuild the SQLite index from zenoh on first startup. '
            'CLI is one-shot and skips by default (#38); '
            'pass this when the index is empty and you want search to work, or during CI verification. '
            'KIOKU_MESH_FORCE_REBUILD=1 has the same effect.'
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
    p_save.add_argument(
        '--visibility',
        default='',
        choices=['', 'user', 'team', 'mesh'],
        help='replication scope: user (your machines), team, mesh (all peers); '
        'default follows config.yaml default_visibility (empty = legacy layout)',
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
    p_search.add_argument(
        '--search-mode',
        dest='search_mode',
        choices=['and', 'or', 'and_or'],
        default='and',
        help='search mode: and (default) / or / and_or',
    )
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
    p_status.add_argument(
        '--show-shadows',
        action='store_true',
        default=False,
        dest='show_shadows',
        help='list shadowed observations (read-only, no unshadow/delete)',
    )
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
        help='Generate a starter zenohd config under ~/.config/kioku-mesh/',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Generate a starter config. Pick --mode by the deployment shape you want:\n'
            '\n'
            '  local  (default) SQLite only, no zenohd. Single-host persistent storage\n'
            '         with zero daemon. The easiest starting point.\n'
            '  hub    zenohd + rocksdb + LAN listener. Central peer that spokes dial in to\n'
            '         (multi-host mesh, persistent).\n'
            '  spoke  zenohd + rocksdb, dials a hub via --connect (multi-host mesh, persistent).\n'
            '\n'
            'For ephemeral Zenoh smoke tests without provisioning, use `kioku-mesh mesh start`.\n'
            'See README §Power users for the multi-host walkthrough.'
        ),
    )
    p_init.add_argument(
        '--mode',
        default='local',
        choices=_INIT_MODES,
        help='deployment shape: local (default) / hub / spoke. See full descriptions above.',
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
    p_init.add_argument(
        '--tls',
        action='store_true',
        help=(
            'emit an mTLS config (tls/ endpoints + transport.link.tls). hub/spoke only; '
            'requires certs provisioned via `kioku-mesh tls` first.'
        ),
    )
    p_init.add_argument('--force', action='store_true', help='overwrite if the target file exists')
    p_init.add_argument(
        '--print',
        dest='to_stdout',
        action='store_true',
        help='write to stdout instead of file',
    )
    p_init.add_argument(
        '--install-systemd',
        dest='install_systemd',
        action='store_true',
        help=(
            'also install a user-scope systemd unit at '
            f'{_default_systemd_user_unit_path()} that runs `zenohd -c <out>` on login. '
            'Linux only; macOS / Windows / non-systemd hosts get a clear error.'
        ),
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

    p_mcp = sub.add_parser(
        'mcp',
        help='MCP client registration helpers',
    )
    p_mcp_sub = p_mcp.add_subparsers(dest='mcp_command', required=True)
    p_mcp_install = p_mcp_sub.add_parser(
        'install',
        help='Register kioku-mesh-mcp with a supported MCP client',
    )
    p_mcp_install.add_argument(
        '--client',
        required=True,
        choices=[c.value for c in MCPClient],
        help='target MCP client (v0.3 supports Claude Code and Codex CLI)',
    )
    p_mcp_install.add_argument(
        '--name',
        default=mcp_install_module.DEFAULT_REGISTRY_NAME,
        help='registry key the client lists the server under (default: kioku_mesh)',
    )
    p_mcp_install.add_argument(
        '-e',
        '--env',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='extra env var passed to the MCP server. Repeatable. Overrides the per-client defaults.',
    )
    p_mcp_install.add_argument(
        '--force',
        action='store_true',
        help='replace an existing registration of the same name',
    )
    p_mcp_install.add_argument(
        '--dry-run',
        action='store_true',
        help='print the command / config block instead of executing the registration',
    )
    p_mcp_install.set_defaults(func=_cmd_mcp_install)

    p_mesh = sub.add_parser(
        'mesh',
        help='Embedded zenoh router for try-it / demo (no zenohd binary required)',
        description=(
            'Ephemeral multi-host mesh without the zenohd binary. Intended as a\n'
            'try-it / demo path — cross-host replication is NOT persistent across\n'
            'router restarts, and writes made while a peer was offline are not\n'
            'replayed later. For production multi-host use, install zenohd and\n'
            'follow the Power users section in the README.\n\n'
            '60-second multi-host start:\n'
            '  Host A: kioku-mesh mesh start\n'
            '  Host B: ZENOH_CONNECT=tcp/<host-a-ip>:17447 KIOKU_MESH_BACKEND=zenoh '
            'kioku-mesh mesh join tcp/<host-a-ip>:17447\n'
            '  Host A or B: ZENOH_CONNECT=tcp/<host-a-ip>:17447 KIOKU_MESH_BACKEND=zenoh '
            'kioku-mesh save "hello mesh"\n'
            '  Host A or B: ZENOH_CONNECT=tcp/<host-a-ip>:17447 KIOKU_MESH_BACKEND=zenoh '
            'kioku-mesh search "hello"\n\n'
            'Env vars:\n'
            '  ZENOH_CONNECT   zenoh router endpoint (e.g. tcp/192.168.1.10:17447)\n'
            '  KIOKU_MESH_BACKEND  set to "zenoh" to use Zenoh transport (default for mesh)'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_mesh_sub = p_mesh.add_subparsers(dest='mesh_command', required=True)

    p_mesh_start = p_mesh_sub.add_parser(
        'start',
        help='Start an in-process zenoh router (foreground; Ctrl-C to stop)',
        description=(
            'Start an in-process zenoh router (mode=router). No zenohd binary required.\n\n'
            'The router subscribes to all peer saves and stores them in the local SQLite index,\n'
            'so `kioku-mesh search` from this terminal will return content saved by remote peers.\n\n'
            'Example:\n'
            '  kioku-mesh mesh start --listen tcp/0.0.0.0:17447\n\n'
            'Then on a remote host:\n'
            '  ZENOH_CONNECT=tcp/<this-host>:17447 KIOKU_MESH_BACKEND=zenoh kioku-mesh save "hello"\n\n'
            'Env vars:\n'
            '  ZENOH_CONNECT     (set automatically by mesh start to point at the local router)\n'
            '  KIOKU_MESH_BACKEND  (set automatically to "zenoh")'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_mesh_start.add_argument(
        '--listen',
        default=_MESH_DEFAULT_LISTEN,
        help=f'TCP listen endpoint (default: {_MESH_DEFAULT_LISTEN})',
    )
    p_mesh_start.set_defaults(func=_cmd_mesh_start)

    p_mesh_join = p_mesh_sub.add_parser(
        'join',
        help='Connect to a mesh peer (foreground; accumulates peer saves locally)',
        description=(
            'Connect to a mesh router as an in-process peer. Starts a replication subscriber\n'
            'so remote saves are accumulated in the local SQLite index (Ctrl-C to stop).\n\n'
            'Example:\n'
            '  kioku-mesh mesh join tcp/192.168.1.10:17447\n\n'
            'After joining, in another terminal:\n'
            '  ZENOH_CONNECT=tcp/192.168.1.10:17447 KIOKU_MESH_BACKEND=zenoh kioku-mesh save "hello"\n'
            '  kioku-mesh search "hello"  # reads from local SQLite filled by the subscriber\n\n'
            'Env vars:\n'
            '  ZENOH_CONNECT     endpoint to connect to (set automatically from <peer> arg)\n'
            '  KIOKU_MESH_BACKEND  set to "zenoh" automatically'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_mesh_join.add_argument(
        'peer',
        help='Peer endpoint to connect to (e.g. tcp/192.168.1.10:17447)',
    )
    p_mesh_join.set_defaults(func=_cmd_mesh_join)

    p_tls = sub.add_parser(
        'tls',
        help='Provision mTLS certificates for the mesh (private CA, CSR-based)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Provision mutual-TLS certificates so only peers holding a cert signed\n'
            'by your CA can join the mesh. Trust model: one private CA, one key per\n'
            'peer that never leaves the peer (only the CSR travels).\n\n'
            'Copy-paste flow (no SSH, no file shuffling):\n'
            '  CA host:   kioku-mesh tls init-ca\n'
            '  each peer: kioku-mesh tls request --san <addr-peers-dial>\n'
            '             (copy the printed CSR block to the CA host)\n'
            '  CA host:   kioku-mesh tls sign        (paste the CSR block)\n'
            '             (copy the printed bundle block back to the peer)\n'
            '  each peer: kioku-mesh tls install     (paste the bundle block)\n'
            '             kioku-mesh init --mode <hub|spoke> --tls --listen ... --force\n\n'
            'Have SSH to the CA host? One command does all three:\n'
            '  each peer: kioku-mesh tls enroll <ca-host> --san <addr-peers-dial>\n\n'
            'The blocks (CSR, signed bundle) are not secret; move them however is\n'
            'convenient — paste, chat, scp, a USB stick. The CA key and each peer\n'
            'key are secret and never appear in a block or leave their host.'
        ),
    )
    p_tls_sub = p_tls.add_subparsers(dest='tls_command', required=True)

    p_tls_initca = p_tls_sub.add_parser('init-ca', help='Create the mesh CA (run once, on the CA host)')
    p_tls_initca.add_argument('--name', default='kioku-mesh-ca', help='CA common name (default: kioku-mesh-ca)')
    p_tls_initca.add_argument(
        '--days',
        type=int,
        default=tls_module.DEFAULT_CA_DAYS,
        help=f'CA validity in days (default: {tls_module.DEFAULT_CA_DAYS})',
    )
    p_tls_initca.add_argument(
        '--force', action='store_true', help='replace an existing CA (invalidates signed peer certs)'
    )
    p_tls_initca.set_defaults(func=_cmd_tls_init_ca)

    p_tls_request = p_tls_sub.add_parser(
        'request',
        help='Generate this peer key (local) + a CSR blob to copy to the CA host',
    )
    p_tls_request.add_argument(
        '--san',
        action='append',
        default=[],
        metavar='ADDR',
        required=True,
        help='address peers dial this host on (IP or hostname). Repeatable; include every reachable address.',
    )
    p_tls_request.add_argument('--cn', default='', help='certificate common name (default: first --san)')
    p_tls_request.add_argument('-o', '--out', default='', help='write the CSR blob to a file instead of stdout')
    p_tls_request.set_defaults(func=_cmd_tls_request)

    p_tls_sign = p_tls_sub.add_parser('sign', help='Sign a peer CSR with the local CA (run on the CA host)')
    p_tls_sign.add_argument(
        'csr',
        nargs='?',
        help='path to a .csr file or saved CSR blob; omit to paste/pipe one on stdin',
    )
    p_tls_sign.add_argument('-o', '--out', default='', help='write the signed cert bundle to a file instead of stdout')
    p_tls_sign.add_argument(
        '--days',
        type=int,
        default=tls_module.DEFAULT_CERT_DAYS,
        help=f'certificate validity in days (default: {tls_module.DEFAULT_CERT_DAYS})',
    )
    p_tls_sign.set_defaults(func=_cmd_tls_sign)

    p_tls_install = p_tls_sub.add_parser(
        'install',
        help='Install a signed cert + CA cert into this host (from a bundle blob or files)',
    )
    p_tls_install.add_argument(
        'bundle',
        nargs='?',
        help='path to a saved cert-bundle blob; omit to paste/pipe one on stdin',
    )
    p_tls_install.add_argument('--cert', help='(file mode) path to this peer signed certificate; use with --ca')
    p_tls_install.add_argument('--ca', help='(file mode) path to the CA certificate; use with --cert')
    p_tls_install.set_defaults(func=_cmd_tls_install)

    p_tls_enroll = p_tls_sub.add_parser(
        'enroll',
        help='One command: request + sign over SSH + install (needs SSH to the CA host)',
    )
    p_tls_enroll.add_argument('ca_host', metavar='CA-HOST', help='SSH destination of the CA host (e.g. user@hub)')
    p_tls_enroll.add_argument(
        '--san',
        action='append',
        default=[],
        metavar='ADDR',
        required=True,
        help='address peers dial this host on (IP or hostname). Repeatable.',
    )
    p_tls_enroll.add_argument('--cn', default='', help='certificate common name (default: first --san)')
    p_tls_enroll.add_argument(
        '--days',
        type=int,
        default=tls_module.DEFAULT_CERT_DAYS,
        help=f'certificate validity in days (default: {tls_module.DEFAULT_CERT_DAYS})',
    )
    p_tls_enroll.add_argument(
        '--remote-mesh',
        default='kioku-mesh',
        help='kioku-mesh command name on the CA host (default: kioku-mesh)',
    )
    p_tls_enroll.add_argument(
        '--ssh-port', type=int, default=0, help='SSH port for the CA host (default: ssh default)'
    )
    p_tls_enroll.add_argument(
        '--ssh-opt',
        action='append',
        default=[],
        metavar='OPT',
        help="pass an -o option to ssh (repeatable), e.g. --ssh-opt 'StrictHostKeyChecking=accept-new'",
    )
    p_tls_enroll.add_argument('--timeout', type=int, default=60, help='SSH timeout in seconds (default: 60)')
    p_tls_enroll.set_defaults(func=_cmd_tls_enroll)

    p_tls_info = p_tls_sub.add_parser('info', help='Show local CA / peer certificate details and expiry')
    p_tls_info.set_defaults(func=_cmd_tls_info)

    p_zenohd = sub.add_parser(
        'zenohd',
        help='Manage zenohd and zenoh-backend-rocksdb binaries',
    )
    p_zenohd_sub = p_zenohd.add_subparsers(dest='zenohd_command', required=True)
    p_zenohd_install = p_zenohd_sub.add_parser(
        'install',
        help='Download and install zenohd + zenoh-backend-rocksdb (version-matched)',
    )
    p_zenohd_install.add_argument(
        '--version',
        default='1.9.0',
        help='Zenoh release version to install (default: 1.9.0)',
    )
    p_zenohd_install.add_argument(
        '--bin-dir',
        default=None,
        metavar='DIR',
        help='Destination directory (default: ~/.local/share/kioku-mesh/bin)',
    )
    p_zenohd_install.add_argument(
        '--verbose',
        action='store_true',
        help='Print download progress',
    )
    p_zenohd_install.set_defaults(func=_cmd_zenohd_install)

    p_migrate = sub.add_parser(
        'migrate-visibility',
        help='Migrate legacy obs/tomb keys into an explicit visibility namespace (ADR-0019 Phase C)',
    )
    p_migrate.add_argument(
        '--from',
        dest='from_source',
        required=True,
        choices=['legacy'],
        help='source namespace (currently only "legacy" is supported)',
    )
    p_migrate.add_argument(
        '--to',
        dest='to_visibility',
        required=True,
        help='target visibility: mesh | user | team | team/<team_id>',
    )
    p_migrate.add_argument(
        '--dry-run',
        dest='dry_run',
        action='store_true',
        help='scan and print plan without any mutations',
    )
    p_migrate.add_argument('--yes', action='store_true', help='skip interactive confirmation')
    _scope_group = p_migrate.add_mutually_exclusive_group()
    _scope_group.add_argument(
        '--scope',
        default='',
        help='identity scope: agent_family[/client_id[/pc_id[/session_id]]] (1-4 segments)',
    )
    _scope_group.add_argument(
        '--key-prefix',
        dest='key_prefix',
        default='',
        help='advanced: legacy obs key prefix (must start with mem/obs); appends /**',
    )
    p_migrate.add_argument(
        '--batch-size',
        dest='batch_size',
        type=_positive_int,
        default=500,
        help='records processed per checkpoint flush (default 500, max 10000)',
    )
    p_migrate.add_argument('--resume', default='', help='resume from an existing checkpoint JSON path')
    p_migrate.add_argument('--checkpoint', default='', help='checkpoint file path (auto-created if not given)')
    p_migrate.add_argument('--backup-dir', dest='backup_dir', default='', help='backup directory (auto-created)')
    p_migrate.set_defaults(func=_cmd_migrate_visibility)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch to the matching subcommand handler.

    The CLI is a one-shot process; the per-startup ``rebuild_from_zenoh``
    scan adds ~15s on a populated mesh (#38) and the local SQLite index
    converges via the replication subscriber anyway. Default to skipping
    that rebuild and only opt back in via ``--rebuild`` (or
    ``KIOKU_MESH_FORCE_REBUILD=1`` at the env layer).

    ``--rebuild`` uses the explicit-override channel so it wins over
    ambient ``KIOKU_MESH_SKIP_REBUILD=1`` in shell profiles / wrappers —
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
        reset_backend()
        # Explicitly close the cached Zenoh session before sys.exit. Without
        # this, the CLI hangs after printing output because the session's
        # replication subscriber thread keeps the interpreter alive past
        # the command's return; users had to ctrl-c to escape.
        _reset_session()


if __name__ == '__main__':
    sys.exit(main())

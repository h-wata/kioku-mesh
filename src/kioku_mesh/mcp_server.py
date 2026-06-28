"""FastMCP server exposing kioku-mesh tools to coding agents.

Identity fields (agent_family, client_id, pc_id, session_id) are resolved
from environment/state on the server side. They are intentionally NOT
arguments to ``save_observation`` so an LLM cannot contaminate the id
space by guessing values. Narrow-down is allowed on ``search_memory``.
"""

from contextlib import closing
from datetime import datetime
from datetime import timezone
import json
import logging
import os
import re
import socket
import sys

from fastmcp import FastMCP

from kioku_mesh.core._env_compat import get_env

from . import __version__
from .backend import get_backend
from .backend import reset_backend
from .config import format_visibility
from .config import get_backend_mode
from .config import get_team_id
from .config import get_user_id
from .config import resolve_write_visibility
from .core.identity import state_dir
from .core.transport import get_session as _get_zenoh_session
from .identity import get_pc_id
from .identity import get_session_id
from .memory.save_lint import lint_observation
from .messaging.keyspace import ack_key
from .messaging.local_index import ack_message as _ack_message_internal
from .messaging.local_index import LocalMessageIndex
from .messaging.models import is_expired
from .messaging.models import Message
from .messaging.purge import purge_expired_msgs
from .models import Observation
from .models import VALID_MEMORY_TYPES
from .store import get_index
from .store import MAX_SEARCH
from .store import search_observations
from .store import start_pending_drain_background
from .store import stop_pending_drain_background

log = logging.getLogger(__name__)

_INSTRUCTIONS = """\
kioku-mesh provides a Zenoh-backed shared memory across coding agents and hosts.
Treat this as ACTIVE PROTOCOL — do not wait for the user to ask.

PROACTIVE SAVE — call ``save_observation`` IMMEDIATELY after ANY of these:
- Architecture / convention / workflow / tool-choice decision is made
- Bug fixed (include root cause; memory_type="bug")
- Non-obvious discovery, gotcha, or edge case found
- Pattern established (naming, structure, approach; memory_type="pattern")
- Config change with rationale (memory_type="config")
- Feature implemented with non-obvious approach
- User performs the semantic act of approval / authorization / preference /
  rejection — regardless of phrasing or language. The trigger is the act, not
  specific words. Examples across languages (illustrative, NOT exhaustive):
    EN: "ok", "sounds good", "go ahead", "ship it", "let's do that",
        "no, do X instead", "approved"
    JA: "OK", "お願い", "採用", "公開して", "進めて", "そうじゃなくて〜"
    ZH: "好的", "可以", "同意", "上线吧", "不要…，改成…"
    KO: "좋아요", "진행해주세요", "동의합니다", "그건 빼고…"
  If the user just made a durable choice in any natural language, save it.
- Session concludes with a clear direction chosen (memory_type="summary")

SKIP saving when the entry would mostly duplicate another source of truth:
- PR / Issue lifecycle ticks: opened, pushed, merged, closed, "review found no blockers"
- Restatement of content already captured in a PR description, Issue body, ADR, CHANGELOG, or commit message
- Per-step implementation progress inside one conversation; use plan / todo tracking instead
- Generic status like "tests pass" or "build is green" without a non-obvious cause or decision

SKIP exception — save the WHY even when the conclusion lives in a SoR:
A PR / ADR / commit captures the *decision*, but rarely its *rationale*. When
the discussion produced any of the following, save them as a separate entry
(memory_type="decision" or "pattern") even though the conclusion is recorded
elsewhere:
- Alternatives that were considered and rejected, and why
- Background constraints (incident history, deadline, stakeholder ask) that
  shaped the choice
- User's strong preference or aesthetic judgement on otherwise-equivalent
  options
These cannot be reconstructed from the SoR later, so they are NOT duplicates.

Self-check after every task: "Did the user or I just make a decision, confirm a
recommendation, fix a bug, learn something, or establish a convention? If yes →
``save_observation`` NOW." Skip transient notes, status checks, and routine
tasks with no new learning.

SEARCH MEMORY (``search_memory`` → ``get_memory``) when:
- The user asks to recall anything ("remember", "what did we do", "前にやった")
- Starting work on something that may have prior context
- The user references a topic you have no context on
- The user's first message names a feature, file, or problem — search before answering

Identity (agent_family / client_id / pc_id / session_id) is resolved on the
server side from environment + state. Do not pass these as tool arguments;
they are intentionally not parameters of ``save_observation``.

Use ``memory_type`` accurately — one of: note, decision, bug, pattern, config,
summary. Prefer decision / bug / pattern / config over summary; use summary only
for a session conclusion with a chosen direction, not as a log of what happened.
Set ``importance`` 1–5 with care: 4-5 for project-wide or durable changes in
assumptions, 3 for reusable but local lessons, and 1-2 only when the entry is
still worth saving after the SKIP rules. Prefer adding ``subject`` + ``summary``
so search results stay scannable.
"""

mcp = FastMCP('kioku-mesh', instructions=_INSTRUCTIONS)

_messaging_index: LocalMessageIndex | None = None


def _get_messaging_index() -> LocalMessageIndex:
    """Return the process-scoped LocalMessageIndex, creating it on first call."""
    global _messaging_index
    if _messaging_index is None:
        db_path = state_dir() / 'messaging' / 'inbox.db'
        _messaging_index = LocalMessageIndex(db_path)
    return _messaging_index


_VALID_VISIBILITIES = frozenset({'', 'user', 'team', 'mesh'})


def _messaging_scopes(visibility: str) -> list[str]:
    """Resolve which msg/** scopes to query based on ``visibility``.

    ``''`` → all configured scopes (user + team + mesh).
    ``'user'`` / ``'team'`` / ``'mesh'`` → that single tier.
    ``user_id`` / ``team_id`` are resolved from server-side config, never
    from tool arguments (ADR-0019).

    Raises:
    ------
    ValueError
        For any visibility value outside the known set ``{'', 'user', 'team', 'mesh'}``.
    """
    if visibility not in _VALID_VISIBILITIES:
        raise ValueError(f"Unknown visibility: {visibility!r}. Use 'mesh', 'user', 'team', or ''.")
    if visibility == 'mesh':
        return ['mesh']
    if visibility == 'user':
        uid = get_user_id()
        return [f'user/{uid}'] if uid else []
    if visibility == 'team':
        tid = get_team_id()
        return [f'team/{tid}'] if tid else []
    # empty → all reachable
    scopes: list[str] = ['mesh']
    uid = get_user_id()
    if uid:
        scopes.append(f'user/{uid}')
    tid = get_team_id()
    if tid:
        scopes.append(f'team/{tid}')
    return scopes


def _split_zenoh_connect_endpoints(raw: str | None) -> list[str]:
    """Split ZENOH_CONNECT into endpoint strings.

    The project historically uses a single endpoint string such as
    ``tcp/127.0.0.1:7447``. For startup diagnostics we also tolerate a
    comma-separated list and treat any reachable endpoint as healthy.
    """
    if raw is None:
        return []
    return [part.strip() for part in raw.split(',') if part.strip()]


def _parse_tcp_endpoint(endpoint: str) -> tuple[str, int] | None:
    """Extract a TCP host/port pair from ``tcp/<host>:<port>``."""
    if not endpoint.startswith('tcp/'):
        return None
    host_port = endpoint.removeprefix('tcp/')
    host, sep, port_text = host_port.rpartition(':')
    if not sep or not host or not port_text:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    return host.strip('[]'), port


def _warn_if_zenoh_connect_unreachable() -> None:
    """Emit a startup warning when every configured TCP endpoint is unreachable."""
    raw = os.environ.get('ZENOH_CONNECT')
    if raw is None:
        return
    endpoints = _split_zenoh_connect_endpoints(raw)
    parsed = [_parse_tcp_endpoint(endpoint) for endpoint in endpoints]
    targets = [target for target in parsed if target is not None]
    if not targets:
        return

    last_error = 'unreachable'
    for host, port in targets:
        try:
            with closing(socket.create_connection((host, port), timeout=0.5)):
                return
        except OSError as e:
            last_error = str(e).strip() or type(e).__name__

    print(
        f'WARNING: ZENOH_CONNECT={raw} is unreachable ({last_error}). Saves will fail until the router is up.',
        file=sys.stderr,
    )


@mcp.tool()
def save_observation(
    content: str,
    project: str = '',
    tags: list[str] | None = None,
    memory_type: str = 'note',
    importance: int = 2,
    subject: str = '',
    summary: str = '',
    source_files: list[str] | None = None,
    references: list[str] | None = None,
    supersedes: list[str] | None = None,
    visibility: str = '',
) -> str:
    """Persist a work note / decision / discovery into the shared kioku-mesh memory.

    Call this PROACTIVELY after ANY decision, bug fix, discovery, or convention —
    do not wait for the user to ask. If you just made a design choice, fixed a
    non-obvious bug, or established a reusable pattern, call this now.

    **Save when**: design decision, non-obvious bug root cause, reusable
    pattern, config change with rationale, session summary.
    **Skip**: PR / Issue lifecycle ticks, restated PR / ADR / commit content,
    in-conversation progress logs, generic "tests pass" notes, status checks,
    transient notes, file listings, and routine tasks with no new learning.

    Prefer ``decision`` / ``bug`` / ``pattern`` / ``config`` over ``summary``.
    Use ``summary`` only for a session conclusion with a chosen direction, not
    as a synonym for "what happened". Treat ``importance`` 4-5 as project-wide
    or durable assumption changes; if an entry feels like importance 1-2,
    reconsider whether it should be saved at all.

    Identity (agent_family / client_id / pc_id / session_id) is resolved from
    environment, not from tool arguments. This prevents LLMs from corrupting
    the identity namespace by passing wrong values.

    Args:
        content: full-text body of the observation.
        project: optional project tag to scope the entry.
        tags: optional list of keyword tags.
        memory_type: category — one of "note", "decision", "bug", "pattern",
            "config", "summary" (default "note").
        importance: 1 (trivial) to 5 (critical), clamped automatically.
        subject: short topic / symbol name (e.g. "get_position latency").
        summary: one-line abstract shown in search results (fallback: content).
        source_files: related file paths for traceability.
        references: related PR / Issue / external identifiers.
        supersedes: list of observation_ids this entry replaces.
        visibility: replication scope — "user" (this user's machines only),
            "team" (the configured team), "mesh" (every mesh peer), or ""
            (default: follow the server-side configured default). The
            user_id / team_id behind the scoped tiers are resolved from
            server configuration, never from tool arguments (ADR-0019).
            An explicit value intentionally overrides the configured
            default — the MCP client is trusted at the host boundary
            (ADR-0014); scope restrictions, if ever needed, belong in a
            future server-side allowlist.

    Returns:
        The generated ``observation_id``.
    """
    if memory_type not in VALID_MEMORY_TYPES:
        return f'memory_type must be one of {sorted(VALID_MEMORY_TYPES)}. got: {memory_type!r}'
    try:
        effective_visibility, scope_id = resolve_write_visibility(visibility)
    except ValueError as e:
        return str(e)
    # ADR-0028 Phase5: save-lint (warn-only)
    lint_warnings = lint_observation(
        content=content,
        memory_type=memory_type,
        subject=subject,
        source_files=source_files,
    )
    obs = Observation(
        content=content,
        project=project,
        tags=tags or [],
        memory_type=memory_type,
        importance=importance,
        subject=subject,
        summary=summary,
        source_files=source_files or [],
        references=references or [],
        supersedes=supersedes or [],
        visibility=effective_visibility,
        scope_id=scope_id,
    )
    backend = get_backend()
    backend.put_observation(obs)
    result: dict = {
        'observation_id': obs.observation_id,
        'status': 'saved',
        'visibility': format_visibility(effective_visibility, scope_id),
        'warnings': [{'code': w.code, 'message': w.message} for w in lint_warnings],
    }
    # ADR-0026 §A: surface likely-superseded entries so the agent can replace
    # them. Only when ``supersedes`` was not already provided. Suggestion
    # only — nothing is hidden or deleted here.
    if not supersedes:
        try:
            candidates = backend.find_supersede_candidates(obs)
            if candidates:
                result['supersede_candidates'] = [
                    {
                        'observation_id': c.observation_id,
                        'created_at': c.created_at[:10],
                        'summary': c.summary or c.subject,
                    }
                    for c in candidates
                ]
        except Exception:  # noqa: BLE001 — detection must never fail a save
            pass
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def search_memory(
    query: str = '',
    agent_family: str = '',
    client_id: str = '',
    pc_id: str = '',
    session_id: str = '',
    project: str = '',
    since_iso: str = '',
    limit: int = 50,
    include_superseded: bool = False,
    search_mode: str = 'and',
) -> str:
    """Search the shared kioku-mesh memory, narrowing by key_expr and filtering in Python.

    If results are unexpectedly empty for work you know was done previously, this
    is a signal that ``save_observation`` may have been skipped — call it
    PROACTIVELY now to capture what is still in context before the session ends.

    ``limit`` defaults to 50 and is internally clamped to ``MAX_SEARCH``.
    Returned observation ids are full 32-char strings so ``delete_memory``
    can be called directly.
    Set ``include_superseded=True`` to also return observations that have been
    superseded by a newer one (hidden by default, ADR-0021).
    ``search_mode`` accepts 'and' (default) | 'or' | 'and_or'.
    'or': any query term matching is sufficient; base filters remain AND.
    'and_or': AND hits first, then OR hits fill remaining limit slots (recall mode).
    Unknown values return an error message.
    """
    try:
        from .memory.local_index import _validate_search_mode  # noqa: PLC0415

        _validate_search_mode(search_mode)
    except ValueError as exc:
        return str(exc)
    results = get_backend().search_observations(
        query=query,
        agent_family=agent_family,
        client_id=client_id,
        pc_id=pc_id,
        session_id=session_id,
        project=project,
        since_iso=since_iso,
        limit=limit,
        include_superseded=include_superseded,
        search_mode=search_mode,
    )
    if not results:
        return 'No matching memories.'
    lines = []
    for obs in results:
        body = obs.summary if obs.summary else obs.content[:80]
        subject_part = f' {obs.subject}' if obs.subject else ''
        project_part = f' ({obs.project})' if obs.project else ''
        refs_part = f' (refs: {", ".join(obs.references)})' if obs.references else ''
        lines.append(
            f'[{obs.memory_type}][{obs.importance}] {obs.created_at[:19]}'
            f'{project_part}{subject_part}{refs_part}\n'
            f'{body} <id={obs.observation_id}>'
        )
    return '\n---\n'.join(lines)


@mcp.tool()
def get_memory(observation_id: str) -> str:
    """Get full content and metadata for a single observation by ID.

    Use this after ``search_memory`` to retrieve the complete record for a
    result that looks relevant. Returns all fields including the extended
    schema fields added in Phase 2 (memory_type, importance, subject,
    summary, source_files, references, supersedes).
    """
    if len(observation_id) != 32:
        return 'observation_id must be a full 32-character match.'
    backend = get_backend()
    obs = backend.find_observation_by_id(observation_id)
    if obs is None:
        return f'observation_id {observation_id} not found.'
    _obs_extras = obs._extras if hasattr(obs, '_extras') else {}  # noqa: SLF001
    superseded_by = _obs_extras.get('superseded_by')
    idx = getattr(backend, '_idx', None) or get_index()  # Phase1 pattern: active backend index
    state_info = idx.inspect_by_id(observation_id)
    state = state_info['state'] if state_info else 'live'
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
        f'superseded_by: {superseded_by or "-"}',
        f'state: {state}',
        '---',
        obs.content,
    ]
    return '\n'.join(lines)


def _normalize_list_filter(v: list[str] | None) -> list[str] | None:
    """Return cleaned list or None (meaning no filter)."""
    if v is None:
        return None
    clean = [s for s in v if s]
    return clean if clean else None


def _resolve_active_index():  # noqa: ANN202
    """Return the active LocalIndex, or None if the index is disabled."""
    from .memory.local_index import LocalIndex  # noqa: PLC0415

    backend = get_backend()
    idx = getattr(backend, '_idx', None) or get_index()
    if idx is None or (isinstance(idx, LocalIndex) and idx.disabled):
        return None
    return idx


def _clamp_recall_limit(limit: int) -> int:
    return max(1, min(limit, 100))


def _format_recall_markdown(hits: list, total: int, filters_summary: str) -> str:
    if not hits:
        return 'No matching current context.'
    lines = [f'recall_context: {total} result(s)', filters_summary, '']
    # Group by (project or "-", memory_type) in first-hit order.
    groups: dict[tuple[str, str], list] = {}
    for item in hits:
        obs = item['obs']
        key = (obs.project or '-', obs.memory_type)
        if key not in groups:
            groups[key] = []
        groups[key].append(item)
    for (proj, mtype), items in groups.items():
        lines.append(f'### project={proj} / memory_type={mtype}')
        lines.append('')
        for item in items:
            obs = item['obs']
            state = item.get('state', 'live')
            lines.append(f'id: {obs.observation_id}')
            lines.append(f'state: {state}')
            lines.append(f'importance: {obs.importance}')
            lines.append(f'created_at: {obs.created_at}')
            lines.append(f'subject: {obs.subject or "-"}')
            lines.append(f'summary: {obs.summary or "-"}')
            lines.append(f'tags: {", ".join(obs.tags) if obs.tags else "-"}')
            lines.append(f'source_files: {", ".join(obs.source_files) if obs.source_files else "-"}')
            lines.append(f'references: {", ".join(obs.references) if obs.references else "-"}')
            if obs.supersedes:
                lines.append(f'supersedes: {", ".join(obs.supersedes)}')
            superseded_by = obs._extras.get('superseded_by') if hasattr(obs, '_extras') else None  # noqa: SLF001
            if superseded_by:
                lines.append(f'superseded_by: {superseded_by}')
            lines.append('content:')
            lines.append(obs.content)
            lines.append('')
    return '\n'.join(lines)


@mcp.tool()
def recall_context(
    query: str = '',
    project: str = '',
    memory_types: list[str] | None = None,
    source_files: list[str] | None = None,
    references: list[str] | None = None,
    since_iso: str = '',
    limit: int = 20,
    search_mode: str = 'and_or',
) -> str:
    """Recall current context with additive filters for memory_types, source_files, and references.

    Returns a deterministic grouped Markdown view of live observations.
    Hidden states (tombstoned, shadowed, superseded) are excluded by default.
    Requires the local index (use search_memory as fallback if index is disabled).

    Args:
        query: recall intent; empty means browse recent context after other filters.
        project: optional exact project filter.
        memory_types: optional list of memory_type values (must be in VALID_MEMORY_TYPES).
        source_files: optional exact-match source_files filter.
        references: optional exact-match references filter.
        since_iso: optional lower created_at bound (ISO 8601).
        limit: maximum results (clamped to 1..100).
        search_mode: 'and' | 'or' | 'and_or' (default and_or).
    """
    if search_mode not in ('and', 'or', 'and_or'):
        return f"search_mode must be one of 'and', 'or', 'and_or'. got: {search_mode!r}"
    if memory_types is not None:
        invalid = [t for t in memory_types if t and t not in VALID_MEMORY_TYPES]
        if invalid:
            return f'memory_types contains invalid values {invalid}. Must be from {sorted(VALID_MEMORY_TYPES)}.'
    memory_types_norm = _normalize_list_filter(memory_types)
    source_files_norm = _normalize_list_filter(source_files)
    references_norm = _normalize_list_filter(references)
    idx = _resolve_active_index()
    if idx is None:
        return 'recall_context requires the local index; run without KIOKU_MESH_DISABLE_INDEX=1 or use search_memory.'
    limit = _clamp_recall_limit(limit)
    hits_obs = idx.search(
        query=query,
        project=project,
        since_iso=since_iso,
        limit=limit,
        search_mode=search_mode,
        include_deleted=False,
        include_superseded=False,
        memory_types=memory_types_norm,
        source_files=source_files_norm,
        references=references_norm,
    )
    hits = []
    for obs in hits_obs:
        state_info = idx.inspect_by_id(obs.observation_id)
        state = state_info['state'] if state_info else 'live'
        hits.append({'obs': obs, 'state': state})
    filter_parts = []
    if project:
        filter_parts.append(f'project={project!r}')
    if memory_types_norm:
        filter_parts.append(f'memory_types={memory_types_norm}')
    if source_files_norm:
        filter_parts.append(f'source_files={source_files_norm}')
    if references_norm:
        filter_parts.append(f'references={references_norm}')
    if since_iso:
        filter_parts.append(f'since={since_iso!r}')
    if query:
        filter_parts.append(f'query={query!r}')
    filters_summary = 'filters: ' + (', '.join(filter_parts) if filter_parts else 'none')
    return _format_recall_markdown(hits, len(hits), filters_summary)


@mcp.tool()
def delete_memory(observation_id: str, reason: str = '') -> str:
    """Soft-delete an observation by emitting a Tombstone.

    Requires the full 32-char observation_id (no short-id lookup) to avoid
    accidental deletion. Physical cleanup is deferred to a GC job.
    """
    if len(observation_id) != 32:
        return 'observation_id must be a full 32-character match.'
    obs = get_backend().find_observation_by_id(observation_id)
    if obs is None:
        return f'observation_id {observation_id} not found.'
    get_backend().put_tombstone(obs, reason=reason)
    return f'deleted (tombstone): {observation_id}'


_SESSION_ID_TS_RE = re.compile(r'^(\d{8}T\d{6}Z)')

# Issue #158 Phase 2: thresholds for the "consider saving" nudge. Tuned so a
# truly idle / read-only session does not get spammed: only nudge after the
# session has been alive for a while and has accumulated zero saves, or after
# a long quiet stretch since the last save.
_NUDGE_SESSION_AGE_S_NO_SAVES = 600  # 10 min
_NUDGE_LAST_SAVE_AGE_S = 1200  # 20 min


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO8601 ``created_at`` value. Returns ``None`` if unparsable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None


def _parse_session_started_at(session_id: str) -> datetime | None:
    """Recover the session start time from the ``YYYYMMDDTHHMMSSZ-...`` prefix.

    Sessions created from a custom ``KIOKU_MESH_SESSION_ID`` may not carry a
    parseable prefix; callers must tolerate ``None``.
    """
    m = _SESSION_ID_TS_RE.match(session_id)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_age(seconds: float | None) -> str:
    """Render a coarse "Xm Ys" / "Xs" age string. Returns ``'-'`` for ``None``."""
    if seconds is None:
        return '-'
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m{rem:02d}s'
    hours, rem_m = divmod(minutes, 60)
    return f'{hours}h{rem_m:02d}m'


def _compute_save_nudge(
    this_session_saves: int,
    last_save_age_s: float | None,
    session_age_s: float | None,
) -> str | None:
    """Decide whether to emit a "consider saving" nudge for the current session.

    Heuristic only; never used to auto-save. See Issue #158.
    """
    if this_session_saves == 0:
        if session_age_s is not None and session_age_s >= _NUDGE_SESSION_AGE_S_NO_SAVES:
            return (
                'No save_observation calls in this session yet — if any decision, '
                'preference, bug root cause, or pattern has been settled, save it now. '
                'Ignore if the session is truly read-only / idle.'
            )
        return None
    if last_save_age_s is not None and last_save_age_s >= _NUDGE_LAST_SAVE_AGE_S:
        return (
            f'Last save was {_format_age(last_save_age_s)} ago in this session — '
            'review whether any newer decision or finding is still unsaved.'
        )
    return None


@mcp.tool()
def get_memory_status() -> str:
    """Summarize the server's view of the kioku-mesh memory for troubleshooting.

    Check ``last_save_at`` and the ``this_session_*`` block in the output —
    if ``this_session_saves`` is 0 in a long-running session, or ``nudge`` is
    present, you have likely skipped ``save_observation``. Call it PROACTIVELY
    now if there are unsaved decisions or discoveries.

    Counts are computed from up to ``MAX_SEARCH`` most-recent entries.
    Per-session counts are derived by re-querying the store with
    ``session_id == current`` so process restarts and multi-process layouts
    stay consistent. Exception messages preserve the type name so connection
    / query / implementation failures are distinguishable.
    """
    try:
        backend = get_backend()
        recent = backend.search_observations(limit=MAX_SEARCH)
        status = backend.get_status()
        by_family: dict[str, int] = {}
        by_pc: dict[str, int] = {}
        for obs in recent:
            by_family[obs.agent_family] = by_family.get(obs.agent_family, 0) + 1
            by_pc[obs.pc_id] = by_pc.get(obs.pc_id, 0) + 1
        truncated = len(recent) >= MAX_SEARCH
        last_save_at = recent[0].created_at if recent else '-'
        session_id = get_session_id()
        # Per-session save count is sourced from the store, not process-local
        # counters, so it survives MCP server restarts (#158 Codex review).
        try:
            session_obs = search_observations(session_id=session_id, limit=MAX_SEARCH)
        except Exception:  # noqa: BLE001 — diagnostics must not break get_memory_status
            session_obs = []
        this_session_saves = len(session_obs)
        now = datetime.now(timezone.utc)
        last_save_dt = _parse_iso(session_obs[0].created_at) if session_obs else None
        last_save_age_s = (now - last_save_dt).total_seconds() if last_save_dt else None
        session_started_at = _parse_session_started_at(session_id)
        session_age_s = (now - session_started_at).total_seconds() if session_started_at else None
        nudge = _compute_save_nudge(this_session_saves, last_save_age_s, session_age_s)
        lines = [
            f'last_save_at: {last_save_at}',
            f'kioku-mesh version: {__version__}',
            f'backend: {status.mode}',
            f'python: {sys.executable}',
            f'pc_id: {get_pc_id()}',
            f'session_id: {session_id}',
            f'session_age: {_format_age(session_age_s)}',
            f'this_session_saves: {this_session_saves}',
            f'this_session_last_save_age: {_format_age(last_save_age_s)}',
            f'zenoh_session: {status.zenoh_session}',
            f'last_put_at_iso: {status.last_put_at_iso or "-"}',
            f'last_put_status: {status.last_put_status}',
            f'pending_puts: {status.pending_puts}',
            f'index_rows: live={status.live} / tomb={status.tombstoned} / shadow={status.shadowed}',
            f'count (within limit {MAX_SEARCH}): {len(recent)}'
            + (' (limit may be reached; consider narrowing)' if truncated else ''),
        ]
        if nudge:
            lines.append(f'nudge: {nudge}')
        for family, count in sorted(by_family.items()):
            lines.append(f'  family {family}: {count}')
        for pc, count in sorted(by_pc.items()):
            lines.append(f'  pc {pc[:8]}: {count}')
        return '\n'.join(lines)
    except Exception as e:  # noqa: BLE001
        return f'failed to read shared memory [{type(e).__name__}]: {e}'


@mcp.tool()
def drain_pending_puts(limit: int | None = None) -> str:
    """Replay pending queued puts immediately through the current MCP process."""
    if limit is not None and limit < 1:
        return 'limit must be 1 or greater.'
    drained = get_backend().drain_pending(limit=limit, wait=True)
    remaining = get_backend().get_status().pending_puts
    return f'pending_puts drain complete: drained={drained}, remaining={remaining}'


@mcp.tool()
def check_messages(
    limit: int = 20,
    visibility: str = '',
    include_acked: bool = False,
    include_expired: bool = False,
    since_iso: str = '',
) -> str:
    """Poll the kioku-mesh inbox for pending messages addressed to this session.

    Queries Zenoh for messages delivered to the current session and agent,
    registers them in the local inbox index, and returns unread entries.

    ``user_id``, ``team_id``, ``session_id``, and ``pc_id`` are resolved
    server-side from config and environment — they are intentionally NOT
    tool arguments (ADR-0019 / ADR-0022).

    Args:
        limit: maximum number of messages to return (1–100, default 20).
        visibility: scope to query — ``''`` (all configured), ``user``,
            ``team``, or ``mesh``.
        include_acked: include already-acknowledged messages (default False).
        include_expired: include TTL-expired messages, for debugging (default False).
        since_iso: optional ISO 8601 lower bound for ``created_at``.

    Returns:
        JSON string with shape ``{"messages": [...], "count": N, "truncated": bool}``.
    """
    limit = max(1, min(100, limit))
    try:
        scopes = _messaging_scopes(visibility)
    except ValueError as e:
        return json.dumps({'error': str(e)})
    session_id = get_session_id()
    from .core.identity import get_client_id

    agent_id = get_client_id()
    index = _get_messaging_index()

    since_dt: datetime | None = None
    if since_iso:
        try:
            since_dt = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
        except ValueError:
            return json.dumps({'error': f'invalid since_iso: {since_iso!r}'})

    messages: list[Message] = []
    seen_ids: set[str] = set()

    try:
        session = _get_zenoh_session()
    except Exception as e:  # noqa: BLE001
        return json.dumps({'error': f'Zenoh session unavailable: {type(e).__name__}: {e}'})

    for scope in scopes:
        selectors = [
            f'msg/{scope}/inbox/session/{session_id}/**',
            f'msg/{scope}/inbox/agent/{agent_id}/**',
        ]
        for selector in selectors:
            try:
                for reply in session.get(selector, timeout=3.0):
                    if not reply.ok:
                        continue
                    msg_key = str(reply.ok.key_expr)
                    try:
                        json_str = reply.ok.payload.to_bytes().decode('utf-8')
                        msg = Message.from_json(json_str)
                    except Exception:  # noqa: BLE001
                        continue
                    # Dedup by msg_id across multiple selectors before any action.
                    if msg.msg_id in seen_ids:
                        continue
                    seen_ids.add(msg.msg_id)
                    # Storage-level TTL purge (Issue #215): delete expired entries
                    # from Zenoh so they do not accumulate indefinitely.
                    # include_expired=True is read-only — skip delete so debug
                    # inspection does not destroy storage.
                    if is_expired(msg):
                        if not include_expired:
                            try:
                                session.delete(msg_key)
                            except Exception:  # noqa: BLE001 — best-effort; non-fatal
                                pass
                            continue
                    # Override scope from key context if not set on message
                    if not msg.scope:
                        msg.scope = scope
                    index.register(msg, session_id)
                    messages.append(msg)
            except Exception:  # noqa: BLE001
                pass

    # Purge expired entries from the local SQLite index in sync with the
    # Zenoh deletes issued above.
    try:
        index.purge_expired()
    except Exception as _e:  # noqa: BLE001
        log.debug('check_messages: inline purge_expired failed: %s', _e)

    # Apply filters
    filtered: list[Message] = []
    for msg in messages:
        if not include_expired and is_expired(msg):
            continue
        if not include_acked and index.is_acked(msg.msg_id, session_id):
            continue
        if since_dt is not None:
            created = msg.created_at
            if created.tzinfo is None:
                from datetime import timezone as _tz

                created = created.replace(tzinfo=_tz.utc)
            if created < since_dt:
                continue
        filtered.append(msg)

    # Sort: (created_at, sender_seq, msg_id) ascending
    def _sort_key(m: Message) -> tuple[str, int, str]:
        ts = m.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if m.created_at else ''
        seq = m.sender_seq if m.sender_seq is not None else 0
        return ts, seq, m.msg_id

    filtered.sort(key=_sort_key)
    truncated = len(filtered) > limit
    page = filtered[:limit]

    items = []
    for msg in page:
        sender = msg.sender if isinstance(msg.sender, dict) else {}
        recipient = msg.recipient if isinstance(msg.recipient, dict) else {}
        body = msg.body if msg.body else msg.payload
        items.append(
            {
                'msg_id': msg.msg_id,
                'subject': msg._extras.get('subject', ''),  # noqa: SLF001
                'body': body,
                'created_at': msg.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if msg.created_at else '',
                'expires_at': msg.expires_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if msg.expires_at else None,
                'scope': msg.scope,
                'sender': {
                    'agent_id': sender.get('agent_id', msg.sender_id),
                    'session_id': sender.get('session_id', ''),
                },
                'recipient': {
                    'kind': recipient.get('kind', 'session'),
                    'session_id': recipient.get('session_id', ''),
                },
                'acked': index.is_acked(msg.msg_id, session_id),
                'delivery_adapters': msg.delivery_adapters,
            }
        )

    return json.dumps({'messages': items, 'count': len(items), 'truncated': truncated})


@mcp.tool()
def ack_message(
    msg_id: str,
    visibility: str = '',
) -> str:
    """Acknowledge a kioku-mesh inbox message as processed by this session.

    Records the ack in the local inbox index and publishes the ack key to
    Zenoh so the sender can observe delivery.

    ``recipient_session_id`` is resolved from the current process's
    session identity — it is intentionally NOT a tool argument (ADR-0022).

    Args:
        msg_id: full 32-hex message id.
        visibility: scope hint — ``''`` (look up from local index), ``user``,
            ``team``, or ``mesh``.

    Returns:
        Confirmation string ``acked: <msg_id> (scope=<scope>)`` on success.
    """
    if not msg_id or len(msg_id) != 32:
        return 'msg_id must be a full 32-hex string.'
    session_id = get_session_id()
    index = _get_messaging_index()

    # Determine scope: prefer local index lookup, fall back to visibility param
    scope = index.find_scope(msg_id, session_id)
    if scope is None:
        if visibility:
            try:
                scopes = _messaging_scopes(visibility)
            except ValueError as e:
                return f'ack failed: {e}'
            scope = scopes[0] if scopes else 'mesh'
        else:
            scope = 'mesh'

    try:
        _ack_message_internal(index, msg_id, session_id)
    except ValueError as e:
        return f'ack failed: {e}'

    # Publish ack to Zenoh (best-effort; local ack is already recorded)
    try:
        zenoh_session = _get_zenoh_session()
        key = ack_key(scope, msg_id, session_id)
        payload = json.dumps({'msg_id': msg_id, 'recipient_session_id': session_id, 'status': 'acknowledged'}).encode(
            'utf-8'
        )
        zenoh_session.put(key, payload)
    except Exception as e:  # noqa: BLE001
        # Local ack succeeded; Zenoh publish failure is non-fatal
        return f'acked: {msg_id} (scope={scope}) [zenoh_publish_failed: {type(e).__name__}]'

    return f'acked: {msg_id} (scope={scope})'


@mcp.tool()
def purge_expired_messages() -> str:
    """Scan the Zenoh msg/** namespace and delete all TTL-expired messages.

    Performs a full storage-level GC sweep across all ``msg/**`` keys
    (not limited to the current session's inbox). Expired entries are
    removed from both Zenoh storage and the local SQLite inbox index.

    TTL expiry follows message-level precedence:
    ``expires_at`` > ``ttl_sec + created_at`` > never-expires.

    Returns:
        Summary string: ``purged N expired message(s)`` or an error message.
    """
    index = _get_messaging_index()
    try:
        session = _get_zenoh_session()
    except Exception as e:  # noqa: BLE001
        return f'purge failed: Zenoh session unavailable: {type(e).__name__}: {e}'
    try:
        count, scan_ok = purge_expired_msgs(session, index)
    except Exception as e:  # noqa: BLE001
        return f'purge failed: {type(e).__name__}: {e}'
    if not scan_ok:
        return 'purge incomplete: scan failed (0 messages purged)'
    return f'purged {count} expired message(s)'


def _is_tty_misinvocation() -> bool:
    """Return True when stdin is a TTY and KIOKU_MESH_MCP_ALLOW_TTY is not set."""
    if get_env('KIOKU_MESH_MCP_ALLOW_TTY') == '1':
        return False
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def main() -> None:
    """Entry point for the ``kioku-mesh-mcp`` console script."""
    if _is_tty_misinvocation():
        print(
            'kioku-mesh-mcp is the stdio MCP server. It is meant to be spawned\n'
            'by an MCP client (Claude Code, Codex CLI, Claude Desktop, etc.),\n'
            'not run interactively.\n'
            '\n'
            'If you wanted to register this server with a client, run:\n'
            '    kioku-mesh mcp install --client claude-code\n'
            '    kioku-mesh mcp install --client codex-cli\n'
            '\n'
            'To force interactive launch anyway (debugging), pipe stdin from /dev/null:\n'
            '    kioku-mesh-mcp < /dev/null\n'
            '\n'
            'Or set KIOKU_MESH_MCP_ALLOW_TTY=1 to bypass this check.',
            file=sys.stderr,
        )
        sys.exit(2)
    if get_backend_mode() != 'local':
        _warn_if_zenoh_connect_unreachable()
        start_pending_drain_background()
    try:
        mcp.run()
    finally:
        if get_backend_mode() != 'local':
            stop_pending_drain_background()
        reset_backend()


if __name__ == '__main__':
    main()

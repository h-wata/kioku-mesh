"""FastMCP server exposing kioku-mesh tools to coding agents.

Identity fields (agent_family, client_id, pc_id, session_id) are resolved
from environment/state on the server side. They are intentionally NOT
arguments to ``save_observation`` so an LLM cannot contaminate the id
space by guessing values. Narrow-down is allowed on ``search_memory``.
"""

from contextlib import closing
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
import os
import re
import socket
import sys

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import __version__
from .backend import get_backend
from .backend import reset_backend
from .config import format_visibility
from .config import get_backend_mode
from .config import get_team_id
from .config import get_user_id
from .config import resolve_write_visibility
from .core.identity import state_dir
from .core.project_alias import expand_project_aliases
from .core.transport import get_session as _get_zenoh_session
from .identity import get_pc_id
from .identity import get_session_id
from .memory.metadata import MetadataRequiredError
from .memory.metadata import validate_required_metadata
from .memory.save_lint import lint_observation
from .messaging.keyspace import ack_key
from .messaging.local_index import ack_message as _ack_message_internal
from .messaging.local_index import INGRESS_EXPIRED_ON_ARRIVAL
from .messaging.local_index import IngressResult
from .messaging.local_index import LocalMessageIndex
from .messaging.models import is_expired
from .messaging.models import Message
from .messaging.purge import purge_expired_msgs
from .models import Observation
from .models import resolve_expires_at
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

PROACTIVE READ — before starting work, call ``recall_context`` once with
{project, limit} to restore prior context for this project. Do this at the
start of a session, not only when the user explicitly asks to recall
something; skipping it means you re-derive decisions and conventions that
were already saved.

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
Before calling ``save_observation`` with ``memory_type="summary"``, self-check
that the entry states a *chosen direction*, not an activity log. "PR merged",
"issues closed", "review posted", or "migration/test ping" style statements
are not, by themselves, worth saving as a summary — apply the SKIP rules above
to your own summary candidates too, not only to other memory_types.

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

When updating or correcting an existing memory on the same topic, do not save
an independent new entry — pass the prior observation_id(s) in
``save_observation``'s ``supersedes`` argument so the store records the
replacement instead of accumulating duplicates.

Self-check after every task: "Did the user or I just make a decision, confirm a
recommendation, fix a bug, learn something, or establish a convention? If yes →
``save_observation`` NOW." Skip transient notes, status checks, and routine
tasks with no new learning.

SEARCH MEMORY (``search_memory`` → ``get_memory``) when:
- The user asks to recall anything ("remember", "what did we do", "前にやった")
- Starting work on something that may have prior context
- The user references a topic you have no context on
- The user's first message names a feature, file, or problem — search before answering

Search pattern: first query with a ``project`` filter and a generous ``limit``,
keeping the query terms to one or two proper nouns / IDs. If that returns zero
results, do not conclude "nothing is stored" yet — retry with (1) the same
concept's key terms in the other language (EN <-> JA/ZH/KO) and (2)
``search_mode='or'``. Only after both retries come up empty is "no prior
memory" a safe conclusion. For broad "what's the context here" recall, prefer
``recall_context`` over ``search_memory``.

Identity (agent_family / client_id / pc_id / session_id) is resolved on the
server side from environment + state. Do not pass these as tool arguments;
they are intentionally not parameters of ``save_observation``.

CROSS-PC ORIGIN — memories replicate across every host in the mesh. An entry
marked ``other pc`` (``origin:`` line in ``recall_context`` / ``get_memory``,
``[origin: ...]`` suffix in ``search_memory``) was written on a DIFFERENT
machine: absolute file paths, tmux pane/session/window targets, ports, PIDs,
and "currently running" state in it describe the ORIGIN host, not this one.
Verify such details exist locally before acting on them — never resume another
host's tmux pane, worktree, or in-flight process as if it were yours. The same
applies when WRITING: if an entry contains host-local details, say which host
they belong to in the content so future readers on other machines are not
misled.

Use ``memory_type`` accurately — one of: note, decision, bug, pattern, config,
summary. Prefer decision / bug / pattern / config over summary; use summary only
for a session conclusion with a chosen direction, not as a log of what happened.
Set ``importance`` 1–5 with care: 5 = changes a project-wide assumption or
durable rule, 4 = a decision reused across future sessions, 3 = a local,
reusable lesson, 1-2 = barely worth saving at all (only after the SKIP rules
still say to save it). A single inconclusive experiment's result is usually a
3, not a 5. If most of your entries land at 4-5, you are over-rating them.
``subject`` and ``summary`` are REQUIRED — a save that omits either, or that
passes a placeholder like ``-`` / ``N/A`` / ``TBD``, is rejected and must be
retried with real values. They are what search and recall render before
falling back to the body, so an entry without them costs every future reader a
full-content read. Put the proper nouns a future searcher would actually type
into ``subject`` / ``summary``. FTS only matches the language an entry was saved in, so a
project-specific term should be written in both English and Japanese (e.g.
subject: "Dispatcher default model / opus 既定") — a Japanese-only entry is
invisible to this mesh's English-speaking agents searching in English, and
vice versa.

``get_memory_status`` reports a ``family <name>: N`` breakdown, but this is an
aggregate over recently *saved* observations, not the current session's live
identity — it cannot confirm the current session is resolved correctly, and a
past unknown entry can linger in the count even after identity is fixed. If
``family unknown`` keeps showing up there, past sessions likely failed to
resolve identity; check the ``KIOKU_MESH_*`` identity environment variables.
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


def _message_diagnostic(msg: Message, verdict: IngressResult) -> dict[str, object]:
    """Describe a message that was withheld, including enough to act on it.

    The envelope travels with the diagnostic on purpose: the caller needs to see
    what it did not receive in order to decide whether the withheld payload
    matters, and a bare "count=0" is exactly the silent failure this replaces.
    """
    sender = msg.sender if isinstance(msg.sender, dict) else {}
    return {
        'code': verdict.code,
        'msg_id': verdict.msg_id,
        'recipient_session_id': verdict.recipient_session_id,
        'message': {
            'subject': msg._extras.get('subject', ''),  # noqa: SLF001
            'body': msg.body if msg.body else msg.payload,
            'created_at': msg.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if msg.created_at else '',
            'expires_at': msg.expires_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if msg.expires_at else None,
            'scope': msg.scope,
            'sender': {
                'agent_id': sender.get('agent_id', msg.sender_id),
                'session_id': sender.get('session_id', ''),
            },
        },
        'ack': dict(verdict.detail),
        'remedy': verdict.remedy or '',
    }


def _acked_flag(
    verdict: IngressResult | None,
    index: LocalMessageIndex,
    msg_id: str,
    session_id: str,
) -> bool:
    """Report the ack state the delivery decision was actually made on.

    Reading it back from the index would let the reported flag disagree with the
    filtering above, which is the kind of gap that makes a suppression bug hard
    to see from the outside.
    """
    return verdict.acked if verdict is not None else index.is_acked(msg_id, session_id)


def _ingress_error_diagnostic(code: str, source: str, error: BaseException) -> dict[str, object]:
    """Describe an ingress failure that never produced a message to describe.

    Same shape as :func:`_message_diagnostic` with an empty envelope, so a
    caller reads one list rather than having to know which failures come with a
    message attached and which do not.
    """
    return {
        'code': code,
        'msg_id': '',
        'recipient_session_id': '',
        'message': None,
        'source': source,
        'ack': {'error': f'{type(error).__name__}: {error}'},
        'remedy': (
            'This inbox listing is incomplete: an arrival could not be read. Poll again, and check the '
            'Zenoh session if it keeps happening.'
        ),
    }


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
    subject: str,
    summary: str,
    project: str = '',
    tags: list[str] | None = None,
    memory_type: str = 'note',
    importance: int = 2,
    source_files: list[str] | None = None,
    references: list[str] | None = None,
    supersedes: list[str] | None = None,
    visibility: str = '',
    expires_at: str = '',
    ttl_sec: int = 0,
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
        subject: REQUIRED (no default — part of the tool's input schema
            ``required`` list). Short topic / symbol name (e.g. "get_position
            latency"). Placeholder values ("-", "N/A", "TBD") are rejected
            with a tool error.
        summary: REQUIRED (no default). One-line abstract shown in search
            results. Placeholder values are rejected with a tool error.
        project: optional project tag to scope the entry.
        tags: optional list of keyword tags.
        memory_type: category — one of "note", "decision", "bug", "pattern",
            "config", "summary" (default "note").
        importance: 1 (trivial) to 5 (critical), clamped automatically.
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
        expires_at: ISO 8601 instant after which this entry is disposable.
            Use for records that are useful now but must not linger — a
            verification ping, a "delete after the report lands" scratch
            note. Expired entries stop appearing in ``recall_context`` /
            ``search_memory`` and become candidates for
            ``kioku-mesh gc-observations``. Omit for durable memory: an
            entry worth ``importance`` 4-5 almost never wants a lifetime.
        ttl_sec: convenience form of ``expires_at`` — seconds from now.
            Ignored when ``expires_at`` is given.

    Returns:
        The generated ``observation_id``.
    """
    if memory_type not in VALID_MEMORY_TYPES:
        return f'memory_type must be one of {sorted(VALID_MEMORY_TYPES)}. got: {memory_type!r}'
    try:
        validate_required_metadata(subject, summary)
    except MetadataRequiredError as e:
        # Raised, not returned: a returned string is is_error=false on the MCP
        # wire, so a caller that dropped subject/summary would read the refusal
        # as a successful save and never retry. ToolError carries the message
        # through as a protocol-level tool error instead.
        raise ToolError(str(e)) from e
    try:
        resolved_expires_at = resolve_expires_at(expires_at=expires_at, ttl_sec=ttl_sec)
    except ValueError as e:
        return str(e)
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
        expires_at=resolved_expires_at,
    )
    backend = get_backend()
    backend.put_observation(obs)
    result: dict = {
        'observation_id': obs.observation_id,
        'status': 'saved',
        'visibility': format_visibility(effective_visibility, scope_id),
        'warnings': [{'code': w.code, 'message': w.message} for w in lint_warnings],
    }
    if resolved_expires_at:
        result['expires_at'] = resolved_expires_at
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
        except Exception as e:  # noqa: BLE001 — detection must never fail a save
            log.debug('supersede suggestion failed (save succeeded): %s', e, exc_info=True)
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
    Observations whose ``expires_at`` has passed are always hidden here
    (Issue #272); they are only reachable via ``get_memory`` by id.
    ``search_mode`` accepts 'and' (default) | 'or' | 'and_or'.
    'or': any query term matching is sufficient; base filters remain AND.
    'and_or': AND hits first, then OR hits fill remaining limit slots (recall mode).
    Unknown values return an error message.
    When ``search_mode`` is left at its default 'and' and that AND search comes
    back empty, a second search with ``search_mode='or'`` is run automatically
    (Issue #276) so agents searching with natural-language queries are not
    penalized for AND's low recall. The fallback is reported by prefixing the
    result with ``(no AND match; fell back to OR)``. This does not change the
    default itself — only 'and' auto-retries; explicit 'or'/'and_or' calls and
    the ``recall_context`` tool are unaffected.
    """
    try:
        from .memory.local_index import _validate_search_mode  # noqa: PLC0415

        _validate_search_mode(search_mode)
    except ValueError as exc:
        return str(exc)
    # Issue #278: expand the project filter exactly once, here, and rebind the
    # parameter so every query issued below — including ones added later, e.g.
    # a retry with a different search_mode — inherits the expanded filter
    # instead of re-deriving it per call site (PR #288 review B3).
    project = expand_project_aliases(project)  # type: ignore[assignment]
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
    fell_back_to_or = False
    if not results and search_mode == 'and':
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
            search_mode='or',
        )
        fell_back_to_or = bool(results)
    if not results:
        return 'No matching memories.'
    lines = []
    if fell_back_to_or:
        lines.append('(no AND match; fell back to OR)')
    for obs in results:
        body = obs.summary if obs.summary else obs.content[:80]
        subject_part = f' {obs.subject}' if obs.subject else ''
        project_part = f' ({obs.project})' if obs.project else ''
        refs_part = f' (refs: {", ".join(obs.references)})' if obs.references else ''
        note = _origin_note(obs)
        origin_part = f' [origin: {obs.client_id or "?"}, {note}]' if note != 'this pc' else ''
        lines.append(
            f'[{obs.memory_type}][{obs.importance}] {obs.created_at[:19]}'
            f'{project_part}{subject_part}{refs_part}{origin_part}\n'
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
        f'origin: {obs.client_id or "-"} ({_origin_note(obs)})',
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


def _origin_note(obs: Observation) -> str:
    """Classify where an observation was written relative to this process's host.

    Compares the stored ``pc_id`` against this host's ``get_pc_id()``:
    ``'this pc'`` on match, ``'other pc'`` on mismatch, ``'unknown pc'`` when
    the entry carries no pc_id (pre-identity legacy payloads).
    """
    if not obs.pc_id:
        return 'unknown pc'
    return 'this pc' if obs.pc_id == get_pc_id() else 'other pc'


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
            lines.append(f'origin: {obs.client_id or "-"} ({_origin_note(obs)})')
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
    Hidden states (tombstoned, shadowed, superseded, expired) are excluded by default.
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
    # Issue #278: same single expansion point as ``search_memory``. The raw
    # input is kept for the filters summary below so the caller can see both
    # what they asked for and what it matched.
    project_filter = expand_project_aliases(project)
    hits_obs = idx.search(
        query=query,
        project=project_filter,
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
        also_matched = [value for value in project_filter if value != project]
        alias_note = f' (also matching {", ".join(repr(v) for v in also_matched)})' if also_matched else ''
        filter_parts.append(f'project={project!r}{alias_note}')
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


def _utcnow() -> datetime:
    """Return the current UTC time.

    Indirection over ``datetime.now(timezone.utc)`` so tests can freeze the
    clock and pin time-window boundaries exactly.
    """
    return datetime.now(timezone.utc)


def _parse_iso_utc(ts: str) -> datetime | None:
    """Parse an ISO8601 ``created_at`` into a UTC-aware datetime, or ``None``.

    ``Observation`` does not validate ``created_at`` on construction and the
    local index tolerates legacy bad writes, so a stored value may be
    offset-less ("naive"). kioku-mesh always writes UTC, so a naive value is
    interpreted as UTC rather than dropped; comparing it against an aware
    ``now`` would otherwise raise ``TypeError`` and take down the whole
    diagnostic output. Missing / unparsable values still return ``None`` and
    are skipped by callers.
    """
    dt = _parse_iso(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    / query / implementation failures are distinguishable. Alongside the
    all-time ``family <name>: N`` breakdown, a separate last-7-days
    ``family_7d <name>: N`` breakdown is included so a recent drop in save
    activity is visible even though it is masked in the all-time counts
    (Issue #280). That window is ``[now-7d, now]`` inclusive on both ends:
    future-dated rows are excluded, rows with a missing / unparsable
    ``created_at`` are skipped individually (never failing the whole output),
    and offset-less ("naive") timestamps are read as UTC. If the underlying
    search hit its ``MAX_SEARCH`` limit while still inside the 7-day window,
    the section is labelled ``PARTIAL`` and each count is prefixed with
    ``>=`` to mark it as a lower bound.
    """
    try:
        backend = get_backend()
        recent = backend.search_observations(limit=MAX_SEARCH)
        status = backend.get_status()
        by_family: dict[str, int] = {}
        by_pc: dict[str, int] = {}
        # search_observations() excludes tombstoned/shadowed rows by default
        # (include_deleted=False), so no extra filtering is needed here to
        # keep deleted observations out of the 7-day window count.
        now = _utcnow()
        seven_days_ago = now - timedelta(days=7)
        by_family_7d: dict[str, int] = {}
        oldest_created_at: datetime | None = None
        unparsable_created_at = 0
        future_created_at = 0
        for obs in recent:
            by_family[obs.agent_family] = by_family.get(obs.agent_family, 0) + 1
            by_pc[obs.pc_id] = by_pc.get(obs.pc_id, 0) + 1
            obs_created_at = _parse_iso_utc(obs.created_at)
            if obs_created_at is None:
                # Missing / unparsable created_at: skipped individually so one
                # bad row cannot fail the whole status output.
                unparsable_created_at += 1
                continue
            if oldest_created_at is None or obs_created_at < oldest_created_at:
                oldest_created_at = obs_created_at
            if obs_created_at > now:
                # The window is [now-7d, now]; a future-dated row (clock skew /
                # bad write) must not inflate the recent-activity signal.
                future_created_at += 1
                continue
            if obs_created_at >= seven_days_ago:
                by_family_7d[obs.agent_family] = by_family_7d.get(obs.agent_family, 0) + 1
        truncated = len(recent) >= MAX_SEARCH
        # If the search hit MAX_SEARCH and even the oldest row we got back is
        # still inside the 7-day window, then rows within that window were cut
        # off: the 7d counts are lower bounds, not exact values (Issue #280
        # cross-review B1). Results come back newest-first, so "oldest returned
        # row is older than the cutoff" is what proves full coverage.
        seven_d_partial = truncated and (oldest_created_at is None or oldest_created_at >= seven_days_ago)
        last_save_at = recent[0].created_at if recent else '-'
        session_id = get_session_id()
        # Per-session save count is sourced from the store, not process-local
        # counters, so it survives MCP server restarts (#158 Codex review).
        try:
            session_obs = search_observations(session_id=session_id, limit=MAX_SEARCH)
        except Exception:  # noqa: BLE001 — diagnostics must not break get_memory_status
            session_obs = []
        this_session_saves = len(session_obs)
        last_save_dt = _parse_iso_utc(session_obs[0].created_at) if session_obs else None
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
        # Separate section: last-7-days family counts, sourced from the same
        # `recent` population as the all-time breakdown above (no extra
        # query), so it is subject to the same MAX_SEARCH truncation. When
        # that truncation actually cuts into the 7-day window the counts are
        # rendered as explicit `>=` lower bounds instead of looking exact.
        # Shown even when empty — "0 saves in the last 7 days" is itself signal.
        if seven_d_partial:
            lines.append(
                f'family (last 7d) [PARTIAL: search limit {MAX_SEARCH} reached; '
                'counts below are lower bounds, true counts may be higher]:'
            )
        else:
            lines.append('family (last 7d):')
        bound = '>=' if seven_d_partial else ''
        for family, count in sorted(by_family_7d.items()):
            lines.append(f'  family_7d {family}: {bound}{count}')
        if unparsable_created_at:
            lines.append(f'  family_7d skipped (missing/unparsable created_at): {unparsable_created_at}')
        if future_created_at:
            lines.append(f'  family_7d skipped (created_at in the future): {future_created_at}')
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
        JSON string with shape
        ``{"messages": [...], "count": N, "truncated": bool, "diagnostics": [...]}``.

        ``diagnostics`` lists everything that did not make it into ``messages``
        and why, so a withheld arrival is never reported as ``count: 0`` alone:

        * ``duplicate_retired`` — an id retired by expiry purge, arriving again.
        * ``protocol_violation`` — a retired id carrying a different message.
        * ``legacy_ack_conflict`` — the pair has a quarantined acknowledgement,
          so whether the message was already read is unknown.
        * ``ack_first_promoted`` — an acknowledgement for this pair was seen
          before the message; it counts as already read.
        * ``expired_on_arrival`` — the message was past its TTL the first time
          it was seen, so it is retired instead of delivered.
        * ``classification_failed`` — the local index could not judge the
          arrival; it is retried on the next poll.
        * ``arrival_undecodable`` / ``selector_failed`` — an arrival could not be
          parsed, or a query failed, so this listing is incomplete.

        Entries carry the withheld envelope (``null`` for the last two, which
        have no readable message), the metadata behind the decision, and the
        command or action that resolves it.
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
    classifications: dict[str, IngressResult] = {}
    # Failures that have no message behind them (an unparseable payload, a
    # selector that raised), reported alongside the per-message diagnostics.
    ingress_errors: list[dict[str, object]] = []

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
                    except Exception as e:  # noqa: BLE001
                        # An arrival that will not parse is still an arrival: it
                        # is addressed to this session and is not being
                        # delivered, so it is reported rather than dropped.
                        log.warning('check_messages: undecodable arrival at %s: %s', msg_key, e)
                        ingress_errors.append(_ingress_error_diagnostic('arrival_undecodable', msg_key, e))
                        continue
                    # Dedup by msg_id across multiple selectors before any action.
                    if msg.msg_id in seen_ids:
                        continue
                    seen_ids.add(msg.msg_id)
                    # Storage-level TTL purge (Issue #215): delete expired entries
                    # from Zenoh so they do not accumulate indefinitely.
                    # include_expired=True is read-only — skip delete so debug
                    # inspection does not destroy storage.
                    if is_expired(msg) and not include_expired:
                        try:
                            session.delete(msg_key)
                        except Exception:  # noqa: BLE001 — best-effort; non-fatal
                            pass
                    # Override scope from key context if not set on message
                    if not msg.scope:
                        msg.scope = scope
                    # Expired arrivals go through the classifier too. Skipping
                    # them here is how an id that expired while this session was
                    # offline stayed reusable: nothing registered it, so the
                    # purge below had nothing to retire and no tombstone existed
                    # to reject a second, different message on the same id.
                    try:
                        classifications[msg.msg_id] = index.register_or_classify(msg, session_id)
                    except Exception as e:  # noqa: BLE001
                        # Whatever went wrong — a locked index, a disk error —
                        # the one outcome that is not acceptable is an empty
                        # inbox with no reason. The arrival is not registered,
                        # so the next poll tries it again.
                        log.warning('check_messages: classification failed for %s: %s', msg.msg_id, e)
                        classifications[msg.msg_id] = IngressResult.classification_failed(msg.msg_id, session_id, e)
                    messages.append(msg)
            except Exception as e:  # noqa: BLE001
                # A failed selector means this listing is incomplete, which is a
                # different statement from "there is no mail".
                log.warning('check_messages: selector %s failed: %s', selector, e)
                ingress_errors.append(_ingress_error_diagnostic('selector_failed', selector, e))

    # Purge expired entries from the local SQLite index in sync with the
    # Zenoh deletes issued above.
    try:
        index.purge_expired()
    except Exception as _e:  # noqa: BLE001
        log.debug('check_messages: inline purge_expired failed: %s', _e)

    # Apply filters
    filtered: list[Message] = []
    diagnostics: list[dict[str, object]] = list(ingress_errors)
    for msg in messages:
        verdict = classifications.get(msg.msg_id)
        # ``include_expired`` is the debug view, so an arrival withheld *for*
        # being expired is shown rather than filed under diagnostics. Every
        # other withholding reason still applies.
        asked_to_see_it = include_expired and verdict is not None and verdict.code == INGRESS_EXPIRED_ON_ARRIVAL
        if verdict is not None and verdict.is_diagnostic and not asked_to_see_it:
            # Withheld from normal mail, but never without saying so: an arrival
            # on a retired or quarantined pair is the case that used to vanish.
            diagnostics.append(_message_diagnostic(msg, verdict))
            continue
        if not include_expired and is_expired(msg):
            continue
        # The classifier decided this inside the transaction that registered the
        # arrival; asking the index again here would answer from whatever state
        # exists now, which is a second, weaker judgement of the same question.
        acked = verdict.acked if verdict is not None else index.is_acked(msg.msg_id, session_id)
        if not include_acked and acked:
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
                'acked': _acked_flag(classifications.get(msg.msg_id), index, msg.msg_id, session_id),
                'delivery_adapters': msg.delivery_adapters,
            }
        )

    return json.dumps({'messages': items, 'count': len(items), 'truncated': truncated, 'diagnostics': diagnostics})


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
    if os.environ.get('KIOKU_MESH_MCP_ALLOW_TTY', '') == '1':
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

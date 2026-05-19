"""FastMCP server exposing mesh-mem tools to coding agents.

Identity fields (agent_family, client_id, pc_id, session_id) are resolved
from environment/state on the server side. They are intentionally NOT
arguments to ``save_observation`` so an LLM cannot contaminate the id
space by guessing values. Narrow-down is allowed on ``search_memory``.
"""

from contextlib import closing
import os
import socket
import sys

from fastmcp import FastMCP

from . import __version__
from .identity import get_pc_id
from .identity import get_session_id
from .models import Observation
from .models import VALID_MEMORY_TYPES
from .store import drain_pending_puts as drain_pending_puts_now
from .store import find_observation_by_id
from .store import get_index
from .store import get_transport_status
from .store import MAX_SEARCH
from .store import put_observation
from .store import put_tombstone
from .store import search_observations
from .store import start_pending_drain_background
from .store import stop_pending_drain_background

_INSTRUCTIONS = """\
mesh-mem provides a Zenoh-backed shared memory across coding agents and hosts.
Treat this as ACTIVE PROTOCOL — do not wait for the user to ask.

PROACTIVE SAVE — call ``save_observation`` IMMEDIATELY after ANY of these:
- Architecture / convention / workflow / tool-choice decision is made
- Bug fixed (include root cause; memory_type="bug")
- Non-obvious discovery, gotcha, or edge case found
- Pattern established (naming, structure, approach; memory_type="pattern")
- Config change with rationale (memory_type="config")
- Feature implemented with non-obvious approach
- User confirms a recommendation, expresses a preference, or rejects an approach
- Session concludes with a clear direction chosen (memory_type="summary")

SKIP saving when the entry would mostly duplicate another source of truth:
- PR / Issue lifecycle ticks: opened, pushed, merged, closed, "review found no blockers"
- Restatement of content already captured in a PR description, Issue body, ADR, CHANGELOG, or commit message
- Per-step implementation progress inside one conversation; use plan / todo tracking instead
- Generic status like "tests pass" or "build is green" without a non-obvious cause or decision

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

mcp = FastMCP('mesh-mem', instructions=_INSTRUCTIONS)


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
) -> str:
    """Persist a work note / decision / discovery into the shared mesh memory.

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

    Returns:
        The generated ``observation_id``.
    """
    if memory_type not in VALID_MEMORY_TYPES:
        return f'memory_type must be one of {sorted(VALID_MEMORY_TYPES)}. got: {memory_type!r}'
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
    )
    put_observation(obs)
    return f'saved: {obs.observation_id}'


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
) -> str:
    """Search the shared mesh memory, narrowing by key_expr and filtering in Python.

    ``limit`` defaults to 50 and is internally clamped to ``MAX_SEARCH``.
    Returned observation ids are full 32-char strings so ``delete_memory``
    can be called directly.
    """
    results = search_observations(
        query=query,
        agent_family=agent_family,
        client_id=client_id,
        pc_id=pc_id,
        session_id=session_id,
        project=project,
        since_iso=since_iso,
        limit=limit,
    )
    if not results:
        return 'No matching memories.'
    lines = []
    for obs in results:
        body = obs.summary if obs.summary else obs.content[:80]
        subject_part = f' {obs.subject}' if obs.subject else ''
        project_part = f' ({obs.project})' if obs.project else ''
        lines.append(
            f'[{obs.memory_type}][{obs.importance}] {obs.created_at[:19]}'
            f'{project_part}{subject_part}\n'
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
    obs = find_observation_by_id(observation_id)
    if obs is None:
        return f'observation_id {observation_id} not found.'
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
    return '\n'.join(lines)


@mcp.tool()
def delete_memory(observation_id: str, reason: str = '') -> str:
    """Soft-delete an observation by emitting a Tombstone.

    Requires the full 32-char observation_id (no short-id lookup) to avoid
    accidental deletion. Physical cleanup is deferred to a GC job.
    """
    if len(observation_id) != 32:
        return 'observation_id must be a full 32-character match.'
    obs = find_observation_by_id(observation_id)
    if obs is None:
        return f'observation_id {observation_id} not found.'
    put_tombstone(obs, reason=reason)
    return f'deleted (tombstone): {observation_id}'


@mcp.tool()
def get_memory_status() -> str:
    """Summarize the server's view of the mesh memory for troubleshooting.

    Counts are computed from up to ``MAX_SEARCH`` most-recent entries.
    Exception messages preserve the type name so connection / query /
    implementation failures are distinguishable.
    """
    try:
        recent = search_observations(limit=MAX_SEARCH)
        transport = get_transport_status()
        counts = get_index().visibility_counts()
        by_family: dict[str, int] = {}
        by_pc: dict[str, int] = {}
        for obs in recent:
            by_family[obs.agent_family] = by_family.get(obs.agent_family, 0) + 1
            by_pc[obs.pc_id] = by_pc.get(obs.pc_id, 0) + 1
        truncated = len(recent) >= MAX_SEARCH
        lines = [
            f'mesh-mem version: {__version__}',
            f'python: {sys.executable}',
            f'pc_id: {get_pc_id()}',
            f'session_id: {get_session_id()}',
            f'zenoh_session: {transport.zenoh_session}',
            f'last_put_at_iso: {transport.last_put_at_iso or "-"}',
            f'last_put_status: {transport.last_put_status}',
            f'recent_puts: {transport.recent_put_ok} ok / {transport.recent_put_error} error',
            f'pending_puts: {transport.pending_puts}',
            f'drain_in_progress: {"yes" if transport.drain_in_progress else "no"}',
            f'drain_last_run_iso: {transport.drain_last_run_iso or "-"}',
            f'drain_total_succeeded: {transport.drain_total_succeeded}',
            f'index_rows: live={counts.live} / tomb={counts.tombstoned} / shadow={counts.shadowed}',
            f'count (within limit {MAX_SEARCH}): {len(recent)}'
            + (' (limit may be reached; consider narrowing)' if truncated else ''),
        ]
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
    drained = drain_pending_puts_now(limit=limit, wait=True)
    remaining = get_transport_status().pending_puts
    return f'pending_puts drain complete: drained={drained}, remaining={remaining}'


def main() -> None:
    """Entry point for the ``mesh-mem-mcp`` console script."""
    _warn_if_zenoh_connect_unreachable()
    start_pending_drain_background()
    try:
        mcp.run()
    finally:
        stop_pending_drain_background()


if __name__ == '__main__':
    main()

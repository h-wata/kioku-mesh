"""FastMCP server exposing mesh-mem tools to coding agents.

Identity fields (agent_family, client_id, pc_id, session_id) are resolved
from environment/state on the server side. They are intentionally NOT
arguments to ``save_observation`` so an LLM cannot contaminate the id
space by guessing values. Narrow-down is allowed on ``search_memory``.
"""

import sys

from fastmcp import FastMCP

from . import __version__
from .identity import get_pc_id
from .identity import get_session_id
from .models import Observation
from .store import find_observation_by_id
from .store import MAX_SEARCH
from .store import put_observation
from .store import put_tombstone
from .store import search_observations

mcp = FastMCP('mesh-mem')


@mcp.tool()
def save_observation(
    content: str,
    project: str = '',
    tags: list[str] | None = None,
) -> str:
    """Persist a work note / decision / discovery into the shared mesh memory.

    Identity (agent_family / client_id / pc_id / session_id) is resolved from
    environment, not from tool arguments. This prevents LLMs from corrupting
    the identity namespace by passing wrong values.

    Args:
        content: free-text body of the observation.
        project: optional project tag to scope the entry.
        tags: optional list of keyword tags.

    Returns:
        The generated ``observation_id``.
    """
    obs = Observation(content=content, project=project, tags=tags or [])
    put_observation(obs)
    return f'保存完了: {obs.observation_id}'


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
        return '該当するメモリはありません。'
    lines = []
    for obs in results:
        tags_str = ', '.join(obs.tags) if obs.tags else ''
        lines.append(
            f'[{obs.agent_family}/{obs.client_id}] {obs.created_at[:19]} '
            f'({obs.project or "no-project"}) '
            f'{obs.content[:200]}'
            f'{f" #{tags_str}" if tags_str else ""} '
            f'<id={obs.observation_id}>'
        )
    return '\n---\n'.join(lines)


@mcp.tool()
def delete_memory(observation_id: str, reason: str = '') -> str:
    """Soft-delete an observation by emitting a Tombstone.

    Requires the full 32-char observation_id (no short-id lookup) to avoid
    accidental deletion. Physical cleanup is deferred to a GC job.
    """
    if len(observation_id) != 32:
        return 'observation_id は 32 文字の完全一致が必要です。'
    obs = find_observation_by_id(observation_id)
    if obs is None:
        return f'observation_id {observation_id} は見つかりませんでした。'
    put_tombstone(obs, reason=reason)
    return f'削除（tombstone）完了: {observation_id}'


@mcp.tool()
def get_memory_status() -> str:
    """Summarize the server's view of the mesh memory for troubleshooting.

    Counts are computed from up to ``MAX_SEARCH`` most-recent entries.
    Exception messages preserve the type name so connection / query /
    implementation failures are distinguishable.
    """
    try:
        recent = search_observations(limit=MAX_SEARCH)
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
            f'件数 (上限 {MAX_SEARCH} 内): {len(recent)}'
            + (' ※上限到達の可能性あり、絞り込み推奨' if truncated else ''),
        ]
        for family, count in sorted(by_family.items()):
            lines.append(f'  family {family}: {count}件')
        for pc, count in sorted(by_pc.items()):
            lines.append(f'  pc {pc[:8]}: {count}件')
        return '\n'.join(lines)
    except Exception as e:  # noqa: BLE001
        return f'共有メモリ取得失敗 [{type(e).__name__}]: {e}'


def main() -> None:
    """Entry point for the ``mesh-mem-mcp`` console script."""
    mcp.run()


if __name__ == '__main__':
    main()

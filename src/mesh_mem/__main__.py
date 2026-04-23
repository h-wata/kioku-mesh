"""Command-line interface for mesh-mem.

Thin wrapper over the same store primitives the MCP server uses.
``gc`` is provided as a scaffold only — physical deletion from RocksDB
needs per-backend work and is deferred to a later milestone.
"""

import argparse
import sys

from . import __version__
from .identity import get_pc_id
from .identity import get_session_id
from .models import Observation
from .store import find_observation_by_id
from .store import MAX_SEARCH
from .store import put_observation
from .store import put_tombstone
from .store import search_observations


def _cmd_save(args: argparse.Namespace) -> int:
    tag_list = [t.strip() for t in (args.tags or '').split(',') if t.strip()]
    obs = Observation(content=args.content, project=args.project or '', tags=tag_list)
    put_observation(obs)
    print(f'保存完了: {obs.observation_id}')
    return 0


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
        print('該当するメモリはありません。')
        return 0
    for obs in results:
        tags_str = f' #{", ".join(obs.tags)}' if obs.tags else ''
        print(
            f'[{obs.agent_family}/{obs.client_id}] {obs.created_at[:19]} '
            f'({obs.project or "-"}) <id={obs.observation_id}>'
        )
        print(f'  {obs.content[:300]}{tags_str}')
        print()
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    if len(args.observation_id) != 32:
        print('observation_id は 32 文字の完全一致が必要です。', file=sys.stderr)
        return 2
    obs = find_observation_by_id(args.observation_id)
    if obs is None:
        print(f'observation_id {args.observation_id} は見つかりませんでした。', file=sys.stderr)
        return 1
    put_tombstone(obs, reason=args.reason or '')
    print(f'削除（tombstone）完了: {args.observation_id}')
    return 0


def _cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    try:
        recent = search_observations(limit=MAX_SEARCH)
    except Exception as e:  # noqa: BLE001
        print(f'共有メモリ取得失敗 [{type(e).__name__}]: {e}', file=sys.stderr)
        return 1
    by_family: dict[str, int] = {}
    by_pc: dict[str, int] = {}
    for obs in recent:
        by_family[obs.agent_family] = by_family.get(obs.agent_family, 0) + 1
        by_pc[obs.pc_id] = by_pc.get(obs.pc_id, 0) + 1
    truncated = len(recent) >= MAX_SEARCH
    print(f'mesh-mem version: {__version__}')
    print(f'pc_id: {get_pc_id()}')
    print(f'session_id: {get_session_id()}')
    print(f'件数 (上限 {MAX_SEARCH} 内): {len(recent)}{" ※上限到達の可能性あり" if truncated else ""}')
    for family, count in sorted(by_family.items()):
        print(f'  family {family}: {count}件')
    for pc, count in sorted(by_pc.items()):
        print(f'  pc {pc[:8]}: {count}件')
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:  # noqa: ARG001
    print('gc: 物理削除 (RocksDB レベル) は未実装。次マイルストーンで対応予定。', file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='mesh-mem', description='mesh-mem CLI')
    parser.add_argument('--version', action='version', version=f'mesh-mem {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)

    p_save = sub.add_parser('save', help='Observation を保存')
    p_save.add_argument('content', help='保存する内容')
    p_save.add_argument('-p', '--project', default='')
    p_save.add_argument('-t', '--tags', default='', help='カンマ区切りのタグ')
    p_save.set_defaults(func=_cmd_save)

    p_search = sub.add_parser('search', help='メモリを検索')
    p_search.add_argument('query', nargs='?', default='', help='検索キーワード (任意)')
    p_search.add_argument('--agent-family', dest='agent_family', default='')
    p_search.add_argument('--client-id', dest='client_id', default='')
    p_search.add_argument('--pc-id', dest='pc_id', default='')
    p_search.add_argument('--session-id', dest='session_id', default='')
    p_search.add_argument('-p', '--project', default='')
    p_search.add_argument('--since', default='', help='ISO8601 時刻以降に限定')
    p_search.add_argument('-n', '--limit', type=int, default=20)
    p_search.set_defaults(func=_cmd_search)

    p_delete = sub.add_parser('delete', help='Observation を論理削除 (tombstone)')
    p_delete.add_argument('observation_id', help='32文字の完全一致 observation_id')
    p_delete.add_argument('-r', '--reason', default='')
    p_delete.set_defaults(func=_cmd_delete)

    p_status = sub.add_parser('status', help='メモリ状態を表示')
    p_status.set_defaults(func=_cmd_status)

    p_gc = sub.add_parser('gc', help='tombstone 対象の物理削除 (未実装)')
    p_gc.add_argument('--force-id', dest='force_id', default='')
    p_gc.set_defaults(func=_cmd_gc)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch to the matching subcommand handler."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

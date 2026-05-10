"""Command-line interface for mesh-mem.

Thin wrapper over the same store primitives the MCP server uses.
``gc`` performs physical delete via ``session.delete`` — the Zenoh storage
backend is expected to propagate the removal through replication so both
sides converge.
"""

import argparse
import sys

from . import __version__
from .identity import get_pc_id
from .identity import get_session_id
from .models import Observation
from .models import VALID_MEMORY_TYPES
from .store import _reset_session
from .store import find_observation_by_id
from .store import gc_expired_tombstones
from .store import MAX_SEARCH
from .store import mesh_ready_label
from .store import physical_delete_observation
from .store import put_observation
from .store import put_tombstone
from .store import search_observations
from .store import set_rebuild_on_init_default
from .store import set_rebuild_on_init_explicit


def _parse_csv(value: str) -> list[str]:
    return [s.strip() for s in value.split(',') if s.strip()]


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
    entries = []
    for obs in results:
        body = obs.summary if obs.summary else obs.content[:80]
        subject_part = f' {obs.subject}' if obs.subject else ''
        project_part = f' ({obs.project})' if obs.project else ''
        entries.append(
            f'[{obs.memory_type}][{obs.importance}] {obs.created_at[:19]}'
            f'{project_part}{subject_part}\n'
            f'{body} <id={obs.observation_id}>'
        )
    print('\n---\n'.join(entries))
    return 0


def _cmd_get_memory(args: argparse.Namespace) -> int:
    if len(args.observation_id) != 32:
        print('observation_id は 32 文字の完全一致が必要です。', file=sys.stderr)
        return 2
    obs = find_observation_by_id(args.observation_id)
    if obs is None:
        print(f'observation_id {args.observation_id} は見つかりませんでした。', file=sys.stderr)
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
    label = mesh_ready_label()
    print(f'mesh_ready: {label}')
    if label != 'yes':
        print(
            '警告: ピアアライメントが未完了です。再起動直後は検索件数が少なく見えることがあります。', file=sys.stderr
        )
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    if args.force_id:
        if len(args.force_id) != 32:
            print('--force-id は 32 文字の完全一致 observation_id が必要です。', file=sys.stderr)
            return 2
        obs_removed, tomb_removed = physical_delete_observation(args.force_id)
        parts = []
        if obs_removed:
            parts.append('obs')
        if tomb_removed:
            parts.append('tomb')
        if parts:
            print(f'物理削除完了 ({", ".join(parts)}) + broadcast purge: {args.force_id}')
        else:
            # No local match, but the broadcast wildcard delete may still have
            # purged a reachable peer's copy — treat as success so scripts do
            # not retry or misinterpret a completed emergency purge as failure.
            print(
                f'observation_id {args.force_id} はこの replica では未所持でした。'
                'broadcast purge は送信済み (ベストエフォート)。'
                '完全を期すなら他 PC でも同コマンドを実行してください。',
            )
        return 0
    purged = gc_expired_tombstones(retention_days=args.retention_days, project=args.project or '')
    project_note = f' (project={args.project})' if args.project else ''
    print(f'retention {args.retention_days} 日超の tombstone{project_note}: {purged} 件を物理削除しました')
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='mesh-mem', description='mesh-mem CLI')
    parser.add_argument('--version', action='version', version=f'mesh-mem {__version__}')
    parser.add_argument(
        '--rebuild',
        action='store_true',
        help=(
            '初回起動時に zenoh から SQLite index を再構築する。'
            'CLI は one-shot 想定でデフォルト skip (#38)。'
            'index が空の状態で search したい場合や CI 検証時に明示指定する。'
            '環境変数 MESH_MEM_FORCE_REBUILD=1 でも同等。'
        ),
    )
    sub = parser.add_subparsers(dest='command', required=True)

    _MEMORY_TYPES = sorted(VALID_MEMORY_TYPES)  # noqa: N806

    p_save = sub.add_parser('save', help='Observation を保存')
    p_save.add_argument('content', help='保存する内容')
    p_save.add_argument('-p', '--project', default='')
    p_save.add_argument('-t', '--tags', default='', help='カンマ区切りのタグ')
    p_save.add_argument(
        '--memory-type',
        dest='memory_type',
        default='note',
        choices=_MEMORY_TYPES,
        help='メモリ種別 (default: note)',
    )
    p_save.add_argument(
        '--importance',
        type=int,
        default=2,
        choices=range(1, 6),
        metavar='1-5',
        help='重要度 1-5 (default: 2)',
    )
    p_save.add_argument('--subject', default='', help='短いトピック名')
    p_save.add_argument('--summary', default='', help='1行サマリー (検索結果に表示)')
    p_save.add_argument(
        '--source-files',
        dest='source_files',
        default='',
        help='関連ファイルパス (カンマ区切り)',
    )
    p_save.add_argument(
        '--supersedes',
        default='',
        help='置き換える observation_id (カンマ区切り、32文字hex)',
    )
    p_save.set_defaults(func=_cmd_save)

    p_search = sub.add_parser('search', help='メモリを検索')
    p_search.add_argument('query', nargs='?', default='', help='検索キーワード (任意)')
    p_search.add_argument('--agent-family', dest='agent_family', default='')
    p_search.add_argument('--client-id', dest='client_id', default='')
    p_search.add_argument('--pc-id', dest='pc_id', default='')
    p_search.add_argument('--session-id', dest='session_id', default='')
    p_search.add_argument('-p', '--project', default='')
    p_search.add_argument('--since', default='', help='ISO8601 時刻以降に限定')
    p_search.add_argument('-n', '--limit', type=int, default=50, help='最大件数 (default: 50)')
    p_search.set_defaults(func=_cmd_search)

    p_delete = sub.add_parser('delete', help='Observation を論理削除 (tombstone)')
    p_delete.add_argument('observation_id', help='32文字の完全一致 observation_id')
    p_delete.add_argument('-r', '--reason', default='')
    p_delete.set_defaults(func=_cmd_delete)

    p_status = sub.add_parser('status', help='メモリ状態を表示')
    p_status.set_defaults(func=_cmd_status)

    p_gc = sub.add_parser('gc', help='tombstone 対象の物理削除 (retention or --force-id)')
    p_gc.add_argument(
        '--force-id',
        dest='force_id',
        default='',
        help='32文字の observation_id を完全一致で物理削除 (機微情報緊急手順用; --project は無視される)',
    )
    p_gc.add_argument(
        '--retention-days',
        dest='retention_days',
        type=int,
        default=30,
        help='retention 日数 (default 30)。この日数より古い tombstone と対応 obs を物理削除',
    )
    p_gc.add_argument(
        '-p',
        '--project',
        default='',
        help='指定したプロジェクトの tombstone のみを削除対象にする (未指定時は全プロジェクト対象)',
    )
    p_gc.set_defaults(func=_cmd_gc)

    p_get = sub.add_parser('get-memory', help='observation_id で単一レコードを取得')
    p_get.add_argument('observation_id', help='32文字の完全一致 observation_id')
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
        # Explicitly close the cached Zenoh session before sys.exit. Without
        # this, the CLI hangs after printing output because the session's
        # replication subscriber thread keeps the interpreter alive past
        # the command's return; users had to ctrl-c to escape.
        _reset_session()


if __name__ == '__main__':
    sys.exit(main())

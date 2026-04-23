# mesh-mem: マルチエージェント分散メモリ PoC設計書

## 概要

複数のAIコーディングエージェント（Claude Code, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop）が共有するクロスエージェントメモリシステム。
mesh 状に繋いだ複数ノードで observation を eventual consistency に同期する汎用設計で、**PoC の transport 実装として Zenoh + RocksDB を採用**する（将来 NATS JetStream 等への載せ替えを妨げない構造にする）。

## PoC 制約 / 前提

- **認証・暗号化なし**。LAN 内に閉じた検証ネットワーク前提。
- **機微情報**（顧客データ、NDA案件内容、資格情報、秘密鍵）は保存しない。
- `mem/**` 空間は同一LAN上のすべての端末から読み書き可能な状態。
- 本番移行時は TLS + `usrpwd` または TLS 認証前提の再設計が必須（将来の拡張参照）。
- NTP/chrony による時刻同期必須（storage replication と衝突解決の正しさが timestamp に依存）。

## ゴール（PoC）

1. 2台のPC（`home` / `office` の2ロール）で zenohd + RocksDB を起動し、router間自動同期を確認
2. Python製 MCP サーバーで `save_observation` / `search_memory` / `delete_memory` / `get_memory_status` を公開
3. Claude Code からMCP経由でobservationをsave → もう1台から search で見えることを確認
4. **オフライン中の差分同期と split-brain 競合挙動を自動/半自動テストで検証**
5. **2-router 環境を自動起動する E2E テストを整備し、設定変更で壊れないことを継続確認**

---

## ネットワーク構成

```
home (192.168.3.x)                     office (192.168.3.y)
┌────────────────────────┐            ┌────────────────────────┐
│ Claude Code / Desktop  │            │ Gemini CLI / Codex CLI │
│   ↓ MCP (stdio)        │            │   ↓ MCP (stdio)        │
│ mesh-mem MCP server   │            │ mesh-mem MCP server   │
│   ↓ zenoh client       │            │   ↓ zenoh client       │
│ zenohd (router)        │◄── link ──►│ zenohd (router)        │
│ + RocksDB backend      │   state    │ + RocksDB backend      │
│   mem/** 永続化        │            │   mem/** 永続化        │
└────────────────────────┘            └────────────────────────┘
```

- MCPサーバーは `tcp/localhost:7447` に接続（loopback endpoint）
- zenohd 同士は LAN IP のみで接続（inter-router endpoint）
- 片方が落ちてもローカル完結で動作継続
- ネットワーク復帰時に storage replication で差分自動同期

### セキュリティ上の注意
- `listen.endpoints` は **0.0.0.0 を避け**、loopback と対向PC向けIFを列挙する。
- ホスト firewall（ufw / iptables）で 7447 を対向PCのIPのみ許可。
- 将来 TLS 化する時に備え、endpoint を `tcp/` と `tls/` で分けられる構造にしておく。

---

## ディレクトリ構成

```
mesh-mem/
├── README.md
├── pyproject.toml
├── config/
│   ├── zenohd_home.json5          # home ロール用 zenohd 設定
│   └── zenohd_office.json5        # office ロール用 zenohd 設定
├── src/
│   └── mesh_mem/
│       ├── __init__.py
│       ├── __main__.py            # CLI エントリポイント
│       ├── mcp_server.py          # FastMCP サーバー本体
│       ├── identity.py            # 識別子解決（env / 永続化ファイル）
│       ├── store.py               # zenoh session の put/get/delete ラッパー
│       └── models.py              # Observation / Tombstone
└── tests/
    ├── conftest.py                # 2-router fixture（subprocess起動）
    ├── test_models.py             # key生成、JSON round-trip
    ├── test_store_single.py       # 単一zenohdでの save/search/delete
    └── test_e2e_sync.py           # 2-router E2E（オフライン同期・split-brain）
```

---

## zenohd設定

### home 用: `config/zenohd_home.json5`

> ロール名 `home` / `office` は plan.md 上の抽象ラベル。実機ホスト名との対応は各自の環境に合わせて README で記載する（例: `home` = 自宅デスクトップ、`office` = 職場ノート）。

```json5
{
  mode: "router",
  // loopback は client 用、LAN IP は対向router用。0.0.0.0 は使わない
  listen: {
    endpoints: [
      "tcp/127.0.0.1:7447",
      "tcp/192.168.3.x:7447",   // 自PCのLAN側IPに置換
    ],
  },
  connect: { endpoints: ["tcp/192.168.3.y:7447"] }, // office 側のIPに置換
  timestamping: {
    enabled: { router: true, peer: true, client: true },
  },
  plugins: {
    storage_manager: {
      volumes: {
        rocksdb: {},
      },
      storages: {
        agent_mem: {
          key_expr: "mem/**",
          strip_prefix: "mem",
          replication: {
            interval: 10.0,           // 秒（float 可）
            sub_intervals: 5,
            hot: 6,
            warm: 30,
            propagation_delay: 250,   // ミリ秒
          },
          volume: {
            id: "rocksdb",
            dir: "agent_mem",
            create_db: true,
          },
        },
      },
    },
  },
}
```

### office 用: `config/zenohd_office.json5`

`home` とIP入れ替えの対称形。`listen` は `tcp/127.0.0.1:7447` と `tcp/192.168.3.y:7447`、`connect` は `tcp/192.168.3.x:7447`。**storage と replication のブロックは完全同一**。

**注意**:
- `timestamping.enabled` を有効にしないと storage_manager / replication が動作しない。
- 両PCの storage 設定（`key_expr`, `strip_prefix`, `replication` ブロック全体）を**完全一致**させないと digest 比較がズレて同期が走らない。
- `replication.interval` は秒、`propagation_delay` はミリ秒の**数値**。文字列（`"10s"` など）を書くとパースエラー。
- NTP / chrony 同期は必須。timestamp ベースで衝突解決するため、clock skew が大きいと新旧判定がおかしくなる。
- 初回起動時は片側だけ先に立ち上げ、RocksDB 初期化完了後に対向側を起動する方が安全。

---

## 識別子設計

複数エージェント・複数PC・複数セッションが同じ空間に書き込むため、Observation / Tombstone の key に入れる識別子を階層で持つ。

| 識別子 | 責務 | 値 | 解決方法 |
|---|---|---|---|
| `agent_family` | エージェント系統 | `claude` / `gemini` / `codex` / `chatgpt` | env `MESH_MEM_AGENT_FAMILY` |
| `client_id` | 実クライアント種別 | `claude-code` / `claude-desktop` / `gemini-cli` / `codex-cli` / `chatgpt-desktop` | env `MESH_MEM_CLIENT_ID` |
| `pc_id` | PC固有の安定ID（UUID） | 初回生成して永続化 | `$MESH_MEM_STATE_DIR/pc_id` 読み書き |
| `session_id` | エージェント起動単位 | `{yyyymmddTHHMMSS}-{short}` | env `MESH_MEM_SESSION_ID` 優先、無ければ自動生成 |
| `observation_id` | 単一メモリ | `uuid4().hex`（32桁） | 毎保存時に生成 |

- `HOSTNAME` は rename / clone / コンテナで安定しないので **pc_id には使わない**（表示用メタデータにのみ使う）。
- `session_id` は未設定時 `default` フォールバック禁止。起動時生成を必須にする。
- `observation_id` は短縮せず full 32 文字で持つ（サイレント上書き防止）。

### `src/mesh_mem/identity.py`

`pc_id` / `session_id` は **プロセス起動時に 1 回だけ確定してキャッシュ**する。`Observation` 生成のたびに別 session_id になるとセッション分裂を引き起こすため。

```python
"""識別子の解決: env 優先、無ければ永続化ファイル or 自動生成。
    pc_id / session_id はプロセス内でキャッシュして不変にする。
"""

import os
import pathlib
import uuid
from datetime import datetime, timezone

_pc_id_cache: str | None = None
_session_id_cache: str | None = None


def state_dir() -> pathlib.Path:
    d = pathlib.Path(
        os.environ.get(
            "MESH_MEM_STATE_DIR",
            pathlib.Path.home() / ".local/share/mesh-mem",
        )
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_pc_id() -> str:
    global _pc_id_cache
    if _pc_id_cache is not None:
        return _pc_id_cache
    p = state_dir() / "pc_id"
    if p.exists():
        _pc_id_cache = p.read_text().strip()
        return _pc_id_cache
    pid = uuid.uuid4().hex
    p.write_text(pid + "\n")
    _pc_id_cache = pid
    return pid


def get_agent_family() -> str:
    return os.environ.get("MESH_MEM_AGENT_FAMILY", "unknown")


def get_client_id() -> str:
    return os.environ.get("MESH_MEM_CLIENT_ID", "unknown")


def get_session_id() -> str:
    """プロセス起動時に 1 回だけ確定してキャッシュ。
    - env MESH_MEM_SESSION_ID があればそれ
    - 無ければ `{yyyymmddTHHMMSSZ}-{short}` を自動生成
    """
    global _session_id_cache
    if _session_id_cache is not None:
        return _session_id_cache
    sid = os.environ.get("MESH_MEM_SESSION_ID")
    if not sid:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sid = f"{ts}-{uuid.uuid4().hex[:8]}"
    _session_id_cache = sid
    return sid
```

---

## Zenoh Key Expression 設計

```
mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
```

- Observation は **immutable**。更新は新 `observation_id` で新規保存。同 key への上書きは禁止。
- 削除は `mem/tomb/...` に Tombstone を put（物理削除は retention job で別途）。
- `mem/heartbeat/**` と `mem/ctx/**` は **将来の拡張**（送信主体・周期・用途を含めて別フェーズで設計）。

### 検索のためのワイルドカード

| クエリ | 意味 |
|---|---|
| `mem/obs/**` | 全エージェント・全PC・全セッション |
| `mem/obs/claude/**` | claude 系全部 |
| `mem/obs/claude/claude-code/**` | Claude Code のみ |
| `mem/obs/*/*/{pc_id}/**` | 特定 PC |
| `mem/obs/*/*/*/{session_id}/**` | 特定セッション |

---

## データモデル

### `src/mesh_mem/models.py`

`from_json` は **未知フィールドを無視** する実装にする。分散保存では「古いデータを新しいコードで読む / 新しいデータを少し古いコードで読む」が日常的に発生するため、スキーマ進化に耐性を持たせる。

```python
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
import json
import uuid

from .identity import (
    get_agent_family,
    get_client_id,
    get_pc_id,
    get_session_id,
)


def _utc_now_iso() -> str:
    """UTC固定で 'Z' サフィックスに寄せる。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _from_dict_compat(cls, data: dict):
    """dataclass に既知フィールドだけ渡す（unknown field は捨てる）。"""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Observation:
    content: str
    agent_family: str = field(default_factory=get_agent_family)
    client_id: str = field(default_factory=get_client_id)
    pc_id: str = field(default_factory=get_pc_id)
    session_id: str = field(default_factory=get_session_id)
    project: str = ""
    tags: list[str] = field(default_factory=list)
    observation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now_iso)

    @property
    def key_expr(self) -> str:
        return (
            f"mem/obs/{self.agent_family}/{self.client_id}/"
            f"{self.pc_id}/{self.session_id}/{self.observation_id}"
        )

    def tombstone_key_expr(self) -> str:
        return self.key_expr.replace("mem/obs/", "mem/tomb/", 1)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "Observation":
        return _from_dict_compat(cls, json.loads(data))


@dataclass
class Tombstone:
    """論理削除マーカー。元 observation の key 階層と対称な mem/tomb/ 以下に put する。"""
    observation_id: str
    reason: str = ""
    deleted_at: str = field(default_factory=_utc_now_iso)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "Tombstone":
        return _from_dict_compat(cls, json.loads(data))
```

> `Heartbeat` は **PoC スコープ外**（将来の拡張参照）。送信主体・周期・停止判定を定義しないまま model だけ置くと中途半端な実装を誘発するため。

---

## Zenoh Store ラッパー

### `src/mesh_mem/store.py`（要点。完全版は実装時に詰める）

設計上の要点:

- **再接続は通信関数内で捕捉**して `_reset_session()` → 再 open → 1 回リトライ。`get_session()` が初回 open しか効かない実装は不可。
- **limit は共通で `MAX_SEARCH = 10000` に clamp**。過大値が指定されても拒否せず上限で止める。
- **since_iso は datetime に parse**して UTC 正規化比較。ISO 文字列の辞書順比較は禁止（`Z` / `+00:00` 混在で壊れる）。
- **`find_observation_by_id` は search を経由しない直接走査**。delete 経路が件数依存で失敗しないようにする。

```python
"""zenoh session の put/get/delete ラッパー。再接続リトライ + 検索 clamp 付き。"""

import functools
import logging
import os
import time
from datetime import datetime, timezone

import zenoh

from .models import Observation, Tombstone

log = logging.getLogger(__name__)

MAX_SEARCH = 10_000   # 返却件数の上限（clamp）。走査件数の上限ではない点に注意。
GET_TIMEOUT = 5.0

_session: zenoh.Session | None = None


def _open_session() -> zenoh.Session:
    endpoint = os.environ.get("ZENOH_CONNECT", "tcp/localhost:7447")
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    config.insert_json5("connect/endpoints", f'["{endpoint}"]')
    return zenoh.open(config)


def _reset_session() -> None:
    """死んでいる可能性のある session を捨てる。"""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
    _session = None


def get_session() -> zenoh.Session:
    """初回 or reset 後に open する。通信失敗時のリトライは with_retry が担当。"""
    global _session
    if _session is None:
        _session = _open_session()
    return _session


class QueryErrorReply(Exception):
    """`session.get()` の reply に `err` が含まれていた場合に raise。
    retryable 扱いにして with_retry で 1 回だけ再試行する。"""


# zenoh の接続/タイムアウト系例外のみリトライ対象。
# zenoh-python 1.9.0 では接続断・open/get/put 失敗を含む Zenoh 関連エラーは `zenoh.ZError`
# を基底に飛ぶので、これを必ず含める（参照: https://zenoh-python.readthedocs.io/en/1.9.0/api_reference.html）。
# 実装不備・データ不整合・API 変更による例外を再接続で隠さないのが目的。
_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    zenoh.ZError,
    ConnectionError,
    TimeoutError,
    QueryErrorReply,
)


def with_retry(func):
    """zenoh 接続系の失敗のみ session を reset して 1 回だけ再試行。
    最終失敗時は原因例外を `raise ... from e` で chain して残す。
    対象外の例外（実装バグ、データ不整合等）はそのまま呼び出し元へ伝播させる。
    """
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        last_exc: BaseException | None = None
        for attempt in range(2):
            try:
                return func(*args, **kwargs)
            except _RETRYABLE_EXC as e:
                last_exc = e
                log.warning("%s retryable failure (attempt %d): %s",
                            func.__name__, attempt + 1, e)
                _reset_session()
                time.sleep(0.2 * (attempt + 1))
            # 対象外の例外は捕まえない（そのまま伝播）
        raise RuntimeError(
            f"{func.__name__} failed after retry"
        ) from last_exc
    return wrapped


@with_retry
def put_observation(obs: Observation) -> None:
    get_session().put(obs.key_expr, obs.to_json())


@with_retry
def put_tombstone(obs: Observation, reason: str = "") -> None:
    tomb = Tombstone(observation_id=obs.observation_id, reason=reason)
    get_session().put(obs.tombstone_key_expr(), tomb.to_json())


def _iter_ok_replies(session: zenoh.Session, key_expr: str, timeout: float = GET_TIMEOUT):
    """session.get() の reply のうち `ok` のみ yield。`err` が 1 件でも来たら
    QueryErrorReply を raise して偽陰性（同期失敗と件数ゼロの混同）を防ぐ。

    利用契約:
        - **ローカル蓄積専用**。呼び出し側は yield された値を list や set に溜めてから
          for ループ終了後に一括処理すること。
        - ループ内で副作用（put / delete / 外部API 呼び出し等）を起こしてはならない。
          途中で `raise` した場合、retry で再実行されて「部分適用」状態の副作用が二重発生
          する危険がある。
        - 副作用付きループが必要な場合は、まず list に collect してから別ループで実行するか、
          idempotent な副作用に限定すること。
    """
    for reply in session.get(key_expr, timeout=timeout):
        if reply.ok:
            yield reply.ok
            continue
        payload = ""
        try:
            if reply.err is not None:
                payload = reply.err.payload.to_string()
        except Exception:
            pass
        raise QueryErrorReply(f"query error for {key_expr}: {payload or 'unknown'}")


def _parse_iso(s: str) -> datetime | None:
    """'Z' サフィックスも fromisoformat で parse できる形に寄せる。"""
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@with_retry
def search_observations(
    query: str = "",
    agent_family: str = "",
    client_id: str = "",
    pc_id: str = "",
    session_id: str = "",
    project: str = "",
    since_iso: str = "",
    limit: int = 50,
) -> list[Observation]:
    """key-expr で絞り込んでから get、Python 側で content/project/tag/since をフィルタ。"""
    limit = max(1, min(limit, MAX_SEARCH))
    since_dt = _parse_iso(since_iso)

    parts = ["mem/obs", agent_family or "*", client_id or "*",
             pc_id or "*", session_id or "*", "**"]
    key_expr = "/".join(parts)
    tomb_expr = key_expr.replace("mem/obs/", "mem/tomb/", 1)

    session = get_session()

    tombs: set[str] = set()
    for ok in _iter_ok_replies(session, tomb_expr):
        tombs.add(ok.key_expr.as_str().rsplit("/", 1)[-1])

    q = query.lower()
    results: list[Observation] = []
    for ok in _iter_ok_replies(session, key_expr):
        try:
            obs = Observation.from_json(ok.payload.to_string())
        except Exception as e:
            log.warning("skip malformed payload at %s: %s",
                        ok.key_expr.as_str(), e)
            continue
        if obs.observation_id in tombs:
            continue
        if project and obs.project != project:
            continue
        if since_dt:
            obs_dt = _parse_iso(obs.created_at)
            if obs_dt is None or obs_dt < since_dt:
                continue
        if q and q not in obs.content.lower() \
                and q not in obs.project.lower() \
                and not any(q in t.lower() for t in obs.tags):
            continue
        results.append(obs)

    # sort も文字列比較ではなく datetime に寄せる。parse 失敗は最古扱い（epoch 0 UTC）。
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    results.sort(
        key=lambda o: _parse_iso(o.created_at) or _epoch,
        reverse=True,
    )
    return results[:limit]


@with_retry
def find_observation_by_id(observation_id: str) -> Observation | None:
    """`mem/obs/**` を **limit なしで** 直接走査して observation_id 完全一致を探す。
    delete 経路が件数依存で失敗しないようにするため、search_observations を経由しない。
    """
    session = get_session()
    for ok in _iter_ok_replies(session, "mem/obs/**"):
        try:
            obs = Observation.from_json(ok.payload.to_string())
        except Exception:
            continue
        if obs.observation_id == observation_id:
            return obs
    return None
```

### スケーラビリティ制約（PoC）

- `MAX_SEARCH = 10000` は **返却件数の上限**（最終的に呼び出し側に返す件数）。
- **走査件数の上限ではない**。`session.get(key_expr)` は該当キーを全部引いてから Python 側でフィルタ・sort・slice する構造なので、key_expr 配下にヒットする件数がそのまま転送量・メモリ・CPUコストになる。`MAX_SEARCH` で性能保護はできないことに注意。
- 走査負荷を抑えたい場合は **呼び出し側が key_expr を絞る**（`agent_family` / `client_id` / `pc_id` / `session_id` 引数で前段絞り込み）ことを前提にする。
- さらに先は FTS インデックス（whoosh/sqlite FTS5）を zenoh subscriber で自動更新する方式に移行。

---

## MCP サーバー

### `src/mesh_mem/mcp_server.py`

```python
"""mesh-mem MCP Server (FastMCP)"""

import importlib.metadata
import sys

from fastmcp import FastMCP

from .identity import get_pc_id, get_session_id
from .models import Observation
from .store import (
    MAX_SEARCH,
    find_observation_by_id,
    put_observation,
    put_tombstone,
    search_observations,
)

mcp = FastMCP("mesh-mem")


@mcp.tool()
def save_observation(
    content: str,
    project: str = "",
    tags: list[str] | None = None,
) -> str:
    """作業結果、決定事項、学びを共有メモリに保存する。
    agent_family / client_id / pc_id / session_id は環境変数から自動解決するため指定不要。
    LLM が誤った値を渡して識別空間が汚れないよう、これらはツール引数から意図的に除外している。
    """
    obs = Observation(content=content, project=project, tags=tags or [])
    put_observation(obs)
    return f"保存完了: {obs.observation_id}"


@mcp.tool()
def search_memory(
    query: str = "",
    agent_family: str = "",
    client_id: str = "",
    pc_id: str = "",
    session_id: str = "",
    project: str = "",
    since_iso: str = "",
    limit: int = 20,
) -> str:
    """共有メモリを検索する。key-expr 絞り込み後に content/project/tag/since でフィルタ。
    limit は内部で MAX_SEARCH(=10000) に clamp される。
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
        return "該当するメモリはありません。"
    lines = []
    for obs in results:
        tags_str = ", ".join(obs.tags) if obs.tags else ""
        # observation_id は **完全32文字** を返す。delete_memory が完全一致を要求するため、
        # LLM が検索結果から直接 delete を呼べるようにする。
        lines.append(
            f"[{obs.agent_family}/{obs.client_id}] {obs.created_at[:19]} "
            f"({obs.project or 'no-project'}) "
            f"{obs.content[:200]}"
            f"{f' #{tags_str}' if tags_str else ''} "
            f"<id={obs.observation_id}>"
        )
    return "\n---\n".join(lines)


@mcp.tool()
def delete_memory(observation_id: str, reason: str = "") -> str:
    """observation を論理削除する（tombstone 方式）。
    observation_id の完全一致（32文字）が必要。物理削除は `mesh-mem gc` が担当。
    内部は search_memory を経由せず mem/obs/** を直接走査するので件数上限の影響を受けない。
    """
    if len(observation_id) != 32:
        return "observation_id は 32 文字の完全一致が必要です。"
    obs = find_observation_by_id(observation_id)
    if obs is None:
        return f"observation_id {observation_id} は見つかりませんでした。"
    put_tombstone(obs, reason=reason)
    return f"削除（tombstone）完了: {observation_id}"


@mcp.tool()
def get_memory_status() -> str:
    """共有メモリと MCP サーバー自身の状態を返す。トラブルシュート用。
    件数は MAX_SEARCH(=10000) 件以内の範囲で集計する（PoC 上限の前提）。
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
            f"mesh-mem version: {_version()}",
            f"python: {sys.executable}",
            f"pc_id: {get_pc_id()}",
            f"session_id: {get_session_id()}",
            f"件数 (上限 {MAX_SEARCH} 内): {len(recent)}"
            + (" ※上限到達、絞り込み必要" if truncated else ""),
        ]
        for family, count in sorted(by_family.items()):
            lines.append(f"  family {family}: {count}件")
        for pc, count in sorted(by_pc.items()):
            lines.append(f"  pc {pc[:8]}: {count}件")
        return "\n".join(lines)
    except Exception as e:
        # 接続断 / QueryErrorReply / 実装バグを切り分けられるよう例外型名を残す
        return f"共有メモリ取得失敗 [{type(e).__name__}]: {e}"


def _version() -> str:
    try:
        return importlib.metadata.version("mesh-mem")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    mcp.run()
```

`agent_family` / `client_id` / `pc_id` / `session_id` は **MCP の `save_observation` 引数からは意図的に除外**し、env と永続化ファイルでのみ解決する。LLM が誤った値を渡して識別空間が壊れることを防ぐため。検索側では絞り込みに使えるよう引数として公開する。

---

## CLI

`__main__.py` は MCP と同じ内部 API を使う薄いラッパー。`save` / `search` / `delete` / `status` / `gc` を提供。

- `mesh-mem save "..."` → env から identity を解決して保存（`--project`, `--tags`）
- `mesh-mem search "..."` → `--agent-family` / `--client-id` / `--pc-id` / `--session-id` / `--project` / `--since` / `--limit`（limit は MAX_SEARCH=10000 に clamp）
- `mesh-mem delete <observation_id>` → tombstone（32文字完全一致）
- `mesh-mem gc [--force-id <id>]` → tombstone 対象の物理削除（retention policy に基づく）
- `mesh-mem status` → 状態表示（MAX_SEARCH 件以内の集計）

（詳細コードは実装時に詰める）

---

## pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mesh-mem"
version = "0.1.0"
description = "Cross-agent distributed memory over Zenoh"
requires-python = ">=3.10"
dependencies = [
    "eclipse-zenoh>=1.9.0",
    "fastmcp>=0.1.0",
]

[project.scripts]
mesh-mem = "mesh_mem.__main__:main"
mesh-mem-mcp = "mesh_mem.mcp_server:mcp.run"
```

---

## MCP登録設定

- `command` は `python` 直叩きではなく、インストール済みの `mesh-mem-mcp` 実行ファイルの**絶対パス**を使う（PATH 依存・venv 事故を回避）。
- `env` で identity を必ず注入する。特に `MESH_MEM_AGENT_FAMILY` と `MESH_MEM_CLIENT_ID` の取り違えが空間汚染に直結する。

### Claude Code: `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "mesh-mem": {
      "command": "/home/USER/.venv/mesh-mem/bin/mesh-mem-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/localhost:7447",
        "MESH_MEM_AGENT_FAMILY": "claude",
        "MESH_MEM_CLIENT_ID": "claude-code"
      }
    }
  }
}
```

### Claude Desktop: `~/.config/Claude/claude_desktop_config.json` (Linux) / `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

`MESH_MEM_CLIENT_ID` を `claude-desktop` に変更して同様に登録。

### Gemini CLI: `~/.gemini/settings.json`

`MESH_MEM_AGENT_FAMILY=gemini`, `MESH_MEM_CLIENT_ID=gemini-cli`。

### Codex CLI / ChatGPT Desktop も同パターン。

### session_id の注入（任意）

エージェント起動フックから `MESH_MEM_SESSION_ID` を渡せばセッション識別を制御できる。未指定なら起動時自動生成。

---

## PoC手順

### Step 1: zenohd + RocksDB のセットアップ（両PC）

前提: Zenoh **1.9.0 "Longwang" 以降**、chrony / systemd-timesyncd 同期済み（`chronyc tracking` で offset 100ms 以内）。

```bash
# zenohd と RocksDB backend (zenoh-backend-rocksdb) のインストール

# RocksDB 格納先を環境変数で明示（未設定時の既定は環境依存でハマりやすい）
# storage の dir: "agent_mem" は ${ZENOH_BACKEND_ROCKSDB_ROOT}/agent_mem に解決される
export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"
mkdir -p "$ZENOH_BACKEND_ROCKSDB_ROOT"

# 片側を先に起動
zenohd -c config/zenohd_home.json5

# 数秒後に対向側を起動
zenohd -c config/zenohd_office.json5
```

> systemd で常駐させる場合は unit の `Environment=ZENOH_BACKEND_ROCKSDB_ROOT=...` で同等の設定を行う。
> ホスト firewall で 7447 を対向PCのIPに限定する設定も同時に入れる。

### Step 2: mesh-mem のインストール

```bash
# 各PCで venv を作ってインストール（PATH 事故回避のため）
python3 -m venv ~/.venv/mesh-mem
~/.venv/mesh-mem/bin/pip install -e .
```

### Step 3: CLI で基本動作確認

```bash
export MESH_MEM_AGENT_FAMILY=claude
export MESH_MEM_CLIENT_ID=claude-code

# home PC で保存
mesh-mem save "Fleet Adapter のKachaka統合でget_positionのレスポンス遅延を発見" \
  --project robo-hi --tags fleet-adapter,kachaka

# office PC で検索（同期確認）
mesh-mem search "kachaka"

mesh-mem status
```

### Step 4: オフライン差分同期の検証（replication 動作確認）

router 間 link が一時的に切れた状態で put → 復旧後に対向側で見えるかを確認する。
**これが通らなければ `replication` 設定が効いていないので PoC 不合格**。

```bash
# 1. PC-B の zenohd を停止
sudo systemctl stop zenohd

# 2. PC-A で数件 put
mesh-mem save "while B is down 1" --project poc --tags offline-test
mesh-mem save "while B is down 2" --project poc --tags offline-test

# 3. PC-B の zenohd を再起動
zenohd -c config/zenohd_office.json5

# 4. interval (10s) + propagation_delay (250ms) + 余裕 = 15〜30 秒待つ

# 5. PC-B で search → 停止中 put 分が見える
mesh-mem search "B is down"
```

合格基準: 復旧から 30 秒以内に両側で内容一致、A/B 役割を入れ替えても対称に成立。

### Step 5: split-brain 復旧後の tombstone 伝播確認

Observation は immutable、削除は `mem/tomb/{...}/{observation_id}` に Tombstone を put する方式。
検索時は「同一 `observation_id` の tombstone key が 1 件でも観測されれば非表示」とするロジックなので、**obs と tombstone の timestamp の勝敗ではなく tombstone key の存在ベース**で隠れる。分断・復旧をまたいでこの不可視化が成立するかを確認する。

```bash
# 1. 両PC間のTCP 7447を iptables で片側遮断
sudo iptables -I INPUT -p tcp --dport 7447 -s 192.168.3.x -j DROP   # on PC-B

# 2. 分断中の操作:
# - PC-A で obs_X を save → 分断中のまま PC-A で delete_memory obs_X (mem/tomb/... に put)
# - PC-B でも別の obs_Y を save
# - どちらも分断中は対向側に届かない

# 3. iptables を flush してリンク復旧
sudo iptables -F

# 4. replication.interval * 2 程度（~20s）+ propagation_delay 余裕 を待つ

# 5. 両側で search して結果を比較
mesh-mem search ""
```

期待仕様:
- `obs_X` は **両側で非表示**（tombstone key が replication で伝播し、search 側で隠される）
- `obs_Y` は **両側に存在**する
- 双方向対称（A で delete して B で非表示、B で delete して A で非表示）に成立

> 注: 本 PoC の削除モデルは「tombstone key の存在ベース」なので、`obs` と `tombstone` の timestamp 勝敗による LWW 挙動は検証対象に含めない。Zenoh replication の key 単位 LWW 挙動そのもの（同一 key の split-brain 書き込み）を検証したい場合は、PoC 後に別ケースとして切り出す。

### Step 6: MCP サーバーの登録と動作確認

各エージェントの settings に env 付きで登録し、
- 「共有メモリに保存して」 → `save_observation` 呼び出し
- 「過去のメモリを検索して」 → `search_memory`
- 「この observation を消して」 → `delete_memory`
- 「メモリ状態を見せて」 → `get_memory_status`（version と実行パスが出ること）

が動くことを確認。

---

## テスト戦略

### 単体テスト: `tests/test_models.py`, `tests/test_store_single.py`
- key 生成（identity の組合せごとに正しい key_expr が出るか）
- JSON round-trip
- `observation_id` 一意性（ランダム10000件で衝突しないこと）
- search の project/session_id/since フィルタ

### 結合テスト: 単一 zenohd 起動
`conftest.py` でテスト用 zenohd を subprocess 起動する fixture を作る。
- save / search / delete のハッピーパス
- tombstone が search から除外されること
- 接続切断時の再接続リトライ

### 異常系テスト: モック session.get() で Reply.err 経路を検証
`session.get()` をフェイクに差し替えた単体テストで、**今回導入した `QueryErrorReply` / `_iter_ok_replies()` / `with_retry` の経路を必ずカバー**する。最低3本:
1. **`ok, ok, err` 混在** → 検索結果を返さずに `QueryErrorReply` を経由して最終的に失敗（件数ゼロに化けないこと）
2. **`err` → retry → `success`** → 1回目 err, 2回目 ok の fake で結果が正常に回復すること
3. **`err` → retry → `err`** → 最終的に `RuntimeError` が raise され、`__cause__` が `QueryErrorReply` であること（`raise from` の連鎖が崩れないこと）

### E2E テスト: `tests/test_e2e_sync.py`（2-router）
2台分の zenohd を subprocess で localhost 上の別ポート（7447, 7448）で起動し、
別プロセスの client から put/get する構成で以下を自動化:
- オフライン差分同期（PoC Step 4 相当）
- split-brain + 復旧時の **tombstone key 伝播による非表示化**（PoC Step 5 相当）

> Docker Compose 版は将来の拡張。PoC では subprocess + 一時ディレクトリで十分。

合格基準を `tests/test_e2e_sync.py` の docstring に明記し、CI 相当で常時実行できる構成にする。

---

## 運用ポリシー

### 保持期間 / 容量管理
- PoC では observation 無期限保持。Tombstone は 30 日経過後に物理削除（`mesh-mem gc` で手動 or cron）。
- RocksDB が肥大化した場合は、該当 storage を一時停止 → 両PCで該当キーを delete → replication 同期を待って再起動、という手順を README 化する。

### 機微情報混入時の緊急手順
1. 該当 observation に対し `mesh-mem delete <id>` を即実行（tombstone 伝播）
2. 両PCで `mesh-mem gc --force-id <id>` を実行し RocksDB から物理削除
3. `mem/obs/**` と `mem/tomb/**` で該当 key が消えたことを確認

---

## 将来の拡張（PoC後）

- **TLS + 認証**: `tls/` エンドポイントに移行、`usrpwd` または TLS 認証を必須化（NDA案件対応）
- **全文検索強化**: 件数が数万件を超えたらローカルに whoosh / sqlite FTS5 インデックスを構築、zenoh subscriber で自動更新
- **Heartbeat / 生存判定**: `mem/heartbeat/{client_id}/{pc_id}/{session_id}` に定周期 put する送信ループと、対向の停止判定（30秒無更新で停止と見做す等）をセットで実装。PoC 範囲外。
- **`mem/ctx/**`**: エージェント横断のプロジェクトコンテキスト共有（設計は別途）。
- **Hooks 自動保存**: Claude Code / Gemini CLI の PostToolUse hook から自動的に save_observation を呼ぶ（ノイズ対策のフィルタ戦略必須）
- **Zenoh 1.9 Regions**: ビル/テナント単位のサブリージョン分離（機密案件のメモリ隔離）
- **QUIC mixed reliability**: observation はReliable、heartbeatはBestEffort
- **Tailscale 経由のリモート同期**: 出先ノートPCを3台目の router として追加
- **削除の分散合意**: 現在の tombstone + last-writer-wins から、より堅牢な CRDT ベース設計への移行検討

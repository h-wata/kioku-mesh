# Local Backend Design — Issue #109

> Historical design note: command/package/path names in the original sketch may use `mesh-mem`. The implemented current behavior is documented in [docs/Spec.md](../Spec.md): `kioku-mesh init --mode local` writes `~/.config/kioku-mesh/config.yaml` (with legacy `mesh-mem` fallback) and stores local-only data under `state_dir()/local/index.db`.


## 背景

`mesh-mem init --mode local` で zenohd 不要な single-machine mode を提供する。
`LocalIndex` (SQLite) をサイドカーから first-class バックエンドに昇格させる。

## バックエンド抽象インターフェース

`src/mesh_mem/backend.py` に `typing.Protocol` で定義する。

```python
class MemoryBackend(Protocol):
    def put_observation(self, obs: Observation) -> None: ...
    def put_tombstone(self, obs: Observation, reason: str = '') -> None: ...
    def search_observations(self, **kwargs) -> list[Observation]: ...
    def find_observation_by_id(self, observation_id: str) -> Observation | None: ...
    def physical_delete_observation(self, observation_id: str) -> tuple[bool, bool]: ...
    def get_status(self) -> BackendStatus: ...
    def drain_pending(self, limit: int | None = None, *, wait: bool = True) -> int: ...
    def gc_tombstones(self, retention_days: int, project: str) -> int: ...
    def close(self) -> None: ...
```

### LocalBackend (SQLite only)
- `put_observation`: `LocalIndex.upsert(obs)` + tombstone リストから消す
- `put_tombstone`: `LocalIndex.mark_deleted(obs.observation_id, tomb.deleted_at)`
- `search_observations`: `LocalIndex.search(**kwargs)`
- `find_observation_by_id`: `LocalIndex.find_by_id(id, include_deleted=True)`
- `physical_delete_observation`: `LocalIndex.physical_delete(id)`
- `get_status`: `LocalIndex.visibility_counts()` のみ（Zenoh 統計なし）
- `drain_pending`: no-op 0 を返す（pending_puts は Zenoh 失敗時キューのため）
- `gc_tombstones`: `LocalIndex.list_tombstoned_obs_in_project` → `LocalIndex.physical_delete`
- zenohd が PATH になくてもエラーにならない

### ZenohBackend (既存 store.py)
- 既存の `put_observation`, `search_observations` 等を委譲するラッパー

## ファイルレイアウト

```
src/mesh_mem/
  config.py          # 新規: ~/.config/mesh-mem/config.yaml 読み書き
  backend.py         # 新規: MemoryBackend Protocol + LocalBackend + get_backend()
  local_index.py     # 変更なし（内部実装として利用）
  store.py           # 変更なし（ZenohBackend の実装として利用）
  __main__.py        # 変更: --mode local 追加、backend 経由に切り替え
  mcp_server.py      # 変更: get_backend() 経由に切り替え
```

## バックエンド選択機構

### config.yaml フォーマット
```yaml
# ~/.config/mesh-mem/config.yaml
backend: local  # or: zenoh (default when absent)
```

### 優先度
1. `MESH_MEM_BACKEND=local` 環境変数（最高優先）
2. `~/.config/mesh-mem/config.yaml` の `backend:` フィールド
3. デフォルト: `zenoh`（後方互換）

### `mesh-mem init --mode local` の動作
1. zenohd.json5 は生成しない
2. `~/.config/mesh-mem/config.yaml` に `backend: local` を書く
3. 次のステップは `mesh-mem save` / `mesh-mem search` がすぐ使える

## B2: backend-switch 時の local row shadowing 問題

### 問題の再現

W4 review で再現確認済み: `LocalBackend` と `ZenohBackend` が同じ SQLite index
(`state_dir()/index.db`) を共有しているため、`local → zenoh` に切り替えた直後に
`rebuild_from_zenoh()` が走ると、Zenoh upstream に存在しない local-only row が
`shadowed` 扱いになり通常 search から消える (`RebuildStats(shadowed=1)`)。
これは silent data loss に相当する。

### 選択した修正方針

**方針 1: local backend 用 state/index を Zenoh cache と物理的に分離する。**

- `LocalBackend` は `state_dir() / 'local' / 'index.db'` を使う
- `ZenohBackend` (store.py 経由) は従来通り `state_dir() / 'index.db'` を使う

選定理由:
- local-only データは Zenoh upstream と独立して永続化すべきであり、
  Zenoh 側の `rebuild_from_zenoh()` が触れないパスに置くのが最もシンプル。
- 方針 2 (識別子フラグ) は rebuild ロジックに変更が必要で影響範囲が広い。
- 方針 3 (明示 migration) は UX が大きく変わり PR スコープ外。
- パス分離は `LocalIndex.connect(db_path)` に引数を渡すだけで実現可能。

### 実装概要

- `src/mesh_mem/backend.py` の `LocalBackend.__init__` で
  `state_dir() / 'local'` ディレクトリを作成し、そこの `index.db` を使う。
- 既存の `store.py` と `local_index.py` への変更なし。
- 回帰テスト: `test_backend_switch_does_not_shadow_local_rows` を追加。

## テスト戦略

### contract tests の共有
`tests/test_backend_contract.py` に `@pytest.fixture(params=['local', 'zenoh'])` でパラメタライズ。
zenoh テストは `pytest.mark.skipif(not zenohd_available(), ...)` でスキップ。

各バックエンドに同一の save → search → delete → gc テストを流す。

### local-only テスト
- zenohd が PATH にない環境でも `put_observation` / `search` が動くか
- `mesh-mem init --mode local` で config.yaml が生成されるか
- `MESH_MEM_BACKEND=local` 環境変数でオーバーライドが効くか

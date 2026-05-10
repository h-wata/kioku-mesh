# ADR-0007: 検索 read-path に SQLite local index sidecar を採用

- Status: Accepted
- Date: 2026-04-30
- Supersedes: ADR-0003

## Context

ADR-0003 では「全件 Zenoh から取得 → Python 側でフィルタ」を PoC スコープの
初期実装として採用し、Issue #7 で SQLite + FTS5 への段階導入を予告していた。

その後の Tier-3 ベンチで以下が確認された。

- 16k obs × `limit=1000` で全走査 2.2 秒。limit ほぼ無関係に走査件数が支配。
- 50k obs スパイク（commit `154559f`, TASK-134）で SQLite rebuild と query が
  サブ 100ms に収まることを確認 → 段階導入を **GO 判定**。

ADR-0003 が punt していた次フェーズの実装が完了したので、新しい read-path
を ADR として記録し、ADR-0003 を Supersede する。

## Decision

検索 read-path を **SQLite local index sidecar** に切り替える。
全 5 phase（Issue #7）で構築:

1. **Phase 1 spike** (`154559f`): 50k obs で SQLite rebuild / query latency 計測
2. **Phase 2 write-path** (`8b06c14`): `LocalIndex.put_observation` /
   `put_tombstone` を `store.put_*` から dual-write。`obs_index` テーブルは
   `observation_id` を PK に `project` / `created_at` の index 付き。
   tombstone は `deleted_at` 列で表現（行は残す）。WAL + `synchronous=NORMAL`。
3. **Phase 3 read-path 切替** (`73e8ba2`): `search_observations` /
   `find_observation_by_id` を `LocalIndex.search` / `find_by_id` に routing。
   旧 Zenoh full scan は `_search_via_zenoh` / `_find_by_id_via_zenoh` として
   残し、`MESH_MEM_DISABLE_INDEX=1` で fallback 可能。`physical_delete` /
   `gc_expired_tombstones` も sidecar とロックステップで purge。
4. **Phase 4 一貫性確保** (`f195cd5`): プロセス起動時に `rebuild_from_zenoh`
   で `mem/obs/**` / `mem/tomb/**` を index に流し込み、`mem/obs/**` /
   `mem/tomb/**` の subscriber を張って replication 受信分も実時間で index 化。
   rebuild は `MESH_MEM_SKIP_REBUILD=1` でスキップ可、Zenoh 接続失敗は warn
   してスワロー（read/write は止めない）。
5. **Phase 5 Tier-4 検証** (`c06f0b0`): 50k obs 実機で sub-200ms を確認。

filter 評価順は内部状態として lock test (`217206e`) で固定。FTS5 は当面導入
せず、`query` は SQLite の `LIKE` 部分一致で十分とする（Tier-4 で latency
要件をクリア）。

## Consequences

- **良い点**: 50k obs でも search latency が sub-200ms に収まり、ADR-0003 で
  punt した「走査件数の削減」を実現。
- **良い点**: identity / project / since_iso のフィルタを Zenoh 往復なしで
  解決でき、ネットワーク転送量が桁オーダーで減る（特に 36MB 級の store で
  顕著）。
- **良い点**: Zenoh 全走査経路は dead code 化せず `MESH_MEM_DISABLE_INDEX=1`
  で温存。index 不具合時の縮退と smoke test の両方で価値がある。
- **悪い点**: index と Zenoh の二重管理に整合性責任が生じる。Phase 4 の
  rebuild + subscribe 二段で吸収しているが、起動順序（zenohd → index init）
  と subscribe 漏れが運用上の壊れ方の入口になりうる。
- **悪い点**: 長時間稼働プロセスで SQLite WAL が肥大化（130MB 観測例）。
  ADR-0008 / commit `f9a0495` で `wal_checkpoint(TRUNCATE)` の周期化により対処。
- **悪い点**: Zenoh の `mem/obs/**` 全走査経路は `find_by_id` 用途で残るが
  36MB 級 store では query timeout の引き金になる（Issue #40 で個別追跡）。
- **今後**: FTS5 / 別言語 ranking が要るユースケースが見えたら段階導入。
  現状は `LIKE` で要件十分。

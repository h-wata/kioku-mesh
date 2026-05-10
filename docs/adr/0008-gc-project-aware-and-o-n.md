# ADR-0008: GC を project スコープ + O(N) fast path に拡張

- Status: Accepted
- Date: 2026-05-08
- Supersedes: ADR-0005

## Context

ADR-0005 では PoC スコープを優先し、`gc_expired_tombstones` を
**project 非依存の保持期間 only** で実装し、Issue #11 で
`--project` フィルタ追加を予告していた。

その後の運用で 2 つの問題が顕在化した。

1. **複数 project が同一 Zenoh スペースに共存する環境**で gc を打つと、
   別 project の tombstone まで削除される（ADR-0005 で予告された通りの
   制約だが、本番運用に到達して影響が無視できなくなった）。Issue #11。
2. **本番ホストで gc が ~60 秒**かかる事案（Issue #32）。原因は
   `mem/tomb/**` の Zenoh 全走査を毎回行っていたこと。マッチ件数が
   project スコープでは少数でも、グローバルな tombstone 総数 M に対して
   O(M) のコストを払っていた。
3. **長時間稼働の mesh-mem-mcp プロセスで SQLite WAL が肥大化**
   （観測 130MB）。SQLite の auto-checkpoint は接続が永続的に open だと
   truncate フェーズに到達できない。

ADR-0005 が punt した内容が完了し、さらに性能特性の課題まで顕在化したため、
新しい GC 設計を ADR として記録し、ADR-0005 を Supersede する。

## Decision

### A. project 非依存制約の解除（commit `2faad5b`, Issue #11）

`gc_expired_tombstones` に `project: str | None` 引数と `gc --project`
CLI フラグを追加。`None` の場合は従来通り全 project sweep（後方互換）。

### B. project-scoped O(N) fast path（commit `f9a0495`, Issue #32-A）

`project` 指定時の経路を Zenoh 全走査から **SQLite index 起点**に切り替える。

- `LocalIndex.list_tombstoned_obs_in_project(project, cutoff_iso)` が
  `(observation_id, payload_json)` を返す。これにより `key_expr` も
  `tombstone_key_expr` も Zenoh 往復ゼロで導出できる。
- `store._gc_via_sqlite_index` が per-id の **exact-key `session.delete`** を
  発行し、SQLite 側も `physical_delete` でロックステップ purge。
- index が空（=fresh state dir）の場合は `rebuild_from_zenoh` を idempotent に
  自動実行してから fast path に入る（`row_count() == 0` でゲート）。
  すでに subscriber 経由で揃っている長時間プロセスでは rebuild をスキップ。
- `MESH_MEM_DISABLE_INDEX=1` または fast path 例外時は legacy global scan に
  fall through。

### C. SQLite WAL の bound 化（commit `f9a0495`, Issue #32-B）

`LocalIndex` が **upsert 256 回ごと**と **`close()` 時**に
`PRAGMA wal_checkpoint(TRUNCATE)` を発行。バックグラウンドスレッドは入れず、
write path に同期で小さく払う。checkpoint 失敗は DEBUG ログで握りつぶし、
write 自体は止めない。

## Consequences

- **良い点**: 複数 project 共存環境で gc が他 project を巻き込まない
  （ADR-0005 が punt した制約を解消）。
- **良い点**: project-scoped gc が tombstone 総数 M に依存せず、対象 project の
  N に対してのみコストを払う。本番で 60 秒 → 秒オーダーに短縮。
- **良い点**: WAL 130MB 級の肥大化が再発しない。長時間稼働 MCP プロセスでも
  SQLite ファイルサイズが現実的なバウンドに収まる。
- **良い点**: 後方互換が保たれている（`--project` なし時は従来通り）。
- **悪い点**: fast path が `MESH_MEM_DISABLE_INDEX=1` では効かず legacy 経路に
  落ちる。disable 環境では性能改善が無く、ADR-0005 時代のコストに戻る点に
  運用者が注意する必要。
- **悪い点**: `rebuild_from_zenoh` 自動起動条件（index 空）はシンプルだが、
  破損 index で row_count > 0 だが内容欠損というケースは自動回復しない
  （手動で削除して再起動する運用）。
- **悪い点**: `wal_checkpoint(TRUNCATE)` を write path 同期で発行するため、
  256 回に 1 回の write は他より僅かに遅い。実測でユーザ可視のスパイクは
  なく許容。
- **今後**: project-agnostic 時の fast path も導入余地あり（全 project
  iteration on SQLite）。優先度は低い。

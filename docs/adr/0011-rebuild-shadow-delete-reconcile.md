# ADR 0011: rebuild reconcile は shadow-delete を経由する

- **Status**: Accepted
- **Date**: 2026-05-18
- **Supersedes**: なし
- **Related**: ADR-0010 (zenoh-as-source-of-truth), ADR-0007 (sqlite-local-index-sidecar), ADR-0002 (existence-based tombstone), Issue #67, PR #65

## Context

ADR-0010 で「Zenoh が真実源、SQLite local index は派生キャッシュ」と定めた。
ただし「Zenoh に存在しない obs を index 上どう扱うか」の具体的手段は #67 で
別途設計する、と punt していた。

実際の reconcile には大きく分けて 3 つの選択肢があった:

- **A. Hard-delete on rebuild**: `existing - zenoh_set` を即 `physical_delete`
- **B. Shadow-delete + 復活**: `shadowed_at` を立てて hide、後で復帰可能
- **C. 起動条件付き hard-delete**: rebuild の Zenoh get が「十分に完全」と
  判定できたときだけ hard-delete

A は ADR-0010 に厳密に従うが、transient な不在 (peer 一時離脱、storage
起動順序、scouting に時間がかかる、長期 offline peer が後で戻ってくる) で
誤 prune が起き、戻ってきても復元できない。mesh-mem は hub-and-spoke
(ADR-0006) で persistence storage が 1 ノードに集中することが多く、その
ノードの起動シーケンス次第で他 peer の rebuild が空の Zenoh view を見る
リスクが具体的に存在する。

C は「十分に完全」の判定が事実上不可能。zenoh の `session.get` は部分応答
を返した場合でも error にならない。タイムアウトを引いても「ネットワーク
が遅いだけ」と区別できない。

B は ADR-0010 の精神に沿いつつ、誤 prune を可逆な中間状態に倒せる。

## Decision

`LocalIndex.rebuild_from_zenoh` は `existing - zenoh_set` の live 行 (まだ
tombstone も shadow も無いもの) を **shadow-delete** (`shadowed_at` 列に
タイムスタンプを set) して hide する。物理削除はしない。

shadow と既存の状態の階層:

- **live**: `deleted_at IS NULL AND shadowed_at IS NULL`。`search` /
  `find_by_id` のデフォルト出力対象
- **shadowed**: `deleted_at IS NULL AND shadowed_at IS NOT NULL`。
  rebuild が「Zenoh に見えなかった」と判定した状態。`search` から
  hide、ただし `include_deleted=True` で diagnostics 用に取得可能
- **tombstoned**: `deleted_at IS NOT NULL`。ADR-0002 の existence-based
  tombstone と一致。`shadowed_at` は同時に NULL に倒す
  (tombstone は shadow より強い意味)

shadow からの復帰経路:

- `upsert(obs)` (subscriber PUT、rebuild の obs スキャン、手動 save) が
  既存行に対して `shadowed_at = NULL` をセット → 復活
- `mark_deleted(obs_id, deleted_at)` が `shadowed_at = NULL` をセット +
  `deleted_at` をセット → tombstone 状態に昇格

rebuild の挙動も次の不変条件を満たす:

- 既に `deleted_at` を持つ行に対しては、新しい tombstone が来ても
  `deleted_at` を上書きしない (existence-based、最初の tomb 時刻保持)
- live row の `payload_json` が unchanged なら upsert を打たない
  (ADR-0007 / Issue #32 の WAL 抑制)

retention 経由の物理削除はこの ADR のスコープ外。長期 shadow を gc する
経路は別途追跡 (#67 の follow-up issue) する。

## Consequences

- **良い点**: ADR-0010 の「Zenoh が正」原則を保持しつつ、transient gap
  での誤 prune を可逆な状態 (shadow) に倒せる。長期 offline peer が
  ようやく rebuild に追いついた瞬間に search 結果が大量に消える、と
  いう破壊的挙動を回避できる。
- **良い点**: 復帰経路が単一 (`upsert` が `shadowed_at` を NULL に
  戻す) なので、subscriber の PUT も rebuild の rescan も手動 save も
  同じ semantics で対称的に shadow を解除できる。
- **良い点**: tombstone と shadow が orthogonal な状態として表現される
  ので、`get_memory_status` の `index_rows: live=… / tomb=… / shadow=…`
  ブレークダウンで read-path の現状が観測できる。
- **悪い点**: index の物理サイズは即座には縮まない。shadow 状態でも
  行は残るため、index.db / WAL のサイズが意図せず長期間膨らむ可能性
  がある。bench データの長期残骸を消すには別途 retention gc が要る。
- **悪い点**: 「永久に消えた」と「一時的に見えない」を区別する責務を
  ユーザに転嫁することになる。`include_deleted=True` で見える shadow
  行を `delete_memory` / `physical_delete` で能動的に消すか、retention
  gc が物理削除するか、Zenoh 側に obs が戻ってきて自動復活するかの
  3 経路でしか永続状態は決まらない。
- **トレードオフ**: shadow を即 hide することで、subscriber 経由で
  あとから来る obs PUT との race が発生しうる (rebuild が shadow した
  直後に PUT が届く)。`upsert` の `ON CONFLICT ... SET shadowed_at =
  NULL` で復帰するので最終的には整合するが、瞬間的に「search から
  消えてから戻る」フリッカーは起きる。実害は小さい想定。

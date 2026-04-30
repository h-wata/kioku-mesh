# ADR-0002: 削除モデルに existence-based tombstone を採用

- Status: Accepted
- Date: 2026-04-27
- Supersedes: なし

## Context

分散環境では削除操作の意味論が難しい。split-brain 後に一方のノードで削除、もう一方では未削除の状態が生じたとき、ネットワーク復旧後に「削除が勝つか、生き残りが勝つか」を決める必要があった。

timestamp による last-writer-wins (LWW) は NTP ズレの影響を受けやすい（実測で 12.75 秒のスキューを確認）。

## Decision

削除を表す tombstone key（`mem/tomb/<agent_family>/<client_id>/<pc_id>/<session_id>/<obs_id>`、obs key の `mem/obs/...` と同一の identity 階層をミラー）を Zenoh にパブリッシュし、**その key の存在を削除シグナルとして扱う**。タイムスタンプ比較ではなく「tombstone key が 1 件でも観測されたら非表示」とする existence-based モデルを採用する。

- `obs` レコードは immutable。更新は新 ID で新規保存
- obs key は `mem/obs/<agent_family>/<client_id>/<pc_id>/<session_id>/<obs_id>` の 5 階層
- GC は `retention_days` 経過後に tombstone key を物理削除
- tombstone の伝播は Zenoh の eventual-consistency に委ねる

## Consequences

- **良い点**: HLC スキューの影響を受けない。tombstone key が届いた時点で削除が確定し、timestamp の前後関係を判定する必要がない
- **良い点**: split-brain 復旧後、tombstone key が replication で届けば自動的に収束する。追加のコンフリクト解決ロジック不要
- **良い点**: obs が immutable なため、Zenoh の key 階層 `mem/obs/<agent_family>/<client_id>/<pc_id>/<session_id>/<obs_id>` でキャッシュやインデックスが容易
- **悪い点**: 「削除の取り消し」ができない。tombstone が先に到達してしまうと obs は復活しない（existence-based の本質的制約）
- **悪い点**: tombstone が未到達のまま GC されると、他ノードでは obs が生き続ける可能性がある（retention_days のノード間合意が必要）

# ADR-0003: 検索実装を全件取得→Python側フィルタで先行実装（FTS5導入前）

- Status: Accepted
- Date: 2026-04-27
- Supersedes: なし

## Context

`search_observations` では project / since_iso / タグ / 全文クエリによる絞り込みが必要。
Zenoh の selector は key 階層（agent_family/client_id/pc_id/session_id）のみを表現でき、任意のメタデータによるフィルタをサポートしない。

SQLite インデックスや Zenoh Rust プラグインの実装は PoC スコープを超えるため、初期実装の選択肢として「全件取得→Python フィルタ」と「軽量 SQLite キャッシュ」を比較した。

## Decision

`mem/obs/**` を全件 Zenoh から取得し、Python 側で project / since_iso / query をフィルタする実装を初期採用する。Zenoh selector には identity 系（agent_family/client_id/pc_id/session_id）の絞り込みのみを乗せる。

Issue #7 で SQLite + FTS5 インデックスによる段階的改善を予定する。

## Consequences

- **良い点**: Zenoh と Python の責任を明確に分離できる（transport vs. filter）。将来の SQLite 導入時に Python 側のみを置き換えればよい
- **良い点**: Rust プラグイン開発を回避し、PoC を Python のみで完結できる
- **悪い点**: TASK-115（Tier-3 ベンチ）で 16k obs × limit=1000 のとき全走査コスト 2.2 秒を確認。limit 値にほぼ依存せず走査件数が支配的
- **悪い点**: obs 件数が増えるほどネットワーク転送量も線形に増加する
- **今後**: 「走査件数（= reply 数）の削減」が第一の打ち手と判定。Issue #7 (TASK-131) で SQLite + FTS5 の段階導入を設計済み

# ADR-0005: GC の初期実装を project 非依存スコープで先行実装

- Status: Superseded by ADR-0008
- Date: 2026-04-27
- Supersedes: なし

## Context

`mesh-mem gc --retention-days N` は保持期間を超えた tombstone を物理削除する。
初期実装時点では単一 project 運用を前提としており、project フィルタの必要性は認識しつつも PoC スコープとして後回しにした。

TASK-125 の実機テストで問題が顕在化: `--retention-days 0` を gc-test プロジェクトに対して実行したところ、DR テスト・NTP テスト等の別プロジェクトの tombstone も一括削除された（期待 3 件削除 → 実測 9 件削除）。

## Decision

初期実装では `gc_expired_tombstones` に `--project` フィルタを持たせず、全 tombstone を保持期間のみで判定する実装を採用する。

Issue #11 で `--project` フィルタの追加を予定する（commit `2faad5b` で先行実装済み）。

## Consequences

- **良い点**: GC 実装がシンプルで、PoC フェーズの検証コストを最小化できる
- **悪い点**: 複数プロジェクトが同一 Zenoh スペースに共存する環境では、GC 実行時に意図しないプロジェクトの tombstone まで削除される
- **悪い点**: テスト環境で複数プロジェクトを並行運用する場合、GC のタイミングによってテスト結果の integrity が損なわれる
- **今後**: `--project` フィルタが追加されることで、プロジェクト単位の GC 制御が可能になる。Issue #11 で対応予定

# ADR-0001: Zenoh + RocksDB をメッシュトランスポートに採用

- Status: Accepted
- Date: 2026-04-27
- Supersedes: なし

## Context

複数の AI エージェント（Claude Code, Claude Desktop, Gemini CLI, Codex CLI 等）が複数 PC にまたがって観測（Observation）を共有するには、分散 pub/sub + 永続化基盤が必要だった。

候補として NATS JetStream、etcd、Redis Sentinel、Zenoh を比較検討した。
PoC フェーズでは「LAN 内 2 ホスト間で eventually-consistent な key-value レプリケーション」を最小コストで実現することが最優先だった。

## Decision

トランスポートに **Zenoh 1.9** + ストレージバックエンドに **RocksDB plugin** を採用する。

- Zenoh の HLC (Hybrid Logical Clock) タイムスタンプが標準装備されており、split-brain 時の衝突解決に追加ロジックが不要
- `replication plugin` の digest 比較機構（hot/warm/cold era）により、常時接続中の定期同期コストを低く保てる
- バイナリプロトコルで LAN 内低遅延通信に最適
- Python SDK が充実しており、エージェント側実装を Python のみで完結できる
- 将来的に NATS 等への置き換えを妨げないよう、transport 層を `store.py` 内に閉じ込める設計とする

## Consequences

- **良い点**: HLC により各 PC が独立してタイムスタンプを発行でき、NTP ズレ（実測 12.75s 超）があっても because-relationship が崩れない
- **良い点**: split-brain 復旧後の再同期が ~5 秒で収束。era 粒度の選択（cold era = 1h 単位）は「常時接続中の定期 digest 交換コスト」にのみ影響し、分断時間の長短には非依存
- **悪い点**: Zenoh の selector では Python 側フィルタ（project/タグ/全文検索）を表現できないため、検索はいったん全件取得→Python フィルタになる（→ ADR-0003 参照）
- **悪い点**: zenohd プロセスの管理（systemd unit, NTP 同期）が運用要件として加わる

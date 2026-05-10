# ADR-0006: メッシュ topology に hub-and-spoke を採用

- Status: Accepted
- Date: 2026-05-10
- Supersedes: なし

## Context

ADR-0001 で採用した Zenoh + RocksDB は transport / storage 層の選択であり、
peer 同士の接続パターン（topology）には触れていなかった。初期の 2-peer
（home / office）構成は事実上の full-mesh で、両 peer が相互に
`connect.endpoints` を持っていた。

3-peer 以上に拡張する段階で次の運用課題が顕在化した。

- full-mesh だと **peer N 台に対し N×(N-1) の `connect` 行を維持**することになり、
  新 peer 追加時に既存全 peer の config を編集 → zenohd 再起動が必要になる。
- 各 spoke が **inbound TCP/7447 を許可するファイアウォール設定**を要求され、
  ノート PC や VPN 越しの spoke ほど運用ハードルが高い。
- gossip-based discovery（Zenoh の機能）も候補だが、`scout` のマルチキャストは
  LAN を跨ぐ環境では使えず、unicast gossip でも結局「seed をどこに置くか」
  の問題に帰着する。

2026-05-10 に 3-PC（home / WSL2 / office）で予備実験を実施し、Zenoh の
**router transit（spoke→hub→spoke の中継）が単方向 connect だけで双方向
セッションを成立させ、cross-spoke の query / replication にも届く**ことを実機で
確認した（`docs/poc-reports/topology-2026-05-10.md`）。

## Decision

mesh-mem の正規 topology を **1 hub + N spokes** とする。

- 常時稼働する peer を 1 台「hub」に指名し、全 spoke が到達しうる IP
  （LAN / Tailscale / VPN）を `listen.endpoints` に**集約**する。
- 各 spoke は `connect.endpoints` に **hub の IP のみ**を書く。spoke 同士は
  互いに `connect` しない。
- spoke→spoke の通信（query / replication digest 交換含む）は
  Zenoh router transit で hub 経由となる。
- 新 spoke 追加時は spoke 側 config だけを書き、既存 peer は無修正・無再起動。
  例外は「hub の `listen` に新 spoke から到達できる IP が無い」ケースのみで、
  この場合に限り hub の listen 拡張（1 度きり）と再起動が必要。
- 公開テンプレート（`config/zenohd_peer.json5.template`）と
  5-peer 例（`config/peers/example_5peer.md`）はこのパターン前提で記述する。

gossip-based discovery（Exp 3）は当面導入しない。N が 10 を大きく超える
段階で再検討する。

## Consequences

- **良い点**: 新 spoke 追加で既存 peer の config / 再起動が一切不要。
  運用上の最大の摩擦が消える。
- **良い点**: spoke 側は **outbound TCP/7447 を hub に向けるだけ**で済み、
  Windows Firewall / nftables の default-deny-inbound と相性が良い。
  ファイアウォール inbound 許可は hub 1 台のみ。
- **良い点**: hub の `listen` を最初に「将来到達しうる全 IP」で集約しておけば、
  以後の spoke 追加は完全に hands-off で済む。
- **悪い点**: hub が **cross-spoke 通信の単一障害点**になる。hub down 中は
  各 spoke の local 読み書きは継続可能（rocksdb は健在）だが、spoke 間の
  replication digest 交換は止まり、復旧後に digest 不一致区間を再同期する。
  ミッションクリティカル用途では `connect.endpoints` に 2 hub を併記する
  運用に切り替える（Zenoh 側でグラフ重複は除去される）。
- **悪い点**: hub の役割を別 peer に切り替える操作は非自明で、全 spoke の
  `{HUB_IP}` 書き換え + 再起動を伴う。`config/peers/example_5peer.md`
  「Changing the hub」セクションでメンテナンス手段を文書化済。
- **悪い点**: 既存の `config/zenohd_home.json5` / `zenohd_office.json5` は
  この pattern の 2-peer 特例として残るが、3-peer 以降の運用は template ベース
  に倒すため、これらは「2-peer の例」と位置付けが変わった（コード上は
  互換維持）。

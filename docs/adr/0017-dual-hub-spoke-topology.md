# ADR-0017: hub 冗長化 — connect.endpoints に 2 hub を併記する

- Status: Accepted
- Date: 2026-06-05
- Supersedes: なし（ADR-0006 の悪い点「hub が単一障害点」への対策）

## Context

ADR-0006 で採用した hub-and-spoke topology は、cross-spoke 通信が hub 経由に
なるため、hub 停止中は spoke 間の replication digest 交換が止まるという既知の
課題を抱えていた。ADR-0006 の「悪い点」節にも

> ミッションクリティカル用途では `connect.endpoints` に 2 hub を併記する
> 運用に切り替える

と記載済みだった。

**Zenoh の挙動検証（2026-06-05 実機確認）**

実際に 2nd hub を追加する前に、「起動時に down している endpoint を
connect.endpoints に書いて大丈夫か」を zenohd v1.9.0 で確認した。

設定:
```json5
{
  mode: "router",
  listen:  { endpoints: ["tcp/127.0.0.1:19999"] },
  connect: { endpoints: ["tcp/127.0.0.1:29999"] },  // 29999 は誰も listen していない
  scouting: { multicast: { enabled: false } },
}
```

結果:
- zenohd は **正常起動** した（WARN/ERROR は一切出力されなかった）
- config dump の `connect` フィールドに `"exit_on_failure": null` が現れており、
  デフォルトが「接続失敗でも終了しない」であることが確認できた
- down endpoint への接続はバックグラウンドでリトライされ、相手が起動次第
  自動接続される

この挙動は Zenoh の設計通りであり、`exit_on_failure: true` を明示しない限り
プロセスが落ちることはない。

## Decision

spoke の `connect.endpoints` に **2 つの hub endpoint** を書くことを標準とする。

```json5
connect: {
  endpoints: [
    "tcp/<HUB1_IP>:7447",   // primary hub（常時稼働を目指す）
    "tcp/<HUB2_IP>:7447",   // secondary hub（停止中でも spoke 起動に影響なし）
  ],
},
```

- hub1 が down しても hub2 経由で spoke 間通信が継続する
- hub2 が down 中でも spoke は正常起動し、hub2 が復旧したタイミングで自動接続する
- Zenoh はグラフ重複（両 hub とも到達可能な場合）を内部で除去するため、
  双方向通信の重複送信は発生しない
- `config/zenohd_peer.json5.template` の `connect.endpoints` を 2 行に拡張する

### 既存 2-peer（home/office）構成への適用

`config/zenohd_home.json5` / `config/zenohd_office.json5` は現状の 2-peer 特例
として残す。3 台目以降を追加する際に template を元に dual-hub 構成を組む。

## Consequences

- **良い点**: hub 1 台が停止しても spoke 間 replication が止まらない
- **良い点**: spoke 起動時に片方の hub が停止していても起動失敗にならない
- **良い点**: 設定変更は spoke 側の config に 1 行追加するだけ。再起動不要。
- **悪い点**: hub を 2 台管理する運用コストが増える。最低 1 台を常時稼働させる
  規律が必要。
- **悪い点**: hub2 が長期停止している場合、replication の「生きている hub 経由の
  digest 交換」のみに頼ることになり ADR-0006 の課題に戻る。定期的に両 hub の
  稼働確認が必要。

# ADR 0015: 証明書 enrollment はコピペをデフォルトにし、SSH 自動化はオプトインにする

- **Status**: Accepted
- **Date**: 2026-05-30
- **Supersedes**: なし
- **Related**: [[0014-mtls-via-csr-private-ca]]

## Context

[[0014-mtls-via-csr-private-ca]] で mTLS を CSR ベースの private CA で提供することにした。
そこでは「ホスト間を移動するのは非秘密(CSR・cert・`ca.crt`)だけで、移動手段は自由」
とだけ決め、**具体的な enrollment 手順**(spoke で CSR を作り、CA ホストで署名し、
証明書を spoke に戻す、までの導線)は未確定のまま `scp` を例示していた。

実機で dogfood した結果、この `scp` ベースの導線が **煩雑**だと分かった。1 peer の
enrollment に最低 4 手:

```
spoke:  tls request                       # CSR を生成
spoke:  scp peer.csr hub:/tmp/...          # CSR を CA ホストへ
hub:    tls sign /tmp/... -o /tmp/...       # 署名
hub:    scp /tmp/...crt ca.crt spoke:/tmp/  # 証明書 + ca.crt を spoke へ戻す
spoke:  tls install --cert ... --ca ...     # 取り込み
```

問題は手数そのものより、**scp という前提依存**だった:

- scp / ssh を日常的に使わないユーザーには不自然な依存になる。
- `/tmp` のパス管理を両ホストで人手でやるため、取り違え・上書きの事故が起きやすい。
- 「ファイルをどこに置いたか」を peer 間で口頭同期する必要が出る。

一方、移動する material は**全部非秘密**([[0014-mtls-via-csr-private-ca]] の核心)。
つまり「安全に運ぶ」必要はなく、「楽に運ぶ」だけが要件。ここに enrollment UX の
分岐があった。

## Decision

**コピペ enrollment をデフォルトの土台にし、SSH 自動化はその上のオプトイン層にする。**

### B(土台): コピペ enrollment

- `tls request` は CSR を **base64 文字列として stdout に出せる**。
- `tls sign` は CSR を **stdin(`-`)から読め**、署名済み証明書(+ `ca.crt`)を
  stdout に返せる。
- ユーザーは 2 つの文字列を端末間でコピペするだけ。scp / ファイルパス管理が消える。
- SSH 非依存・追加依存ゼロ・経路を問わない(SSH でも Slack でも Tailscale でも何でもよい)。

根拠は「**Claude Code の認証もコピペで、それでみんな回っている**」。非秘密文字列の
コピペは既に広く受け入れられている UX で、学習コストが無い。

### C(オプトイン上位): SSH 自動化

- `tls enroll <ca-host>` で request → sign → install のサイクルを SSH 越しに**全自動**化。
- SSH を既に使っている人向けの上位の糖衣。B の上に薄く乗るだけで、B が無いと存在できない。

### stdout 露出への配慮

移動する material は非秘密なので stdout に出ても**実害は無い**。ただし
「端末履歴 / スクロールバック / ログに残るのが嫌」という**選好**はあり得る。そこで:

- **stdout をデフォルト**にしつつ、`-o <file>` でファイル出力も選べる両対応にする。
- **秘密鍵 (`peer.key`) は何があっても表示しない**。コピペ対象は常に非秘密のみ。

## Consequences

- **良い点**: 1 peer の enrollment が「2 文字列をコピペ」に縮む。scp / パス管理の事故面が消える。
- **良い点**: SSH を使わない層でも mTLS を運用できる(B が transport 非依存なため)。
- **良い点**: SSH を使う層は `tls enroll` 一発で済む(C)。両方の層を1つの設計で満たす。
- **良い点**: stdout を嫌う層には `-o` で逃げ道があり、それでも秘密鍵は絶対に動かない不変条件は保たれる。
- **悪い点**: コピペは長い base64 文字列を扱うので、改行混入・途中切れによる貼り付けミスの
  余地がある(SSH 自動化の C を使えば回避できる)。
- **悪い点**: CLI 表面が増える(`tls request` の stdout モード、`tls sign -` の stdin モード、
  `tls enroll`)。`docs/mtls.md` の walkthrough も更新が要る。

## Alternatives Considered

### Alt 1: scp 現状維持

実装ゼロだが、dogfood で煩雑さと scp 依存が確認済み。却下(本 ADR の出発点)。

### Alt 2: SSH 全自動を唯一の手段にする

`tls enroll` だけ提供し B を作らない。最短手数だが、SSH を使わないユーザーに
**不自然な依存**を強制する。「数台の個人マシン」スケールでは SSH を前提にできない層が
いる。SSH はオプトイン上位 (C) に留め、土台は transport 非依存の B にした。却下。

### Alt 3: ACME / step-ca / SPIFFE/SPIRE などの enrollment プロトコル

自動更新・短命証明書・大規模配布まで賄える本格 PKI。だが kioku-mesh の規模(信頼
ネットワーク上の数台の個人マシン)に対して **明確に overkill**。常駐サービス・追加
インフラ・運用知識を要求し、ゼロ依存で試せる現在の性格と噛み合わない。却下。

## Migration

- 後方互換性影響なし。既存の `tls request` / `sign`(ファイル引数・`-o`)はそのまま動く。
  stdout / stdin モードは**追加**であって置換ではない。
- 既存の scp 手順を覚えたユーザーは引き続きそれを使える。コピペは新しいデフォルト導線。
- 本 ADR は決定の記録のみ。CLI 実装(`tls request` の stdout モード、`tls sign -` の
  stdin モード、`tls enroll`、`-o` 整備)は別 PR で着地する。

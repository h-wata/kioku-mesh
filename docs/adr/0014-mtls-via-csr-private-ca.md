# ADR 0014: mesh の mutual TLS を CSR ベースの private CA で提供する

- **Status**: Accepted
- **Date**: 2026-05-30
- **Supersedes**: なし
- **Related**: [[0001-zenoh-rocksdb-transport]], [[0006-hub-and-spoke-mesh-topology]], [[0010-zenoh-as-source-of-truth]], PR #138

## Context

kioku-mesh はデフォルトで **Zenoh のポート (`7447/tcp`) に到達できる相手を信頼する** —
すなわち **network admission**(Tailscale / WireGuard / firewall / 信頼された LAN)に
trust を委ねている。閉じた信頼ネットワークではこれで十分で、証明書はゼロで済む。

しかし network admission だけでは足りない場面がある:

- 共有 LAN(到達できる相手 = 信頼できる相手、が成り立たない)
- zero-trust posture を要求される環境
- 「誰が mesh に参加したか」を証明書で監査したい要求

このとき、transport 層で **相手の身元を暗号学的に検証**し、かつ **通信を暗号化**する
仕組みが要る。Zenoh の rustls スタックは mutual TLS をサポートしているので、
これに乗せられる。問題は **どういう trust model と鍵運用で mTLS を提供するか** で、
ここに複数の分岐があった。

## Decision

mTLS を **opt-in の追加 trust 軸**として提供する。network admission をデフォルトのまま
据え置き、必要な人だけが `--tls` で有効化する。具体的な設計判断は以下:

### 1. 一つの private CA + peer ごとの CSR(鍵は動かさない)

- CA は1つ。`ca.key` が全 peer 証明書に署名する。`ca.crt`(公開半分)を全 peer に配布。
- **秘密鍵は所有する peer 上で生成し、ホストから一切出ない**。peer は CSR(署名要求 =
  公開情報)を出し、CA がそれに署名し、署名済み証明書が戻る。
- ホスト間を移動するのは **非秘密のみ**(CSR・`peer.crt`・`ca.crt`)。2つの秘密
  (`ca.key` と各 `peer.key`)は決して移動しない。これは `ssh-copy-id` が既存の
  secure channel 上で公開鍵だけ押し込むのと同じ形。

### 2. loopback は平文のまま — 信頼境界はホスト

mTLS はネットワークを跨ぐリンクを守る。ローカル CLI / MCP クライアントから
**自分自身の** zenohd への hop は `tcp/127.0.0.1` で、ワイヤに出ない。したがって
`--tls` は **loopback endpoint を意図的に平文のまま残し**、cross-host (`tls/`)
リンクだけを暗号化する。**信頼境界はホスト**: マシンにローカルアクセスできる者は
その router と話せる / リモート peer は CA が署名した証明書を提示しなければならない。

### 3. その他の暗号・運用上の選択

- 鍵は **EC P-256**(小さく速く、Zenoh の rustls で完全サポート)。
- peer 証明書は **serverAuth と clientAuth 両方の EKU** を持つ。全 zenoh router は
  同じ identity で link を受けると同時に dial するため。
- **cross-host `udp/` endpoint は `init --tls` で拒否**する。TLS は UDP を包めないので、
  非認証リンクを黙って吐くのではなくエラーにする。
- peer 証明書は既定 825 日、CA は約 10 年。
- `verify_name_on_connect` を有効化し、証明書は **SAN**(IP / hostname)を持つ。
  peer をプロビジョンするときは「他の peer がそれに到達するために使う全アドレス」を
  `--san` で渡す(LAN IP と Tailscale IP の両方など)。
- `tls install` は署名済み証明書をローカルの `peer.key` と照合する。別 peer 向けに
  発行された(CA 署名は妥当だが鍵が違う)証明書を、後の zenohd handshake で失敗する
  前に **ここで弾く**。

CLI 表面は `tls init-ca` / `request` / `sign` / `install` / `info` と `init --tls`、
`doctor` の `tls_certs` チェック。証明書が存在しない限り `init --tls` は config を
吐かないので、zenohd が起動時に拒否する設定を生成できない。

## Consequences

- **良い点**: network admission しか要らない既存ユーザーは何も変わらない(ゼロ証明書の
  まま)。mTLS が要る層だけがコストを払う、漸進的な trust 強化。
- **良い点**: 秘密鍵がホストから出ない設計なので、CSR・証明書の配送経路が漏れても
  秘密は漏れない。配送手段(scp / Tailscale / USB / コピペ)を自由に選べる。
- **良い点**: loopback を平文に残すことで、ローカルの `save` / `search` / MCP クライアントが
  追加設定なしに動き続ける。「信頼境界 = ホスト」という説明可能なモデルになる。
- **悪い点**: CA 鍵が漏れると誰でも妥当な peer を発行できる。`ca.key` の保護がシステムの
  単一障害点。`ca.crt` を `init-ca --force` で作り直すと全 peer 証明書が一斉に無効になる。
- **悪い点**: 証明書の配送・更新という運用作業が増える(enrollment の煩雑さは
  [[0015-cert-enrollment-copy-paste-default]] で別途扱う)。
- **悪い点**: replication ブロックは依然 peer 間で byte-for-byte 一致が必要。`--tls` は
  transport / listen / connect セクションしか変えない。

## Alternatives Considered

### Alt 1: 常時 mTLS(network admission を廃し、証明書を必須化)

「到達できる = 信頼できる」の曖昧さを構造的に消せるが、kioku-mesh の主用途
(Tailscale / 信頼 LAN 上の数台の個人マシン)では過剰。全ユーザーに CA 構築と証明書
配送を強制すると、ゼロ依存で試せる現在のオンボーディングを壊す。opt-in に留めた。

### Alt 2: 鍵配布型 PKI(CA が鍵ペアを生成して秘密鍵ごと配る)

CSR の往復が要らず一見簡単だが、秘密鍵がネットワークを移動する時点で「鍵はホストから
出ない」という最大の安全特性を失う。配送経路が漏れたら即終わり。却下。

### Alt 3: loopback も含めて全リンクを TLS 化

一見一貫して見えるが、ローカル CLI / MCP クライアントにも証明書を要求することになり、
`save` / `search` 一発の体験に証明書管理が割り込む。ワイヤに出ない hop を暗号化する実益も
無い。loopback 平文 + 「信頼境界 = ホスト」の方が説明も実装も素直。却下。

### Alt 4: 外部 PKI / 公的 CA / mesh VPN の証明書に相乗り

Let's Encrypt 等の公的 CA は内部 IP / 私設 hostname 向けには使えない。Tailscale の
証明書に相乗りする手もあるが、特定 mesh VPN への依存を mesh 層に持ち込むことになる。
自前の小さな private CA が最も依存が少なく、どの transport(LAN / Tailscale / WireGuard)
でも同じ手順で動く。却下。

## Migration

- 後方互換性影響なし。`--tls` を付けない限り挙動は完全に従来通り(network admission)。
- 既存 mesh は何もしなくてよい。mTLS に移行したい人だけが `docs/mtls.md` の walkthrough に従う。
- on-disk スキーマ・env var・replication フォーマットは無変更。

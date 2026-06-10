# ADR-0020: skills-mesh — 同一 mesh 上の別 namespace で skill を共有する

- Status: Proposed
- Date: 2026-06-10
- Supersedes: なし
- Related: ADR-0001, ADR-0010, ADR-0014, ADR-0019

## Context

kioku-mesh は observation（コーディングエージェントの記憶）を Zenoh mesh で
複製する。一方、エージェントの能力を定義する **skill**（Claude Code の
SKILL.md のような指示ドキュメント）は、現状ホストごとの手動コピーで配布
しており、複数 PC / 少人数 team での共有手段が無い。

kioku-mesh の基盤 — Zenoh replication、tombstone、mTLS（ADR-0014）、
local index、MCP server — は observation 固有ではなくドキュメント一般に
汎用である。skill 共有を別アプリとして作ると、この基盤の二重実装になる。

ただし skill と observation はデータモデルが本質的に異なる。

| | observation | skill |
|---|---|---|
| 同一性 | UUID（匿名） | 名前（`name` で参照される） |
| 変更 | append-only・不変 | 更新される（改訂が前提） |
| 読み方 | 検索でヒットしたものを読む | 一覧から選び、本文を読み込んで**実行**する |
| 失敗時の影響 | 誤情報の参照 | エージェント挙動の乗っ取り（supply-chain） |

「実行されるドキュメント」である点が決定的な差で、observation と同じ
namespace / 同じ信頼前提に押し込むことはできない。

また、エージェントが skill を使う実際のパターンは「起動時に全 skill の
説明文（description）だけを一覧で読み、必要になった時に本文を読む」という
2 段階である（Claude Code の skills-catalog と同じ）。全 skill 本文を
常時配るのは無駄であり、discovery 用メタデータと本文は分離すべきである。

## Decision

skills-mesh を **kioku-mesh と同一 mesh 上の別 namespace** として実装する。
別アプリ化はしない（将来の切り出しは妨げない）。

### Key layout — 2 層 KV + immutable versioned body

```text
skill/{visibility}/{team_id?}/index/{name}
skill/{visibility}/{team_id?}/body/{name}/{version}
```

- **index entry**（軽量・discovery 用）: `name`, `description`, `latest_version`,
  `publisher`（mTLS cert subject 由来）, `updated_at`, `tags` を持つ小さな JSON。
  エージェントは session 開始時に index だけを一覧読みする。
- **body**（本文）: SKILL.md 相当の本文。`{name}/{version}` キーは **immutable** —
  一度 put した version は書き換えない。改訂は新 version の put と
  index entry の `latest_version` 更新で行う。
- 削除は ADR-0002 と同型の tombstone で表現する。

immutable versioned body により:

- 「読んだ skill が実行中に書き換わる」TOCTOU を排除できる
- version 固定（pin）が key 指定だけで実現できる
- 監査（いつ誰がどの内容を publish したか）が key 履歴に残る
- Zenoh の put / tombstone / replication 設計（ADR-0001/0002/0010）に
  そのまま乗り、新しい同期プリミティブを必要としない

### Visibility — 初期スコープは priv / team のみ

ADR-0019 の visibility tier をそのまま採用するが、**`pub` は当面封印**する。

```text
skill/priv/...  → Zenoh に載せない。local のみ（ADR-0019 の priv と同じ）
skill/team/{team_id}/... → 該当 team storage を持つ peer のみに複製
skill/pub/...   → 将来。署名・レビュー基盤ができるまで実装しない
```

理由: skill は実行されるため、不特定多数への配布は署名検証と取り込み
レビューの基盤なしには supply-chain 攻撃面になる。個人の複数 PC 間
（priv）と、相互に信頼済みの少人数 team（team + mTLS）に限定すれば、
信頼境界は「mesh に参加できる = cert を持つ」に一致し、既存の
ADR-0014 の境界で説明できる。

### 信頼境界と取り込み

- index entry の `publisher` は mTLS cert subject から server 側で
  解決する（self-claim させない）。
- mesh から取得した skill を**エージェントが自動で有効化することはしない**。
  取り込みは明示的な操作（CLI / MCP tool）とし、ユーザーが diff を
  確認して手元の skills ディレクトリに配置するフローを既定とする。
- 将来 `pub` を解禁する場合は、cert チェーンによる署名検証と
  ADR-0019 の言う Zenoh ACL を前提条件とする。

### 実装順序

ADR-0019（visibility-tiered replication）の namespace / per-scope storage
実装を前提とする。0019 の Phase B（visibility-aware write 分岐）完了後に
skills-mesh の index/body namespace を追加するのが最小差分になる。

## Consequences

- 良い点: replication・tombstone・mTLS・MCP server の既存基盤を再利用でき、
  別アプリの立ち上げコストと二重実装を避けられる。
- 良い点: 2 層 KV により「description 一覧は常時軽量、本文は遅延 fetch」
  というエージェントの実利用パターンに合う。
- 良い点: immutable versioned body は TOCTOU・監査・pin を key 設計だけで
  解決し、上書き競合の調停が不要になる。
- 良い点: priv/team 限定により、信頼境界が既存の mTLS 境界と一致する。
- 悪い点: ADR-0019 の実装完了が前提となり、単独では着手できない。
- 悪い点: index entry と body の整合（latest_version が存在しない body を
  指す等）は eventual consistency 下で一時的に壊れうる。reader は
  「index が指す version が無ければ既知の最新 version に fallback」する
  防御的読み取りが必要。
- 悪い点: 古い version の body が蓄積する。GC（ADR-0005/0008 と同様の
  retention）を skills 側にも設計する必要がある。
- 悪い点: `pub` 封印により、OSS 的な skill 配布のユースケースは当面
  対象外になる。

## Alternatives Considered

### Alt 1: 別アプリ（skills-mesh を独立プロダクトにする）

信頼境界・release cadence を完全分離できるが、Zenoh transport、mTLS
enrollment、local index、MCP server scaffolding をすべて再実装することに
なる。構想検証の段階でこのコストは正当化できない。namespace 分離を
保っておけば、将来パッケージを切り出す選択肢は残る。

### Alt 2: observation として skill を保存する（memory_type="skill"）

実装は最小だが、append-only・UUID 同一性のモデルに「名前で参照され
改訂される実行ドキュメント」が合わない。検索でしか発見できず、
version pin も latest 解決も payload 解析頼みになる。実行物と記憶を
同じ信頼境界に置くことになり却下。

### Alt 3: mutable body（skill/{vis}/{name} を上書き）

key 数は減るが、読んだ skill が実行中に変わる TOCTOU、上書き競合の
調停、監査不能が残る。実行されるドキュメントには不変 version が必要。

### Alt 4: 本文も index に同居させる単層 KV

実装は単純だが、session 開始時の一覧読みで全 skill 本文を転送する
ことになり、skill 数・サイズに対してスケールしない。discovery メタと
本文の分離は Claude Code 側の遅延ロードモデルとも一致するため 2 層を採る。

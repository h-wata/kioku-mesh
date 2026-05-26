# ADR 0013: "Tier 1" を公開アーキテクチャから外し、demo mode に格下げ

- **Status**: Accepted
- **Date**: 2026-05-26
- **Supersedes**: なし（README v0.3.0 の Tier 0 / 1 / 2 narrative #110 を実質的に上書き）
- **Related**: [[0001-zenoh-rocksdb-transport]], [[0010-zenoh-as-source-of-truth]]

## Context

v0.3.0 リリース時、README は次の 3 段 narrative で kioku-mesh のアーキテクチャを説明していた：

| Tier | What runs | Persistence |
|---|---|---|
| Tier 0 | SQLite のみ | local persistent |
| Tier 1 | in-process zenoh router (`mesh start`) | **ephemeral** cross-host |
| Tier 2 | zenohd + RocksDB | persistent cross-host |

設計上の意図は「ゼロ依存 → mesh お試し → 本番 mesh」の段階的なオンボーディング階段を提示することだった。

実運用で何度か触ってみたところ、3 つの噛み合わせの悪さが浮上した：

1. **Tier 0 → 1 で持続性が後退する**。Tier の番号は通常「機能が増える階段」を示唆するが、Tier 1 では cross-host の永続性が失われる（peer A が offline の間に peer B が書いた分は、Tier 1 ではどこにも保持されない）。「Tier を上げたら何かを失う」というのは概念モデルとして罠で、README を熟読しないと気付かない。
2. **Tier 1 の正味のユースケースが極めて狭い**。実質「zenohd を入れる前にお試しで mesh を動かしてみる」 + 「全 peer が常時オンラインの短期コラボ」の 2 用途に限られる。前者は demo として価値があるが、後者は実運用にはほぼ流れない（落ちたら取りこぼすメッシュを本番で使う動機がない）。
3. **第一級概念として並べる認知コストが、得られる説明力に見合わない**。3 段表を見せた結果、ユーザーは「Tier 1 と Tier 2 のどちらを選ぶべきか？」を判断する必要が生じる。だが実運用の答えは常に Tier 2 (zenohd) で、Tier 1 は単に「zenohd を入れる前のお試し手段」でしかない。第一級扱いは選択肢を 1 つ余計に増やしている。

## Decision

README の公開アーキテクチャから "Tier 1" を取り除き、**`Local` vs `Mesh` の 2 軸**に整理する：

| Mode | What runs | What you get |
|---|---|---|
| **Local** (default) | SQLite のみ | single-machine persistence |
| **Mesh** | zenohd + RocksDB | persistent multi-host mesh |

旧 Tier 1 (`mesh start` / `mesh join` の in-process zenoh router) は **「Try mesh without zenohd」の demo 注記** として README の `Architecture` セクション末尾に格下げ。「demo であって本番ではない」「cross-host replication は ephemeral」を文言として明示する。

**コード側は据え置き** — `mesh start` / `mesh join` サブコマンドは残し、CLI help から "Tier 1" 表現だけを外して "try-it / demo path" に書き換える。動作・引数・互換性は無変更。

過去の design notes (`docs/design/issue-112-tier1-fix-mesh-integration.md`) は履歴として残し、冒頭に本 ADR への参照を追記する。

## Consequences

- **良い点**: README の概念モデルが単調になる（Local → Mesh は「機能が増える」方向に揃う）。新規ユーザーが選ぶべき選択肢が 2 つに減り、判断負荷が下がる。
- **良い点**: "Tier 1 で本番運用しようとして取りこぼしに気付く" 罠を構造的に塞げる（demo 注記として明示）。
- **良い点**: `mesh start` のコードパスとテストは残るので、demo として動かしたい層には引き続き機能を提供できる。
- **悪い点**: v0.3.0 リリースノートで「Tier 1 を新規追加」を宣伝した直後の方針後退になる。CHANGELOG / 移行ノートで「動作は変えていない、命名と位置付けの整理のみ」を明示してリリース後の混乱を抑える。
- **悪い点**: 過去の Issue / PR / commit message では引き続き "Tier 1" 表現が残る。検索ヒット時にユーザーが本 ADR にたどり着けるよう、`docs/design/issue-112-tier1-fix-mesh-integration.md` の冒頭に注記を入れて橋渡しする。

## Alternatives Considered

### Alt 1: 命名のみ修正（`Tier 1 → Mesh (ephemeral)` / `Tier 2 → Mesh (persistent)`）

揮発性が名前に出るので「気付かないと困る」問題は解消するが、「実運用は Tier 2 一択なのに 3 つ並んでいる」という認知負荷の本体は残る。階段の単調性も回復しない（`Local → Mesh (ephemeral) → Mesh (persistent)` で中段が下段の劣化版に見える構造は同じ）。却下。

### Alt 2: コードごと完全削除

`mesh start` / `mesh join` を消し、mesh モードは zenohd 前提に統一する。コード量と試験対象は減るが、「zenohd を install せずに mesh の絵が見られる」体験を失う。新規ユーザーが zenohd の install で詰まったときに撤退する確率が上がるので、demo path としての価値を捨てるのは早すぎる。却下。

### Alt 3: 現状維持

ユーザー体験の劣化（罠 / 認知負荷）を放置するコストの方が、README 書き換えコストよりも明確に大きい。却下。

## Migration

- 後方互換性影響なし（CLI 引数・on-disk スキーマ・env var ともに無変更）
- 旧 README を見て "Tier 1" を覚えたユーザーは `mesh start` / `mesh join` をそのまま使い続けられる
- リリースは次の minor (v0.4.0 想定) に同梱予定。`### Changed` 単独のドキュメンテーション変更なので bug fix release には載せない

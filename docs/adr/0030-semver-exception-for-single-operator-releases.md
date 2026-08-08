# ADR-0030: Semver exception for single-operator releases

- Status: Accepted
- Date: 2026-08-09
- Supersedes: ADR-0029 (v1.0 scope 決定事項の semver 条項のみ。他の決定事項
  [v1.0 に含む/含まない範囲、deprecation 運用、conflicting_latest cleanup] は
  維持される)
- Related: ADR-0029

## Context

ADR-0029 は v1.0 scope の一部として、「v1.0.0 以降、公開 CLI / MCP / Python API と
on-disk schema は Semantic Versioning に従い、破壊的変更は semver-major bump か
明示的な移行パスを要求する」という契約を定めた（README / CHANGELOG.md 冒頭に
反映済み）。

2026-08-09 にリリースした v1.1.0 は、この契約上は major bump に相当する破壊的
変更を含む:

- MCP `save_observation` と CLI `kioku-mesh save` で `subject` / `summary` を
  必須化した。省略時（または `-` / `N/A` / `TBD` 等のプレースホルダ）は
  エラーになり、既存の呼び出し元は修正なしに動かなくなる。
- `agent_family` の解決順を変更した（`KIOKU_MESH_*` → ランチャ検出 →
  `unknown`+警告。旧 `MESH_MEM_*` は読まない）。

ユーザー判断で、この変更は v2.0.0 ではなく v1.1.0（minor）としてリリースされた。
理由は、このリポジトリの利用者が本人環境のみであり、major bump が本来担う
「下流の利用者に移行を促す」という機能が働かないため。CHANGELOG.md の
`[1.1.0]` 節の冒頭には後方非互換である旨と "Upgrade notes for v1.1" が
既に記載されている。

ADR-0029 の semver 条項は「破壊的変更は常に major」という前提で書かれており、
単一運用者という条件下でこの前提を緩めてよい場合の扱いを規定していない。この
ままでは、今回のような判断のたびに ADR の文言と実際の運用が乖離する。

## Decision

単一運用者である間に限り、破壊的変更を minor リリースに含めることを認める。
ADR-0029 の semver 条項（「破壊的変更は常に semver-major bump か明示的な移行
パスを要求する」）を、この例外の範囲でのみ supersede する。ADR-0029 のそれ
以外の決定事項（v1.0 に含む/含まない機能範囲、deprecation 運用手順、
conflicting_latest の手動クリーンアップ方針）はそのまま有効。

例外を適用するには、次をすべて満たすこと:

1. CHANGELOG.md の該当バージョン節の**冒頭**に、破壊的変更を含む旨を明記する。
2. 同節に upgrade notes（既存の呼び出しをどう直すか）を書く。
3. GitHub Release のリリースノートでも、破壊的変更を**最上部**に置く。

### 失効条件

この例外はリポジトリの利用者が単一運用者である間のみ有効。第三者の利用者が
付いた時点で例外は失効し、以後は ADR-0029 の通常の semver 契約（破壊的変更は
常に major bump）に戻る。

### 具体例: v1.1.0

- 何が破壊的だったか: `save_observation` (MCP) / `kioku-mesh save` (CLI) の
  `subject` / `summary` 必須化、`agent_family` 解決順の変更。
- なぜ minor で出したか: 利用者が本人環境のみで、major bump の「移行を促す」
  効果が働かないため。
- CHANGELOG での記載: `[1.1.0]` 節冒頭の blockquote に
  「このリリースは後方非互換の変更を含む (minor bump だが安全な更新では
  ない)」と明記し、"Upgrade notes for v1.1" を同節に含めた。

この ADR は、破壊的変更を minor に含めてよい条件のみを扱う。リリース頻度や
ブランチ戦略など、他の運用ルールには踏み込まない。

## Consequences

- 良い点: 単一運用者という実態に対して、実際に運用してきたやり方（v1.1.0）を
  ADR の文言に一致させられる。次に同種の判断をするときに、ADR-0029 の
  文言とその場の判断が食い違って見えることがなくなる。
- 良い点: 失効条件を明記したことで、第三者の利用者が付いた後にこの例外が
  惰性で使われ続けるリスクを抑えられる。
- 悪い点: minor バージョン番号だけを見て「安全に upgrade できる」と判断する
  読み手（将来利用者を含む）には、CHANGELOG を確認しない限り破壊的変更が
  伝わらない。CHANGELOG 冒頭記載と Release note 最上部記載の二重化は、この
  リスクを緩和するための最低条件であり省略できない。

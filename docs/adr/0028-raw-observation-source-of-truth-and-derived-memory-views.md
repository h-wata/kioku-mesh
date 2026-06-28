# ADR-0028: Raw ObservationをSource of Truthとし、派生メモリビューを再構築可能に保つ

- Status: Accepted（kioku-mesh のメモリモデルと MCP の位置づけを定義する基盤 ADR）
- Date: 2026-06-27
- Supersedes: なし（ADR-0002 / 0021 / 0025 / 0026 / 0027 が前提にしてきた設計原則を明文化）
- Related: ADR-0002, ADR-0007, ADR-0019, ADR-0021, ADR-0025, ADR-0026, ADR-0027,
  arXiv:2606.24775 "Are We Ready For An Agent-Native Memory System?"

## Context

kioku-mesh は単なる DB ではない。MCP（Model Context Protocol）を通じて
AI Agent の **保存・想起・置換** を制御する **Agent Memory Protocol** である。
これまでの ADR は個別の機構（tombstone, supersede, FTS, importance ランキング,
incremental rebuild）を一つずつ決めてきたが、それらが共有している
**根底のメモリモデル** は暗黙のままだった。本 ADR はそれを明文化し、今後の
追加機能（embedding / graph / summary / recall_context など）が従うべき
原則を固定する。

論文 arXiv:2606.24775 が指摘するとおり、長期メモリの失敗は「忘れること」
だけではない。**古い decision / config / preference が現在も有効であるかの
ように検索されると、Agent が過去の前提を現在の事実として扱う。** これを本 ADR
では **hallucinations of the past（過去の幻覚）** と呼ぶ。append-only な
ストアでは何も消えないがゆえに、この失敗モードがむしろ顕在化しやすい。

ここから 2 つの区別が要る:

- **Historical truth（履歴的真実）**: 「当時こう決めた / こう設定した」という
  記録。Raw Observation は永続的にこれを保持する。
- **Current effective context（現在有効な文脈）**: 「いま何が有効か」。通常検索
  が Agent に返すべきはこちら。Raw Observation はすべて履歴として残るが、
  常に current effective context であるとは限らない。

つまり **Source of Truth として全部を保持すること** と、**通常検索で何を出すか**
は別レイヤーで制御する。前者は Raw Observation、後者は derived view が担う。

## Decision

### 1. Raw Observation を唯一の Source of Truth とする

`Observation`（Zenoh/RocksDB 上の append-only payload）を **唯一の永続的
Source of Truth** とする。以下はすべて **再構築可能な derived view** であり、
Source of Truth にはしない:

- SQLite read index（per-host sidecar）
- FTS5 / BM25 検索インデックス
- （将来）Embedding index
- （将来）Graph view
- （将来）Summary / consolidation
- Recall cache

derived view は Raw Observation から **いつでも rebuild できる** ことを不変条件
とする（ADR-0025 の incremental rebuild はこの不変条件の運用面）。新しい view を
足すときは「Raw Observation から再構築可能か」を満たすことが受け入れ条件になる。

### 2. MCP tool は CRUD ではなく Agent behavior protocol

MCP tool を DB の CRUD としてではなく、**Agent の振る舞いを規定する高レベル
プロトコル** として設計する:

- `save_observation` = **durable context capture**（揮発しがちな文脈を永続化する）
- `search_memory` = **recall primitive**（想起の最小単位）
- `get_memory` = **context expansion**（一点を起点に文脈を広げる）
- 将来の `recall_context` = 単なる検索 API ではなく、**Agent の「思い出し方」を
  制御する高レベル MCP layer**

`recall_context` は内部で次を組み合わせる（個々はすでに存在する部品）:

- `memory_type` filter
- `source_files` / `references` filter
- FTS / BM25
- time window
- importance（ADR-0027）
- superseded / tombstoned / shadowed の除外
- （任意）embedding / graph view

### 3. delete・supersede・tombstone・shadow を明確に分ける

4 つの状態は **別の意味** を持ち、混同しない:

- **delete**: 「**存在させたくない記憶**」に使う明示的な論理削除。秘密の誤保存、
  ダミーデータなど。
- **supersede**: 「**当時は正しかったが現在の前提ではない記憶**」を現在文脈から
  沈める。履歴としては残す（ADR-0021 / 0026）。
- **tombstone**: 明示的な論理削除の実体（ADR-0002 の existence-based tombstone）。
- **shadow**: **Source of Truth に存在しない row を local index 検索から隠す
  reconciliation 状態**。物理削除の伝搬や、不完全な rebuild/scan の結果として
  生じる。

運用ルール:

- **stale な decision / config / preference は delete ではなく supersede で沈める。**
  delete は「存在させたくない記憶」、supersede は「当時は正しかったが現在の前提
  ではない記憶」。この使い分けが hallucinations of the past への主防御になる。

### 4. shadow は欠陥ではなく整合性回復機構として扱う（まず可視化）

shadow は悪い設計ではない。**他 PC で物理削除された記憶や、検索性能調査用の
ダミーデータを、他 PC の検索からも消す** ために必要な機構である。Source of Truth
に無い row を local index 側で隠すことで、mesh 全体の整合性を回復する。

ただし弱点がある: **意図した物理削除の伝搬** と、**一時的な rebuild/scan の
不完全** が、同じ shadow 状態に見えてしまう可能性がある。したがって改善の方向は
**shadow の廃止ではなく、status/doctor での可視化**:

- `status` で shadowed count を出す
- `doctor` で shadow の意味を説明する
- shadowed rows を inspect できるようにする
- `rebuild` で何件 shadow したか分かるようにする
- `suspected_shadow → confirmed_shadow` の二段階化は、**誤 shadow が実運用で
  問題になったら** 検討する（今は入れない）

可視化を先に入れ、二段階化はデータが必要性を示してから判断する。

### 5. Graph / Embedding / Summary は Source of Truth にしない

これらは必要になった時点で **derived view として追加** し、Raw Observation から
再構築可能に保つ。Graph DB / Embedding DB / Summary が Raw Observation の
**代替** になることはない。

導入の前提として、**kioku-mesh の実ワークロード向け評価セット** で効果を測って
から入れる（下記「評価」参照）。一般ベンチで良いから入れる、ではなく、
本プロジェクトの想起タスクで効くことを確認してから入れる。

### 6. save quality を一級の関心事にする

何を保存するかが想起品質を決める。指針:

**保存すべきもの:**

- decision
- config change
- bug root cause
- reusable pattern
- non-obvious gotcha
- user preference / approval / rejection
- PR / ADR / commit に残らない **WHY**

**保存すべきでないもの:**

- 一時的な進捗
- generic な "tests pass"
- PR / Issue のライフサイクル tick
- 既存 System of Record の単なる再記述
- ダミーデータ
- secret / token / credential

## Consequences

- **良い点**: 今後の機能追加（embedding / graph / summary / recall_context）の
  受け入れ条件が一本化される —「Raw Observation から再構築可能な derived view か」。
- **良い点**: hallucinations of the past を設計レベルの一級リスクとして扱い、
  delete と supersede の使い分けという具体的防御に落ちる。
- **良い点**: shadow を「欠陥」ではなく整合性回復機構と位置づけ、改善の方向を
  廃止ではなく可視化に固定できる。
- **中立**: 本 ADR 自体はコードを変えない原則の明文化。具体実装は follow-up と
  個別 ADR（0026/0027 と今後）に委ねる。
- **悪い点（受容する）**: Raw Observation を常に保持するため、ストレージは
  append-only で増え続ける。物理回収は tombstone/shadow 済み行の purge に限定し、
  live obs は年齢/importance で evict しない（ADR-0026/0027 と一致）。
- Implemented: Phase 1-6 merged in #242-#247 (2026-06-28)

## Non-goals

- Graph DB を Source of Truth にしない。
- Embedding DB を Source of Truth にしない。
- Summary を Raw Observation の代替にしない。
- live observation を自動 evict しない。
- shadow を廃止しない。
- MCP tools を単なる DB CRUD として扱わない。
- 全メモリを LLM で定期 consolidation することを必須にしない。

## Follow-ups

本 ADR から派生する具体タスク（個別 issue / ADR 化する）:

- save lint（保存品質の警告）
- secret scan（save 時の secret/token 検出）
- `.kiokuignore`
- local ↔ mesh の export / import
- promote-local-to-mesh（local スコープを mesh に昇格）
- unknown `memory_type` の raw value 保持
- memory quality benchmark

## 評価（Graph / Embedding 導入前に測る）

Graph や Embedding を入れる前に、kioku-mesh ワークロード向けの評価セットで効果を
測る。評価すべき項目:

- stale setting replacement（古い設定が新しい設定に置き換わって想起されるか）
- bug root-cause recall
- source-file scoped recall
- decision / config recall
- superseded / tombstoned / shadowed の visibility
- rebuild consistency
- save noise rejection（ノイズ保存の抑制）
- long-horizon recall（長期スパンの想起）

## 結論

kioku-mesh は、Raw Observation を **永続的な Source of Truth** として保持し、
MCP を通じて Agent の **保存・想起・置換** を制御する。検索・Graph・Embedding・
Summary は、workload に応じて **再構築可能な derived view** として追加する。
長期メモリの主なリスクは忘却だけでなく、**古い記憶を現在の事実として扱う
hallucinations of the past** である。そのため **delete より supersede を優先** し、
**shadow は Source of Truth と local index の整合性回復** として扱う。

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

---

## 補遺: backfill 修復コピーと raw/live/effective 集計規約 (SQUAD-224)

Status: Implemented
Date: 2026-08-16
Related: SQUAD-222-consult（`worker4_report.yaml` answers.q4_adr）, TASK-360, TASK-361, TASK-367

### Context

2026-08-15、`kioku-mesh backfill-metadata --apply` が subject/summary 欠落
observation を修復した結果（330 組 660 行、当時の `deleted_at IS NULL` 件数
1517 件の 43.5%）を「重複書き込みバグ」と誤検知し、実在しないバグの調査タスクが
起票される手戻りが発生した。原因は本 ADR の「1. Raw Observation を唯一の
Source of Truth とする」で示した raw/derived の区別が、backfill の生成物と
「件数」の集計規約にまで具体化されていなかったことにある。ここに 4 点を補う。

### Decision

**(a) backfill-metadata は append-only の修復コピーを正規動作とする**

`backfill-metadata --apply`（実装: `src/kioku_mesh/__main__.py`
`_cmd_backfill_metadata`, `src/kioku_mesh/memory/metadata.py`）は元 row を
一切書き換えない。ADR-0002 の Observation immutable 制約と本 ADR の
append-only 原則により、同一 `created_at` / `content` を持つ row が増えて
見えても、それは**新しい `observation_id` + `supersedes=[<元の id>]` を持つ
修復コピーの append**であり、二重書き込みではない。ADR-0021 の
existence-based supersede filter が元 row を通常検索から沈める
（`obs_index.superseded_by` に元 row 側から逆参照が立つ）。

**(b) 「件数」を報告するときは raw / live / effective のいずれかを明記する**

- **raw**: `deleted_at IS NULL` のみで数えた、監査用の全 row 数。backfill
  コピーとその修復元の両方を含む。
- **live**: raw から、supersede コピーに置き換えられた行のうち **その
  supersede コピー自身が live（未 tombstone・未 shadow・未失効）である**
  行だけを除いた、supersedes チェーンで「現在の代表」である row 数。
  `superseded_by IS NOT NULL` を単純に除外するのではない
  （existence-based supersede filter、ADR-0021 / `local_index.py:669-687`）。
  supersede コピー自身が tombstone・shadow・失効している場合、元 row は
  隠されずに live のまま残る。
- **effective**: live にさらに `project` 等の呼び出し固有フィルタを重ねた、
  「実際にある検索呼び出しが返す」件数。

重複検出（同一 content が何件あるか等）は、既定で **live または effective**
の logical view を対象にする。raw に対して重複を数えると、supersede コピーの
存在そのものを「重複」と誤認する（TASK-361 の 132/77 誤読はこのパターン）。

**(c) `deleted_at IS NULL` 単独は defect 判定に使わない**

`deleted_at IS NULL` は raw inventory（監査・整合性チェック用の母数）であり、
それ単独の増減や絶対値を「バグの証拠」として扱わない。supersede は
tombstone と異なり `deleted_at` を変更しないため、backfill・rename・
project 移行などの正規の supersede 運用はすべて raw の見かけ上の件数を
押し上げる。バグかどうかの判定は必ず `superseded_by` / `supersedes` を
参照した logical view（live/effective）で行う。

**(d) report には使用した SQL・フィルタ・snapshot 時点を残す**

生データ件数や集計を根拠にした report は、使った SQL（もしくは同等の
フィルタ条件の文章化）と、対象にした snapshot／DB の取得時点を残す。
これにより後続 task が同じ集計を再現・検証でき、時点不整合（ある時点の
実測を別時点の現行障害として扱う誤り、TASK-360/PR #285 のケース）を
第三者が machine-checkable に検出できる。

### Analysis query template

対象テーブルは `obs_index`（`src/kioku_mesh/memory/local_index.py` の
`_ensure_schema` 参照）。列名は `deleted_at` / `shadowed_at` /
`superseded_by` / `project`。

```sql
-- raw: 監査用。tombstone されていない全 row（backfill コピーとその修復元を含む）
SELECT COUNT(*) FROM obs_index WHERE deleted_at IS NULL;

-- live: LocalIndex.search の既定 (include_superseded=False, include_expired=False)
-- と同じ existence-based supersede filter (local_index.py:669-687)。単純な
-- `superseded_by IS NULL` ではなく、supersede コピー自身が live（未 tombstone・
-- 未 shadow・未失効）でない限り元 row を隠さない。:now は snapshot 取得時点の
-- UTC ISO8601（例 '2026-08-16T06:41:42.000Z'）。
SELECT COUNT(*) FROM obs_index
WHERE deleted_at IS NULL AND shadowed_at IS NULL
  AND (expires_at IS NULL OR expires_at = '' OR expires_at > :now)
  AND (superseded_by IS NULL OR superseded_by NOT IN (
    SELECT observation_id FROM obs_index
    WHERE deleted_at IS NULL AND shadowed_at IS NULL
      AND (expires_at IS NULL OR expires_at = '' OR expires_at > :now)
  ));

-- effective: live にさらに検索呼び出し固有のフィルタ（例: project スコープ）を
-- かけた「実際に search_memory 等が返す」件数。shadowed_at / expires_at は
-- live の定義に既に含まれるため、ここでは project のみ追加する。
SELECT COUNT(*) FROM obs_index
WHERE deleted_at IS NULL AND shadowed_at IS NULL
  AND (expires_at IS NULL OR expires_at = '' OR expires_at > :now)
  AND (superseded_by IS NULL OR superseded_by NOT IN (
    SELECT observation_id FROM obs_index
    WHERE deleted_at IS NULL AND shadowed_at IS NULL
      AND (expires_at IS NULL OR expires_at = '' OR expires_at > :now)
  ))
  AND project = 'kioku-mesh';
```

### 実測 (2026-08-16 15:41 JST snapshot, TASK-377 で再検証・更新)

本番 local index sidecar (`/home/gisen/.local/share/kioku-mesh/index.db`) を
`/home/gisen/work-tmp/adr-0028-verify/index_copy_20260816_154135.db` へ
コピーし（SELECT のみ実行、コピー元は未変更）、上記 SQL と `LocalIndex.search`
の直接呼び出し（`now_iso` を snapshot 取得時点に固定）の両方を実行して
一致を確認した。

| 区分 | SQL / 呼び出し | 件数 |
|---|---|---:|
| raw | `deleted_at IS NULL` | 1538 |
| （参考・誤り）単純な `superseded_by IS NULL` | raw AND `superseded_by IS NULL` | 1146 |
| live | 本補遺の existence-based SQL | 1147 |
| live（`LocalIndex.search()` 直接呼び出し、既定引数） | — | 1147（SQL と一致） |
| effective (`project='kioku-mesh'`) | live AND project | 273 |
| effective（`LocalIndex.search(project='kioku-mesh')` 直接呼び出し） | — | 273（SQL と一致） |
| supersede 済み（`superseded_by IS NOT NULL`、単純カウント） | `deleted_at IS NULL AND superseded_by IS NOT NULL` | 392 |
| supersede により実際に隠れた行（raw − existence-based live） | 1538 − 1147 | 391 |

**単純フィルタとの差異（1146 vs 1147、392 vs 391）**: 該当する 1 件
（`observation_id=01677efa58e049b4bfff85491fa8f103`）は `superseded_by` が
指す supersede コピー自身が既に `deleted_at` 付きで tombstone されている
ケースだった。単純な `superseded_by IS NOT NULL` はこの元 row も
「supersede 済み」として隠すが、existence-based filter は supersede コピー
自身が live でない（tombstone 済み）限り元 row を隠さないため、この 1 件は
live のまま残る。単純フィルタと existence-based filter は一般に一致しない
ことを示す実例であり、PR310-B1 で指摘された 1137 vs 1138 の食い違いと
同種の事象が本 snapshot でも再現した（絶対値は書き込みが進んだため異なる）。

**期待との差異、および明記すべき点**: TASK-361 の起票文は「全 live 1517 件」と
書いていたが、その 1517 は本補遺の定義でいう **raw**（`deleted_at IS NULL`
のみ）の値であり、**live**（existence-based supersede filter 適用後）では
ない。本 snapshot（2026-08-16 15:41 JST）の raw 実測 1538 もほぼ整合する
（起票からの経過時間で新規保存が増えた分の差）。一方、本補遺の定義に基づく
実際の live は 1147 で raw より 391 少ない——この 391 が
まさに backfill 修復コピーによって沈められた元 row の数である。つまり
「live ≒ raw に近い値のはず」という素朴な期待は成立せず、**raw と live の差
（391）こそが supersede の正常な効果**であり、バグの兆候ではない。この
raw/live の呼び分けの欠如自体が (b) で示した誤読の再現であり、本補遺が
解消しようとしている問題と一致する。

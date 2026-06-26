# ADR-0021: 検索品質 — FTS5 (trigram) 導入と supersedes-aware search

- Status: Accepted
- Date: 2026-06-10
- Supersedes: なし（ADR-0007 の「今後: FTS5 は要件が見えたら段階導入」を具体化）
- Related: ADR-0002, ADR-0007, ADR-0010, ADR-0011, Issue #167

## Context

検索 read-path は ADR-0007 で SQLite local index sidecar に切り替わったが、
全文検索は `LOWER(payload_json) LIKE '%query%'`（`local_index.py` の
`search`）のままで、ADR-0007 自身が「FTS5 は要件が見えたら段階導入」と punt
していた。observation の蓄積が進み、保存（write）よりも **想起の精度
（recall quality）** が価値の上限になりつつある。現状の制約は 3 つ。

1. **検索対象が `payload_json` 全体**: content / subject / summary だけで
   なく identity フィールド（pc_id, session_id 等の UUID hex）にも部分一致
   するため、短いクエリで無関係な false positive が出る。コード内コメント
   でも「PoC として許容」と明記されている既知の妥協。
2. **ランキングが存在しない**: 結果順は常に `created_at DESC`。関連度の
   高い古い decision より、無関係だが新しい note が上に来る。複数語クエリの
   AND 検索もフレーズ検索もできない（単一部分文字列のみ）。
3. **`supersedes` フィールドが検索で無視される**: `Observation.supersedes`
   は model / CLI (`--supersedes`) / MCP tool 引数として既に存在するが、
   read 側はこれを一切使っていない。置換済みの古い decision が新しい
   decision と同列に検索結果へ並び、エージェントが古い方を読んで誤った
   前提で作業するリスクがある。append-only モデル（ADR-0002）では「更新」
   が新 ID での再保存になるため、運用が続くほど superseded entry の比率は
   構造的に増え続ける。

また kioku-mesh のチャットとコンテンツは日本語が多く、全文検索の改善が
**日本語で退化しない**ことが制約になる。LIKE 部分一致は言語非依存に動くが、
FTS5 のデフォルトトークナイザ（unicode61）は空白区切りを前提とし、日本語の
語境界を切れない。

## Decision

local index に 2 つの拡張を入れる。いずれも SQLite sidecar 内で完結し、
Zenoh payload / key layout / replication には一切手を入れない（ADR-0010 の
「index は派生キャッシュ」の範囲内）。

### A. FTS5 (trigram tokenizer) による全文検索

- `obs_index` と並走する FTS5 仮想テーブルを追加する:

  ```sql
  CREATE VIRTUAL TABLE obs_fts USING fts5(
    observation_id UNINDEXED,
    content, subject, summary, tags, project,
    tokenize = 'trigram'
  );
  ```

  `obs_index` の PK が TEXT（UUID hex）で FTS5 external-content の INTEGER
  rowid 前提に合わないため、独立テーブルとして `upsert` / `mark_deleted` /
  `physical_delete` / `rebuild_from_zenoh` から **lockstep で同期**する
  （ADR-0007 Phase 2 の dual-write と同じ規律）。
- インデックス対象を content / subject / summary / tags / project の
  **意味のあるフィールドに限定**し、identity フィールドへの偶発一致
  （上記制約 1）を構造的に解消する。
- トークナイザは **trigram** を採用する。言語非依存に 3-gram で索引する
  ため、日本語・英語とも従来の部分一致と同等の再現率を保ちながら、
  複数語 AND・フレーズ・bm25 ランキングが使えるようになる。
- `query` 指定時の結果順は `bm25(obs_fts)` を一次キー、`created_at DESC`
  をタイブレークとする。`query` なしの検索は従来通り `created_at DESC`
  （挙動不変）。
- **フォールバック 3 段**:
  1. trigram は 3 文字未満のクエリにマッチできないため、クエリが
     3 文字未満（漢字 2 文字の日本語語彙は普通に存在する）の場合は
     従来の LIKE 経路に自動フォールバックする。
  2. 実行環境の SQLite が FTS5 / trigram 非対応（trigram は SQLite 3.34+）
     の場合は、テーブル作成を skip して LIKE 経路で動き続ける。起動時に
     1 度だけ capability check を行い、`doctor` で確認可能にする。
  3. `MESH_MEM_DISABLE_INDEX=1` の Zenoh full-scan 経路（ADR-0007）は
     無変更。FTS5 はあくまで index 有効時の最適化。
- スキーマ移行は既存の forward-only パターン（`_ensure_schema` +
  `SCHEMA_VERSION` bump）に乗せ、移行時に `obs_index` から `obs_fts` を
  一括 backfill する。

### B. supersedes-aware search（非表示 + チェーン辿り）

- `obs_index` に `superseded_by TEXT` 列を追加する。obs の `upsert` 時に
  payload の `supersedes` リストを読み、参照先の行へ `superseded_by =
  <new observation_id>` を書く（逆向きエッジの実体化）。エッジ情報は
  Zenoh payload 内の `supersedes` に既に永続化されているため、この列は
  rebuild で完全に再構成できる**派生状態**であり、ADR-0010 / ADR-0011 の
  reconcile 設計と矛盾しない。
- **デフォルトの search から superseded 行を除外**する。ただし tombstone /
  shadow と異なり行は live のまま残し、`include_superseded=True`（MCP /
  CLI / `LocalIndex.search` に追加）で表示できる。
- 非表示の判定は **existence-based**（ADR-0002 と同型）にする: 「自分を
  supersede した observation が live（tombstone も shadow もされていない）
  で index に存在するときのみ隠す」。superseding 側が後から削除された
  場合、古い entry は自動的に検索結果へ復帰する。フラグの真偽ではなく
  参照先の存在で決まるため、eventual consistency 下で順序が前後しても
  最終的に正しい状態に収束する。
- `get_memory` の出力に置換チェーンを足す: `superseded_by: <id>`（より
  新しい版への前方リンク）と既存の `supersedes`（後方リンク）を併記し、
  エージェントが古い entry に到達しても 1 hop で最新版へ辿れるようにする。
- 状態の優先順位は **tombstoned > shadowed > superseded > live**。
  superseded は論理的な格下げであって削除ではないため、GC / retention の
  対象にはしない（将来の蒸留・consolidation 機能の入力として残す価値が
  ある）。

### 対象外（このADRで決めないこと）

- 埋め込みベクトルによる意味検索（sqlite-vec 等）。FTS5 と直交する別レイヤ
  として、必要になった時点で別 ADR で扱う。
- `importance` や参照頻度によるランキング補正。bm25 の実効を見てから判断。
- Zenoh fallback 経路（`_search_via_zenoh`）への supersedes フィルタ追加。
  fallback は縮退運転であり、機能差は許容する。

## Consequences

- **良い点**: 検索対象が意味フィールドに限定され、UUID への偶発部分一致
  という既知の false positive 源が消える。
- **良い点**: trigram により日本語の再現率を落とさずに、複数語 AND・
  フレーズ・bm25 ランキングが手に入る。trigram インデックスは LIKE/GLOB
  クエリの高速化にも転用できるため、フォールバック経路の性能も改善する。
- **良い点**: 置換済み decision がデフォルトで沈み、エージェントが古い
  前提を拾う事故が構造的に減る。`supersedes` という既存フィールドが
  初めて read 側の価値を持つ。
- **良い点**: 全変更が SQLite sidecar 内に閉じ、Zenoh payload / key /
  replication は不変。旧バージョンの peer と混在しても wire 上は何も
  変わらず、rolling upgrade の問題（ADR-0012 の轍）を踏まない。
- **悪い点**: trigram インデックスはテキスト本体の数倍に膨らみうる。
  index.db のサイズ増加と、upsert ごとの FTS 同期書き込みコストを払う。
  WAL checkpoint 周期（ADR-0008）への影響は計測して確認する。
- **悪い点**: `obs_index` と `obs_fts` の二重管理が増える。lockstep 同期の
  漏れは「検索に出ない／消えない」という静かな壊れ方をするため、
  rebuild が常に full backfill することを回復経路として保証する。
- **悪い点**: 3 文字未満クエリの LIKE フォールバックは挙動の二重性を生む。
  境界（2 文字クエリと 3 文字クエリでランキング有無が変わる）は
  ドキュメントで明示する。
- **悪い点**: superseded 非表示は「ユーザーが意図的に古い版を探したい」
  ケースで一手間（`include_superseded=True`）を要求する。また supersede
  エッジが replication 遅延で未着の間は古い entry が見え続ける（eventual
  consistency の許容範囲とする）。

## Alternatives Considered

### Alt 1: unicode61 トークナイザ + 日本語クエリは LIKE フォールバック

インデックスサイズは小さいが、日本語クエリが常に LIKE 経路に落ちるため
「日本語ではランキングも複数語 AND も効かない」という言語間の機能差が
恒久化する。kioku-mesh の主要ユーザーのチャット言語が日本語である以上、
主言語側が劣化する選択は本末転倒。却下。

### Alt 2: 古い observation の payload に superseded_by を書き戻す

Observation は immutable（ADR-0002、models.py の不変条件）であり、既存
payload の書き換えは append-only モデルを壊す。逆向きエッジは index 側の
派生状態として実体化すれば十分。却下。

### Alt 3: 外部検索エンジン（tantivy / meilisearch 等）

ランキング品質は上がるが、常駐プロセスまたは native 依存が増え、
「ゼロ依存で local モードが動く」という性格（ADR-0016 で守った導線）と
噛み合わない。SQLite 内蔵の FTS5 で要件を満たせる。却下。

### Alt 4: superseded をランキング降格のみで扱う（非表示にしない）

情報が常に見える安全側の設計だが、limit 件数の枠を古い版が消費し続け、
「最新の決定だけ欲しい」という支配的ユースケースで雑音が残る。
existence-based の非表示 + opt-in 表示の方が、tombstone で確立済みの
メンタルモデルとも一致する。却下。

## Migration

- 後方互換性影響なし。`SCHEMA_VERSION` bump と forward-only migration で
  `obs_fts` 作成 + backfill、`superseded_by` 列追加を行う。既存 index.db は
  初回起動時に自動移行される。
- 旧バージョンへのロールバック時は未知テーブル / 列が無視されるだけで
  動作する（SQLite は additive change に対して双方向に寛容）。
- Zenoh 側スキーマ・env var・CLI 既存引数は無変更。`include_superseded` は
  追加引数であり、未指定時の挙動変化は「superseded 行が沈む」のみ。
- 実装順序: Issue #167（store.py 分割)や ADR-0019 とは独立に着手できる。
  ただし ADR-0019 実装後は visibility scope と search filter の合成が必要に
  なるため、先に入れる場合は `LocalIndex.search` の filter 合成部を
  visibility 追加を見込んだ形に保つ。

---

## Follow-up: search_mode パラメータ追加 (TASK-313 / Issue #218)

Status: Implemented
Date: 2026-06-26

### Context

TASK-304/#211 で複数語クエリを明示 AND 化した結果、precision は上がったが、
Dispatcher/agent が自然文に近い長いクエリを投げると 0 件になり recall を落とす。
ADR-0021 は recall quality が価値の上限とするため、AND 以外の search mode が必要。

### Decision

search_memory / CLI / backend に search_mode='and'|'or'|'and_or' を追加する。
default は後方互換のため 'and'。
'or' は query terms のいずれかに一致すれば返す。
'and_or' は AND results を先頭にし、不足分を OR results で dedupe 補完する recall mode。
raw FTS5 query syntax は public API として公開しない。
base filter(deleted/shadowed/superseded/project/identity/visibility)は全モードで AND を維持する。

### Consequences

既存呼び出しは不変。recall 重視の agent は search_mode='and_or' を明示できる。
実装は search pipeline の引数伝搬と query predicate/ranking の複雑化を伴う。
OR mode では result set が増えるため bm25/tie-break と limit の設計が検索品質を左右する。

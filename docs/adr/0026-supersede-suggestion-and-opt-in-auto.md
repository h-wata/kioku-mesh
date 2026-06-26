# ADR-0026: supersede の半自動化 — save 時の候補提案と opt-in 自動置換

- Status: Proposed
- Date: 2026-06-26
- Supersedes: なし（ADR-0021 の supersedes-aware search を write 側へ拡張）
- Related: ADR-0002, ADR-0010, ADR-0011, ADR-0019, ADR-0021,
  arXiv:2606.24775 "Are We Ready For An Agent-Native Memory System?"

## Context

ADR-0021 で **supersedes-aware search** が入り、`Observation.supersedes` に
旧 observation_id を渡せば、新しい版が live な間は古い版が検索結果から沈む。
`superseded_by` 列・existence-based hiding・`rebuild_from_zenoh` での再構成
（`local_index.py` の `upsert` / search filter / rebuild）は実装済みで、
**置換の「表現」と「読み取り」は完成している**。

残った穴は **置換の「起点」が 100% 手動**であること。エージェントが
新しい `decision` / `config` を保存するとき、旧 observation_id を
`supersedes` に明示しなければ、新旧が両方 live のまま search に並ぶ。
典型例:

> 4月: `save "DB は SQLite を使う" --memory-type decision --subject db`
> 6月: `save "DB は PostgreSQL にする" --memory-type decision --subject db`
> （`--supersedes <4月のid>` を付け忘れる）
> → search "db" が **両方を返し、どちらが現行方針か判別できない**。

これは arXiv:2606.24775 が append-only ストアの最重要失敗モードとして挙げる
**「過去の幻覚（hallucinations of the past）」** そのもの。kioku-mesh は
ADR-0002 で observation を immutable とし、更新を新 ID での再保存として表現
するため、運用が続くほど superseded されるべき stale entry の比率は構造的に
増える。`supersedes` 機構があっても、起点が手動である限りこの穴は塞がらない。

同じ論文の他の所見が、**解の方向を「削除」ではなく「置換」に限定**する:

1. **「局所メンテ > 全体再編成」（コスト効率フロンティア）**。LLM
   consolidation・グラフ全体同期・whole-memory rewrite はコスト最悪。
2. kioku-mesh の GC（`memory/purge.py` の `gc_expired_tombstones` /
   `gc_expired_shadows`）は **tombstone / shadow を retention 超過後に物理
   回収するだけ**で、live な observation を年齢や importance で evict する
   経路は存在しない。「古い decision が勝手に消える」事故は起きない一方、
   「stale fact が live のまま積もる」問題は未対応。
3. SQLite index は Zenoh/RocksDB の source-of-truth から `rebuild_from_zenoh`
   で再構成される派生キャッシュ（ADR-0010）。あるホストで live obs を物理
   削除しても、source に tombstone を書かない限り **次の rebuild でメッシュ
   から復活**する。よって「重要度の低い古い記憶を自動 evict する」設計は
   mesh の rebuild モデルと正面衝突し、論文のコスト警告とも矛盾する。

結論として、塞ぐべきは「過去の幻覚」一点であり、手段は **既存の supersede
機構を起点側で半自動化する**こと。自動削除・自動 consolidation は採らない。

## Decision

ADR-0021 で実体化済みの supersede 機構の上に、**薄い検出・提案層**を足す。
3 つの部品はいずれも局所的（save パスのローカル index 参照、または doctor
の読み取り）で、Zenoh payload / key layout / replication には手を入れない。

### A. save 時の supersede 候補検出と提案（suggest-first・既定）

- **対象タイプ**: `memory_type ∈ {decision, config}`（revisable な型）のみ。
  `note` / `bug` / `pattern` / `summary` は追記が自然なので対象外。
- **候補条件**: 保存しようとする observation と、
  - 同一 effective visibility / scope_id（ADR-0019 の解決済みスコープ）
  - 同一 `project`
  - 同一 `memory_type`
  - 正規化後の `subject` が一致
  する **live**（tombstone も shadow も superseded もされていない）observation。
- **subject 正規化**: lowercase + trim + 連続空白の畳み込み程度に留める。
  subject は自由文字列であり**弱いキー**であることを設計上の前提とする
  （だから既定は silent な自動置換をしない）。
- **動作**: save 応答に候補を添える。**実際の supersede はしない。**

  ```
  saved: <new_id> (visibility=team/kioku-mesh)
  supersede_candidates: 1
    - <old_id>  subject=db  created=2026-04-12  "DB は SQLite を使う"
    hint: 訂正なら supersedes=<old_id> を付けて再保存、または
          `kioku-mesh supersede <new_id> --over <old_id>` で確定
  ```

  検出は store の save パスから `LocalIndex.search`
  （`project` + `subject` + `memory_type` filter、`include_superseded=False`、
  live のみ、`limit` 小）で行う。**追加の Zenoh round-trip は不要**で、
  ローカル index への 1 クエリで完結する。
- MCP の `save_observation` 応答にも同じ候補リストを構造化フィールド
  （`supersede_candidates: [{id, subject, created_at, summary}]`）で返す。
  エージェントは続けて `supersedes` を付けて再保存するか、無視するかを選べる。

### B. opt-in 自動置換（auto-supersede）

- config（`~/.config/kioku-mesh/config.yaml` の `auto_supersede: false` を
  既定、env `KIOKU_MESH_AUTO_SUPERSEDE`）で明示的に有効化したときのみ動く。
- **発火条件を A よりさらに絞る**: live 候補が **ちょうど 1 件**で、
  subject / memory_type / scope が完全一致のとき**だけ**。候補が 2 件以上
  または曖昧なときは auto せず A の提案に留める。
- **動作**: 書き込む直前の Observation の `supersedes` に候補 id を 1 件追加
  してから保存する。これだけで ADR-0021 の existence-based hiding /
  `superseded_by` 再構成にそのまま乗るため、**新しい replication / index
  セマンティクスは一切増えない**。
- **可逆性**: 万一 subject 衝突で誤って別事実を supersede しても、判定は
  existence-based なので、superseder 側を delete すれば旧 entry は自動的に
  検索へ復帰する（ADR-0021）。auto を「1 件・完全一致」に限定したうえで
  この可逆性が安全網になる。

### C. doctor による「複数 live 最新」検査（mesh 並行保存の受け皿）

- 2 つのホストが互いの保存を見る前に同 subject の新 decision を保存すると、
  save 時にはローカル index に相手がまだ無く、A も B も発火できない
  → **同 subject+project+type に複数 live decision が並ぶ**。
- これは save 時には原理的に捕れないので、`kioku-mesh doctor` に
  **「conflicting-latest」検査**を足す: 同一 (project, normalized subject,
  memory_type, scope) に live decision/config が複数あるグループを列挙し、
  `supersede` で解消するよう促す。読み取りのみ・局所的で、グローバル
  再編成は行わない（論文 Finding 5 と整合）。

### 対象外（このADRで決めないこと）

- **`importance` を検索ランキングに使う**こと。現状 `LocalIndex.search` は
  `bm25(obs_fts)` → `created_at DESC` で並べ、`importance` を順位付けに
  使っていない。importance の正しい住所は **GC/eviction ではなく検索順位**
  だが、これは read-path の独立論点なので **別 ADR-0027** に切り出す。
  本 ADR では「importance を eviction の判断材料にしない」理由（mesh rebuild
  と衝突）を明記するに留める。
- **自動 eviction / LLM consolidation / 全体再編成**。mesh rebuild モデルと
  衝突し、論文がコスト最悪と指摘する領域。採らない。
- **subject の意味的（埋め込み）マッチ**。弱いキー問題の本質解決だが、
  FTS / supersede と直交する別レイヤ。必要になった時点で別 ADR。

## Consequences

- **良い点**: append-only における唯一現実的な「過去の幻覚」経路
  （decision/config の置換し忘れ）を、save の瞬間に proactively 塞ぐ。
- **良い点**: ADR-0021 で完成済みの supersede 機構の上に乗る**薄い検出層**
  なので実装リスクが小さい。新しい wire 形式・index 状態を増やさない。
- **良い点**: mesh フレンドリー。supersede は immutable obs 上の明示ポインタ
  として普通に replicate され、hiding / rebuild は既にメッシュ全体で動く。
  自動 eviction と違い rebuild と喧嘩しない。
- **良い点**: suggest-first が既定なので、誤検出があってもエージェント/人間
  の確認を挟む。silent な誤置換が起きない。
- **悪い点**: `subject` は弱いキー。表記揺れ（"db" vs "database"）は取りこぼし、
  別物の同名 subject は false candidate になりうる。suggest なら無害、auto は
  「1 件・完全一致」限定 + existence-based の可逆性で緩和する。
- **悪い点**: mesh 並行保存（互いを見る前の同時保存）は save 時に捕れず、
  C の doctor 検査に委ねる。即時性はない。
- **悪い点**: save パスにローカル index 読み取りが 1 回増える。decision/config
  限定かつローカル SQLite なので軽微だが、ゼロではない。
- **悪い点**: auto-supersede は config の理解を要求する。既定 off で安全側。

## Alternatives Considered

### Alt 1: importance-aware の自動 eviction（古く重要度の低い記憶を GC で消す）

当初の最有力案。しかし (a) 現状 GC は live obs を年齢/importance で消す経路を
持たず「重要な記憶が古いだけで消える」問題は存在しない、(b) live obs を物理
削除しても source-of-truth に tombstone を書かなければ rebuild で復活、source
に書けばそれは eviction ではなく自動 delete で論文最大の警告に該当、(c) 論文は
全体再編成系をコスト最悪と明示。mesh の rebuild モデルと構造的に衝突するため
**却下**。importance は eviction ではなく検索順位に使う（ADR-0027）。

### Alt 2: auto-supersede を既定 ON にする

stale が即時に消えて理想的に見えるが、`subject` が弱いキーである以上、
別事実を silent に無効化する事故が起きうる。kioku-mesh は複数エージェントの
共有メモリであり、誤置換の影響範囲が広い。suggest-first を既定とし、auto は
明示 opt-in + 「1 件・完全一致」限定とする方が安全。**却下**。

### Alt 3: subject ではなく埋め込み類似で置換候補を検出する

表記揺れに強く弱いキー問題の本質に近いが、埋め込みモデル/ベクトル索引の依存が
増え、「ゼロ依存で local が動く」性格（ADR-0016）と FTS 中心の現設計から外れる。
別レイヤとして将来別 ADR で扱う。本 ADR は既存メタデータ（subject/type/project）
だけで成立させる。**却下（将来再検討）**。

### Alt 4: LLM consolidation で重複 decision を統合する

複数の stale decision を 1 つに要約・統合する案。論文が whole-memory rewrite を
コスト最悪と指摘する領域で、mesh rebuild とも相性が最悪（統合結果を source に
書くと元エントリの扱いが複雑化）。kioku-mesh の append-only + 局所 supersede の
規律から外れる。**却下**。

## Migration

- **後方互換**。A は save 応答に候補を**添えるだけ**で、新しい必須引数や wire
  変更はない。既存の save 呼び出しは挙動不変（応答に情報が増えるのみ）。
- B（auto-supersede）は config / env の opt-in。既定 off なので未設定環境は
  従来通り完全手動。
- C（doctor 検査）は読み取り専用の追加チェック。
- Zenoh payload / key layout / replication / `Observation` スキーマは不変。
  `supersedes` は既存フィールドで、auto はそこに値を詰めるだけ。
- 実装順序: ADR-0021（supersedes-aware search）に依存。ADR-0019（visibility）
  実装後はスコープ解決済みの effective visibility/scope_id を候補条件に含める
  こと（同一スコープ内でのみ候補を出す）。
- 段階導入: まず A（suggest）+ C（doctor）を入れて誤検出率を運用で観測し、
  十分低ければ B（auto, opt-in）を有効化、という順が安全。

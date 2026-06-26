# ADR-0025: 検索 rebuild — incremental FTS rebuild と drift-detection guard

- Status: Implemented (PR #228, Issue #224)
- Date: 2026-06-26
- Supersedes: なし（ADR-0021 R1「rebuild obs_fts as recovery path」を amend）
- Related: ADR-0007, ADR-0010, ADR-0011, ADR-0021

## Context

`LocalIndex.rebuild_from_zenoh` は起動・reconcile ごとに呼ばれ、Zenoh
（source of truth, ADR-0010）から全 observation を取得して SQLite local
index（ADR-0007）と突き合わせる。obs_index 側は既に **差分適用** で、
変化のない行は `unchanged` としてスキップしている。

ところが ADR-0021 で導入した FTS5 (`obs_fts`) の更新だけは、
`rebuild_from_zenoh` のたびに `DELETE FROM obs_fts` → 全行 re-INSERT する
**フル rebuild** になっている（コード上 `# ADR-0021 R1: ... recovery path`
と明記）。通常の write path（`upsert` / `delete`）は ADR-0021 の lockstep
で既に行単位の差分更新なので、フル rebuild が残るのはこの reconcile 経路
だけ。

trigram トークナイザは CJK 部分一致のために**インデックス対象テキストの
文字数にほぼ比例**してトークンを生成する。実測（現状 553 行 / 生テキスト
435KB）では:

- フル FTS rebuild: 約 60ms、FTS インデックス実体 約 3.8MB（生テキストの約 9 倍）
- 同条件で unicode61 は 約 10ms / 346KB だが、**日本語の部分一致が一切効かない**
  （スペース区切りが無く文全体が 1 トークンになるため）。`メッシュ` 等の
  検索が 0 件になる実害があり、trigram は「無駄」ではなく CJK 検索を素の
  SQLite だけで成立させるための必要コストである（ICU トークナイザはこの
  環境の SQLite ビルドに含まれない）。

したがってボトルネックの本質はトークナイザではなく、**reconcile のたびに
全行を再トークナイズしている**点にある。現状規模では 60ms で無視できるが、
trigram の特性上テキスト総量に線形なので、蓄積が 100 倍になれば数秒〜十数
秒に達し、毎起動のコストとして看過できなくなる。

フル rebuild には「FTS が何らかの理由で obs_index と乖離しても、全捨て
再構築で必ず修復される」という self-heal の副次効果があり、これを失わずに
差分化する必要がある。

## Decision

`rebuild_from_zenoh` の FTS 更新を、obs_index 側で既に計算済みの差分を
再利用した **incremental rebuild** に変更する。

1. **差分のみ FTS 更新**: reconcile が確定した
   - 追加・変更行（`upsert_rows` に入った obs）→ `_FTS_UPSERT_SQL` で upsert
   - 新規 tombstone（`mark_rows`）→ `_FTS_DELETE_SQL` で削除
   - 新規 shadow（`shadow_rows`, ADR-0011）→ `_FTS_DELETE_SQL` で削除
   - `unchanged` 行は lockstep 済みなので FTS を触らない

   同一パスで upsert かつ tombstone される行は net-delete（削除を後勝ち）と
   する。

2. **drift-detection guard で self-heal を担保**: 差分適用後に
   `COUNT(obs_fts)` と生 obs_index 件数（`deleted_at IS NULL AND
   shadowed_at IS NULL`）を比較し、不一致を検知したときだけ
   `_rebuild_fts_from_obs_index` によるフル rebuild にフォールバックする。
   通常は差分で速く、構造的ドリフトが起きたら自動修復される。

3. **トークナイザは trigram のまま**: 上記 Context の通り CJK 検索の要件
   から trigram を維持する（本 ADR のスコープ外。将来 Lindera 等の形態素
   トークナイザへ移行する場合は別 ADR で扱う）。

これは ADR-0021 を置換せず、その R1「フル rebuild を recovery path とする」
方針を「平常時は差分・ドリフト時のみフル」へ amend する位置づけ。

## Consequences

- **良い点**:
  - reconcile 時の FTS コストが「全行再トークナイズ」から「変化した行のみ」
    に減り、蓄積が増えても起動コストがほぼ一定になる。
  - obs_index 側で既にある差分計算を再利用するだけで、新たな状態を持たない
    （実装が局所的で、`rebuild_from_zenoh` の 1 トランザクション内で完結）。
  - drift guard により self-heal を維持。FTS が構造的に乖離しても次の
    reconcile で自動修復される。

- **悪い点 / トレードオフ**:
  - count ベースの drift guard は**行数の乖離**は検知するが、件数が一致した
    まま内容が壊れる silent content drift は検知できない。これは lockstep
    （ADR-0021）の正しさに依存し、最終手段として明示的フル rebuild
    （`_rebuild_fts_from_obs_index` を呼ぶ経路 / 必要なら env フラグ）を
    エスケープハッチとして残すことで緩和する。
  - 差分適用と drift 判定の分岐が増え、reconcile ロジックの読みやすさは
    わずかに低下する。回帰テスト（差分更新・tombstone 削除・shadow 削除・
    drift フォールバック・unchanged 不変の 5 ケース）でカバーする（実装済み）。
  - trigram のインデックスサイズ（生テキストの約 9 倍）は本 ADR では改善
    しない。サイズが問題化したら別途トークナイザ ADR で扱う。

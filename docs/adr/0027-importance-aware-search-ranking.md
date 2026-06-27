# ADR-0027: importance を検索ランキングに使う（eviction ではなく順位付け）

- Status: Accepted（importance を query 時の主ランキングキーに、bm25 はタイブレーク）
- Date: 2026-06-27
- Supersedes: なし（ADR-0021 が punt した「importance によるランキング補正」を具体化）
- Related: ADR-0002, ADR-0007, ADR-0021, ADR-0026,
  arXiv:2606.24775 "Are We Ready For An Agent-Native Memory System?"

## Context

`Observation.importance`（1–5）は save 時に付与され、MCP の instructions も
「4-5 は project-wide / durable な変更、3 は再利用可能だがローカル、1-2 は
それでも残す価値がある時だけ」と明確な意味を与えている。しかし read 側は
この値を **一切使っていない**。`LocalIndex.search` の順序は:

- query あり（FTS）: `ORDER BY f.rank (bm25), created_at DESC, observation_id DESC`
- query あり（LIKE フォールバック）/ query なし: `ORDER BY created_at DESC, ...`

つまり「billing の決定どれだっけ」と検索したとき、importance 5 の決定が
importance 2 の些末な note と **同じ重みで並び**、関連度（bm25）か新しさだけで
順位が決まる。kioku-mesh の支配的ワークロードは *curated な決定の正確な単発
リコール*（ADR-0021 の問題意識）であり、ここで「重要なものを上に出す」のは
素直な品質改善になる。

ADR-0021 は FTS5 + bm25 を入れた際、`importance` や参照頻度によるランキング
補正を **「bm25 の実効を見てから判断」** として明示的に punt していた。bm25 が
入って運用された今、その続きを入れる。

重要な制約（調査の結論、ADR-0026 とも共有）: **importance を「忘却（eviction）」
の判断に使ってはいけない。** kioku-mesh の GC は live obs を年齢/importance で
evict せず、自動 eviction は Zenoh→SQLite の rebuild で復活して mesh モデルと
衝突する（ADR-0026 の Alt 1 参照）。importance の正しい住所は **検索順位**で
あって、保存寿命ではない。本 ADR はその住所を実装する。

## Decision

`LocalIndex.search` のランキングに importance を **二次キー**として足す。
SQLite sidecar 内で完結し、Zenoh payload / key / replication には触れない
（ADR-0021 と同じ範囲）。新しい API 引数は増やさない。

### ランキング規則

**query があるときだけ**、importance を **主ランキングキー**にし、関連度シグナル
（bm25 / recency）を importance バケット内のタイブレークにする:

- FTS query パス: `ORDER BY importance DESC, f.rank, created_at DESC, observation_id DESC`
  → importance 5 の決定が、その語に軽く触れただけの importance 2 の note より
  上に来る。同 importance 内では bm25 が順序を決める。
- LIKE フォールバック / 短語 query（bm25 スコアが無い経路）:
  `ORDER BY importance DESC, created_at DESC, observation_id DESC`
  → スコアが無いので importance 主キー、次いで recency。
- OR / and_or モードも同様に、関連度キーの前に importance を置く。OR の FTS パスは
  `(f.rank IS NULL)` を先頭に保ち、FTS 一致を LIKE-only より先に出す既存契約を維持。

### なぜ「重み付きブレンド」ではなく importance 主キーなのか（実測根拠）

当初は `ORDER BY (f.rank - w·importance)` の **加重ブレンド**を実装したが、実測で
**固定重みが破綻する**ことが判明した。SQLite の trigram bm25 rank は単語1語の
一致で **おおよそ 1e-6 オーダー**、文書間の差は更に小さい（実測例: -1.9e-6 〜
-7.7e-7）。一方 importance の寄与は `w·importance`。スケールが約6桁違うため、

- w を小さくすると importance が **まったく効かない**（bm25 の 1e-6 に埋もれる）、
- w を実用的にすると importance が **完全支配**し、関連度差を無視する。

しかも bm25 の絶対スケールはコーパスサイズ/文書長分布に依存するので、**どの
コーパスでも安定して効く固定 w は存在しない**。これは ADR-0021 が importance
ランキングを「bm25 の実効を見てから」と punt した警戒を裏づける結果でもある。

trigram bm25 は単語1語クエリの文書間をほとんど区別しない（差が 1e-6）一方、
importance は 1–5 の安定したバケットを与える。よって **importance を主キー、
bm25 を同 importance 内のタイブレーク**にするのが、重み調整不要で頑健、かつ
「重要な決定を上に」という狙いを確実に満たす設計になる。

### 2 つの意図的な除外

1. **query なしの browse 列挙は純粋に時系列のまま**
   （`ORDER BY created_at DESC`）。query を伴わない検索の契約は「最近のもの」で
   あり、importance で並べ替えると驚きになる。importance は *関連度*ランキングの
   ための信号で、browse のための信号ではない。
2. **カーソルページネーション（bulk-delete, #66）では importance を入れない**。
   `(created_at, observation_id)` の strict-tuple カーソルが順序前提で歩くため、
   importance を挟むと walk が行を飛ばす/重複する。`rank_by_importance =
   bool(query_terms) and not cursor_observation_id` で構造的に除外する。

### トレードオフと将来（このADRでは決めない）

importance 主キーは、**多語の精密クエリで関連度差より importance を優先する**。
curated な決定の単発リコールが支配的な kioku-mesh のワークロードではこれが
望ましいが、将来「多語クエリの精密な関連度順位」が要件化したら、result set 内で
bm25 を **正規化**（min-max / ランク位置）してから importance とブレンドする
Python 側 re-rank を別 ADR で検討する。固定重みの加重ブレンドには戻らない。

## Consequences

- **良い点**: 既存の `importance` フィールドが初めて read 側の価値を持つ。
  「重要な決定を上に」がゼロ依存・SQLite 内で、重み調整なしに実現する。
- **良い点**: 固定重みを持たないので、コーパスサイズや文書長分布が変わっても
  挙動が安定する（加重ブレンドの致命的弱点を回避）。
- **良い点**: browse とカーソル経路を除外したことで、recency 契約と bulk-delete
  の正しさは不変。同 importance 内では bm25 が順序を保つ。
- **悪い点**: 多語の精密クエリでも importance が関連度に優先する。curated 想起
  ワークロードでは妥当だが、精密な関連度順位が要る用途では粗く感じうる
  （将来の正規化 re-rank で対応、本ADR外）。
- **悪い点**: importance は save 時のエージェント判断に依存する弱い信号。
  誇張された importance がランキングを歪めうる。MCP instructions が importance
  の付け方を規定している前提に乗る。
- **中立**: `importance` は obs_index の実カラムなので追加コストはほぼゼロ
  （ORDER BY 句に 1 列足すだけ、新規インデックスは張らない）。

## Alternatives Considered

### Alt 1: importance を eviction（GC）に使う

却下済み（ADR-0026 Alt 1）。GC は live obs を年齢/importance で消さず、自動
eviction は mesh rebuild で復活して破綻する。importance の住所は順位であって
寿命ではない。

### Alt 2: 加重ブレンド `ORDER BY (f.rank - w·importance)`

**実装して実測した上で却下**。SQLite trigram bm25 rank が ~1e-6 と小さく、かつ
コーパス依存でスケールが動くため、importance を効かせる固定 w が存在しない
（小さいと無効、大きいと関連度を全面的に上書き）。詳細は本文「なぜ importance
主キーか」参照。将来やるなら result set 内で bm25 を正規化してからのブレンド。

### Alt 3: importance を bm25 の後のタイブレークにする（関連度優先）

最初に実装した案。関連度を絶対に覆さず安全だが、trigram bm25 が単語1語クエリの
文書間をほぼ区別しない（差 1e-6）ため、実クエリで importance がほとんど効かず
「重要な決定を上に」という狙いを達成できないことがスモークで判明。却下。

### Alt 4: browse（query なし）も importance で並べ替える

「project の現状を見る」用途では importance 5 の決定が上に来て嬉しい面もあるが、
query なし列挙の「最近のもの」という契約を壊し、`status` 的な時系列ビューが
直感に反する。関連度ランキング（query あり）に限定する方が予測可能。却下。

### Alt 5: search に `rank_by` 引数を足して opt-in にする

API 表面が増え、エージェント/CLI 双方の呼び出しを更新する必要がある。影響は
query 指定時の並び順のみで browse/カーソルは不変、かつ wire 変更もないため、
常時 ON で十分。将来 result set 正規化 re-rank を入れて挙動が大きく変わるなら、
その時に opt-in を再検討する。

## Migration

- 後方互換。新しい引数・スキーマ変更・wire 変更なし。`importance` は既存カラム。
- 影響は **query 指定時の並び順のみ**。browse / カーソルページネーション /
  tombstone・supersede フィルタの挙動は不変。
- 旧バージョン peer との混在で wire は変わらないため rolling upgrade の問題なし。
- Zenoh fallback 経路（`_search_via_zenoh`）は縮退運転として importance ランキング
  を持たない（ADR-0021 が supersedes フィルタを fallback に入れなかったのと同じ
  方針）。index 有効時の最適化と位置づける。

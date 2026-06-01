# Proactive Save の遵守率を測る — opportunity coverage 設計 (#105)

Date: 2026-05-28
Refs: PR #103, ADR-0009, Issue #104, worker4_design.yaml (D51-4)

## 背景 / 問題

PR #103 で proactive save reinforcement（server instructions + 各 tool docstring
への分散 reminder + `get_memory_status` の `last_save_at`）を実装したが、その
acceptance は **定性的（dogfooding 評価）に留まっている**。「長セッションで LLM が
保存規約を無音で外す」という元の問題に対し、施策が効いているかを **客観的に計測
する手段が無い**。`save_observation` の呼び出し回数だけでは「呼ぶべきだったのに
呼ばなかった」機会損失（false negative）を捉えられない。

本書は、保存の遵守率を **opportunity coverage（保存機会のカバー率）** という単一
指標で定義し、それを小さなトレースから反復改善できるパイプラインの第一歩を設計
する。`scripts/save_coverage.py` がその試作実装である。

## スコープ外

mesh-mem server 単体では完結しない（Issue 本文どおり）。保存機会イベントの抽出は
**client / hook / ログ解析の協調**が必要で、server に計測責務を持たせると、
ADR-0009 の「規約は配るが計測はしない」境界を侵す。本書とスクリプトは
**server プロセスの外側**（`scripts/` の解析ツール）に閉じる。

## 指標の定義

```
opportunity coverage = (保存に至った保存機会の数) / (保存機会の総数)
```

- **保存機会 (opportunity)**: 「保存すべき」と規約 (`_INSTRUCTIONS`) が言う瞬間。
  バグ修正完了、設計判断、非自明な発見、パターン確立、根拠付き config 変更、
  セッション結論など。
- **保存 (save)**: 実際の `save_observation` 呼び出し。
- 1 つの save が 1 つの opportunity を「カバー」する **1:1 対応**とする。proactive に
  保存しすぎても 1 機会しか消費しない（過剰保存を coverage に化けさせない）。

呼び出し回数（recall の分子だけ）ではなく **分母（機会）と突き合わせる**点が
PR #103 の定性 acceptance との差分。coverage が低い = 機会を取りこぼしている、
という機会損失を直接可視化する。

### マッチングのルール

ある opportunity が **covered** とみなされるのは、その時刻 **以降** かつ
`window_seconds`（既定 1800s = 30 分）**以内**に save が存在するとき。

- **time window**: 「保存はその場で」という規約の意図を反映。30 分を超えて後から
  保存されたものは、文脈が薄れた後追いとみなし coverage に数えない（既定値であり
  トレースに応じて調整する前提）。
- **greedy 1:1**: 時刻順に走査し、save が来たら **最も古い** 適格な未消化
  opportunity に割り当てる。割り当て先が無い save は `orphan_saves`（ログ化されて
  いない機会に対する保存、またはノイズ）。どの save にも届かなかった opportunity は
  `missed`。
- **type 突き合わせ（任意）**: `--require-type-match` を付けると、opportunity の
  `kind` と save の `memory_type` が整合する場合のみカバー成立とする
  （`bug↔bug`, `decision↔decision`, `discovery→note` 等。`OPPORTUNITY_TO_MEMORY_TYPE`
  参照）。既定は off（time window のみ）。厳密化は反復の余地として opt-in にした。

## トレース形式（パイプラインの中間表現）

transport 非依存にするため、入力は **1 行 1 JSON オブジェクトの JSONL** に正規化
する。hook / ログスクレイパ / 手動アノテーションのいずれが生成してもよい。

```jsonl
{"ts": "2026-05-28T10:00:00Z", "type": "opportunity", "kind": "bug",     "label": "fixed null-deref in parser"}
{"ts": "2026-05-28T10:02:00Z", "type": "save",        "memory_type": "bug", "observation_id": "..."}
```

| フィールド | 対象 | 必須 | 意味 |
|---|---|---|---|
| `ts` | 両方 | ✓ | ISO8601（`Z` 許容、UTC 正規化） |
| `type` | 両方 | ✓ | `"opportunity"` / `"save"` |
| `kind` | opportunity | – | `bug`/`decision`/`discovery`/`pattern`/`config`/`summary`/`note` |
| `memory_type` | save | – | `save_observation` の `memory_type` |
| `label` (or `detail`) | 両方 | – | 人間可読の短い説明（レポート表示用） |
| `observation_id` | save | – | 追跡用（指標計算には未使用） |

不正行（壊れた JSON、`ts`/`type` 欠落、パース不能な timestamp）は行番号付きで
`ValueError` にする。**壊れたトレースで指標を黙って歪めない**ためのフェイルファスト。

## データソース（機会イベントをどう得るか）

`save` イベントは比較的容易に得られる（下記いずれか）が、`opportunity` イベントの
抽出が本質的に難しい。**まずは小さく**から始め、自動化は段階的に上げる。

### save イベント

1. **mesh-mem index から**: `kioku-mesh search --since ... --format ...` の結果を
   `{"type":"save","ts":created_at,"memory_type":...}` に変換（最も確実）。
2. **MCP client ログから**: Claude Code 等の tool-call ログで
   `save_observation` 呼び出しを拾う。

### opportunity イベント（難所）

- **L0 — 手動アノテーション（まずここから）**: セッションを振り返り、保存すべき
  だった瞬間を人手で JSONL に起こす。小さなトレースで指標設計を回すための出発点。
- **L1 — hook ベースの近似**: Claude Code の `PostToolUse` / `Stop` hook（plan.md
  の「Hooks 自動保存」の発想）で、保存機会の **候補シグナル** を機械抽出する。例:
  - 編集 tool 成功（`Edit`/`Write`）の直後 → 実装/修正完了の候補
  - テスト実行が pass→ 直前に fail があれば bug fix 完了の候補
  - commit / PR マージ等のライフサイクルは **機会ではない**（`_INSTRUCTIONS` の
    SKIP 規約と整合させ、ノイズとして除外）
- **L2 — トランスクリプト解析**: 会話ログを LLM で後処理し、決定/発見/規約確立を
  分類して opportunity に落とす（コスト高、精度検証が別途必要）。

L1/L2 は誤検出が coverage を不当に下げうるため、**ノイズ除去フィルタの設計が肝**。
SKIP 規約（PR/Issue lifecycle、PR/ADR/commit の再掲、進捗ログ、無根拠の "tests pass"）
を opportunity 抽出側にも適用する。

## パイプライン（段階）

```
[セッション] ──┬─ save ソース  (index / client log) ──┐
               │                                        ├─► 正規化 ─► trace.jsonl ─► save_coverage.py ─► coverage / missed / orphan
               └─ opportunity ソース (manual / hook / 解析) ┘
```

第一イテレーション（本 PR）が提供するのは右半分の **指標計算器** と、入力契約
（JSONL スキーマ）だけ。左半分（抽出器）は L0（手動）で回し、L1 hook は別 PR で
反復する。

## 試作実装 — `scripts/save_coverage.py`

server パッケージ (`src/mesh_mem/`) には入れず、`scripts/`（既存 `hooks/` と同じく
client/解析ツール置き場）に置く。テストは `tests/test_save_coverage.py` で
importlib によりパス読み込みして検証する。

```console
$ python scripts/save_coverage.py scripts/save_coverage_example.jsonl
opportunity coverage: 75.0% (3/4)
window: 1800s, type-match: off
missed opportunities (1):
  2026-05-28T11:00:00+00:00 [pattern] established the session-start reminder convention
```

主な API（テストが固定する不変条件）:

- `parse_events(lines) -> list[Event]` — JSONL を厳格パース
- `compute_coverage(events, window_seconds=1800, require_type_match=False) -> CoverageReport`
- `format_report(report, as_json=False) -> str`
- CLI フラグ: `--window-seconds` / `--require-type-match` / `--json` /
  `--min-coverage`（CI ゲート用、下回ると exit 1）

`--min-coverage` により、ゴールデントレースに対する回帰を CI で検出できる
（「施策変更で coverage が下がっていないか」の客観チェック）。

## 限界 / 既知の弱点

- **opportunity の ground truth が主観的**。L0 手動アノテーションは観測者バイアスを
  持つ。指標の絶対値より、**同条件での施策 A/B の相対比較**に使うのが妥当。
- **window と 1:1 マッチは経験則**。バースト的に複数機会が密集すると、後続 save が
  古い機会から順に消化されるため、意味的に正しい対応とズレうる。type 突き合わせで
  緩和できるが完全ではない。
- **clock skew**。分散保存の `created_at` は host 間で揺れる（plan.md / Issue #10）。
  単一セッション内トレースなら影響は小さいが、複数 host を跨ぐ集計では注意。

## 受け入れ / 検証

- `tests/test_save_coverage.py`: パース・full coverage・window 超過の miss/orphan・
  1:1 マッチ・type ゲート・機会ゼロ時 0.0・JSON 出力・`--min-coverage` exit code を
  assert（全 11 ケース）。
- サンプルトレース `scripts/save_coverage_example.jsonl` は coverage 75%（4 機会中
  3 カバー、pattern 1 件 miss）になるよう構成し、ドキュメントと挙動を固定。

## 次の一手（別 PR）

1. `kioku-mesh search` 出力 → save トレースへの変換ヘルパ（実データで回す）。
2. `PostToolUse` hook による opportunity 候補抽出（L1）＋ SKIP フィルタ。
3. coverage を時系列で記録し、施策（#104 の deferred-load 緩和等）の前後比較を行う。

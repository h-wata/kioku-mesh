# ADR-0018: save_observation 取り逃し対策は「言語非依存 instructions + session-scoped nudge」で server 側に寄せる

- Status: Accepted
- Date: 2026-06-05
- Supersedes: なし（ADR-0009 を補強）
- Related: Issue #158, PR #159 (Phase 1), PR #160 (Phase 2), PR #161 (Phase 4)

## Context

2026-06-01〜06-03 の Claude Code セッションを遡及監査したところ、
ADR-0009 で server 同梱した `_INSTRUCTIONS` の PROACTIVE SAVE プロトコル
が **承認系決定で取り逃しを起こしている** ことが分かった。

具体例（いずれも durable な decision にもかかわらず未 save）:

- kioku-mesh public 化 / Show HN 紹介: 承認 21 turn / save 0
- 連載記事の UV 推奨採用: 承認 5 / save 0
- tmux-multi-agents の Worker 全員 Sonnet 化: 承認 2 / save 0

根因を 3 つに整理した。

1. **承認 trigger が抽象的すぎる**: 「User confirms a recommendation」だけだと
   LLM が承認の semantic act を取りこぼす。とくに日本語などの非英語チャットで顕著。
2. **SoR 言い直し SKIP ルールの過剰適用**: 「PR / ADR / commit に書いたから
   SKIP」と判定して、why / 代替案 / ユーザー嗜好まで切り落とす。SoR から
   後で再構成できない情報まで失う。
3. **session 単位の自己点検が無い**: 「今のセッションで何件 save したか」を
   LLM が低コストで確認する手段が無い。

外部 (Claude Code 設定 / 別 hook) ではなく **kioku-mesh 本体で対処** することにした。
理由: kioku-mesh は OSS で multi-client（Claude Code / Codex CLI / 他）を
想定しており、外付け施策は配布できない。

## Decision

3 つの補完的レイヤを順に投入する。

### Layer 1 (Phase 1, PR #159) — `_INSTRUCTIONS` の意味的 + 多言語化

- 承認 trigger を「approval / authorization / preference / rejection の
  **semantic act**」として再定義し、EN / JA / ZH / KO の例を anchor として
  添える。単語列挙ではなく「概念 + 例」で LLM の多言語理解に乗せる。
- SKIP 節に **exception** を明文化: SoR は *decision* を保存するが
  *rationale*（捨てた代替案 / 背景制約 / ユーザー嗜好）は保存しない。これらは
  別 entry として save 可と明示。
- `tests/test_mcp_server.py` に語句 anchor の存在を assert するテストを追加し、
  将来のリファクタで規約が無言で落ちることを防ぐ。

### Layer 2 (Phase 2, PR #160) — `get_memory_status` の session-scoped nudge

- 既存出力に `session_age` / `this_session_saves` /
  `this_session_last_save_age` / 条件付き `nudge` を追加。
- `this_session_saves` は process-local counter ではなく **既存 Observation
  store を `session_id == current` で query** して算出。MCP server の
  再起動・multi-process・pending queue いずれにも堅い（Codex review 反映）。
- nudge は (a) session 経過 ≥10 分 & save 0、または (b) 直近 save から
  ≥20 分経過、のいずれかでのみ発火。文言は英語固定で機械可読。
- 自動 save / 自動判定はしない。heuristic は **点検促し止まり**。

### Layer 3 (Phase 4, PR #161) — Optional client 側 hook

- `scripts/hooks/check-unsaved-decisions.sh` を repo 同梱。Claude Code の
  `PreCompact` および `UserPromptSubmit (^/clear)` から発火し、transcript
  JSONL の承認系 turn 数と `save_observation` 呼び出し数を比較してリマインダー
  を出す。
- transcript 形式は client 依存（Claude Code 専用）なので **core 機能ではなく
  optional integration** として `docs/mcp-clients.md` / `*.ja.md` に install
  手順だけ案内。
- デフォルト regex は EN + JA。他言語はスクリプト冒頭のコメントで案内し
  利用者がカスタマイズする。

### 採用しなかった案

- **Phase 3: 専用 tool `mem_session_review`** — Layer 2 と機能が重複するため
  最初は見送り。Layer 2 の nudge が不足だと判明したら独立 tool に切り出す。
- **Tool response への自動 nudge 注入** — `save_observation` 以外の全 tool
  response 末尾に server が文言を混ぜる案。structured output を期待する
  client や、結果を機械処理する agent への悪影響が大きく、誤発火時の MCP
  信頼性低下も避けたいため **不採用**。nudge は `get_memory_status` /
  （将来の）`mem_session_review` のような明示 tool に閉じる。
- **承認語の機械判定による自動 save** — heuristic を品質指標化する形に
  なるため不採用。「外部送信確認用の OK」と「durable decision の OK」は
  surface 文では区別できない、という Codex review を受け入れた。
- **process-local な session save counter を server に持つ** — 再起動 / 複数
  process / pending queue でズレるため不採用。store query で代替（Layer 2）。

## Consequences

### Positive

- 承認の semantic act が言語非依存になり、JA / ZH / KO の非英語チャットで
  recall が改善する見込み。
- `get_memory_status` の nudge により、LLM が低コストで「このセッション
  まだ save 0 だ」を自己認識できる。process 再起動でもズレない。
- ADR-0009 の「server 同梱で配布する」方針を継承しつつ、client 多様化
  （Codex CLI など）にも届く。
- Layer 3 を optional に分離したことで、core 機能の言語非依存性を保てる。

### Negative / Trade-offs

- nudge の閾値（10 分 / 20 分）は経験則。誤発火 / 過小発火が観測されたら
  チューニングが要る。
- Layer 3 は Claude Code 専用かつ regex ベースのため、他 client / 他言語
  ユーザーは恩恵を受けない。ただしこれは optional と明示。
- 既存 client のうち `get_memory_status` を呼ばない agent には Layer 2 が
  届かない。Layer 1 の instructions 改訂で部分的に補う。

### 検証

- 直近 5 セッションを Layer 3 script で走査し、漏れがあったセッション 3 件で
  警告が点灯、漏れの無い 1 件は silent、未送信セッションで誤発火しないことを
  確認済み。
- `tests/test_mcp_server.py` を 23 → 26 件に拡張。Layer 1 / 2 双方の語句 +
  挙動を assert。

## References

- Issue #158: Improve save_observation recall: language-agnostic instructions + session save nudge
- PR #159: docs(mcp): make approval trigger language-agnostic + carve out WHY in SKIP rule
- PR #160: feat(mcp): add per-session save block + nudge to get_memory_status
- PR #161: docs(hooks): add optional Claude Code save-reminder script
- ADR-0009: MCP server の `instructions` で PROACTIVE SAVE 規約を出荷
- Codex review on Issue #158 (process-local state / Phase 4 不採用 / heuristic 限界)

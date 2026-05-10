# ADR-0009: MCP server の `instructions` で PROACTIVE SAVE 規約を出荷

- Status: Accepted
- Date: 2026-05-08
- Supersedes: なし

## Context

mesh-mem の MCP tool 群（`save_observation` / `search_memory` /
`get_memory` / `delete_memory` / `get_memory_status`）は登録済みだが、
**コーディングエージェントに「いつ呼ぶべきか」を伝える帯域内シグナルが
無かった**。結果、dogfooding は手動 save に縛られ、自動トリガが効く
engram（SessionStart hook を持つ）と比べて再現性が劣っていた。

選択肢として以下を比較した。

1. **per-project `CLAUDE.md` に保存規約を貼る** — 配布性が無く、新規 project
   ごとに人手で同期する負担が発生。形骸化が早い。
2. **server-side instructions を MCP プロトコルで配布** — `FastMCP` の
   `instructions=` を経由し、Claude Code が `# MCP Server Instructions`
   セクションに自動で展開する。サーバーバイナリと一緒にプロトコルが
   出荷される。
3. **クライアント側 SDK でラップ** — クライアント実装ごとに重複作業が
   発生し、SDK を持たない MCP クライアントには届かない。

実装コストと配布性の両面で 2 が優位。

## Decision

`FastMCP('mesh-mem', instructions=_INSTRUCTIONS)` で **PROACTIVE SAVE
プロトコル**を MCP server 自身に同梱して配布する（commit `e7414a3`,
`src/mesh_mem/mcp_server.py`）。

instructions には以下を含める。

- **PROACTIVE SAVE トリガ**: decision / bug / discovery / pattern / config /
  feature / preference / session-summary を列挙し、
  「task ごとに self-check して該当すれば即 `save_observation`」を要求。
- **SEARCH MEMORY トリガ**: ユーザが想起を求めた時 / 既知性のあるトピック
  着手時 / 文脈のないリファレンス時。
- **identity の server 側解決**: `agent_family` / `client_id` / `pc_id` /
  `session_id` は環境 + state から解決済みのため、LLM 引数として渡さない
  ことを明記。
- **`memory_type` の closed enum** と `importance` 1–5、`subject` /
  `summary` の付与推奨。

合わせて `tests/test_mcp_server.py::test_server_advertises_proactive_instructions`
で `initialize_result.instructions` に `PROACTIVE SAVE` /
`save_observation` / `search_memory` の文字列が含まれることを assert し、
将来のリファクタで規約が無言で落ちることを防ぐ。

## Consequences

- **良い点**: per-project の CLAUDE.md 編集なしに、mesh-mem MCP を接続した
  全クライアントが規約を自動受信する。新規セットアップが「サーバー登録
  だけ」で完結。
- **良い点**: 規約とサーバー実装がリポジトリ内で同居するため、
  `memory_type` enum や identity 解決のような実装変更と同期して規約を
  更新できる（ADR-0004 や ADR-0008 と整合する更新もここに集約）。
- **良い点**: smoke test で文言の必須要素を固定したため、リファクタによる
  サイレント縮退を CI で検出可能。
- **悪い点**: instructions の文言を変えるたびにテストの assert と CHANGELOG
  両方を更新する必要がある（コストは小さいが、`memory_type` の語彙変更時に
  忘れやすい）。
- **悪い点**: instructions を読まない / 表示しない MCP クライアントには
  効かない。Claude Code は `# MCP Server Instructions` で必ず surface する
  が、他クライアントの挙動は実装依存。
- **悪い点**: server 側で「常時アクティブな指示」を出荷することは、ユーザ
  視点では「LLM が勝手に保存を始める」挙動として観測されうる。`memory_type`
  の意味付けと importance 1–5 を docstring に明示してフレームレートを
  揃えるが、最終的にはユーザの好みによる無効化口（環境変数等）が必要に
  なる可能性がある（現状は未実装）。

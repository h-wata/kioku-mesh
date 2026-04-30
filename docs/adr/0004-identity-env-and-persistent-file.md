# ADR-0004: pc_id / session_id を環境変数＋永続ファイルの二層で管理し MCP 引数から除外

- Status: Accepted
- Date: 2026-04-27
- Supersedes: なし

## Context

分散メモリに保存される各 Observation には、どのエージェント・PC・セッションから発行されたかを示す識別子が必要。
識別子の設計で検討した課題:
- `agent_family` / `client_id` はユーザーが明示的に注入すべき情報
- `pc_id` を HOSTNAME で代替すると rename/clone/コンテナ環境で壊れる
- `session_id` を毎回新規生成すると、同一作業の観測が複数セッションに分裂する
- MCP tool 引数で識別子を受け付けると LLM が誤値（例: "claude" vs "claude-code"）を渡して識別空間を汚染する

## Decision

識別子を以下の方針で管理する:

| 識別子 | 管理方法 |
|--------|---------|
| `agent_family` / `client_id` | 環境変数のみ（`MESH_MEM_AGENT_FAMILY`, `MESH_MEM_CLIENT_ID`） |
| `pc_id` | 初回起動時に UUID を生成し `$HOME/.local/share/mesh-mem/pc_id` に永続化。hardlink publish で複数プロセスの race を排除 |
| `session_id` | プロセス起動時に環境変数 `MESH_MEM_SESSION_ID` を参照、未設定なら UUID を自動生成してプロセス内でキャッシュ |

MCP tool `save_observation` の引数から識別子を**意図的に除外**し、LLM が識別子を制御できないようにする。

## Consequences

- **良い点**: LLM が誤った識別子を渡すことによる名前空間汚染を防止できる
- **良い点**: プロセス内で session_id が変化しないため、同一作業の観測が同一セッションに集約される
- **良い点**: pc_id の UUID 永続化により、ホスト名変更/クローン/コンテナ再起動でも同一 PC として認識される
- **悪い点**: 識別子を変更したいときは環境変数の再設定や永続ファイルの手動削除が必要で、通常のユーザーには不透明
- **悪い点**: MCP tool から識別子を制御できないため、テスト時に特定の識別子を再現するには環境変数の注入が必要

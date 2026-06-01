# マルチエージェント identity（単一ホスト・複数エージェント）

1 台の PC で複数エージェントを並走させる場面は普通にあります。Claude Code を
別ターミナルで 2 つ、Codex CLI セッション 1 つ、夜間 cron で動く autonomous
cleanup 1 つ、といった具合です。それぞれが Zenoh key で衝突しないように、
各エージェントは **distinct な identity** を持つ必要があります。

## Identity の 4 階層

kioku-mesh は key prefix を次の 4 階層で組み立てます：

| 階層 | 取得元 | 用途 |
|------|--------|------|
| `agent_family` | env `MESH_MEM_AGENT_FAMILY` | 実装の種別: `claude-code`, `codex`, `auto-agent`, `kachaka-bridge`, ... |
| `client_id`    | env `MESH_MEM_CLIENT_ID`    | 同じ family 内の異なるインスタンス |
| `pc_id`        | `$MESH_MEM_STATE_DIR/pc_id` に永続化（自動生成 UUID） | ホスト単位の identity、再起動でも不変 |
| `session_id`   | env `MESH_MEM_SESSION_ID`、未指定なら自動生成 `{ts}-{uuid8}` | プロセス起動ごと、agent のライフタイム内は不変 |

Zenoh key layout: `mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}`

同じホスト上で **`client_id` が distinct** なら、`agent_family` と `pc_id` を
共有していても key は衝突しません。逆に `agent_family` と `client_id` が
同じ 2 つの agent は同一論理 agent として扱われ、観測は重なる key に
書かれます（同じ `session_id` 内では last-writer-wins）。

## 同じホストで Claude Code を 2 つ走らせる

```bash
# Terminal 1
export MESH_MEM_AGENT_FAMILY=claude-code
export MESH_MEM_CLIENT_ID=claude-instance-1
claude

# Terminal 2 (same host)
export MESH_MEM_AGENT_FAMILY=claude-code
export MESH_MEM_CLIENT_ID=claude-instance-2
claude

# Autonomous agent (e.g. cron / systemd timer)
export MESH_MEM_AGENT_FAMILY=auto-agent
export MESH_MEM_CLIENT_ID=nightly-cleanup
~/.venv/kioku-mesh/bin/kioku-mesh gc --retention-days 30
```

## 命名規則（推奨）

- `agent_family`: 小文字＋ハイフン (`claude-code`, `codex`, `auto-agent`)
- `client_id`: 用途のヒントを残したいなら `{family}-{purpose}-{N}` 形式
  (`claude-code-research-1`, `codex-refactor-2`)。任意の unique 文字列で OK。
- agent の履歴を 1 ストリームとして検索可能にしておきたいので、再起動を
  跨いで `client_id` は使い回すのが基本。役割が変わったときだけ変える。

プロジェクト間をまたぐシェルでは、`direnv` でディレクトリ単位に env を
スコープすると楽です：

```bash
# .envrc in ~/projects/foo
export MESH_MEM_CLIENT_ID=claude-code-foo

# .envrc in ~/projects/bar
export MESH_MEM_CLIENT_ID=claude-code-bar
```

## 検索時のエージェントフィルタ

```bash
# このマシンのこの Claude Code インスタンスだけ
kioku-mesh search "auth" --client-id claude-instance-1

# 任意の peer 上のあらゆる Claude Code
kioku-mesh search "auth" --agent-family claude-code

# 特定の autonomous agent
kioku-mesh search "" --client-id nightly-cleanup --limit 100
```

## MCP から起動されるエージェント

MCP ハーネス（Claude Code、Claude Desktop、Codex、Gemini）から起動された
エージェントは、shell の export を読めません。ハーネスが渡したものしか
継承されないので、Claude Code と Codex CLI では
`kioku-mesh mcp install --client <client>` が妥当な既定値を書き込みます。
独自 identity が必要なら `--env KEY=VALUE` を追加してください。その他の MCP
クライアントでは、MCP server エントリの `env` ブロックに
`MESH_MEM_CLIENT_ID`（family default と違う場合は `MESH_MEM_AGENT_FAMILY` も）
を設定する必要があります。MCP tool 引数で identity を渡さない理由は
[ADR-0004](adr/0004-identity-env-and-persistent-file.md) を参照。

```jsonc
// ~/.claude.json or claude_desktop_config.json
{
  "mcpServers": {
    "kioku_mesh": {
      "command": "/home/USER/.venv/kioku-mesh/bin/kioku-mesh-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/127.0.0.1:7447",
        "MESH_MEM_AGENT_FAMILY": "claude-code",
        "MESH_MEM_CLIENT_ID": "claude-code-instance-1"
      }
    }
  }
}
```

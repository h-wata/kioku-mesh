# MCP 登録

対応済みクライアントでは wrapper を優先してください：

```bash
kioku-mesh mcp install --client claude-code
kioku-mesh mcp install --client codex-cli
```

wrapper はインストール済みの `kioku-mesh-mcp` binary を解決し、クライアント別の
登録先へ書き込み、既定の identity env も設定します。上書きしたい値は
`--env KEY=VALUE`、既存登録の置換は `--force`、生成される command / config の確認は
`--dry-run` を使います。

未対応クライアントや手書き config では、`kioku-mesh-mcp` console script を
**絶対パス**で登録します。典型的には `uv tool install` なら
`~/.local/bin/kioku-mesh-mcp`、手動 venv なら
`~/.venv/kioku-mesh/bin/kioku-mesh-mcp` です。PATH に依存する形式だと、agent が
デスクトップショートカット等から別環境で起動されたときに壊れます。各 agent は
それぞれ自分の `MESH_MEM_CLIENT_ID` を持ち、`MESH_MEM_AGENT_FAMILY` だけが同 family
の兄弟間で共通になります。

## Claude Code

まず wrapper を使います：

```bash
kioku-mesh mcp install --client claude-code
claude mcp list   # expect: kioku_mesh: ... - ✓ Connected
```

raw command を確認・調整したい場合の手動 equivalent：

```bash
claude mcp add kioku_mesh -s user \
  -e ZENOH_CONNECT=tcp/127.0.0.1:7447 \
  -e MESH_MEM_AGENT_FAMILY=claude \
  -e MESH_MEM_CLIENT_ID=claude-code \
  -- /home/USER/.local/bin/kioku-mesh-mcp     # uv tool install のパス (手動 venv なら /home/USER/.venv/kioku-mesh/bin/kioku-mesh-mcp)

claude mcp list   # expect: kioku_mesh: ... - ✓ Connected
```

### `claude -p` から非対話 smoke を回す

非対話 `claude -p` セッションから MCP tool を呼ぶときは、
`--permission-mode bypassPermissions` を付ける必要があります。`-p` モードには
permission ダイアログが無いので、このフラグ無しだと最初の tool 呼び出しが
JSON 出力の `permission_denials` に落ちて、LLM が "permission needed" で
早期終了します。対話セッションには影響しません。

```bash
claude -p --permission-mode bypassPermissions --output-format json \
  "kioku-mesh MCP の save_observation で 'smoke' を保存して" \
  | jq '{result, denials:.permission_denials, error:.is_error}'
```

## Claude Desktop

- Linux: `~/.config/Claude/claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Claude Desktop は自分の config ファイルから `mcpServers` を読みます：

```json
{
  "mcpServers": {
    "kioku-mesh": {
      "command": "/home/USER/.local/bin/kioku-mesh-mcp",
      "env": {
        "ZENOH_CONNECT": "tcp/localhost:7447",
        "MESH_MEM_AGENT_FAMILY": "claude",
        "MESH_MEM_CLIENT_ID": "claude-desktop"
      }
    }
  }
}
```

## Gemini CLI — `~/.gemini/settings.json`

`"MESH_MEM_AGENT_FAMILY": "gemini"`、`"MESH_MEM_CLIENT_ID": "gemini-cli"` で
同じパターン。

## Codex CLI / ChatGPT Desktop

Codex CLI は wrapper を優先します：

```bash
kioku-mesh mcp install --client codex-cli
```

ChatGPT Desktop やその他のクライアントでは、`chatgpt` などの family と、対応する
`*-desktop` / `*-cli` client id を手動 config に入れます。`observation_id` 空間は
共有なので、client id を取り違えても `search_memory --client-id` のフィルタが
効かなくなる程度で、ストレージは破損しません。

## オプション: session id の固定

launch hook を持つ agent は、自分が制御できる値（例: 会話 id）を
`MESH_MEM_SESSION_ID` に設定できます。未指定の場合 kioku-mesh は
`{YYYYMMDDTHHMMSSZ}-{short-uuid}` を起動時 1 回だけ自動生成してキャッシュします。

## Claude Code SessionStart hook

Claude Code は `~/.claude/settings.json` から `SessionStart` hook を読み込んで
実行できます。hook で「カレントプロジェクトの直近の kioku-mesh 活動」を取得して
新セッションの最初の prompt に注入できると、特に **別 PC で発生して Zenoh で
レプリケートされてきた活動** を起動時に拾えるので便利です。

本リポジトリには sample hook `scripts/hooks/session-start.sh` が同梱されています。
`~/.claude/hooks/` 配下に install します：

```bash
install -d ~/.claude/hooks
cp /ABSOLUTE/PATH/TO/kioku-mesh/scripts/hooks/session-start.sh \
  ~/.claude/hooks/session-start-kioku-mesh.sh
chmod +x ~/.claude/hooks/session-start-kioku-mesh.sh
```

そして `~/.claude/settings.json` に `SessionStart` hook エントリを追加：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/session-start-kioku-mesh.sh"
          }
        ]
      }
    ]
  }
}
```

sample script の挙動：

- `PROJECT=$(basename "$PWD")` でカレントプロジェクト名を取得
- 7 日前の UTC ISO8601 `--since` 時刻を埋め込み Python で算出
- `kioku-mesh search --project "$PROJECT" --since "$SINCE" --limit 10 --format markdown` を実行
- マッチがあれば見出し + markdown bullet を出力
- マッチが無ければ silent exit（セッション開始 reminder にノイズを足さない）

検証手順：

1. `kioku-mesh save ... -p "$PROJECT"` でいくつか観測を保存
2. そのプロジェクトディレクトリで新規 Claude Code セッションを起動
3. 最初の prompt に `## Recent kioku-mesh context ...` セクションが含まれていることを確認
4. hook が `~/.claude/settings.json` から読み込まれていることを確認したい場合は、Claude Code 内で `/hooks` を実行

## Claude Code 「save リマインダー」hook（任意・Issue #158）

長時間セッションでは LLM が durable な決定を `save_observation` で取り逃す
ことがあります。MCP server 側は `get_memory_status` の nudge で promote
していますが（[PR #160](https://github.com/h-wata/kioku-mesh/pull/160)）、
**context が落ちる直前** に発火する client 側リマインダーを足すと取りこぼし
がさらに減ります。

本リポジトリには `scripts/hooks/check-unsaved-decisions.sh` が同梱されています。
Claude Code セッションの transcript JSONL を stdin で受け取り、承認系の user
turn 数と `save_observation` ツール呼び出し数を比較して、差が大きいときだけ
1 段落のリマインダーを出します。自動 save は一切しません。

一度だけ install します：

```bash
install -d ~/.claude/hooks
cp /ABSOLUTE/PATH/TO/kioku-mesh/scripts/hooks/check-unsaved-decisions.sh \
  ~/.claude/hooks/check-unsaved-decisions.sh
chmod +x ~/.claude/hooks/check-unsaved-decisions.sh
```

`~/.claude/settings.json` に、context loss を挟む 2 イベント — `PreCompact`
（auto-compaction）と `UserPromptSubmit` の `^/clear` matcher — に登録します：

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/check-unsaved-decisions.sh"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "^/clear",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/check-unsaved-decisions.sh"
          }
        ]
      }
    ]
  }
}
```

デフォルトの `APPROVAL_REGEX` は英語 + 日本語をカバーします。別言語で
Claude Code とやり取りする場合は、スクリプト冒頭の regex を編集してください。
ZH / KO の starter pattern はファイル内コメントに記載しています。本
heuristic は本質的に言語ローカルなので、server 側機能ではなく **client 側
optional script** として配布しています。

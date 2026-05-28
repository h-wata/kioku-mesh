# MCP 登録

インストール済みの `kioku-mesh-mcp` console script を MCP クライアントに
登録します。インストール先の **絶対パス** を使ってください — `uv tool install`
で入れた場合は `~/.local/bin/kioku-mesh-mcp`、手動 venv の場合は
`~/.venv/mesh-mem/bin/kioku-mesh-mcp` が定位置です。PATH に依存する形式だと、
agent がデスクトップショートカット等から別環境で起動されたときに壊れます。
各 agent はそれぞれ自分の `MESH_MEM_CLIENT_ID` を持ち、`MESH_MEM_AGENT_FAMILY`
だけが同 family の兄弟間で共通になります。

## Claude Code

`claude mcp add` を使います。CLI は `~/.claude.json` だけを読むので、
そこに書く必要があります。`~/.claude/settings.json` の `mcpServers` 配下は
`claude mcp list` から **silent に無視** されるので、手で編集しないこと。

```bash
claude mcp add mesh_mem -s user \
  -e ZENOH_CONNECT=tcp/127.0.0.1:7447 \
  -e MESH_MEM_AGENT_FAMILY=claude \
  -e MESH_MEM_CLIENT_ID=claude-code \
  -- /home/USER/.local/bin/kioku-mesh-mcp     # uv tool install のパス (手動 venv なら /home/USER/.venv/mesh-mem/bin/kioku-mesh-mcp)

claude mcp list   # expect: mesh_mem: ... - ✓ Connected
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

`codex` / `chatgpt` family と、対応する `*-cli` / `*-desktop` client id を
当てるだけで、形は同じです。`observation_id` 空間は共有なので、client id を
取り違えても `search_memory --client-id` のフィルタが効かなくなる程度で、
ストレージは破損しません。

## オプション: session id の固定

launch hook を持つ agent は、自分が制御できる値（例: 会話 id）を
`MESH_MEM_SESSION_ID` に設定できます。未指定の場合 kioku-mesh は
`{YYYYMMDDTHHMMSSZ}-{short-uuid}` を起動時 1 回だけ自動生成してキャッシュします。

## Deferred tool loading — 保存の activation energy を下げる

一部の MCP client（例: Claude Code の `ToolSearch` deferred-tools 機構）は tool
スキーマを lazy ロードします。`save_observation` のような proactive tool は、agent が
「保存しよう」と思った瞬間に「スキーマをロードする」一手間を挟むことになり、
activation energy が上がります。長セッションではこれが「保存スキップ」方向に効きます。
kioku-mesh は **これを server 側で解決できません** — MCP に always-load hint が無く、
登録済み tool を常駐させるかどうかは client の裁量だからです。詳細な分析と上流への
提案は `docs/design/issue-104-mcp-eager-load-deferred-tools.md` を参照。

今すぐ取れる手段：

1. **tool 名が明示済み（既出の緩和）。** server `instructions` と各 tool docstring は
   `save_observation` を名前で呼んでいるため、deferred でも agent は「探す」のではなく
   既知の名前を 1 回取りに行くだけで済みます。
2. **プロジェクト memory で priming（最も実効的）。** プロジェクト `./CLAUDE.md`
   （または `~/.claude/CLAUDE.md`）に、tool を名前付きで呼ぶ 1 行ルールを足します。
   例: 「決定・バグ修正・発見・規約確立をしたら `mesh_mem` の `save_observation` を
   proactive に呼ぶ」。tool 名がプロンプトに常駐するので初回の deferred フェッチが
   確実に発火します。
3. **tool を pin（対応 client のみ）。** 特定 MCP tool を常時ロード / pin できる
   client なら、kioku-mesh の保存系 tool を pin します。

## Claude Code SessionStart hook

Claude Code は `~/.claude/settings.json` から `SessionStart` hook を読み込んで
実行できます。hook で「カレントプロジェクトの直近の kioku-mesh 活動」を取得して
新セッションの最初の prompt に注入できると、特に **別 PC で発生して Zenoh で
レプリケートされてきた活動** を起動時に拾えるので便利です。

本リポジトリには sample hook `scripts/hooks/session-start.sh` が同梱されています。
`~/.claude/hooks/` 配下に install します：

```bash
install -d ~/.claude/hooks
cp /ABSOLUTE/PATH/TO/mesh-mem/scripts/hooks/session-start.sh \
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

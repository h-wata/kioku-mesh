# MCP registration

Register the installed `mesh-mem-mcp` console script. Use the **absolute path** inside the venv — the PATH-dependent form breaks when agents are launched from a desktop shortcut with a different environment. Each agent carries its own `MESH_MEM_CLIENT_ID`; only `MESH_MEM_AGENT_FAMILY` is shared across siblings of the same family.

## Claude Code

Use `claude mcp add` — it writes to `~/.claude.json`, which is the only location the CLI actually reads. Entries placed under `mcpServers` in `~/.claude/settings.json` are silently ignored by `claude mcp list`, so do not hand-edit that file for MCP registration.

```bash
claude mcp add mesh_mem -s user \
  -e ZENOH_CONNECT=tcp/127.0.0.1:7447 \
  -e MESH_MEM_AGENT_FAMILY=claude \
  -e MESH_MEM_CLIENT_ID=claude-code \
  -- /home/USER/.venv/mesh-mem/bin/mesh-mem-mcp

claude mcp list   # expect: mesh_mem: ... - ✓ Connected
```

### Non-interactive smoke from `claude -p`

When invoking MCP tools from a non-interactive `claude -p` session, pass
`--permission-mode bypassPermissions`. In `-p` mode there is no permission
dialog, so without this flag the first tool call lands in
`permission_denials` of the JSON output and the LLM exits early with a
"permission needed" message. The flag does NOT affect interactive
sessions.

```bash
claude -p --permission-mode bypassPermissions --output-format json \
  "mesh-mem MCP の save_observation で 'smoke' を保存して" \
  | jq '{result, denials:.permission_denials, error:.is_error}'
```

## Claude Desktop

- Linux: `~/.config/Claude/claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Claude Desktop does read `mcpServers` from its own config file:

```json
{
  "mcpServers": {
    "mesh-mem": {
      "command": "/home/USER/.venv/mesh-mem/bin/mesh-mem-mcp",
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

`"MESH_MEM_AGENT_FAMILY": "gemini"`, `"MESH_MEM_CLIENT_ID": "gemini-cli"`.

## Codex CLI / ChatGPT Desktop

Follow the same pattern with `codex` / `chatgpt` family and the matching `*-cli` / `*-desktop` client id. The `observation_id` space is shared; mis-tagging the client id just makes `search_memory --client-id` filters useless — it does not corrupt storage.

## Optional: session id pinning

Agents that expose a launch hook can set `MESH_MEM_SESSION_ID` to a value they control (e.g. the conversation id). When unset, mesh-mem autogenerates `{YYYYMMDDTHHMMSSZ}-{short-uuid}` once per process and caches it.

## Claude Code SessionStart hook

Claude Code supports `SessionStart` hooks from `~/.claude/settings.json`. A
hook can load recent mesh-mem activity from the current project and inject it
into the first prompt of a new session, which is especially useful when the
activity happened on another PC and replicated through Zenoh.

This repo ships a sample hook at `scripts/hooks/session-start.sh`. Install it
under `~/.claude/hooks/`:

```bash
install -d ~/.claude/hooks
cp /ABSOLUTE/PATH/TO/mesh-mem/scripts/hooks/session-start.sh \
  ~/.claude/hooks/session-start-mesh-mem.sh
chmod +x ~/.claude/hooks/session-start-mesh-mem.sh
```

Then add a `SessionStart` hook entry to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/session-start-mesh-mem.sh"
          }
        ]
      }
    ]
  }
}
```

What the sample does:

- derives `PROJECT=$(basename "$PWD")`
- computes a UTC ISO8601 `--since` timestamp for 7 days ago via embedded Python
- runs `mesh-mem search --project "$PROJECT" --since "$SINCE" --limit 10 --format markdown`
- prints a top-level heading plus markdown bullets when matches exist
- stays silent when no rows match, so the session reminder does not get noise

To verify it:

1. Save a few observations under the current project with `mesh-mem save ... -p "$PROJECT"`.
2. Start a fresh Claude Code session in that project.
3. Confirm the first prompt contains a `## Recent mesh-mem context ...` section.
4. Run `/hooks` in Claude Code if you want to confirm the hook is loaded from `~/.claude/settings.json`.

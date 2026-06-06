#!/usr/bin/env bash
# Optional Claude Code integration for kioku-mesh (Issue #158, Phase 4).
#
# Fires from a `PreCompact` hook and/or a `UserPromptSubmit` hook with a
# `^/clear` matcher, so it nudges right before Claude Code is about to lose
# session context. Reads the per-session JSONL transcript that Claude Code
# passes via stdin, counts approval-like user turns vs. `save_observation`
# tool calls, and prints a reminder when the gap is large.
#
# This script is a **client-side heuristic** layered on top of the
# language-agnostic protocol shipped in the MCP server `_INSTRUCTIONS`
# (PR #159) and the per-session nudge inside `get_memory_status` (PR #160).
# The MCP server side is fully internationalised; the regex below is
# necessarily local-language and therefore lives outside the server.
#
# It NEVER calls `save_observation` on its own — the LLM remains in charge
# of judging whether the unsaved content qualifies under the SKIP rules.
#
# Customise APPROVAL_REGEX for your primary chat language(s). Defaults
# cover English + Japanese. Examples for other languages:
#   ZH: 好的|可以|同意|上线吧|不要.*改成
#   KO: 좋아요|진행해주세요|동의합니다|그건 빼고
# Append with `|` to extend rather than replace.

set -u

APPROVAL_REGEX='(^|[^A-Za-z])(OK|Ok|ok|approved|ship it|sounds good|go ahead|do it|lets do|let'\''s do)([^A-Za-z]|$)|お願い|採用|いいね|そうして|そうしてほしい|やって|承認|公開してください|public化|それで進め|マージして|出してください'

# When stdin is empty (manual run / smoke), exit cleanly so the hook does
# not block the caller. Real PreCompact / UserPromptSubmit events always
# deliver a JSON payload with `transcript_path`.
payload=$(cat)
[ -z "$payload" ] && exit 0

transcript=$(printf '%s' "$payload" | jq -r '.transcript_path // empty' 2>/dev/null)
[ -z "$transcript" ] && exit 0
[ ! -f "$transcript" ] && exit 0

# User-text content lives under `.message.content`, either as a plain string
# or as an array with `{type: "text"}` parts. Skip the system-injected
# `<command-name>` style entries — those are local commands, not user prose.
approvals=$(jq -r '
  select(.type == "user")
  | (.message.content
      | if type == "string" then .
        elif type == "array" then
          map(select(.type == "text") | .text) | join(" ")
        else "" end)
  | select(. != "" and (startswith("<") | not))
' "$transcript" 2>/dev/null | grep -cE "$APPROVAL_REGEX")

# Count any tool_use whose name contains `save_observation` so this stays
# stable across the kioku-mesh MCP server-name prefix (`mcp__kioku_mesh__`,
# legacy `mcp__mesh_mem__`, etc.).
saves=$(jq -r '
  select(.type == "assistant")
  | (.message.content // [])
  | if type == "array" then
      .[] | select(.type == "tool_use") | .name
    else empty end
' "$transcript" 2>/dev/null | grep -c save_observation)

should_warn=0
if [ "$approvals" -ge 1 ] && [ "$saves" -eq 0 ]; then
  should_warn=1
elif [ "$approvals" -ge 4 ] && [ "$approvals" -gt $((saves * 2)) ]; then
  should_warn=1
fi

if [ "$should_warn" -eq 1 ]; then
  cat <<EOF
[kioku-mesh] approval-like user turns=${approvals}, save_observation calls=${saves}.
Before this context is dropped, review whether any decision / preference /
bug root cause / pattern from this session is still unsaved and worth
``save_observation``. Skip if everything that mattered is already in a PR,
ADR, commit message, or earlier save.
EOF
fi

exit 0

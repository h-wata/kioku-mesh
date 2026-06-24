# Messaging MVP Design — Issue #185

Date: 2026-06-24
Author: W4 / Codex

## 概要

Issue #185 は ADR-0022 の初期 MVP を、実装可能な仕様へ落とすための設計メモである。対象は direct 1:1 配送、MCP poll 受信、TTL 付き短期 inbox spool、最小 presence、opt-in tmux adapter、`save_observation` 昇格ブリッジに限定する。broadcast と topic/channel は direct 運用上の不足が見えてから追加する。

MVP の基本方針は「メッセージは短期フロー、memory は長期ストック」である。Zenoh は配送と短期 spool の source として使うが、`mem/**` とは別 namespace の `msg/**` を使い、memory の `Observation` / tombstone / GC と混ぜない。永続化したい内容だけが bridge 経由で `save_observation` に昇格する。

ADR-0023 に従い、実装は `core` の Zenoh session / identity / config / visibility 解決を共有し、`messaging` は `memory` を直接 import しない。昇格だけは `bridge` が `messaging` と `memory` を接続する。

## Zenoh key 設計

### Namespace

Messaging は `mem/**` とは分離し、次の prefix を使う。

```text
msg/mesh/...
msg/team/{team_id}/...
msg/user/{user_id}/...
```

これは ADR-0019 の visibility tier と同じ届く範囲を表す。`user_id` / `team_id` は tool 引数にせず、`core.config` と host-side config から解決する。`mesh` は scope id を持たない。

### MVP key shapes

```text
# session_id 宛 direct inbox
msg/{scope}/inbox/session/{recipient_session_id}/{msg_id}

# agent_id 宛 direct inbox
msg/{scope}/inbox/agent/{recipient_agent_id}/{msg_id}

# ack。receiver session ごとに 1 key
msg/{scope}/ack/{msg_id}/{recipient_session_id}

# presence
msg/{scope}/presence/{agent_id}/{session_id}
```

`{scope}` は次のいずれかに展開する。

```text
mesh
team/{team_id}
user/{user_id}
```

Selectors:

```text
# check_messages for current process
msg/**/inbox/session/{current_session_id}/**
msg/**/inbox/agent/{current_agent_id}/**

# ack lookup for filtering
msg/**/ack/{msg_id}/{current_session_id}

# presence lookup
msg/**/presence/**
msg/team/{team_id}/presence/**
msg/user/{user_id}/presence/**
```

`agent_id` は MVP では sanitized `client_id` を既定値にする。将来、同じ client_id の複数 instance を明示的に分けたい場合は `messaging.agent_id` config を追加するが、MVP では `client_id` と `session_id` の組み合わせで十分に一意性を取る。

### 比較した key 案

案 A: `msg/{scope}/inbox/{agent_id}/{session_id}/{msg_id}`

- 長所: 1 本の key shape で agent と session の両方を表しやすい。
- 短所: agent 宛か session 宛かが曖昧になり、agent 宛 broadcast 的に複数 session が読む場合の ack 粒度が崩れる。

案 B: `msg/{scope}/inbox/{recipient_kind}/{recipient_id}/{msg_id}`（推奨）

- 長所: `session` 宛と `agent` 宛を明確に分けられる。`check_messages` は自分の session id と agent id の 2 selector を読むだけでよい。
- 長所: `ack/{msg_id}/{recipient_session_id}` により agent 宛メッセージでも実際に読んだ session を記録できる。
- 短所: check 時に 2 selector を読む必要がある。

案 C: `msg/{scope}/direct/{sender}/{recipient}/{seq}`

- 長所: sender 単位の順序を key に埋め込める。
- 短所: dedup id と retry idempotency が弱くなる。recipient が agent/session のどちらかを別 field に逃がす必要がある。

MVP は案 B を採用する。

## メッセージスキーマ（JSON）

Inbox payload は UTF-8 JSON とする。未知 field は forward compatibility のため reader が無視できるようにする。

```json
{
  "schema_version": 1,
  "msg_id": "018f6b7a2d7f4b6db4f2f7f6c6b3d8e9",
  "kind": "direct",
  "scope": {
    "visibility": "team",
    "scope_id": "kioku-mesh"
  },
  "sender": {
    "agent_id": "codex-cli",
    "agent_family": "codex",
    "client_id": "codex-cli",
    "pc_id": "31b3...",
    "session_id": "20260624T010203Z-a1b2c3d4",
    "host": "devbox"
  },
  "recipient": {
    "kind": "session",
    "agent_id": "",
    "session_id": "20260624T020304Z-b2c3d4e5"
  },
  "created_at": "2026-06-24T01:02:03.123456Z",
  "expires_at": "2026-06-24T01:17:03.123456Z",
  "ttl_sec": 900,
  "sender_seq": 42,
  "priority": "normal",
  "subject": "status request",
  "body": "Please report current task status.",
  "content_type": "text/plain",
  "requires_ack": true,
  "delivery_adapters": ["mcp"],
  "reply_to": null,
  "correlation_id": "task-216",
  "metadata": {}
}
```

Field definitions:

| Field | Type | Required | Notes |
|---|---:|---:|---|
| `schema_version` | int | yes | MVP は `1`。 |
| `msg_id` | string | yes | 32 hex UUID。Retry は同じ `msg_id` / key へ再 put して idempotent にする。 |
| `kind` | string | yes | MVP は `direct` のみ。 |
| `scope.visibility` | string | yes | `user` / `team` / `mesh`。 |
| `scope.scope_id` | string | yes | `user` / `team` は resolved id、`mesh` は空文字。 |
| `sender.*` | object | yes | host/server 側 identity。LLM 入力ではなく送信 process が埋める。 |
| `recipient.kind` | string | yes | `agent` または `session`。 |
| `recipient.agent_id` | string | conditional | `recipient.kind == "agent"` のとき必須。 |
| `recipient.session_id` | string | conditional | `recipient.kind == "session"` のとき必須。 |
| `created_at` | string | yes | UTC ISO。 |
| `expires_at` | string | yes | UTC ISO。Reader は期限切れを返さない。 |
| `ttl_sec` | int | yes | default 900、min 30、max 86400。 |
| `sender_seq` | int | no | sender session 内の best-effort monotonic sequence。順序表示用で、厳密配送保証には使わない。 |
| `priority` | string | no | `low` / `normal` / `high`。MVP は sort の tie-break 程度。 |
| `subject` | string | no | 短い見出し。 |
| `body` | string | yes | MVP は text。最大サイズは 64 KiB を推奨上限にする。 |
| `content_type` | string | yes | MVP は `text/plain`。 |
| `requires_ack` | bool | yes | MVP は default `true`。 |
| `delivery_adapters` | list[string] | no | `mcp`, `tmux`, `hook`。未指定なら `["mcp"]`。 |
| `reply_to` | string or null | no | 返信元 `msg_id`。 |
| `correlation_id` | string | no | タスク ID 等。 |
| `metadata` | object | no | adapter-specific ではない軽量 metadata。 |

Ack payload:

```json
{
  "schema_version": 1,
  "msg_id": "018f6b7a2d7f4b6db4f2f7f6c6b3d8e9",
  "acked_at": "2026-06-24T01:04:00.000000Z",
  "recipient_session_id": "20260624T020304Z-b2c3d4e5",
  "recipient_agent_id": "codex-cli",
  "pc_id": "31b3...",
  "status": "acknowledged"
}
```

## inbox spool 仕様（TTL/ack/重複/再送）

### 推奨方針

MVP は Zenoh storage backed short spool + local SQLite inbox index を採用する。

- Zenoh `msg/**` が短期 spool の source。
- receiver は `check_messages` で current session / agent 宛 key を読み、local inbox index に cache して返す。
- `ack_message` は local index を acked にし、`msg/{scope}/ack/{msg_id}/{recipient_session_id}` を put する。
- `expires_at` を過ぎた message は返さない。GC は Phase 1 では local index から消すだけでよく、Zenoh storage 側の TTL purge は後続でよい。

### TTL の持ち方

比較:

1. Zenoh attachment / transport metadata に TTL を持つ
   - 長所: payload が薄い。
   - 短所: storage / reader / CLI の互換性が落ち、fallback scan で意味を失いやすい。
2. payload field に `expires_at` / `ttl_sec` を持つ（推奨）
   - 長所: どの reader でも期限判定できる。JSON schema と test が単純。
   - 短所: Zenoh storage が自動削除しないため GC が必要。
3. publisher timeout だけで表現する
   - 長所: 実装が薄い。
   - 短所: offline / turn 中 receiver の spool 要件を満たせない。

MVP は 2 を採用する。default TTL は 15 分 (`900` 秒)、最小 30 秒、最大 24 時間とする。tmux adapter を有効にする場合でも TTL は同じで、入力注入が失敗したら MCP poll で回収できる。

### Ack の仕組み

比較:

1. delete-on-read
   - 却下。Zenoh storage の key を消す操作が reader 全体に影響し、agent 宛メッセージを複数 session が読む場合に不自然。
2. expiry-only
   - 却下。sender が読了を知れず、receiver も再表示抑制を local state に完全依存する。
3. explicit ack key + local ack state（推奨）
   - 採用。`ack_message` が local index と Zenoh ack key の両方を更新する。

Ack key は message 本体を消さない。receiver は local ack state と ack key の両方を見て、ack 済み message を既定では返さない。ack put に失敗しても local ack は残し、次回 `ack_message` または background sync で再 put できる。

### 重複検知

- primary key は `msg_id`。
- local inbox index は `msg_id + recipient_session_id` を unique にする。
- 同じ key への retry put は同一 message と見なす。payload が同じなら no-op、payload が違う場合は warning として最初に見た payload を保持する。
- `check_messages` は Zenoh scan 結果を local index へ upsert してから、unacked かつ unexpired の message だけ返す。

### 再送戦略

比較:

1. sender 側 retry
   - 採用。Zenoh put の retryable failure に対して、memory の pending_puts と同じ思想で local pending message queue に入れる。
2. receiver 側 NAK
   - MVP では採用しない。missing detection と ordering state が増える。
3. TTL 切れ即破棄
   - 採用。ただし破棄は "delivery abandoned" であり error ではない。必要なら sender は新しい `msg_id` で再送する。

MVP では sender retry は best-effort で、`requires_ack=true` でも ack がないことを自動再送の条件にはしない。Ack timeout / resend policy は運用を見て Phase 1.5 以降で追加する。

## 配送順序保証方針

MVP は strict ordering を保証しない。方針は best-effort ordering + optional `sender_seq` である。

根拠:

- Zenoh pub/sub の到着順は同一 session / 同一 path の近傍では期待できても、multi-router / offline spool / retry / local index rebuild を跨ぐと end-to-end FIFO として説明できない。
- agent coordination の MVP は短い依頼・状況共有が主用途で、チャットログの完全順序よりも「取りこぼさない」「重複しない」「ack できる」が重要。
- strict sequence を要求すると missing seq 待ち・NAK・reorder buffer・timeout が必要になり、Issue #185 の初期価値を遅らせる。

Implementation rule:

- sender session は process-local monotonic `sender_seq` を付けてもよい。
- `check_messages` は `(created_at, sender.session_id, sender_seq, msg_id)` で昇順 sort する。
- `sender_seq` の gap があっても待たない。返却時に `ordering_note` または debug log で見えるようにする程度に留める。

## presence schema

Presence は宛先解決用の短期 state であり、認可 token ではない。

Key:

```text
msg/{scope}/presence/{agent_id}/{session_id}
```

Payload:

```json
{
  "schema_version": 1,
  "agent_id": "claude-code-worker2",
  "agent_family": "claude",
  "client_id": "claude-code-worker2",
  "pc_id": "31b3...",
  "session_id": "20260624T020304Z-b2c3d4e5",
  "host": "worker-host",
  "pid": 12345,
  "cwd": "/home/gisen/work/kioku-mesh",
  "project": "kioku-mesh",
  "last_seen": "2026-06-24T02:03:30.000000Z",
  "expires_at": "2026-06-24T02:05:00.000000Z",
  "ttl_sec": 90,
  "capabilities": ["mcp_poll", "ack", "promote_to_memory"],
  "delivery_adapters": [
    {
      "type": "mcp",
      "enabled": true
    },
    {
      "type": "tmux",
      "enabled": false,
      "adapter_id": "host-a:%6"
    }
  ],
  "scopes": ["user/hwata", "team/kioku-mesh"],
  "metadata": {}
}
```

Presence update interval: 30 seconds.

Presence TTL: 90 seconds.

Publication scope:

- `user`: publish by default when `user_id` is configured.
- `team`: publish when `team_id` is configured and project/team config selects team participation.
- `mesh`: off by default for presence. Enable only with `messaging.presence.publish_mesh: true` because mesh presence reveals host/session topology to every peer.

`check_messages` does not require presence. Presence is for sender-side discovery and tmux adapter targeting.

## MCP tool シグネチャ

### `check_messages`

```python
def check_messages(
    limit: int = 20,
    visibility: str = '',
    include_acked: bool = False,
    include_expired: bool = False,
    since_iso: str = '',
) -> str: ...
```

Arguments:

- `limit`: 1..100。default 20。
- `visibility`: `''`, `user`, `team`, `mesh`。空なら reachable scopes を読む。`user_id` / `team_id` は server-side config で解決する。
- `include_acked`: default false。
- `include_expired`: default false。debug 用。
- `since_iso`: optional lower bound for `created_at`。

Return shape は JSON string を推奨する。

```json
{
  "messages": [
    {
      "msg_id": "018f...",
      "subject": "status request",
      "body": "Please report current task status.",
      "created_at": "2026-06-24T01:02:03.123456Z",
      "expires_at": "2026-06-24T01:17:03.123456Z",
      "scope": "team/kioku-mesh",
      "sender": {
        "agent_id": "codex-cli",
        "session_id": "20260624T010203Z-a1b2c3d4"
      },
      "recipient": {
        "kind": "session",
        "session_id": "20260624T020304Z-b2c3d4e5"
      },
      "acked": false,
      "delivery_adapters": ["mcp"]
    }
  ],
  "count": 1,
  "truncated": false
}
```

### `ack_message`

```python
def ack_message(
    msg_id: str,
    visibility: str = '',
) -> str: ...
```

Arguments:

- `msg_id`: full 32-hex id。short id は不可。
- `visibility`: 空なら reachable scopes から該当 message を探す。明示時はその scope に限定。

Return:

```text
acked: <msg_id> (scope=team/kioku-mesh)
```

`ack_message` は current process の `session_id` で ack する。LLM が recipient identity を指定する余地を作らない。

### 送信 API

Issue #185 の受信正経路は `check_messages` / `ack_message` だが、実装には送信面が必要である。MVP では CLI または internal API として次を用意する。

```python
def send_message(
    body: str,
    recipient_agent_id: str = '',
    recipient_session_id: str = '',
    visibility: str = '',
    ttl_sec: int = 900,
    subject: str = '',
    priority: str = 'normal',
    delivery_adapters: list[str] | None = None,
) -> str: ...
```

`recipient_agent_id` と `recipient_session_id` はどちらか一方を必須にする。MCP tool として公開するかは別判断でよいが、CLI / tests / tmux adapter から使える core API は Phase 1 で必要になる。

## tmux adapter opt-in 機構

tmux adapter は remote input injection に近いので既定 off とする。MCP poll が正経路で、tmux は明示 opt-in の補助経路である。

Config keys:

```yaml
messaging:
  tmux:
    enabled: false
    allowed_panes:
      - session: "ros-agents"
        window: "0"
        pane: "%6"
        scopes: ["team/kioku-mesh"]
        allowed_senders: ["codex-cli", "claude-code-worker2"]
        inject_mode: "paste-enter"
```

Environment override for tests only:

```text
MESH_MEM_MESSAGING_TMUX=1
```

Enable flow:

1. User runs a future command such as `kioku-mesh messaging tmux allow --pane %6 --scope team`.
2. CLI writes the exact pane id and scope into config.
3. Presence advertises tmux adapter only for that exact pane and scope.
4. Local subscriber checks message scope, sender allowlist, recipient session/agent match, and pane allowlist before injection.
5. Injection writes one bounded text block. No shell execution, no interpolation, no arbitrary tmux target from message payload.

Security constraints:

- Never enable by default.
- Message payload must not choose tmux target.
- Adapter must reject messages larger than a conservative limit, e.g. 8 KiB for direct injection.
- Adapter should prefer paste-buffer + Enter over raw per-character `send-keys` when available.
- Adapter should record local delivery status, but ack still requires `ack_message` or an explicit adapter ack after successful injection. Injection does not mean the agent semantically processed the message.

## save_observation 昇格ブリッジ

Bridge belongs in `src/mesh_mem/bridge/`, not in `messaging`, to preserve ADR-0023 layering.

MVP principle: no automatic memory write for every message.

Promotion triggers:

- User or agent explicitly calls a future `promote_message` command/tool.
- A message has metadata `metadata.promote_hint == true` and the receiving agent chooses to promote it.
- A thread summary is explicitly created from a set of message ids.

Promotion flow:

1. `bridge` loads message by `msg_id` from messaging inbox/index.
2. It verifies the receiver can read the message under current scope.
3. It builds `Observation.content` from message body plus minimal provenance.
4. It calls memory backend / `save_observation` path with explicit `visibility`.
5. It records `promoted_observation_id` in local message metadata and optionally publishes a `msg/{scope}/promotion/{msg_id}/{observation_id}` event in a later phase.

Default mapping:

| Message | Observation |
|---|---|
| `body` | `content` |
| `subject` | `subject` |
| `correlation_id`, `msg_id` | `references` |
| sender/recipient metadata | appended provenance block or `source_files` empty |
| `priority == high` | `importance` 3, otherwise 2 |
| explicit category | `memory_type`, default `note` |

Promotion must preserve the user's chosen memory visibility. If not specified, use `resolve_write_visibility('')` and surface the effective visibility in the result, matching current `save_observation`.

## セキュリティ・scope 判定ロジック

Messaging inherits ADR-0014 and ADR-0019:

- mTLS is opt-in transport authentication. Without Zenoh ACL, it authenticates peers at the transport boundary but does not make `user` / `team` a hard confidentiality boundary.
- visibility tiers are soft isolation by key/storage scope. They reduce accidental replication and storage but are not authorization by themselves.
- LLMs must not provide `user_id` / `team_id`. They may choose `visibility`; IDs are server-side resolved.

Send/read conditions:

| Scope | Send condition | Read condition | Notes |
|---|---|---|---|
| `user` | local `user_id` configured | same local `user_id` configured | Intended for one user's machines. |
| `team` | `team_id` configured by env/project/global config | same `team_id` configured | Default for project team coordination. |
| `mesh` | `messaging.allow_mesh_send: true` or explicit CLI flag | mesh reachable | Use cautiously; presence mesh publication remains separate opt-in. |

Presence policy:

- Publish user/team presence by default only for joined scopes.
- Publish mesh presence only with `messaging.presence.publish_mesh: true`.
- Presence must not include secrets, tokens, command lines, or full environment.

tmux adapter policy:

- Requires local config opt-in.
- Requires scope match and sender allowlist.
- Treat message body as untrusted text. It is input to an agent, not a shell command.
- Do not auto-ack semantic processing after injection unless adapter can observe explicit ack. For MVP, injection success may publish `delivery_status=injected`, but `ack_message` remains the semantic read acknowledgement.

## 実装 Phase 分割（Claude 実装側向け引継ぎ）

### Phase 1: inbox spool 実装（最小依存）

Goal: direct message write/read/ack data model without MCP exposure.

Files:

- `src/mesh_mem/messaging/models.py`
- `src/mesh_mem/messaging/keyspace.py`
- `src/mesh_mem/messaging/spool.py`
- `src/mesh_mem/messaging/local_index.py`
- `tests/test_messaging_keyspace.py`
- `tests/test_messaging_spool.py`

Implementation:

- Define `Message`, `Ack`, schema tolerant JSON parse.
- Build `msg/{scope}/...` keys.
- Use `core.transport.get_session()` for put/get.
- Implement local SQLite inbox index under `state_dir()/messaging/inbox.db`.
- Implement `send_message`, `check_inbox`, `ack_message` internal APIs.
- Implement pending sender retry for retryable Zenoh failures, modeled after memory pending queue but separate DB.

Tests:

- key builder and parser for mesh/team/user.
- TTL expired message not returned.
- duplicate `msg_id` upsert is idempotent.
- ack hides message by default.
- no import from `mesh_mem.memory` in `mesh_mem.messaging`.

### Phase 2: presence + MCP tools

Goal: expose receiving path to agents.

Files:

- `src/mesh_mem/messaging/presence.py`
- `src/mesh_mem/mcp_server.py`
- `tests/test_messaging_presence.py`
- `tests/test_mcp_messaging.py`

Implementation:

- Publish presence with 30s interval / 90s TTL.
- Add `check_messages` and `ack_message` MCP tools.
- Ensure tool args do not include `user_id`, `team_id`, `pc_id`, or `session_id`.
- Return JSON string for stable client parsing.

Tests:

- `check_messages` sees session and agent inbox.
- `ack_message` uses current session id.
- invalid `msg_id` rejected.
- team/user visibility errors mirror existing `save_observation` behavior.

### Phase 3: tmux adapter（opt-in）

Goal: local delivery adapter for explicitly allowed panes.

Files:

- `src/mesh_mem/messaging/tmux_adapter.py`
- `src/mesh_mem/core/config.py` or new `src/mesh_mem/messaging/config.py`
- CLI command in `src/mesh_mem/__main__.py`
- `tests/test_messaging_tmux_adapter.py`

Implementation:

- Read `messaging.tmux.allowed_panes` config.
- Match message scope, sender allowlist, recipient.
- Inject bounded text into exact tmux pane.
- Publish local delivery status only; do not treat injection as semantic ack.

Tests:

- disabled by default.
- rejects unlisted pane / sender / scope.
- escapes or passes payload as literal text.
- no shell execution.

### Phase 4: save_observation 昇格ブリッジ

Goal: explicit promotion of selected messages to memory.

Files:

- `src/mesh_mem/bridge/message_memory.py`
- optional MCP/CLI exposure in `mcp_server.py` / `__main__.py`
- `tests/test_message_memory_bridge.py`
- update `tests/test_layering.py` if needed to allow bridge -> messaging/memory while keeping memory <-> messaging direct imports forbidden.

Implementation:

- Load message by id.
- Validate current receiver can read it.
- Convert to `Observation`.
- Call memory backend only from bridge.
- Return observation id and effective visibility.

Tests:

- promotion writes expected observation.
- messaging layer does not import memory.
- duplicate promotion can either return existing promoted id or create a new observation only when explicitly requested.

## 未解決事項・ADR 追補候補

- Zenoh storage-level TTL purge for `msg/**`: MVP can filter expired messages client-side, but long-running meshes need a cleanup strategy.
- Hard authorization: ADR-0019 is soft isolation. Team/user confidentiality requires future Zenoh ACL tied to mTLS cert subject, not just key prefix.
- Mesh-scope presence default: this memo recommends off by default, but product UX may later want a visible "mesh roster".
- Sender ack timeout policy: MVP does not auto-resend solely because ack is missing. If users expect delivery receipts, add explicit delivery state and timeout semantics.
- Multi-session agent delivery: agent-level inbox may be read by multiple active sessions. MVP records ack per recipient session; later work may need "ack by any active session" aggregation.
- Message body size limit: this memo recommends 64 KiB for MCP poll and 8 KiB for tmux injection, but exact limits should be validated against Zenoh and client UX.

"""Messaging layer — ADR-0022 (ADR-0023 layering enforced).

Phase 1 exports:
  models      — Message, Ack, is_expired
  keyspace    — key builder functions for msg/** and inbox/** namespaces
  spool       — in-memory MessageSpool + send_message / check_inbox internal API
  local_index — SQLite ack state + msg_id dedup + ack_message internal API

Phase 2 exports (presence heartbeat, Zenoh bridge):
  presence     — PresenceManager (30s heartbeat / 90s TTL / scope isolation)
  zenoh_bridge — ZenohBridge (spool <-> Zenoh put/sub, 64 KiB body limit)

Phase 3+ (not yet implemented): tmux adapter.

Body size limits (Issue #202) live in ``limits``: 64 KiB body / 128 KiB envelope
for the MCP path, 8 KiB for tmux injection. Over-limit is rejected, not truncated.

memory モジュールへの直接依存は禁止（ADR-0023 参照）。
"""

from .keyspace import ack_key
from .keyspace import agent_inbox_key
from .keyspace import mesh_broadcast_key
from .keyspace import parse_scope_from_key
from .keyspace import session_inbox_key
from .keyspace import team_key
from .keyspace import user_key
from .limits import check_body_size
from .limits import MAX_BODY_BYTES
from .limits import MAX_ENVELOPE_BYTES
from .limits import MAX_TMUX_BODY_BYTES
from .limits import MessageBodyTooLarge
from .local_index import ack_message
from .local_index import LocalMessageIndex
from .models import Ack
from .models import is_expired
from .models import Message
from .presence import Presence
from .presence import PresenceManager
from .spool import check_inbox
from .spool import MessageSpool
from .spool import send_message
from .zenoh_bridge import ZenohBridge

__all__ = [
    'MAX_BODY_BYTES',
    'MAX_ENVELOPE_BYTES',
    'MAX_TMUX_BODY_BYTES',
    'Ack',
    'LocalMessageIndex',
    'Message',
    'MessageBodyTooLarge',
    'MessageSpool',
    'Presence',
    'PresenceManager',
    'ZenohBridge',
    'ack_key',
    'ack_message',
    'agent_inbox_key',
    'check_body_size',
    'check_inbox',
    'is_expired',
    'mesh_broadcast_key',
    'parse_scope_from_key',
    'send_message',
    'session_inbox_key',
    'team_key',
    'user_key',
]

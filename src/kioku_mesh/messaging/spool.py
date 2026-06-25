"""TTL-aware in-memory inbox spool and send/check internal API (Phase 1 — ADR-0022).

Phase 1 uses a pure in-memory dict.  Zenoh pub/sub is added in Phase 2.
messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

from .models import is_expired
from .models import Message


class MessageSpool:
    """Ephemeral in-memory store for outbound/inbound messages.

    put() is idempotent on msg_id — a duplicate is silently ignored, which
    makes retry-safe by design (same msg_id == same message).

    TTL enforcement is client-side: get() and list_active() filter expired
    messages without deleting them; call purge_expired() to reclaim memory.

    # TODO(Phase 2): back this spool with a Zenoh storage subscription.
    """

    def __init__(self) -> None:
        self._store: dict[str, Message] = {}

    def put(self, msg: Message) -> None:
        """Store msg; silently ignores duplicate msg_id (idempotent)."""
        if msg.msg_id not in self._store:
            self._store[msg.msg_id] = msg

    def get(self, msg_id: str) -> Message | None:
        """Return the message, or None if absent or TTL-expired."""
        msg = self._store.get(msg_id)
        if msg is None or is_expired(msg):
            return None
        return msg

    def list_active(self, scope: str | None = None) -> list[Message]:
        """Return all live (non-expired) messages, optionally filtered by scope."""
        result = []
        for msg in self._store.values():
            if is_expired(msg):
                continue
            if scope is not None and msg.scope != scope:
                continue
            result.append(msg)
        return result

    def remove(self, msg_id: str) -> None:
        """Delete a message by id (no-op if not found)."""
        self._store.pop(msg_id, None)

    def purge_expired(self) -> int:
        """Delete all TTL-expired messages; return the count removed."""
        expired = [mid for mid, msg in self._store.items() if is_expired(msg)]
        for mid in expired:
            del self._store[mid]
        return len(expired)


def send_message(spool: MessageSpool, msg: Message) -> str:
    """Put msg into spool and return its msg_id.

    # TODO(Phase 2): Zenoh publish/subscribe integration
    """
    spool.put(msg)
    return msg.msg_id


def check_inbox(spool: MessageSpool, scope: str) -> list[Message]:
    """Return active (non-expired) messages for the given scope.

    # TODO(Phase 2): Zenoh publish/subscribe integration
    """
    return spool.list_active(scope=scope)

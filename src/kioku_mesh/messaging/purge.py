"""Storage-level TTL purge for the messaging layer (Issue #215).

Implementation approach: lazy expire-delete (option c).

On each purge call, enumerates Zenoh ``msg/**`` storage, deserializes each
entry, and issues a ``session.delete`` for every expired message. The local
SQLite inbox index is also purged via
:meth:`~.local_index.LocalMessageIndex.purge_expired` to keep both stores
in sync.

Why lazy expire-delete over alternatives:
  a) Zenoh backend-level TTL: no per-message TTL hook in the Zenoh Python API.
     Config-level TTL would be global (same TTL for all messages), ignoring
     per-message ``expires_at`` / ``ttl_sec`` values.
  b) Periodic background GC: requires lifecycle management (thread + shutdown
     coordination) in the MCP server, which is otherwise stateless.
  d) Drain-path extension: ``drain_pending_puts`` targets pending memory
     replication, not messaging storage — wrong abstraction layer.

Lazy delete integrates naturally into the ``check_messages`` hot-path via
:func:`purge_expired_msgs`, and can also be triggered explicitly by callers
(e.g. the ``purge_expired_messages`` MCP tool or operator scripts).

messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from .models import is_expired
from .models import Message

if TYPE_CHECKING:
    import zenoh

    from .local_index import LocalMessageIndex

log = logging.getLogger(__name__)

_MSG_SCAN_SELECTOR = 'msg/**'


def purge_expired_msgs(
    session: 'zenoh.Session',
    index: 'LocalMessageIndex',
    *,
    now: datetime | None = None,
) -> int:
    """Delete expired messages from Zenoh storage and the local SQLite index.

    Scans the ``msg/**`` key space in Zenoh storage, identifies entries whose
    TTL has elapsed (via :func:`~.models.is_expired`), and issues an exact-key
    ``session.delete`` for each.  The local inbox index is also swept via
    :meth:`~.local_index.LocalMessageIndex.purge_expired` so the two stores
    remain consistent.

    Malformed payloads and individual delete failures are logged and skipped
    so one bad entry cannot abort the whole sweep.  A transport failure on
    the initial scan returns 0 immediately (conservative: never delete on
    ambiguity).

    Args:
        session: An open Zenoh session.
        index: The local messaging inbox index.
        now: Reference time for expiry checks (default: :func:`datetime.now`).

    Returns:
        Number of Zenoh keys deleted.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    expired_keys: list[str] = []
    try:
        for reply in session.get(_MSG_SCAN_SELECTOR, timeout=3.0):
            if not reply.ok:
                continue
            key = str(reply.ok.key_expr)
            try:
                json_str = reply.ok.payload.to_bytes().decode('utf-8')
                msg = Message.from_json(json_str)
            except Exception:  # noqa: BLE001 — skip malformed payloads
                log.debug(
                    'purge_expired_msgs: skipping malformed payload at %s',
                    key)
                continue
            if is_expired(msg):
                expired_keys.append(key)
    except Exception as e:  # noqa: BLE001
        log.warning('purge_expired_msgs: scan failed — skipping purge: %s', e)
        return 0

    purged = 0
    for key in expired_keys:
        try:
            session.delete(key)
            purged += 1
            log.debug('purge_expired_msgs: deleted %s', key)
        except Exception as e:  # noqa: BLE001
            log.warning('purge_expired_msgs: delete failed for %s: %s', key, e)

    try:
        index.purge_expired(now=now)
    except Exception as e:  # noqa: BLE001
        log.warning('purge_expired_msgs: local index purge failed: %s', e)

    return purged

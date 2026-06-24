"""SQLite-backed local ack state and msg_id dedup index for the messaging layer (Phase 1).

Mirrors the role of memory.local_index for observations, but scoped to messaging
and completely separate from the memory layer (ADR-0023).

messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
import sqlite3

from .models import Ack
from .models import Message


def _iso(dt: datetime) -> str:
    """Normalize to a consistent UTC ISO 8601 string (Z-suffix) for SQLite storage."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id               TEXT NOT NULL,
    recipient_session_id TEXT NOT NULL,
    scope                TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    expires_at           TEXT,
    is_acked             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (msg_id, recipient_session_id)
);
CREATE TABLE IF NOT EXISTS acks (
    msg_id               TEXT NOT NULL,
    recipient_session_id TEXT NOT NULL,
    acked_at             TEXT NOT NULL,
    PRIMARY KEY (msg_id, recipient_session_id)
);
"""


class LocalMessageIndex:
    """Local SQLite index for ack state and msg_id deduplication."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)
            conn.commit()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def register(self, msg: Message, recipient_session_id: str) -> bool:
        """Register msg for a recipient session; returns True if inserted, False if already known (dedup)."""
        with self._connect() as conn:
            try:
                conn.execute(
                    'INSERT INTO messages (msg_id, recipient_session_id, scope, created_at, expires_at)'
                    ' VALUES (?, ?, ?, ?, ?)',
                    (
                        msg.msg_id,
                        recipient_session_id,
                        msg.scope,
                        _iso(msg.created_at),
                        _iso(msg.expires_at) if msg.expires_at is not None else None,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def record_ack(self, ack: Ack) -> None:
        """Record an ack and mark the per-session row as acked.

        Raises ValueError if (msg_id, recipient_session_id) is not registered.
        """
        with self._connect() as conn:
            row = conn.execute(
                'SELECT 1 FROM messages WHERE msg_id = ? AND recipient_session_id = ?',
                (ack.msg_id, ack.recipient_session_id),
            ).fetchone()
            if row is None:
                raise ValueError(f'unknown msg_id: {ack.msg_id!r}')
            conn.execute(
                'INSERT OR REPLACE INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                (ack.msg_id, ack.recipient_session_id, _iso(ack.acked_at)),
            )
            conn.execute(
                'UPDATE messages SET is_acked = 1 WHERE msg_id = ? AND recipient_session_id = ?',
                (ack.msg_id, ack.recipient_session_id),
            )
            conn.commit()

    def is_acked(self, msg_id: str, recipient_session_id: str) -> bool:
        """Return True if this (msg_id, session) pair has been acked."""
        with self._connect() as conn:
            row = conn.execute(
                'SELECT 1 FROM acks WHERE msg_id = ? AND recipient_session_id = ?',
                (msg_id, recipient_session_id),
            ).fetchone()
            return row is not None

    def list_unacked(self, recipient_session_id: str, scope: str | None = None) -> list[str]:
        """Return msg_ids of unacked messages for a recipient session, optionally filtered by scope."""
        with self._connect() as conn:
            if scope is not None:
                rows = conn.execute(
                    'SELECT msg_id FROM messages WHERE is_acked = 0 AND recipient_session_id = ? AND scope = ?',
                    (recipient_session_id, scope),
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT msg_id FROM messages WHERE is_acked = 0 AND recipient_session_id = ?',
                    (recipient_session_id,),
                ).fetchall()
            return [row['msg_id'] for row in rows]

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete messages whose expires_at has passed; returns count removed.

        Client-side TTL purge only — Zenoh storage-level cleanup is deferred
        to a later phase (design memo Open Question #1).
        """
        effective_now = now if now is not None else datetime.now(timezone.utc)
        now_iso = _iso(effective_now)
        with self._connect() as conn:
            cursor = conn.execute(
                'DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at <= ?',
                (now_iso,),
            )
            conn.commit()
            return cursor.rowcount


def ack_message(
    index: LocalMessageIndex,
    msg_id: str,
    recipient_session_id: str,
) -> Ack:
    """Create an Ack object, record it in the index, and return it.

    Ack put to the Zenoh ack key is added in Phase 2 (design memo Open Question #4:
    ack timeout/resend policy is not enforced in Phase 1).
    # TODO(Phase 2): git push origin msg/{scope}/ack/{msg_id}/{recipient_session_id}
    """
    ack = Ack(msg_id=msg_id, recipient_session_id=recipient_session_id)
    index.record_ack(ack)
    return ack

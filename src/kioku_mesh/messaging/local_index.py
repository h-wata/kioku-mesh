"""SQLite-backed local ack state and msg_id dedup index for the messaging layer (Phase 1).

Mirrors the role of memory.local_index for observations, but scoped to messaging
and completely separate from the memory layer (ADR-0023).

messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
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


#: Minimum age of an acks row with no messages row before
#: :meth:`LocalMessageIndex.purge_orphan_acks` will delete it. Sized to be far
#: beyond any plausible ack-before-message delivery race (seconds), so the only
#: rows it can reach are ones genuinely stranded by the pre-#299 purge.
ORPHAN_ACK_GRACE_SEC = 86_400

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

    def find_scope(self, msg_id: str, recipient_session_id: str) -> str | None:
        """Return the scope for a registered (msg_id, recipient_session_id) pair, or None."""
        with self._connect() as conn:
            row = conn.execute(
                'SELECT scope FROM messages WHERE msg_id = ? AND recipient_session_id = ?',
                (msg_id, recipient_session_id),
            ).fetchone()
            return row['scope'] if row else None

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete messages whose expires_at has passed; returns count removed.

        Client-side TTL purge only — Zenoh storage-level cleanup is deferred
        to a later phase (design memo Open Question #1).

        In the same transaction, deletes the acks rows for exactly the
        ``(msg_id, recipient_session_id)`` pairs removed by this call (design
        doc docs/design/0201-messaging-ack-timeout-policy.md §4.2). Without
        this, a later re-registration of the same msg_id would find a stale
        acks row and be reported as already-acked, hiding it from the
        receiver with no error or warning.

        The deletion is deliberately scoped to those pairs. An acks row with
        no messages row is *not* proof of staleness: distributed delivery is
        not end-to-end FIFO, so an ack can legitimately be indexed before the
        message it acks. Sweeping every orphan here would silently drop such
        a row even on calls that expire nothing. Orphans left behind by code
        that predates this fix are handled by the explicit, grace-period
        guarded :meth:`purge_orphan_acks` instead.
        """
        effective_now = now if now is not None else datetime.now(timezone.utc)
        now_iso = _iso(effective_now)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            expired = conn.execute(
                'SELECT msg_id, recipient_session_id FROM messages WHERE expires_at IS NOT NULL AND expires_at <= ?',
                (now_iso,),
            ).fetchall()
            if not expired:
                conn.commit()
                return 0
            # Per-pair DELETE via the acks primary key — no scan of acks, so
            # the cost is proportional to what actually expired, not to how
            # many acks the receiver has accumulated (this runs on every
            # check_messages poll).
            conn.executemany(
                'DELETE FROM acks WHERE msg_id = ? AND recipient_session_id = ?',
                [(row['msg_id'], row['recipient_session_id']) for row in expired],
            )
            cursor = conn.execute(
                'DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at <= ?',
                (now_iso,),
            )
            removed = cursor.rowcount
            conn.commit()
            return removed

    def purge_orphan_acks(
        self,
        *,
        grace_sec: int = ORPHAN_ACK_GRACE_SEC,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> int:
        """Delete acks rows that have no messages row and are older than ``grace_sec``.

        Explicit maintenance path for acks rows stranded by :meth:`purge_expired`
        before it started removing them (Issue #299). It is deliberately *not*
        called from ``check_messages`` or :meth:`purge_expired`: it scans the
        acks table, and the only thing separating a stranded orphan from an ack
        that raced ahead of its own message is age — a race is reconciled in
        seconds, so anything older than the grace period is stale by
        construction.

        ``acked_at <= now - grace_sec`` is deleted (inclusive boundary, matching
        :meth:`purge_expired`). Returns the number of rows deleted, or, with
        ``dry_run=True``, the number that would be deleted.
        """
        effective_now = now if now is not None else datetime.now(timezone.utc)
        cutoff_iso = _iso(effective_now - timedelta(seconds=grace_sec))
        where = (
            ' FROM acks WHERE acked_at <= ? AND NOT EXISTS ('
            '  SELECT 1 FROM messages'
            '  WHERE messages.msg_id = acks.msg_id'
            '    AND messages.recipient_session_id = acks.recipient_session_id'
            ')'
        )
        with self._connect() as conn:
            if dry_run:
                row = conn.execute('SELECT COUNT(*)' + where, (cutoff_iso,)).fetchone()
                return int(row[0])
            cursor = conn.execute('DELETE' + where, (cutoff_iso,))
            removed = cursor.rowcount
            conn.commit()
            return removed


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

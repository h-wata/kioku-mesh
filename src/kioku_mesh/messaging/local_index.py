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


# Bumped when the messaging inbox schema changes. v1 is the original
# messages/acks pair; v2 adds the ack-state tables described in the N4 design
# (immutable msg_id + tombstone + pending ack-first + legacy isolation) and
# runs the one-time classification of pre-existing acks.
MESSAGING_SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS messaging_schema_version (
    version INTEGER PRIMARY KEY
);
-- An acknowledgement observed before its message. Deliberately NOT read by
-- is_acked: it becomes authoritative only when the message arrives and unit 2
-- promotes it inside the same transaction.
CREATE TABLE IF NOT EXISTS pending_acks (
    msg_id               TEXT NOT NULL,
    recipient_session_id TEXT NOT NULL,
    acked_at             TEXT NOT NULL,
    source_key           TEXT,
    first_seen_at        TEXT NOT NULL,
    PRIMARY KEY (msg_id, recipient_session_id)
);
-- Ids retired by expiry purge. Kept forever on purpose: a tombstone that is
-- garbage-collected stops the receiver from rejecting reuse of that id, which
-- is the enforcement unit 2 relies on.
CREATE TABLE IF NOT EXISTS message_tombstones (
    msg_id               TEXT NOT NULL,
    recipient_session_id TEXT NOT NULL,
    tombstoned_at        TEXT NOT NULL,
    reason               TEXT,
    PRIMARY KEY (msg_id, recipient_session_id)
);
-- Acks that had no message at upgrade time. Whether each one is purge residue
-- or a legitimate ack observed before its message cannot be decided from the
-- stored columns, so the ambiguity is recorded rather than guessed away.
CREATE TABLE IF NOT EXISTS legacy_unknown_acks (
    msg_id               TEXT NOT NULL,
    recipient_session_id TEXT NOT NULL,
    acked_at             TEXT NOT NULL,
    migrated_at          TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'unresolved',
    resolved_at          TEXT,
    resolution_note      TEXT,
    PRIMARY KEY (msg_id, recipient_session_id)
);
-- Append-only. Every recovery writes its before image here with the backup it
-- was taken against, so an operator can reconstruct what was changed and why.
CREATE TABLE IF NOT EXISTS recovery_audit (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id               TEXT NOT NULL,
    recipient_session_id TEXT NOT NULL,
    action               TEXT NOT NULL,
    before_json          TEXT NOT NULL,
    operator             TEXT,
    performed_at         TEXT NOT NULL,
    backup_path          TEXT NOT NULL
);
"""


def _stored_schema_version(conn: sqlite3.Connection) -> int:
    """Return the version recorded in the database, or 1 for a pre-versioning file.

    A v1 database has no version row at all, so "no row" and "version 1" are the
    same statement — the tables the old code created are exactly the v1 schema.
    """
    row = conn.execute('SELECT version FROM messaging_schema_version').fetchone()
    return int(row[0]) if row is not None else 1


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing inbox database up to :data:`MESSAGING_SCHEMA_VERSION`.

    The DDL above is additive and already applied, so the only work left is the
    one-time classification of acks that predate the new state model, plus
    stamping the version. Everything runs in a single transaction: an
    interrupted upgrade leaves the original ``acks`` table exactly as it was.

    Idempotent twice over — the version check skips the pass on reopen, and
    :func:`~.orphan_acks.classify_unmatched_acks` converges if it is called
    again anyway.

    Reopening is not a re-classification, though: once the version is stamped
    the pass is skipped, so an unmatched ack written by an old binary *after*
    the upgrade stays in ``acks`` until that function is called explicitly. The
    rollout quiesces old writers before upgrading for exactly this reason, and
    unit 2 is what stops such a row from being read as an acknowledgement.
    """
    from . import orphan_acks  # noqa: PLC0415 - avoids an import cycle at module load

    if _stored_schema_version(conn) >= MESSAGING_SCHEMA_VERSION:
        return
    try:
        conn.execute('BEGIN IMMEDIATE')
        moved = orphan_acks.classify_unmatched_acks(conn)
        orphan_acks.verify_classification(conn, moved)
        conn.execute('DELETE FROM messaging_schema_version')
        conn.execute('INSERT INTO messaging_schema_version (version) VALUES (?)', (MESSAGING_SCHEMA_VERSION,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class LocalMessageIndex:
    """Local SQLite index for ack state and msg_id deduplication."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)
            conn.commit()
            _migrate(conn)

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

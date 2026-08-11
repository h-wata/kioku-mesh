"""SQLite-backed local ack state and msg_id dedup index for the messaging layer (Phase 1).

Mirrors the role of memory.local_index for observations, but scoped to messaging
and completely separate from the memory layer (ADR-0023).

messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
import sqlite3
from typing import Any

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
# runs the one-time classification of pre-existing acks. v3 records the envelope
# a tombstoned id was retired with, so a later arrival on that id can be told
# apart from a plain retry of the same envelope.
MESSAGING_SCHEMA_VERSION = 3

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
    original_created_at  TEXT,
    original_expires_at  TEXT,
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


# --- ingress classification codes -------------------------------------------
#
# Every arrival lands on exactly one of these. They exist so that "the message
# is not in your inbox" always comes with a reason attached: the failure this
# unit fixes was a message being filtered out with nothing said about it.

#: A message not seen before. Delivered normally.
INGRESS_REGISTERED = 'registered'
#: The same message arriving again while it is still live (transport retry,
#: replication, a second selector matching the same key). Not re-registered.
INGRESS_DUPLICATE_LIVE = 'duplicate_live'
#: The id was retired by expiry purge and the envelope is unchanged — a retry
#: of something that has already expired. Withheld, and reported.
INGRESS_DUPLICATE_RETIRED = 'duplicate_retired'
#: The id was retired by expiry purge and the envelope differs, so the sender
#: reused a retired id for a different message. Withheld, and reported.
INGRESS_PROTOCOL_VIOLATION = 'protocol_violation'
#: The pair has a quarantined ack whose meaning is unknown. Withheld from
#: normal mail, reported with the payload, and resolvable only by an operator.
INGRESS_LEGACY_ACK_CONFLICT = 'legacy_ack_conflict'
#: The ack for this pair arrived before the message did; the pending ack is now
#: authoritative and the message counts as acknowledged.
INGRESS_ACK_FIRST_PROMOTED = 'ack_first_promoted'

#: Codes whose messages are withheld for a reason the caller has to be told.
INGRESS_DIAGNOSTIC_CODES = frozenset(
    {
        INGRESS_DUPLICATE_RETIRED,
        INGRESS_PROTOCOL_VIOLATION,
        INGRESS_LEGACY_ACK_CONFLICT,
    }
)

_INVENTORY_COMMAND = 'kioku-mesh messaging orphan-acks list --format json'


@dataclass(frozen=True)
class IngressResult:
    """What the index decided about one arriving message.

    ``suppressed`` is the single answer to "should this stay out of the normal
    message list", so callers never have to re-derive suppression from ack
    state — re-deriving it is what let the old code drop messages silently.
    """

    code: str
    msg_id: str
    recipient_session_id: str
    registered: bool
    acked: bool
    suppressed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    remedy: str | None = None

    @property
    def is_diagnostic(self) -> bool:
        return self.code in INGRESS_DIAGNOSTIC_CODES


def _stored_schema_version(conn: sqlite3.Connection) -> int:
    """Return the version recorded in the database, or 1 for a pre-versioning file.

    A v1 database has no version row at all, so "no row" and "version 1" are the
    same statement — the tables the old code created are exactly the v1 schema.
    """
    row = conn.execute('SELECT version FROM messaging_schema_version').fetchone()
    return int(row[0]) if row is not None else 1


def _add_missing_tombstone_columns(conn: sqlite3.Connection) -> None:
    """Add the v3 tombstone columns to a database created by the v2 code.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a v2
    database keeps the narrower tombstone table until it is widened here. The
    columns are nullable and appended, which is what keeps this additive: a v2
    reader sees the table it already knew.
    """
    existing = {row[1] for row in conn.execute('PRAGMA table_info(message_tombstones)')}
    for column in ('original_created_at', 'original_expires_at'):
        if column not in existing:
            conn.execute(f'ALTER TABLE message_tombstones ADD COLUMN {column} TEXT')


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
        _add_missing_tombstone_columns(conn)
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
        """Register msg for a recipient session; returns True if inserted, False if already known (dedup).

        Thin wrapper over :meth:`register_or_classify` so there is exactly one
        ingress path — a caller that only wants the dedup answer still goes
        through the classifier and cannot bypass tombstone or quarantine
        checks by accident.
        """
        return self.register_or_classify(msg, recipient_session_id).registered

    def register_or_classify(self, msg: Message, recipient_session_id: str) -> IngressResult:
        """Classify one arriving message and apply its consequences atomically.

        This is the single ingress point for the messaging layer. Classification
        and the write it implies (registering the message, promoting a pending
        ack) happen inside one ``BEGIN IMMEDIATE`` transaction, so two pollers
        racing on the same arrival cannot both register it or both promote the
        same pending ack.

        Every branch is a bounded primary-key lookup — at most four of them —
        because this runs on the poll hot path and must not grow with the size
        of the quarantine or the tombstone table.
        """
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                result = self._classify_locked(conn, msg, recipient_session_id)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

    def _classify_locked(
        self,
        conn: sqlite3.Connection,
        msg: Message,
        recipient_session_id: str,
    ) -> IngressResult:
        """Body of :meth:`register_or_classify`, run under the write lock."""
        pair = (msg.msg_id, recipient_session_id)
        created_iso = _iso(msg.created_at)
        expires_iso = _iso(msg.expires_at) if msg.expires_at is not None else None

        live = conn.execute(
            'SELECT is_acked FROM messages WHERE msg_id = ? AND recipient_session_id = ?',
            pair,
        ).fetchone()
        if live is not None:
            acked = self._acked_locked(conn, msg.msg_id, recipient_session_id)
            return IngressResult(
                code=INGRESS_DUPLICATE_LIVE,
                msg_id=msg.msg_id,
                recipient_session_id=recipient_session_id,
                registered=False,
                acked=acked,
                suppressed=acked,
            )

        tomb = conn.execute(
            'SELECT tombstoned_at, reason, original_created_at, original_expires_at'
            ' FROM message_tombstones WHERE msg_id = ? AND recipient_session_id = ?',
            pair,
        ).fetchone()
        if tomb is not None:
            return self._retired_result(tomb, msg, recipient_session_id, created_iso, expires_iso)

        legacy = conn.execute(
            'SELECT acked_at, migrated_at, state FROM legacy_unknown_acks'
            ' WHERE msg_id = ? AND recipient_session_id = ?',
            pair,
        ).fetchone()
        if legacy is not None and legacy['state'] == 'unresolved':
            # Register the payload so it is not lost and so an operator can
            # promote the quarantined ack onto a real message, but keep it out
            # of normal mail until the ambiguity is resolved one way or another.
            self._insert_message_locked(conn, msg, recipient_session_id, created_iso, expires_iso, is_acked=0)
            return IngressResult(
                code=INGRESS_LEGACY_ACK_CONFLICT,
                msg_id=msg.msg_id,
                recipient_session_id=recipient_session_id,
                registered=True,
                acked=False,
                suppressed=True,
                detail={
                    'acked_at': legacy['acked_at'],
                    'migrated_at': legacy['migrated_at'],
                    'state': legacy['state'],
                },
                remedy=(
                    f'A quarantined acknowledgement exists for this pair, so it is unknown whether the '
                    f'message was already read. Inspect it with `{_INVENTORY_COMMAND}`, then resolve the '
                    f'exact pair with `kioku-mesh messaging orphan-acks recover --msg-id {msg.msg_id} '
                    f'--session-id {recipient_session_id} --action release|promote --backup <new path> '
                    f'--execute`.'
                ),
            )

        pending = conn.execute(
            'SELECT acked_at, source_key, first_seen_at FROM pending_acks'
            ' WHERE msg_id = ? AND recipient_session_id = ?',
            pair,
        ).fetchone()
        if pending is not None:
            self._insert_message_locked(conn, msg, recipient_session_id, created_iso, expires_iso, is_acked=1)
            conn.execute(
                'INSERT OR REPLACE INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                (msg.msg_id, recipient_session_id, pending['acked_at']),
            )
            conn.execute('DELETE FROM pending_acks WHERE msg_id = ? AND recipient_session_id = ?', pair)
            return IngressResult(
                code=INGRESS_ACK_FIRST_PROMOTED,
                msg_id=msg.msg_id,
                recipient_session_id=recipient_session_id,
                registered=True,
                acked=True,
                suppressed=True,
                detail={'acked_at': pending['acked_at'], 'source_key': pending['source_key']},
            )

        self._insert_message_locked(conn, msg, recipient_session_id, created_iso, expires_iso, is_acked=0)
        return IngressResult(
            code=INGRESS_REGISTERED,
            msg_id=msg.msg_id,
            recipient_session_id=recipient_session_id,
            registered=True,
            acked=False,
            suppressed=False,
        )

    @staticmethod
    def _insert_message_locked(
        conn: sqlite3.Connection,
        msg: Message,
        recipient_session_id: str,
        created_iso: str,
        expires_iso: str | None,
        *,
        is_acked: int,
    ) -> None:
        conn.execute(
            'INSERT INTO messages (msg_id, recipient_session_id, scope, created_at, expires_at, is_acked)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            (msg.msg_id, recipient_session_id, msg.scope, created_iso, expires_iso, is_acked),
        )

    @staticmethod
    def _retired_result(
        tomb: sqlite3.Row,
        msg: Message,
        recipient_session_id: str,
        created_iso: str,
        expires_iso: str | None,
    ) -> IngressResult:
        """Decide between a retry of a retired message and reuse of its id.

        An id is retired for good once its message expires, so neither case is
        delivered. They are still worth telling apart: an unchanged envelope is
        a transport retry arriving late, while a changed one means a sender
        pinned a new message onto an id that is no longer available.
        """
        recorded_created = tomb['original_created_at']
        recorded_expires = tomb['original_expires_at']
        changed = [
            name
            for name, was, now in (
                ('created_at', recorded_created, created_iso),
                ('expires_at', recorded_expires, expires_iso),
            )
            if was is not None and was != now
        ]
        detail: dict[str, Any] = {
            'tombstoned_at': tomb['tombstoned_at'],
            'reason': tomb['reason'],
            'original_created_at': recorded_created,
            'original_expires_at': recorded_expires,
            'arriving_created_at': created_iso,
            'arriving_expires_at': expires_iso,
            'changed_fields': changed,
        }
        if changed:
            return IngressResult(
                code=INGRESS_PROTOCOL_VIOLATION,
                msg_id=msg.msg_id,
                recipient_session_id=recipient_session_id,
                registered=False,
                acked=False,
                suppressed=True,
                detail=detail,
                remedy=(
                    f'msg_id {msg.msg_id} was retired when its message expired and cannot carry a new '
                    f'message ({", ".join(changed)} changed). Resend with a new msg_id.'
                ),
            )
        return IngressResult(
            code=INGRESS_DUPLICATE_RETIRED,
            msg_id=msg.msg_id,
            recipient_session_id=recipient_session_id,
            registered=False,
            acked=False,
            suppressed=True,
            detail=detail,
            remedy=(
                f'msg_id {msg.msg_id} already expired and was purged, so this re-delivery is not shown '
                f'as new mail. Resend with a new msg_id if the content still matters.'
            ),
        )

    def record_remote_ack(self, ack: Ack, source_key: str | None = None) -> str:
        """Record an acknowledgement observed from outside this session.

        Returns ``'authoritative'`` when the message is present and the ack is
        recorded as usual, or ``'pending'`` when the ack arrived first and is
        parked until its message shows up. Parking it is the point: an ack with
        no message is exactly the shape that used to suppress live mail, so it
        is kept somewhere ``is_acked`` does not read.
        """
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                known = conn.execute(
                    'SELECT 1 FROM messages WHERE msg_id = ? AND recipient_session_id = ?',
                    (ack.msg_id, ack.recipient_session_id),
                ).fetchone()
                if known is not None:
                    self._write_ack_locked(conn, ack)
                    state = 'authoritative'
                else:
                    conn.execute(
                        'INSERT OR IGNORE INTO pending_acks'
                        ' (msg_id, recipient_session_id, acked_at, source_key, first_seen_at)'
                        ' VALUES (?, ?, ?, ?, ?)',
                        (
                            ack.msg_id,
                            ack.recipient_session_id,
                            _iso(ack.acked_at),
                            source_key,
                            _iso(datetime.now(timezone.utc)),
                        ),
                    )
                    state = 'pending'
                conn.commit()
                return state
            except BaseException:
                conn.rollback()
                raise

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
            self._write_ack_locked(conn, ack)
            conn.commit()

    @staticmethod
    def _write_ack_locked(conn: sqlite3.Connection, ack: Ack) -> None:
        conn.execute(
            'INSERT OR REPLACE INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
            (ack.msg_id, ack.recipient_session_id, _iso(ack.acked_at)),
        )
        conn.execute(
            'UPDATE messages SET is_acked = 1 WHERE msg_id = ? AND recipient_session_id = ?',
            (ack.msg_id, ack.recipient_session_id),
        )

    def is_acked(self, msg_id: str, recipient_session_id: str) -> bool:
        """Return True if this (msg_id, session) pair has been acked.

        Only an acknowledgement that still has its message counts. An ack row on
        its own proves nothing about a message that arrives later carrying the
        same pair — believing one is what made live mail disappear (N4) — so the
        message row is the anchor and the ack has to hang off it.
        """
        with self._connect() as conn:
            return self._acked_locked(conn, msg_id, recipient_session_id)

    @staticmethod
    def _acked_locked(conn: sqlite3.Connection, msg_id: str, recipient_session_id: str) -> bool:
        row = conn.execute(
            'SELECT 1 FROM messages m WHERE m.msg_id = ? AND m.recipient_session_id = ?'
            ' AND (m.is_acked = 1 OR EXISTS ('
            '   SELECT 1 FROM acks a WHERE a.msg_id = m.msg_id'
            '   AND a.recipient_session_id = m.recipient_session_id))',
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
        """Retire messages whose expires_at has passed; returns count removed.

        Retiring a pair means three things in one transaction: a tombstone that
        keeps the id from being reused, the removal of the pair's ack, and the
        removal of the message. Deleting the message alone — what this used to
        do — left the ack behind with nothing to vouch for it, which is how new
        unmatched acks kept being created after unit 1 quarantined the old ones.

        Client-side TTL purge only — Zenoh storage-level cleanup is deferred
        to a later phase (design memo Open Question #1).
        """
        effective_now = now if now is not None else datetime.now(timezone.utc)
        now_iso = _iso(effective_now)
        with self._connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                expiring = conn.execute(
                    'SELECT msg_id, recipient_session_id, created_at, expires_at FROM messages'
                    ' WHERE expires_at IS NOT NULL AND expires_at <= ?',
                    (now_iso,),
                ).fetchall()
                if not expiring:
                    conn.rollback()
                    return 0
                pairs = [(r['msg_id'], r['recipient_session_id']) for r in expiring]
                conn.executemany(
                    'INSERT OR IGNORE INTO message_tombstones'
                    ' (msg_id, recipient_session_id, tombstoned_at, reason,'
                    '  original_created_at, original_expires_at)'
                    " VALUES (?, ?, ?, 'expiry_purge', ?, ?)",
                    [
                        (r['msg_id'], r['recipient_session_id'], now_iso, r['created_at'], r['expires_at'])
                        for r in expiring
                    ],
                )
                conn.executemany('DELETE FROM acks WHERE msg_id = ? AND recipient_session_id = ?', pairs)
                conn.executemany('DELETE FROM messages WHERE msg_id = ? AND recipient_session_id = ?', pairs)
                conn.commit()
                return len(pairs)
            except BaseException:
                conn.rollback()
                raise


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

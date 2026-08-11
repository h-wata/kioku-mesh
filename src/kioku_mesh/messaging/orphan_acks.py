"""Classification, inventory and recovery for acks with no matching message.

Unit 1 of the N4 fix (design TASK-317-design, option C).

An ack row whose ``(msg_id, recipient_session_id)`` has no ``messages`` row is
ambiguous. It may be residue from the old purge bug, or it may be a legitimate
acknowledgement observed before its message arrived. The stored columns cannot
tell those apart, and age does not either — a message can sit undelivered for a
long time. So this module does not decide: it moves such rows into
``legacy_unknown_acks``, which nothing reads as an acknowledgement, and gives
operators a read-only inventory plus an exact-pair, backup-gated way to resolve
individual rows.

What is deliberately *not* here: any bulk delete, any age-based cleanup, and any
"this looks stale so it must be residue" heuristic. Deleting a legitimate
ack-first row loses a real acknowledgement, and no column in the database can
prove which rows those are.

messaging モジュールは memory モジュールを直接 import しない (ADR-0023).
"""

from __future__ import annotations

import base64
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import sqlite3

# Values that look like an attempt to select more than one row. Recovery takes
# an exact pair only, so these are refused up front rather than being treated as
# literal ids — a caller typing `--msg-id '*'` means "all of them", and the one
# thing this module must never do is act on "all of them".
_BULK_SELECTORS = frozenset({'', '*', '%', '?', 'all', 'any', 'none'})
_WILDCARD_CHARS = ('*', '%', '?')

_VALID_ACTIONS = ('release', 'promote')


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Migration-time classification
# ---------------------------------------------------------------------------


def classify_unmatched_acks(conn: sqlite3.Connection) -> int:
    """Move every ack without a matching message into ``legacy_unknown_acks``.

    Acks that do have a message stay exactly where they are and keep answering
    :meth:`~.local_index.LocalMessageIndex.is_acked`; only the unmatched ones
    move. The move is lossless — ``acked_at`` travels with the row — and
    converges when called again: a pair that is already quarantined keeps the
    time it was quarantined with, and a differing time on the incoming row is
    appended to ``resolution_note`` rather than being dropped or overwritten.

    Note this is not called again on its own. Once the schema version is
    stamped, reopening the database skips the pass entirely, so an ack written
    by an old binary *after* the upgrade stays in ``acks`` until something calls
    this function explicitly. The rollout procedure handles that by quiescing
    old writers before upgrading.

    Runs inside the caller's transaction so the whole upgrade commits or rolls
    back as one unit. Returns the number of rows moved.
    """
    orphans = conn.execute(
        'SELECT a.msg_id, a.recipient_session_id, a.acked_at'
        ' FROM acks a LEFT JOIN messages m'
        ' ON a.msg_id = m.msg_id AND a.recipient_session_id = m.recipient_session_id'
        ' WHERE m.msg_id IS NULL'
    ).fetchall()
    if not orphans:
        return 0
    migrated_at = _now_iso()
    _record_conflicting_ack_times(conn, orphans, observed_at=migrated_at)
    conn.executemany(
        'INSERT OR IGNORE INTO legacy_unknown_acks'
        ' (msg_id, recipient_session_id, acked_at, migrated_at, state)'
        " VALUES (?, ?, ?, ?, 'unresolved')",
        [(r[0], r[1], r[2], migrated_at) for r in orphans],
    )
    conn.executemany(
        'DELETE FROM acks WHERE msg_id = ? AND recipient_session_id = ?',
        [(r[0], r[1]) for r in orphans],
    )
    return len(orphans)


def _record_conflicting_ack_times(
    conn: sqlite3.Connection,
    orphans: list[sqlite3.Row],
    *,
    observed_at: str,
) -> None:
    """Keep an ``acked_at`` that a second classification pass would discard.

    The source row is deleted after the move, and ``INSERT OR IGNORE`` keeps the
    already-quarantined time, so a pair that gets a *different* time on a later
    pass would otherwise lose it silently. Which of the two times is real cannot
    be decided here, so both are kept and the operator sees the second one in
    the inventory.
    """
    for row in orphans:
        existing = conn.execute(
            'SELECT acked_at, resolution_note FROM legacy_unknown_acks WHERE msg_id = ? AND recipient_session_id = ?',
            (row[0], row[1]),
        ).fetchone()
        # Indexed access, not by name: the caller's connection may or may not
        # have a Row factory set.
        if existing is None or existing[0] == row[2]:
            continue
        note = f'also observed acked_at={row[2]} at {observed_at}'
        merged = f'{existing[1]} | {note}' if existing[1] else note
        conn.execute(
            'UPDATE legacy_unknown_acks SET resolution_note = ? WHERE msg_id = ? AND recipient_session_id = ?',
            (merged, row[0], row[1]),
        )


def verify_classification(conn: sqlite3.Connection, moved: int) -> None:
    """Fail the migration unless every ack still left is backed by a message.

    Checked rather than assumed: if the move left an unmatched row behind, the
    database would keep answering ``is_acked`` from a row nothing can vouch for,
    which is the bug this whole unit exists to remove. Raising here rolls the
    transaction back and leaves the original database untouched.
    """
    leftover = conn.execute(
        'SELECT COUNT(*) FROM acks a LEFT JOIN messages m'
        ' ON a.msg_id = m.msg_id AND a.recipient_session_id = m.recipient_session_id'
        ' WHERE m.msg_id IS NULL'
    ).fetchone()[0]
    if leftover:
        raise RuntimeError(
            f'refusing to complete the messaging schema migration: {leftover} ack row(s) still have no '
            'matching message after classification; the database was left unchanged.'
        )


# ---------------------------------------------------------------------------
# Inventory (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyAckEntry:
    """One quarantined pair, with the context an operator needs to judge it.

    ``age_days`` is reported because it is useful to a human, and for no other
    reason: nothing in this module treats it as evidence.
    """

    msg_id: str
    recipient_session_id: str
    acked_at: str
    migrated_at: str
    state: str
    has_live_message: bool
    has_tombstone: bool
    age_days: float | None = None
    resolved_at: str | None = None
    resolution_note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            'msg_id': self.msg_id,
            'recipient_session_id': self.recipient_session_id,
            'acked_at': self.acked_at,
            'migrated_at': self.migrated_at,
            'state': self.state,
            'has_live_message': self.has_live_message,
            'has_tombstone': self.has_tombstone,
            'age_days': self.age_days,
            'resolved_at': self.resolved_at,
            'resolution_note': self.resolution_note,
        }


@dataclass(frozen=True)
class InventoryPage:
    """One page of the inventory.

    ``migrated`` is False when the database predates the schema that has the
    quarantine table, which is a different statement from "there is nothing
    quarantined" and is reported as such instead of raising.
    """

    entries: list[LegacyAckEntry] = field(default_factory=list)
    next_cursor: str | None = None
    migrated: bool = True


@contextmanager
def open_read_only(db_path: str | Path) -> Generator[sqlite3.Connection, None, None]:
    """Open the inbox database in SQLite's read-only mode.

    Enforced by the connection URI rather than by convention, so an inventory
    query cannot touch the file's mtime or contents even by mistake. That
    matters because the inventory is meant to be safe to run against a live
    deployment before anyone has taken a backup.
    """
    conn = sqlite3.connect(f'file:{Path(db_path)}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def _encode_cursor(msg_id: str, recipient_session_id: str) -> str:
    raw = json.dumps([msg_id, recipient_session_id], ensure_ascii=False).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii')


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        msg_id, session = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf-8'))
    except Exception as e:  # noqa: BLE001 - any malformed token is the same user error
        raise ValueError(f'invalid cursor: {cursor!r}') from e
    return msg_id, session


def _age_days(acked_at: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(acked_at.rstrip('Z')).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0


def list_legacy_unknown_acks(
    db_path: str | Path,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> InventoryPage:
    """List quarantined pairs, newest schema first, without writing anything.

    Paginated by primary key so a large quarantine can be walked in bounded
    chunks and no row is served twice. The scan is operator-triggered; the
    message hot path never runs it.
    """
    if limit <= 0:
        raise ValueError(f'limit must be positive, got {limit}')
    with open_read_only(db_path) as conn:
        if not _has_table(conn, 'legacy_unknown_acks'):
            return InventoryPage(entries=[], next_cursor=None, migrated=False)
        params: list[object] = []
        where = ''
        if cursor is not None:
            last_msg, last_session = _decode_cursor(cursor)
            where = ' WHERE (msg_id, recipient_session_id) > (?, ?)'
            params.extend([last_msg, last_session])
        # One row beyond the page tells us whether another page exists without
        # a second COUNT query.
        params.append(limit + 1)
        rows = conn.execute(
            'SELECT msg_id, recipient_session_id, acked_at, migrated_at, state, resolved_at, resolution_note'
            f' FROM legacy_unknown_acks{where}'
            ' ORDER BY msg_id, recipient_session_id LIMIT ?',
            params,
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        entries = [
            LegacyAckEntry(
                msg_id=r['msg_id'],
                recipient_session_id=r['recipient_session_id'],
                acked_at=r['acked_at'],
                migrated_at=r['migrated_at'],
                state=r['state'],
                has_live_message=_pair_exists(conn, 'messages', r['msg_id'], r['recipient_session_id']),
                has_tombstone=_pair_exists(conn, 'message_tombstones', r['msg_id'], r['recipient_session_id']),
                age_days=_age_days(r['acked_at']),
                resolved_at=r['resolved_at'],
                resolution_note=r['resolution_note'],
            )
            for r in rows
        ]
    next_cursor = (
        _encode_cursor(entries[-1].msg_id, entries[-1].recipient_session_id) if has_more and entries else None
    )
    return InventoryPage(entries=entries, next_cursor=next_cursor, migrated=True)


def _pair_exists(conn: sqlite3.Connection, table: str, msg_id: str, recipient_session_id: str) -> bool:
    if not _has_table(conn, table):
        return False
    row = conn.execute(
        f'SELECT 1 FROM {table} WHERE msg_id = ? AND recipient_session_id = ?',  # noqa: S608 - fixed table names
        (msg_id, recipient_session_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Recovery (exact pair, backup-gated, audited)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryResult:
    """What a recovery did, or would have done when ``executed`` is False."""

    action: str
    msg_id: str
    recipient_session_id: str
    executed: bool
    affected: int
    before: dict[str, object] | None = None
    backup_path: str | None = None


def create_backup(source: str | Path, destination: str | Path) -> None:
    """Copy the live database to ``destination`` with SQLite's backup API.

    The backup API is used rather than a file copy because the source may be
    written concurrently, and a byte copy of a database mid-write is not a
    database. The copy is then integrity-checked, so a backup that cannot be
    restored is discovered here rather than during an incident.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f'file:{Path(source)}?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            result = dst.execute('PRAGMA integrity_check').fetchone()[0]
            if result != 'ok':
                raise OSError(f'backup at {destination} failed its integrity check: {result}')
        finally:
            dst.close()
    finally:
        src.close()


def _require_exact(value: str, label: str) -> None:
    if value is None or value.strip().lower() in _BULK_SELECTORS or any(c in value for c in _WILDCARD_CHARS):
        raise ValueError(
            f'refusing to recover: {label} must name one exact value, got {value!r}. '
            'Wildcards, ranges, ages and "all" are not accepted — recovery acts on a single '
            '(msg_id, recipient_session_id) pair so a legitimate ack-first row is never swept up.'
        )


def _authoritative_ack(
    conn: sqlite3.Connection,
    msg_id: str,
    recipient_session_id: str,
) -> dict[str, object] | None:
    row = conn.execute(
        'SELECT msg_id, recipient_session_id, acked_at FROM acks WHERE msg_id = ? AND recipient_session_id = ?',
        (msg_id, recipient_session_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _require_promotable(
    msg_id: str,
    recipient_session_id: str,
    has_message: bool,
    legacy_row: dict[str, object],
    existing_ack: dict[str, object] | None,
) -> None:
    """Refuse a promotion that would destroy evidence instead of recovering it.

    Two things can make a promotion wrong, and both are refused rather than
    resolved automatically:

    * No matching message. The promotion would create exactly the state this
      unit exists to remove — an authoritative ack with nothing behind it.
      ``release`` is the action for a message that is gone.
    * A different authoritative ack already exists. That ack has a message
      behind it, so it is stronger evidence than a quarantined row of unknown
      origin; replacing it would silently roll an acknowledgement back to an
      older time with nothing left to restore it from. Which of the two is
      correct is a judgement about what actually happened, so it belongs to the
      operator: ``release`` keeps the existing ack, and if the quarantined time
      is genuinely the right one it can be applied from the audit trail.

    An existing ack with the *same* ``acked_at`` is not a conflict — it is this
    same promotion having already happened — so it is allowed through and
    handled as a no-op on the ack table.
    """
    if not has_message:
        raise ValueError(
            f'refusing to promote ({msg_id}, {recipient_session_id}): there is no matching message, so '
            'there is nothing for the acknowledgement to belong to. Use release if the message is gone.'
        )
    if existing_ack is not None and existing_ack['acked_at'] != legacy_row['acked_at']:
        raise ValueError(
            f'refusing to promote ({msg_id}, {recipient_session_id}): the message already has an '
            f'acknowledgement recorded at {existing_ack["acked_at"]}, and promoting would replace it with '
            f'the quarantined {legacy_row["acked_at"]}. The existing acknowledgement has a message behind '
            'it and would not be recoverable afterwards. Use release to keep it, or resolve the conflict '
            'deliberately after inspecting both times.'
        )


def recover(
    db_path: str | Path,
    *,
    msg_id: str,
    recipient_session_id: str,
    action: str,
    backup_path: str | Path | None,
    execute: bool = False,
    operator: str | None = None,
) -> RecoveryResult:
    """Release or promote one quarantined pair.

    ``release`` records that the pair is not an acknowledgement, so the message
    may be presented again. ``promote`` records the opposite — that it really
    was an acknowledgement observed early — and moves it into the authoritative
    ``acks`` table; that only makes sense once the message exists, so it is
    refused otherwise, and it is refused again under the write lock in case the
    message went away in between (see :func:`_require_promotable`).

    Nothing is written unless every gate is satisfied: an exact pair, a backup
    path that does not already exist, a successful backup, and ``execute``.
    Without ``execute`` this is a dry run that still reports the row it would
    touch, so an operator can check the before image first.
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f'unknown action {action!r}; expected one of {", ".join(_VALID_ACTIONS)}')
    _require_exact(msg_id, '--msg-id')
    _require_exact(recipient_session_id, '--session-id')

    db_path = Path(db_path)
    with open_read_only(db_path) as conn:
        if not _has_table(conn, 'legacy_unknown_acks'):
            raise ValueError(
                f'refusing to recover: {db_path} has not been migrated to messaging schema '
                'v2 yet, so there is no quarantine to recover from.'
            )
        row = conn.execute(
            'SELECT * FROM legacy_unknown_acks WHERE msg_id = ? AND recipient_session_id = ?',
            (msg_id, recipient_session_id),
        ).fetchone()
        before = dict(row) if row is not None else None
        has_message = _pair_exists(conn, 'messages', msg_id, recipient_session_id) if row is not None else False
        existing_ack = _authoritative_ack(conn, msg_id, recipient_session_id) if row is not None else None

    if row is not None and action == 'promote':
        # Checked here as well as under the write lock so an operator learns
        # about the problem before taking a backup, not after.
        _require_promotable(msg_id, recipient_session_id, has_message, before, existing_ack)

    if not execute:
        return RecoveryResult(
            action=action,
            msg_id=msg_id,
            recipient_session_id=recipient_session_id,
            executed=False,
            affected=0,
            before=before,
        )

    if backup_path is None:
        raise ValueError(
            'refusing to recover without a backup: pass an absolute --backup path. Recovery edits ack '
            'state that cannot be reconstructed from anywhere else.'
        )
    backup_path = Path(backup_path)
    if backup_path.exists():
        raise ValueError(
            f'refusing to recover: the backup path {backup_path} already exists. Name a new file so an '
            'earlier backup is never overwritten.'
        )

    if before is None:
        # Nothing to change, so nothing is backed up or audited either.
        return RecoveryResult(
            action=action,
            msg_id=msg_id,
            recipient_session_id=recipient_session_id,
            executed=True,
            affected=0,
            before=None,
        )

    # Backup first: if this raises, the database has not been touched.
    create_backup(db_path, backup_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        # Re-read under the write lock: the row may have been resolved between
        # the dry-run read and now, and acting on a stale before image would
        # overwrite whatever happened in between.
        current = conn.execute(
            'SELECT * FROM legacy_unknown_acks WHERE msg_id = ? AND recipient_session_id = ?',
            (msg_id, recipient_session_id),
        ).fetchone()
        if current is None or dict(current) != before:
            conn.rollback()
            raise ValueError(
                f'refusing to recover ({msg_id}, {recipient_session_id}): the row changed since it was '
                'read. Re-run the dry run and check the new state before executing.'
            )
        now = _now_iso()
        # The preflight above ran against a different snapshot. Whatever the
        # promotion depends on has to be true *here*, under the lock that holds
        # until commit — otherwise an expiry purge or a second operator between
        # the two reads decides what this write does.
        current_message = conn.execute(
            'SELECT msg_id, recipient_session_id, scope, created_at, expires_at, is_acked FROM messages'
            ' WHERE msg_id = ? AND recipient_session_id = ?',
            (msg_id, recipient_session_id),
        ).fetchone()
        current_ack = _authoritative_ack(conn, msg_id, recipient_session_id)
        if action == 'promote':
            _require_promotable(
                msg_id,
                recipient_session_id,
                current_message is not None,
                before,
                current_ack,
            )
            if current_ack is None:
                conn.execute(
                    'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                    (msg_id, recipient_session_id, before['acked_at']),
                )
            # else: the same acknowledgement is already authoritative (a rerun,
            # or a crash between the two writes), so there is nothing to write
            # and nothing to overwrite. Resolving the quarantined row below is
            # the only work left.
            conn.execute(
                'UPDATE messages SET is_acked = 1 WHERE msg_id = ? AND recipient_session_id = ?',
                (msg_id, recipient_session_id),
            )
        conn.execute(
            'UPDATE legacy_unknown_acks SET state = ?, resolved_at = ?, resolution_note = ?'
            ' WHERE msg_id = ? AND recipient_session_id = ?',
            (
                'promoted' if action == 'promote' else 'released',
                now,
                f'{action} by {operator or "unknown"}',
                msg_id,
                recipient_session_id,
            ),
        )
        conn.execute(
            'INSERT INTO recovery_audit'
            ' (msg_id, recipient_session_id, action, before_json, operator, performed_at, backup_path)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                msg_id,
                recipient_session_id,
                action,
                # The whole state the decision was made against, not just the
                # quarantined row: "what did this overwrite" is the question an
                # audit log has to be able to answer.
                json.dumps(
                    {
                        'legacy_unknown_ack': before,
                        'authoritative_ack': current_ack,
                        'matching_message': dict(current_message) if current_message is not None else None,
                    },
                    ensure_ascii=False,
                ),
                operator,
                now,
                str(backup_path),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return RecoveryResult(
        action=action,
        msg_id=msg_id,
        recipient_session_id=recipient_session_id,
        executed=True,
        affected=1,
        before=before,
        backup_path=str(backup_path),
    )

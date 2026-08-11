"""Unit 1 of the N4 fix: additive schema migration + inventory/recovery foundation.

Background (TASK-300-rereview3 N4, design TASK-317-design option C): the old
purge bug left ``acks`` rows with no ``messages`` row. Those rows are *not*
inert — ``is_acked`` is an exact-pair point lookup, so a live message carrying
the same ``(msg_id, recipient_session_id)`` is suppressed with no warning.

Unit 1 does not change the suppression logic itself (that is unit 2). It
establishes the state model those decisions will be made from:

- schema versioning plus ``pending_acks`` / ``message_tombstones`` /
  ``legacy_unknown_acks`` / ``recovery_audit``
- a lossless, idempotent, transactional migration that keeps matched acks
  authoritative and quarantines unmatched ones as legacy-unknown rather than
  deleting them on an age guess
- a read-only, paginated inventory
- an exact-pair, backup-gated recovery path
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
import sqlite3

import pytest

from kioku_mesh.__main__ import main as cli_main
from kioku_mesh.messaging import orphan_acks
from kioku_mesh.messaging.local_index import LocalMessageIndex
from kioku_mesh.messaging.local_index import MESSAGING_SCHEMA_VERSION
from kioku_mesh.messaging.models import Ack
from kioku_mesh.messaging.models import Message

# ---------------------------------------------------------------------------
# Old-schema fixtures — a v1 database as it exists on a deployed host
# ---------------------------------------------------------------------------

_V1_DDL = """
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


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_v1_db(
    path: Path,
    *,
    matched: list[tuple[str, str]] = (),
    orphans: list[tuple[str, str, datetime]] = (),
) -> None:
    """Build a pre-migration database.

    ``matched`` pairs get both a messages row and an acks row (a normal
    acknowledged delivery). ``orphans`` get an acks row only — the shape the
    old purge bug left behind, and also the shape a legitimate ack observed
    before its message would take.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_DDL)
        for msg_id, session in matched:
            conn.execute(
                'INSERT INTO messages (msg_id, recipient_session_id, scope, created_at, expires_at, is_acked)'
                ' VALUES (?, ?, ?, ?, ?, 1)',
                (msg_id, session, 'mesh', _iso(_now()), None),
            )
            conn.execute(
                'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                (msg_id, session, _iso(_now())),
            )
        for msg_id, session, acked_at in orphans:
            conn.execute(
                'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                (msg_id, session, _iso(acked_at)),
            )
        conn.commit()
    finally:
        conn.close()


def _rows(path: Path, table: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f'SELECT * FROM {table} ORDER BY msg_id, recipient_session_id').fetchall()
    finally:
        conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def _pairs(path: Path, table: str) -> set[tuple[str, str]]:
    return {(r['msg_id'], r['recipient_session_id']) for r in _rows(path, table)}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / 'messaging' / 'inbox.db'


# ---------------------------------------------------------------------------
# Migration: classification
# ---------------------------------------------------------------------------


def test_migration_keeps_matched_acks_authoritative_and_quarantines_orphans(db_path: Path) -> None:
    """The central split: an ack with a message stays put, one without moves to legacy-unknown."""
    year_ago = _now() - timedelta(days=365)
    _write_v1_db(
        db_path,
        matched=[('matched-1', 'sess-a')],
        orphans=[('orphan-1', 'sess-a', year_ago)],
    )

    LocalMessageIndex(db_path)

    assert _pairs(db_path, 'acks') == {('matched-1', 'sess-a')}
    assert _pairs(db_path, 'legacy_unknown_acks') == {('orphan-1', 'sess-a')}
    # Lossless: the original acked_at travels with the row.
    (legacy,) = _rows(db_path, 'legacy_unknown_acks')
    assert legacy['acked_at'] == _iso(year_ago)
    assert legacy['state'] == 'unresolved'


def test_migration_does_not_delete_orphans_however_old_they_are(db_path: Path) -> None:
    """Age is display information, never a deletion criterion (design: ambiguity_policy)."""
    _write_v1_db(db_path, orphans=[('ancient', 'sess-a', _now() - timedelta(days=3650))])

    LocalMessageIndex(db_path)

    assert _count(db_path, 'legacy_unknown_acks') == 1
    assert _count(db_path, 'acks') == 0


def test_migration_separates_orphans_per_session(db_path: Path) -> None:
    """The unit of state is the pair, so the same msg_id under two sessions stays two rows."""
    _write_v1_db(
        db_path,
        matched=[('shared', 'sess-a')],
        orphans=[('shared', 'sess-b', _now()), ('shared', 'sess-c', _now())],
    )

    LocalMessageIndex(db_path)

    assert _pairs(db_path, 'acks') == {('shared', 'sess-a')}
    assert _pairs(db_path, 'legacy_unknown_acks') == {('shared', 'sess-b'), ('shared', 'sess-c')}


def test_migration_on_an_empty_database_is_a_no_op(db_path: Path) -> None:
    _write_v1_db(db_path)

    LocalMessageIndex(db_path)

    assert _count(db_path, 'legacy_unknown_acks') == 0
    assert _count(db_path, 'acks') == 0


def test_migration_is_idempotent_across_reopens(db_path: Path) -> None:
    """Acceptance: applying the migration twice changes no row, count or state."""
    _write_v1_db(
        db_path,
        matched=[('matched-1', 'sess-a')],
        orphans=[('orphan-1', 'sess-a', _now() - timedelta(days=365))],
    )

    LocalMessageIndex(db_path)
    first = {t: [dict(r) for r in _rows(db_path, t)] for t in ('messages', 'acks', 'legacy_unknown_acks')}

    LocalMessageIndex(db_path)
    second = {t: [dict(r) for r in _rows(db_path, t)] for t in ('messages', 'acks', 'legacy_unknown_acks')}

    assert first == second


def test_running_the_classification_again_directly_is_also_idempotent(db_path: Path) -> None:
    """Not just version-guarded: the classification itself must be safe to repeat.

    Reopening the database skips the pass once the version is stamped, so this
    function is the only re-classification entry point there is; calling it has
    to converge rather than duplicate.
    """
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now())])
    LocalMessageIndex(db_path)
    before = [dict(r) for r in _rows(db_path, 'legacy_unknown_acks')]

    conn = sqlite3.connect(db_path)
    try:
        orphan_acks.classify_unmatched_acks(conn)
        conn.commit()
    finally:
        conn.close()

    assert [dict(r) for r in _rows(db_path, 'legacy_unknown_acks')] == before


def test_migration_stamps_the_schema_version(db_path: Path) -> None:
    _write_v1_db(db_path)

    LocalMessageIndex(db_path)

    conn = sqlite3.connect(db_path)
    try:
        (version,) = conn.execute('SELECT version FROM messaging_schema_version').fetchone()
    finally:
        conn.close()
    assert version == MESSAGING_SCHEMA_VERSION


def test_migration_adds_every_unit_one_table(db_path: Path) -> None:
    _write_v1_db(db_path)

    LocalMessageIndex(db_path)

    conn = sqlite3.connect(db_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert {'pending_acks', 'message_tombstones', 'legacy_unknown_acks', 'recovery_audit'} <= names
    # Additive only: nothing the old version wrote is dropped.
    assert {'messages', 'acks'} <= names


def test_a_failure_mid_migration_rolls_the_database_back(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """Acceptance: fault injection during migration leaves the original database untouched."""
    _write_v1_db(
        db_path,
        matched=[('matched-1', 'sess-a')],
        orphans=[('orphan-1', 'sess-a', _now())],
    )
    before = {t: [dict(r) for r in _rows(db_path, t)] for t in ('messages', 'acks')}

    def boom(conn: sqlite3.Connection, moved: int) -> None:
        raise RuntimeError('simulated failure after the rows were moved')

    monkeypatch.setattr(orphan_acks, 'verify_classification', boom)

    with pytest.raises(RuntimeError, match='simulated failure'):
        LocalMessageIndex(db_path)

    after = {t: [dict(r) for r in _rows(db_path, t)] for t in ('messages', 'acks')}
    assert after == before
    conn = sqlite3.connect(db_path)
    try:
        moved = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'legacy_unknown_acks'"
        ).fetchone()[0]
        if moved:
            assert conn.execute('SELECT COUNT(*) FROM legacy_unknown_acks').fetchone()[0] == 0
    finally:
        conn.close()


def test_a_new_database_starts_at_the_current_schema_version(db_path: Path) -> None:
    """A fresh install needs no classification pass and is immediately current."""
    index = LocalMessageIndex(db_path)
    msg = Message(sender_id='s', scope='mesh', payload={'text': 'hi'})
    assert index.register(msg, 'sess-a') is True

    conn = sqlite3.connect(db_path)
    try:
        (version,) = conn.execute('SELECT version FROM messaging_schema_version').fetchone()
    finally:
        conn.close()
    assert version == MESSAGING_SCHEMA_VERSION


def test_legacy_unknown_rows_are_not_read_as_acknowledgements(db_path: Path) -> None:
    """The point of the quarantine: a quarantined row must not answer is_acked."""
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now() - timedelta(days=365))])

    index = LocalMessageIndex(db_path)

    assert index.is_acked('orphan-1', 'sess-a') is False


# ---------------------------------------------------------------------------
# Inventory — read-only and paginated
# ---------------------------------------------------------------------------


def test_inventory_lists_quarantined_rows_with_their_metadata(db_path: Path) -> None:
    acked_at = _now() - timedelta(days=365)
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', acked_at)])
    LocalMessageIndex(db_path)

    page = orphan_acks.list_legacy_unknown_acks(db_path)

    (entry,) = page.entries
    assert entry.msg_id == 'orphan-1'
    assert entry.recipient_session_id == 'sess-a'
    assert entry.acked_at == _iso(acked_at)
    assert entry.state == 'unresolved'
    assert entry.has_live_message is False
    assert entry.has_tombstone is False


def test_inventory_reports_a_matching_live_message(db_path: Path) -> None:
    """The dangerous case operators need to see: a live message on a quarantined pair."""
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', _now() - timedelta(days=365))])
    index = LocalMessageIndex(db_path)
    index.register(Message(sender_id='s', scope='mesh', payload={'t': 'x'}, msg_id='collide'), 'sess-a')

    (entry,) = orphan_acks.list_legacy_unknown_acks(db_path).entries

    assert entry.has_live_message is True


def test_inventory_does_not_write_to_the_database(db_path: Path) -> None:
    """Acceptance: list is read-only — no mtime change, no row-count change."""
    _write_v1_db(db_path, orphans=[(f'orphan-{i}', 'sess-a', _now()) for i in range(3)])
    LocalMessageIndex(db_path)
    before_mtime = db_path.stat().st_mtime_ns
    before_counts = {t: _count(db_path, t) for t in ('messages', 'acks', 'legacy_unknown_acks', 'recovery_audit')}

    orphan_acks.list_legacy_unknown_acks(db_path)

    assert db_path.stat().st_mtime_ns == before_mtime
    assert {t: _count(db_path, t) for t in before_counts} == before_counts


def test_inventory_refuses_to_open_the_database_for_writing(db_path: Path) -> None:
    """Read-only is enforced by the connection, not by convention."""
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now())])
    LocalMessageIndex(db_path)

    with orphan_acks.open_read_only(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                'INSERT INTO legacy_unknown_acks (msg_id, recipient_session_id, acked_at, migrated_at, state)'
                " VALUES ('x', 'y', 'z', 'z', 'unresolved')"
            )


def test_inventory_paginates_with_a_cursor(db_path: Path) -> None:
    _write_v1_db(db_path, orphans=[(f'orphan-{i}', 'sess-a', _now()) for i in range(5)])
    LocalMessageIndex(db_path)

    first = orphan_acks.list_legacy_unknown_acks(db_path, limit=2)
    assert len(first.entries) == 2
    assert first.next_cursor is not None

    second = orphan_acks.list_legacy_unknown_acks(db_path, limit=2, cursor=first.next_cursor)
    assert len(second.entries) == 2

    third = orphan_acks.list_legacy_unknown_acks(db_path, limit=2, cursor=second.next_cursor)
    assert len(third.entries) == 1
    assert third.next_cursor is None

    seen = [e.msg_id for e in (*first.entries, *second.entries, *third.entries)]
    assert sorted(seen) == [f'orphan-{i}' for i in range(5)]
    assert len(set(seen)) == 5  # no row served twice across pages


def test_inventory_on_an_unmigrated_database_says_so_instead_of_crashing(db_path: Path) -> None:
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now())])

    page = orphan_acks.list_legacy_unknown_acks(db_path)

    assert page.migrated is False
    assert page.entries == []


# ---------------------------------------------------------------------------
# Recovery — exact pair, backup-gated, audited
# ---------------------------------------------------------------------------


@pytest.fixture
def quarantined_db(db_path: Path) -> Path:
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now() - timedelta(days=365))])
    LocalMessageIndex(db_path)
    return db_path


def test_recover_without_execute_is_a_dry_run(quarantined_db: Path, tmp_path: Path) -> None:
    """Acceptance: no --execute, no write — and the caller still sees what would happen."""
    result = orphan_acks.recover(
        quarantined_db,
        msg_id='orphan-1',
        recipient_session_id='sess-a',
        action='release',
        backup_path=tmp_path / 'backup.db',
        execute=False,
    )

    assert result.executed is False
    assert result.affected == 0
    assert result.before is not None  # the dry run still reports the row it would touch
    assert not (tmp_path / 'backup.db').exists()
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'
    assert _count(quarantined_db, 'recovery_audit') == 0


def test_recover_requires_a_backup_path(quarantined_db: Path) -> None:
    with pytest.raises(ValueError, match='backup'):
        orphan_acks.recover(
            quarantined_db,
            msg_id='orphan-1',
            recipient_session_id='sess-a',
            action='release',
            backup_path=None,
            execute=True,
        )

    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


@pytest.mark.parametrize('bad', ['', '*', 'orphan-*', '%', 'all', 'ALL', '?'])
def test_recover_rejects_wildcards_and_bulk_selectors(quarantined_db: Path, tmp_path: Path, bad: str) -> None:
    """Acceptance: an exact pair only — range, age-only, all and wildcard are refused."""
    with pytest.raises(ValueError, match='exact'):
        orphan_acks.recover(
            quarantined_db,
            msg_id=bad,
            recipient_session_id='sess-a',
            action='release',
            backup_path=tmp_path / 'backup.db',
            execute=True,
        )

    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'
    assert not (tmp_path / 'backup.db').exists()


def test_recover_rejects_a_wildcard_session(quarantined_db: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='exact'):
        orphan_acks.recover(
            quarantined_db,
            msg_id='orphan-1',
            recipient_session_id='*',
            action='release',
            backup_path=tmp_path / 'backup.db',
            execute=True,
        )


def test_recover_writes_nothing_when_the_backup_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch, quarantined_db: Path, tmp_path: Path
) -> None:
    """Acceptance: backup failure means zero recovered rows."""

    def boom(source: Path, destination: Path) -> None:
        raise OSError('simulated backup failure')

    monkeypatch.setattr(orphan_acks, 'create_backup', boom)

    with pytest.raises(OSError, match='simulated backup failure'):
        orphan_acks.recover(
            quarantined_db,
            msg_id='orphan-1',
            recipient_session_id='sess-a',
            action='release',
            backup_path=tmp_path / 'backup.db',
            execute=True,
        )

    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'
    assert _count(quarantined_db, 'recovery_audit') == 0


def test_recover_refuses_to_overwrite_an_existing_backup(quarantined_db: Path, tmp_path: Path) -> None:
    """A backup that clobbers an earlier one is not a backup."""
    existing = tmp_path / 'backup.db'
    existing.write_text('not a database', encoding='utf-8')

    with pytest.raises(ValueError, match='exists'):
        orphan_acks.recover(
            quarantined_db,
            msg_id='orphan-1',
            recipient_session_id='sess-a',
            action='release',
            backup_path=existing,
            execute=True,
        )

    assert existing.read_text(encoding='utf-8') == 'not a database'
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


def test_release_marks_only_the_named_pair(db_path: Path, tmp_path: Path) -> None:
    """Pair isolation: the neighbour row must be untouched."""
    _write_v1_db(
        db_path,
        orphans=[('orphan-1', 'sess-a', _now()), ('orphan-1', 'sess-b', _now()), ('orphan-2', 'sess-a', _now())],
    )
    LocalMessageIndex(db_path)

    result = orphan_acks.recover(
        db_path,
        msg_id='orphan-1',
        recipient_session_id='sess-a',
        action='release',
        backup_path=tmp_path / 'backup.db',
        execute=True,
        operator='tester',
    )

    assert result.executed is True
    assert result.affected == 1
    states = {(r['msg_id'], r['recipient_session_id']): r['state'] for r in _rows(db_path, 'legacy_unknown_acks')}
    assert states == {
        ('orphan-1', 'sess-a'): 'released',
        ('orphan-1', 'sess-b'): 'unresolved',
        ('orphan-2', 'sess-a'): 'unresolved',
    }


def test_promote_requires_a_matching_message(quarantined_db: Path, tmp_path: Path) -> None:
    """Promotion means "this really was an ack-first", which only makes sense with a message."""
    with pytest.raises(ValueError, match='no matching message'):
        orphan_acks.recover(
            quarantined_db,
            msg_id='orphan-1',
            recipient_session_id='sess-a',
            action='promote',
            backup_path=tmp_path / 'backup.db',
            execute=True,
        )

    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


def test_promote_moves_the_row_into_authoritative_acks(db_path: Path, tmp_path: Path) -> None:
    acked_at = _now() - timedelta(days=2)
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', acked_at)])
    index = LocalMessageIndex(db_path)
    index.register(Message(sender_id='s', scope='mesh', payload={'t': 'x'}, msg_id='collide'), 'sess-a')

    result = orphan_acks.recover(
        db_path,
        msg_id='collide',
        recipient_session_id='sess-a',
        action='promote',
        backup_path=tmp_path / 'backup.db',
        execute=True,
    )

    assert result.affected == 1
    assert _pairs(db_path, 'acks') == {('collide', 'sess-a')}
    assert _rows(db_path, 'legacy_unknown_acks')[0]['state'] == 'promoted'
    # The acked_at travels with the promotion rather than being restamped.
    assert _rows(db_path, 'acks')[0]['acked_at'] == _iso(acked_at)
    assert index.is_acked('collide', 'sess-a') is True


def test_recovery_is_recorded_in_the_audit_log(quarantined_db: Path, tmp_path: Path) -> None:
    backup = tmp_path / 'backup.db'

    orphan_acks.recover(
        quarantined_db,
        msg_id='orphan-1',
        recipient_session_id='sess-a',
        action='release',
        backup_path=backup,
        execute=True,
        operator='tester',
    )

    (audit,) = _rows(quarantined_db, 'recovery_audit')
    assert audit['msg_id'] == 'orphan-1'
    assert audit['recipient_session_id'] == 'sess-a'
    assert audit['action'] == 'release'
    assert audit['operator'] == 'tester'
    assert audit['backup_path'] == str(backup)
    assert 'unresolved' in audit['before_json']  # the before image, not just the outcome


def test_recover_on_an_unknown_pair_changes_nothing(quarantined_db: Path, tmp_path: Path) -> None:
    result = orphan_acks.recover(
        quarantined_db,
        msg_id='not-there',
        recipient_session_id='sess-a',
        action='release',
        backup_path=tmp_path / 'backup.db',
        execute=True,
    )

    assert result.affected == 0
    assert _count(quarantined_db, 'recovery_audit') == 0
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


def test_the_backup_round_trips_the_pre_recovery_state(quarantined_db: Path, tmp_path: Path) -> None:
    """The backup has to be restorable, or the gate is theatre."""
    backup = tmp_path / 'backup.db'

    orphan_acks.recover(
        quarantined_db,
        msg_id='orphan-1',
        recipient_session_id='sess-a',
        action='release',
        backup_path=backup,
        execute=True,
    )
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'released'

    conn = sqlite3.connect(backup)
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        conn.close()

    restored = tmp_path / 'restored.db'
    restored.write_bytes(backup.read_bytes())
    assert _rows(restored, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


# ---------------------------------------------------------------------------
# CLI — the operator-facing surface of the same guards
# ---------------------------------------------------------------------------


def test_cli_list_reports_quarantined_pairs(quarantined_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['messaging', 'orphan-acks', 'list', '--db', str(quarantined_db), '--format', 'json'])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['migrated'] is True
    assert [(e['msg_id'], e['recipient_session_id']) for e in payload['entries']] == [('orphan-1', 'sess-a')]


def test_cli_list_does_not_write_to_the_database(quarantined_db: Path) -> None:
    before_mtime = quarantined_db.stat().st_mtime_ns

    assert cli_main(['messaging', 'orphan-acks', 'list', '--db', str(quarantined_db)]) == 0

    assert quarantined_db.stat().st_mtime_ns == before_mtime


def test_cli_recover_defaults_to_a_dry_run(quarantined_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No --execute on the command line means no write, exactly as in the API."""
    rc = cli_main(
        [
            'messaging',
            'orphan-acks',
            'recover',
            '--db',
            str(quarantined_db),
            '--msg-id',
            'orphan-1',
            '--session-id',
            'sess-a',
            '--action',
            'release',
        ]
    )

    assert rc == 0
    assert 'dry run' in capsys.readouterr().out
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


def test_cli_recover_refuses_to_execute_without_a_backup(
    quarantined_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_main(
        [
            'messaging',
            'orphan-acks',
            'recover',
            '--db',
            str(quarantined_db),
            '--msg-id',
            'orphan-1',
            '--session-id',
            'sess-a',
            '--action',
            'release',
            '--execute',
        ]
    )

    assert rc == 2
    assert 'backup' in capsys.readouterr().err
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


def test_cli_recover_refuses_a_wildcard_msg_id(quarantined_db: Path, tmp_path: Path) -> None:
    rc = cli_main(
        [
            'messaging',
            'orphan-acks',
            'recover',
            '--db',
            str(quarantined_db),
            '--msg-id',
            '*',
            '--session-id',
            'sess-a',
            '--action',
            'release',
            '--backup',
            str(tmp_path / 'b.db'),
            '--execute',
        ]
    )

    assert rc == 2
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'unresolved'


def test_cli_has_no_bulk_cleanup_command(capsys: pytest.CaptureFixture[str]) -> None:
    """The destructive command this replaces must not come back by accident."""
    with pytest.raises(SystemExit):
        cli_main(['messaging', 'orphan-acks', 'purge'])

    assert 'purge' not in capsys.readouterr().out


def test_cli_recover_executes_with_a_backup(quarantined_db: Path, tmp_path: Path) -> None:
    backup = tmp_path / 'inbox.backup.db'

    rc = cli_main(
        [
            'messaging',
            'orphan-acks',
            'recover',
            '--db',
            str(quarantined_db),
            '--msg-id',
            'orphan-1',
            '--session-id',
            'sess-a',
            '--action',
            'release',
            '--backup',
            str(backup),
            '--operator',
            'tester',
            '--execute',
        ]
    )

    assert rc == 0
    assert backup.exists()
    assert _rows(quarantined_db, 'legacy_unknown_acks')[0]['state'] == 'released'
    assert _count(quarantined_db, 'recovery_audit') == 1


# ---------------------------------------------------------------------------
# Promote — the write transaction has to re-check what it is promoting onto
# (cross-review PR304-B1)
# ---------------------------------------------------------------------------


def _register_live_message(db_path: Path, msg_id: str, session: str) -> LocalMessageIndex:
    index = LocalMessageIndex(db_path)
    index.register(Message(sender_id='s', scope='mesh', payload={'t': 'x'}, msg_id=msg_id), session)
    return index


def test_promote_refuses_when_the_message_disappears_before_the_write(
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins: the message is re-checked under the write lock, not only in preflight.

    The preflight read and the write are two different points in time. Anything
    that can delete the message in between — expiry purge, another operator —
    would otherwise leave an authoritative ack with nothing behind it, which is
    precisely the state this whole unit exists to prevent.
    """
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', _now() - timedelta(days=2))])
    _register_live_message(db_path, 'collide', 'sess-a')
    real_backup = orphan_acks.create_backup

    def _backup_then_delete_the_message(source: Path, destination: Path) -> None:
        real_backup(source, destination)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM messages WHERE msg_id = 'collide'")
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(orphan_acks, 'create_backup', _backup_then_delete_the_message)

    with pytest.raises(ValueError, match='no matching message'):
        orphan_acks.recover(
            db_path,
            msg_id='collide',
            recipient_session_id='sess-a',
            action='promote',
            backup_path=tmp_path / 'backup.db',
            execute=True,
        )

    assert _pairs(db_path, 'acks') == set(), 'an ack was created for a message that no longer exists'
    assert _rows(db_path, 'legacy_unknown_acks')[0]['state'] == 'unresolved'
    assert _count(db_path, 'recovery_audit') == 0


def test_promote_refuses_to_overwrite_a_different_authoritative_ack(db_path: Path, tmp_path: Path) -> None:
    """Pins: a newer acknowledgement is never rolled back to the quarantined one.

    An authoritative ack has a message behind it, so it is better evidence than
    the quarantined row. Overwriting it would discard a real acknowledgement,
    and doing so through a REPLACE leaves nothing to restore it from.
    """
    legacy_acked_at = _now() - timedelta(days=365)
    fresh_acked_at = _now() - timedelta(minutes=5)
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', legacy_acked_at)])
    index = _register_live_message(db_path, 'collide', 'sess-a')
    index.record_ack(Ack(msg_id='collide', recipient_session_id='sess-a', acked_at=fresh_acked_at))

    with pytest.raises(ValueError, match='already has an acknowledgement'):
        orphan_acks.recover(
            db_path,
            msg_id='collide',
            recipient_session_id='sess-a',
            action='promote',
            backup_path=tmp_path / 'backup.db',
            execute=True,
        )

    assert _rows(db_path, 'acks')[0]['acked_at'] == _iso(fresh_acked_at), 'the newer ack was rolled back'
    assert _rows(db_path, 'legacy_unknown_acks')[0]['state'] == 'unresolved'
    assert _count(db_path, 'recovery_audit') == 0


def test_promote_dry_run_also_reports_the_conflicting_ack(db_path: Path, tmp_path: Path) -> None:
    """Pins: the conflict is reported before an operator takes a backup, not after."""
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', _now() - timedelta(days=365))])
    index = _register_live_message(db_path, 'collide', 'sess-a')
    index.record_ack(Ack(msg_id='collide', recipient_session_id='sess-a'))

    with pytest.raises(ValueError, match='already has an acknowledgement'):
        orphan_acks.recover(
            db_path,
            msg_id='collide',
            recipient_session_id='sess-a',
            action='promote',
            backup_path=tmp_path / 'backup.db',
            execute=False,
        )


def test_promote_of_an_already_authoritative_ack_only_resolves_the_quarantine(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """Pins: re-promoting the same acknowledgement converges instead of failing.

    Same pair, same ``acked_at`` — the promotion has already happened (a rerun,
    or a crash between the two writes). Nothing is overwritten, so the only work
    left is marking the quarantined row resolved.
    """
    acked_at = _now() - timedelta(days=2)
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', acked_at)])
    index = _register_live_message(db_path, 'collide', 'sess-a')
    index.record_ack(Ack(msg_id='collide', recipient_session_id='sess-a', acked_at=acked_at))

    result = orphan_acks.recover(
        db_path,
        msg_id='collide',
        recipient_session_id='sess-a',
        action='promote',
        backup_path=tmp_path / 'backup.db',
        execute=True,
    )

    assert result.affected == 1
    assert _rows(db_path, 'acks')[0]['acked_at'] == _iso(acked_at)
    assert _rows(db_path, 'legacy_unknown_acks')[0]['state'] == 'promoted'


def test_the_audit_before_image_records_the_ack_state_the_decision_was_made_on(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """Pins: the audit shows what was there, including the authoritative side.

    A before image of the quarantined row alone cannot answer "what did this
    overwrite" — the question an audit log exists to answer.
    """
    acked_at = _now() - timedelta(days=2)
    _write_v1_db(db_path, orphans=[('collide', 'sess-a', acked_at)])
    index = _register_live_message(db_path, 'collide', 'sess-a')
    index.record_ack(Ack(msg_id='collide', recipient_session_id='sess-a', acked_at=acked_at))

    orphan_acks.recover(
        db_path,
        msg_id='collide',
        recipient_session_id='sess-a',
        action='promote',
        backup_path=tmp_path / 'backup.db',
        execute=True,
        operator='tester',
    )

    (audit,) = _rows(db_path, 'recovery_audit')
    before = json.loads(audit['before_json'])
    assert before['legacy_unknown_ack']['state'] == 'unresolved'
    assert before['authoritative_ack']['acked_at'] == _iso(acked_at)
    assert before['matching_message'] is not None


def test_a_second_classification_keeps_a_conflicting_acked_at_visible(db_path: Path) -> None:
    """Pins: a differing ack time on a re-run is recorded, not silently dropped.

    ``INSERT OR IGNORE`` keeps the quarantined time and the source row is then
    deleted, so without this the second time would vanish. Which one is real
    cannot be decided from the database, so both are kept for the operator.
    """
    first_time = _now() - timedelta(days=365)
    second_time = _now() - timedelta(days=1)
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', first_time)])
    LocalMessageIndex(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
            ('orphan-1', 'sess-a', _iso(second_time)),
        )
        orphan_acks.classify_unmatched_acks(conn)
        conn.commit()
    finally:
        conn.close()

    (row,) = _rows(db_path, 'legacy_unknown_acks')
    assert row['acked_at'] == _iso(first_time), 'the quarantined time must not be restamped'
    assert _iso(second_time) in row['resolution_note']
    assert _pairs(db_path, 'acks') == set()

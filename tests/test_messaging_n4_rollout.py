"""Unit 3 of the N4 fix: the per-node rollout completion check.

Units 1 and 2 changed the data model and the ingress path. What was still
missing is the operator's half: a way to answer "is this node actually done?"
without reading rows by hand. The design (TASK-317-design, deployment_migration
/ completion_checks) lists what has to be true, and this is that list turned
into something a rollout script can run on every host:

- the database is at the current messaging schema (the migration has run);
- no ack with no message is sitting in ``acks`` outside the quarantine;
- nothing was quarantined *after* the migration pass, which would mean an old
  writer is still creating bare acks.

Deliberately *not* a blocker: quarantined rows that are still unresolved. The
whole point of unit 1 is that those cannot be classified from the data, so an
operator may leave them alone indefinitely. Failing the rollout check on them
would manufacture pressure to clear the quarantine — the bulk-cleanup reflex
this design exists to prevent.
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
from kioku_mesh.messaging import local_index as local_index_module
from kioku_mesh.messaging import orphan_acks
from kioku_mesh.messaging.local_index import LocalMessageIndex
from kioku_mesh.messaging.local_index import MESSAGING_SCHEMA_VERSION
from kioku_mesh.messaging.models import Ack
from kioku_mesh.messaging.models import Message

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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / 'messaging' / 'inbox.db'


def _write_v1_db(path: Path, *, orphans: list[tuple[str, str, datetime]] = ()) -> None:
    """Build a pre-migration database with the shape the old purge bug left."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_DDL)
        for msg_id, session, acked_at in orphans:
            conn.execute(
                'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                (msg_id, session, _iso(acked_at)),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_bare_ack(path: Path, msg_id: str, session: str) -> None:
    """Write an ack with no message straight into ``acks``, bypassing the API.

    This is what an old writer still running against a migrated database does:
    ``record_ack`` refuses it, so no supported path can produce it, which is
    precisely why the rollout check has to look for it.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
            (msg_id, session, _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()


def _make_msg(*, expires_at: datetime | None = None) -> Message:
    return Message(sender_id='sender', scope='mesh', payload={'text': 'hello'}, expires_at=expires_at)


# ---------------------------------------------------------------------------
# The green path
# ---------------------------------------------------------------------------


def test_a_freshly_created_database_is_already_complete(db_path: Path) -> None:
    """Nothing to migrate means nothing to block on."""
    LocalMessageIndex(db_path)

    status = orphan_acks.rollout_status(db_path)

    assert status.complete is True
    assert status.blockers == []
    assert status.schema_version == MESSAGING_SCHEMA_VERSION
    assert status.expected_schema_version == MESSAGING_SCHEMA_VERSION
    assert status.quarantined_total == 0


def test_the_writer_version_running_here_is_reported(db_path: Path) -> None:
    """The fleet check is "every node, same version", so the version has to be in the output."""
    from kioku_mesh import __version__

    LocalMessageIndex(db_path)

    assert orphan_acks.rollout_status(db_path).writer_version == __version__


def test_normal_acknowledged_traffic_does_not_block_the_rollout(db_path: Path) -> None:
    index = LocalMessageIndex(db_path)
    msg = _make_msg()
    index.register(msg, 'sess-a')
    index.record_ack(Ack(msg_id=msg.msg_id, recipient_session_id='sess-a'))

    status = orphan_acks.rollout_status(db_path)

    assert status.complete is True
    assert status.unmatched_acks_outside_quarantine == 0


def test_a_migrated_quarantine_is_reported_but_does_not_block(db_path: Path) -> None:
    """Unresolved pre-existing ambiguity is the expected steady state, not a failure."""
    year_ago = _now() - timedelta(days=365)
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', year_ago), ('orphan-2', 'sess-a', year_ago)])

    LocalMessageIndex(db_path)
    status = orphan_acks.rollout_status(db_path)

    assert status.quarantined_total == 2
    assert status.quarantined_unresolved == 2
    assert status.quarantined_by_provenance == {'migration': 2}
    assert status.quarantined_after_migration == 0
    assert status.oldest_migrated_at is not None
    assert status.complete is True


def test_resolving_a_pair_moves_it_out_of_the_unresolved_count(db_path: Path, tmp_path: Path) -> None:
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now() - timedelta(days=10))])
    LocalMessageIndex(db_path)

    orphan_acks.recover(
        db_path,
        msg_id='orphan-1',
        recipient_session_id='sess-a',
        action='release',
        backup_path=str(tmp_path / 'backup.db'),
        execute=True,
        operator='test',
    )

    status = orphan_acks.rollout_status(db_path)
    assert status.quarantined_unresolved == 0
    assert status.quarantined_by_state == {'released': 1}
    assert status.complete is True


# ---------------------------------------------------------------------------
# The three blockers
# ---------------------------------------------------------------------------


def test_an_unmigrated_database_blocks_on_its_schema_version(db_path: Path) -> None:
    """A v1 database on a host nobody restarted is the first thing the rollout must catch."""
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now())])

    status = orphan_acks.rollout_status(db_path)

    assert status.complete is False
    assert status.schema_version == 1
    assert any('schema v1' in b for b in status.blockers)


def test_a_bare_ack_left_outside_the_quarantine_blocks(db_path: Path) -> None:
    """The signature of an old writer: an ack with no message, still in `acks`."""
    LocalMessageIndex(db_path)
    _insert_bare_ack(db_path, 'written-by-an-old-writer', 'sess-a')

    status = orphan_acks.rollout_status(db_path)

    assert status.complete is False
    assert status.unmatched_acks_outside_quarantine == 1
    assert any('still in `acks`' in b for b in status.blockers)


def test_a_pair_quarantined_after_the_migration_blocks(db_path: Path) -> None:
    """Unit 2 quarantines such an ack on arrival; that it happened at all is the signal."""
    index = LocalMessageIndex(db_path)
    _insert_bare_ack(db_path, 'late-bare-ack', 'sess-a')
    # The arrival of the matching message is what moves the bare ack into the
    # quarantine, with a provenance saying it did not come from the migration.
    msg = _make_msg()
    msg.msg_id = 'late-bare-ack'
    index.register_or_classify(msg, 'sess-a')

    status = orphan_acks.rollout_status(db_path)

    assert status.quarantined_after_migration == 1
    assert status.quarantined_by_provenance.get('post_migration_ack') == 1
    assert status.complete is False
    assert any('after the migration pass' in b for b in status.blockers)


def test_every_blocker_is_listed_rather_than_only_the_first(db_path: Path) -> None:
    """`complete` is defined as "no blockers", so the list must be the whole answer."""
    _write_v1_db(db_path, orphans=[])
    _insert_bare_ack(db_path, 'orphan-on-a-v1-db', 'sess-a')

    status = orphan_acks.rollout_status(db_path)

    assert len(status.blockers) == 2
    assert status.complete is False


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_the_status_check_does_not_write_to_the_database(db_path: Path) -> None:
    """It is meant to run against a live deployment before anyone has a backup."""
    _write_v1_db(db_path, orphans=[('orphan-1', 'sess-a', _now())])
    LocalMessageIndex(db_path)
    before_mtime = db_path.stat().st_mtime_ns
    before_size = db_path.stat().st_size

    orphan_acks.rollout_status(db_path)

    assert db_path.stat().st_mtime_ns == before_mtime
    assert db_path.stat().st_size == before_size


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_the_cli_exits_zero_when_the_node_is_done(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    LocalMessageIndex(db_path)

    assert cli_main(['messaging', 'orphan-acks', 'status', '--db', str(db_path)]) == 0
    assert 'rollout: complete' in capsys.readouterr().out


def test_the_cli_exits_one_when_something_still_blocks(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit status is what a rollout script reads, so it has to differ from the green case."""
    LocalMessageIndex(db_path)
    _insert_bare_ack(db_path, 'written-by-an-old-writer', 'sess-a')

    assert cli_main(['messaging', 'orphan-acks', 'status', '--db', str(db_path)]) == 1
    out = capsys.readouterr().out
    assert 'rollout: NOT complete' in out
    assert 'still in `acks`' in out


def test_the_cli_json_form_carries_the_same_verdict(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    LocalMessageIndex(db_path)
    _insert_bare_ack(db_path, 'written-by-an-old-writer', 'sess-a')

    code = cli_main(['messaging', 'orphan-acks', 'status', '--db', str(db_path), '--format', 'json'])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload['complete'] is False
    assert payload['unmatched_acks_outside_quarantine'] == 1
    assert payload['expected_schema_version'] == MESSAGING_SCHEMA_VERSION
    assert len(payload['blockers']) == 1


def test_the_cli_says_so_when_there_is_no_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / 'messaging' / 'inbox.db'

    assert cli_main(['messaging', 'orphan-acks', 'status', '--db', str(missing)]) == 2
    assert 'no messaging index' in capsys.readouterr().err


def test_the_cli_still_refuses_the_removed_bulk_cleanup_command() -> None:
    """The destructive command deleted in #300's review must not come back with unit 3."""
    with pytest.raises(SystemExit) as exc:
        cli_main(['messaging', 'purge-orphan-acks'])

    assert exc.value.code == 2


def test_the_index_still_exposes_no_bulk_purge_api() -> None:
    """The CLI guard above only stops one caller. The API itself must not exist either."""
    assert not hasattr(LocalMessageIndex, 'purge_orphan_acks')
    assert not hasattr(local_index_module, 'ORPHAN_ACK_GRACE_SEC')

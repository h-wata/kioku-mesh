"""Unit 2 of the N4 fix: ack suppression judgement and ingress diagnostics.

Background (TASK-300-rereview3 N4, design TASK-317-design option C): an ack row
whose message is gone used to answer ``is_acked`` on its own, so a live message
carrying the same ``(msg_id, recipient_session_id)`` disappeared with no
warning. Unit 1 quarantined the acks that already existed; this unit fixes the
judgement itself:

- every ingress goes through one ``register_or_classify`` transaction
- ``is_acked`` only believes an ack that still has a message behind it
- expiry purge tombstones the pair and removes its ack in the same transaction,
  so purging can no longer manufacture a new unmatched ack
- anything that is withheld from the normal message list is reported as a
  diagnostic instead of vanishing
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from kioku_mesh.messaging import local_index as local_index_module
from kioku_mesh.messaging import orphan_acks
from kioku_mesh.messaging.local_index import LocalMessageIndex
from kioku_mesh.messaging.models import Ack
from kioku_mesh.messaging.models import Message

# ---------------------------------------------------------------------------
# Helpers
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _write_v1_db_with_orphan(path: Path, msg_id: str, session: str, acked_at: datetime) -> None:
    """Build a pre-migration database holding one ack with no message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_DDL)
        conn.execute(
            'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
            (msg_id, session, _iso(acked_at)),
        )
        conn.commit()
    finally:
        conn.close()


def _msg(
    msg_id: str,
    *,
    session: str = 'sess-a',
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    body: str = 'hello',
) -> Message:
    return Message(
        sender_id='sender-x',
        scope='mesh',
        payload={'text': body},
        body=body,
        msg_id=msg_id,
        created_at=created_at or _now(),
        expires_at=expires_at,
        recipient={'kind': 'session', 'session_id': session},
    )


def _rows(path: Path, table: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f'SELECT * FROM {table}').fetchall()
    finally:
        conn.close()


def _pairs(path: Path, table: str) -> set[tuple[str, str]]:
    return {(r['msg_id'], r['recipient_session_id']) for r in _rows(path, table)}


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / 'messaging' / 'inbox.db'


# ---------------------------------------------------------------------------
# The N4 symptom itself: expiry purge must not manufacture a new unmatched ack
# ---------------------------------------------------------------------------


def test_purge_tombstones_the_pair_and_takes_its_ack_with_it(db_path: Path) -> None:
    """The regression that produces new orphans: purge deleted messages only.

    Deleting the message while leaving the ack behind recreates exactly the
    state unit 1 had to quarantine, so the purge has to do both — plus record
    the tombstone that lets a later arrival be recognised as retired.
    """
    index = LocalMessageIndex(db_path)
    # Registered while live and purged once its expiry has passed: an arrival
    # that is already expired never becomes a message row at all.
    index.register_or_classify(_msg('expiring', expires_at=_now() + timedelta(hours=1)), 'sess-a')
    index.record_ack(Ack(msg_id='expiring', recipient_session_id='sess-a'))

    assert index.purge_expired(now=_now() + timedelta(hours=2)) == 1

    assert _pairs(db_path, 'messages') == set()
    assert _pairs(db_path, 'acks') == set(), 'purge left an ack with no message — the N4 shape'
    assert _pairs(db_path, 'message_tombstones') == {('expiring', 'sess-a')}


def test_purge_does_not_touch_acks_of_messages_that_survive(db_path: Path) -> None:
    """Positive control: only the expiring pair is affected."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('live', expires_at=_now() + timedelta(hours=3)), 'sess-a')
    index.record_ack(Ack(msg_id='live', recipient_session_id='sess-a'))
    index.register_or_classify(_msg('dying', expires_at=_now() + timedelta(hours=1)), 'sess-a')

    index.purge_expired(now=_now() + timedelta(hours=2))

    assert _pairs(db_path, 'acks') == {('live', 'sess-a')}
    assert _pairs(db_path, 'message_tombstones') == {('dying', 'sess-a')}


def test_a_reput_after_purge_is_never_silently_dropped(db_path: Path) -> None:
    """The N4 symptom, end to end at the index level.

    Before this unit the purged pair's ack stayed behind and answered
    ``is_acked`` for the re-arriving payload, so the message was filtered out
    with nothing said about it. Now the pair is tombstoned: the message is
    still withheld (its id was retired), but the caller is told so.
    """
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('reused', expires_at=_now() + timedelta(hours=1)), 'sess-a')
    index.record_ack(Ack(msg_id='reused', recipient_session_id='sess-a'))
    index.purge_expired(now=_now() + timedelta(hours=2))

    result = index.register_or_classify(_msg('reused', expires_at=_now() + timedelta(hours=3)), 'sess-a')

    assert result.code == local_index_module.INGRESS_PROTOCOL_VIOLATION
    assert result.registered is False
    assert result.suppressed is True
    assert result.detail['tombstoned_at']
    assert result.remedy


def test_an_identical_reput_after_purge_is_a_retired_duplicate(db_path: Path) -> None:
    """A retry of the very same envelope is not a protocol violation.

    The id is still retired so the payload does not come back as new mail, but
    calling a plain transport retry a violation would cry wolf.
    """
    index = LocalMessageIndex(db_path)
    created = _now() - timedelta(minutes=5)
    expires = _now() - timedelta(seconds=1)
    index.register_or_classify(_msg('retried', created_at=created, expires_at=expires), 'sess-a')
    index.purge_expired()

    result = index.register_or_classify(_msg('retried', created_at=created, expires_at=expires), 'sess-a')

    assert result.code == local_index_module.INGRESS_DUPLICATE_RETIRED
    assert result.registered is False
    assert result.suppressed is True


def test_a_new_msg_id_after_purge_is_delivered_normally(db_path: Path) -> None:
    """The other half of the contract: resending means a new id, and that works."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('old-id', expires_at=_now() - timedelta(seconds=1)), 'sess-a')
    index.purge_expired()

    result = index.register_or_classify(_msg('new-id', expires_at=_now() + timedelta(hours=1)), 'sess-a')

    assert result.code == local_index_module.INGRESS_REGISTERED
    assert result.registered is True
    assert result.suppressed is False


# ---------------------------------------------------------------------------
# Quarantined (legacy-unknown) pairs
# ---------------------------------------------------------------------------


def test_a_quarantined_pair_reports_a_conflict_and_keeps_the_payload(db_path: Path) -> None:
    """Acceptance: a 365-day-old quarantined pair must not end at count=0."""
    _write_v1_db_with_orphan(db_path, 'collide', 'sess-a', _now() - timedelta(days=365))
    index = LocalMessageIndex(db_path)

    result = index.register_or_classify(_msg('collide'), 'sess-a')

    assert result.code == local_index_module.INGRESS_LEGACY_ACK_CONFLICT
    assert result.suppressed is True
    # The message row is created so an operator can promote the ack onto it,
    # and so the payload is not lost while the conflict is unresolved.
    assert result.registered is True
    assert _pairs(db_path, 'messages') == {('collide', 'sess-a')}
    assert result.detail['acked_at']
    assert result.detail['state'] == 'unresolved'
    assert 'orphan-acks' in (result.remedy or '')


def test_is_acked_stays_false_while_a_conflict_is_unresolved(db_path: Path) -> None:
    """The quarantined row must not become authoritative just by being looked at."""
    _write_v1_db_with_orphan(db_path, 'collide', 'sess-a', _now() - timedelta(days=365))
    index = LocalMessageIndex(db_path)

    index.register_or_classify(_msg('collide'), 'sess-a')

    assert index.is_acked('collide', 'sess-a') is False


def test_releasing_a_quarantined_pair_lets_the_message_through(db_path: Path, tmp_path: Path) -> None:
    """After release the pair is no longer a conflict, so the message is normal mail."""
    _write_v1_db_with_orphan(db_path, 'collide', 'sess-a', _now() - timedelta(days=365))
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('collide'), 'sess-a')

    orphan_acks.recover(
        db_path,
        msg_id='collide',
        recipient_session_id='sess-a',
        action='release',
        backup_path=tmp_path / 'backup.db',
        execute=True,
    )

    result = index.register_or_classify(_msg('collide'), 'sess-a')

    assert result.code == local_index_module.INGRESS_DUPLICATE_LIVE
    assert result.suppressed is False
    assert result.acked is False


def test_promoting_a_quarantined_pair_marks_the_message_acknowledged(db_path: Path, tmp_path: Path) -> None:
    """The opposite resolution: the ack was real, so the message is suppressed as acked."""
    _write_v1_db_with_orphan(db_path, 'collide', 'sess-a', _now() - timedelta(days=365))
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('collide'), 'sess-a')

    orphan_acks.recover(
        db_path,
        msg_id='collide',
        recipient_session_id='sess-a',
        action='promote',
        backup_path=tmp_path / 'backup.db',
        execute=True,
    )

    result = index.register_or_classify(_msg('collide'), 'sess-a')

    assert result.code == local_index_module.INGRESS_DUPLICATE_LIVE
    assert result.acked is True
    assert result.suppressed is True
    assert index.is_acked('collide', 'sess-a') is True


# ---------------------------------------------------------------------------
# Ack-first: legitimate, and kept apart from authoritative acks
# ---------------------------------------------------------------------------


def test_an_ack_seen_before_its_message_is_held_pending(db_path: Path) -> None:
    index = LocalMessageIndex(db_path)

    state = index.record_remote_ack(Ack(msg_id='early', recipient_session_id='sess-a'), source_key='msg/mesh/ack/x')

    assert state == 'pending'
    assert _pairs(db_path, 'pending_acks') == {('early', 'sess-a')}
    assert _pairs(db_path, 'acks') == set(), 'a pending ack must not sit in the authoritative table'
    assert index.is_acked('early', 'sess-a') is False


def test_a_pending_ack_is_promoted_when_its_message_arrives(db_path: Path) -> None:
    index = LocalMessageIndex(db_path)
    acked_at = _now() - timedelta(minutes=3)
    index.record_remote_ack(Ack(msg_id='early', recipient_session_id='sess-a', acked_at=acked_at))

    result = index.register_or_classify(_msg('early'), 'sess-a')

    assert result.code == local_index_module.INGRESS_ACK_FIRST_PROMOTED
    assert result.registered is True
    assert result.acked is True
    assert result.suppressed is True
    assert _pairs(db_path, 'pending_acks') == set()
    assert _pairs(db_path, 'acks') == {('early', 'sess-a')}
    # The original ack time travels with the promotion rather than being restamped.
    assert _rows(db_path, 'acks')[0]['acked_at'] == _iso(acked_at)
    assert index.is_acked('early', 'sess-a') is True


def test_a_remote_ack_for_a_known_message_is_authoritative_immediately(db_path: Path) -> None:
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('known'), 'sess-a')

    state = index.record_remote_ack(Ack(msg_id='known', recipient_session_id='sess-a'))

    assert state == 'authoritative'
    assert _pairs(db_path, 'pending_acks') == set()
    assert index.is_acked('known', 'sess-a') is True


# ---------------------------------------------------------------------------
# is_acked: only an ack with a message behind it counts
# ---------------------------------------------------------------------------


def test_is_acked_ignores_an_ack_row_that_has_no_message(db_path: Path) -> None:
    """The core judgement change: an ack row on its own suppresses nothing.

    A bare ack row can appear on a live database — an old writer during a
    rolling upgrade, a hand-edited row — and must not be believed.
    """
    index = LocalMessageIndex(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
            ('bare', 'sess-a', _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()

    assert index.is_acked('bare', 'sess-a') is False


def test_is_acked_is_true_for_a_normally_acknowledged_message(db_path: Path) -> None:
    """Positive control, so "always False" cannot pass the test above."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('normal'), 'sess-a')
    index.record_ack(Ack(msg_id='normal', recipient_session_id='sess-a'))

    assert index.is_acked('normal', 'sess-a') is True


# ---------------------------------------------------------------------------
# Dedup, per-session independence, concurrency, query plans
# ---------------------------------------------------------------------------


def test_a_transport_duplicate_of_a_live_message_registers_once(db_path: Path) -> None:
    index = LocalMessageIndex(db_path)
    msg = _msg('dup')

    first = index.register_or_classify(msg, 'sess-a')
    second = index.register_or_classify(msg, 'sess-a')

    assert first.code == local_index_module.INGRESS_REGISTERED
    assert second.code == local_index_module.INGRESS_DUPLICATE_LIVE
    assert second.registered is False
    assert second.suppressed is False, 'an unacked duplicate is still deliverable, not withheld'
    assert len(_rows(db_path, 'messages')) == 1


def test_recipient_sessions_are_classified_independently(db_path: Path) -> None:
    """Tombstoning one session's copy must not retire another session's."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('shared', expires_at=_now() - timedelta(seconds=1)), 'sess-a')
    index.purge_expired()

    result = index.register_or_classify(_msg('shared', session='sess-b'), 'sess-b')

    assert result.code == local_index_module.INGRESS_REGISTERED


def test_concurrent_classification_of_the_same_message_registers_once(db_path: Path) -> None:
    """Two pollers racing on one arrival must not both insert it."""
    index = LocalMessageIndex(db_path)
    msg = _msg('raced')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: index.register_or_classify(msg, 'sess-a'), range(2)))

    assert sorted(r.code for r in results) == [
        local_index_module.INGRESS_DUPLICATE_LIVE,
        local_index_module.INGRESS_REGISTERED,
    ]
    assert len(_rows(db_path, 'messages')) == 1


def test_concurrent_promotion_of_a_pending_ack_happens_once(db_path: Path) -> None:
    index = LocalMessageIndex(db_path)
    index.record_remote_ack(Ack(msg_id='raced-ack', recipient_session_id='sess-a'))
    msg = _msg('raced-ack')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: index.register_or_classify(msg, 'sess-a'), range(2)))

    assert sum(r.code == local_index_module.INGRESS_ACK_FIRST_PROMOTED for r in results) == 1
    assert len(_rows(db_path, 'acks')) == 1
    assert _pairs(db_path, 'pending_acks') == set()


@pytest.mark.parametrize(
    'table',
    ['messages', 'acks', 'message_tombstones', 'pending_acks', 'legacy_unknown_acks'],
)
def test_hot_path_lookups_use_an_index_rather_than_a_scan(db_path: Path, table: str) -> None:
    """Design requirement: classification stays bounded as the tables grow."""
    LocalMessageIndex(db_path)
    conn = sqlite3.connect(db_path)
    try:
        plan = conn.execute(
            f'EXPLAIN QUERY PLAN SELECT 1 FROM {table} WHERE msg_id = ? AND recipient_session_id = ?',  # noqa: S608
            ('x', 'y'),
        ).fetchall()
    finally:
        conn.close()
    detail = ' '.join(str(row[-1]) for row in plan)
    assert 'SCAN' not in detail, detail
    assert 'SEARCH' in detail, detail


def test_classification_stays_bounded_on_a_large_quarantine(db_path: Path) -> None:
    """100k quarantined rows must not slow an unrelated arrival down."""
    index = LocalMessageIndex(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            'INSERT INTO legacy_unknown_acks (msg_id, recipient_session_id, acked_at, migrated_at, state)'
            " VALUES (?, ?, ?, ?, 'unresolved')",
            [(f'legacy-{i}', 'sess-a', _iso(_now()), _iso(_now())) for i in range(100_000)],
        )
        conn.commit()
    finally:
        conn.close()

    result = index.register_or_classify(_msg('fresh'), 'sess-a')

    assert result.code == local_index_module.INGRESS_REGISTERED


# ---------------------------------------------------------------------------
# check_messages: the diagnostics contract
# ---------------------------------------------------------------------------

pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402

from kioku_mesh.mcp_server import mcp  # noqa: E402
import kioku_mesh.mcp_server as mcp_module  # noqa: E402


def _reply(msg: Message, key: str) -> MagicMock:
    reply = MagicMock()
    reply.ok = MagicMock()
    reply.ok.key_expr = key
    reply.ok.payload.to_bytes.return_value = msg.to_json().encode('utf-8')
    return reply


def _check_messages(tmp_path: Path, msg: Message, session_id: str, **kwargs: object) -> dict:
    mcp_module._messaging_index = None
    mock_session = MagicMock()
    mock_session.get.return_value = [_reply(msg, f'msg/mesh/inbox/session/{session_id}/{msg.msg_id}')]

    async def _go() -> dict:
        async with Client(mcp) as client:
            result = await client.call_tool('check_messages', kwargs)
            return json.loads(result.data)

    with (
        patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
        patch('kioku_mesh.mcp_server.get_session_id', return_value=session_id),
        patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
    ):
        return asyncio.run(_go())


def test_check_messages_reports_a_legacy_conflict_instead_of_dropping_it(tmp_path: Path) -> None:
    """Acceptance: a live message on a quarantined pair is not just count=0."""
    _write_v1_db_with_orphan(tmp_path / 'messaging' / 'inbox.db', 'collide', 'n4-sess', _now() - timedelta(days=365))

    result = _check_messages(tmp_path, _msg('collide', session='n4-sess', body='please read me'), 'n4-sess')

    assert result['count'] == 0
    (diag,) = result['diagnostics']
    assert diag['code'] == 'legacy_ack_conflict'
    assert diag['msg_id'] == 'collide'
    assert diag['recipient_session_id'] == 'n4-sess'
    assert diag['message']['body'] == 'please read me', 'the payload must survive the conflict'
    assert diag['ack']['acked_at']
    assert 'orphan-acks list' in diag['remedy']


def test_check_messages_reports_a_retired_id_instead_of_dropping_it(tmp_path: Path) -> None:
    """The purge-then-reput path, through the real tool."""
    db = tmp_path / 'messaging' / 'inbox.db'
    index = LocalMessageIndex(db)
    index.register_or_classify(_msg('reused', session='n4-sess', expires_at=_now() - timedelta(seconds=1)), 'n4-sess')
    index.purge_expired()

    result = _check_messages(
        tmp_path,
        _msg('reused', session='n4-sess', expires_at=_now() + timedelta(hours=1)),
        'n4-sess',
    )

    assert result['count'] == 0
    (diag,) = result['diagnostics']
    assert diag['code'] in {'protocol_violation', 'duplicate_retired'}
    assert diag['msg_id'] == 'reused'


def test_check_messages_returns_a_released_message_normally(tmp_path: Path) -> None:
    """End to end: release resolves the conflict and the reply arrives as mail."""
    db = tmp_path / 'messaging' / 'inbox.db'
    _write_v1_db_with_orphan(db, 'collide', 'n4-sess', _now() - timedelta(days=365))
    msg = _msg('collide', session='n4-sess', body='the reply')
    _check_messages(tmp_path, msg, 'n4-sess')

    orphan_acks.recover(
        db,
        msg_id='collide',
        recipient_session_id='n4-sess',
        action='release',
        backup_path=tmp_path / 'backup.db',
        execute=True,
    )
    result = _check_messages(tmp_path, msg, 'n4-sess')

    assert result['count'] == 1
    assert result['messages'][0]['body'] == 'the reply'
    assert result['diagnostics'] == []


def test_check_messages_keeps_its_existing_shape_for_ordinary_mail(tmp_path: Path) -> None:
    """Backward compatibility: the added key is additive and empty by default."""
    result = _check_messages(tmp_path, _msg('plain', session='ok-sess'), 'ok-sess')

    assert result['count'] == 1
    assert result['truncated'] is False
    assert result['messages'][0]['msg_id'] == 'plain'
    assert result['diagnostics'] == []


# ---------------------------------------------------------------------------
# No ingress may bypass the classifier
# ---------------------------------------------------------------------------


def test_check_messages_routes_every_arrival_through_the_classifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ingress that inserted messages itself would reintroduce N4."""
    seen: list[tuple[str, str]] = []
    original = LocalMessageIndex.register_or_classify

    def _spy(self, msg: Message, recipient_session_id: str):  # noqa: ANN001, ANN202
        seen.append((msg.msg_id, recipient_session_id))
        return original(self, msg, recipient_session_id)

    monkeypatch.setattr(LocalMessageIndex, 'register_or_classify', _spy)

    _check_messages(tmp_path, _msg('routed', session='route-sess'), 'route-sess')

    assert seen == [('routed', 'route-sess')]


def test_registering_a_message_is_written_in_exactly_one_place() -> None:
    """Structural guard: one INSERT site means one place that can be wrong."""
    src = Path(local_index_module.__file__).parent.parent
    offenders = [
        path.relative_to(src).as_posix()
        for path in sorted(src.rglob('*.py'))
        if 'INSERT INTO messages' in path.read_text(encoding='utf-8')
        or 'INSERT OR REPLACE INTO messages' in path.read_text(encoding='utf-8')
    ]

    assert offenders == ['messaging/local_index.py'], offenders


def test_the_plain_register_helper_still_goes_through_the_classifier(db_path: Path) -> None:
    """``register`` is kept for existing callers, but not as a side door."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('retire-me', expires_at=_now() - timedelta(seconds=1)), 'sess-a')
    index.purge_expired()

    assert index.register(_msg('retire-me', expires_at=_now() + timedelta(hours=1)), 'sess-a') is False
    assert _pairs(db_path, 'messages') == set()


# ---------------------------------------------------------------------------
# PR305-B1: an ack row written after the migration must not become authoritative
# ---------------------------------------------------------------------------


def _insert_bare_ack(path: Path, msg_id: str, session: str, acked_at: datetime | None = None) -> None:
    """Write an ack with no message into an already-migrated database.

    The shape an old writer leaves behind during a rolling upgrade: the
    migration pass classified whatever existed when the database was opened and
    is never run again, so a row written afterwards is not covered by it.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
            (msg_id, session, _iso(acked_at or _now())),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_bare_ack_written_after_the_migration_is_quarantined_on_arrival(db_path: Path) -> None:
    """The N4 shape, reached without a pre-migration database."""
    index = LocalMessageIndex(db_path)
    _insert_bare_ack(db_path, 'late-ack', 'sess-a')

    result = index.register_or_classify(_msg('late-ack'), 'sess-a')

    assert result.code == local_index_module.INGRESS_LEGACY_ACK_CONFLICT
    assert result.suppressed is True
    assert result.acked is False
    assert index.is_acked('late-ack', 'sess-a') is False
    assert _pairs(db_path, 'acks') == set(), 'the bare ack must not stay authoritative'
    assert _pairs(db_path, 'legacy_unknown_acks') == {('late-ack', 'sess-a')}


def test_quarantining_a_bare_ack_records_where_it_came_from(db_path: Path) -> None:
    """Release or promote is an operator judgement; provenance is the evidence."""
    index = LocalMessageIndex(db_path)
    _insert_bare_ack(db_path, 'late-ack', 'sess-a')
    index.register_or_classify(_msg('late-ack'), 'sess-a')

    (row,) = _rows(db_path, 'legacy_unknown_acks')

    assert row['provenance'] == 'post_migration_ack'
    assert row['state'] == 'unresolved'


def test_check_messages_reports_a_bare_ack_conflict_instead_of_dropping_it(tmp_path: Path) -> None:
    """Acceptance for PR305-B1: never count=0 with empty diagnostics."""
    LocalMessageIndex(tmp_path / 'messaging' / 'inbox.db')
    _insert_bare_ack(tmp_path / 'messaging' / 'inbox.db', 'late-ack', 'n4-sess')

    result = _check_messages(tmp_path, _msg('late-ack', session='n4-sess', body='still live'), 'n4-sess')

    assert result['count'] == 0
    (diag,) = result['diagnostics']
    assert diag['code'] == 'legacy_ack_conflict'
    assert diag['message']['body'] == 'still live'
    assert 'orphan-acks' in diag['remedy']


def test_a_quarantined_pair_stays_withheld_on_later_polls(tmp_path: Path) -> None:
    """The conflict is not a one-poll warning that then lets the mail through."""
    LocalMessageIndex(tmp_path / 'messaging' / 'inbox.db')
    _insert_bare_ack(tmp_path / 'messaging' / 'inbox.db', 'late-ack', 'n4-sess')
    msg = _msg('late-ack', session='n4-sess', body='still live')

    first = _check_messages(tmp_path, msg, 'n4-sess')
    second = _check_messages(tmp_path, msg, 'n4-sess')

    assert first['count'] == 0
    assert second['count'] == 0, 'an unresolved quarantine must not expire after one poll'
    assert second['diagnostics'][0]['code'] == 'legacy_ack_conflict'


def test_a_bare_ack_on_a_tombstoned_pair_is_quarantined_too(db_path: Path) -> None:
    """The retired verdict must not leave the ack behind to suppress a later poll."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('retired', expires_at=_now() - timedelta(seconds=1)), 'sess-a')
    index.purge_expired()
    _insert_bare_ack(db_path, 'retired', 'sess-a')

    result = index.register_or_classify(_msg('retired', expires_at=_now() + timedelta(hours=1)), 'sess-a')

    assert result.suppressed is True
    assert _pairs(db_path, 'acks') == set()


# ---------------------------------------------------------------------------
# PR305-B2: an arrival that is already expired still goes through the classifier
# ---------------------------------------------------------------------------


def _tombstones(tmp_path: Path) -> set[tuple[str, str]]:
    return _pairs(tmp_path / 'messaging' / 'inbox.db', 'message_tombstones')


def test_an_expired_first_arrival_retires_its_id(tmp_path: Path) -> None:
    """Acceptance for PR305-B2: the id is retired even if it never was live."""
    result = _check_messages(
        tmp_path,
        _msg('too-late', session='exp-sess', expires_at=_now() - timedelta(seconds=1)),
        'exp-sess',
    )

    assert result['count'] == 0
    assert _tombstones(tmp_path) == {('too-late', 'exp-sess')}


def test_an_expired_first_arrival_is_reported_rather_than_dropped(tmp_path: Path) -> None:
    """A message that expired before it was ever read is worth exactly one word."""
    result = _check_messages(
        tmp_path,
        _msg('too-late', session='exp-sess', body='was urgent', expires_at=_now() - timedelta(seconds=1)),
        'exp-sess',
    )

    (diag,) = result['diagnostics']
    assert diag['code'] == 'expired_on_arrival'
    assert diag['message']['body'] == 'was urgent'
    assert diag['remedy']


def test_an_id_retired_by_an_expired_arrival_cannot_be_reused(tmp_path: Path) -> None:
    """Codex's PR305-B2 probe: the second, live message must not sail through."""
    _check_messages(
        tmp_path,
        _msg('reuse-me', session='exp-sess', expires_at=_now() - timedelta(seconds=1)),
        'exp-sess',
    )

    result = _check_messages(
        tmp_path,
        _msg('reuse-me', session='exp-sess', body='different', expires_at=_now() + timedelta(hours=1)),
        'exp-sess',
    )

    assert result['count'] == 0, 'a retired id must not carry a new message'
    (diag,) = result['diagnostics']
    assert diag['code'] == 'protocol_violation'


def test_an_expired_arrival_is_reported_only_once(tmp_path: Path) -> None:
    """The retry of an expired arrival is a duplicate, not news."""
    msg = _msg('too-late', session='exp-sess', expires_at=_now() - timedelta(seconds=1))
    _check_messages(tmp_path, msg, 'exp-sess')

    second = _check_messages(tmp_path, msg, 'exp-sess')

    assert second['count'] == 0
    assert [d['code'] for d in second['diagnostics']] == ['duplicate_retired']


# ---------------------------------------------------------------------------
# PR305-B3: a classifier failure is a diagnostic, not silence
# ---------------------------------------------------------------------------


def test_a_classifier_failure_is_reported_instead_of_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance for PR305-B3: 'the index is locked' must not read as 'no mail'."""

    def _boom(self, msg: Message, recipient_session_id: str):  # noqa: ANN001, ANN202
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(LocalMessageIndex, 'register_or_classify', _boom)

    result = _check_messages(tmp_path, _msg('unclassified', session='err-sess', body='keep me'), 'err-sess')

    assert result['count'] == 0
    (diag,) = result['diagnostics']
    assert diag['code'] == 'classification_failed'
    assert diag['message']['body'] == 'keep me'
    assert 'OperationalError' in diag['ack']['error']


def test_a_failed_classification_leaves_the_message_to_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message dropped by a transient failure has to come back on the next poll."""
    calls: list[str] = []
    original = LocalMessageIndex.register_or_classify

    def _fail_once(self, msg: Message, recipient_session_id: str):  # noqa: ANN001, ANN202
        calls.append(msg.msg_id)
        if len(calls) == 1:
            raise sqlite3.OperationalError('database is locked')
        return original(self, msg, recipient_session_id)

    monkeypatch.setattr(LocalMessageIndex, 'register_or_classify', _fail_once)
    msg = _msg('retry-me', session='err-sess', body='keep me')
    _check_messages(tmp_path, msg, 'err-sess')

    result = _check_messages(tmp_path, msg, 'err-sess')

    assert result['count'] == 1
    assert result['messages'][0]['body'] == 'keep me'


def test_a_selector_failure_is_reported_instead_of_swallowed(tmp_path: Path) -> None:
    """A failed query means 'your inbox listing is incomplete', not 'empty'."""
    mcp_module._messaging_index = None
    mock_session = MagicMock()
    mock_session.get.side_effect = RuntimeError('zenoh query failed')

    async def _go() -> dict:
        async with Client(mcp) as client:
            return json.loads((await client.call_tool('check_messages', {})).data)

    with (
        patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
        patch('kioku_mesh.mcp_server.get_session_id', return_value='sel-sess'),
        patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
    ):
        result = asyncio.run(_go())

    assert result['count'] == 0
    codes = {d['code'] for d in result['diagnostics']}
    assert codes == {'selector_failed'}
    assert 'zenoh query failed' in result['diagnostics'][0]['ack']['error']


def test_an_undecodable_arrival_is_reported_instead_of_swallowed(tmp_path: Path) -> None:
    """A payload that will not parse is a delivery failure the caller can see."""
    mcp_module._messaging_index = None
    reply = MagicMock()
    reply.ok = MagicMock()
    reply.ok.key_expr = 'msg/mesh/inbox/session/bad-sess/garbled'
    reply.ok.payload.to_bytes.return_value = b'{not json'
    mock_session = MagicMock()
    mock_session.get.return_value = [reply]

    async def _go() -> dict:
        async with Client(mcp) as client:
            return json.loads((await client.call_tool('check_messages', {})).data)

    with (
        patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
        patch('kioku_mesh.mcp_server.get_session_id', return_value='bad-sess'),
        patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
    ):
        result = asyncio.run(_go())

    assert result['count'] == 0
    assert {d['code'] for d in result['diagnostics']} == {'arrival_undecodable'}


# ---------------------------------------------------------------------------
# Step 6: every suppression path says why (the enumeration, pinned)
# ---------------------------------------------------------------------------


def _arrange_legacy_conflict(tmp_path: Path) -> Message:
    LocalMessageIndex(tmp_path / 'messaging' / 'inbox.db')
    _insert_bare_ack(tmp_path / 'messaging' / 'inbox.db', 'sup-1', 'sup-sess')
    return _msg('sup-1', session='sup-sess')


def _arrange_duplicate_retired(tmp_path: Path) -> Message:
    expired = _msg('sup-2', session='sup-sess', expires_at=_now() - timedelta(seconds=1))
    _check_messages(tmp_path, expired, 'sup-sess')
    return expired


def _arrange_protocol_violation(tmp_path: Path) -> Message:
    _check_messages(tmp_path, _msg('sup-3', session='sup-sess', expires_at=_now() - timedelta(seconds=1)), 'sup-sess')
    return _msg('sup-3', session='sup-sess', expires_at=_now() + timedelta(hours=1))


def _arrange_expired_on_arrival(tmp_path: Path) -> Message:
    return _msg('sup-4', session='sup-sess', expires_at=_now() - timedelta(seconds=1))


def _arrange_ack_first_promoted(tmp_path: Path) -> Message:
    index = LocalMessageIndex(tmp_path / 'messaging' / 'inbox.db')
    index.record_remote_ack(Ack(msg_id='sup-5', recipient_session_id='sup-sess'), source_key='msg/mesh/ack/sup-5')
    return _msg('sup-5', session='sup-sess')


@pytest.mark.parametrize(
    ('arrange', 'expected_code'),
    [
        (_arrange_legacy_conflict, 'legacy_ack_conflict'),
        (_arrange_duplicate_retired, 'duplicate_retired'),
        (_arrange_protocol_violation, 'protocol_violation'),
        (_arrange_expired_on_arrival, 'expired_on_arrival'),
        (_arrange_ack_first_promoted, 'ack_first_promoted'),
    ],
)
def test_every_withholding_path_names_itself(tmp_path: Path, arrange, expected_code: str) -> None:  # noqa: ANN001
    """No arrival leaves check_messages without either delivery or a reason."""
    msg = arrange(tmp_path)

    result = _check_messages(tmp_path, msg, 'sup-sess')

    assert result['count'] == 0
    assert [d['code'] for d in result['diagnostics']] == [expected_code]
    assert result['diagnostics'][0]['msg_id'] == msg.msg_id


def test_every_ingress_code_is_deliberately_diagnostic_or_not() -> None:
    """A new code has to be classified on purpose, not default into silence."""
    delivered_or_already_read = {
        local_index_module.INGRESS_REGISTERED,
        local_index_module.INGRESS_DUPLICATE_LIVE,
    }
    all_codes = {
        value
        for name, value in vars(local_index_module).items()
        if name.startswith('INGRESS_') and isinstance(value, str)
    }

    assert all_codes - delivered_or_already_read == set(local_index_module.INGRESS_DIAGNOSTIC_CODES)


# ---------------------------------------------------------------------------
# The v4 upgrade, and the single source of truth for "already read"
# ---------------------------------------------------------------------------


def test_a_v3_database_gains_the_provenance_column_without_losing_rows(db_path: Path) -> None:
    """Upgrading in place must not need the quarantine to be rebuilt."""
    LocalMessageIndex(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('ALTER TABLE legacy_unknown_acks DROP COLUMN provenance')
        conn.execute(
            'INSERT INTO legacy_unknown_acks (msg_id, recipient_session_id, acked_at, migrated_at, state)'
            " VALUES ('old', 'sess-a', ?, ?, 'unresolved')",
            (_iso(_now()), _iso(_now())),
        )
        conn.execute('DELETE FROM messaging_schema_version')
        conn.execute('INSERT INTO messaging_schema_version (version) VALUES (3)')
        conn.commit()
    finally:
        conn.close()

    LocalMessageIndex(db_path)

    (row,) = _rows(db_path, 'legacy_unknown_acks')
    assert row['msg_id'] == 'old'
    assert row['provenance'] == 'migration', 'a row found by the upgrade pass is what the default records'


def test_delivery_uses_the_classifier_verdict_rather_than_a_second_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ack row appearing mid-poll must not make a just-classified message vanish."""
    monkeypatch.setattr(LocalMessageIndex, 'is_acked', lambda self, msg_id, session: True)

    result = _check_messages(tmp_path, _msg('mid-poll', session='race-sess', body='do not vanish'), 'race-sess')

    assert result['count'] == 1, 'suppression must come from the transaction that classified the arrival'
    assert result['messages'][0]['acked'] is False


def test_an_expired_arrival_is_retired_without_ever_being_registered(db_path: Path) -> None:
    """The tombstone is written by the classifier, not by a later purge."""
    index = LocalMessageIndex(db_path)

    result = index.register_or_classify(_msg('dead-on-arrival', expires_at=_now() - timedelta(seconds=1)), 'sess-a')

    assert result.code == local_index_module.INGRESS_EXPIRED_ON_ARRIVAL
    assert result.registered is False
    assert _pairs(db_path, 'messages') == set()
    assert _pairs(db_path, 'message_tombstones') == {('dead-on-arrival', 'sess-a')}


def test_a_failed_purge_cannot_make_a_retired_id_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found by fault injection: the retire must not depend on a second write.

    ``check_messages`` purges after the poll and swallows a purge failure. If
    retiring an expired arrival happened there, one failed purge would leave the
    id live and the next, different message on it would be delivered as new mail
    — the immutable-msg_id rule holding only as long as an unrelated write
    succeeds.
    """

    def _boom(self, now: datetime | None = None) -> int:  # noqa: ANN001
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(LocalMessageIndex, 'purge_expired', _boom)
    _check_messages(
        tmp_path,
        _msg('purge-fail', session='pf-sess', expires_at=_now() - timedelta(seconds=1)),
        'pf-sess',
    )
    monkeypatch.undo()

    result = _check_messages(
        tmp_path,
        _msg('purge-fail', session='pf-sess', body='reused id', expires_at=_now() + timedelta(hours=1)),
        'pf-sess',
    )

    assert result['count'] == 0
    assert [d['code'] for d in result['diagnostics']] == ['protocol_violation']

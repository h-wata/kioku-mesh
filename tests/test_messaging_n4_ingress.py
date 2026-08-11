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
    msg = _msg('expiring', expires_at=_now() - timedelta(seconds=1))
    index.register_or_classify(msg, 'sess-a')
    index.record_ack(Ack(msg_id='expiring', recipient_session_id='sess-a'))

    assert index.purge_expired() == 1

    assert _pairs(db_path, 'messages') == set()
    assert _pairs(db_path, 'acks') == set(), 'purge left an ack with no message — the N4 shape'
    assert _pairs(db_path, 'message_tombstones') == {('expiring', 'sess-a')}


def test_purge_does_not_touch_acks_of_messages_that_survive(db_path: Path) -> None:
    """Positive control: only the expiring pair is affected."""
    index = LocalMessageIndex(db_path)
    index.register_or_classify(_msg('live', expires_at=_now() + timedelta(hours=1)), 'sess-a')
    index.record_ack(Ack(msg_id='live', recipient_session_id='sess-a'))
    index.register_or_classify(_msg('dying', expires_at=_now() - timedelta(seconds=1)), 'sess-a')

    index.purge_expired()

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
    index.register_or_classify(_msg('reused', expires_at=_now() - timedelta(seconds=1)), 'sess-a')
    index.record_ack(Ack(msg_id='reused', recipient_session_id='sess-a'))
    index.purge_expired()

    result = index.register_or_classify(_msg('reused', expires_at=_now() + timedelta(hours=1)), 'sess-a')

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

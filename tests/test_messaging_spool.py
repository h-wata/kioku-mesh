"""Unit tests for messaging spool, models, and local_index (Phase 1)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from kioku_mesh.messaging.local_index import ack_message
from kioku_mesh.messaging.local_index import LocalMessageIndex
from kioku_mesh.messaging.models import Ack
from kioku_mesh.messaging.models import is_expired
from kioku_mesh.messaging.models import Message
from kioku_mesh.messaging.spool import check_inbox
from kioku_mesh.messaging.spool import MessageSpool
from kioku_mesh.messaging.spool import send_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(
    scope: str = 'mesh',
    ttl_sec: int | None = 900,
    expires_at: datetime | None = None,
    sender_id: str = 'test-sender',
    **kwargs: object,
) -> Message:
    return Message(
        sender_id=sender_id,
        scope=scope,
        payload={'text': 'hello'},
        ttl_sec=ttl_sec,
        expires_at=expires_at,
        **kwargs,
    )


def _past(seconds: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _future(seconds: int = 900) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# is_expired
# ---------------------------------------------------------------------------


class TestIsExpired:
    def test_future_expires_at_not_expired(self) -> None:
        msg = _make_msg(expires_at=_future(), ttl_sec=None)
        assert not is_expired(msg)

    def test_past_expires_at_is_expired(self) -> None:
        msg = _make_msg(expires_at=_past(), ttl_sec=None)
        assert is_expired(msg)

    def test_ttl_sec_not_expired(self) -> None:
        msg = _make_msg(ttl_sec=900)
        assert not is_expired(msg)

    def test_ttl_sec_expired(self) -> None:
        old_created = datetime.now(timezone.utc) - timedelta(seconds=1000)
        msg = _make_msg(ttl_sec=900, created_at=old_created)
        assert is_expired(msg)

    def test_no_ttl_never_expires(self) -> None:
        msg = _make_msg(expires_at=None, ttl_sec=None)
        assert not is_expired(msg)

    def test_expires_at_takes_precedence_over_ttl_sec(self) -> None:
        # expires_at in past → expired, even though ttl_sec is large
        msg = _make_msg(expires_at=_past(), ttl_sec=9999)
        assert is_expired(msg)


# ---------------------------------------------------------------------------
# MessageSpool
# ---------------------------------------------------------------------------


class TestMessageSpool:
    def test_put_and_get(self) -> None:
        spool = MessageSpool()
        msg = _make_msg()
        spool.put(msg)
        assert spool.get(msg.msg_id) == msg

    def test_get_missing_returns_none(self) -> None:
        spool = MessageSpool()
        assert spool.get('nonexistent') is None

    def test_get_expired_returns_none(self) -> None:
        spool = MessageSpool()
        msg = _make_msg(expires_at=_past(), ttl_sec=None)
        spool.put(msg)
        assert spool.get(msg.msg_id) is None

    def test_idempotent_put(self) -> None:
        spool = MessageSpool()
        msg = _make_msg()
        spool.put(msg)
        spool.put(msg)  # second call must be a no-op
        active = spool.list_active()
        assert len([m for m in active if m.msg_id == msg.msg_id]) == 1

    def test_list_active_excludes_expired(self) -> None:
        spool = MessageSpool()
        active = _make_msg()
        expired = _make_msg(expires_at=_past(), ttl_sec=None)
        spool.put(active)
        spool.put(expired)
        result = spool.list_active()
        assert active in result
        assert expired not in result

    def test_list_active_scope_filter(self) -> None:
        spool = MessageSpool()
        team_msg = _make_msg(scope='team/kioku-mesh')
        mesh_msg = _make_msg(scope='mesh')
        spool.put(team_msg)
        spool.put(mesh_msg)
        assert spool.list_active(scope='team/kioku-mesh') == [team_msg]
        assert spool.list_active(scope='mesh') == [mesh_msg]

    def test_remove(self) -> None:
        spool = MessageSpool()
        msg = _make_msg()
        spool.put(msg)
        spool.remove(msg.msg_id)
        assert spool.get(msg.msg_id) is None

    def test_purge_expired(self) -> None:
        spool = MessageSpool()
        active = _make_msg()
        expired = _make_msg(expires_at=_past(), ttl_sec=None)
        spool.put(active)
        spool.put(expired)
        count = spool.purge_expired()
        assert count == 1
        assert spool.get(active.msg_id) is not None


# ---------------------------------------------------------------------------
# send_message / check_inbox
# ---------------------------------------------------------------------------


class TestSendCheckAPI:
    def test_send_message_returns_msg_id(self) -> None:
        spool = MessageSpool()
        msg = _make_msg(scope='mesh')
        returned_id = send_message(spool, msg)
        assert returned_id == msg.msg_id

    def test_check_inbox_returns_active(self) -> None:
        spool = MessageSpool()
        msg = _make_msg(scope='team/kioku-mesh')
        send_message(spool, msg)
        result = check_inbox(spool, 'team/kioku-mesh')
        assert msg in result

    def test_check_inbox_scope_filter(self) -> None:
        spool = MessageSpool()
        msg_mesh = _make_msg(scope='mesh')
        msg_team = _make_msg(scope='team/kioku-mesh')
        send_message(spool, msg_mesh)
        send_message(spool, msg_team)
        assert msg_mesh in check_inbox(spool, 'mesh')
        assert msg_team not in check_inbox(spool, 'mesh')

    def test_check_inbox_excludes_expired(self) -> None:
        spool = MessageSpool()
        expired = _make_msg(scope='mesh', expires_at=_past(), ttl_sec=None)
        send_message(spool, expired)
        assert check_inbox(spool, 'mesh') == []


# ---------------------------------------------------------------------------
# LocalMessageIndex
# ---------------------------------------------------------------------------


class TestLocalMessageIndex:
    def test_register_inserts(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg(expires_at=_future(), ttl_sec=None)
        assert idx.register(msg, 'test-session') is True

    def test_register_dedup_returns_false(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        assert idx.register(msg, 'test-session') is True
        assert idx.register(msg, 'test-session') is False

    def test_ack_message_flow(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg, 'sess-abc')
        ack = ack_message(idx, msg.msg_id, 'sess-abc')
        assert isinstance(ack, Ack)
        assert ack.msg_id == msg.msg_id
        assert ack.recipient_session_id == 'sess-abc'
        assert idx.is_acked(msg.msg_id, 'sess-abc')

    def test_is_acked_false_before_ack(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg, 'sess-xyz')
        assert not idx.is_acked(msg.msg_id, 'sess-xyz')

    def test_list_unacked_scope_filter(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg_mesh = _make_msg(scope='mesh')
        msg_team = _make_msg(scope='team/kioku-mesh')
        idx.register(msg_mesh, 'test-session')
        idx.register(msg_team, 'test-session')
        unacked_mesh = idx.list_unacked('test-session', scope='mesh')
        assert msg_mesh.msg_id in unacked_mesh
        assert msg_team.msg_id not in unacked_mesh

    def test_list_unacked_excludes_acked(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg, 'sess-abc')
        ack_message(idx, msg.msg_id, 'sess-abc')
        assert msg.msg_id not in idx.list_unacked('sess-abc')

    def test_purge_expired(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        active = _make_msg(expires_at=_future(), ttl_sec=None)
        expired = _make_msg(expires_at=_past(), ttl_sec=None)
        idx.register(active, 'test-session')
        idx.register(expired, 'test-session')
        count = idx.purge_expired()
        assert count == 1
        remaining = idx.list_unacked('test-session')
        assert active.msg_id in remaining
        assert expired.msg_id not in remaining

    def test_purge_expired_with_no_expires_at(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg(expires_at=None, ttl_sec=None)  # no expiry
        idx.register(msg, 'test-session')
        count = idx.purge_expired()
        assert count == 0
        assert msg.msg_id in idx.list_unacked('test-session')

    def test_two_sessions_ack_independently(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg, 'sess-1')
        idx.register(msg, 'sess-2')
        ack_message(idx, msg.msg_id, 'sess-1')
        assert idx.is_acked(msg.msg_id, 'sess-1')
        assert not idx.is_acked(msg.msg_id, 'sess-2')
        assert msg.msg_id not in idx.list_unacked('sess-1')
        assert msg.msg_id in idx.list_unacked('sess-2')

    def test_ack_unknown_msg_raises_value_error(self, tmp_path: Path) -> None:
        import pytest

        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        with pytest.raises(ValueError, match='unknown msg_id'):
            ack_message(idx, 'nonexistent-id', 'sess-abc')

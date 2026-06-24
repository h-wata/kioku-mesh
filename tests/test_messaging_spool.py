"""Unit tests for messaging spool, models, and local_index (Phase 1)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from mesh_mem.messaging.local_index import ack_message
from mesh_mem.messaging.local_index import LocalMessageIndex
from mesh_mem.messaging.models import Ack
from mesh_mem.messaging.models import is_expired
from mesh_mem.messaging.models import Message
from mesh_mem.messaging.spool import check_inbox
from mesh_mem.messaging.spool import MessageSpool
from mesh_mem.messaging.spool import send_message

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
        assert idx.register(msg) is True

    def test_register_dedup_returns_false(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        assert idx.register(msg) is True
        assert idx.register(msg) is False

    def test_ack_message_flow(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg)
        ack = ack_message(idx, msg.msg_id, 'sess-abc')
        assert isinstance(ack, Ack)
        assert ack.msg_id == msg.msg_id
        assert ack.recipient_session_id == 'sess-abc'
        assert idx.is_acked(msg.msg_id, 'sess-abc')

    def test_is_acked_false_before_ack(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg)
        assert not idx.is_acked(msg.msg_id, 'sess-xyz')

    def test_list_unacked_scope_filter(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg_mesh = _make_msg(scope='mesh')
        msg_team = _make_msg(scope='team/kioku-mesh')
        idx.register(msg_mesh)
        idx.register(msg_team)
        unacked_mesh = idx.list_unacked(scope='mesh')
        assert msg_mesh.msg_id in unacked_mesh
        assert msg_team.msg_id not in unacked_mesh

    def test_list_unacked_excludes_acked(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg()
        idx.register(msg)
        ack_message(idx, msg.msg_id, 'sess-abc')
        assert msg.msg_id not in idx.list_unacked()

    def test_purge_expired(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        active = _make_msg(expires_at=_future(), ttl_sec=None)
        expired = _make_msg(expires_at=_past(), ttl_sec=None)
        idx.register(active)
        idx.register(expired)
        count = idx.purge_expired()
        assert count == 1
        remaining = idx.list_unacked()
        assert active.msg_id in remaining
        assert expired.msg_id not in remaining

    def test_purge_expired_with_no_expires_at(self, tmp_path: Path) -> None:
        idx = LocalMessageIndex(tmp_path / 'inbox.db')
        msg = _make_msg(expires_at=None, ttl_sec=None)  # no expiry
        idx.register(msg)
        count = idx.purge_expired()
        assert count == 0
        assert msg.msg_id in idx.list_unacked()

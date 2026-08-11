"""Regression tests for storage-level TTL purge (Issue #215).

Verifies that:
- Expired messages are DELETED from Zenoh storage (not merely filtered).
- Non-expired messages are NOT deleted from Zenoh storage.
- purge_expired_msgs also cleans the local SQLite inbox index.
- check_messages performs inline lazy-delete for expired messages it encounters.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402

from kioku_mesh.mcp_server import mcp  # noqa: E402
import kioku_mesh.mcp_server as mcp_module  # noqa: E402
from kioku_mesh.messaging.local_index import _iso  # noqa: E402
from kioku_mesh.messaging.local_index import LocalMessageIndex  # noqa: E402
from kioku_mesh.messaging.models import Ack  # noqa: E402
from kioku_mesh.messaging.models import Message  # noqa: E402
from kioku_mesh.messaging.purge import purge_expired_msgs  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _past(seconds: int = 1) -> datetime:
    return _utc_now() - timedelta(seconds=seconds)


def _future(seconds: int = 900) -> datetime:
    return _utc_now() + timedelta(seconds=seconds)


def _make_msg(
    scope: str = 'mesh',
    session_id: str = 'test-sess',
    expires_at: datetime | None = None,
    ttl_sec: int | None = 900,
    body: str = 'hello',
) -> Message:
    return Message(
        sender_id='sender-x',
        scope=scope,
        payload={'text': body},
        body=body,
        recipient={'kind': 'session', 'session_id': session_id},
        expires_at=expires_at or _future(ttl_sec or 900),
        ttl_sec=ttl_sec,
    )


def _make_zenoh_reply(msg: Message, key: str = 'msg/mesh/inbox/session/test-sess/abc123') -> MagicMock:
    """Build a mock Zenoh reply carrying ``msg`` at ``key``."""
    reply = MagicMock()
    reply.ok = MagicMock()
    reply.ok.key_expr = key
    reply.ok.payload.to_bytes.return_value = msg.to_json().encode('utf-8')
    return reply


def _reset_index(tmp_path: Path) -> None:
    mcp_module._messaging_index = None


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# purge_expired_msgs — standalone function
# ---------------------------------------------------------------------------


class TestPurgeExpiredMsgs:
    def _make_index(self, tmp_path: Path) -> LocalMessageIndex:
        return LocalMessageIndex(tmp_path / 'messaging' / 'inbox.db')

    def test_deletes_expired_key_from_zenoh(self, tmp_path: Path) -> None:
        """Expired message is deleted from Zenoh storage (session.delete called)."""
        expired_key = 'msg/mesh/inbox/session/s/aaa'
        expired_msg = _make_msg(expires_at=_past(1))
        reply = _make_zenoh_reply(expired_msg, key=expired_key)

        session = MagicMock()
        session.get.return_value = [reply]
        index = self._make_index(tmp_path)

        count, scan_ok = purge_expired_msgs(session, index)

        assert scan_ok is True
        assert count == 1
        session.delete.assert_called_once_with(expired_key)

    def test_does_not_delete_live_message(self, tmp_path: Path) -> None:
        """Non-expired message is NOT deleted from Zenoh storage."""
        live_key = 'msg/mesh/inbox/session/s/bbb'
        live_msg = _make_msg(expires_at=_future(900))
        reply = _make_zenoh_reply(live_msg, key=live_key)

        session = MagicMock()
        session.get.return_value = [reply]
        index = self._make_index(tmp_path)

        count, scan_ok = purge_expired_msgs(session, index)

        assert scan_ok is True
        assert count == 0
        session.delete.assert_not_called()

    def test_mixed_expired_and_live(self, tmp_path: Path) -> None:
        """Only expired messages are deleted; live messages are untouched."""
        expired_key = 'msg/mesh/inbox/session/s/exp'
        live_key = 'msg/mesh/inbox/session/s/live'

        expired_msg = _make_msg(expires_at=_past(5))
        live_msg = _make_msg(expires_at=_future(300))

        session = MagicMock()
        session.get.return_value = [
            _make_zenoh_reply(expired_msg, key=expired_key),
            _make_zenoh_reply(live_msg, key=live_key),
        ]
        index = self._make_index(tmp_path)

        count, scan_ok = purge_expired_msgs(session, index)

        assert scan_ok is True
        assert count == 1
        session.delete.assert_called_once_with(expired_key)

    def test_purge_returns_zero_on_scan_failure(self, tmp_path: Path) -> None:
        """Transport failure during scan returns (0, False) — conservative, no deletes."""
        session = MagicMock()
        session.get.side_effect = RuntimeError('no zenoh')
        index = self._make_index(tmp_path)

        count, scan_ok = purge_expired_msgs(session, index)

        assert scan_ok is False
        assert count == 0
        session.delete.assert_not_called()

    def test_purge_cleans_local_sqlite_index(self, tmp_path: Path) -> None:
        """Local SQLite index entries for expired messages are removed."""
        expired_msg = _make_msg(expires_at=_past(1), session_id='idx-sess')
        expired_key = 'msg/mesh/inbox/session/idx-sess/ccc'

        index = self._make_index(tmp_path)
        index.register(expired_msg, 'idx-sess')

        session = MagicMock()
        session.get.return_value = [_make_zenoh_reply(expired_msg, key=expired_key)]

        purge_expired_msgs(session, index, now=_utc_now())

        # After purge, the SQLite row should be gone
        remaining = index.list_unacked('idx-sess')
        assert expired_msg.msg_id not in remaining

    def test_skips_malformed_payload(self, tmp_path: Path) -> None:
        """Malformed JSON payloads are skipped without crashing."""
        session = MagicMock()
        bad_reply = MagicMock()
        bad_reply.ok = MagicMock()
        bad_reply.ok.key_expr = 'msg/mesh/inbox/session/s/bad'
        bad_reply.ok.payload.to_bytes.return_value = b'not-json-{{'
        session.get.return_value = [bad_reply]
        index = self._make_index(tmp_path)

        count, scan_ok = purge_expired_msgs(session, index)

        assert scan_ok is True
        assert count == 0
        session.delete.assert_not_called()

    def test_delete_failure_does_not_crash(self, tmp_path: Path) -> None:
        """A delete failure for one key does not abort the purge."""
        expired_key = 'msg/mesh/inbox/session/s/ddd'
        expired_msg = _make_msg(expires_at=_past(1))
        reply = _make_zenoh_reply(expired_msg, key=expired_key)

        session = MagicMock()
        session.get.return_value = [reply]
        session.delete.side_effect = RuntimeError('delete failed')
        index = self._make_index(tmp_path)

        # Must not raise
        count, scan_ok = purge_expired_msgs(session, index)
        assert scan_ok is True
        assert count == 0  # delete failed, so count stays 0


# ---------------------------------------------------------------------------
# LocalMessageIndex.purge_expired — orphan acks (design doc §4.2 pitfall)
# ---------------------------------------------------------------------------


class TestPurgeExpiredOrphanAcks:
    """Regression tests for the purge-asymmetry bug.

    purge_expired() must not leave behind acks rows for messages it deletes,
    or a re-put of the same msg_id is silently invisible to the receiver
    (see docs/design/0201-messaging-ack-timeout-policy.md §4.2).
    """

    def _make_index(self, tmp_path: Path) -> LocalMessageIndex:
        return LocalMessageIndex(tmp_path / 'messaging' / 'inbox.db')

    def test_reput_after_purge_is_visible_again(self, tmp_path: Path) -> None:
        """Core regression: re-registering a purged msg_id must not be treated as acked."""
        index = self._make_index(tmp_path)
        msg = _make_msg(expires_at=_past(1), session_id='sess-1')

        index.register(msg, 'sess-1')
        index.record_ack(Ack(msg_id=msg.msg_id, recipient_session_id='sess-1'))
        assert index.is_acked(msg.msg_id, 'sess-1') is True

        removed = index.purge_expired()
        assert removed == 1

        # Same msg_id re-put (sender resends before the "reuse msg_id" ban lands
        # everywhere, or a pre-existing orphan from before this fix) must be
        # visible again — not silently swallowed by a stale acks row.
        inserted = index.register(msg, 'sess-1')
        assert inserted is True
        assert index.is_acked(msg.msg_id, 'sess-1') is False

    def test_purge_zero_messages_is_noop(self, tmp_path: Path) -> None:
        index = self._make_index(tmp_path)
        assert index.purge_expired() == 0

    def test_purge_messages_only_no_acks(self, tmp_path: Path) -> None:
        """An expired message with no ack at all is purged without error."""
        index = self._make_index(tmp_path)
        msg = _make_msg(expires_at=_past(1), session_id='sess-2')
        index.register(msg, 'sess-2')

        assert index.purge_expired() == 1
        assert index.find_scope(msg.msg_id, 'sess-2') is None

    def test_purge_cleans_preexisting_orphan_ack_with_no_message_row(self, tmp_path: Path) -> None:
        """Migration case: an orphan ack with no corresponding message row.

        e.g. left behind by a purge_expired call under the old code, is
        cleaned up by a later purge_expired call, without a message ever
        having been registered.
        """
        index = self._make_index(tmp_path)
        with index._connect() as conn:  # noqa: SLF001 — test-only direct DB access
            conn.execute(
                'INSERT INTO acks (msg_id, recipient_session_id, acked_at) VALUES (?, ?, ?)',
                ('orphan-preexisting', 'sess-3', _iso(_utc_now())),
            )
            conn.commit()
        assert index.is_acked('orphan-preexisting', 'sess-3') is True

        index.purge_expired()

        assert index.is_acked('orphan-preexisting', 'sess-3') is False

    def test_purge_multiple_receivers_only_cleans_expired_receiver_acks(self, tmp_path: Path) -> None:
        """Same msg_id delivered to two receivers: purging one is isolated.

        Purging one receiver's expired copy must not disturb the other
        receiver's still-live copy or ack.
        """
        index = self._make_index(tmp_path)
        msg_id = 'shared-msg-id'
        expired_msg = _make_msg(expires_at=_past(1), session_id='recv-expired')
        expired_msg.msg_id = msg_id
        live_msg = _make_msg(expires_at=_future(900), session_id='recv-live')
        live_msg.msg_id = msg_id

        index.register(expired_msg, 'recv-expired')
        index.record_ack(Ack(msg_id=msg_id, recipient_session_id='recv-expired'))
        index.register(live_msg, 'recv-live')
        index.record_ack(Ack(msg_id=msg_id, recipient_session_id='recv-live'))

        removed = index.purge_expired()
        assert removed == 1

        # Expired receiver's ack is gone along with its message row.
        assert index.is_acked(msg_id, 'recv-expired') is False
        # Live receiver's message + ack are untouched.
        assert index.find_scope(msg_id, 'recv-live') == live_msg.scope
        assert index.is_acked(msg_id, 'recv-live') is True

    def test_purge_after_reput_of_different_msg_id_is_unaffected(self, tmp_path: Path) -> None:
        """Sanity: purge of one expired msg_id does not disturb an unrelated live msg_id."""
        index = self._make_index(tmp_path)
        expired = _make_msg(expires_at=_past(1), session_id='sess-4')
        other = _make_msg(expires_at=_future(900), session_id='sess-4')

        index.register(expired, 'sess-4')
        index.register(other, 'sess-4')

        assert index.purge_expired() == 1
        assert index.find_scope(other.msg_id, 'sess-4') == other.scope


# ---------------------------------------------------------------------------
# check_messages — inline lazy-delete integration
# ---------------------------------------------------------------------------


class TestCheckMessagesLazyDelete:
    def _call(self, **kwargs) -> dict:
        async def _go() -> dict:
            async with Client(mcp) as client:
                result = await client.call_tool('check_messages', kwargs)
                return json.loads(result.data)

        return _run(_go())

    def test_check_messages_deletes_expired_from_zenoh(self, tmp_path: Path) -> None:
        """check_messages deletes expired messages from Zenoh storage (not just filters them)."""
        _reset_index(tmp_path)
        expired_key = 'msg/mesh/inbox/session/lazy-sess/expired123'
        expired_msg = _make_msg(
            session_id='lazy-sess',
            expires_at=_past(1),
        )
        reply = _make_zenoh_reply(expired_msg, key=expired_key)

        mock_session = MagicMock()
        mock_session.get.return_value = [reply]

        with (
            patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('kioku_mesh.mcp_server.get_session_id', return_value='lazy-sess'),
            patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        # Expired message must NOT appear in results
        assert result['count'] == 0
        # AND must be deleted from Zenoh storage
        mock_session.delete.assert_called_once_with(expired_key)

    def test_check_messages_does_not_delete_live_messages(self, tmp_path: Path) -> None:
        """check_messages does not delete non-expired messages from Zenoh."""
        _reset_index(tmp_path)
        live_key = 'msg/mesh/inbox/session/live-sess/live456'
        live_msg = _make_msg(
            session_id='live-sess',
            expires_at=_future(900),
        )
        reply = _make_zenoh_reply(live_msg, key=live_key)

        mock_session = MagicMock()
        mock_session.get.return_value = [reply]

        with (
            patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('kioku_mesh.mcp_server.get_session_id', return_value='live-sess'),
            patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        assert result['count'] == 1
        mock_session.delete.assert_not_called()

    def test_check_messages_include_expired_is_readonly(self, tmp_path: Path) -> None:
        """With include_expired=True, expired messages are returned but NOT deleted (read-only)."""
        _reset_index(tmp_path)
        expired_key = 'msg/mesh/inbox/session/dbg-sess/expdbg'
        expired_msg = _make_msg(
            session_id='dbg-sess',
            expires_at=_past(1),
        )
        reply = _make_zenoh_reply(expired_msg, key=expired_key)

        mock_session = MagicMock()
        mock_session.get.return_value = [reply]

        with (
            patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('kioku_mesh.mcp_server.get_session_id', return_value='dbg-sess'),
            patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call(include_expired=True)

        # Expired message is returned for debugging
        assert result['count'] == 1
        # But NOT deleted — include_expired=True is read-only (C1 fix)
        mock_session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# purge_expired_messages MCP tool
# ---------------------------------------------------------------------------


class TestPurgeExpiredMessagesTool:
    def _call(self) -> str:
        async def _go() -> str:
            async with Client(mcp) as client:
                result = await client.call_tool('purge_expired_messages', {})
                return result.data

        return _run(_go())

    def test_tool_registered_in_mcp(self) -> None:
        async def _go() -> list[str]:
            async with Client(mcp) as client:
                tools = await client.list_tools()
                return [t.name for t in tools]

        names = _run(_go())
        assert 'purge_expired_messages' in names

    def test_purge_tool_returns_count(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        expired_key = 'msg/mesh/inbox/session/tool-sess/zzz'
        expired_msg = _make_msg(expires_at=_past(1))
        reply = _make_zenoh_reply(expired_msg, key=expired_key)

        mock_session = MagicMock()
        mock_session.get.return_value = [reply]

        with (
            patch('kioku_mesh.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        assert 'purged' in result
        assert '1' in result
        mock_session.delete.assert_called_once_with(expired_key)

    def test_purge_tool_zenoh_unavailable(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        with (
            patch('kioku_mesh.mcp_server._get_zenoh_session', side_effect=RuntimeError('no zenoh')),
            patch('kioku_mesh.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        assert 'purge failed' in result

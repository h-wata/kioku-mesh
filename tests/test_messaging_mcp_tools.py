"""Tests for check_messages / ack_message MCP tools (Phase 2).

Uses FastMCP's in-process Client pattern (same as test_mcp_server.py).
Zenoh is mocked — these are unit tests for tool logic, not transport.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402

from mesh_mem.mcp_server import mcp  # noqa: E402
import mesh_mem.mcp_server as mcp_module  # noqa: E402
from mesh_mem.messaging.models import Message  # noqa: E402


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _future(seconds: int = 900) -> datetime:
    return _utc_now() + timedelta(seconds=seconds)


def _make_msg(
    scope: str = 'mesh',
    session_id: str = 'test-sess-001',
    body: str = 'hello',
    sender_id: str = 'sender-x',
    expires_at: datetime | None = None,
) -> Message:
    return Message(
        sender_id=sender_id,
        scope=scope,
        payload={'text': body},
        body=body,
        recipient={'kind': 'session', 'session_id': session_id},
        ttl_sec=900,
        expires_at=expires_at or _future(900),
    )


def _make_reply(msg: Message) -> MagicMock:
    """Wrap a Message as a mock Zenoh reply."""
    reply = MagicMock()
    reply.ok = MagicMock()
    reply.ok.payload.to_bytes.return_value = msg.to_json().encode('utf-8')
    return reply


def _reset_index(tmp_path: Path) -> None:
    """Force mcp_server to create a fresh messaging index under tmp_path."""
    mcp_module._messaging_index = None


# ---------------------------------------------------------------------------
# Helpers to assert tool signatures (no forbidden args)
# ---------------------------------------------------------------------------


class TestToolSignatures:
    def test_check_messages_not_in_forbidden_args(self) -> None:
        """check_messages must not expose user_id/team_id/session_id/pc_id."""
        import inspect

        from mesh_mem.mcp_server import check_messages

        sig = inspect.signature(check_messages)
        forbidden = {'user_id', 'team_id', 'session_id', 'pc_id'}
        exposed = set(sig.parameters) & forbidden
        assert not exposed, f'Forbidden args exposed in check_messages: {exposed}'

    def test_ack_message_not_in_forbidden_args(self) -> None:
        import inspect

        from mesh_mem.mcp_server import ack_message

        sig = inspect.signature(ack_message)
        forbidden = {'user_id', 'team_id', 'session_id', 'pc_id', 'recipient_session_id'}
        exposed = set(sig.parameters) & forbidden
        assert not exposed, f'Forbidden args exposed in ack_message: {exposed}'


# ---------------------------------------------------------------------------
# check_messages
# ---------------------------------------------------------------------------


class TestCheckMessages:
    def _call(self, **kwargs) -> dict:
        async def _go() -> dict:
            async with Client(mcp) as client:
                result = await client.call_tool('check_messages', kwargs)
                return json.loads(result.data)

        return _run(_go())

    def test_returns_messages_from_zenoh(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        msg = _make_msg(session_id='fixed-sess')

        mock_session = MagicMock()
        mock_session.get.return_value = [_make_reply(msg)]

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('mesh_mem.mcp_server.get_session_id', return_value='fixed-sess'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        assert result['count'] >= 1
        ids = [m['msg_id'] for m in result['messages']]
        assert msg.msg_id in ids

    def test_scope_id_not_in_returned_messages(self, tmp_path: Path) -> None:
        """The returned message objects must not contain raw user_id/team_id."""
        _reset_index(tmp_path)
        msg = _make_msg()
        mock_session = MagicMock()
        mock_session.get.return_value = [_make_reply(msg)]

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('mesh_mem.mcp_server.get_session_id', return_value='test-sess'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        for item in result['messages']:
            assert 'user_id' not in item
            assert 'team_id' not in item
            assert 'pc_id' not in item

    def test_limit_is_respected(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        msgs = [_make_msg(session_id='s', body=f'msg{i}') for i in range(5)]
        mock_session = MagicMock()
        mock_session.get.return_value = [_make_reply(m) for m in msgs]

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('mesh_mem.mcp_server.get_session_id', return_value='s'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call(limit=2)

        assert len(result['messages']) <= 2
        assert result['truncated'] is True

    def test_expired_messages_excluded_by_default(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        expired_msg = _make_msg(
            session_id='s',
            expires_at=_utc_now() - timedelta(seconds=1),
        )
        mock_session = MagicMock()
        mock_session.get.return_value = [_make_reply(expired_msg)]

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('mesh_mem.mcp_server.get_session_id', return_value='s'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        assert result['count'] == 0

    def test_include_expired_flag(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        expired_msg = _make_msg(
            session_id='s',
            expires_at=_utc_now() - timedelta(seconds=1),
        )
        mock_session = MagicMock()
        mock_session.get.return_value = [_make_reply(expired_msg)]

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_session),
            patch('mesh_mem.mcp_server.get_session_id', return_value='s'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call(include_expired=True)

        assert result['count'] == 1

    def test_zenoh_unavailable_returns_error_json(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', side_effect=RuntimeError('no zenoh')),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call()

        assert 'error' in result

    def test_tool_registered_in_mcp(self) -> None:
        async def _go() -> list[str]:
            async with Client(mcp) as client:
                tools = await client.list_tools()
                return [t.name for t in tools]

        names = _run(_go())
        assert 'check_messages' in names
        assert 'ack_message' in names


# ---------------------------------------------------------------------------
# ack_message
# ---------------------------------------------------------------------------


class TestAckMessage:
    def _call(self, **kwargs) -> str:
        async def _go() -> str:
            async with Client(mcp) as client:
                result = await client.call_tool('ack_message', kwargs)
                return result.data

        return _run(_go())

    def test_invalid_msg_id_returns_error(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        with patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path):
            result = self._call(msg_id='short')
        assert 'msg_id' in result.lower() or '32' in result

    def test_ack_uses_server_side_session_id(self, tmp_path: Path) -> None:
        """ack_message must call record_ack with server-resolved session_id, not caller-supplied."""
        _reset_index(tmp_path)
        msg = _make_msg(session_id='server-sess-id')

        # Pre-register the message in the index using the server session id
        from mesh_mem.messaging.local_index import LocalMessageIndex

        db_path = tmp_path / 'messaging' / 'inbox.db'
        idx = LocalMessageIndex(db_path)
        idx.register(msg, 'server-sess-id')

        mock_zenoh = MagicMock()

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_zenoh),
            patch('mesh_mem.mcp_server.get_session_id', return_value='server-sess-id'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call(msg_id=msg.msg_id)

        assert 'acked' in result
        assert msg.msg_id in result

    def test_ack_publishes_to_zenoh(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        msg = _make_msg(session_id='sess-pub', scope='mesh')

        from mesh_mem.messaging.local_index import LocalMessageIndex

        db_path = tmp_path / 'messaging' / 'inbox.db'
        idx = LocalMessageIndex(db_path)
        idx.register(msg, 'sess-pub')

        mock_zenoh = MagicMock()

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_zenoh),
            patch('mesh_mem.mcp_server.get_session_id', return_value='sess-pub'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            self._call(msg_id=msg.msg_id)

        mock_zenoh.put.assert_called_once()
        key_arg = mock_zenoh.put.call_args[0][0]
        assert msg.msg_id in key_arg

    def test_ack_not_in_index_returns_error(self, tmp_path: Path) -> None:
        _reset_index(tmp_path)
        unknown_id = 'f' * 32

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=MagicMock()),
            patch('mesh_mem.mcp_server.get_session_id', return_value='sess-x'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            result = self._call(msg_id=unknown_id)

        assert 'ack failed' in result or 'unknown' in result.lower()

    def test_ack_recorded_in_local_index(self, tmp_path: Path) -> None:
        """After ack_message, the message is marked acked in the local index."""
        _reset_index(tmp_path)
        msg = _make_msg(session_id='sess-ack-check', scope='mesh')

        from mesh_mem.messaging.local_index import LocalMessageIndex

        db_path = tmp_path / 'messaging' / 'inbox.db'
        idx = LocalMessageIndex(db_path)
        idx.register(msg, 'sess-ack-check')

        mock_zenoh = MagicMock()

        with (
            patch('mesh_mem.mcp_server._get_zenoh_session', return_value=mock_zenoh),
            patch('mesh_mem.mcp_server.get_session_id', return_value='sess-ack-check'),
            patch('mesh_mem.mcp_server.state_dir', return_value=tmp_path),
        ):
            self._call(msg_id=msg.msg_id)

        # Use a freshly-opened index to verify persistence
        idx2 = LocalMessageIndex(db_path)
        assert idx2.is_acked(msg.msg_id, 'sess-ack-check')

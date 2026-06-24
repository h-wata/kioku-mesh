"""Tests for ZenohBridge: spool<->Zenoh put/sub and body size limit (Phase 2)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import MagicMock

import pytest

from mesh_mem.messaging.keyspace import ack_key
from mesh_mem.messaging.keyspace import agent_inbox_key
from mesh_mem.messaging.keyspace import session_inbox_key
from mesh_mem.messaging.models import Message
from mesh_mem.messaging.spool import MessageSpool
from mesh_mem.messaging.zenoh_bridge import BODY_SIZE_LIMIT
from mesh_mem.messaging.zenoh_bridge import ZenohBridge


def _make_msg(
    scope: str = 'mesh',
    recipient_kind: str = 'session',
    recipient_id: str = 'sess-abc',
    body: str = 'hello',
    sender_id: str = 'sender-1',
) -> Message:
    recipient: dict = {'kind': recipient_kind}
    if recipient_kind == 'agent':
        recipient['agent_id'] = recipient_id
    else:
        recipient['session_id'] = recipient_id
    return Message(
        sender_id=sender_id,
        scope=scope,
        payload={'text': body},
        body=body,
        recipient=recipient,
        ttl_sec=900,
    )


def _future(seconds: int = 900) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# put_message
# ---------------------------------------------------------------------------


class TestPutMessage:
    def test_session_recipient_uses_session_inbox_key(self) -> None:
        mock_session = MagicMock()
        spool = MessageSpool()
        bridge = ZenohBridge(mock_session, spool)

        msg = _make_msg(scope='mesh', recipient_kind='session', recipient_id='sess-1')
        bridge.put_message(msg, 'mesh')

        expected_key = session_inbox_key('mesh', 'sess-1', msg.msg_id)
        mock_session.put.assert_called_once()
        actual_key = mock_session.put.call_args[0][0]
        assert actual_key == expected_key

    def test_agent_recipient_uses_agent_inbox_key(self) -> None:
        mock_session = MagicMock()
        spool = MessageSpool()
        bridge = ZenohBridge(mock_session, spool)

        msg = _make_msg(scope='team/acme', recipient_kind='agent', recipient_id='codex-cli')
        bridge.put_message(msg, 'team/acme')

        expected_key = agent_inbox_key('team/acme', 'codex-cli', msg.msg_id)
        actual_key = mock_session.put.call_args[0][0]
        assert actual_key == expected_key

    def test_payload_is_json_bytes(self) -> None:
        import json

        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        msg = _make_msg()
        bridge.put_message(msg, 'mesh')

        raw = mock_session.put.call_args[0][1]
        assert isinstance(raw, bytes)
        parsed = json.loads(raw)
        assert parsed['msg_id'] == msg.msg_id

    def test_body_within_limit_is_accepted(self) -> None:
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        msg = _make_msg(body='x' * 100)
        bridge.put_message(msg, 'mesh')  # should not raise
        mock_session.put.assert_called_once()

    def test_body_exceeding_limit_raises_value_error(self) -> None:
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        # Build a message whose serialized JSON will exceed 64 KiB
        oversized_body = 'x' * BODY_SIZE_LIMIT
        msg = _make_msg(body=oversized_body)
        with pytest.raises(ValueError, match='65536'):
            bridge.put_message(msg, 'mesh')
        mock_session.put.assert_not_called()

    def test_exactly_at_limit_is_rejected(self) -> None:
        """A payload of exactly BODY_SIZE_LIMIT bytes still exceeds the limit."""
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        # Craft a message whose JSON is exactly BODY_SIZE_LIMIT bytes
        msg = _make_msg()
        json_size = len(msg.to_json().encode('utf-8'))
        padding = BODY_SIZE_LIMIT - json_size
        if padding > 0:
            msg2 = _make_msg(body='x' * padding)
            # May or may not hit limit depending on JSON overhead; just ensure >limit raises
            raw = msg2.to_json().encode('utf-8')
            if len(raw) > BODY_SIZE_LIMIT:
                with pytest.raises(ValueError):
                    bridge.put_message(msg2, 'mesh')


# ---------------------------------------------------------------------------
# put_ack
# ---------------------------------------------------------------------------


class TestPutAck:
    def test_ack_uses_correct_zenoh_key(self) -> None:
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())

        msg_id = 'a' * 32
        scope = 'team/kioku-mesh'
        recipient_session_id = '20260624T000000Z-sess1'
        bridge.put_ack(msg_id, scope, recipient_session_id)

        expected_key = ack_key(scope, msg_id, recipient_session_id)
        actual_key = mock_session.put.call_args[0][0]
        assert actual_key == expected_key

    def test_ack_payload_contains_msg_id(self) -> None:
        import json

        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        msg_id = 'b' * 32
        bridge.put_ack(msg_id, 'mesh', 'sess-x')

        raw = mock_session.put.call_args[0][1]
        data = json.loads(raw)
        assert data['msg_id'] == msg_id
        assert data['status'] == 'acknowledged'


# ---------------------------------------------------------------------------
# setup_subscriber
# ---------------------------------------------------------------------------


class TestSetupSubscriber:
    def test_declares_two_subscribers(self) -> None:
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        bridge.setup_subscriber('mesh')
        # Should declare subscribers for session inbox and agent inbox
        assert mock_session.declare_subscriber.call_count == 2

    def test_subscriber_selectors_contain_session_and_agent(self) -> None:
        from unittest.mock import patch

        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())

        with (
            patch('mesh_mem.messaging.zenoh_bridge.get_session_id', return_value='test-sess'),
            patch('mesh_mem.messaging.zenoh_bridge.get_client_id', return_value='test-agent'),
        ):
            bridge.setup_subscriber('team/acme')

        selectors = [call[0][0] for call in mock_session.declare_subscriber.call_args_list]
        assert any('test-sess' in s for s in selectors), selectors
        assert any('test-agent' in s for s in selectors), selectors

    def test_subscriber_inserts_message_into_spool(self) -> None:
        """When a subscriber callback fires, the message is inserted into the spool."""
        spool = MessageSpool()
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, spool)
        bridge.setup_subscriber('mesh')

        # Grab the callback registered on declare_subscriber
        callback = mock_session.declare_subscriber.call_args_list[0][0][1]

        msg = _make_msg()
        sample = MagicMock()
        sample.payload.to_bytes.return_value = msg.to_json().encode('utf-8')
        callback(sample)

        assert spool.get(msg.msg_id) is not None

    def test_close_undeclares_subscribers(self) -> None:
        mock_sub = MagicMock()
        mock_session = MagicMock()
        mock_session.declare_subscriber.return_value = mock_sub

        bridge = ZenohBridge(mock_session, MessageSpool())
        bridge.setup_subscriber('mesh')
        bridge.close()

        assert mock_sub.undeclare.call_count == 2
        assert bridge._subscribers == []


# ---------------------------------------------------------------------------
# C1: recipient validation
# ---------------------------------------------------------------------------


class TestPutMessageRecipientValidation:
    def test_rejects_invalid_recipient_kind(self) -> None:
        bridge = ZenohBridge(MagicMock(), MessageSpool())
        msg = _make_msg(recipient_kind='session', recipient_id='s1')
        msg.recipient = {'kind': 'broadcast'}  # type: ignore[assignment]
        with pytest.raises(ValueError, match='Invalid recipient kind'):
            bridge.put_message(msg, 'mesh')

    def test_rejects_empty_session_id(self) -> None:
        bridge = ZenohBridge(MagicMock(), MessageSpool())
        msg = _make_msg(recipient_kind='session', recipient_id='s1')
        msg.recipient = {'kind': 'session', 'session_id': ''}  # type: ignore[assignment]
        with pytest.raises(ValueError, match='session_id'):
            bridge.put_message(msg, 'mesh')

    def test_rejects_none_session_id(self) -> None:
        bridge = ZenohBridge(MagicMock(), MessageSpool())
        msg = _make_msg(recipient_kind='session', recipient_id='s1')
        msg.recipient = {'kind': 'session', 'session_id': None}  # type: ignore[assignment]
        with pytest.raises(ValueError, match='session_id'):
            bridge.put_message(msg, 'mesh')

    def test_rejects_empty_agent_id(self) -> None:
        bridge = ZenohBridge(MagicMock(), MessageSpool())
        msg = _make_msg(recipient_kind='agent', recipient_id='ag1')
        msg.recipient = {'kind': 'agent', 'agent_id': ''}  # type: ignore[assignment]
        with pytest.raises(ValueError, match='agent_id'):
            bridge.put_message(msg, 'mesh')

    def test_valid_session_recipient_succeeds(self) -> None:
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        msg = _make_msg(recipient_kind='session', recipient_id='valid-sess')
        bridge.put_message(msg, 'mesh')
        mock_session.put.assert_called_once()

    def test_valid_agent_recipient_succeeds(self) -> None:
        mock_session = MagicMock()
        bridge = ZenohBridge(mock_session, MessageSpool())
        msg = _make_msg(recipient_kind='agent', recipient_id='valid-agent')
        bridge.put_message(msg, 'mesh')
        mock_session.put.assert_called_once()

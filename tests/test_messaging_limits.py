"""Boundary tests for messaging body size limits (Issue #202 — ADR-0022).

Covers, for both the MCP path (64 KiB) and the tmux path (8 KiB):
  - empty body
  - exactly at the limit (accepted)
  - limit + 1 byte (rejected)
  - multibyte characters straddling the limit (bytes, not characters, are counted)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from kioku_mesh.core.config import MessagingTmuxAdapterConfig
from kioku_mesh.messaging.limits import body_byte_len
from kioku_mesh.messaging.limits import check_body_size
from kioku_mesh.messaging.limits import check_envelope_size
from kioku_mesh.messaging.limits import MAX_BODY_BYTES
from kioku_mesh.messaging.limits import MAX_ENVELOPE_BYTES
from kioku_mesh.messaging.limits import MAX_TMUX_BODY_BYTES
from kioku_mesh.messaging.limits import MessageBodyTooLarge
from kioku_mesh.messaging.models import Message
from kioku_mesh.messaging.spool import MessageSpool
from kioku_mesh.messaging.spool import send_message
from kioku_mesh.messaging.tmux_adapter import try_inject
from kioku_mesh.messaging.zenoh_bridge import BODY_SIZE_LIMIT
from kioku_mesh.messaging.zenoh_bridge import ENVELOPE_SIZE_LIMIT
from kioku_mesh.messaging.zenoh_bridge import ZenohBridge

_PANE = '%6'
_JP = 'あ'  # 3 UTF-8 bytes


def _msg(body: object = 'hello', **overrides: object) -> Message:
    kwargs: dict = {
        'sender_id': 'codex-cli',
        'scope': 'user',
        'payload': {},
        'body': body,
        'recipient': {'kind': 'session', 'session_id': 'sess-1'},
    }
    kwargs.update(overrides)
    return Message(**kwargs)  # type: ignore[arg-type]


def _tmux_cfg(**overrides: object) -> MessagingTmuxAdapterConfig:
    base = MessagingTmuxAdapterConfig(
        enabled=True,
        pane_allowlist=[_PANE],
        sender_allowlist=['codex-cli'],
        scope_allowlist=['user'],
        max_body_bytes=MAX_TMUX_BODY_BYTES,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestConstantsStayInSync:
    """Constants duplicated across layers (ADR-0023 forbids core → messaging)."""

    def test_tmux_config_default_matches_messaging_constant(self) -> None:
        assert MessagingTmuxAdapterConfig().max_body_bytes == MAX_TMUX_BODY_BYTES

    def test_bridge_aliases_match_limits_module(self) -> None:
        assert BODY_SIZE_LIMIT == MAX_BODY_BYTES
        assert ENVELOPE_SIZE_LIMIT == MAX_ENVELOPE_BYTES


class TestBodyByteLen:
    """Sizes are UTF-8 bytes, not characters."""

    def test_empty_body_is_zero(self) -> None:
        assert body_byte_len('') == 0

    def test_multibyte_counts_bytes_not_chars(self) -> None:
        assert body_byte_len(_JP * 10) == 30

    def test_dict_body_measured_as_json(self) -> None:
        body = {'text': _JP * 10}
        assert body_byte_len(body) == len(json.dumps(body, ensure_ascii=False).encode('utf-8'))


class TestCheckBodySizeBoundaries:
    """64 KiB MCP limit: at-limit accepted, +1 rejected, empty accepted."""

    def test_empty_body_accepted(self) -> None:
        assert check_body_size('', channel='mcp') == 0

    def test_exactly_at_limit_accepted(self) -> None:
        assert check_body_size('x' * MAX_BODY_BYTES, channel='mcp') == MAX_BODY_BYTES

    def test_one_byte_over_limit_rejected(self) -> None:
        with pytest.raises(MessageBodyTooLarge) as exc:
            check_body_size('x' * (MAX_BODY_BYTES + 1), channel='mcp')
        assert exc.value.size == MAX_BODY_BYTES + 1
        assert exc.value.limit == MAX_BODY_BYTES

    def test_multibyte_exactly_at_limit_accepted(self) -> None:
        """21845 JP chars (65535 B) + 1 ASCII byte == exactly 65536 bytes."""
        body = _JP * (MAX_BODY_BYTES // 3) + 'x'
        assert len(body.encode('utf-8')) == MAX_BODY_BYTES
        assert check_body_size(body, channel='mcp') == MAX_BODY_BYTES

    def test_multibyte_straddling_limit_rejected(self) -> None:
        """A char whose bytes straddle the limit rejects the whole body — no partial cut.

        The body is 21846 characters, which is well under the limit when counted
        as characters, and 65538 bytes, which is over it. Byte counting must win.
        """
        body = _JP * (MAX_BODY_BYTES // 3 + 1)
        assert len(body) < MAX_BODY_BYTES  # character count is under the limit
        assert len(body.encode('utf-8')) == MAX_BODY_BYTES + 2
        with pytest.raises(MessageBodyTooLarge):
            check_body_size(body, channel='mcp')

    def test_error_message_is_actionable(self) -> None:
        with pytest.raises(MessageBodyTooLarge) as exc:
            check_body_size('x' * (MAX_BODY_BYTES + 1), channel='mcp', msg_id='abc123')
        text = str(exc.value)
        assert str(MAX_BODY_BYTES) in text  # the limit
        assert str(MAX_BODY_BYTES + 1) in text  # the actual size
        assert 'abc123' in text  # which message
        assert 'save_observation' in text  # what to do instead

    def test_error_is_a_value_error(self) -> None:
        """Existing `except ValueError` call sites keep working."""
        with pytest.raises(ValueError):
            check_body_size('x' * (MAX_BODY_BYTES + 1))


class TestCheckEnvelopeSize:
    def test_at_limit_accepted(self) -> None:
        assert check_envelope_size(b'x' * MAX_ENVELOPE_BYTES) == MAX_ENVELOPE_BYTES

    def test_over_limit_rejected(self) -> None:
        with pytest.raises(MessageBodyTooLarge) as exc:
            check_envelope_size(b'x' * (MAX_ENVELOPE_BYTES + 1))
        assert exc.value.channel == 'envelope'


class TestZenohBridgeBoundaries:
    """put_message enforces the body cap and the envelope cap."""

    def test_empty_body_is_put(self) -> None:
        session = MagicMock()
        ZenohBridge(session, MessageSpool()).put_message(_msg(body=''), 'mesh')
        session.put.assert_called_once()

    def test_body_exactly_at_limit_is_put(self) -> None:
        """A 64 KiB body is accepted even though the envelope adds ~434 bytes."""
        session = MagicMock()
        ZenohBridge(session, MessageSpool()).put_message(_msg(body='x' * MAX_BODY_BYTES), 'mesh')
        session.put.assert_called_once()

    def test_body_one_over_limit_is_rejected(self) -> None:
        session = MagicMock()
        with pytest.raises(MessageBodyTooLarge):
            ZenohBridge(session, MessageSpool()).put_message(_msg(body='x' * (MAX_BODY_BYTES + 1)), 'mesh')
        session.put.assert_not_called()

    def test_multibyte_straddling_limit_is_rejected(self) -> None:
        session = MagicMock()
        body = _JP * (MAX_BODY_BYTES // 3 + 1)
        with pytest.raises(MessageBodyTooLarge):
            ZenohBridge(session, MessageSpool()).put_message(_msg(body=body), 'mesh')
        session.put.assert_not_called()

    def test_oversized_payload_field_is_rejected_by_envelope_cap(self) -> None:
        """A small body cannot smuggle a huge payload past the body check."""
        session = MagicMock()
        msg = _msg(body='ok', payload={'blob': 'x' * MAX_ENVELOPE_BYTES})
        with pytest.raises(MessageBodyTooLarge) as exc:
            ZenohBridge(session, MessageSpool()).put_message(msg, 'mesh')
        assert exc.value.channel == 'envelope'
        session.put.assert_not_called()


class TestSendMessageBoundaries:
    """spool.send_message fails fast on an over-limit body."""

    def test_at_limit_accepted(self) -> None:
        spool = MessageSpool()
        msg = _msg(body='x' * MAX_BODY_BYTES)
        assert send_message(spool, msg) == msg.msg_id

    def test_over_limit_rejected_and_not_spooled(self) -> None:
        spool = MessageSpool()
        msg = _msg(body='x' * (MAX_BODY_BYTES + 1))
        with pytest.raises(MessageBodyTooLarge):
            send_message(spool, msg)
        assert spool.get(msg.msg_id) is None


class TestTmuxAdapterBoundaries:
    """8 KiB tmux limit: drop (never truncate) over-limit bodies."""

    def test_empty_body_injects(self) -> None:
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as run,
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep'),
        ):
            assert try_inject(_msg(body=''), _PANE, _tmux_cfg()) is True
        assert run.call_count == 2

    def test_exactly_at_limit_injects(self) -> None:
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as run,
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep'),
        ):
            assert try_inject(_msg(body='x' * MAX_TMUX_BODY_BYTES), _PANE, _tmux_cfg()) is True
        assert run.call_count == 2

    def test_one_byte_over_limit_is_dropped(self) -> None:
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as run:
            assert try_inject(_msg(body='x' * (MAX_TMUX_BODY_BYTES + 1)), _PANE, _tmux_cfg()) is False
        run.assert_not_called()

    def test_multibyte_straddling_limit_is_dropped_not_truncated(self) -> None:
        body = _JP * (MAX_TMUX_BODY_BYTES // 3 + 1)  # 8193 bytes, 2731 chars
        assert len(body) < MAX_TMUX_BODY_BYTES
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as run:
            assert try_inject(_msg(body=body), _PANE, _tmux_cfg()) is False
        run.assert_not_called()

    def test_multibyte_exactly_at_limit_injects_whole_body(self) -> None:
        body = _JP * (MAX_TMUX_BODY_BYTES // 3) + 'xx'  # 8192 bytes exactly
        assert len(body.encode('utf-8')) == MAX_TMUX_BODY_BYTES
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as run,
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep'),
        ):
            assert try_inject(_msg(body=body), _PANE, _tmux_cfg()) is True
        assert run.call_args_list[0].args[0][-1] == body  # injected intact, not cut

    def test_drop_is_logged_with_actionable_message(self) -> None:
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run'),
            patch('kioku_mesh.messaging.tmux_adapter._LOG.warning') as warn,
        ):
            try_inject(_msg(body='x' * (MAX_TMUX_BODY_BYTES + 1)), _PANE, _tmux_cfg())
        warn.assert_called_once()
        text = warn.call_args.args[0] % warn.call_args.args[1:]
        assert str(MAX_TMUX_BODY_BYTES) in text  # the limit
        assert str(MAX_TMUX_BODY_BYTES + 1) in text  # the actual size
        assert 'delivery_adapters' in text  # what to do instead

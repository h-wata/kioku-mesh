"""Defensive tests for messaging.tmux_adapter (ADR-0022 Phase 3)."""

from __future__ import annotations

from unittest.mock import call
from unittest.mock import patch

from kioku_mesh.core.config import MessagingTmuxAdapterConfig
from kioku_mesh.messaging.models import Message
from kioku_mesh.messaging.tmux_adapter import try_inject
import kioku_mesh.messaging.tmux_adapter as _adapter_module

_PANE = 'ros-agents:0.1'
_SENDER = 'test-sender'
_SCOPE = 'user'
_BODY = 'hello from test'


def _cfg(**overrides: object) -> MessagingTmuxAdapterConfig:
    """Return an all-allowed config with optional field overrides."""
    base = MessagingTmuxAdapterConfig(
        enabled=True,
        pane_allowlist=[_PANE],
        sender_allowlist=[_SENDER],
        scope_allowlist=[_SCOPE],
        max_body_bytes=8192,
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _msg(**overrides: object) -> Message:
    """Return a valid Message with optional field overrides."""
    defaults: dict = {'sender_id': _SENDER, 'scope': _SCOPE, 'payload': {}, 'body': _BODY}
    defaults.update(overrides)
    return Message(**defaults)


class TestTmuxAdapterGuards:
    """Each guard condition independently prevents injection."""

    def test_default_off_no_inject(self) -> None:
        """config.enabled=False means no tmux send-keys call, ever."""
        cfg = _cfg(enabled=False)
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run:
            result = try_inject(_msg(), _PANE, cfg)
        assert result is False
        mock_run.assert_not_called()

    def test_pane_not_in_allowlist_no_inject(self) -> None:
        """pane_id not in pane_allowlist → silent drop, no injection."""
        cfg = _cfg(pane_allowlist=['other-session:0.0'])
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run:
            result = try_inject(_msg(), _PANE, cfg)
        assert result is False
        mock_run.assert_not_called()

    def test_sender_not_in_allowlist_no_inject(self) -> None:
        """sender_id not in sender_allowlist → silent drop, no injection."""
        cfg = _cfg(sender_allowlist=['other-agent'])
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run:
            result = try_inject(_msg(sender_id='unknown-sender'), _PANE, cfg)
        assert result is False
        mock_run.assert_not_called()

    def test_scope_mismatch_no_inject(self) -> None:
        """message.scope not in scope_allowlist → silent drop, no injection."""
        cfg = _cfg(scope_allowlist=['team/kioku-mesh'])
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run:
            result = try_inject(_msg(scope='mesh'), _PANE, cfg)
        assert result is False
        mock_run.assert_not_called()

    def test_payload_too_large_no_inject(self) -> None:
        """Body exceeding max_body_bytes → silent drop (no exception raised)."""
        large_body = 'x' * 8193
        cfg = _cfg(max_body_bytes=8192)
        with patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run:
            result = try_inject(_msg(body=large_body), _PANE, cfg)
        assert result is False
        mock_run.assert_not_called()

    def test_payload_8192_bytes_accepted(self) -> None:
        """Body at exactly 8192 bytes (the limit) must be injected, not dropped.

        Guards accept body_bytes <= max_body_bytes; only body_bytes > limit drops.
        """
        boundary_body = 'x' * 8192
        cfg = _cfg(max_body_bytes=8192)
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run,
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep'),
        ):
            result = try_inject(_msg(body=boundary_body), _PANE, cfg)
        assert result is True
        assert mock_run.call_count == 2


class TestTmuxAdapterInjection:
    """Verify correct injection behavior when all guards pass."""

    def test_valid_message_injects_body_and_enter(self) -> None:
        """All guards pass → subprocess.run called twice (body then Enter)."""
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run,
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep') as mock_sleep,
        ):
            result = try_inject(_msg(), _PANE, _cfg())

        assert result is True
        assert mock_run.call_count == 2
        body_call, enter_call = mock_run.call_args_list
        assert body_call == call(
            ['tmux', 'send-keys', '-t', _PANE, _BODY],
            check=True,
            capture_output=True,
            timeout=5,
        )
        assert enter_call == call(
            ['tmux', 'send-keys', '-t', _PANE, 'Enter'],
            check=True,
            capture_output=True,
            timeout=5,
        )
        # Confirm the sleep between the two send-keys calls occurred.
        mock_sleep.assert_called_with(0.3)

    def test_inject_failure_retries_then_drops(self) -> None:
        """subprocess.run always fails → one retry, then drop + logging.error."""
        with (
            patch(
                'kioku_mesh.messaging.tmux_adapter.subprocess.run',
                side_effect=Exception('tmux error'),
            ),
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep'),
            patch('kioku_mesh.messaging.tmux_adapter._LOG') as mock_log,
        ):
            result = try_inject(_msg(), _PANE, _cfg())

        assert result is False
        mock_log.error.assert_called_once()
        error_args = mock_log.error.call_args[0]
        assert 'drop message' in error_args[0]

    def test_inject_success_does_not_auto_ack(self) -> None:
        """Successful injection must NOT auto-call ack_message.

        Injection is a delivery mechanism only; semantic ack is the
        recipient agent's responsibility via MCP check_messages / ack_message.
        """
        with (
            patch('kioku_mesh.messaging.tmux_adapter.subprocess.run') as mock_run,
            patch('kioku_mesh.messaging.tmux_adapter.time.sleep'),
        ):
            result = try_inject(_msg(), _PANE, _cfg())

        assert result is True
        mock_run.assert_called()
        # tmux_adapter must not expose or call ack_message.
        assert not hasattr(
            _adapter_module, 'ack_message'
        ), 'tmux_adapter must not import or expose ack_message (注入 ≠ ack)'

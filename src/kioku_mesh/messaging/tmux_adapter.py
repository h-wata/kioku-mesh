"""Opt-in tmux send-keys delivery adapter for messaging (ADR-0022 Phase 3).

Default off — ``config.enabled`` must be ``True`` for any pane injection.
messaging パッケージは memory を直接 import しない (ADR-0023).
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kioku_mesh.core.config import MessagingTmuxAdapterConfig
    from kioku_mesh.messaging.models import Message

_LOG = logging.getLogger(__name__)


def _inject_to_pane(pane_id: str, body: str) -> bool:
    """Send body text then Enter into the specified tmux pane.

    Two separate send-keys calls are intentional — combining body and Enter
    in one command causes Enter to be dropped in some tmux versions.
    """
    try:
        subprocess.run(
            ['tmux', 'send-keys', '-t', pane_id, body],
            check=True,
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.3)
        subprocess.run(
            ['tmux', 'send-keys', '-t', pane_id, 'Enter'],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning('tmux inject failed pane=%s: %s', pane_id, exc)
        return False


def try_inject(message: Message, pane_id: str, config: MessagingTmuxAdapterConfig) -> bool:
    """Try to inject a message body into a tmux pane after guard checks.

    Guards (all must pass; any failure silently drops the message and returns False):
      1. ``config.enabled`` is ``True``
      2. ``pane_id`` is in ``config.pane_allowlist``
      3. ``message.sender_id`` is in ``config.sender_allowlist``
      4. ``message.scope`` is in ``config.scope_allowlist``
      5. body byte length <= ``config.max_body_bytes``

    On injection failure: retries once after 0.5 s, then drops with
    ``logging.error``.

    # NOTE: tmux 注入成功は semantic ack ではない。
    # ack は recipient agent が MCP poll (check_messages) 後に
    # 自分で ack_message を呼ぶ責務がある。
    """
    if not config.enabled:
        return False

    if pane_id not in config.pane_allowlist:
        _LOG.debug('tmux_adapter: pane %r not in allowlist, drop mid=%s', pane_id, message.msg_id)
        return False

    if message.sender_id not in config.sender_allowlist:
        _LOG.debug(
            'tmux_adapter: sender %r not in allowlist, drop mid=%s',
            message.sender_id,
            message.msg_id,
        )
        return False

    if message.scope not in config.scope_allowlist:
        _LOG.debug(
            'tmux_adapter: scope %r not in allowlist, drop mid=%s',
            message.scope,
            message.msg_id,
        )
        return False

    body = message.body if isinstance(message.body, str) else str(message.body)
    body_bytes = len(body.encode())
    if body_bytes > config.max_body_bytes:
        _LOG.warning(
            'tmux_adapter: drop message mid=%s body_bytes=%d > limit=%d',
            message.msg_id,
            body_bytes,
            config.max_body_bytes,
        )
        return False

    if _inject_to_pane(pane_id, body):
        return True

    time.sleep(0.5)

    if _inject_to_pane(pane_id, body):
        return True

    _LOG.error(
        'tmux_adapter: drop message mid=%s to pane=%s after retry',
        message.msg_id,
        pane_id,
    )
    return False

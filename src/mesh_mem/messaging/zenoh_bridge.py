"""Zenoh put/subscribe bridge for kioku-mesh messaging (Phase 2 — ADR-0022).

Connects the in-process :class:`MessageSpool` to the Zenoh transport layer:
  - ``put_message``: serialize :class:`Message` and publish to the inbox key
  - ``put_ack``:     publish an ack record to the Zenoh ack key
  - ``setup_subscriber``: declare Zenoh subscribers that feed incoming
    messages into the spool (push-delivery path, Phase 3 activation)

Body size limit: 64 KiB.  ``put_message`` raises ``ValueError`` if the
serialized payload exceeds ``BODY_SIZE_LIMIT``.

messaging モジュールは memory モジュールを直接 import しない (ADR-0023).
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from ..core.identity import get_client_id
from ..core.identity import get_session_id
from .keyspace import ack_key
from .keyspace import agent_inbox_key
from .keyspace import session_inbox_key
from .models import Message
from .spool import MessageSpool

if TYPE_CHECKING:
    import zenoh

log = logging.getLogger(__name__)

BODY_SIZE_LIMIT = 65536  # 64 KiB — hard cap per design memo


class ZenohBridge:
    """Bridge between in-process :class:`MessageSpool` and Zenoh transport.

    Parameters
    ----------
    session:
        An open :class:`zenoh.Session` (from :func:`~mesh_mem.core.transport.get_session`).
    spool:
        The in-process :class:`MessageSpool` to feed on incoming messages.
    """

    def __init__(self, session: zenoh.Session, spool: MessageSpool) -> None:
        self._session = session
        self._spool = spool
        self._subscribers: list[Any] = []  # zenoh.Subscriber instances

    def put_message(self, msg: Message, scope: str) -> None:
        """Serialize ``msg`` and publish it to the appropriate Zenoh inbox key.

        The recipient kind is read from ``msg.recipient['kind']``:
          - ``'agent'``   → :func:`~.keyspace.agent_inbox_key`
          - ``'session'`` (default) → :func:`~.keyspace.session_inbox_key`

        Raises:
        ------
        ValueError
            When the serialized payload exceeds ``BODY_SIZE_LIMIT`` bytes.
        """
        payload_bytes = msg.to_json().encode('utf-8')
        if len(payload_bytes) > BODY_SIZE_LIMIT:
            raise ValueError(
                f'message body exceeds {BODY_SIZE_LIMIT}-byte limit (msg_id={msg.msg_id!r}, size={len(payload_bytes)})'
            )

        recipient: dict[str, Any] = msg.recipient if isinstance(msg.recipient, dict) else {}
        kind = recipient.get('kind', 'session')
        if kind == 'agent':
            recipient_id = str(recipient.get('agent_id', '') or '')
            key = agent_inbox_key(scope, recipient_id, msg.msg_id)
        else:
            recipient_id = str(recipient.get('session_id', '') or recipient.get('id', '') or '')
            key = session_inbox_key(scope, recipient_id, msg.msg_id)

        self._session.put(key, payload_bytes)
        log.debug('put_message: key=%r size=%d', key, len(payload_bytes))

    def put_ack(self, msg_id: str, scope: str, recipient_session_id: str) -> None:
        """Publish an ack record to the Zenoh ack key.

        The payload is a minimal JSON blob; the primary record is maintained
        in the local :class:`~.local_index.LocalMessageIndex`.
        """
        key = ack_key(scope, msg_id, recipient_session_id)
        payload = json.dumps(
            {
                'msg_id': msg_id,
                'recipient_session_id': recipient_session_id,
                'status': 'acknowledged',
            }
        ).encode('utf-8')
        self._session.put(key, payload)
        log.debug('put_ack: key=%r', key)

    def setup_subscriber(self, scope: str) -> None:
        """Declare Zenoh subscribers that feed incoming inbox messages into the spool.

        Subscribes to:
          - ``msg/{scope}/inbox/session/{session_id}/**``
          - ``msg/{scope}/inbox/agent/{agent_id}/**``

        Messages received via these subscribers are inserted into the
        in-process spool for later retrieval by ``check_messages``.
        This is the push-delivery path; Phase 2 check_messages uses
        poll-based Zenoh get instead.

        # TODO(Phase 3): activate push delivery and wire into tmux adapter
        """
        session_id = get_session_id()
        agent_id = get_client_id()
        spool = self._spool

        def _on_sample(sample: Any) -> None:
            try:
                json_str = sample.payload.to_bytes().decode('utf-8')
                msg = Message.from_json(json_str)
                spool.put(msg)
                log.debug('subscriber received msg_id=%r', msg.msg_id)
            except Exception as e:  # noqa: BLE001
                log.warning('subscriber failed to parse incoming message: %s', e)

        selectors = [
            f'msg/{scope}/inbox/session/{session_id}/**',
            f'msg/{scope}/inbox/agent/{agent_id}/**',
        ]
        for selector in selectors:
            try:
                sub = self._session.declare_subscriber(selector, _on_sample)
                self._subscribers.append(sub)
                log.debug('setup_subscriber: declared %r', selector)
            except Exception as e:  # noqa: BLE001
                log.warning('setup_subscriber failed for %r: %s', selector, e)

    def close(self) -> None:
        """Undeclare all active subscribers."""
        for sub in self._subscribers:
            try:
                sub.undeclare()
            except Exception:  # noqa: BLE001
                pass
        self._subscribers.clear()

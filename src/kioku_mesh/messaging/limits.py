"""Message body size limits and validation (Issue #202 — ADR-0022).

The limits here are *client/UX* limits, not Zenoh transport limits: measurement
on 2026-08-11 (zenoh 1.9.0, loopback router, memory volume) showed put/get
succeeding byte-for-byte up to 64 MiB with sub-50 ms latency, so the transport
is not the binding constraint. What binds is the recipient: ``check_messages``
returns bodies inline to an LLM, and a single 64 KiB body already costs roughly
16k tokens of the recipient's context window.

Limits:
  - ``MAX_BODY_BYTES`` (64 KiB) — MCP poll path (``ZenohBridge.put_message``)
  - ``MAX_ENVELOPE_BYTES`` (192 KiB) — the serialized JSON message as a whole,
    so ``payload`` / ``_extras`` / metadata cannot bypass the body limit
  - ``MAX_TMUX_BODY_BYTES`` (8 KiB) — tmux ``send-keys`` injection path

Over-limit behaviour is **reject**, never truncate or split:
  - truncating changes the meaning of text that an agent will act on, and a
    byte-level cut can split a UTF-8 sequence
  - splitting needs reassembly plus ordering, and ADR-0022 MVP explicitly gives
    no ordering guarantee (``sender_seq`` is best-effort only)

The limits are enforced on **both** ends. Sender-side checks only bind senders
running this version, so data already in Zenoh storage, older peers and external
publishers would otherwise walk straight past the cap; the receive path
(``check_messages`` and the push subscriber) re-validates after deserialization.
See :func:`withheld_body_notice` for what the receive path does with an
over-limit message.

All sizes are **UTF-8 byte** counts, not character counts.

messaging モジュールは memory モジュールを直接 import しない (ADR-0023).
"""

from __future__ import annotations

from collections.abc import Sequence
import json

MAX_BODY_BYTES = 65536  # 64 KiB — MCP poll body hard cap
# 192 KiB — serialized Message JSON hard cap. Sized as 3x the body cap: body,
# plus the legacy ``payload`` mirror that callers predating ``body`` still fill,
# plus headroom for envelope fields / _extras. It exists so payload/metadata
# cannot smuggle content past the body cap, not as a second body budget.
MAX_ENVELOPE_BYTES = 196608
MAX_TMUX_BODY_BYTES = 8192  # 8 KiB — tmux injection hard cap
# Receive-side (``check_messages`` response) caps. The envelope cap above bounds
# what arrives; these bound what is *returned inline to the LLM*, which is a
# different budget: an in-limit 192 KiB envelope can still put ~190 KiB into a
# single metadata field, and withholding the body alone leaves that field as an
# alternate inline channel (Issue #202 review finding M1).
MAX_METADATA_FIELD_BYTES = 1024  # 1 KiB — identity-shaped fields (ids, scope, one adapter)
MAX_SUBJECT_BYTES = 4096  # 4 KiB — subject is a header, not a second body
MAX_DELIVERY_ADAPTERS = 16  # list length cap, so N small entries cannot add up
# 72 KiB per returned message: the 64 KiB body budget plus 8 KiB of headroom for
# notice text and the bounded metadata around it. Enforced by *measuring* the
# encoded item, so it holds regardless of which field the bulk hid in. A whole
# ``check_messages`` response is therefore bounded by ``limit`` × this value.
MAX_RESPONSE_ITEM_BYTES = MAX_BODY_BYTES + 8192
# NOTE: MessagingTmuxAdapterConfig.max_body_bytes in core/config.py must keep the
# same default (core must not import messaging — ADR-0023). test_messaging_limits.py
# asserts the two stay in sync.

_CHANNEL_HINTS = {
    'mcp': (
        'Shorten the body, or store the full content with save_observation and '
        'send a short pointer (observation_id) instead.'
    ),
    'envelope': (
        'Shorten the body and any payload/metadata fields, or store the full content '
        'with save_observation and send a short pointer (observation_id) instead.'
    ),
    'tmux': (
        'Shorten the body for tmux injection, or drop "tmux" from delivery_adapters '
        'and let the recipient read the full message via check_messages.'
    ),
}


class MessageBodyTooLarge(ValueError):
    """Raised when a message body / envelope exceeds its channel limit.

    Subclasses ``ValueError`` so existing ``except ValueError`` call sites keep
    working. The message is written for the *sender*: it states the actual size,
    the limit, and what to do next.
    """

    def __init__(self, *, size: int, limit: int, channel: str = 'mcp', msg_id: str = '') -> None:
        self.size = size
        self.limit = limit
        self.channel = channel
        self.msg_id = msg_id
        what = 'message envelope' if channel == 'envelope' else 'message body'
        hint = _CHANNEL_HINTS.get(channel, _CHANNEL_HINTS['mcp'])
        where = f' (msg_id={msg_id!r})' if msg_id else ''
        super().__init__(
            f'{what} is {size} bytes, over the {limit}-byte ({limit // 1024} KiB) '
            f'limit for the {channel} channel{where}. {hint}'
        )


def body_byte_len(body: object) -> int:
    """Return the UTF-8 byte length of ``body`` as it is counted for limits.

    ``str`` / ``bytes`` are measured directly; anything else is measured as its
    compact JSON form (``ensure_ascii=False``), which is how a structured body
    reaches the wire.
    """
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode('utf-8'))
    return len(json.dumps(body, ensure_ascii=False, default=str).encode('utf-8'))


def check_body_size(
    body: object,
    *,
    limit: int = MAX_BODY_BYTES,
    channel: str = 'mcp',
    msg_id: str = '',
) -> int:
    """Validate a body against ``limit`` and return its byte length.

    A body of exactly ``limit`` bytes is accepted; ``limit + 1`` is rejected.

    Raises:
    ------
    MessageBodyTooLarge
        When the body exceeds ``limit`` bytes.
    """
    size = body_byte_len(body)
    if size > limit:
        raise MessageBodyTooLarge(size=size, limit=limit, channel=channel, msg_id=msg_id)
    return size


def withheld_body_notice(reason: str, withheld_fields: Sequence[str] = ()) -> str:
    """Return the placeholder that replaces an over-limit body on the receive path.

    Receive-side over-limit handling is **withhold-and-say-so**, not drop and not
    truncate:
      - dropping the message outright would remove the recipient's only signal
        that anything arrived, i.e. content vanishes with no warning
      - truncating hands the agent a partial instruction that reads as complete
        (the same reason the sender path rejects rather than cuts)
    So the message keeps its identity fields and its body is replaced with this
    notice, which states the actual size, the limit, and what to do next.

    ``withheld_fields`` names the other response fields that were dropped for the
    same message, so the recipient is told *what* is missing rather than seeing a
    silently empty ``subject`` / ``delivery_adapters`` that reads as authoritative.
    """
    tail = ''
    if withheld_fields:
        tail = f' Also withheld from this response: {", ".join(withheld_fields)}.'
    return f'[kioku-mesh: message body withheld — {reason}]{tail}'


def withheld_field_notice(field: str, size: int, limit: int) -> str:
    """Return the placeholder that replaces an over-limit *metadata* field.

    Metadata fields are identity-shaped (ids, scope, adapter names). Truncating
    one yields a value that still reads as an id but no longer denotes anything,
    so they are replaced wholesale — same withhold-and-say-so rule as the body.
    """
    return f'[kioku-mesh: {field} withheld — {size} bytes, over the {limit}-byte limit]'


def bound_metadata_value(
    value: object,
    *,
    field: str,
    limit: int = MAX_METADATA_FIELD_BYTES,
) -> tuple[object, bool]:
    """Bound one returned metadata value.

    Returns ``(value, False)`` when it is within ``limit``, or
    ``(notice, True)`` when it was withheld for being over it.
    """
    if body_byte_len(value) <= limit:
        return value, False
    return withheld_field_notice(field, body_byte_len(value), limit), True


def check_envelope_size(
    payload: bytes,
    *,
    limit: int = MAX_ENVELOPE_BYTES,
    msg_id: str = '',
) -> int:
    """Validate a serialized message envelope against ``limit`` and return its size.

    Raises:
    ------
    MessageBodyTooLarge
        When the serialized envelope exceeds ``limit`` bytes.
    """
    size = len(payload)
    if size > limit:
        raise MessageBodyTooLarge(size=size, limit=limit, channel='envelope', msg_id=msg_id)
    return size

"""Message and Ack domain models for the messaging layer (Phase 1 — ADR-0022, ADR-0023).

Phase 1 provides the data model only; Zenoh transport is added in Phase 2.
messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from dataclasses import fields as _fields
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from typing import Any
import uuid


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Normalize to a consistent UTC ISO 8601 string (Z-suffix) for storage."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _parse_dt(value: str | datetime | None) -> datetime | None:
    """Parse an ISO string or passthrough a datetime; return None for falsy input."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # Python 3.10 fromisoformat does not handle trailing 'Z'
    s = value.rstrip('Z')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Message:
    """A scoped agent-to-agent message (direct delivery, Phase 1)."""

    sender_id: str
    scope: str  # "mesh" | "team/{team_id}" | "user/{user_id}"
    payload: dict[str, Any]
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: datetime | None = None
    ttl_sec: int | None = None
    sender_seq: int | None = None  # best-effort monotonic; no ordering guarantee

    def to_json(self) -> str:
        return json.dumps(
            {
                'msg_id': self.msg_id,
                'sender_id': self.sender_id,
                'scope': self.scope,
                'payload': self.payload,
                'created_at': _iso(self.created_at),
                'expires_at': _iso(self.expires_at) if self.expires_at is not None else None,
                'ttl_sec': self.ttl_sec,
                'sender_seq': self.sender_seq,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, s: str) -> Message:
        """Parse JSON, ignoring unknown fields for forward compatibility."""
        data: dict[str, Any] = json.loads(s)
        data['created_at'] = _parse_dt(data.get('created_at')) or _utc_now()
        data['expires_at'] = _parse_dt(data.get('expires_at'))
        known = {f.name for f in _fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Ack:
    """Acknowledgement of a received Message."""

    msg_id: str
    recipient_session_id: str
    acked_at: datetime = field(default_factory=_utc_now)


def is_expired(msg: Message) -> bool:
    """Return True if msg has passed its expiry deadline.

    Precedence: expires_at > ttl_sec + created_at > never-expires (False).
    """
    now = datetime.now(timezone.utc)
    if msg.expires_at is not None:
        exp = msg.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp
    if msg.ttl_sec is not None:
        created = msg.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now >= created + timedelta(seconds=msg.ttl_sec)
    return False

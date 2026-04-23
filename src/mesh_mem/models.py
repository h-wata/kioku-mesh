"""Domain models for mesh-mem.

Observation is immutable: updates MUST be emitted with a fresh
``observation_id``. Deletion is represented by a Tombstone under a mirrored
``mem/tomb/...`` key; the search layer then hides observations whose
tombstone key is present (existence-based, not timestamp LWW).

``from_json`` tolerates unknown fields so that older readers do not break
when a future writer emits extra keys.
"""

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import fields
from datetime import datetime
from datetime import timezone
import json
from typing import Any
import uuid

from .identity import get_agent_family
from .identity import get_client_id
from .identity import get_pc_id
from .identity import get_session_id


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in a compact 'Z'-suffixed ISO form."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _from_dict_compat(cls: type, data: dict[str, Any]) -> Any:
    """Build ``cls`` from ``data`` while dropping unknown fields."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Observation:
    content: str
    agent_family: str = field(default_factory=get_agent_family)
    client_id: str = field(default_factory=get_client_id)
    pc_id: str = field(default_factory=get_pc_id)
    session_id: str = field(default_factory=get_session_id)
    project: str = ''
    tags: list[str] = field(default_factory=list)
    observation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=_utc_now_iso)

    @property
    def key_expr(self) -> str:
        """Return the Zenoh key expression placing this observation into the mesh."""
        return f'mem/obs/{self.agent_family}/{self.client_id}/{self.pc_id}/{self.session_id}/{self.observation_id}'

    def tombstone_key_expr(self) -> str:
        """Return the mirrored tombstone key (``mem/tomb/...``) for this observation."""
        return self.key_expr.replace('mem/obs/', 'mem/tomb/', 1)

    def to_json(self) -> str:
        """Serialize to a compact UTF-8 JSON string."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> 'Observation':
        """Parse JSON tolerantly, dropping unknown fields for forward/back compat."""
        return _from_dict_compat(cls, json.loads(data))


@dataclass
class Tombstone:
    """Logical delete marker put at ``mem/tomb/...`` mirroring the observation key."""

    observation_id: str
    reason: str = ''
    deleted_at: str = field(default_factory=_utc_now_iso)

    def to_json(self) -> str:
        """Serialize to a compact UTF-8 JSON string."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> 'Tombstone':
        """Parse JSON tolerantly, dropping unknown fields for forward/back compat."""
        return _from_dict_compat(cls, json.loads(data))

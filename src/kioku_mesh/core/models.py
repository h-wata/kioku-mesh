"""Domain models for kioku-mesh.

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
import logging
from typing import Any
import uuid

from .identity import get_agent_family
from .identity import get_client_id
from .identity import get_pc_id
from .identity import get_session_id
from .keyspace import obs_key
from .keyspace import tomb_key
from .keyspace import VALID_VISIBILITIES

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in a compact 'Z'-suffixed ISO form."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _from_dict_compat(cls: type, data: dict[str, Any]) -> Any:
    """Build ``cls`` from ``data``, stashing unknown fields in ``_extras``."""
    known_field_names = {f.name for f in fields(cls)}
    known_data = {k: v for k, v in data.items() if k in known_field_names}
    extras = {k: v for k, v in data.items() if k not in known_field_names}
    inst = cls(**known_data)
    inst._extras = extras  # noqa: SLF001
    return inst


VALID_MEMORY_TYPES: frozenset[str] = frozenset({'note', 'decision', 'bug', 'pattern', 'config', 'summary'})
"""Closed enum for ``Observation.memory_type``.

The MCP server's instructions advertise this same set; both ends must agree
or LLMs that ignore the docstring will silently land entries that fall out
of the documented categories (which they did, before validation existed).

Forward-compat note: ``Observation.from_json`` deliberately bypasses this
validation so that older readers can still ingest a future writer that adds
a new memory_type. Only newly-constructed observations are checked.
"""


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
    memory_type: str = 'note'
    importance: int = 2
    subject: str = ''
    summary: str = ''
    source_files: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    # ADR-0019 Phase B: replication scope. '' = legacy layout (mem/obs/...).
    # Deliberately NOT defaulted from config: a legacy payload parsed via
    # from_json must keep deriving its original (legacy) keys, otherwise
    # delete/gc would target the wrong namespace. Write entry points resolve
    # the effective visibility via config.resolve_write_visibility and pass
    # it explicitly.
    visibility: str = ''
    scope_id: str = ''

    def __post_init__(self) -> None:
        if self.importance < 1:
            self.importance = 1
        elif self.importance > 5:
            self.importance = 5
        if self.memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(f'memory_type must be one of {sorted(VALID_MEMORY_TYPES)}; got {self.memory_type!r}')
        if self.visibility not in VALID_VISIBILITIES:
            raise ValueError(f'visibility must be one of {sorted(VALID_VISIBILITIES)}; got {self.visibility!r}')
        # Non-dataclass side channel for unknown fields from newer schemas.
        # Persistence boundary: _extras is only preserved through to_json / from_json.
        # Any clone path that bypasses this pair (e.g. dataclasses.replace()) will drop _extras.
        self._extras: dict[str, Any] = {}

    @property
    def key_expr(self) -> str:
        """Zenoh key expression placing this observation into the mesh."""
        return obs_key(
            self.visibility,
            self.scope_id,
            self.agent_family,
            self.client_id,
            self.pc_id,
            self.session_id,
            self.observation_id,
        )

    def tombstone_key_expr(self) -> str:
        """Return the mirrored tombstone key (``.../tomb/...``) for this observation."""
        return tomb_key(
            self.visibility,
            self.scope_id,
            self.agent_family,
            self.client_id,
            self.pc_id,
            self.session_id,
            self.observation_id,
        )

    def to_json(self) -> str:
        """Serialize to a compact UTF-8 JSON string, re-emitting preserved unknown fields."""
        data = {**getattr(self, '_extras', {}), **asdict(self)}
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> 'Observation':
        """Parse JSON tolerantly, dropping unknown fields for forward/back compat.

        A peer running a future schema may emit a ``memory_type`` value not
        in this version's :data:`VALID_MEMORY_TYPES`. To preserve forward-
        compat, we clamp such values to ``"note"`` and log at DEBUG so
        full scans of legacy data do not spam WARNING-level logs. The
        observation is still ingested, just relabelled. The original value
        is lost on the receiver, which is acceptable for v0.x.
        """
        parsed = json.loads(data)
        raw_type = parsed.get('memory_type')
        if raw_type is not None and raw_type not in VALID_MEMORY_TYPES:
            log.debug(
                'Observation.from_json: clamping unknown memory_type %r to "note" (peer may be on a newer schema)',
                raw_type,
            )
            parsed['memory_type'] = 'note'
        raw_visibility = parsed.get('visibility')
        if raw_visibility is not None and raw_visibility not in VALID_VISIBILITIES:
            # A future tier we do not know. Clamp to '' so parsing succeeds;
            # locally derived keys would be wrong for such an observation, but
            # read paths only need the payload and delete/gc stay conservative.
            log.debug(
                'Observation.from_json: clamping unknown visibility %r to legacy (peer may be on a newer schema)',
                raw_visibility,
            )
            parsed['visibility'] = ''
        return _from_dict_compat(cls, parsed)


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

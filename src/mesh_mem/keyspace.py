"""Zenoh key-namespace vocabulary for kioku-mesh (ADR-0019 Phase A).

Single home for the key shapes the read paths must understand. ADR-0019
introduces visibility-tiered namespaces next to the legacy one:

```text
legacy:  mem/{obs|tomb}/{agent}/{client}/{pc}/{session}/{obs_id}
mesh:    mem/mesh/{obs|tomb}/{agent}/{client}/{pc}/{session}/{obs_id}
user:    mem/user/{user_id}/{obs|tomb}/{agent}/{client}/{pc}/{session}/{obs_id}
team:    mem/team/{team_id}/{obs|tomb}/{agent}/{client}/{pc}/{session}/{obs_id}
```

Phase A (this module): **readers** — the replication subscriber, the index
rebuild scan, the fallback search, and the shadow re-verify — cover the
legacy namespace *and* all tiered namespaces, so observations written by a
newer (Phase B+) peer are already visible here. Zenoh's ``**`` matches zero
or more chunks, so a single ``mem/**/obs/**`` selector covers every shape
above without enumerating tiers (verified against zenoh-python 1.9).

Write paths still emit legacy keys only; visibility-aware key *building*
lands in Phase B and will live here as well.
"""

OBS_MARKER = 'obs'
TOMB_MARKER = 'tomb'

# Read selectors covering legacy + all visibility tiers (Phase A).
OBS_READ_KEY_EXPR = 'mem/**/obs/**'
TOMB_READ_KEY_EXPR = 'mem/**/tomb/**'

# Scoped tiers carry a non-empty scope id segment: mem/user/{user_id}/...
_SCOPED_TIERS = ('user', 'team')
# The mesh tier has no scope id: mem/mesh/obs/...
_UNSCOPED_TIERS = ('mesh',)

_IDENTITY_SEGMENTS = 4  # agent_family / client_id / pc_id / session_id


def obs_selector(agent_family: str = '', client_id: str = '', pc_id: str = '', session_id: str = '') -> str:
    """Identity-narrowed obs selector covering legacy + tiered namespaces."""
    return '/'.join(
        [
            'mem/**/obs',
            agent_family or '*',
            client_id or '*',
            pc_id or '*',
            session_id or '*',
            '**',
        ]
    )


def tomb_selector(agent_family: str = '', client_id: str = '', pc_id: str = '', session_id: str = '') -> str:
    """Identity-narrowed tombstone selector covering legacy + tiered namespaces."""
    return obs_selector(agent_family, client_id, pc_id, session_id).replace('mem/**/obs/', 'mem/**/tomb/', 1)


def find_by_id_selector(observation_id: str) -> str:
    """Leaf-id obs selector covering legacy + tiered namespaces."""
    return f'mem/**/obs/**/{observation_id}'


def _is_obs_id(segment: str) -> bool:
    return len(segment) == 32 and all(c in '0123456789abcdef' for c in segment)


def obs_id_from_key(key_expr: str) -> str | None:
    """Extract a 32-hex observation_id from a canonical kioku-mesh key.

    Conservative parser (Issue #64): accepts only the exact shapes listed
    in the module docstring — legacy, ``mesh``, and scoped ``user`` /
    ``team`` — with the ``obs`` / ``tomb`` marker in its canonical
    position, exactly four identity segments after the marker, and a
    32-lowercase-hex trailing segment. Anything else returns ``None`` so a
    stray DELETE on an unrelated key cannot drive ``physical_delete``
    against a real row whose id happens to collide with the trailing
    token.
    """
    parts = key_expr.split('/')
    if not parts or parts[0] != 'mem' or len(parts) < 3:
        return None

    # Locate the obs/tomb marker according to the namespace shape.
    if parts[1] in (OBS_MARKER, TOMB_MARKER):
        marker_idx = 1  # legacy
    elif parts[1] in _UNSCOPED_TIERS:
        marker_idx = 2  # mem/mesh/{obs|tomb}/...
    elif parts[1] in _SCOPED_TIERS:
        marker_idx = 3  # mem/{user|team}/{scope_id}/{obs|tomb}/...
        if len(parts) <= marker_idx or not parts[2]:
            return None
    else:
        return None

    if len(parts) <= marker_idx or parts[marker_idx] not in (OBS_MARKER, TOMB_MARKER):
        return None
    # marker + 4 identity segments + obs_id
    if len(parts) != marker_idx + 1 + _IDENTITY_SEGMENTS + 1:
        return None
    obs_id = parts[-1]
    return obs_id if _is_obs_id(obs_id) else None

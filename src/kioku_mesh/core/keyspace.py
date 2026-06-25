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

import re

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

# '' = legacy (un-tiered) — writes under mem/{obs,tomb}/... as before.
VALID_VISIBILITIES: frozenset[str] = frozenset({'', 'mesh', 'user', 'team'})

_SCOPE_SLUG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


def validate_scope_slug(visibility: str, scope_id: str) -> None:
    """Reject scope ids that would break the key structure.

    The scope id becomes one Zenoh key chunk, so it must be a plain slug:
    1-64 chars of ``[A-Za-z0-9._-]`` starting alphanumeric. Validated both
    at config resolution time (so a bad KIOKU_MESH_USER_ID fails the save
    with an actionable message instead of crashing in the key builder) and
    again in :func:`_namespace_prefix` as defense in depth.
    """
    if not scope_id:
        raise ValueError(f"visibility '{visibility}' requires a scope_id (user_id / team_id)")
    if not _SCOPE_SLUG_RE.match(scope_id):
        raise ValueError(
            f'scope_id for visibility {visibility!r} must match [A-Za-z0-9][A-Za-z0-9._-]*'
            f' (max 64 chars); got {scope_id!r}'
        )


def _namespace_prefix(visibility: str, scope_id: str) -> str:
    """Return the key prefix up to (not including) the obs/tomb marker.

    Phase B (ADR-0019): writers branch on ``visibility``. ``''`` keeps the
    legacy layout so un-configured installs behave exactly as before.
    Scoped tiers (``user`` / ``team``) require a non-empty ``scope_id``
    (the user_id / team_id resolved from config — never from LLM input).
    """
    if visibility == '':
        return 'mem'
    if visibility in _UNSCOPED_TIERS:
        return f'mem/{visibility}'
    if visibility in _SCOPED_TIERS:
        validate_scope_slug(visibility, scope_id)
        return f'mem/{visibility}/{scope_id}'
    raise ValueError(f'visibility must be one of {sorted(VALID_VISIBILITIES)}; got {visibility!r}')


def obs_key(
    visibility: str,
    scope_id: str,
    agent_family: str,
    client_id: str,
    pc_id: str,
    session_id: str,
    observation_id: str,
) -> str:
    """Build the canonical obs key for the given visibility tier."""
    prefix = _namespace_prefix(visibility, scope_id)
    return f'{prefix}/{OBS_MARKER}/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}'


def tomb_key(
    visibility: str,
    scope_id: str,
    agent_family: str,
    client_id: str,
    pc_id: str,
    session_id: str,
    observation_id: str,
) -> str:
    """Build the canonical tombstone key mirroring :func:`obs_key`."""
    prefix = _namespace_prefix(visibility, scope_id)
    return f'{prefix}/{TOMB_MARKER}/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}'


def mirror_to_tomb_key(obs_key_expr: str) -> str:
    """Convert a canonical obs key into its mirrored tomb key (any namespace).

    Marker-relative replacement, so it works for legacy and tiered shapes
    alike. Callers must pass canonical obs keys (validate with
    :func:`obs_id_from_key` first when the key came off the wire).
    """
    return obs_key_expr.replace(f'/{OBS_MARKER}/', f'/{TOMB_MARKER}/', 1)


def mirror_to_obs_key(tomb_key_expr: str) -> str:
    """Inverse of :func:`mirror_to_tomb_key` for canonical tomb keys."""
    return tomb_key_expr.replace(f'/{TOMB_MARKER}/', f'/{OBS_MARKER}/', 1)


def broadcast_obs_selector(observation_id: str) -> str:
    """Best-effort wildcard delete pattern for an obs id across all namespaces."""
    return f'mem/**/{OBS_MARKER}/**/{observation_id}'


def broadcast_tomb_selector(observation_id: str) -> str:
    """Best-effort wildcard delete pattern for a tomb id across all namespaces."""
    return f'mem/**/{TOMB_MARKER}/**/{observation_id}'


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

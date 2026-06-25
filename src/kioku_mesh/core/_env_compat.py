"""Environment variable compatibility helpers for MESH_MEM_* → KIOKU_MESH_* migration.

This module provides :func:`get_env` which reads ``KIOKU_MESH_*`` env vars and
transparently falls back to the legacy ``MESH_MEM_*`` prefix with a
:class:`DeprecationWarning`.

See ADR-0024 for the migration plan.  ``MESH_MEM_*`` support will be removed in
``v1.0.0``.
"""

import os
import warnings

_NEW_PREFIX = 'KIOKU_MESH_'
_OLD_PREFIX = 'MESH_MEM_'
_REMOVAL_VERSION = 'v1.0.0'

# D1: emit DeprecationWarning once per legacy key per process lifetime.
_warned_keys: set[str] = set()


def get_env(key: str, default: str | None = '') -> str | None:
    """Return the value of *key* (a ``KIOKU_MESH_*`` env var), with legacy fallback.

    Resolution order:
    1. ``KIOKU_MESH_<suffix>`` — the new canonical name.
    2. ``MESH_MEM_<suffix>``   — deprecated; emits :class:`DeprecationWarning` and
       will be removed in :data:`_REMOVAL_VERSION`.
    3. *default*               — returned when neither var is set.

    Args:
        key: The new-style env var name (must start with ``KIOKU_MESH_``).
        default: Fallback value when the variable is not set.  Pass ``None`` to
            distinguish "not set" from empty string.

    Returns:
        The resolved value, or *default*.
    """
    if not key.startswith(_NEW_PREFIX):
        raise ValueError(f'get_env: key must start with {_NEW_PREFIX!r}, got {key!r}')

    val = os.environ.get(key)
    if val is not None:
        return val

    suffix = key[len(_NEW_PREFIX) :]
    legacy_key = _OLD_PREFIX + suffix
    legacy_val = os.environ.get(legacy_key)
    if legacy_val is not None:
        if legacy_key not in _warned_keys:
            _warned_keys.add(legacy_key)
            warnings.warn(
                f'Environment variable {legacy_key!r} is deprecated. '
                f'Please rename it to {key!r}. '
                f'{legacy_key!r} support will be removed in {_REMOVAL_VERSION} (ADR-0024).',
                DeprecationWarning,
                stacklevel=3,
            )
        return legacy_val

    return default

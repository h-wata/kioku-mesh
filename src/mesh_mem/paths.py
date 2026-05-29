"""Resolve kioku-mesh on-disk directories with legacy mesh-mem fallback (#128).

The project was renamed mesh-mem -> kioku-mesh (#121) but on-disk paths were
left as ``mesh-mem`` to avoid forcing a data migration. This module switches the
preferred directory name to ``kioku-mesh`` while still reading an existing
``mesh-mem`` directory when that is the only one present, so upgrading users keep
working until they manually ``mv``. Nothing is moved automatically.
"""

from __future__ import annotations

from pathlib import Path
import sys

APP_DIR = 'kioku-mesh'
LEGACY_APP_DIR = 'mesh-mem'

_warned: set[str] = set()


def resolve_app_dir(base: Path) -> Path:
    """Return the kioku-mesh dir under ``base``, falling back to legacy mesh-mem.

    Resolution:
      * ``base/kioku-mesh`` exists -> use it.
      * only ``base/mesh-mem`` exists -> use it and warn once (manual ``mv`` nudge).
      * both exist -> prefer kioku-mesh and warn once (possible split state).
      * neither exists (fresh) -> return ``base/kioku-mesh``.

    Never moves or creates anything; callers create the dir as before.
    """
    new = base / APP_DIR
    legacy = base / LEGACY_APP_DIR
    new_exists = new.exists()
    legacy_exists = legacy.exists()
    if new_exists and legacy_exists:
        _warn_once(
            str(legacy),
            f'note: both {new} and legacy {legacy} exist; using {new}. '
            f'Remove {legacy} once you have confirmed nothing important remains in it.',
        )
        return new
    if not new_exists and legacy_exists:
        _warn_once(
            str(legacy),
            f'warning: using legacy path {legacy}. kioku-mesh now prefers {new}. Migrate with:  mv {legacy} {new}',
        )
        return legacy
    return new


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(message, file=sys.stderr)

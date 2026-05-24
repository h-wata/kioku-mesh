"""Read and write ~/.config/mesh-mem/config.yaml.

This config is separate from the zenohd JSON5 config and stores
kioku-mesh-specific runtime settings such as which backend to use.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _config_dir() -> Path:
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'mesh-mem'


def _config_path() -> Path:
    return _config_dir() / 'config.yaml'


def _read_yaml(path: Path) -> dict:
    if yaml is None:
        # Fallback: simple key: value line parser for the single-field case.
        out: dict = {}
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    k, _, v = line.partition(':')
                    out[k.strip()] = v.strip()
        except OSError:
            pass
        return out
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def get_backend_mode() -> str:
    """Return the configured backend mode.

    Priority (highest to lowest):
      1. ``MESH_MEM_BACKEND`` env var
      2. ``backend:`` field in ``~/.config/mesh-mem/config.yaml``
      3. Default: ``'zenoh'``
    """
    env = os.environ.get('MESH_MEM_BACKEND', '').strip()
    if env:
        return env
    cfg = _read_yaml(_config_path())
    return str(cfg.get('backend', 'zenoh')).strip() or 'zenoh'


def write_local_config() -> Path:
    """Write config.yaml with ``backend: local`` and return the path."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('backend: local\n', encoding='utf-8')
    return path

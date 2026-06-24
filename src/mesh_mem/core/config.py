"""Read and write ~/.config/kioku-mesh/config.yaml.

This config is separate from the zenohd JSON5 config and stores
kioku-mesh-specific runtime settings such as which backend to use.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import os
from pathlib import Path

from .paths import resolve_app_dir

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _config_dir() -> Path:
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return resolve_app_dir(Path(base))


def _config_path() -> Path:
    return _config_dir() / 'config.yaml'


PROJECT_CONFIG_NAME = '.kioku-mesh.yaml'


def find_project_config(start: Path | None = None) -> Path | None:
    """Walk upward from ``start`` (default: cwd) looking for ``.kioku-mesh.yaml``.

    Returns the first match, searching ``start`` itself, then each parent
    up to the filesystem root (``.editorconfig`` style). ``None`` when no
    project config exists anywhere on the path.
    """
    cur = (start or Path.cwd()).resolve()
    for directory in (cur, *cur.parents):
        candidate = directory / PROJECT_CONFIG_NAME
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _read_project_config() -> dict:
    path = find_project_config()
    if path is None:
        return {}
    return _read_yaml(path)


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


def get_user_id() -> str:
    """Return the configured user scope id for visibility-tiered writes.

    Priority (highest to lowest):
      1. ``MESH_MEM_USER_ID`` env var
      2. ``user_id:`` field in ``~/.config/kioku-mesh/config.yaml``
      3. ``''`` (unset)

    Deliberately NOT exposed as an MCP tool argument (ADR-0019, same
    philosophy as ADR-0004 identity): an LLM-supplied value could pollute
    the ``mem/user/{user_id}/**`` namespace. Note the value must match
    across all of one user's machines for their memories to converge.

    Also deliberately NOT read from the project-local ``.kioku-mesh.yaml``
    (unlike :func:`get_team_id` / :func:`get_default_visibility`): user_id
    identifies a person, and a file that may be committed to a repository
    must never be able to set it — otherwise anyone cloning a repo could
    have their writes land in (and pollute) someone else's user namespace.
    """
    env = os.environ.get('MESH_MEM_USER_ID', '').strip()
    if env:
        return env
    cfg = _read_yaml(_config_path())
    return str(cfg.get('user_id', '') or '').strip()


def get_team_id() -> str:
    """Return the configured team scope id.

    Priority (highest to lowest):
      1. ``MESH_MEM_TEAM_ID`` env var
      2. ``team_id:`` in the nearest project ``.kioku-mesh.yaml`` (cwd upward)
      3. ``team_id:`` in ``~/.config/kioku-mesh/config.yaml``
      4. ``''`` (unset)
    """
    env = os.environ.get('MESH_MEM_TEAM_ID', '').strip()
    if env:
        return env
    project = _read_project_config()
    val = str(project.get('team_id', '') or '').strip()
    if val:
        return val
    cfg = _read_yaml(_config_path())
    return str(cfg.get('team_id', '') or '').strip()


def get_default_visibility() -> str:
    """Return the default visibility for new writes.

    Priority (highest to lowest):
      1. ``MESH_MEM_DEFAULT_VISIBILITY`` env var
      2. ``default_visibility:`` in the nearest project ``.kioku-mesh.yaml``
         (searched from cwd upward — per-directory default, ADR-0019)
      3. ``default_visibility:`` in ``~/.config/kioku-mesh/config.yaml``
      4. ``''`` (legacy layout — behaves exactly like pre-0.6)
    """
    env = os.environ.get('MESH_MEM_DEFAULT_VISIBILITY', '').strip()
    if env:
        return env
    project = _read_project_config()
    val = str(project.get('default_visibility', '') or '').strip()
    if val:
        return val
    cfg = _read_yaml(_config_path())
    return str(cfg.get('default_visibility', '') or '').strip()


def resolve_write_visibility(explicit: str = '') -> tuple[str, str]:
    """Resolve ``(visibility, scope_id)`` for a new write.

    ``explicit`` (a tool/CLI argument) wins over the configured default.
    Scoped tiers resolve their scope id from config only:

      - ``user`` -> :func:`get_user_id` (must be configured)
      - ``team`` -> :func:`get_team_id` (must be configured)

    Raises ``ValueError`` with an actionable message when a scoped tier is
    requested without its id configured, or on an unknown visibility.
    """
    from .keyspace import VALID_VISIBILITIES
    from .keyspace import validate_scope_slug

    visibility = (explicit or get_default_visibility()).strip()
    if visibility not in VALID_VISIBILITIES:
        valid = sorted(v for v in VALID_VISIBILITIES if v)
        raise ValueError(f'visibility must be one of {valid} (or empty for legacy); got {visibility!r}')
    if visibility == 'user':
        user_id = get_user_id()
        if not user_id:
            raise ValueError(
                "visibility 'user' requires a user_id: set MESH_MEM_USER_ID or add "
                "'user_id: <slug>' to ~/.config/kioku-mesh/config.yaml (use the same value on all your machines)"
            )
        validate_scope_slug(visibility, user_id)
        return visibility, user_id
    if visibility == 'team':
        team_id = get_team_id()
        if not team_id:
            raise ValueError(
                "visibility 'team' requires a team_id: set MESH_MEM_TEAM_ID or add "
                "'team_id: <slug>' to .kioku-mesh.yaml (project) or ~/.config/kioku-mesh/config.yaml"
            )
        validate_scope_slug(visibility, team_id)
        return visibility, team_id
    return visibility, ''


def format_visibility(visibility: str, scope_id: str) -> str:
    """Human-readable effective visibility for save responses.

    ADR-0019 trust mitigation: every save response must surface the
    effective scope (the project ``.kioku-mesh.yaml`` can change where a
    write lands, so agent and human alike see it on each save).

      - ``('', '')``          -> ``'legacy'``
      - ``('mesh', '')``      -> ``'mesh'``
      - ``('user', 'hwata')`` -> ``'user/hwata'``
    """
    if not visibility:
        return 'legacy'
    if scope_id:
        return f'{visibility}/{scope_id}'
    return visibility


def get_backend_mode() -> str:
    """Return the configured backend mode.

    Priority (highest to lowest):
      1. ``MESH_MEM_BACKEND`` env var
      2. ``backend:`` field in ``~/.config/kioku-mesh/config.yaml``
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


@dataclass
class MessagingTmuxAdapterConfig:
    """Config for the opt-in tmux send-keys delivery adapter (ADR-0022 Phase 3).

    Default off — ``enabled`` must be explicitly set to ``True`` in config;
    no pane injection occurs otherwise.
    """

    enabled: bool = False
    pane_allowlist: list[str] = field(default_factory=list)
    sender_allowlist: list[str] = field(default_factory=list)
    scope_allowlist: list[str] = field(default_factory=lambda: ['user'])
    max_body_bytes: int = 8192


def get_messaging_tmux_adapter_config() -> MessagingTmuxAdapterConfig:
    """Read tmux adapter config from global config YAML; defaults to all-off."""
    cfg = _read_yaml(_config_path())
    messaging: dict = cfg.get('messaging', {}) or {}
    tmux: dict = messaging.get('tmux', {}) or {}
    return MessagingTmuxAdapterConfig(
        enabled=bool(tmux.get('enabled', False)),
        pane_allowlist=list(tmux.get('pane_allowlist', [])),
        sender_allowlist=list(tmux.get('sender_allowlist', [])),
        scope_allowlist=list(tmux.get('scope_allowlist', ['user'])),
        max_body_bytes=int(tmux.get('max_body_bytes', 8192)),
    )

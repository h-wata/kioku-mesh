"""Resolve identity values for kioku-mesh.

Order of precedence per identity:
    - env var (when defined)
    - persisted file on disk (pc_id only)
    - auto-generated on first access (cached for process lifetime)

``pc_id`` and ``session_id`` MUST be stable for the lifetime of the process.
Re-generating ``session_id`` per call would fragment the kioku-mesh key
space across Observation/Heartbeat emissions and break searchability.

Filesystem requirement:
    ``KIOKU_MESH_STATE_DIR`` must reside on a filesystem that supports POSIX
    hard links (ext4 / btrfs / xfs / tmpfs / NFSv3+). FAT / exFAT / certain
    older SMB mounts do NOT and will cause ``get_pc_id()`` to raise
    ``OSError`` on first run. kioku-mesh targets Linux dev hosts where the
    default location (``~/.local/share/kioku-mesh``) sits on such a
    filesystem out of the box; point the env var at a non-hardlink mount
    at your own risk.
"""

from datetime import datetime
from datetime import timezone
from enum import Enum
import getpass
import os
import pathlib
import socket
import sys
import uuid

from kioku_mesh.core._env_compat import get_env

_pc_id_cache: str | None = None
_session_id_cache: str | None = None


class IdentitySource(str, Enum):
    """Where an identity value came from. Used for `kioku-mesh status` display."""

    ENV = 'env'
    # Reserved for future launcher detection (Claude Code / Gemini CLI env
    # markers). Not produced in v0.3 — leaving the enum value lets `status`
    # output and tests stabilize their shape before detection is wired in.
    DETECTED = 'detected'
    DEFAULT = 'default'


# Characters that would corrupt the Zenoh key expression
# (mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}).
# Identity segments are user-controlled (env or default-derived from hostname)
# so sanitize before letting them into the key namespace.
_ZENOH_UNSAFE_CHARS = ('/', '*', '?', '$', '#', '\n', '\r', '\t')


def _sanitize_key_segment(value: str, fallback: str) -> str:
    """Make ``value`` safe to use as a single Zenoh key segment.

    Strips whitespace and replaces characters that would break key parsing or
    open wildcard interpretation. Returns ``fallback`` when sanitization
    leaves an empty string (e.g. a hostname that was just dots).
    """
    cleaned = value.strip()
    for ch in _ZENOH_UNSAFE_CHARS:
        cleaned = cleaned.replace(ch, '-')
    return cleaned or fallback


def _default_user_name() -> str:
    """Best-effort current user name across Linux / macOS / Windows / containers."""
    candidates = (
        lambda: os.environ.get('USER'),
        lambda: os.environ.get('LOGNAME'),
        lambda: os.environ.get('USERNAME'),  # Windows
        getpass.getuser,  # may raise KeyError in minimal containers
    )
    for getter in candidates:
        try:
            v = getter()
        except Exception:  # noqa: BLE001 — every fallback below this is safe
            continue
        if v:
            return v
    return 'user'


def _default_short_hostname() -> str:
    """First label of the FQDN, safe-default ``host`` if resolution fails."""
    try:
        h = socket.gethostname()
    except Exception:  # noqa: BLE001
        h = ''
    return h.split('.', 1)[0] or 'host'


def state_dir() -> pathlib.Path:
    r"""Return the writable state directory, creating it if absent.

    Resolution order:
        1. ``KIOKU_MESH_STATE_DIR`` env var when set to a **non-empty** value
           (all OSes). An empty string (``KIOKU_MESH_STATE_DIR=''``) is
           treated as "not set" and falls through to the per-OS default;
           this differs from v0.2.0, which interpreted an empty string as
           the current working directory. Set the variable to ``.`` when
           the cwd-relative behavior is required.
        2. Per-OS default:
           - Linux:   ``~/.local/share/kioku-mesh`` (fixed base; ``XDG_DATA_HOME``
             is intentionally NOT honored to preserve pre-v0.2.1 behavior
             and avoid a silent migration for users who set it). Falls back to
             the legacy ``~/.local/share/mesh-mem`` when only that exists (#128).
           - macOS:   ``~/Library/Application Support/kioku-mesh``
           - Windows: ``%LOCALAPPDATA%\kioku-mesh``

    On macOS / Windows the default is resolved through ``platformdirs``;
    those platforms had no pre-v0.2.1 hardcoded path to preserve.
    """
    from .paths import APP_DIR
    from .paths import resolve_app_dir

    override = get_env('KIOKU_MESH_STATE_DIR')
    if override:
        d = pathlib.Path(override)
    elif sys.platform == 'linux':
        # v0.2.0 compatibility: keep the fixed base even when
        # XDG_DATA_HOME is set, so upgrading users do not silently lose
        # access to their existing pc_id / SQLite index / session state.
        # resolve_app_dir prefers ~/.local/share/kioku-mesh and falls back to
        # the legacy ~/.local/share/mesh-mem when only that exists (#128).
        d = resolve_app_dir(pathlib.Path.home() / '.local/share')
    else:
        # macOS / Windows: delegate to platformdirs.
        # Imported lazily so tests that monkeypatch the env var do not
        # require platformdirs at collection time.
        import platformdirs

        d = pathlib.Path(platformdirs.user_data_dir(APP_DIR, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_pc_id() -> str:
    """Return the per-host stable UUID, generating+persisting it on first call.

    The create-if-absent path uses a **temp-file + ``os.link`` atomic publish**
    so two kioku-mesh processes racing on a fresh host cannot observe a
    half-written ``pc_id``:

        1. write the candidate UUID to a uniquely-named temp file
        2. atomically ``os.link(tmp, pc_id)`` — either wins (pc_id now holds
           our content in full) or raises FileExistsError (someone else
           already published a fully-written value)
        3. on loss, read the winner's value from ``pc_id``

    The earlier ``O_CREAT|O_EXCL`` variant left a window where the loser
    could read ``pc_id`` between create and write and cache an empty string.
    ``os.link`` closes that window because the target only appears with the
    source's complete content.
    """
    global _pc_id_cache
    if _pc_id_cache is not None:
        return _pc_id_cache
    dir_ = state_dir()
    p = dir_ / 'pc_id'

    existing = _read_pc_id_file(p)
    if existing is not None:
        _pc_id_cache = existing
        return existing

    pid = uuid.uuid4().hex
    tmp = dir_ / f'.pc_id.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}'
    tmp.write_text(pid + '\n')
    try:
        os.link(tmp, p)
        won = True
    except FileExistsError:
        won = False
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    if won:
        _pc_id_cache = pid
        return pid

    existing = _read_pc_id_file(p)
    if existing is None:
        # pc_id exists but is unreadable/empty — a previous process likely
        # crashed between create and write. Surface it rather than caching ''.
        raise RuntimeError(f'pc_id at {p} exists but has no content')
    _pc_id_cache = existing
    return existing


def _read_pc_id_file(p: pathlib.Path) -> str | None:
    """Return the stored pc_id if the file exists with non-empty content."""
    if not p.exists():
        return None
    value = p.read_text().strip()
    return value or None


def resolve_agent_family() -> tuple[str, IdentitySource]:
    """Resolve agent_family and where it came from.

    v0.3 keeps ``agent_family`` as ``'unknown'`` when env-unset (#82). It's
    the aggregation axis used by ``search --agent-family``, so the cost of
    misclassifying observations (e.g. labeling everything ``claude``
    because ``CLAUDECODE=1`` happens to leak into a non-Claude session) is
    higher than the cost of an uninformative default. Launcher detection
    is a follow-up that will produce :attr:`IdentitySource.DETECTED`.
    """
    v = get_env('KIOKU_MESH_AGENT_FAMILY', '').strip()
    if v:
        return v, IdentitySource.ENV
    return 'unknown', IdentitySource.DEFAULT


def resolve_client_id() -> tuple[str, IdentitySource]:
    """Resolve client_id and where it came from.

    Default is ``<user>@<host_short>`` — searchable by humans
    (``--client-id alice@mbp``) and complementary to ``pc_id`` which already
    plays the opaque-UUID role. Falls back to safe placeholders when user
    or hostname can't be resolved (e.g. in minimal containers).
    """
    v = get_env('KIOKU_MESH_CLIENT_ID', '').strip()
    if v:
        return v, IdentitySource.ENV
    user = _sanitize_key_segment(_default_user_name(), 'user')
    host = _sanitize_key_segment(_default_short_hostname(), 'host')
    return f'{user}@{host}', IdentitySource.DEFAULT


def get_agent_family() -> str:
    """Return the agent family (claude / gemini / codex / chatgpt). See :func:`resolve_agent_family`."""
    return resolve_agent_family()[0]


def get_client_id() -> str:
    """Return the client id (e.g. ``claude-code``, ``alice@mbp``). See :func:`resolve_client_id`."""
    return resolve_client_id()[0]


def get_session_id() -> str:
    """Return the session id, resolved once per process.

    Precedence:
        - ``KIOKU_MESH_SESSION_ID`` env var if set
        - auto-generated ``{YYYYMMDDTHHMMSSZ}-{short-uuid}``
    """
    global _session_id_cache
    if _session_id_cache is not None:
        return _session_id_cache
    sid = get_env('KIOKU_MESH_SESSION_ID')
    if not sid:
        ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        sid = f'{ts}-{uuid.uuid4().hex[:8]}'
    _session_id_cache = sid
    return sid


def reset_caches() -> None:
    """Clear cached pc_id / session_id. Test-only helper."""
    global _pc_id_cache, _session_id_cache
    _pc_id_cache = None
    _session_id_cache = None

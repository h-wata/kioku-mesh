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
import logging
import os
import pathlib
import socket
import sys
import uuid

log = logging.getLogger(__name__)

_pc_id_cache: str | None = None
_session_id_cache: str | None = None
# One-shot warning latch. Identity is resolved on every Observation
# construction, so the warning must not repeat per save.
_unknown_family_warned: bool = False
# Same latch, for the "several launchers claim this process" case.
_ambiguous_family_warned: bool = False

# Env markers a launcher sets on the processes it spawns, mapped to the family
# they identify. Only launcher-owned names belong here: a marker a human might
# export by hand would misclassify observations, which is worse than 'unknown'.
# ``CODEX_HOME`` is deliberately absent — it is a user-configurable location
# that people export from their shell profile, and Codex CLI does not pass it
# to the MCP subprocesses it spawns anyway, so listing it could only ever
# mislabel a non-Codex process.
_LAUNCHER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('claude', ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT')),
    ('codex', ('CODEX_SANDBOX',)),
    ('gemini', ('GEMINI_CLI', 'GEMINI_SANDBOX')),
)


class IdentitySource(str, Enum):
    """Where an identity value came from. Used for `kioku-mesh status` display."""

    ENV = 'env'
    # Launcher detection from well-known agent env markers (CLAUDECODE=1 etc.).
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


def state_dir(*, create: bool = True) -> pathlib.Path:
    r"""Return the writable state directory, creating it if absent.

    Pass ``create=False`` to resolve the path without touching the
    filesystem. Read-only callers need that: ``kioku-mesh doctor`` reports on
    a host's state, and a diagnostic that materializes the directory it is
    diagnosing turns "nothing is set up here" into "something is set up here"
    just by looking.

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

    override = os.environ.get('KIOKU_MESH_STATE_DIR', '')
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
    if create:
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


def detect_agent_family() -> str:
    """Return the agent family implied by launcher env markers, or ``''``.

    Only markers a launcher sets for its own child processes are consulted,
    so a family is never inferred from something a user might export by hand.
    Claude Code passes ``CLAUDECODE`` / ``CLAUDE_CODE_ENTRYPOINT`` to the MCP
    subprocesses it spawns; Codex CLI passes no marker at all (verified on the
    running MCP servers), so a Codex MCP entry has to set
    ``KIOKU_MESH_AGENT_FAMILY`` explicitly to be attributable.

    Markers of **more than one** family can coexist: agents nest (Claude Code
    launching Codex, or the reverse) and the child inherits the parent's
    marker alongside its own. There is no ordering that identifies "the
    current agent" in that case, so detection declines rather than picking
    the first table entry — a confidently wrong family is trusted silently,
    while ``'unknown'`` is visible in ``kioku-mesh status`` and searchable as
    a defect. The ambiguity is logged once so the operator can set
    ``KIOKU_MESH_AGENT_FAMILY`` explicitly.
    """
    detected = [family for family, markers in _LAUNCHER_MARKERS if _any_marker_set(markers)]
    if len(detected) > 1:
        _warn_ambiguous_family(detected)
        return ''
    return detected[0] if detected else ''


def _any_marker_set(markers: tuple[str, ...]) -> bool:
    """Report whether at least one of ``markers`` is present with a non-blank value."""
    return any(os.environ.get(marker, '').strip() for marker in markers)


def _warn_ambiguous_family(detected: list[str]) -> None:
    """Warn once that nested launcher markers made detection ambiguous."""
    global _ambiguous_family_warned
    if _ambiguous_family_warned:
        return
    _ambiguous_family_warned = True
    log.warning(
        'launcher markers for multiple agent families are set (%s); the current agent cannot be '
        "identified from them, so agent_family falls back to 'unknown'. Set KIOKU_MESH_AGENT_FAMILY "
        'in this process (or in the MCP client entry that spawns it) to attribute these saves.',
        ', '.join(sorted(detected)),
    )


def resolve_agent_family() -> tuple[str, IdentitySource]:
    """Resolve agent_family and where it came from.

    Precedence:
        1. ``KIOKU_MESH_AGENT_FAMILY`` — explicit current config
        2. launcher detection (:func:`detect_agent_family`), which declines
           when markers of several families are present at once
        3. ``'unknown'`` — warns once, since it makes the entry unattributable

    The pre-v1.0 env names are deliberately NOT consulted: ADR-0029 removed
    them in v1.0.0 and that removal stands. A client config still exporting an
    old name is a config to repair (``kioku-mesh mcp install --client <client>
    --force``), not a case to keep supporting here.

    Explicit config outranks detection: a value the operator wrote is a
    stronger signal than an env marker that may have leaked in from a parent
    process. ``'unknown'`` remains the last resort rather than a guess, but it
    is no longer silent — an unattributable save means the identity config is
    broken and the operator needs to know.
    """
    v = os.environ.get('KIOKU_MESH_AGENT_FAMILY', '').strip()
    if v:
        return v, IdentitySource.ENV
    detected = detect_agent_family()
    if detected:
        return detected, IdentitySource.DETECTED
    _warn_unresolved_family()
    return 'unknown', IdentitySource.DEFAULT


def _warn_unresolved_family() -> None:
    """Warn once that saves from this process will be unattributable."""
    global _unknown_family_warned
    if _unknown_family_warned:
        return
    _unknown_family_warned = True
    log.warning(
        'agent_family could not be resolved; observations saved from this process will be '
        "recorded as 'unknown' and will not be findable via `search --agent-family`. "
        'Set KIOKU_MESH_AGENT_FAMILY, or re-run `kioku-mesh mcp install --client <client> --force`.'
    )


def resolve_client_id() -> tuple[str, IdentitySource]:
    """Resolve client_id and where it came from.

    ``KIOKU_MESH_CLIENT_ID`` or the default; no launcher exposes a client name
    to detect, and the pre-v1.0 env name is not read (see
    :func:`resolve_agent_family`). Default is ``<user>@<host_short>`` —
    searchable by humans (``--client-id alice@mbp``) and complementary to
    ``pc_id`` which already plays the opaque-UUID role. Falls back to safe
    placeholders when user or hostname can't be resolved (e.g. in minimal
    containers).
    """
    v = os.environ.get('KIOKU_MESH_CLIENT_ID', '').strip()
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
    sid = os.environ.get('KIOKU_MESH_SESSION_ID', '')
    if not sid:
        ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        sid = f'{ts}-{uuid.uuid4().hex[:8]}'
    _session_id_cache = sid
    return sid


def reset_caches() -> None:
    """Clear cached pc_id / session_id and the one-shot warning latches. Test-only helper."""
    global _pc_id_cache, _session_id_cache, _unknown_family_warned, _ambiguous_family_warned
    _pc_id_cache = None
    _session_id_cache = None
    _unknown_family_warned = False
    _ambiguous_family_warned = False

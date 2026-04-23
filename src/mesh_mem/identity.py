"""Resolve identity values for mesh-mem.

Order of precedence per identity:
    - env var (when defined)
    - persisted file on disk (pc_id only)
    - auto-generated on first access (cached for process lifetime)

``pc_id`` and ``session_id`` MUST be stable for the lifetime of the process.
Re-generating ``session_id`` per call would fragment the mesh-mem key
space across Observation/Heartbeat emissions and break searchability.
"""

from datetime import datetime
from datetime import timezone
import os
import pathlib
import uuid

_pc_id_cache: str | None = None
_session_id_cache: str | None = None


def state_dir() -> pathlib.Path:
    """Return the writable state directory, creating it if absent."""
    d = pathlib.Path(
        os.environ.get(
            'MESH_MEM_STATE_DIR',
            pathlib.Path.home() / '.local/share/mesh-mem',
        )
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_pc_id() -> str:
    """Return the per-host stable UUID, generating+persisting it on first call.

    The create-if-absent path uses a **temp-file + ``os.link`` atomic publish**
    so two mesh-mem processes racing on a fresh host cannot observe a
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


def get_agent_family() -> str:
    """Return the agent family (claude / gemini / codex / chatgpt) from env."""
    return os.environ.get('MESH_MEM_AGENT_FAMILY', 'unknown')


def get_client_id() -> str:
    """Return the client id (e.g. ``claude-code``, ``gemini-cli``) from env."""
    return os.environ.get('MESH_MEM_CLIENT_ID', 'unknown')


def get_session_id() -> str:
    """Return the session id, resolved once per process.

    Precedence:
        - ``MESH_MEM_SESSION_ID`` env var if set
        - auto-generated ``{YYYYMMDDTHHMMSSZ}-{short-uuid}``
    """
    global _session_id_cache
    if _session_id_cache is not None:
        return _session_id_cache
    sid = os.environ.get('MESH_MEM_SESSION_ID')
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

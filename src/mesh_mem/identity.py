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

    The create-if-absent path uses ``O_CREAT | O_EXCL`` so two mesh-mem
    processes starting concurrently on a fresh host cannot write divergent
    UUIDs and end up with different cached pc_ids for their lifetimes.
    """
    global _pc_id_cache
    if _pc_id_cache is not None:
        return _pc_id_cache
    p = state_dir() / 'pc_id'
    pid = uuid.uuid4().hex
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        _pc_id_cache = p.read_text().strip()
        return _pc_id_cache
    try:
        os.write(fd, (pid + '\n').encode())
    finally:
        os.close(fd)
    _pc_id_cache = pid
    return pid


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

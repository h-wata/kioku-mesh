"""Diagnostic checks backing `mesh-mem doctor` (#84).

The doctor command exists so a first-touch user can answer "why isn't this
working" without reading the README's Troubleshooting / Time sync / MCP
registration sections one at a time.

Scope decision (Codex consult on #84): keep v0.3 to the small, deterministic
set of checks that a unit test can drive with monkeypatched probes. The wider
"is this the right zenohd / has the clock drifted / which MCP clients are
registered" checks are platform-specific, easy to misdiagnose, and overlap
with #85; they are deferred to a follow-up (or downgraded to best-effort
WARNs once they prove out).
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import socket
from typing import Any, Callable

from . import __version__
from .identity import state_dir

ZENOH_DEFAULT_ENDPOINT = 'tcp/localhost:7447'
ZENOH_CONNECT_TIMEOUT_SEC = 1.0


class CheckStatus(str, Enum):
    """Severity for a single doctor check.

    Ordering (PASS < WARN < FAIL) is used to fold per-check results into a
    single exit code via :func:`worst_status`.
    """

    PASS = 'pass'
    WARN = 'warn'
    FAIL = 'fail'


# Severity rank used by ``worst_status``. Defined alongside the enum so adding
# a new severity doesn't silently default to the lowest rank.
_SEVERITY_RANK: dict[CheckStatus, int] = {
    CheckStatus.PASS: 0,
    CheckStatus.WARN: 1,
    CheckStatus.FAIL: 2,
}


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome.

    ``summary`` is the one-line human-readable headline shown in text output
    and exposed verbatim in JSON. ``hint`` is the actionable next step the
    user should take when ``status`` is not PASS — empty when no action is
    needed. ``details`` holds machine-readable specifics (probed endpoint,
    errno, resolved path) for JSON consumers.
    """

    name: str
    status: CheckStatus
    summary: str
    hint: str = ''
    details: dict[str, Any] = field(default_factory=dict)


def worst_status(results: list[CheckResult]) -> CheckStatus:
    """Return the highest-severity status across ``results``."""
    if not results:
        return CheckStatus.PASS
    return max(results, key=lambda r: _SEVERITY_RANK[r.status]).status


def exit_code_for(status: CheckStatus) -> int:
    """Map a status to a shell exit code (PASS=0, WARN=1, FAIL=2)."""
    return _SEVERITY_RANK[status]


# -- Individual checks ---------------------------------------------------------

# Each check is a pure function: it takes the inputs it needs as arguments,
# returns a CheckResult, and never raises. Tests drive checks directly by
# substituting probe functions / paths.


def _parse_zenoh_endpoint(raw: str) -> tuple[str, int] | None:
    """Parse ``tcp/host:port`` into ``(host, port)``; return None if unparseable.

    Zenoh endpoints look like ``tcp/127.0.0.1:7447`` or ``udp/0.0.0.0:7447``.
    The doctor only probes via TCP — UDP endpoints are reported as
    unprobeable rather than guessed.
    """
    spec = raw.strip()
    if not spec.startswith('tcp/'):
        return None
    host_port = spec[len('tcp/') :]
    if ':' not in host_port:
        return None
    host, _, port_str = host_port.rpartition(':')
    try:
        port = int(port_str)
    except ValueError:
        return None
    if not host or not (0 < port < 65536):
        return None
    return host, port


def check_zenohd_reachable(
    endpoint: str | None = None,
    *,
    timeout: float = ZENOH_CONNECT_TIMEOUT_SEC,
    connect: Callable[[tuple[str, int], float], None] | None = None,
) -> CheckResult:
    """Probe ``ZENOH_CONNECT`` via TCP.

    The probe is a one-shot connect with a short timeout. We do NOT send a
    Zenoh handshake — verifying that *something* listens on the socket is
    enough for "is the local router up", and skipping the handshake keeps
    the dependency surface (and false-positive risk from version skew) tight.
    """
    raw = endpoint if endpoint is not None else os.environ.get('ZENOH_CONNECT', ZENOH_DEFAULT_ENDPOINT)
    parsed = _parse_zenoh_endpoint(raw)
    if parsed is None:
        return CheckResult(
            name='zenohd_reachable',
            status=CheckStatus.FAIL,
            summary=f'ZENOH_CONNECT={raw!r} is not a tcp/host:port endpoint',
            hint='Set ZENOH_CONNECT to a tcp/host:port form (e.g. tcp/127.0.0.1:7447) or unset it to use the default.',
            details={'endpoint': raw},
        )
    host, port = parsed
    probe = connect or _default_tcp_probe
    try:
        probe((host, port), timeout)
    except OSError as e:
        return CheckResult(
            name='zenohd_reachable',
            status=CheckStatus.FAIL,
            summary=f'tcp/{host}:{port} is not reachable',
            hint=(
                'Start zenohd in another terminal: `zenohd -c ~/.config/mesh-mem/zenohd.json5`. '
                'Run `mesh-mem init` first if the config file is missing.'
            ),
            details={'endpoint': raw, 'host': host, 'port': port, 'error': type(e).__name__, 'errno': e.errno},
        )
    return CheckResult(
        name='zenohd_reachable',
        status=CheckStatus.PASS,
        summary=f'tcp/{host}:{port} accepts TCP connections',
        details={'endpoint': raw, 'host': host, 'port': port},
    )


def _default_tcp_probe(addr: tuple[str, int], timeout: float) -> None:
    """Open a short-lived TCP connection. Raises OSError on failure."""
    with socket.create_connection(addr, timeout=timeout):
        pass


def check_zenohd_binary(which: Callable[[str], str | None] | None = None) -> CheckResult:
    """Verify the ``zenohd`` binary is on PATH.

    A missing binary is the most common first-touch failure: `mesh-mem init`
    can write a config, but starting zenohd requires the router to be
    installed separately (apt / cargo / build-from-source).
    """
    resolver = which or shutil.which
    path = resolver('zenohd')
    if path:
        return CheckResult(
            name='zenohd_binary',
            status=CheckStatus.PASS,
            summary=f'zenohd found at {path}',
            details={'path': path},
        )
    return CheckResult(
        name='zenohd_binary',
        status=CheckStatus.FAIL,
        summary='zenohd not found on PATH',
        hint=(
            'Install zenohd via `cargo install --locked zenoh --bin zenohd` and the '
            '`zenoh-backend-rocksdb` plugin, or use the distro package if available. '
            'See the README "Install zenohd" section for current install paths.'
        ),
        details={'path': None},
    )


def check_config_file(path: Path | None = None) -> CheckResult:
    """Verify that a `mesh-mem init`-generated config exists at the default location."""
    target = path if path is not None else _default_config_path()
    if target.is_file():
        return CheckResult(
            name='config_file',
            status=CheckStatus.PASS,
            summary=f'zenohd config present at {target}',
            details={'path': str(target)},
        )
    return CheckResult(
        name='config_file',
        status=CheckStatus.FAIL,
        summary=f'zenohd config missing at {target}',
        hint='Run `mesh-mem init` to generate a starter config.',
        details={'path': str(target)},
    )


def _default_config_path() -> Path:
    """Mirror the path `mesh-mem init` writes (XDG_CONFIG_HOME-aware)."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'mesh-mem' / 'zenohd.json5'


def check_state_dir_hardlinks(state_dir_path: Path | None = None) -> CheckResult:
    """Verify that ``MESH_MEM_STATE_DIR`` resides on a hard-link-capable filesystem.

    ``get_pc_id`` uses ``os.link`` for an atomic publish (see identity.py).
    FAT / exFAT / some older SMB mounts don't support hard links and trigger
    a confusing OSError on first run. Catch that upfront with a real
    temp-file + ``os.link`` round-trip — Codex consult on #84 flagged this
    as the testable variant of the check.
    """
    target = state_dir_path if state_dir_path is not None else _resolve_state_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return CheckResult(
            name='state_dir_hardlinks',
            status=CheckStatus.FAIL,
            summary=f'state dir {target} is not writable',
            hint='Pick a writable path via MESH_MEM_STATE_DIR or fix permissions.',
            details={'path': str(target), 'error': type(e).__name__, 'errno': e.errno},
        )
    probe_src = target / f'.doctor.tmp.{os.getpid()}'
    probe_dst = target / f'.doctor.link.{os.getpid()}'
    try:
        probe_src.write_text('doctor probe', encoding='utf-8')
        try:
            os.link(probe_src, probe_dst)
        except OSError as e:
            return CheckResult(
                name='state_dir_hardlinks',
                status=CheckStatus.FAIL,
                summary=f'state dir {target} does not support hard links',
                hint=(
                    'mesh-mem stores pc_id via an atomic os.link publish. Move MESH_MEM_STATE_DIR '
                    'onto ext4 / btrfs / xfs / tmpfs / NFSv3+ (FAT / exFAT / some SMB shares do not qualify).'
                ),
                details={'path': str(target), 'error': type(e).__name__, 'errno': e.errno},
            )
    finally:
        for f in (probe_dst, probe_src):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
    return CheckResult(
        name='state_dir_hardlinks',
        status=CheckStatus.PASS,
        summary=f'state dir {target} is writable and supports hard links',
        details={'path': str(target)},
    )


def _resolve_state_dir() -> Path:
    """Resolve MESH_MEM_STATE_DIR without triggering identity caching side-effects."""
    return state_dir()


# -- Orchestration & rendering -------------------------------------------------


def run_all_checks() -> list[CheckResult]:
    """Run every v0.3 doctor check in stable order.

    Order matters for the human-readable text output (most foundational
    failures first). JSON consumers should look at the per-check ``name``
    rather than relying on index.
    """
    return [
        check_zenohd_binary(),
        check_config_file(),
        check_zenohd_reachable(),
        check_state_dir_hardlinks(),
    ]


def to_json(results: list[CheckResult]) -> str:
    """Serialize results into the documented JSON shape.

    Shape:
        {
          "version": "<mesh-mem version>",
          "ok": bool,
          "worst_status": "pass" | "warn" | "fail",
          "checks": [{"name", "status", "summary", "hint", "details"}, ...]
        }
    """
    worst = worst_status(results)
    payload = {
        'version': __version__,
        'ok': worst is CheckStatus.PASS,
        'worst_status': worst.value,
        'checks': [_check_to_dict(r) for r in results],
    }
    return json.dumps(payload, ensure_ascii=False)


def _check_to_dict(result: CheckResult) -> dict[str, Any]:
    """Convert a CheckResult into a JSON-friendly dict (enum -> string)."""
    d = asdict(result)
    d['status'] = result.status.value
    return d


_STATUS_TEXT_LABEL: dict[CheckStatus, str] = {
    CheckStatus.PASS: 'PASS',
    CheckStatus.WARN: 'WARN',
    CheckStatus.FAIL: 'FAIL',
}


def format_text(results: list[CheckResult]) -> str:
    """Render results as plain text suitable for terminal output.

    Layout: one block per check (``[STATUS] name — summary`` + optional hint
    indented under it), followed by a one-line overall verdict.
    """
    lines: list[str] = []
    for r in results:
        lines.append(f'[{_STATUS_TEXT_LABEL[r.status]}] {r.name} — {r.summary}')
        if r.hint:
            lines.append(f'    hint: {r.hint}')
    worst = worst_status(results)
    verdict = {
        CheckStatus.PASS: 'all checks passed',
        CheckStatus.WARN: 'completed with warnings',
        CheckStatus.FAIL: 'one or more checks failed',
    }[worst]
    lines.append('')
    lines.append(f'verdict: {verdict}')
    return '\n'.join(lines)

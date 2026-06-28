"""Diagnostic checks backing `kioku-mesh doctor` (#84).

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
import logging
import os
from pathlib import Path
import shutil
import socket
from typing import Any, Callable

from kioku_mesh.core._env_compat import get_env

from . import __version__
from .identity import state_dir
from .paths import resolve_app_dir

log = logging.getLogger(__name__)

ZENOH_DEFAULT_ENDPOINT = 'tcp/localhost:7447'
ZENOH_CONNECT_TIMEOUT_SEC = 1.0
MESH_ROUTER_DEFAULT_ENDPOINT = 'tcp/localhost:17447'


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
    # Accept both tcp/ and tls/: a TLS endpoint still rides on TCP, so a plain
    # TCP connect is a valid liveness probe for "is the router up" even though
    # we don't complete the TLS handshake.
    if spec.startswith('tcp/'):
        host_port = spec[len('tcp/') :]
    elif spec.startswith('tls/'):
        host_port = spec[len('tls/') :]
    else:
        return None
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
                'Start zenohd in another terminal: `zenohd -c ~/.config/kioku-mesh/zenohd.json5`. '
                'Run `kioku-mesh init` first if the config file is missing.'
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

    A missing binary is the most common first-touch failure: `kioku-mesh init`
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
    """Verify that a `kioku-mesh init`-generated config exists at the default location."""
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
        hint='Run `kioku-mesh init` to generate a starter config.',
        details={'path': str(target)},
    )


def _default_config_path() -> Path:
    """Mirror the path `kioku-mesh init` writes (XDG_CONFIG_HOME-aware)."""
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return resolve_app_dir(Path(base)) / 'zenohd.json5'


def check_state_dir_hardlinks(state_dir_path: Path | None = None) -> CheckResult:
    """Verify that ``KIOKU_MESH_STATE_DIR`` resides on a hard-link-capable filesystem.

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
            hint='Pick a writable path via KIOKU_MESH_STATE_DIR or fix permissions.',
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
                    'kioku-mesh stores pc_id via an atomic os.link publish. Move KIOKU_MESH_STATE_DIR '
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
    """Resolve KIOKU_MESH_STATE_DIR without triggering identity caching side-effects."""
    return state_dir()


def check_embedded_router(
    endpoint: str | None = None,
    *,
    timeout: float = ZENOH_CONNECT_TIMEOUT_SEC,
    connect: Callable[[tuple[str, int], float], None] | None = None,
) -> CheckResult:
    """Probe the embedded zenoh router listen endpoint via TCP.

    Reads ``KIOKU_MESH_ROUTER_ENDPOINT`` (default ``tcp/localhost:17447``).
    A missing router is WARN (not FAIL) because zenohd or a remote router
    may serve the same role.
    """
    raw = endpoint if endpoint is not None else get_env('KIOKU_MESH_ROUTER_ENDPOINT', MESH_ROUTER_DEFAULT_ENDPOINT)
    parsed = _parse_zenoh_endpoint(raw)
    if parsed is None:
        return CheckResult(
            name='embedded_router',
            status=CheckStatus.WARN,
            summary=f'KIOKU_MESH_ROUTER_ENDPOINT={raw!r} is not a tcp/host:port endpoint',
            hint='Set KIOKU_MESH_ROUTER_ENDPOINT to a tcp/host:port form (e.g. tcp/127.0.0.1:17447).',
            details={'endpoint': raw},
        )
    host, port = parsed
    probe = connect or _default_tcp_probe
    try:
        probe((host, port), timeout)
    except OSError:
        return CheckResult(
            name='embedded_router',
            status=CheckStatus.WARN,
            summary=f'Embedded router not reachable at tcp/{host}:{port}',
            hint='Run `kioku-mesh mesh start` to start an in-process router (no zenohd needed).',
            details={'endpoint': raw, 'host': host, 'port': port, 'running': False},
        )
    # TCP reachable — try a zenoh peer probe to get router identity.
    # Note: connected_peers count requires in-process access to the router session;
    # it is not available via external probe. We report the router ZIDs visible
    # from a short-lived peer connection as the best external approximation.
    router_zids: list[str] = []
    try:
        import time

        import zenoh as _zenoh

        tmp_cfg = _zenoh.Config()
        tmp_cfg.insert_json5('mode', '"peer"')
        tmp_cfg.insert_json5('connect/endpoints', f'["{raw}"]')
        tmp_cfg.insert_json5('scouting/multicast/enabled', 'false')
        tmp_session = _zenoh.open(tmp_cfg)
        time.sleep(0.3)
        router_zids = [str(z) for z in tmp_session.info.routers_zid()]
        tmp_session.close()
    except Exception:  # noqa: BLE001
        pass

    return CheckResult(
        name='embedded_router',
        status=CheckStatus.PASS,
        summary=f'Embedded router listening on tcp/{host}:{port}',
        details={
            'endpoint': raw,
            'host': host,
            'port': port,
            'running': True,
            'router_zids': router_zids,
            'peer_count_note': (
                'connected_peers count requires in-process router access; '
                'router_zids shows routers visible from external probe'
            ),
        },
    )


# mTLS peer certs renewed with this much runway left are still PASS; below it we
# WARN so a rotation happens before a silent mesh-wide handshake failure.
TLS_CERT_WARN_DAYS = 30


def check_tls_certs(config_path: Path | None = None) -> CheckResult:
    """Validate the mTLS cert store when (and only when) the mesh config uses TLS.

    Non-TLS deployments (network-admission trust) PASS with a note rather than
    nagging about absent certs. When the generated config references
    ``enable_mtls``, the three cert-store files must exist and the peer cert
    must not be expired (or near expiry).
    """
    cfg = config_path if config_path is not None else _default_config_path()
    tls_in_use = False
    if cfg.is_file():
        try:
            tls_in_use = 'enable_mtls' in cfg.read_text(encoding='utf-8')
        except OSError:
            tls_in_use = False

    from . import tls as tls_module

    ca = tls_module.ca_cert_path()
    cert = tls_module.peer_cert_path()
    key = tls_module.peer_key_path()

    # Only the active config decides whether certs matter. A plaintext config
    # left behind stale/expired cert files (e.g. after reverting from --tls)
    # must not FAIL/WARN — those files are simply unused here.
    if not tls_in_use:
        return CheckResult(
            name='tls_certs',
            status=CheckStatus.PASS,
            summary='mTLS not configured (using network-admission trust)',
            details={'tls_in_use': False},
        )

    missing = [str(p) for p in (ca, cert, key) if not p.is_file()]
    if missing:
        return CheckResult(
            name='tls_certs',
            status=CheckStatus.FAIL,
            summary='mTLS config references certs that are missing from the TLS store',
            hint=(
                'Provision them: `kioku-mesh tls init-ca` (CA host), `kioku-mesh tls request --san <addr>` '
                '(this host) -> sign on the CA host -> `kioku-mesh tls install`.'
            ),
            details={'tls_in_use': tls_in_use, 'missing': missing},
        )

    try:
        info = tls_module.inspect_cert(cert.read_bytes())
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name='tls_certs',
            status=CheckStatus.FAIL,
            summary=f'peer certificate at {cert} is unreadable',
            hint='Re-run `kioku-mesh tls request` / `tls install` to regenerate it.',
            details={'tls_in_use': tls_in_use, 'error': type(e).__name__},
        )

    details = {
        'tls_in_use': tls_in_use,
        'not_valid_after': info.not_valid_after.isoformat(),
        'days_remaining': info.days_remaining,
        'sans': info.sans,
    }
    if info.expired:
        return CheckResult(
            name='tls_certs',
            status=CheckStatus.FAIL,
            summary=f'peer certificate expired on {info.not_valid_after:%Y-%m-%d}',
            hint='Rotate it: `kioku-mesh tls request` -> sign on the CA host -> `kioku-mesh tls install`.',
            details=details,
        )
    if info.days_remaining < TLS_CERT_WARN_DAYS:
        return CheckResult(
            name='tls_certs',
            status=CheckStatus.WARN,
            summary=f'peer certificate expires in {info.days_remaining} days',
            hint='Rotate soon: `kioku-mesh tls request` -> sign on the CA host -> `kioku-mesh tls install`.',
            details=details,
        )
    return CheckResult(
        name='tls_certs',
        status=CheckStatus.PASS,
        summary=f'mTLS peer certificate valid for {info.days_remaining} more days',
        details=details,
    )


# ADR-0021: FTS5 capability check.


def check_fts5(index: object = None) -> CheckResult:
    """Report FTS5 and trigram tokenizer availability in the local SQLite index.

    A WARN (not FAIL) indicates LIKE fallback is in use — search still works,
    but recall for Japanese queries and bm25 ranking are unavailable.
    """
    from .memory.local_index import _FTS_CAP_LIKE  # noqa: PLC0415
    from .memory.local_index import _FTS_CAP_TRIGRAM  # noqa: PLC0415
    from .memory.local_index import LocalIndex  # noqa: PLC0415

    idx: object = index
    if idx is None:
        try:
            idx = LocalIndex.connect()
        except Exception:  # noqa: BLE001
            return CheckResult(
                name='fts5',
                status=CheckStatus.WARN,
                summary='FTS5 check skipped: could not open local index',
                hint='Check KIOKU_MESH_INDEX_DB or run `kioku-mesh init`.',
            )
    fts_cap = getattr(idx, '_fts_cap', _FTS_CAP_LIKE)
    if fts_cap == _FTS_CAP_TRIGRAM:
        return CheckResult(
            name='fts5',
            status=CheckStatus.PASS,
            summary='FTS5 trigram available',
            details={'fts_cap': fts_cap},
        )
    if fts_cap != _FTS_CAP_LIKE:
        return CheckResult(
            name='fts5',
            status=CheckStatus.PASS,
            summary='FTS5 available (trigram not available, using standard FTS5)',
            hint='Upgrade SQLite >= 3.38.0 to enable trigram tokenizer for Japanese substring search.',
            details={'fts_cap': fts_cap},
        )
    return CheckResult(
        name='fts5',
        status=CheckStatus.WARN,
        summary='FTS5 not available, using LIKE fallback',
        hint='Upgrade SQLite to a build with FTS5 enabled for full-text search support.',
        details={'fts_cap': fts_cap},
    )


# ADR-0028 Phase 1: shadow visibility check.


def check_shadow_visibility(index: object = None) -> CheckResult:
    """Report the number of rebuild-shadowed observations in the local index.

    Shadowed observations were present in the local index but not seen during
    the last ``rebuild_from_zenoh`` sweep. They are hidden from search and
    ranking but have not been physically deleted. A WARN means the index has
    unresolved shadow state that may indicate a rebuild coverage gap; running
    ``kioku-mesh gc --shadows`` or waiting for GC retention to expire will
    eventually clean them up.
    """
    from .memory.local_index import LocalIndex  # noqa: PLC0415

    idx: object = index
    if idx is None:
        try:
            idx = LocalIndex.connect()
        except Exception:  # noqa: BLE001
            return CheckResult(
                name='shadow_visibility',
                status=CheckStatus.WARN,
                summary='shadow visibility check skipped: could not open local index',
                hint='Check KIOKU_MESH_INDEX_DB or run `kioku-mesh init`.',
            )
    try:
        rows = idx.list_shadowed_obs(limit=10_000)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return CheckResult(
            name='shadow_visibility',
            status=CheckStatus.WARN,
            summary='shadow visibility check skipped: list_shadowed_obs failed',
        )
    shadowed_count = len(rows)
    if shadowed_count == 0:
        return CheckResult(
            name='shadow_visibility',
            status=CheckStatus.PASS,
            summary='shadowed observations: 0',
            details={'shadowed': 0},
        )
    # Build per-project summary for details.
    by_project: dict[str, int] = {}
    for _obs_id, proj, _created_at, _shadowed_at, _summary in rows:
        key = proj or '(no project)'
        by_project[key] = by_project.get(key, 0) + 1
    return CheckResult(
        name='shadow_visibility',
        status=CheckStatus.WARN,
        summary=(
            f'shadowed observations: {shadowed_count} '
            '(missing from source-of-truth during rebuild, hidden from search, not yet physically deleted)'
        ),
        hint=(
            'Shadowed rows are cleaned up automatically by `kioku-mesh gc --shadows`. '
            'If counts are unexpectedly high, re-run `rebuild_from_zenoh` or inspect '
            'with `kioku-mesh status --show-shadows`.'
        ),
        details={'shadowed': shadowed_count, 'by_project': by_project},
    )


# ADR-0026 §C: conflicting-latest check.

# Upper bound on the live set this check scans. Mirrors the supersede
# detector's _POOL_LIMIT: a sweep that hits this cap may have missed
# conflicts beyond it, so the result is reported as inconclusive rather
# than a clean PASS (the C3 "no silent truncation" rule, applied to doctor).
_CONFLICT_SCAN_LIMIT = 10_000


def check_conflicting_latest(observations: list[Any] | None = None) -> CheckResult:
    """Flag subjects that carry more than one live decision/config entry.

    The save-time supersede suggestion (ADR-0026 §A) cannot fire when two
    hosts each save a new decision for the same subject before seeing each
    other's write — locally, neither sees the other as a candidate. The
    result is several live, non-superseded ``decision`` / ``config`` entries
    that share a (project, normalized subject, memory_type, scope) key, with
    no signal of which is current. This read-only sweep surfaces those
    groups so an operator can supersede or delete the stale ones. It is
    deliberately local (no global reorganization, per arXiv:2606.24775).

    ``observations`` is injectable for tests; by default it pulls the live,
    non-superseded set from the active backend. The default fetch is capped
    at :data:`_CONFLICT_SCAN_LIMIT`; hitting that cap is surfaced (debug log
    + ``details['truncated']``) instead of being silently treated as a clean
    scan. An injected list is taken as complete — truncation only applies to
    the backend-fetched path.
    """
    from .memory.supersede import normalize_subject  # noqa: PLC0415
    from .memory.supersede import SUPERSEDE_TYPES  # noqa: PLC0415

    truncated = False
    if observations is None:
        try:
            from .memory.backend import get_backend  # noqa: PLC0415

            observations = get_backend().search_observations(limit=_CONFLICT_SCAN_LIMIT, include_superseded=False)
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name='conflicting_latest',
                status=CheckStatus.WARN,
                summary='conflicting-latest check skipped: could not read memory',
                hint='Check the backend config / `kioku-mesh status`.',
                details={'error': type(e).__name__},
            )
        if len(observations) >= _CONFLICT_SCAN_LIMIT:
            truncated = True
            log.debug(
                'conflicting-latest scan reached _CONFLICT_SCAN_LIMIT=%d live entries; '
                'conflicts beyond the cap are not checked',
                _CONFLICT_SCAN_LIMIT,
            )

    groups: dict[tuple[str, str, str, str, str], list[Any]] = {}
    for o in observations:
        if o.memory_type not in SUPERSEDE_TYPES:
            continue
        subject_key = normalize_subject(o.subject)
        if not subject_key:
            continue
        key = (o.project, o.memory_type, subject_key, o.visibility, o.scope_id)
        groups.setdefault(key, []).append(o)

    conflicts = {k: v for k, v in groups.items() if len(v) > 1}
    if not conflicts:
        if truncated:
            # Cannot certify a clean state: the cap may have hidden conflicts.
            return CheckResult(
                name='conflicting_latest',
                status=CheckStatus.WARN,
                summary=(
                    f'no conflicts found, but the scan was truncated at {_CONFLICT_SCAN_LIMIT} '
                    'live entries — result is incomplete'
                ),
                hint=(
                    'Narrow the working set (e.g. per-project) or raise the scan limit to verify; '
                    'this is a soft cap, not a correctness boundary.'
                ),
                details={'conflicts': 0, 'truncated': True, 'scan_limit': _CONFLICT_SCAN_LIMIT},
            )
        return CheckResult(
            name='conflicting_latest',
            status=CheckStatus.PASS,
            summary='no subject has multiple live decision/config entries',
            details={'conflicts': 0, 'truncated': False},
        )

    examples = []
    for (project, mtype, subject_key, _vis, _scope), obs_list in sorted(conflicts.items())[:5]:
        ids = [o.observation_id for o in obs_list]
        examples.append(
            {
                'project': project,
                'memory_type': mtype,
                'subject': subject_key,
                'count': len(obs_list),
                'observation_ids': ids,
            }
        )
    summary = f'{len(conflicts)} subject(s) have multiple live decision/config entries'
    if truncated:
        summary += f' (scan truncated at {_CONFLICT_SCAN_LIMIT}; more may exist)'
    return CheckResult(
        name='conflicting_latest',
        status=CheckStatus.WARN,
        summary=summary,
        hint=(
            'Resolve each by superseding (save the current one with supersedes=[old_ids]) '
            'or deleting the stale entries (`kioku-mesh delete <id>`).'
        ),
        details={'conflicts': len(conflicts), 'examples': examples, 'truncated': truncated},
    )


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
        check_embedded_router(),
        check_tls_certs(),
        check_fts5(),
        check_shadow_visibility(),
        check_conflicting_latest(),
    ]


def to_json(results: list[CheckResult]) -> str:
    """Serialize results into the documented JSON shape.

    Shape:
        {
          "version": "<kioku-mesh version>",
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

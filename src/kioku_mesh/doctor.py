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
    raw = (
        endpoint
        if endpoint is not None
        else os.environ.get('KIOKU_MESH_ROUTER_ENDPOINT', MESH_ROUTER_DEFAULT_ENDPOINT)
    )
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
    unresolved shadow state that may indicate a rebuild coverage gap; shadow
    rows are physically deleted by default during ``kioku-mesh gc``'s
    retention sweep (pass ``--no-shadow-prune`` to skip it), so waiting for
    the next GC run will eventually clean them up.
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
            "Shadowed rows are physically deleted by default during `kioku-mesh gc`'s "
            'retention sweep (pass --no-shadow-prune to disable). '
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


_FRAGMENT_SCAN_LIMIT = 10_000


def check_tool_call_fragments(observations: list[Any] | None = None) -> CheckResult:
    """List stored observations whose text carries leaked MCP tool-call markup.

    The save-time guard (``save_observation``) only protects writes made after
    it shipped; entries saved before it, or replicated from a peer running an
    older build, are already in the store. This read-only sweep surfaces them
    so an operator can decide what to do — it never rewrites anything (repair
    is a one-off append-only supersede-copy, not a doctor action).

    Scans the *effective* set (live, non-superseded) — the entries
    ``search_memory`` / ``recall_context`` actually return. ``observations``
    is injectable for tests; the default fetch is capped at
    :data:`_FRAGMENT_SCAN_LIMIT` and a hit on that cap is surfaced in
    ``details['truncated']``.
    """
    from .memory.save_lint import find_tool_call_fragment  # noqa: PLC0415

    truncated = False
    if observations is None:
        try:
            from .memory.backend import get_backend  # noqa: PLC0415

            observations = get_backend().search_observations(limit=_FRAGMENT_SCAN_LIMIT, include_superseded=False)
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name='tool_call_fragments',
                status=CheckStatus.WARN,
                summary='tool-call fragment scan skipped: could not read memory',
                hint='Check the backend config / `kioku-mesh status`.',
                details={'error': type(e).__name__},
            )
        truncated = len(observations) >= _FRAGMENT_SCAN_LIMIT

    hits = []
    for o in observations:
        for field_name in ('content', 'subject', 'summary'):
            fragment = find_tool_call_fragment(getattr(o, field_name, '') or '')
            if fragment:
                hits.append(
                    {
                        'observation_id': o.observation_id,
                        'created_at': o.created_at[:10],
                        'field': field_name,
                        'fragment': fragment,
                    }
                )
                break

    scanned = len(observations)
    details: dict[str, Any] = {'hits': len(hits), 'scanned': scanned, 'truncated': truncated}
    if not hits:
        return CheckResult(
            name='tool_call_fragments',
            status=CheckStatus.PASS,
            summary=f'no tool-call markup found in {scanned} effective (live, non-superseded) entries',
            details=details,
        )
    details['examples'] = hits[:5]
    summary = f'{len(hits)} of {scanned} effective (live, non-superseded) entries carry MCP tool-call markup'
    if truncated:
        summary += f' (scan truncated at {_FRAGMENT_SCAN_LIMIT}; more may exist)'
    return CheckResult(
        name='tool_call_fragments',
        status=CheckStatus.WARN,
        summary=summary,
        hint=(
            'Inspect with `kioku-mesh get-memory <id>`. Repair by saving a cleaned copy with '
            'supersedes=[old_id] (ADR-0028 append-only); nothing is rewritten in place.'
        ),
        details=details,
    )


# ADR-0019 Phase D: legacy namespace preflight check.


def _config_file_replication(config_path: Path) -> dict[str, Any]:
    """Return ``{storage_name: replication_block}`` from the rendered zenohd config.

    N2: the admin space exposes ``key_expr`` / ``strip_prefix`` / ``volume``
    and nothing else — the ``replication`` block is simply not there
    (measured against a production router). So replication settings are read
    back from the file the renderer wrote; agreement between *peers* can only
    be established by the two-node harness (design v3 task 4), never by this
    check. Parsed with zenoh's own JSON5 reader so comments and trailing
    commas in the generated config are handled.
    """
    try:
        import zenoh as _zenoh  # noqa: PLC0415

        cfg = _zenoh.Config.from_file(str(config_path))
        raw = cfg.get_json('plugins/storage_manager/storages')
        storages = json.loads(raw)
    except Exception:  # noqa: BLE001 — an unreadable config is reported by check_config_file
        return {}
    if not isinstance(storages, dict):
        return {}
    return {name: body.get('replication') for name, body in storages.items() if isinstance(body, dict)}


def check_storage_scopes(
    *,
    live: list[Any] | None = None,
    config_path: Path | None = None,
    endpoint: str | None = None,
    blocked_pending: list[tuple[str, str]] | None = None,
    embedded_router: bool | None = None,
) -> CheckResult:
    """Compare declared ``storage_scopes`` with this host's live zenohd storages.

    FAIL means writes have (or will have) nowhere durable to land: a scope
    declared but never rendered, a rendered storage never restarted into, a
    wrong dir / strip prefix, or the pre-split broad ``agent_mem`` still
    overlapping a scope. Peer storages are shown for diagnosis only —
    durability is judged against self (design v3, B2).
    """
    from .core import scope as scope_mod  # noqa: PLC0415

    details: dict[str, Any] = {}
    try:
        declared = scope_mod.resolve_storage_scopes()
    except scope_mod.ScopeConfigError as e:
        return CheckResult(
            name='storage_scopes',
            status=CheckStatus.FAIL,
            summary=f'storage_scopes is invalid: {e}',
            hint='Fix storage_scopes in ~/.config/kioku-mesh/config.yaml (entries: mesh, user/<id>, team/<id>).',
        )
    details['declared'] = [s.label for s in declared]

    local_ok = scope_mod.local_router_endpoint_ok(endpoint)
    details['local_router_endpoint'] = local_ok

    if live is None:
        try:
            from .core.transport import get_session  # noqa: PLC0415

            session = get_session()
            if embedded_router is None:
                embedded_router = scope_mod.is_storageless_embedded_router(session)
            live = scope_mod.fetch_self_storages(session)
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                name='storage_scopes',
                status=CheckStatus.FAIL,
                summary=f'cannot read live storages from zenohd ({type(e).__name__})',
                hint='Start zenohd (or fix ZENOH_CONNECT), then re-run `kioku-mesh doctor`.',
                details={**details, 'error': str(e)},
            )
    details['live'] = [
        {'name': s.name, 'key_expr': s.key_expr, 'strip_prefix': s.strip_prefix, 'volume_dir': s.volume_dir}
        for s in live
    ]
    details['embedded_router'] = bool(embedded_router)

    # Tier 1 (`kioku-mesh mesh start`): that router cannot hold a storage at
    # all, so a mesh-only host is allowed to save through it. Say so plainly
    # instead of reporting a storage set it can never have. Only for a local
    # endpoint — a remote embedded router is not this host's quickstart, and
    # the write gate refuses it (N6), so doctor must not advertise it as
    # accepted either.
    if embedded_router and local_ok and [s.label for s in declared] == ['mesh']:
        return CheckResult(
            name='storage_scopes',
            status=CheckStatus.WARN,
            summary='Tier 1 embedded router (mesh start): mesh-only saves are accepted but not stored durably',
            hint=(
                'Saves reach connected peers live and this process indexes them, but no storage keeps them. '
                'Run zenohd (`kioku-mesh init` + start) for durable storage. '
                'user/team scopes stay refused on this router.'
            ),
            details=details,
        )

    problems: list[str] = []
    for spec in declared:
        match = [s for s in live if s.key_expr == spec.key_expr and s.strip_prefix == spec.strip_prefix]
        if not match:
            problems.append(
                f'{spec.label}: no live storage with key_expr={spec.key_expr} strip_prefix={spec.strip_prefix}'
            )
            continue
        if len(match) > 1:
            problems.append(f'{spec.label}: ambiguous — {len(match)} live storages claim it')
        wrong_dir = [s for s in match if s.volume_dir != spec.volume_dir]
        if wrong_dir:
            problems.append(f'{spec.label}: volume dir is {wrong_dir[0].volume_dir!r}, expected {spec.volume_dir!r}')

    declared_key_exprs = {s.key_expr for s in declared}
    overlapping = [
        s
        for s in live
        if s.key_expr not in declared_key_exprs and any(s.covers(f'{spec.key_prefix}/obs/probe') for spec in declared)
    ]
    if overlapping:
        problems.append(
            'broad/overlapping storage still present: ' + ', '.join(f'{s.name}({s.key_expr})' for s in overlapping)
        )

    cfg = config_path if config_path is not None else _default_config_path()
    details['replication_from_config'] = _config_file_replication(cfg)
    details['replication_source'] = (
        f'{cfg} — the Zenoh admin space does not expose replication settings, so peer agreement '
        'is verified by the two-node harness, not by doctor'
    )

    if blocked_pending is None:
        try:
            from .memory.pending_queue import scope_blocked_pending_puts  # noqa: PLC0415

            blocked_pending = scope_blocked_pending_puts()
        except Exception:  # noqa: BLE001
            blocked_pending = []
    if blocked_pending:
        details['scope_blocked_pending_puts'] = [{'key_expr': k, 'reason': r} for k, r in blocked_pending]
        problems.append(f'{len(blocked_pending)} queued put(s) are blocked by the scope preflight and stay queued')

    if problems:
        details['problems'] = problems
        return CheckResult(
            name='storage_scopes',
            status=CheckStatus.FAIL,
            summary=f'declared storage_scopes do not match the live zenohd ({len(problems)} problem(s))',
            hint=(
                'Run `kioku-mesh config render-storages --apply` and restart zenohd; '
                'apply the same change on every host sharing the scope.'
            ),
            details=details,
        )
    if not local_ok:
        return CheckResult(
            name='storage_scopes',
            status=CheckStatus.WARN,
            summary='storage scopes match, but ZENOH_CONNECT is not a local router so "self" may be another host',
            hint='Start the local zenohd and point ZENOH_CONNECT at it before trusting this check.',
            details=details,
        )
    return CheckResult(
        name='storage_scopes',
        status=CheckStatus.PASS,
        summary=f'live storages match declared storage_scopes ({", ".join(s.label for s in declared)})',
        details=details,
    )


def check_legacy_namespace(
    session: Any | None = None,
    *,
    records: list[Any] | None = None,
) -> CheckResult:
    """Detect unmigrated legacy namespace observations (ADR-0019 Phase D preflight).

    Scans ``mem/obs/**`` and ``mem/tomb/**`` for observations not yet migrated
    to visibility-tiered namespaces. Returns WARN if legacy records exist,
    PASS if the legacy namespace is empty.

    ``records`` is injectable for tests (list of
    :class:`~kioku_mesh.memory.visibility_migration.RawLegacyRecord`).
    When ``records`` is ``None``, ``session`` is used to perform a Zenoh scan
    via :func:`~kioku_mesh.memory.visibility_migration.scan_legacy_visibility`.
    If both are ``None``, the check attempts to open a transient Zenoh session;
    if that fails it returns WARN with a skip notice.
    """
    from .memory.visibility_migration import scan_legacy_visibility  # noqa: PLC0415

    if records is None:
        _session = session
        _opened = False
        if _session is None:
            try:
                import zenoh as _zenoh  # noqa: PLC0415

                cfg = _zenoh.Config()
                _session = _zenoh.open(cfg)
                _opened = True
            except Exception:  # noqa: BLE001
                return CheckResult(
                    name='legacy_namespace',
                    status=CheckStatus.WARN,
                    summary='legacy namespace check skipped: could not open Zenoh session',
                    hint='Ensure zenohd is running, then re-run `kioku-mesh doctor`.',
                    details={'skipped': True},
                )
        try:
            records = scan_legacy_visibility(_session)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name='legacy_namespace',
                status=CheckStatus.WARN,
                summary='legacy namespace check skipped: scan failed',
                hint='Check the Zenoh connection and retry `kioku-mesh doctor`.',
                details={'error': type(exc).__name__},
            )
        finally:
            if _opened:
                try:
                    _session.close()
                except Exception:  # noqa: BLE001
                    pass

    obs_records = [r for r in records if r.kind == 'obs']
    tomb_records = [r for r in records if r.kind == 'tomb']
    legacy_obs = len(obs_records)
    legacy_tomb = len(tomb_records)

    samples = [r.key for r in records[:5]]

    # Extract scope hints (client_id/pc_id) from key segments.
    # Legacy key shape: mem/obs/<client_id>/<pc_id>/...
    scope_hints: list[str] = []
    _seen_scopes: set[str] = set()
    for r in records[:20]:
        parts = r.key.split('/')
        if len(parts) >= 4:
            scope = f'{parts[2]}/{parts[3]}'
            if scope not in _seen_scopes:
                _seen_scopes.add(scope)
                scope_hints.append(scope)

    # Try to extract min/max created_at from JSON payloads.
    earliest_ts: str | None = None
    latest_ts: str | None = None
    for r in records:
        try:
            payload_dict = json.loads(r.payload)
            ts = payload_dict.get('created_at', '')
            if ts:
                if earliest_ts is None or ts < earliest_ts:
                    earliest_ts = ts
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
        except Exception:  # noqa: BLE001
            pass

    if legacy_obs == 0 and legacy_tomb == 0:
        return CheckResult(
            name='legacy_namespace',
            status=CheckStatus.PASS,
            summary='legacy namespace: no unmigrated observations found',
            details={
                'legacy_obs': 0,
                'legacy_tomb': 0,
                'samples': [],
                'scope_hints': [],
                'earliest_ts': None,
                'latest_ts': None,
            },
        )

    return CheckResult(
        name='legacy_namespace',
        status=CheckStatus.WARN,
        summary=(
            f'legacy namespace: {legacy_obs} obs and {legacy_tomb} tombstones '
            'found in mem/obs/** / mem/tomb/** — run migrate-visibility to clear'
        ),
        hint='Run `kioku-mesh migrate-visibility --from legacy --to <user|team|mesh>` to migrate.',
        details={
            'legacy_obs': legacy_obs,
            'legacy_tomb': legacy_tomb,
            'samples': samples,
            'scope_hints': scope_hints,
            'earliest_ts': earliest_ts,
            'latest_ts': latest_ts,
        },
    )


# -- Identity configuration check (TASK-275 §3.1) ------------------------------

# Env var prefix retired in v1.0.0 (ADR-0029 / #266). Hand-written MCP client
# configs that still carry it are how 286 consecutive observations came to be
# saved as `unknown` for five weeks without anyone noticing.
#
# Reported as FAIL: identity resolution is KIOKU_MESH_* -> launcher detection
# -> unknown, with no read of the retired prefix (#275 settled on removing it
# rather than reinstating a deprecated fallback). A config that names only the
# old keys therefore does not set identity at all — it is broken, not merely
# deprecated.
_LEGACY_ENV_PREFIX = 'MESH_MEM_'
_CURRENT_ENV_PREFIX = 'KIOKU_MESH_'

# Current identity env vars, quoted in the remediation hint so the fix is
# copy-pasteable rather than a pointer to the docs.
_IDENTITY_ENV_KEYS = ('KIOKU_MESH_AGENT_FAMILY', 'KIOKU_MESH_CLIENT_ID')

# The retired names this check looks for: exactly the counterparts of
# _IDENTITY_ENV_KEYS. Scoped to identity rather than to the whole MESH_MEM_*
# prefix so the finding, the summary and the "rename to
# KIOKU_MESH_AGENT_FAMILY / KIOKU_MESH_CLIENT_ID" hint all describe the same
# thing — a prefix-wide scan would FAIL on e.g. MESH_MEM_STATE_DIR while
# advising a rename that has nothing to do with it.
_LEGACY_IDENTITY_ENV_KEYS = tuple(_LEGACY_ENV_PREFIX + k[len(_CURRENT_ENV_PREFIX) :] for k in _IDENTITY_ENV_KEYS)

# How many recent observations the unknown-dominance probe reads, and the
# share of them that must be 'unknown' before it warns. 50 is roughly a day
# of active multi-agent use, so a freshly-broken config surfaces within a
# day rather than after a release cycle. 0.8 leaves room for the genuinely
# unattributed writes (CLI `kioku-mesh save`, cron jobs) that are expected
# to stay 'unknown' forever.
_IDENTITY_SCAN_LIMIT = 50
_UNKNOWN_DOMINANCE_RATIO = 0.8

# agent_family values that mean "nobody told us who wrote this".
_UNKNOWN_FAMILIES = ('', 'unknown')


def _default_identity_config_paths() -> list[Path]:
    """Return the MCP client config files this check inspects.

    Claude Code keeps its MCP registrations in ``~/.claude.json``; Codex CLI
    uses ``~/.codex/config.toml`` (path shared with :mod:`.mcp_install` so the
    two never drift). Both are read-only inputs here.
    """
    from .mcp_install import _default_codex_config_path  # noqa: PLC0415

    return [Path.home() / '.claude.json', _default_codex_config_path()]


def _collect_legacy_env_keys(data: Any) -> list[str]:
    """Return retired identity keys under ``data`` that have no current twin.

    Only the keys in :data:`_LEGACY_IDENTITY_ENV_KEYS` count, and only when
    the ``KIOKU_MESH_*`` counterpart is absent from the *same* mapping. A
    config setting both names side by side is already correct — the current
    name is the one that is read, so the retired key is inert and reporting it
    would be noise.

    Other retired ``MESH_MEM_*`` keys (``MESH_MEM_STATE_DIR`` and friends) are
    deliberately out of scope: this check's summary and hint are about
    identity, and a finding that cannot be acted on by following its own hint
    is worse than no finding.

    Walks the parsed config structurally rather than grepping the raw text:
    a substring scan would also fire on prose in a description field or on a
    path that merely mentions the old name.
    """
    found: set[str] = set()
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _LEGACY_IDENTITY_ENV_KEYS:
                    counterpart = _CURRENT_ENV_PREFIX + key[len(_LEGACY_ENV_PREFIX) :]
                    if counterpart not in node:
                        found.add(key)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(found)


def _load_config_mapping(path: Path) -> Any | None:
    """Parse ``path`` as JSON or TOML; return None when it can't be read.

    Fail-soft by contract: an unreadable or malformed client config is not
    evidence of a stale identity setting, so the caller skips that file
    instead of reporting a failure the user cannot act on.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        if path.suffix == '.toml':
            import tomllib  # noqa: PLC0415

            return tomllib.loads(raw.decode('utf-8'))
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.debug('identity check: could not parse %s (%s)', path, type(exc).__name__)
        return None


def _unknown_family_ratio(families: list[str]) -> float:
    """Share of ``families`` that are unset or 'unknown'."""
    if not families:
        return 0.0
    unknown = sum(1 for f in families if (f or '').strip().lower() in _UNKNOWN_FAMILIES)
    return unknown / len(families)


def _readonly_index_db_path() -> Path | None:
    """Resolve the index DB to sample, without creating anything.

    Mirrors how the backends pick their index — LocalBackend keeps its own at
    ``state_dir()/local/index.db``, the Zenoh sidecar index lives at
    ``state_dir()/index.db`` unless ``KIOKU_MESH_INDEX_DB`` overrides it — but
    resolves the path only. ``state_dir(create=False)`` is what keeps a fresh
    host fresh; returns None when there is no file-backed index to read.
    """
    from .core.config import get_backend_mode  # noqa: PLC0415
    from .core.identity import state_dir  # noqa: PLC0415

    try:
        if get_backend_mode() == 'local':
            return state_dir(create=False) / 'local' / 'index.db'
        override = os.environ.get('KIOKU_MESH_INDEX_DB', '').strip()
        if override:
            return None if override == ':memory:' else Path(override)
        return state_dir(create=False) / 'index.db'
    except Exception as exc:  # noqa: BLE001
        log.debug('identity check: could not resolve index path (%s)', type(exc).__name__)
        return None


def _read_recent_agent_families(limit: int) -> list[str] | None:
    """Read ``agent_family`` off the most recent rows without writing anything.

    Deliberately *not* routed through ``get_backend()``: constructing a
    backend creates the state directory, migrates the raw store, applies the
    index schema and may kick off a Zenoh rebuild or subscriber — all
    side effects a diagnostic must not have (a fresh host would come away
    with a raw.db and an index.db it did not have before doctor ran).

    Opens the existing SQLite file through the read-only URI instead, so the
    file is never created and the schema is never applied. Returns None
    whenever the sample cannot be taken (no index yet, unreadable, or a
    schema this build does not know) — an unavailable sample is not evidence
    of a broken identity, so the caller skips that finding rather than
    reporting it.
    """
    path = _readonly_index_db_path()
    if path is None or not path.is_file():
        return None
    import sqlite3  # noqa: PLC0415

    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        log.debug('identity check: could not open %s read-only (%s)', path, type(exc).__name__)
        return None
    try:
        rows = conn.execute(
            'SELECT payload_json FROM obs_index '
            'WHERE deleted_at IS NULL AND shadowed_at IS NULL '
            'ORDER BY created_at DESC LIMIT ?',
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        log.debug('identity check: could not read %s (%s)', path, type(exc).__name__)
        return None
    finally:
        conn.close()

    families: list[str] = []
    for (payload,) in rows:
        try:
            families.append(str(json.loads(payload or '{}').get('agent_family', '') or ''))
        except Exception:  # noqa: BLE001
            # A row whose payload won't parse is not attributable either.
            families.append('')
    return families


def check_identity(
    config_paths: list[Path] | None = None,
    *,
    observations: list[Any] | None = None,
) -> CheckResult:
    """Detect identity settings that have silently stopped taking effect.

    Two symptoms of the same failure mode, folded into one check:

    1. **Retired identity env vars (FAIL).** An MCP client config declares its
       identity only through :data:`_LEGACY_IDENTITY_ENV_KEYS`, with no
       ``KIOKU_MESH_*`` counterpart in the same mapping. Nothing reads that
       prefix any more — v1.0.0 removed it (#266) and #275 kept it removed —
       so those settings do not take effect at all. That is a broken config,
       not a deprecated one, hence FAIL.
    2. **Unknown dominance (WARN).** ``agent_family`` is unset or ``unknown``
       on at least :data:`_UNKNOWN_DOMINANCE_RATIO` of the most recent
       :data:`_IDENTITY_SCAN_LIMIT` observations. WARN rather than FAIL
       because the ratio is a symptom with several possible causes, including
       legitimately unattributed writes (CLI ``kioku-mesh save``, cron).

    The retired-key finding takes the headline when both fire — it is the
    more severe of the two and it names the file and key to edit, while the
    ratio only reports the consequence. Both findings are always reported in
    ``details`` regardless of which one sets the summary.

    This check never writes. Configs are read and parsed, never rewritten
    (renaming keys in a user's editor-managed config is the user's call, and
    ``kioku-mesh mcp install`` already does it deliberately), and the
    observation sample is read straight off the existing index through a
    read-only SQLite connection rather than through ``get_backend()``, which
    would create the state directory and index a fresh host does not have yet.
    Missing configs are normal — a host with neither client installed passes.

    ``config_paths`` and ``observations`` are injectable for tests; the
    defaults read the real client configs and the on-disk index.
    """
    paths = _default_identity_config_paths() if config_paths is None else config_paths

    legacy_hits: list[dict[str, Any]] = []
    inspected: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        data = _load_config_mapping(path)
        if data is None:
            # Unparseable config: recorded as inspected-but-skipped rather
            # than treated as clean, so the summary can't claim coverage it
            # doesn't have.
            continue
        inspected.append(str(path))
        keys = _collect_legacy_env_keys(data)
        if keys:
            legacy_hits.append({'path': str(path), 'keys': keys})

    # Unknown-dominance probe. Skipped (not failed) when there is no readable
    # index — a host that has never saved anything, or whose store is
    # unreachable, is not evidence of a broken identity.
    unknown_ratio: float | None = None
    sampled = 0
    families = (
        _read_recent_agent_families(_IDENTITY_SCAN_LIMIT)
        if observations is None
        else [str(getattr(o, 'agent_family', '') or '') for o in observations]
    )
    if families is not None:
        sampled = len(families)
        if sampled:
            unknown_ratio = _unknown_family_ratio(families)

    details: dict[str, Any] = {
        'legacy_env_prefix': _LEGACY_ENV_PREFIX,
        'legacy_identity_keys': list(_LEGACY_IDENTITY_ENV_KEYS),
        'legacy_hits': legacy_hits,
        'inspected_configs': inspected,
        'sampled_observations': sampled,
        'unknown_ratio': None if unknown_ratio is None else round(unknown_ratio, 3),
        'unknown_threshold': _UNKNOWN_DOMINANCE_RATIO,
    }

    if legacy_hits:
        where = ', '.join(f'{h["path"]} ({", ".join(h["keys"])})' for h in legacy_hits)
        return CheckResult(
            name='identity',
            status=CheckStatus.FAIL,
            summary=f'MCP config declares identity only via retired {_LEGACY_ENV_PREFIX}* env vars: {where}',
            hint=(
                f'These names are no longer read, so identity is unset. Rename them to '
                f'{" / ".join(_IDENTITY_ENV_KEYS)}, or re-register with '
                '`kioku-mesh mcp install`, then restart the MCP client.'
            ),
            details=details,
        )

    if unknown_ratio is not None and unknown_ratio >= _UNKNOWN_DOMINANCE_RATIO:
        return CheckResult(
            name='identity',
            status=CheckStatus.WARN,
            summary=(f'{unknown_ratio:.0%} of the last {sampled} observations have agent_family=unknown'),
            hint=(
                f'Set {" / ".join(_IDENTITY_ENV_KEYS)} in the MCP client env '
                '(or run `kioku-mesh mcp install`) and restart the client.'
            ),
            details=details,
        )

    if not inspected and unknown_ratio is None:
        return CheckResult(
            name='identity',
            status=CheckStatus.PASS,
            summary='identity: no MCP client config found to check',
            details=details,
        )

    return CheckResult(
        name='identity',
        status=CheckStatus.PASS,
        summary='identity: no retired env vars in MCP configs, recent saves are attributed',
        details=details,
    )


# -- Orchestration & rendering -------------------------------------------------


# The checks doctor runs, in the order it runs them. Held as function *names*
# rather than function objects for two reasons: a test can assert membership
# and ordering without executing anything (running the real checks just to
# read back their names would touch the developer's own ~/.config, TLS certs
# and memory store), and callers that patch a check on this module still see
# their replacement, since the lookup happens per call.
_CHECK_ORDER: tuple[str, ...] = (
    'check_zenohd_binary',
    'check_config_file',
    'check_zenohd_reachable',
    'check_state_dir_hardlinks',
    'check_embedded_router',
    'check_tls_certs',
    'check_fts5',
    'check_shadow_visibility',
    'check_conflicting_latest',
    'check_tool_call_fragments',
    'check_legacy_namespace',
    'check_storage_scopes',
    'check_identity',
)


def run_all_checks() -> list[CheckResult]:
    """Run every v0.3 doctor check in stable order.

    Order matters for the human-readable text output (most foundational
    failures first). JSON consumers should look at the per-check ``name``
    rather than relying on index.
    """
    checks: list[Callable[[], CheckResult]] = [globals()[name] for name in _CHECK_ORDER]
    return [check() for check in checks]


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

"""Storage-scope contract for kioku-mesh (ADR-0019 Phase E, design v3 task 1).

One host declares which visibility scopes it stores in a single host-global
list (``storage_scopes`` in ``~/.config/kioku-mesh/config.yaml``):

```yaml
storage_scopes:
  - mesh
  - user/hwata
  - team/sbgisen
```

Everything that needs "which scopes does this host hold, and what Zenoh
storage serves each of them" derives it here: the zenohd storage renderer
(task 3), the read-path selectors (task 2), and the save preflight below.

The save preflight is the write-side gate: a save may only publish a key
whose scope is (a) declared in ``storage_scopes`` and (b) actually served
by a matching storage in the **running local zenohd**, checked live via
the Zenoh admin space before ``session.put()`` / SQLite upsert /
pending-puts enqueue happen. The declaration alone is not evidence: a
config edit that was never rendered, or rendered but never restarted,
leaves saves with no durable home.

Two deliberate choices, both from the design review (worker3_design_review_404):

- **self-scoped admin selector (N1)**. ``@/*/router/...`` costs median
  8.0 ms / max 221.5 ms and blocks on unreachable peers; ``@/<self_zid>/...``
  costs median 0.1 ms. Only :func:`fetch_peer_storages` (doctor's
  diagnostic display) uses the wildcard.
- **no caching**. Config and admin state are re-read on every preflight
  so a long-lived MCP process notices a config edit, a renderer apply, or
  a zenohd restart immediately. At 0.1 ms there is nothing to cache.

Enforcement is staged behind ``KIOKU_MESH_SCOPE_ISOLATION`` (default
``off``, ADR-0019 Phase E): the preflight always runs and logs, but only
rejects when the flag is ``enforce``. Without the staging every existing
install — whose zenohd still has the single broad ``agent_mem``
(``mem/**``) storage — would fail every save the moment this lands, before
the storage cutover (task 3) has had a chance to run.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from typing import Any

from .config import get_storage_scopes
from .keyspace import validate_scope_slug

log = logging.getLogger(__name__)

SCOPE_ISOLATION_ENV = 'KIOKU_MESH_SCOPE_ISOLATION'

# Self-scoped admin get: cheap and independent of remote peer liveness (N1).
ADMIN_STORAGES_SUFFIX = 'router/status/plugins/storage_manager/storages/**'
ADMIN_GET_TIMEOUT = 2.0

_UNSCOPED_TIERS = ('mesh',)
_SCOPED_TIERS = ('user', 'team')


class ScopeConfigError(ValueError):
    """``storage_scopes`` is malformed — a config bug, not a runtime condition."""


class ScopePreflightError(RuntimeError):
    """A write was refused because no live storage can hold it durably.

    Deliberately not part of ``transport._RETRYABLE_EXC``: retrying does not
    make a missing storage appear, and the message tells the operator what to
    do instead.
    """


@dataclass(frozen=True)
class ScopeSpec:
    """One storage scope and everything derived from it.

    ``visibility`` / ``scope_id`` match the write-path pair resolved by
    ``config.resolve_write_visibility``; the rest is the storage and read
    contract the design's target-state table specifies.
    """

    visibility: str
    scope_id: str = ''

    @property
    def label(self) -> str:
        """Canonical ``storage_scopes`` spelling: ``mesh`` / ``user/hwata``."""
        return f'{self.visibility}/{self.scope_id}' if self.scope_id else self.visibility

    @property
    def key_prefix(self) -> str:
        """Key prefix owned by this scope (``mem/mesh``, ``mem/user/hwata``)."""
        return f'mem/{self.label}'

    @property
    def key_expr(self) -> str:
        """Zenoh storage key expression for this scope."""
        return f'{self.key_prefix}/**'

    @property
    def strip_prefix(self) -> str:
        """Storage strip prefix.

        ``mesh`` strips only ``mem`` so existing ``mem/mesh/...`` keys keep
        their on-disk ``mesh/...`` form (design v3 S1); scoped tiers strip
        their whole prefix so each scope gets its own flat DB namespace.
        """
        return 'mem' if self.visibility in _UNSCOPED_TIERS else self.key_prefix

    @property
    def volume_dir(self) -> str:
        """RocksDB directory name for this scope."""
        if self.visibility in _UNSCOPED_TIERS:
            return 'mesh'
        return f'{self.visibility}_{self.scope_id}'.replace('/', '_')

    @property
    def storage_name(self) -> str:
        """Return the zenohd storage id for this scope."""
        return f'{self.volume_dir}_store'

    @property
    def obs_read_key_expr(self) -> str:
        return f'{self.key_prefix}/obs/**'

    @property
    def tomb_read_key_expr(self) -> str:
        return f'{self.key_prefix}/tomb/**'


def parse_scope(raw: str) -> ScopeSpec:
    """Parse one ``storage_scopes`` entry; raise :class:`ScopeConfigError`.

    Accepts exactly ``mesh``, ``user/<slug>``, ``team/<slug>``. Legacy
    (un-tiered), wildcards, and extra segments are rejected rather than
    normalized — a typo must not silently widen what a host stores.
    """
    entry = (raw or '').strip().strip('/')
    if not entry:
        raise ScopeConfigError('storage_scopes contains an empty entry')
    if '*' in entry:
        raise ScopeConfigError(f'storage_scopes must not contain wildcards; got {entry!r}')
    parts = entry.split('/')
    tier = parts[0]
    if tier in _UNSCOPED_TIERS:
        if len(parts) != 1:
            raise ScopeConfigError(f"scope 'mesh' takes no scope id; got {entry!r}")
        return ScopeSpec(tier)
    if tier in _SCOPED_TIERS:
        if len(parts) != 2:
            raise ScopeConfigError(f'scope {tier!r} must be {tier}/<id>; got {entry!r}')
        try:
            validate_scope_slug(tier, parts[1])
        except ValueError as e:
            raise ScopeConfigError(f'invalid storage_scopes entry {entry!r}: {e}') from e
        return ScopeSpec(tier, parts[1])
    raise ScopeConfigError(f"storage_scopes entries must be 'mesh', 'user/<id>' or 'team/<id>'; got {entry!r}")


def resolve_storage_scopes(raw: list[str] | None = None) -> tuple[ScopeSpec, ...]:
    """Return this host's declared storage scopes, in declaration order.

    Reads ``storage_scopes`` from the host-global config on every call (no
    caching — see the module docstring). An absent key means the pre-Phase-E
    default of mesh only. ``mesh`` is mandatory: dropping it would strand
    the tier every host is expected to replicate.
    """
    entries = get_storage_scopes() if raw is None else raw
    if entries is None:
        return (ScopeSpec('mesh'),)
    if not isinstance(entries, list):
        raise ScopeConfigError(f'storage_scopes must be a list; got {type(entries).__name__}')
    if not entries:
        raise ScopeConfigError('storage_scopes must not be empty (at minimum: [mesh])')
    scopes: list[ScopeSpec] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise ScopeConfigError(f'storage_scopes entries must be strings; got {entry!r}')
        spec = parse_scope(entry)
        if spec.label in seen:
            raise ScopeConfigError(f'duplicate storage_scopes entry: {spec.label!r}')
        seen.add(spec.label)
        scopes.append(spec)
    if not any(s.visibility == 'mesh' for s in scopes):
        raise ScopeConfigError("storage_scopes must include 'mesh'")
    return tuple(scopes)


def scope_for_write(visibility: str, scope_id: str = '') -> ScopeSpec:
    """Return the scope a write with this ``(visibility, scope_id)`` lands in."""
    label = f'{visibility}/{scope_id}' if scope_id else visibility
    return parse_scope(label)


def scope_from_key(key_expr: str) -> ScopeSpec | None:
    """Return the scope owning ``key_expr``, or ``None`` for legacy/foreign keys.

    Used by the preflight so both a fresh save and a replayed pending-put
    row (which carries only the key) are classified the same way.
    """
    parts = (key_expr or '').split('/')
    if len(parts) < 3 or parts[0] != 'mem':
        return None
    try:
        if parts[1] in _UNSCOPED_TIERS:
            return parse_scope(parts[1])
        if parts[1] in _SCOPED_TIERS:
            return parse_scope(f'{parts[1]}/{parts[2]}')
    except ScopeConfigError:
        return None
    return None


def enforcement_enabled() -> bool:
    """Return True when ``KIOKU_MESH_SCOPE_ISOLATION=enforce`` (default: off)."""
    return os.environ.get(SCOPE_ISOLATION_ENV, '').strip().lower() == 'enforce'


# -- live storage inspection (Zenoh admin space) -------------------------------


@dataclass(frozen=True)
class LiveStorage:
    """One storage as the running zenohd reports it in the admin space.

    Note the fields: the admin space exposes ``key_expr``, ``strip_prefix``
    and ``volume`` only — **no ``replication`` block** (measured, N2). Any
    replication comparison must go against the rendered config file or the
    two-node harness, never against this.
    """

    name: str
    key_expr: str
    strip_prefix: str
    volume_dir: str
    zid: str = ''

    def covers(self, key: str) -> bool:
        """Return True when this storage's key expression includes ``key``."""
        import zenoh

        try:
            return zenoh.KeyExpr(self.key_expr).includes(zenoh.KeyExpr(key))
        except Exception:  # noqa: BLE001 — a malformed live key_expr is not coverage
            return False


def _parse_storage_reply(key: str, payload: str, zid: str = '') -> LiveStorage | None:
    try:
        data: Any = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    volume = data.get('volume')
    volume_dir = str(volume.get('dir', '')) if isinstance(volume, dict) else ''
    return LiveStorage(
        name=key.rsplit('/', 1)[-1],
        key_expr=str(data.get('key_expr', '')),
        strip_prefix=str(data.get('strip_prefix', '')),
        volume_dir=volume_dir,
        zid=zid,
    )


def self_router_zid(session: Any) -> str:
    """Return the ZID of the router this session is attached to.

    N6 caveat: when the local zenohd is down and ``ZENOH_CONNECT`` points
    elsewhere, this is a *remote* router — its storages are another host's.
    Always pair it with :func:`local_router_endpoint_ok`.
    """
    zids = list(session.info.routers_zid())
    if not zids:
        raise ScopePreflightError('no Zenoh router is attached to this session')
    return str(zids[0])


def local_router_endpoint_ok(endpoint: str | None = None) -> bool:
    """Return True when the configured Zenoh endpoint is this host's own router (N6).

    Checks ``ZENOH_CONNECT`` (the same value ``transport._open_session``
    dials) for a loopback host. A non-loopback endpoint means "self" as
    reported by the admin space may belong to another machine, so live
    storage there is not evidence of local durability.
    """
    raw = endpoint if endpoint is not None else os.environ.get('ZENOH_CONNECT', 'tcp/localhost:7447')
    host_port = raw.split('/', 1)[-1]
    host = host_port.rpartition(':')[0] or host_port
    return host.strip('[]') in ('localhost', '127.0.0.1', '::1', '')


def fetch_self_storages(session: Any, *, timeout: float = ADMIN_GET_TIMEOUT) -> list[LiveStorage]:
    """Read the live storage definitions of *this host's* router (N1: self-scoped)."""
    zid = self_router_zid(session)
    out: list[LiveStorage] = []
    for reply in session.get(f'@/{zid}/{ADMIN_STORAGES_SUFFIX}', timeout=timeout):
        sample = getattr(reply, 'ok', None)
        if sample is None:
            continue
        parsed = _parse_storage_reply(str(sample.key_expr), sample.payload.to_string(), zid)
        if parsed is not None:
            out.append(parsed)
    return out


def fetch_peer_storages(session: Any, *, timeout: float = ADMIN_GET_TIMEOUT) -> list[LiveStorage]:
    """Read storage definitions of every reachable router — doctor display only.

    The wildcard selector is the expensive one (median 8.0 ms, max 221.5 ms,
    blocks on unresponsive peers), so it must never sit on the save path.
    """
    out: list[LiveStorage] = []
    for reply in session.get(f'@/*/{ADMIN_STORAGES_SUFFIX}', timeout=timeout):
        sample = getattr(reply, 'ok', None)
        if sample is None:
            continue
        key = str(sample.key_expr)
        zid = key.split('/')[1] if key.startswith('@/') else ''
        parsed = _parse_storage_reply(key, sample.payload.to_string(), zid)
        if parsed is not None:
            out.append(parsed)
    return out


# -- write preflight -----------------------------------------------------------


@dataclass(frozen=True)
class PreflightVerdict:
    """Outcome of one write preflight."""

    ok: bool
    key_expr: str
    scope: str = ''
    reason: str = ''
    hint: str = ''

    @property
    def message(self) -> str:
        return f'save refused: {self.reason}' + (f' {self.hint}' if self.hint else '')


_DOCTOR_HINT = 'Run `kioku-mesh doctor` for the live diff.'


def evaluate_write_key(key_expr: str, session: Any | None) -> PreflightVerdict:
    """Decide whether ``key_expr`` has a durable home on this host, right now.

    ``session`` is passed in rather than resolved here so the caller's own
    (possibly stubbed) transport is the one inspected; ``None`` means the
    transport is down, which is itself a refusal — an unconfirmable write
    must not be accepted.

    Never raises: callers decide what a failure means (reject the save,
    keep a queued row, or report it in doctor).
    """
    scope = scope_from_key(key_expr)
    if scope is None:
        return PreflightVerdict(
            False,
            key_expr,
            reason=f'{key_expr} is not in a visibility-scoped namespace (mem/mesh, mem/user/<id>, mem/team/<id>)',
            hint='Legacy keys have no scope storage; run `kioku-mesh migrate-visibility` first.',
        )
    try:
        declared = resolve_storage_scopes()
    except ScopeConfigError as e:
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=f'storage_scopes is invalid: {e}',
            hint='Fix storage_scopes in ~/.config/kioku-mesh/config.yaml.',
        )
    if all(s.label != scope.label for s in declared):
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=f'write scope {scope.label} is not in storage_scopes ({", ".join(s.label for s in declared)})',
            hint=(
                f'Add {scope.label} to storage_scopes in ~/.config/kioku-mesh/config.yaml, run '
                '`kioku-mesh config render-storages --apply`, restart zenohd, then retry '
                f'(on every host joining {scope.label}). {_DOCTOR_HINT}'
            ),
        )
    if not local_router_endpoint_ok():
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=(
                'ZENOH_CONNECT does not point at a local router, so live storage reported as "self" '
                'may belong to another host'
            ),
            hint=f'Start the local zenohd and point ZENOH_CONNECT at it. {_DOCTOR_HINT}',
        )
    if session is None:
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason='no Zenoh session is available, so live storage cannot be confirmed',
            hint=f'Start or reconnect zenohd, then retry. {_DOCTOR_HINT}',
        )
    try:
        live = fetch_self_storages(session)
    except Exception as e:  # noqa: BLE001 — any admin failure is "cannot confirm durability"
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=f'cannot read the live storage list from zenohd ({type(e).__name__}: {e})',
            hint=f'Start or reconnect zenohd, then retry. {_DOCTOR_HINT}',
        )
    return _verdict_against_live(key_expr, scope, live)


def _verdict_against_live(key_expr: str, scope: ScopeSpec, live: list[LiveStorage]) -> PreflightVerdict:
    exact = [s for s in live if s.key_expr == scope.key_expr and s.strip_prefix == scope.strip_prefix]
    covering = [s for s in live if s.covers(key_expr)]
    if not exact:
        near = ', '.join(f'{s.name}({s.key_expr}, strip={s.strip_prefix})' for s in live) or 'none'
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=(
                f'live zenohd has no exact {scope.label} storage '
                f'(want key_expr={scope.key_expr}, strip_prefix={scope.strip_prefix}); live storages: {near}'
            ),
            hint=(f'Run `kioku-mesh config render-storages --apply` and restart zenohd. {_DOCTOR_HINT}'),
        )
    if not any(s.covers(key_expr) for s in exact):
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=f'the {scope.label} storage does not cover {key_expr}',
            hint=_DOCTOR_HINT,
        )
    broad = [s for s in covering if s.key_expr != scope.key_expr]
    if broad:
        names = ', '.join(f'{s.name}({s.key_expr})' for s in broad)
        return PreflightVerdict(
            False,
            key_expr,
            scope.label,
            reason=f'an overlapping broad storage would also receive this key: {names}',
            hint=(
                'Remove the broad storage (the pre-split agent_mem) with '
                f'`kioku-mesh config render-storages --apply` and restart zenohd. {_DOCTOR_HINT}'
            ),
        )
    return PreflightVerdict(True, key_expr, scope.label)


def preflight_write_key(key_expr: str, session: Any | None) -> PreflightVerdict:
    """Gate a write on live storage coverage; raise when enforcing.

    Called before ``session.put()``, the SQLite upsert, and the pending-puts
    enqueue, so a refused save leaves no trace anywhere — that is the point
    of fail-closed: never show a save as accepted when the mesh has nowhere
    to keep it.

    With ``KIOKU_MESH_SCOPE_ISOLATION`` unset (the staged default) a failed
    verdict is logged and the write proceeds, so hosts that have not run the
    storage cutover yet keep working.
    """
    verdict = evaluate_write_key(key_expr, session)
    if verdict.ok:
        return verdict
    if enforcement_enabled():
        raise ScopePreflightError(verdict.message)
    log.warning('%s (allowed: %s is not "enforce")', verdict.message, SCOPE_ISOLATION_ENV)
    return verdict

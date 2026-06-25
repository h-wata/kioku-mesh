"""Presence heartbeat for kioku-mesh messaging (Phase 2 — ADR-0022).

Publishes session presence to ``msg/{scope}/presence/{agent_id}/{session_id}``
every 30 seconds with a 90-second TTL.

Scope publication policy:
  - user:  published when ``user_id`` is configured (KIOKU_MESH_USER_ID or config.yaml)
  - team:  published when ``team_id`` is configured
  - mesh:  off by default; opt-in via KIOKU_MESH_MESSAGING_PRESENCE_MESH=1

messaging モジュールは memory モジュールを直接 import しない (ADR-0023).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
import os
import socket
import threading
from typing import Any

from kioku_mesh.core._env_compat import get_env

from ..core.config import get_team_id
from ..core.config import get_user_id
from ..core.identity import get_agent_family
from ..core.identity import get_client_id
from ..core.identity import get_session_id
from ..core.transport import get_session as _get_zenoh_session

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30  # seconds between publishes
PRESENCE_TTL = 90  # seconds — active if last_seen is within this window


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _presence_key(scope: str, agent_id: str, session_id: str) -> str:
    """Build the Zenoh key for a presence entry."""
    return f'msg/{scope}/presence/{agent_id}/{session_id}'


def _publication_scopes() -> list[str]:
    """Return the scopes to which this host should publish presence."""
    scopes: list[str] = []
    user_id = get_user_id()
    if user_id:
        scopes.append(f'user/{user_id}')
    team_id = get_team_id()
    if team_id:
        scopes.append(f'team/{team_id}')
    if get_env('KIOKU_MESH_MESSAGING_PRESENCE_MESH') == '1':
        scopes.append('mesh')
    return scopes


@dataclass
class Presence:
    """A single peer's presence record parsed from Zenoh."""

    agent_id: str
    session_id: str
    host: str
    last_seen: datetime
    capabilities: list[str] = field(default_factory=list)
    delivery_adapters: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)

    def is_active(self, now: datetime | None = None) -> bool:
        """Return True if last_seen is within PRESENCE_TTL seconds."""
        effective_now = now if now is not None else _utc_now()
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
        last = self.last_seen
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (effective_now - last).total_seconds() <= PRESENCE_TTL

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the presence payload dict (design memo schema_version=1)."""
        last_seen_utc = self.last_seen
        if last_seen_utc.tzinfo is None:
            last_seen_utc = last_seen_utc.replace(tzinfo=timezone.utc)
        expires_at = last_seen_utc + timedelta(seconds=PRESENCE_TTL)
        return {
            'schema_version': 1,
            'agent_id': self.agent_id,
            'agent_family': get_agent_family(),
            'client_id': get_client_id(),
            'session_id': self.session_id,
            'host': self.host,
            'pid': os.getpid(),
            'last_seen': last_seen_utc.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'expires_at': expires_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'ttl_sec': PRESENCE_TTL,
            'capabilities': self.capabilities,
            'delivery_adapters': self.delivery_adapters,
            'scopes': list(self.scopes),
        }


def _parse_presence(data: dict[str, Any]) -> Presence | None:
    """Parse a raw presence dict; return None on missing required fields."""
    try:
        last_seen_str = data.get('last_seen', '')
        if not last_seen_str:
            return None
        last_seen = datetime.fromisoformat(last_seen_str.replace('Z', '+00:00'))
        return Presence(
            agent_id=data.get('agent_id', ''),
            session_id=data.get('session_id', ''),
            host=data.get('host', ''),
            last_seen=last_seen,
            capabilities=list(data.get('capabilities', [])),
            delivery_adapters=list(data.get('delivery_adapters', [])),
        )
    except Exception:  # noqa: BLE001
        return None


class PresenceManager:
    """Manages the presence heartbeat loop and peer discovery queries.

    Uses an asyncio event loop running in a daemon thread for the heartbeat
    so the calling code stays synchronous.
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._agent_id = get_client_id()
        self._session_id = get_session_id()
        self._host = socket.gethostname()

    def _build_presence(self) -> Presence:
        return Presence(
            agent_id=self._agent_id,
            session_id=self._session_id,
            host=self._host,
            last_seen=_utc_now(),
            capabilities=['mcp_poll', 'ack'],
            delivery_adapters=['mcp'],
        )

    def _publish_once(self) -> None:
        """Publish current presence to all configured scopes."""
        scopes = _publication_scopes()
        if not scopes:
            return
        presence = self._build_presence()
        presence.scopes = list(scopes)
        payload = json.dumps(presence.to_dict()).encode('utf-8')
        try:
            session = _get_zenoh_session()
            for scope in scopes:
                key = _presence_key(scope, self._agent_id, self._session_id)
                session.put(key, payload)
        except Exception as e:  # noqa: BLE001
            log.warning('presence heartbeat publish failed: %s', e)

    def start_heartbeat(self) -> None:
        """Start the asyncio-based heartbeat in a background daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        stop_event = self._stop_event

        async def _loop() -> None:
            while not stop_event.is_set():
                self._publish_once()
                # Sleep HEARTBEAT_INTERVAL seconds, waking every second to check stop.
                for _ in range(HEARTBEAT_INTERVAL):
                    if stop_event.is_set():
                        return
                    await asyncio.sleep(1.0)

        def _run() -> None:
            asyncio.run(_loop())

        self._thread = threading.Thread(target=_run, daemon=True, name='presence-heartbeat')
        self._thread.start()

    def stop(self) -> None:
        """Signal the heartbeat loop to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=HEARTBEAT_INTERVAL + 2)
            self._thread = None

    def list_active_peers(self, scope: str) -> list[Presence]:
        """Return active peers for the given scope by querying Zenoh.

        Only returns peers whose ``last_seen`` is within ``PRESENCE_TTL`` seconds.
        Peers in other scopes are not visible here — the caller must pass the
        correct scope string (e.g. ``'team/kioku-mesh'`` or ``'user/hwata'``).
        """
        key_expr = f'msg/{scope}/presence/**'
        peers: list[Presence] = []
        try:
            session = _get_zenoh_session()
            for reply in session.get(key_expr, timeout=2.0):
                if not reply.ok:
                    continue
                try:
                    data: dict[str, Any] = json.loads(reply.ok.payload.to_bytes())
                    p = _parse_presence(data)
                    if p is not None and p.is_active():
                        peers.append(p)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            log.warning('list_active_peers failed for scope %r: %s', scope, e)
        return peers

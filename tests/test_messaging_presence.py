"""Tests for messaging presence heartbeat and peer discovery (Phase 2)."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import time
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from kioku_mesh.messaging.presence import _parse_presence
from kioku_mesh.messaging.presence import _presence_key
from kioku_mesh.messaging.presence import _publication_scopes
from kioku_mesh.messaging.presence import Presence
from kioku_mesh.messaging.presence import PRESENCE_TTL
from kioku_mesh.messaging.presence import PresenceManager


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past(seconds: int) -> datetime:
    return _now() - timedelta(seconds=seconds)


def _future(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Presence dataclass
# ---------------------------------------------------------------------------


class TestPresenceIsActive:
    def test_recent_last_seen_is_active(self) -> None:
        p = Presence(
            agent_id='a',
            session_id='s',
            host='h',
            last_seen=_past(10),
        )
        assert p.is_active()

    def test_exactly_at_ttl_is_not_active(self) -> None:
        p = Presence(
            agent_id='a',
            session_id='s',
            host='h',
            last_seen=_past(PRESENCE_TTL),
        )
        assert not p.is_active()

    def test_expired_is_not_active(self) -> None:
        p = Presence(
            agent_id='a',
            session_id='s',
            host='h',
            last_seen=_past(PRESENCE_TTL + 5),
        )
        assert not p.is_active()

    def test_custom_now_param(self) -> None:
        last = _now() - timedelta(seconds=50)
        p = Presence(agent_id='a', session_id='s', host='h', last_seen=last)
        now_near = last + timedelta(seconds=40)
        assert p.is_active(now=now_near)
        now_far = last + timedelta(seconds=91)
        assert not p.is_active(now=now_far)


class TestPresenceToDict:
    def test_required_fields_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('KIOKU_MESH_USER_ID', 'testuser')
        with (
            patch('kioku_mesh.messaging.presence.get_agent_family', return_value='claude'),
            patch('kioku_mesh.messaging.presence.get_client_id', return_value='claude-code'),
        ):
            p = Presence(
                agent_id='claude-code',
                session_id='20260624T000000Z-abc1',
                host='devbox',
                last_seen=_now(),
                capabilities=['mcp_poll'],
                delivery_adapters=['mcp'],
            )
            d = p.to_dict()
        assert d['schema_version'] == 1
        assert d['agent_id'] == 'claude-code'
        assert d['session_id'] == '20260624T000000Z-abc1'
        assert d['ttl_sec'] == PRESENCE_TTL
        assert 'last_seen' in d
        assert 'expires_at' in d

    def test_expires_at_is_last_seen_plus_ttl(self) -> None:
        last = _now()
        p = Presence(agent_id='a', session_id='s', host='h', last_seen=last)
        with (
            patch('kioku_mesh.messaging.presence.get_agent_family', return_value='unknown'),
            patch('kioku_mesh.messaging.presence.get_client_id', return_value='a'),
        ):
            d = p.to_dict()
        last_seen_dt = datetime.fromisoformat(d['last_seen'].replace('Z', '+00:00'))
        expires_dt = datetime.fromisoformat(d['expires_at'].replace('Z', '+00:00'))
        delta = (expires_dt - last_seen_dt).total_seconds()
        assert abs(delta - PRESENCE_TTL) < 1


# ---------------------------------------------------------------------------
# Key builder
# ---------------------------------------------------------------------------


class TestPresenceKey:
    def test_mesh_scope(self) -> None:
        assert _presence_key('mesh', 'agent1', 'sess1') == 'msg/mesh/presence/agent1/sess1'

    def test_user_scope(self) -> None:
        assert _presence_key('user/hwata', 'agent1', 'sess1') == 'msg/user/hwata/presence/agent1/sess1'

    def test_team_scope(self) -> None:
        assert _presence_key('team/kioku-mesh', 'a', 's') == 'msg/team/kioku-mesh/presence/a/s'


# ---------------------------------------------------------------------------
# Publication scopes
# ---------------------------------------------------------------------------


class TestPublicationScopes:
    def test_no_config_no_scopes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_MESSAGING_PRESENCE_MESH', raising=False)
        scopes = _publication_scopes()
        assert scopes == []

    def test_user_scope_when_user_id_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('KIOKU_MESH_USER_ID', 'hwata')
        monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_MESSAGING_PRESENCE_MESH', raising=False)
        assert 'user/hwata' in _publication_scopes()

    def test_team_scope_when_team_id_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('KIOKU_MESH_TEAM_ID', 'kioku-mesh')
        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_MESSAGING_PRESENCE_MESH', raising=False)
        assert 'team/kioku-mesh' in _publication_scopes()

    def test_mesh_scope_requires_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
        monkeypatch.setenv('KIOKU_MESH_MESSAGING_PRESENCE_MESH', '1')
        assert 'mesh' in _publication_scopes()

    def test_mesh_scope_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_MESSAGING_PRESENCE_MESH', raising=False)
        assert 'mesh' not in _publication_scopes()

    def test_user_scope_not_visible_in_team_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('KIOKU_MESH_USER_ID', 'alice')
        monkeypatch.setenv('KIOKU_MESH_TEAM_ID', 'acme')
        monkeypatch.delenv('KIOKU_MESH_MESSAGING_PRESENCE_MESH', raising=False)
        scopes = _publication_scopes()
        assert 'user/alice' in scopes
        assert 'team/acme' in scopes
        # Scopes are distinct — user scope entry must not appear in team scope
        assert 'user/alice' not in [s for s in scopes if s.startswith('team/')]


# ---------------------------------------------------------------------------
# _parse_presence
# ---------------------------------------------------------------------------


class TestParsePresence:
    def test_valid_dict(self) -> None:
        data: dict[str, Any] = {
            'agent_id': 'codex-cli',
            'session_id': '20260624T010203Z-a1b2',
            'host': 'devbox',
            'last_seen': '2026-06-24T01:02:03.000000Z',
            'capabilities': ['mcp_poll'],
            'delivery_adapters': ['mcp'],
        }
        p = _parse_presence(data)
        assert p is not None
        assert p.agent_id == 'codex-cli'
        assert p.session_id == '20260624T010203Z-a1b2'

    def test_missing_last_seen_returns_none(self) -> None:
        assert _parse_presence({'agent_id': 'a', 'session_id': 's', 'host': 'h'}) is None

    def test_invalid_dict_returns_none(self) -> None:
        assert _parse_presence({'last_seen': 'not-a-date'}) is None


# ---------------------------------------------------------------------------
# PresenceManager heartbeat
# ---------------------------------------------------------------------------


class TestPresenceManagerHeartbeat:
    def test_start_heartbeat_publishes_to_zenoh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """start_heartbeat publishes at least one presence entry within 2 seconds."""
        monkeypatch.setenv('KIOKU_MESH_USER_ID', 'testuser')
        mock_session = MagicMock()
        published_keys: list[str] = []

        def _capture_put(key: str, payload: Any) -> None:
            published_keys.append(key)

        mock_session.put.side_effect = _capture_put

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            mgr.start_heartbeat()
            time.sleep(0.3)
            mgr.stop()

        assert any('presence' in k for k in published_keys), f'no presence key published; got {published_keys}'

    def test_stop_terminates_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        monkeypatch.delenv('KIOKU_MESH_TEAM_ID', raising=False)
        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=MagicMock()):
            mgr = PresenceManager()
            mgr.start_heartbeat()
            assert mgr._thread is not None and mgr._thread.is_alive()
            mgr.stop()
            assert mgr._thread is None

    def test_start_heartbeat_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling start_heartbeat twice does not start a second thread."""
        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=MagicMock()):
            mgr = PresenceManager()
            mgr.start_heartbeat()
            thread1 = mgr._thread
            mgr.start_heartbeat()
            assert mgr._thread is thread1
            mgr.stop()

    def test_publish_once_puts_per_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('KIOKU_MESH_USER_ID', 'u1')
        monkeypatch.setenv('KIOKU_MESH_TEAM_ID', 't1')
        mock_session = MagicMock()
        put_calls: list[str] = []
        mock_session.put.side_effect = lambda k, _: put_calls.append(k)

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            mgr._publish_once()

        user_keys = [k for k in put_calls if 'user/u1' in k]
        team_keys = [k for k in put_calls if 'team/t1' in k]
        assert len(user_keys) == 1
        assert len(team_keys) == 1


# ---------------------------------------------------------------------------
# PresenceManager.list_active_peers  — scope isolation
# ---------------------------------------------------------------------------


class TestListActivePeers:
    def _make_reply(self, data: dict[str, Any]) -> MagicMock:
        reply = MagicMock()
        reply.ok = MagicMock()
        reply.ok.payload.to_bytes.return_value = json.dumps(data).encode()
        return reply

    def _active_peer_data(self, agent_id: str = 'peer1', session_id: str = 's1') -> dict[str, Any]:
        return {
            'agent_id': agent_id,
            'session_id': session_id,
            'host': 'host1',
            'last_seen': _now().strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'capabilities': [],
            'delivery_adapters': [],
        }

    def test_returns_active_peers_for_scope(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = [self._make_reply(self._active_peer_data())]

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            peers = mgr.list_active_peers('team/kioku-mesh')

        assert len(peers) == 1
        assert peers[0].agent_id == 'peer1'
        # Ensure only the requested scope was queried
        mock_session.get.assert_called_once_with('msg/team/kioku-mesh/presence/**', timeout=2.0)

    def test_user_scope_query_does_not_include_team_scope(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = []

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            mgr.list_active_peers('user/hwata')

        mock_session.get.assert_called_once_with('msg/user/hwata/presence/**', timeout=2.0)

    def test_expired_peer_filtered_out(self) -> None:
        expired_data = {
            'agent_id': 'old-peer',
            'session_id': 's2',
            'host': 'h2',
            'last_seen': _past(PRESENCE_TTL + 10).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'capabilities': [],
            'delivery_adapters': [],
        }
        mock_session = MagicMock()
        mock_session.get.return_value = [self._make_reply(expired_data)]

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            peers = mgr.list_active_peers('mesh')

        assert peers == []

    def test_zenoh_error_returns_empty_list(self) -> None:
        mock_session = MagicMock()
        mock_session.get.side_effect = RuntimeError('zenoh down')

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            peers = mgr.list_active_peers('mesh')

        assert peers == []


# ---------------------------------------------------------------------------
# C2: scopes field in Presence.to_dict()
# ---------------------------------------------------------------------------


class TestPresencePayloadScopes:
    def test_to_dict_includes_scopes_field(self) -> None:
        p = Presence(agent_id='a', session_id='s', host='h', last_seen=_now())
        with (
            patch('kioku_mesh.messaging.presence.get_agent_family', return_value='claude'),
            patch('kioku_mesh.messaging.presence.get_client_id', return_value='a'),
        ):
            d = p.to_dict()
        assert 'scopes' in d
        assert isinstance(d['scopes'], list)

    def test_scopes_field_reflects_assigned_scopes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('KIOKU_MESH_USER_ID', 'hwata')
        monkeypatch.setenv('KIOKU_MESH_TEAM_ID', 'kioku')
        put_calls: list[dict] = []

        mock_session = MagicMock()

        def _capture(key: str, payload: Any) -> None:
            put_calls.append({'key': key, 'payload': json.loads(payload)})

        mock_session.put.side_effect = _capture

        with patch('kioku_mesh.messaging.presence._get_zenoh_session', return_value=mock_session):
            mgr = PresenceManager()
            mgr._publish_once()

        assert len(put_calls) >= 1
        for call in put_calls:
            scopes = call['payload'].get('scopes', None)
            assert scopes is not None, 'scopes field missing from presence payload'
            assert isinstance(scopes, list)
            assert len(scopes) > 0, 'scopes field should not be empty when user/team configured'

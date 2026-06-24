"""Unit tests for mesh_mem.messaging.keyspace (Phase 1)."""

from __future__ import annotations

from mesh_mem.messaging.keyspace import ack_key
from mesh_mem.messaging.keyspace import agent_inbox_key
from mesh_mem.messaging.keyspace import mesh_broadcast_key
from mesh_mem.messaging.keyspace import parse_scope_from_key
from mesh_mem.messaging.keyspace import session_inbox_key
from mesh_mem.messaging.keyspace import team_key
from mesh_mem.messaging.keyspace import user_key


class TestMeshBroadcastKey:
    def test_contains_msg_id(self) -> None:
        key = mesh_broadcast_key('abc123')
        assert key == 'msg/mesh/abc123'

    def test_prefix(self) -> None:
        key = mesh_broadcast_key('x')
        assert key.startswith('msg/mesh/')


class TestScopedKeys:
    def test_team_key(self) -> None:
        assert team_key('kioku-mesh', 'msg001') == 'msg/team/kioku-mesh/msg001'

    def test_user_key(self) -> None:
        assert user_key('hwata', 'msg002') == 'msg/user/hwata/msg002'

    def test_team_key_with_hyphen_in_team_id(self) -> None:
        assert team_key('my-team-x', 'id1') == 'msg/team/my-team-x/id1'

    def test_user_key_with_alphanumeric_user_id(self) -> None:
        assert user_key('user42', 'id2') == 'msg/user/user42/id2'


class TestInboxKeys:
    def test_session_inbox_key_mesh_scope(self) -> None:
        key = session_inbox_key('mesh', 'sess-abc', 'msg003')
        assert key == 'msg/mesh/inbox/session/sess-abc/msg003'

    def test_session_inbox_key_team_scope(self) -> None:
        key = session_inbox_key('team/kioku-mesh', 'sess-abc', 'msg003')
        assert key == 'msg/team/kioku-mesh/inbox/session/sess-abc/msg003'

    def test_session_inbox_key_user_scope(self) -> None:
        key = session_inbox_key('user/hwata', 'sess-abc', 'msg003')
        assert key == 'msg/user/hwata/inbox/session/sess-abc/msg003'

    def test_agent_inbox_key_mesh_scope(self) -> None:
        key = agent_inbox_key('mesh', 'agent-x', 'msg004')
        assert key == 'msg/mesh/inbox/agent/agent-x/msg004'

    def test_agent_inbox_key_team_scope(self) -> None:
        key = agent_inbox_key('team/kioku-mesh', 'agent-x', 'msg004')
        assert key == 'msg/team/kioku-mesh/inbox/agent/agent-x/msg004'

    def test_inbox_key_starts_with_msg_scope(self) -> None:
        key = session_inbox_key('mesh', 'sess', 'mid')
        assert key.startswith('msg/mesh/inbox/session/')


class TestAckKey:
    def test_mesh_scope(self) -> None:
        assert ack_key('mesh', 'msg005', 'sess-y') == 'msg/mesh/ack/msg005/sess-y'

    def test_team_scope(self) -> None:
        assert ack_key('team/kioku-mesh', 'msg006', 'sess-z') == 'msg/team/kioku-mesh/ack/msg006/sess-z'

    def test_user_scope(self) -> None:
        assert ack_key('user/hwata', 'msg007', 'sess-q') == 'msg/user/hwata/ack/msg007/sess-q'


class TestParseScopeFromKey:
    def test_mesh(self) -> None:
        key = mesh_broadcast_key('msg001')
        assert parse_scope_from_key(key) == 'mesh'

    def test_team(self) -> None:
        key = team_key('kioku-mesh', 'msg002')
        assert parse_scope_from_key(key) == 'team/kioku-mesh'

    def test_user(self) -> None:
        key = user_key('hwata', 'msg003')
        assert parse_scope_from_key(key) == 'user/hwata'

    def test_ack_key_mesh(self) -> None:
        # ack keys start with msg/mesh/ → scope is 'mesh'
        key = ack_key('mesh', 'msg001', 'sess')
        assert parse_scope_from_key(key) == 'mesh'

    def test_ack_key_team(self) -> None:
        key = ack_key('team/kioku-mesh', 'msg001', 'sess')
        assert parse_scope_from_key(key) == 'team/kioku-mesh'

    def test_unknown_key_returns_none(self) -> None:
        assert parse_scope_from_key('some/random/key') is None

    def test_inbox_key_returns_scope(self) -> None:
        # B1: inbox keys are now under msg/{scope}/inbox/... so parse_scope_from_key returns the scope
        assert parse_scope_from_key(session_inbox_key('mesh', 'sess', 'mid')) == 'mesh'
        assert parse_scope_from_key(session_inbox_key('team/kioku-mesh', 'sess', 'mid')) == 'team/kioku-mesh'
        assert parse_scope_from_key(agent_inbox_key('user/hwata', 'agent-x', 'mid')) == 'user/hwata'

    def test_empty_string_returns_none(self) -> None:
        assert parse_scope_from_key('') is None

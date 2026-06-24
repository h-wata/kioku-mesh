"""Zenoh key builder and parser for the messaging namespace (ADR-0022, Phase 1).

Key shapes (Phase 1):
  msg/mesh/{msg_id}
  msg/team/{team_id}/{msg_id}
  msg/user/{user_id}/{msg_id}
  inbox/session/{session_id}/{msg_id}
  inbox/agent/{agent_id}/{msg_id}
  msg/{scope}/ack/{msg_id}/{recipient_session_id}

messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

import re


def mesh_broadcast_key(msg_id: str) -> str:
    """Key for a mesh-scoped broadcast message."""
    return f'msg/mesh/{msg_id}'


def team_key(team_id: str, msg_id: str) -> str:
    """Key for a team-scoped message."""
    return f'msg/team/{team_id}/{msg_id}'


def user_key(user_id: str, msg_id: str) -> str:
    """Key for a user-scoped message."""
    return f'msg/user/{user_id}/{msg_id}'


def session_inbox_key(session_id: str, msg_id: str) -> str:
    """Key for a session-targeted inbox entry."""
    return f'inbox/session/{session_id}/{msg_id}'


def agent_inbox_key(agent_id: str, msg_id: str) -> str:
    """Key for an agent-targeted inbox entry."""
    return f'inbox/agent/{agent_id}/{msg_id}'


def ack_key(scope: str, msg_id: str, recipient_session_id: str) -> str:
    """Explicit ack key written by the recipient after processing a message."""
    return f'msg/{scope}/ack/{msg_id}/{recipient_session_id}'


_SCOPE_RE_TEAM = re.compile(r'^msg/(team/[^/]+)/')
_SCOPE_RE_USER = re.compile(r'^msg/(user/[^/]+)/')


def parse_scope_from_key(key: str) -> str | None:
    """Extract the scope segment from a msg/** key.

    Returns one of "mesh", "team/{team_id}", "user/{user_id}",
    or None if the key does not match a known msg/** pattern.
    """
    if key.startswith('msg/mesh/'):
        return 'mesh'
    m = _SCOPE_RE_TEAM.match(key)
    if m:
        return m.group(1)
    m = _SCOPE_RE_USER.match(key)
    if m:
        return m.group(1)
    return None

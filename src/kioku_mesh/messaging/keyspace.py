"""Zenoh key builder and parser for the messaging namespace (ADR-0022, Phase 1).

Key shapes (Phase 1):
  msg/mesh/{msg_id}                                         — mesh-scoped broadcast body
  msg/team/{team_id}/{msg_id}                               — team-scoped message body
  msg/user/{user_id}/{msg_id}                               — user-scoped message body
  msg/{scope}/inbox/session/{recipient_session_id}/{msg_id} — direct inbox for a session
  msg/{scope}/inbox/agent/{recipient_agent_id}/{msg_id}     — direct inbox for an agent
  msg/{scope}/ack/{msg_id}/{recipient_session_id}           — explicit ack written by recipient

messaging モジュールは memory モジュールを直接 import しない (ADR-0023)。
"""

from __future__ import annotations

import re


# TODO(Phase 2): broadcast key is a future extension; not used by direct delivery in Phase 1
def mesh_broadcast_key(msg_id: str) -> str:
    """Key for a mesh-scoped broadcast message body (Phase 2+)."""
    return f'msg/mesh/{msg_id}'


def team_key(team_id: str, msg_id: str) -> str:
    """Key for a team-scoped message body."""
    return f'msg/team/{team_id}/{msg_id}'


def user_key(user_id: str, msg_id: str) -> str:
    """Key for a user-scoped message body."""
    return f'msg/user/{user_id}/{msg_id}'


def session_inbox_key(scope: str, recipient_session_id: str, msg_id: str) -> str:
    """Key for a session-targeted direct inbox entry.

    Follows the design memo pattern: msg/{scope}/inbox/session/{recipient_session_id}/{msg_id}
    so Phase 2 can use the selector msg/**/inbox/session/{current_session_id}/** to receive.
    """
    return f'msg/{scope}/inbox/session/{recipient_session_id}/{msg_id}'


def agent_inbox_key(scope: str, recipient_agent_id: str, msg_id: str) -> str:
    """Key for an agent-targeted direct inbox entry.

    Follows the design memo pattern: msg/{scope}/inbox/agent/{recipient_agent_id}/{msg_id}
    so Phase 2 can use the selector msg/**/inbox/agent/{current_agent_id}/** to receive.
    """
    return f'msg/{scope}/inbox/agent/{recipient_agent_id}/{msg_id}'


def ack_key(scope: str, msg_id: str, recipient_session_id: str) -> str:
    """Explicit ack key written by the recipient after processing a message."""
    return f'msg/{scope}/ack/{msg_id}/{recipient_session_id}'


_SCOPE_RE_TEAM = re.compile(r'^msg/(team/[^/]+)/')
_SCOPE_RE_USER = re.compile(r'^msg/(user/[^/]+)/')


def parse_scope_from_key(key: str) -> str | None:
    """Extract the scope segment from a msg/** key.

    Returns one of "mesh", "team/{team_id}", "user/{user_id}",
    or None if the key does not match a known msg/** pattern.

    Works for message body keys, inbox keys (msg/{scope}/inbox/...),
    and ack keys (msg/{scope}/ack/...).
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

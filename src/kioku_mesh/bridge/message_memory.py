"""MessageMemoryBridge — messaging-to-memory promotion bridge (Phase 4, ADR-0022, ADR-0023).

Bridge is the only layer allowed to import both messaging and memory.
messaging layer must NOT import memory directly (ADR-0023 layering rule).

Promotion is opt-in: only messages with metadata.promote_hint == True or
an explicit promote() call result in a save_observation call.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class SaveObservationCallable(Protocol):
    """Protocol for the memory-layer save function injected into the bridge."""

    def __call__(
        self,
        content: str,
        project: str = ...,
        tags: list[str] | None = ...,
        memory_type: str = ...,
        importance: int = ...,
        subject: str = ...,
        summary: str = ...,
        source_files: list[str] | None = ...,
        references: list[str] | None = ...,
        supersedes: list[str] | None = ...,
        visibility: str = ...,
    ) -> str: ...


def _importance_from_message(msg: Any) -> int:
    """Map message priority to observation importance.

    high  → 3, normal/other → 2, low → 1.
    """
    priority = getattr(msg, 'payload', {}).get('priority') or ''
    if not priority and isinstance(getattr(msg, 'body', None), dict):
        priority = msg.body.get('priority', '')
    if priority == 'high':
        return 3
    if priority == 'low':
        return 1
    return 2


def _build_content(msg: Any) -> str:
    """Build observation content from a message."""
    body = getattr(msg, 'body', None)
    if isinstance(body, str) and body:
        return body
    if isinstance(body, dict):
        return body.get('text', '') or str(body)
    # Fallback to legacy payload field
    payload = getattr(msg, 'payload', {})
    if isinstance(payload, dict):
        return payload.get('text', '') or str(payload)
    return str(msg)


def _build_references(msg: Any) -> list[str]:
    """Collect msg_id and correlation_id as references."""
    refs: list[str] = []
    msg_id = getattr(msg, 'msg_id', None)
    if msg_id:
        refs.append(f'msg_id:{msg_id}')
    corr = getattr(msg, 'correlation_id', None)
    if corr:
        refs.append(f'correlation_id:{corr}')
    return refs


def _build_provenance_tags(msg: Any) -> list[str]:
    """Build tags carrying sender/scope provenance."""
    tags: list[str] = ['messaging-promoted']
    sender_id = getattr(msg, 'sender_id', None)
    if sender_id:
        tags.append(f'sender:{sender_id}')
    scope = getattr(msg, 'scope', None)
    if scope:
        tags.append(f'scope:{scope}')
    return tags


def _requires_promotion(msg: Any) -> bool:
    """Return True when the message opts-in to promotion.

    Promotion is opt-in (default off) per design memo Phase 4:
    - _extras['metadata']['promote_hint'] == True (full JSON schema path), OR
    - payload['promote_hint'] == True (convenience shorthand)
    """
    # Full schema path: metadata lives in _extras (unknown fields bucket)
    extras = getattr(msg, '_extras', None)
    if isinstance(extras, dict):
        metadata = extras.get('metadata', {})
        if isinstance(metadata, dict) and metadata.get('promote_hint') is True:
            return True
    # Convenience shorthand via payload dict
    payload = getattr(msg, 'payload', None)
    if isinstance(payload, dict) and payload.get('promote_hint') is True:
        return True
    return False


def _validate_message(msg: Any) -> None:
    """Raise ValueError if required Message fields are missing."""
    if not getattr(msg, 'msg_id', None):
        raise ValueError('Message.msg_id is required for promotion')
    if not getattr(msg, 'sender_id', None):
        raise ValueError('Message.sender_id is required for promotion')
    if not getattr(msg, 'scope', None):
        raise ValueError('Message.scope is required for promotion')


class MessageMemoryBridge:
    """Promotes selected messages from the messaging layer to memory (save_observation).

    Args:
        save_fn: Callable with the save_observation signature; injected for testability.
            Pass kioku_mesh.mcp_server.save_observation`` or a compatible callable.
    """

    def __init__(self, save_fn: Callable[..., str]) -> None:
        self._save_fn = save_fn
        self._promoted_ids: set[str] = set()

    def promote(self, msg: Any, *, force: bool = False) -> bool:
        """Promote msg to memory via save_observation if promotion criteria are met.

        Args:
            msg: A kioku_mesh.messaging.models.Message`` instance (or compatible object).
            force: If True, promote unconditionally (bypasses promote_hint check).
                   Still respects idempotency — duplicate msg_id is a no-op.

        Returns:
            True if save_observation was called, False otherwise.

        Raises:
            ValueError: If required fields (msg_id, sender_id, scope) are absent.
        """
        _validate_message(msg)

        msg_id: str = msg.msg_id

        # Idempotency guard: same msg_id promoted only once per bridge instance
        if msg_id in self._promoted_ids:
            return False

        if not force and not _requires_promotion(msg):
            return False

        content = _build_content(msg)
        subject = ''
        if isinstance(getattr(msg, 'payload', None), dict):
            subject = msg.payload.get('subject', '')
        if not subject and isinstance(getattr(msg, 'body', None), dict):
            subject = msg.body.get('subject', '')

        importance = _importance_from_message(msg)
        references = _build_references(msg)
        tags = _build_provenance_tags(msg)

        # Derive visibility from message scope (best-effort mapping)
        scope: str = msg.scope or ''
        if scope.startswith('team/'):
            visibility = 'team'
        elif scope.startswith('user/'):
            visibility = 'user'
        elif scope == 'mesh':
            visibility = 'mesh'
        else:
            visibility = ''

        # created_at → summary prefix for traceability
        created_at = getattr(msg, 'created_at', None)
        created_str = ''
        if created_at is not None:
            if hasattr(created_at, 'astimezone'):
                created_str = created_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            else:
                created_str = str(created_at)
        summary = f'[promoted from msg:{msg_id[:8]}] {created_str}'.strip()

        self._save_fn(
            content=content,
            subject=subject,
            memory_type='note',
            importance=importance,
            tags=tags,
            references=references,
            summary=summary,
            visibility=visibility,
        )

        self._promoted_ids.add(msg_id)
        # TODO(Phase 5): promote() の戻り値を bool → PromotionResult(success, observation_id, visibility) に拡張予定
        # 現在 save_fn の戻り値 (observation_id / visibility) は捨てている
        return True

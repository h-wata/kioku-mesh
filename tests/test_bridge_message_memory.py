"""Tests for MessageMemoryBridge (Phase 4, ADR-0022, ADR-0023)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import MagicMock

import pytest

from kioku_mesh.bridge import MessageMemoryBridge
from kioku_mesh.bridge.message_memory import MessageMemoryBridge as _BridgeDirect
from kioku_mesh.messaging.models import Message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(
    sender_id: str = 'sender-a',
    scope: str = 'team/kioku-mesh',
    body: str | dict = 'hello world',
    promote_hint: bool = False,
    priority: str = 'normal',
    **kwargs: object,
) -> Message:
    payload: dict = {'text': 'fallback', 'priority': priority}
    if promote_hint:
        # Use payload shorthand (convenience path in _requires_promotion)
        payload['promote_hint'] = True
    return Message(
        sender_id=sender_id,
        scope=scope,
        payload=payload,
        body=body,
        **kwargs,
    )


def _make_bridge() -> tuple[MessageMemoryBridge, MagicMock]:
    save_fn = MagicMock(return_value='saved: obs-id-001 (visibility=team)')
    bridge = MessageMemoryBridge(save_fn)
    return bridge, save_fn


# ---------------------------------------------------------------------------
# Test 1: 正常系昇格 — promote_hint あり
# ---------------------------------------------------------------------------


class TestPromoteWithHint:
    def test_save_observation_called_when_promote_hint_true(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=True)
        result = bridge.promote(msg)
        assert result is True
        save_fn.assert_called_once()

    def test_returns_false_when_no_promote_hint(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: 昇格条件 mismatch — promote_hint なし
# ---------------------------------------------------------------------------


class TestNoPromoteHint:
    def test_save_not_called_without_hint(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False, scope='mesh')
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_force_promotes_without_hint(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        result = bridge.promote(msg, force=True)
        assert result is True
        save_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: メタデータ転送 — sender / scope / created_at が渡る
# ---------------------------------------------------------------------------


class TestMetadataMapping:
    def test_sender_in_tags(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(sender_id='codex-cli', scope='team/kioku-mesh', promote_hint=True)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        tags = kwargs.get('tags', [])
        assert any('sender:codex-cli' in t for t in tags)

    def test_scope_in_tags(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(scope='team/kioku-mesh', promote_hint=True)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        tags = kwargs.get('tags', [])
        assert any('scope:team/kioku-mesh' in t for t in tags)

    def test_created_at_in_summary(self) -> None:
        bridge, save_fn = _make_bridge()
        ts = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)
        msg = _make_msg(promote_hint=True, created_at=ts)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        summary = kwargs.get('summary', '')
        assert '2026-06-24' in summary

    def test_msg_id_in_references(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=True)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        references = kwargs.get('references', [])
        assert any(msg.msg_id in ref for ref in references)

    def test_team_scope_maps_to_team_visibility(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(scope='team/kioku-mesh', promote_hint=True)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        assert kwargs.get('visibility') == 'team'

    def test_user_scope_maps_to_user_visibility(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(scope='user/hwata', promote_hint=True)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        assert kwargs.get('visibility') == 'user'

    def test_mesh_scope_maps_to_mesh_visibility(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(scope='mesh', promote_hint=True)
        bridge.promote(msg)
        _, kwargs = save_fn.call_args
        assert kwargs.get('visibility') == 'mesh'


# ---------------------------------------------------------------------------
# Test 4: invalid message — 必須 field 欠落で ValueError
# ---------------------------------------------------------------------------


class TestInvalidMessage:
    def test_missing_msg_id_raises(self) -> None:
        bridge, _ = _make_bridge()

        class _BadMsg:
            msg_id = ''
            sender_id = 'x'
            scope = 'mesh'
            payload = {}

        with pytest.raises(ValueError, match='msg_id'):
            bridge.promote(_BadMsg())

    def test_missing_sender_id_raises(self) -> None:
        bridge, _ = _make_bridge()

        class _BadMsg:
            msg_id = 'abc123'
            sender_id = ''
            scope = 'mesh'
            payload = {}

        with pytest.raises(ValueError, match='sender_id'):
            bridge.promote(_BadMsg())

    def test_missing_scope_raises(self) -> None:
        bridge, _ = _make_bridge()

        class _BadMsg:
            msg_id = 'abc123'
            sender_id = 'x'
            scope = ''
            payload = {}

        with pytest.raises(ValueError, match='scope'):
            bridge.promote(_BadMsg())


# ---------------------------------------------------------------------------
# Test 5: 重複 promote 防止 — 同 msg_id は 1 回だけ
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_promote_calls_save_once(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=True)
        result1 = bridge.promote(msg)
        result2 = bridge.promote(msg)
        assert result1 is True
        assert result2 is False
        save_fn.assert_called_once()

    def test_different_msg_ids_both_promoted(self) -> None:
        bridge, save_fn = _make_bridge()
        msg_a = _make_msg(promote_hint=True)
        msg_b = _make_msg(promote_hint=True)
        bridge.promote(msg_a)
        bridge.promote(msg_b)
        assert save_fn.call_count == 2


# ---------------------------------------------------------------------------
# Test 6: bridge depth — bridge が messaging と memory を両方 import できる
# ---------------------------------------------------------------------------


class TestBridgeLayering:
    def test_bridge_imports_messaging_models(self) -> None:
        from kioku_mesh.messaging.models import Message  # noqa: F401 (import check)

        assert Message is not None

    def test_bridge_module_importable(self) -> None:
        assert _BridgeDirect is MessageMemoryBridge

    def test_bridge_re_export_from_init(self) -> None:
        import kioku_mesh.bridge as _bridge_pkg

        assert _bridge_pkg.MessageMemoryBridge is MessageMemoryBridge

    def test_bridge_can_reference_save_observation_protocol(self) -> None:
        from kioku_mesh.bridge.message_memory import SaveObservationCallable

        save_fn = MagicMock(return_value='saved: id (visibility=team)')
        assert isinstance(save_fn, SaveObservationCallable)


# ---------------------------------------------------------------------------
# Test 7 (R1): promote_hint strict bool — truthy 非 bool 値で昇格しない
# ---------------------------------------------------------------------------


class TestPromoteHintStrictBool:
    def test_promote_hint_string_yes_does_not_promote(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        msg.payload['promote_hint'] = 'yes'
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_promote_hint_int_1_does_not_promote(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        msg.payload['promote_hint'] = 1
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_promote_hint_string_true_does_not_promote(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        msg.payload['promote_hint'] = 'true'
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_promote_hint_float_1_does_not_promote(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        msg.payload['promote_hint'] = 1.0
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_promote_hint_bool_true_promotes(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=True)
        result = bridge.promote(msg)
        assert result is True
        save_fn.assert_called_once()

    def test_promote_hint_bool_false_does_not_promote(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        msg.payload['promote_hint'] = False
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_promote_hint_extras_metadata_strict_bool(self) -> None:
        """_extras['metadata']['promote_hint'] も strict bool True のみ昇格する."""
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        # _extras に metadata を設定 (truthy 非 bool)
        msg._extras['metadata'] = {'promote_hint': 'yes'}
        result = bridge.promote(msg)
        assert result is False
        save_fn.assert_not_called()

    def test_promote_hint_extras_metadata_bool_true_promotes(self) -> None:
        bridge, save_fn = _make_bridge()
        msg = _make_msg(promote_hint=False)
        msg._extras['metadata'] = {'promote_hint': True}
        result = bridge.promote(msg)
        assert result is True
        save_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8 (R2): save_fn 例外時に _promoted_ids 未登録 — 再試行可能
# ---------------------------------------------------------------------------


class TestSaveFnFailurePath:
    def test_promote_save_fn_failure_keeps_retriable(self) -> None:
        save_fn = MagicMock(side_effect=RuntimeError('storage unavailable'))
        bridge = MessageMemoryBridge(save_fn)
        msg = _make_msg(promote_hint=True)

        # save_fn が例外を投げると promote() も例外を伝播する
        with pytest.raises(RuntimeError, match='storage unavailable'):
            bridge.promote(msg)

        # msg_id は promoted_ids に登録されていない (再試行可能)
        assert msg.msg_id not in bridge._promoted_ids

        # 同じ msg_id で再度 promote() を呼べること
        save_fn.side_effect = None
        save_fn.return_value = 'saved: obs-retry (visibility=team)'
        result = bridge.promote(msg)
        assert result is True
        assert msg.msg_id in bridge._promoted_ids

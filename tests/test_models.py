"""Unit tests for mesh-mem data models."""

import json

import pytest

from mesh_mem.models import Observation
from mesh_mem.models import Tombstone
from mesh_mem.models import VALID_MEMORY_TYPES
import mesh_mem.models as models_module


def test_observation_key_expr_contains_all_identity_fragments() -> None:
    obs = Observation(content='hello')
    parts = obs.key_expr.split('/')
    assert parts[0] == 'mem'
    assert parts[1] == 'obs'
    # 5 segments: agent_family / client_id / pc_id / session_id / observation_id
    assert len(parts) == 7
    assert parts[-1] == obs.observation_id


def test_observation_id_is_32_hex_chars() -> None:
    obs = Observation(content='x')
    assert len(obs.observation_id) == 32
    int(obs.observation_id, 16)  # valid hex


def test_tombstone_key_expr_mirrors_observation() -> None:
    obs = Observation(content='x')
    tomb_key = obs.tombstone_key_expr()
    assert tomb_key.startswith('mem/tomb/')
    assert tomb_key.endswith(obs.observation_id)
    assert tomb_key.replace('mem/tomb/', 'mem/obs/', 1) == obs.key_expr


def test_observation_json_round_trip_preserves_fields() -> None:
    obs = Observation(content='hello', project='robo-hi', tags=['a', 'b'])
    restored = Observation.from_json(obs.to_json())
    assert restored.content == obs.content
    assert restored.project == obs.project
    assert restored.tags == obs.tags
    assert restored.observation_id == obs.observation_id


def test_observation_from_json_drops_unknown_fields() -> None:
    obs = Observation(content='hello')
    raw = json.loads(obs.to_json())
    raw['future_field'] = 'ignore me'
    raw['another'] = 42
    restored = Observation.from_json(json.dumps(raw))
    assert restored.content == 'hello'


def test_tombstone_json_round_trip() -> None:
    t = Tombstone(observation_id='a' * 32, reason='test')
    restored = Tombstone.from_json(t.to_json())
    assert restored.observation_id == t.observation_id
    assert restored.reason == 'test'


def test_observation_id_uniqueness_across_10000() -> None:
    ids = {Observation(content='').observation_id for _ in range(10000)}
    assert len(ids) == 10000


def test_key_expr_reflects_explicit_identity_fields() -> None:
    obs = Observation(
        content='test',
        agent_family='gemini',
        client_id='gemini-cli',
        pc_id='testpc123',
        session_id='sess456',
    )
    expected = f'mem/obs/gemini/gemini-cli/testpc123/sess456/{obs.observation_id}'
    assert obs.key_expr == expected
    assert obs.tombstone_key_expr() == expected.replace('mem/obs/', 'mem/tomb/', 1)


def test_observation_default_values() -> None:
    obs = Observation(content='hello')
    assert obs.memory_type == 'note'
    assert obs.importance == 2
    assert obs.subject == ''
    assert obs.summary == ''
    assert obs.source_files == []
    assert obs.references == []
    assert obs.supersedes == []


def test_observation_with_all_fields() -> None:
    obs = Observation(
        content='decision about X',
        memory_type='decision',
        importance=5,
        subject='architecture',
        summary='use Zenoh for transport',
        source_files=['src/mesh_mem/store.py'],
        references=['h-wata/mesh-mem#73'],
        supersedes=['abc123'],
    )
    d = json.loads(obs.to_json())
    assert d['memory_type'] == 'decision'
    assert d['importance'] == 5
    assert d['subject'] == 'architecture'
    assert d['summary'] == 'use Zenoh for transport'
    assert d['source_files'] == ['src/mesh_mem/store.py']
    assert d['references'] == ['h-wata/mesh-mem#73']
    assert d['supersedes'] == ['abc123']
    restored = Observation.from_json(obs.to_json())
    assert restored.memory_type == obs.memory_type
    assert restored.importance == obs.importance
    assert restored.source_files == obs.source_files
    assert restored.references == obs.references
    assert restored.supersedes == obs.supersedes


def test_observation_importance_clamp() -> None:
    assert Observation(content='x', importance=0).importance == 1
    assert Observation(content='x', importance=10).importance == 5
    assert Observation(content='x', importance=1).importance == 1
    assert Observation(content='x', importance=5).importance == 5
    assert Observation(content='x', importance=3).importance == 3


def test_observation_old_json_compat() -> None:
    old_json = json.dumps(
        {
            'content': 'old obs',
            'agent_family': 'claude',
            'client_id': 'claude-code',
            'pc_id': 'pc1',
            'session_id': 'sess1',
            'project': 'test',
            'tags': [],
            'observation_id': 'a' * 32,
            'created_at': '2026-01-01T00:00:00.000000Z',
        }
    )
    obs = Observation.from_json(old_json)
    assert obs.content == 'old obs'
    assert obs.memory_type == 'note'
    assert obs.importance == 2
    assert obs.subject == ''
    assert obs.summary == ''
    assert obs.source_files == []
    assert obs.references == []
    assert obs.supersedes == []


def test_observation_new_json_old_code_compat() -> None:
    obs = Observation(
        content='new obs',
        memory_type='decision',
        importance=4,
        subject='test subject',
        summary='test summary',
        source_files=['a.py'],
        references=['h-wata/mesh-mem#73'],
        supersedes=['b' * 32],
    )
    new_json = obs.to_json()
    raw = json.loads(new_json)
    raw['unknown_future_field'] = 'ignored'
    restored = Observation.from_json(json.dumps(raw))
    assert restored.content == 'new obs'
    assert restored.memory_type == 'decision'
    assert restored.importance == 4


def test_observation_rejects_invalid_memory_type() -> None:
    """Construction with an out-of-enum memory_type must raise.

    Without this guard, an LLM that ignores the MCP server's instructions
    can land entries (e.g. memory_type='feature') that fall outside the
    documented set, polluting category-based search.
    """
    with pytest.raises(ValueError, match='memory_type'):
        Observation(content='x', memory_type='feature')


def test_observation_accepts_all_documented_memory_types() -> None:
    for mt in VALID_MEMORY_TYPES:
        obs = Observation(content='x', memory_type=mt)
        assert obs.memory_type == mt


def test_observation_preserves_unknown_fields_through_round_trip() -> None:
    """Unknown fields from a newer schema are preserved via _extras and re-emitted on to_json."""
    new_payload = json.dumps(
        {
            'content': 'x',
            'future_field_scalar': 1,
            'future_field_list': ['a', 'b'],
        }
    )
    obs = Observation.from_json(new_payload)
    round_tripped = json.loads(obs.to_json())
    assert round_tripped['content'] == 'x'
    assert round_tripped['future_field_scalar'] == 1
    assert round_tripped['future_field_list'] == ['a', 'b']


def test_observation_extras_empty_for_normal_json() -> None:
    """Normal JSON with no unknown fields leaves _extras empty and to_json unchanged."""
    obs = Observation(content='hello', project='test')
    restored = Observation.from_json(obs.to_json())
    assert getattr(restored, '_extras', {}) == {}
    assert json.loads(restored.to_json())['content'] == 'hello'
    assert json.loads(restored.to_json())['project'] == 'test'


def test_observation_extras_dataclass_field_wins_on_collision() -> None:
    """Dataclass field wins over _extras when the same key appears in both."""
    obs = Observation(content='original', project='p1')
    # Simulate a future schema promotion: a key now in _extras also exists as a dataclass field
    obs._extras = {'content': 'from_extras', 'project': 'from_extras'}
    result = json.loads(obs.to_json())
    assert result['content'] == 'original'
    assert result['project'] == 'p1'


def test_observation_from_json_clamps_unknown_memory_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamp unknown memory_type to 'note' for forward-compat.

    A peer on a future schema may emit a value not in VALID_MEMORY_TYPES.
    ``from_json`` must clamp to 'note' rather than raise so replication
    does not stall on schema drift.
    """
    debug_msgs: list[str] = []

    def _debug(msg: str, *args: object) -> None:
        debug_msgs.append(msg % args if args else msg)

    monkeypatch.setattr(models_module.log, 'debug', _debug)
    raw = {
        'content': 'from a future peer',
        'memory_type': 'feature',  # not in VALID_MEMORY_TYPES
        'observation_id': 'a' * 32,
    }
    obs = Observation.from_json(json.dumps(raw))
    assert obs.memory_type == 'note'
    assert any('feature' in msg for msg in debug_msgs)

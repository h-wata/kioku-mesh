"""Unit tests for mesh-mem data models."""

import json

from mesh_mem.models import Observation
from mesh_mem.models import Tombstone


def test_observation_key_expr_contains_all_identity_fragments() -> None:
    obs = Observation(content='hello')
    parts = obs.key_expr.split('/')
    assert parts[0] == 'mem'
    assert parts[1] == 'obs'
    # agent_family / client_id / pc_id / session_id / observation_id の 5 段
    assert len(parts) == 7
    assert parts[-1] == obs.observation_id


def test_observation_id_is_32_hex_chars() -> None:
    obs = Observation(content='x')
    assert len(obs.observation_id) == 32
    int(obs.observation_id, 16)  # hex として valid


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
    assert obs.supersedes == []


def test_observation_with_all_fields() -> None:
    obs = Observation(
        content='decision about X',
        memory_type='decision',
        importance=5,
        subject='architecture',
        summary='use Zenoh for transport',
        source_files=['src/mesh_mem/store.py'],
        supersedes=['abc123'],
    )
    d = json.loads(obs.to_json())
    assert d['memory_type'] == 'decision'
    assert d['importance'] == 5
    assert d['subject'] == 'architecture'
    assert d['summary'] == 'use Zenoh for transport'
    assert d['source_files'] == ['src/mesh_mem/store.py']
    assert d['supersedes'] == ['abc123']
    restored = Observation.from_json(obs.to_json())
    assert restored.memory_type == obs.memory_type
    assert restored.importance == obs.importance
    assert restored.source_files == obs.source_files
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
    assert obs.supersedes == []


def test_observation_new_json_old_code_compat() -> None:
    obs = Observation(
        content='new obs',
        memory_type='decision',
        importance=4,
        subject='test subject',
        summary='test summary',
        source_files=['a.py'],
        supersedes=['b' * 32],
    )
    new_json = obs.to_json()
    raw = json.loads(new_json)
    raw['unknown_future_field'] = 'ignored'
    restored = Observation.from_json(json.dumps(raw))
    assert restored.content == 'new obs'
    assert restored.memory_type == 'decision'
    assert restored.importance == 4

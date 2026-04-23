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

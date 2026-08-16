"""Tests for MCP tool-call fragment detection (TASK-373).

Covers:
  - ``save_lint.find_tool_call_fragment`` against the two leak shapes actually
    observed in the store, and against prose that must NOT be flagged
  - the ``save_observation`` boundary rejecting such a value
  - ``doctor.check_tool_call_fragments`` sweeping stored entries

The polluted samples are abbreviated tails of the real entries
(fd239c2018, 6999fdd0, 2bdd33a1, 2ed5825a); the clean samples include the two
store entries that discuss this bug in prose, which the detector must leave
alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from kioku_mesh.core.models import Observation
from kioku_mesh.doctor import check_tool_call_fragments
from kioku_mesh.doctor import CheckStatus
from kioku_mesh.memory.save_lint import find_tool_call_fragment

# -- the leak shapes seen in production ---------------------------------------

FULL_LEAK = (
    'KILL 後も integrity_check=ok。verdict は approve、非 blocking 6 件。</content>\n'
    '<memory_type>pattern</memory_type>\n'
    '<project>kioku-mesh</project>\n'
    '<importance>5</importance>\n'
)
PARTIAL_LEAK_MEMORY_TYPE = '外部先例では上位 N の既定はいずれも 10。</content>\n<parameter name="memory_type">bug'
PARTIAL_LEAK_REFERENCES = 'Heuristic は点検促し止まり。</content>\n<parameter name="references">["#158", "#104"]'

POLLUTED = [FULL_LEAK, PARTIAL_LEAK_MEMORY_TYPE, PARTIAL_LEAK_REFERENCES]

CLEAN = [
    'Root cause: race condition in the flush path when two sessions write at once.',
    # a note *about* this bug: names the markup without carrying a weld
    'save_observation の content 末尾に tool call の閉じタグが残る事象を調査した。原因はクライアント側。',
    # unrelated XML/HTML in a code sample
    '<html><body><p>doc fixture</p></body></html> を parse するテストを追加した。',
    # our parameter names in ordinary prose
    'content と summary と memory_type の3引数を必須にするか検討したが、summary のみ必須とした。',
    # an XML-ish tag that is not one of our parameters
    'テンプレートは <project_root>/config.yaml を展開する。</project_root> は使わない。',
]


@pytest.mark.parametrize('text', POLLUTED)
def test_detects_observed_leak_shapes(text: str) -> None:
    assert find_tool_call_fragment(text) is not None


@pytest.mark.parametrize('text', CLEAN)
def test_does_not_flag_legitimate_prose(text: str) -> None:
    assert find_tool_call_fragment(text) is None


def test_fragment_is_reported_verbatim() -> None:
    """The returned fragment is the matched text, so the error can quote it."""
    assert find_tool_call_fragment(PARTIAL_LEAK_MEMORY_TYPE) == '<parameter name="memory_type"'


def test_detects_leak_far_from_the_end() -> None:
    """A weld hundreds of characters from the end still matches (no tail window).

    2ed5825a's weld sits 589 chars from the end of a 2210-char content; a
    fixed tail window would have missed it.
    """
    assert find_tool_call_fragment(FULL_LEAK + 'x' * 2000) is not None


# -- doctor sweep --------------------------------------------------------------


def _obs(content: str) -> Observation:
    return Observation(content=content, project='demo', memory_type='note', subject='s', summary='one line')


def test_doctor_passes_on_clean_store() -> None:
    result = check_tool_call_fragments(observations=[_obs(t) for t in CLEAN])
    assert result.status is CheckStatus.PASS
    assert result.details['hits'] == 0
    assert result.details['scanned'] == len(CLEAN)


def test_doctor_warns_and_lists_polluted_entries() -> None:
    polluted = _obs(FULL_LEAK)
    result = check_tool_call_fragments(observations=[_obs(CLEAN[0]), polluted])
    assert result.status is CheckStatus.WARN
    assert result.details['hits'] == 1
    assert result.details['examples'][0]['observation_id'] == polluted.observation_id
    assert result.details['examples'][0]['field'] == 'content'


def test_doctor_scans_subject_and_summary_too() -> None:
    obs = Observation(
        content='clean body',
        project='demo',
        memory_type='note',
        subject='s',
        summary=PARTIAL_LEAK_REFERENCES,
    )
    result = check_tool_call_fragments(observations=[obs])
    assert result.details['examples'][0]['field'] == 'summary'


# -- MCP boundary --------------------------------------------------------------

pytest.importorskip('fastmcp')

from fastmcp import Client  # noqa: E402 — must follow importorskip

from kioku_mesh.mcp_server import mcp  # noqa: E402


def _mock_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import kioku_mesh.mcp_server as _mcp_mod  # noqa: PLC0415

    class _NoopBackend:
        def put_observation(self, obs: Any) -> None:  # noqa: ANN401
            raise AssertionError('a rejected save must never reach the backend')

        def find_supersede_candidates(self, obs: Any) -> list:  # noqa: ANN401
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(_mcp_mod, 'get_backend', _NoopBackend)


@pytest.mark.parametrize('field', ['content', 'subject', 'summary'])
def test_save_observation_rejects_tool_call_fragment(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked fragment is a tool error, not a successful-looking string.

    A returned string is ``is_error=false`` on the MCP wire, so the client
    would read the refusal as a save and never retry.
    """
    import asyncio  # noqa: PLC0415

    _mock_backend(monkeypatch)
    args = {
        'content': 'A perfectly ordinary observation body.',
        'subject': 'tool call guard',
        'summary': 'one line abstract',
        field: PARTIAL_LEAK_MEMORY_TYPE,
    }

    async def _go() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool('save_observation', args, raise_on_error=False)

    result = asyncio.run(_go())
    assert result.is_error is True
    message = ' '.join(getattr(block, 'text', '') for block in result.content)
    assert field in message
    assert 'tool-call fragment' in message

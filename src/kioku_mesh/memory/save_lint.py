"""Save-quality validators (warn-only) for kioku-mesh observations.

ADR-0028 Phase5: lint_observation() inspects content/memory_type/subject
before persistence and returns a list of LintWarning. An empty list means
no issues were found. The function NEVER raises — callers must not gate
saves on the output.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class LintWarning(NamedTuple):
    code: str
    message: str


_GENERIC_NOISE_TERMS: frozenset[str] = frozenset(
    {
        'tests pass',
        'test passed',
        'all tests pass',
        'done',
        '完了',
        '進捗',
        'pass',
        'ok',
        'green',
        'build succeeded',
    }
)

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ('sk-*** (OpenAI-style API key)', re.compile(r'sk-[A-Za-z0-9]{20,}')),
    ('ghp_*** (GitHub PAT)', re.compile(r'ghp_[A-Za-z0-9]{10,}')),
    ('Bearer *** (HTTP Bearer token)', re.compile(r'Bearer [A-Za-z0-9._-]{20,}')),
]


# -- MCP tool-call fragment detection (TASK-373) -------------------------------
#
# A field value that arrives with the *next* parameter's markup appended to it
# means the client failed to terminate the string while assembling the tool
# call — every observed case (2026-06..2026-08, all client_id=claude-code) has
# the leak at the very end of ``content``. This is permanent input validation,
# not a workaround for one client: any LLM client builds tool calls the same
# way, and an MCP server must not trust the field boundaries it is handed.
#
# Detection only. Nothing here strips or rewrites the value — repairing
# already-stored text is a one-off migration, not a server responsibility.

_SAVE_PARAM_NAMES = (
    'content|subject|summary|project|tags|memory_type|importance'
    '|source_files|references|supersedes|visibility|expires_at|ttl_sec'
)

# Matched anywhere in the value, not just at the end: the leaked block runs from
# the unterminated ``</content>`` to the end of the tool call and is often
# hundreds of characters long, so a fixed tail window misses real cases (the
# 2026-08-11 pair welds at 589 / 642 chars from the end). Precision comes from
# anchoring on save_observation's own parameter names instead — generic XML in
# prose does not match. Deliberately quoting this markup in an observation is
# the known false positive; the tool error says how to reword.
_TOOL_CALL_FRAGMENT_PATTERNS: list[re.Pattern[str]] = [
    # ``</content>``, ``</subject>`` or ``</summary>`` welded to the following
    # parameter's tag — any of the three fields save_observation accepts can
    # be left unterminated by the client (PR312-B1: content-only anchoring
    # missed subject/summary-origin welds).
    re.compile(r'</(?:content|subject|summary)>\s*</?(?:%s)>' % _SAVE_PARAM_NAMES),
    # Claude Code's ``<parameter name="...">`` notation naming one of our params
    re.compile(r'<parameter\s+name="(?:%s)"' % _SAVE_PARAM_NAMES),
]
# A bare tool-call closing tag is deliberately NOT a pattern: every observed
# leak also carries one of the two welds above, while text that only mentions
# the closing tag is a note *about* this bug (2 such entries exist in the
# store). Matching it would have cost 2 false positives and caught nothing new.


def find_tool_call_fragment(text: str) -> str | None:
    """Return the MCP tool-call fragment leaked into ``text``, or None."""
    for pattern in _TOOL_CALL_FRAGMENT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def lint_observation(
    content: str,
    memory_type: str,
    subject: str,
    source_files: list[str] | None = None,
) -> list[LintWarning]:
    """Check content/memory_type/subject and return quality warnings.

    Never raises. Returns [] when no issues found.
    """
    warnings: list[LintWarning] = []

    # 1. generic_noise: short content exactly matching known progress/status terms
    if len(content) < 100 and content.strip().lower() in _GENERIC_NOISE_TERMS:
        warnings.append(
            LintWarning(
                code='GENERIC_NOISE',
                message="Content appears to be generic progress/status noise (e.g. 'tests pass', 'done').",
            )
        )

    # 2. missing_subject: decision/config without a subject
    if memory_type in ('decision', 'config') and not subject.strip():
        warnings.append(
            LintWarning(
                code='MISSING_SUBJECT',
                message=f"memory_type '{memory_type}' should have a non-empty subject.",
            )
        )

    # 3. secret_pattern: obvious API key / token patterns (conservative)
    for pattern_name, pattern_re in _SECRET_PATTERNS:
        if pattern_re.search(content):
            warnings.append(
                LintWarning(
                    code='SECRET_PATTERN',
                    message=f'Content may contain a secret/token (matched pattern: {pattern_name}).',
                )
            )
            break

    # 4. kiokuignore — TODO: implement in a future issue (Phase5 scope omitted)
    # if source_files: check .kiokuignore path rules against source_files

    return warnings

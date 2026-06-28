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

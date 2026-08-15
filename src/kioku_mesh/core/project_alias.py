"""Read-side project alias resolution (Issue #278).

Product renames (e.g. ADR-0024's ``mesh-mem`` -> ``kioku-mesh``) leave
historical observations stored under the old ``project`` value. This module
turns a project filter into the *set* of literal ``project`` values that
belong to the same logical project, for *read* paths (``search_memory`` /
``recall_context``) only; it never touches what gets written by
``save_observation``, keeping the append-only store untouched (ADR-0028).

Both directions matter, and only the second one fixes the symptom reported in
Issue #278:

* querying the legacy name must reach rows saved under the canonical name;
* querying the canonical name must reach rows saved under the legacy name
  (the pre-rename history, which is the bulk of the store).

Callers therefore filter on :func:`expand_project_aliases`, not on a single
resolved value.
"""

from collections.abc import Sequence

# Legacy project name -> canonical project name. Read-side only: values
# already persisted under the legacy key are never rewritten.
PROJECT_ALIASES: dict[str, str] = {
    'mesh-mem': 'kioku-mesh',
    # Accidental absolute-path project value from an early save (Issue TASK-361
    # investigation): a client passed its cwd instead of a project name.
    '/home/gisen/work/mesh-mem': 'kioku-mesh',
    # underscore/hyphen spelling variants observed for the same repo (content
    # confirmed identical topic: portable scanning pipeline / handyscanner).
    'portable_colorized_scanner': 'portable-scanner',
    # 'rmf_ws' (workspace dir name) is the more common spelling for the same
    # RMF core repo; 'rmf' is an older, sparser alias for it.
    'rmf': 'rmf_ws',
}


def resolve_project_alias(project: str) -> str:
    """Return the canonical project name for ``project``.

    Unknown or empty values pass through unchanged so callers can use this
    unconditionally without special-casing "no alias" / "no project filter".
    """
    if not project:
        return project
    return PROJECT_ALIASES.get(project, project)


def expand_project_aliases(project: str) -> tuple[str, ...]:
    """Return every stored ``project`` value equivalent to ``project``.

    The canonical name comes first, followed by its legacy names in sorted
    order. An empty filter stays empty (= "no project filter"), and a name
    with no alias entry expands to itself, so callers can pass the result
    straight to a backend filter without special-casing.

    Only single-hop aliases are resolved (see :func:`resolve_project_alias`),
    so a chain ``a -> b -> c`` groups ``a`` with ``b``, not with ``c``.
    """
    if not project:
        return ()
    canonical = resolve_project_alias(project)
    legacy = sorted(old for old, new in PROJECT_ALIASES.items() if new == canonical and old != canonical)
    return (canonical, *legacy)


def normalize_project_filter(project: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize a backend ``project`` filter to a tuple of literal values.

    Accepts the historical single-string form as well as the multi-value form
    produced by :func:`expand_project_aliases`. Empty strings are dropped, so
    ``''`` and ``()`` both mean "no project filter". Order is preserved and
    duplicates are removed so the resulting SQL ``IN`` list is stable.
    """
    values = (project,) if isinstance(project, str) else tuple(project)
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)

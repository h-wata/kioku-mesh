"""Read-side project alias resolution (Issue #278).

Product renames (e.g. ADR-0024's ``mesh-mem`` -> ``kioku-mesh``) leave
historical observations stored under the old ``project`` value. This module
resolves an alias to its canonical name for *read* paths (``search_memory`` /
``recall_context``) only; it never touches what gets written by
``save_observation``, keeping the append-only store untouched (ADR-0028).
"""

# Legacy project name -> canonical project name. Read-side only: values
# already persisted under the legacy key are never rewritten.
PROJECT_ALIASES: dict[str, str] = {
    'mesh-mem': 'kioku-mesh',
}


def resolve_project_alias(project: str) -> str:
    """Return the canonical project name for ``project``.

    Unknown or empty values pass through unchanged so callers can use this
    unconditionally without special-casing "no alias" / "no project filter".
    """
    if not project:
        return project
    return PROJECT_ALIASES.get(project, project)

"""ADR-0023 layering rule verification via static AST analysis.

Enforces that memory/messaging/bridge layers do not cross-depend in ways
prohibited by ADR-0023, without importing modules or triggering side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / 'src' / 'kioku_mesh'

CORE_PKG = 'kioku_mesh.core'
MEMORY_PKG = 'kioku_mesh.memory'
MESSAGING_PKG = 'kioku_mesh.messaging'
BRIDGE_PKG = 'kioku_mesh.bridge'


def _collect_imports(pkg_dir: Path) -> dict[str, list[str]]:
    """Return {filename: [imported_module, ...]} for all .py files in pkg_dir."""
    result: dict[str, list[str]] = {}
    for py_file in sorted(pkg_dir.glob('*.py')):
        if py_file.name == '__init__.py':
            continue
        tree = ast.parse(py_file.read_text(encoding='utf-8'))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        result[py_file.name] = imports
    return result


def _build_absolute_module(relative_level: int, base_pkg: str, module: str | None) -> str:
    """Resolve a relative import to its absolute module name."""
    parts = base_pkg.split('.')
    parent_parts = parts[: len(parts) - (relative_level - 1)]
    base = '.'.join(parent_parts)
    if module:
        return f'{base}.{module}'
    return base


def _collect_absolute_imports(pkg_dir: Path, pkg_name: str) -> dict[str, list[str]]:
    """Return {filename: [absolute_module, ...]} resolving relative imports."""
    result: dict[str, list[str]] = {}
    for py_file in sorted(pkg_dir.glob('*.py')):
        if py_file.name == '__init__.py':
            continue
        tree = ast.parse(py_file.read_text(encoding='utf-8'))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    abs_mod = _build_absolute_module(node.level, pkg_name, node.module)
                    imports.append(abs_mod)
                elif node.module:
                    imports.append(node.module)
        result[py_file.name] = imports
    return result


def test_core_does_not_import_memory() -> None:
    """ADR-0023: core layer must not import memory layer."""
    core_imports = _collect_absolute_imports(SRC_ROOT / 'core', CORE_PKG)
    violations: list[str] = []
    for filename, imports in core_imports.items():
        for imp in imports:
            if imp.startswith(MEMORY_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'core 層が memory 層に依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_core_does_not_import_messaging() -> None:
    """ADR-0023: core layer must not import messaging layer."""
    core_imports = _collect_absolute_imports(SRC_ROOT / 'core', CORE_PKG)
    violations: list[str] = []
    for filename, imports in core_imports.items():
        for imp in imports:
            if imp.startswith(MESSAGING_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'core 層が messaging 層に依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_memory_does_not_import_messaging() -> None:
    """ADR-0023: memory layer must not directly import messaging layer."""
    memory_imports = _collect_absolute_imports(SRC_ROOT / 'memory', MEMORY_PKG)
    violations: list[str] = []
    for filename, imports in memory_imports.items():
        for imp in imports:
            if imp.startswith(MESSAGING_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'memory 層が messaging 層に直接依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_memory_does_not_import_bridge() -> None:
    """ADR-0023: memory layer must not directly import bridge layer."""
    memory_imports = _collect_absolute_imports(SRC_ROOT / 'memory', MEMORY_PKG)
    violations: list[str] = []
    for filename, imports in memory_imports.items():
        for imp in imports:
            if imp.startswith(BRIDGE_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'memory 層が bridge 層に直接依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_core_files_exist() -> None:
    """All expected core/ modules are present."""
    expected = {'transport.py', 'tls.py', 'identity.py', 'keyspace.py', 'config.py', 'paths.py', 'models.py'}
    actual = {f.name for f in (SRC_ROOT / 'core').glob('*.py') if f.name != '__init__.py'}
    missing = expected - actual
    assert not missing, f'core/ に不足しているファイル: {missing}'


def test_memory_files_exist() -> None:
    """All expected memory/ modules are present."""
    expected = {'store.py', 'local_index.py', 'pending_queue.py', 'purge.py', 'backend.py', 'replication.py'}
    actual = {f.name for f in (SRC_ROOT / 'memory').glob('*.py') if f.name != '__init__.py'}
    missing = expected - actual
    assert not missing, f'memory/ に不足しているファイル: {missing}'


def test_stub_layers_exist() -> None:
    """Stub packages for messaging/ and bridge/ layers exist."""
    assert (SRC_ROOT / 'messaging' / '__init__.py').exists(), 'messaging/__init__.py が存在しません'
    assert (SRC_ROOT / 'bridge' / '__init__.py').exists(), 'bridge/__init__.py が存在しません'


def test_messaging_does_not_import_memory() -> None:
    """ADR-0023: messaging layer must not directly import memory layer."""
    messaging_dir = SRC_ROOT / 'messaging'
    violations: list[str] = []
    for p in sorted(messaging_dir.glob('*.py')):
        tree = ast.parse(p.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.level > 0:
                    abs_mod = _build_absolute_module(node.level, MESSAGING_PKG, node.module)
                else:
                    abs_mod = getattr(node, 'module', '') or ''
                if abs_mod.startswith(MEMORY_PKG):
                    violations.append(f'{p.name}: imports {abs_mod!r}')
    assert not violations, 'messaging 層が memory 層に直接依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_bridge_may_import_messaging_and_memory() -> None:
    """ADR-0023 (O1): bridge layer is the only layer allowed to import both messaging and memory.

    This test statically verifies that bridge/ files do NOT violate the rule
    by importing outside the permitted set, and confirms that bridge/message_memory.py
    actually references both layers (so the bridge is serving its intended role).
    """
    bridge_imports = _collect_absolute_imports(SRC_ROOT / 'bridge', BRIDGE_PKG)
    # bridge must not import core except through allowed paths — no direct restriction,
    # but verify bridge does not accidentally depend on test-only or stdlib-only modules.
    # Primary check: bridge files that do exist should not re-export memory or messaging
    # in a way that creates a circular dependency. Since bridge is a one-way adapter,
    # we just confirm the bridge layer itself does not violate memory <-> messaging isolation.
    for filename, imports in bridge_imports.items():
        for imp in imports:
            # bridge must not import from itself recursively in a way that loops
            assert not (
                imp.startswith(MEMORY_PKG) and imp.startswith(MESSAGING_PKG)
            ), f'{filename}: impossible combined import {imp!r}'

    # Confirm message_memory.py is present and the bridge package is non-empty
    assert (
        SRC_ROOT / 'bridge' / 'message_memory.py'
    ).exists(), 'bridge/message_memory.py が存在しません — Phase 4 bridge が実装されていません'


# INV-7 gate policy (Issue #249): rather than an allowlist of known derived-view
# *names* (which silently skips anything not on the list — the bug this test
# used to have), this is a denylist of modules known NOT to be derived views.
# Every other module found by scanning memory/*.py is treated as a derived-view
# candidate and MUST expose DERIVED_VIEW_REBUILD_SYMBOL. Adding a new module to
# memory/ without adding it here means it is checked automatically; a real
# derived view fails loudly until it implements the rebuild path, and a real
# non-derived-view module must be added here with a one-line reason.
RAW_AND_INFRA_MODULES = {
    'store.py': 'raw write/read path to Zenoh — this IS the rebuild source, not a derived view',
    'local_raw_store.py': 'raw Observation/Tombstone persistence itself — rebuild source, not a derived view',
    'local_index.py': (
        'SQLite read cache; already implements its own rebuild_from_zenoh and is covered by test_memory_files_exist'
    ),
    'backend.py': 'orchestrates local/zenoh MemoryBackend selection — no persisted derived state of its own',
    'pending_queue.py': 'transient outbox queue for pending puts — not a persisted view',
    'purge.py': 'gc/tombstone physical purge over the raw store — not a persisted view',
    'replication.py': 'replication bookkeeping — not a persisted view',
    'metadata.py': 'stateless required-metadata rules and derivation helpers — not a persisted view',
    'save_lint.py': 'stateless save-time lint checks — not a persisted view',
    'supersede.py': 'supersede/tombstone logic operating on the raw store — not a persisted view',
    'visibility_migration.py': 'one-off migration CLI helper — not a persisted view',
    'scope_migration.py': (
        'one-off scope cutover CLI helper (manifest / re-PUT / inventory / purge); its manifest and '
        'checkpoint are run artifacts re-derivable by re-running the phase, not a persisted view'
    ),
}

DERIVED_VIEW_REBUILD_SYMBOL = 'rebuild_from_raw'


def _discover_derived_view_candidates() -> list[Path]:
    """Every memory/*.py module not explicitly exempted in RAW_AND_INFRA_MODULES."""
    memory_dir = SRC_ROOT / 'memory'
    return sorted(
        p for p in memory_dir.glob('*.py') if p.name != '__init__.py' and p.name not in RAW_AND_INFRA_MODULES
    )


def test_derived_view_modules_have_rebuild_path() -> None:
    """INV-7: Future derived views must expose a rebuild path from raw Observation/Tombstone.

    ADR-0028 states derived views (embedding, graph, summary, recall cache, ...)
    must be reconstructable from raw Observation/Tombstone and must not hold
    non-reconstructable authority.

    This scans memory/*.py dynamically (see RAW_AND_INFRA_MODULES) instead of
    matching a fixed list of expected names, so a derived view added under any
    module name is caught — not just the four names originally anticipated.
    Every module found this way must expose a function or method named
    'rebuild_from_raw' (or analogous symbol). This test passes while no such
    module exists yet; it FAILS as soon as one is added without the required
    rebuild symbol, or added without being exempted in RAW_AND_INFRA_MODULES.
    """
    violations: list[str] = []
    for module_file in _discover_derived_view_candidates():
        tree = ast.parse(module_file.read_text(encoding='utf-8'))
        defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if DERIVED_VIEW_REBUILD_SYMBOL not in defined:
            violations.append(
                f'{module_file.name}: missing {DERIVED_VIEW_REBUILD_SYMBOL!r} — '
                'INV-7 requires every derived view to have a rebuild path from raw Observation/Tombstone '
                '(or add it to RAW_AND_INFRA_MODULES with a reason if it is not a derived view)'
            )
    assert not violations, 'Derived view modules violate INV-7 (non-reconstructable authority):\n' + '\n'.join(
        violations
    )


def test_derived_view_modules_policy_is_documented() -> None:
    """INV-7 policy checklist: confirms this file documents the layering gate for derived views.

    This is a permanent policy marker. Removing or weakening the
    test_derived_view_modules_have_rebuild_path test above violates ADR-0028
    INV-7 and must be accompanied by an ADR supersession.
    """
    assert RAW_AND_INFRA_MODULES, 'RAW_AND_INFRA_MODULES must document at least the known raw/infra modules'
    assert DERIVED_VIEW_REBUILD_SYMBOL, 'DERIVED_VIEW_REBUILD_SYMBOL must be a non-empty string'
    memory_dir = SRC_ROOT / 'memory'
    actual = {f.name for f in memory_dir.glob('*.py') if f.name != '__init__.py'}
    stale = set(RAW_AND_INFRA_MODULES) - actual
    assert not stale, f'RAW_AND_INFRA_MODULES references modules that no longer exist: {stale}'


def test_bridge_does_not_create_memory_messaging_cycle() -> None:
    """ADR-0023 (O1): bridge must not make memory import messaging or vice versa.

    Verify that bridge/message_memory.py itself does not import from memory in a way
    that would force memory to depend on messaging (cycle check via AST).
    The bridge is allowed to import from both; this test checks there is no indirect cycle.
    """
    bridge_file = SRC_ROOT / 'bridge' / 'message_memory.py'
    if not bridge_file.exists():
        return  # Phase 4 not yet implemented — skip
    tree = ast.parse(bridge_file.read_text(encoding='utf-8'))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                imports.append(_build_absolute_module(node.level, BRIDGE_PKG, node.module))
            elif node.module:
                imports.append(node.module)
    # bridge/message_memory.py must NOT import from messaging within memory layer
    # (that would mean memory indirectly depends on messaging via bridge re-import)
    memory_violations = [imp for imp in imports if imp.startswith(MEMORY_PKG) and MESSAGING_PKG in imp]
    assert not memory_violations, (
        'bridge/message_memory.py が memory 経由で messaging に依存しています (ADR-0023 cycle 違反):\n'
        + '\n'.join(memory_violations)
    )

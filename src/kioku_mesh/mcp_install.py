"""Register ``kioku-mesh-mcp`` with supported MCP clients (#85).

v0.3 scope: Claude Code (via ``claude mcp add``) and Codex CLI (via direct
edit of ``~/.codex/config.toml``). Claude Desktop is deferred until #87
(macOS / Windows verification); Gemini CLI is deferred to v0.4. Both gaps
are intentional — the manual recipes in ``docs/mcp-clients.md`` still
cover those clients.

Design notes:
- The absolute path to ``kioku-mesh-mcp`` is resolved at install time
  (``shutil.which``) and baked into the registration. The MCP launcher
  (Claude Code, Codex CLI) is invoked from environments that may not
  inherit an interactive shell's PATH, so PATH-relative invocations break.
- Each client's installer is a pure function that takes its probe
  dependencies (subprocess runner, config path) as arguments so tests
  can drive without monkeypatching globals.
- Codex CLI's TOML config is edited via line-based block substitution
  rather than a TOML round-trip — re-serializing the whole file would
  drop user comments and reformat unrelated sections. Block-level
  substitution preserves everything outside ``[mcp_servers.<name>]``
  and its nested tables.
- ``--repair`` goes one step finer: it rewrites only the identity key
  tokens inside the target entry's env, so that entry's own ``args``,
  ``enabled``, ``startup_timeout_sec``, comments and value quoting also
  survive. The result is re-parsed and compared against the intended
  document before anything is written.
- ``--repair`` for Claude Code reads and writes Claude Code's MCP config
  JSON directly (``${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json`` for the
  ``user`` / ``local`` scopes, ``<cwd>/.mcp.json`` for ``project``).
  That file is authoritative: an external edit shows up in ``claude mcp
  get`` immediately, and the CLI itself rewrites it (taking its own
  ``backups/``) rather than owning a separate registry. Going through
  ``claude mcp get`` + ``remove`` + ``add`` instead is *lossy* and cannot
  be made lossless: ``Args:`` is printed space-joined so an argument
  containing a space cannot be recovered, and a multi-line env value's
  continuation lines print at column 0 byte-identically to unknown
  fields. Editing the JSON also removes the window where the entry is
  deleted but not yet re-added. See #279 / PR #287.
- That JSON edit is token-level too: the identity key tokens are replaced
  in the raw text and every other byte is copied through, so unknown
  numbers, escapes and formatting survive. The write itself follows the
  referent of a symlinked config, fails closed when another process
  changed the file in the meantime, keeps the original's mode/group and
  extended attributes (ACLs included, or fails closed when they cannot be
  carried over), validates the staged file before it lands, and fsyncs the
  directory — keeping the backup if anything goes wrong once the
  replacement is already live.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
from typing import Any, Callable


class MCPClient(str, Enum):
    """MCP clients that ``kioku-mesh mcp install`` supports."""

    CLAUDE_CODE = 'claude-code'
    CODEX_CLI = 'codex-cli'


# Family / client_id defaults per client. Users can override via --env if needed.
_DEFAULT_FAMILY: dict[MCPClient, str] = {
    MCPClient.CLAUDE_CODE: 'claude',
    MCPClient.CODEX_CLI: 'codex',
}
_DEFAULT_CLIENT_ID: dict[MCPClient, str] = {
    MCPClient.CLAUDE_CODE: 'claude-code',
    MCPClient.CODEX_CLI: 'codex-cli',
}

# Registration key default. Underscore form matches existing docs/mcp-clients.md
# examples and the most common existing installs in the wild; TOML accepts both
# without quoting so the choice is purely conventional.
DEFAULT_REGISTRY_NAME = 'kioku_mesh'

# Default Zenoh transport endpoint baked into installed env. Matches the same
# default used by store.py:get_session().
_DEFAULT_ZENOH_CONNECT = 'tcp/127.0.0.1:7447'


@dataclass(frozen=True)
class InstallPlan:
    """All the info one client installer needs.

    ``command`` is the absolute path to ``kioku-mesh-mcp``. ``env`` is the
    fully-resolved env block (defaults already merged with user overrides).

    ``args``, ``scope`` and ``transport`` only matter to the Claude Code
    path, where they become ``claude mcp add`` flags. ``--repair`` does not
    build a plan at all — it edits the config JSON in place — so these carry
    the fresh-install defaults only.
    """

    client: MCPClient
    name: str
    command: str
    env: dict[str, str] = field(default_factory=dict)
    args: tuple[str, ...] = ()
    scope: str = 'user'
    transport: str = 'stdio'


# TOML bare keys allow only ASCII letters, digits, underscore, and hyphen
# (per https://toml.io/en/v1.0.0#keys). A name with any other character —
# notably ``.`` — would either generate invalid TOML or, worse, silently
# rewrite the wrong table because ``[mcp_servers.foo.bar]`` is a nested
# table by spec. The same regex is the safest also-good-as-a-claude-MCP-name
# constraint (Claude Code's CLI doesn't formally publish a charset but ASCII
# alphanumerics + `_-` covers everything in docs/mcp-clients.md examples).
_VALID_REGISTRY_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _validate_registry_name(name: str) -> None:
    """Reject registry keys that wouldn't survive both TOML and Claude CLI safely.

    Surfaces the rejection as ``ValueError`` so the CLI layer maps it to a
    documented exit code rather than ``[mcp_servers.foo.bar]`` silently
    landing as a nested table in the user's Codex config (Codex review on
    #97).
    """
    if not name or not _VALID_REGISTRY_NAME_RE.fullmatch(name):
        raise ValueError(
            f'registry name {name!r} must match [A-Za-z0-9_-]+ '
            '(no dots, spaces, or other characters that break TOML bare keys).'
        )


def build_install_plan(
    client: MCPClient,
    name: str = DEFAULT_REGISTRY_NAME,
    *,
    extra_env: dict[str, str] | None = None,
    kioku_mesh_mcp_path: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> InstallPlan:
    """Resolve defaults into a fully-specified plan.

    Args:
        client: which MCP client to register with.
        name: registry key (e.g. ``kioku_mesh`` or ``kioku-mesh``).
        extra_env: extra env vars merged on top of the default kioku-mesh env.
        kioku_mesh_mcp_path: pin a specific binary path (for tests or non-PATH
            installs). When omitted, resolved via ``shutil.which``.
        which: PATH resolver, defaults to ``shutil.which``. Tests inject a fake.

    Raises:
        FileNotFoundError: when ``kioku-mesh-mcp`` can't be resolved.
        ValueError: when ``name`` is not a TOML / Claude-safe bare key.
    """
    _validate_registry_name(name)
    resolver = which or shutil.which
    command = kioku_mesh_mcp_path or resolver('kioku-mesh-mcp')
    if not command:
        raise FileNotFoundError(
            'kioku-mesh-mcp not on PATH. Install kioku-mesh first '
            '(`uv tool install kioku-mesh` from PyPI, or '
            '`uv tool install git+https://github.com/h-wata/kioku-mesh.git`).'
        )
    env: dict[str, str] = {
        'ZENOH_CONNECT': _DEFAULT_ZENOH_CONNECT,
        'KIOKU_MESH_AGENT_FAMILY': _DEFAULT_FAMILY[client],
        'KIOKU_MESH_CLIENT_ID': _DEFAULT_CLIENT_ID[client],
    }
    if extra_env:
        env.update(extra_env)
    return InstallPlan(client=client, name=name, command=command, env=env)


# -- Claude Code (via `claude mcp add`) ----------------------------------------


def _build_claude_add_command(claude_binary: str, plan: InstallPlan) -> list[str]:
    """Build the ``claude mcp add`` argv. Pure — for both dry-run and execution.

    ``-t`` is only emitted for non-stdio transports: stdio is the CLI default
    and omitting it keeps the fresh-install argv identical to what previous
    versions produced.
    """
    cmd: list[str] = [claude_binary, 'mcp', 'add', plan.name, '-s', plan.scope]
    if plan.transport and plan.transport != 'stdio':
        cmd.extend(['-t', plan.transport])
    for key, value in plan.env.items():
        cmd.extend(['-e', f'{key}={value}'])
    cmd.append('--')
    cmd.append(plan.command)
    cmd.extend(plan.args)
    return cmd


def install_claude_code(
    plan: InstallPlan,
    *,
    force: bool = False,
    dry_run: bool = False,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Register ``kioku-mesh-mcp`` with Claude Code.

    The CLI route via ``claude mcp add`` is the only registration path that
    Claude Code actually reads — entries under ``~/.claude/settings.json``
    are silently ignored. See ``docs/mcp-clients.md`` §Claude Code.

    Returns the status message printed to the user.
    """
    resolver = which or shutil.which
    claude = resolver('claude')
    if not claude:
        raise FileNotFoundError(
            'claude binary not on PATH. Install Claude Code first (https://docs.claude.com/en/docs/claude-code).'
        )
    cmd = _build_claude_add_command(claude, plan)
    if dry_run:
        return ' '.join(shlex.quote(part) for part in cmd)

    runner = run or _default_subprocess_run

    # Best-effort dedupe: if ``plan.name`` is already listed we either refuse
    # or remove-then-add for an idempotent --force replace. The `claude mcp
    # list` output is line-oriented "<name>: <command>" so a substring match
    # against ``plan.name + ':'`` is good enough — exact tokenization isn't
    # needed.
    list_result = runner([claude, 'mcp', 'list'])
    if list_result.returncode == 0 and f'{plan.name}:' in list_result.stdout:
        if not force:
            return f'error: {plan.name!r} is already registered with Claude Code. Use --force to overwrite.'
        # The remove step has to succeed before we re-add; if Claude refuses
        # (permission, state mismatch) we want the underlying error rather
        # than a confusing "claude mcp add failed" downstream (Codex review #97).
        remove_result = runner([claude, 'mcp', 'remove', plan.name])
        if remove_result.returncode != 0:
            stderr = (remove_result.stderr or '').strip() or '(no stderr)'
            raise RuntimeError(f'claude mcp remove {plan.name} failed (rc={remove_result.returncode}): {stderr}')

    result = runner(cmd)
    if result.returncode != 0:
        stderr = (result.stderr or '').strip()
        raise RuntimeError(f'claude mcp add failed (rc={result.returncode}): {stderr}')
    return f'registered {plan.name} with Claude Code via {claude}'


def _default_subprocess_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing stdout/stderr and never raising on non-zero rc."""
    return subprocess.run(argv, check=False, capture_output=True, text=True)


# -- Codex CLI (via ~/.codex/config.toml) --------------------------------------


def _default_codex_config_path() -> Path:
    """Return the Codex CLI config path. No env override exists upstream."""
    return Path.home() / '.codex' / 'config.toml'


_TOML_BASIC_STRING_ESCAPES = {
    '\\': '\\\\',
    '"': '\\"',
    '\b': '\\b',
    '\t': '\\t',
    '\n': '\\n',
    '\f': '\\f',
    '\r': '\\r',
}


def _toml_basic_string(value: str) -> str:
    r"""Render ``value`` as a TOML 1.0 basic string, escaping what the spec requires.

    Interpolating a raw value into ``"..."`` breaks on quotes and backslashes
    (a Windows path or a value containing ``"`` produced an unparseable file —
    Codex review B3 on #287). Control characters other than the ones with a
    short escape get the ``\\uXXXX`` form, which is the only legal spelling
    for them inside a basic string.
    """
    out = []
    for ch in value:
        escaped = _TOML_BASIC_STRING_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ch < ' ' or ch == '\x7f':
            out.append(f'\\u{ord(ch):04X}')
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def _render_codex_toml_block(plan: InstallPlan) -> str:
    """Render the TOML block for one ``[mcp_servers.<name>]`` entry.

    Inline-table form for env would be possible but the nested
    ``[mcp_servers.X.env]`` table matches the Codex CLI examples in the
    wild and is easier for users to edit by hand.
    """
    lines = [
        '# Added by `kioku-mesh mcp install --client codex-cli`. Re-run with --force to update.',
        f'[mcp_servers.{plan.name}]',
        f'command = {_toml_basic_string(plan.command)}',
        '',
        f'[mcp_servers.{plan.name}.env]',
    ]
    for key, value in plan.env.items():
        lines.append(f'{key} = {_toml_basic_string(value)}')
    return '\n'.join(lines)


def _replace_codex_block(existing: str, name: str, new_block: str) -> str:
    """Replace the ``[mcp_servers.<name>]`` block (plus nested tables) in-place.

    Block extent: from the first table header line that starts
    ``[mcp_servers.<name>]`` OR ``[mcp_servers.<name>.`` (any nested
    sub-table) through the line before the next non-matching ``[``-prefixed
    table header (or EOF). Lines in between belong to our block by TOML
    semantics.

    Comments / blank lines immediately preceding the block are NOT swept —
    they survive into the result so user annotations on adjacent sections
    don't get clobbered.
    """
    lines = existing.split('\n')
    span = _find_codex_block_span(lines, name)

    if span is None:
        # No existing block — fall through to append at end of file.
        suffix = '' if existing.endswith('\n') else '\n'
        return existing + suffix + '\n' + new_block + '\n'

    start_idx, end_idx = span
    new_lines = lines[:start_idx] + new_block.split('\n') + lines[end_idx:]
    return '\n'.join(new_lines)


def _find_codex_block_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """Return ``(start, end)`` line indices of the ``[mcp_servers.<name>]`` block.

    ``end`` is exclusive and points at the next unrelated table header (or
    EOF). Returns ``None`` when the entry has no table header of its own —
    e.g. it lives inside an inline ``[mcp_servers]`` table, a shape the
    line-oriented editors here deliberately refuse to touch.
    """
    server_header = f'[mcp_servers.{name}]'
    nested_prefix = f'[mcp_servers.{name}.'

    start_idx: int | None = None
    end_idx = len(lines)
    for i, raw in enumerate(lines):
        stripped = raw.lstrip()
        if stripped.startswith(server_header) or stripped.startswith(nested_prefix):
            if start_idx is None:
                start_idx = i
            continue
        if start_idx is not None and stripped.startswith('['):
            end_idx = i
            break

    if start_idx is None:
        return None
    return start_idx, end_idx


def install_codex_cli(
    plan: InstallPlan,
    *,
    force: bool = False,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> str:
    """Register ``kioku-mesh-mcp`` with Codex CLI by editing ``config.toml``.

    Codex CLI reads ``mcp_servers.<name>`` tables from its TOML config.
    There is no upstream CLI command analogous to ``claude mcp add``, so
    direct config edit is the documented path.
    """
    target = config_path or _default_codex_config_path()
    block = _render_codex_toml_block(plan)
    if dry_run:
        return f'# would write to {target}\n{block}'

    if target.exists():
        existing_text = target.read_text(encoding='utf-8')
        try:
            data = tomllib.loads(existing_text)
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(f'cannot parse {target} as TOML: {e}') from e
        already = data.get('mcp_servers', {}).get(plan.name) is not None
        if already and not force:
            return f'error: mcp_servers.{plan.name} already exists in {target}. Use --force to overwrite.'
        new_text = _replace_codex_block(existing_text, plan.name, block)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        new_text = block + '\n'

    target.write_text(new_text, encoding='utf-8')
    return f'wrote mcp_servers.{plan.name} to {target}'


# -- Public entry point (called by `kioku-mesh mcp install` handler) --------------


def install(
    client: MCPClient,
    *,
    name: str = DEFAULT_REGISTRY_NAME,
    extra_env: dict[str, str] | None = None,
    kioku_mesh_mcp_path: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Drive a client-specific installer with consistent error semantics."""
    plan = build_install_plan(
        client,
        name=name,
        extra_env=extra_env,
        kioku_mesh_mcp_path=kioku_mesh_mcp_path,
    )
    if client is MCPClient.CLAUDE_CODE:
        return install_claude_code(plan, force=force, dry_run=dry_run)
    if client is MCPClient.CODEX_CLI:
        return install_codex_cli(plan, force=force, dry_run=dry_run)
    raise ValueError(f'unsupported client: {client!r}')  # pragma: no cover


# -- --repair: overwrite retired MESH_MEM_* identity env in an existing entry ---

# Mirrors doctor.py's check_identity constants (kept as a small local copy
# rather than a cross-module import: doctor.py already imports from this
# module for _default_codex_config_path, and a second import direction back
# into doctor would be one more coupling than four constants are worth).
_LEGACY_ENV_PREFIX = 'MESH_MEM_'
_CURRENT_ENV_PREFIX = 'KIOKU_MESH_'
_IDENTITY_ENV_SUFFIXES = ('AGENT_FAMILY', 'CLIENT_ID')


def _identity_env_renames(env: dict[str, str]) -> dict[str, str]:
    """Return the ``{legacy_key: current_key}`` renames ``--repair`` would apply.

    Only the identity suffixes in :data:`_IDENTITY_ENV_SUFFIXES` are touched,
    and only when the current-prefix key is *absent* from ``env`` — the exact
    condition doctor.py's ``check_identity`` FAILs on. A config that already
    carries both names, or unrelated user env vars, yields no renames so
    ``--repair`` never rewrites anything the user didn't ask it to fix.
    """
    renames: dict[str, str] = {}
    for suffix in _IDENTITY_ENV_SUFFIXES:
        legacy_key = _LEGACY_ENV_PREFIX + suffix
        current_key = _CURRENT_ENV_PREFIX + suffix
        if legacy_key in env and current_key not in env:
            renames[legacy_key] = current_key
    return renames


def _repair_identity_env(env: dict[str, str]) -> dict[str, str]:
    """Apply :func:`_identity_env_renames` to ``env``, preserving key order."""
    renames = _identity_env_renames(env)
    if not renames:
        return dict(env)
    return {renames.get(key, key): value for key, value in env.items()}


def _rename_codex_env_key_line(line: str, legacy: str, current: str) -> tuple[str, int]:
    """Rename a bare or quoted ``legacy`` key at the head of a key/value line.

    Only the key token is touched — the ``=`` spacing, the value text (basic
    string, literal string, whatever the user wrote) and any trailing comment
    come through byte-for-byte, which is what makes this safe where
    re-rendering the block was not (Codex review B3 on #287). A quoted key
    keeps its quotes, so the rename really is the key name and nothing else
    (Codex review NB1 on #287).
    """
    pattern = re.compile(r'^(\s*)(["\']?)' + re.escape(legacy) + r'\2(\s*=)')
    return pattern.subn(lambda m: f'{m.group(1)}{m.group(2)}{current}{m.group(2)}{m.group(3)}', line, count=1)


def _rename_codex_env_key_inline(line: str, legacy: str, current: str) -> tuple[str, int]:
    """Rename ``legacy`` inside an inline ``env = { ... }`` table on one line.

    As with :func:`_rename_codex_env_key_line`, the surrounding quote style of
    the key token is preserved.
    """
    pattern = re.compile(r'(?<![A-Za-z0-9_.-])(["\']?)' + re.escape(legacy) + r'\1(\s*=)')
    return pattern.subn(lambda m: f'{m.group(1)}{current}{m.group(1)}{m.group(2)}', line, count=1)


_CODEX_INLINE_ENV_RE = re.compile(r'^\s*(?:env|"env"|\'env\')\s*=\s*\{')


def _rename_codex_env_keys(existing: str, name: str, renames: dict[str, str]) -> str:
    """Rewrite only the ``renames`` key tokens inside ``mcp_servers.<name>``'s env.

    Everything else in the file — including the entry's own ``args``,
    ``enabled``, ``startup_timeout_sec``, comments and formatting — is left
    untouched, because nothing is re-rendered.

    Raises:
        RuntimeError: when the entry has no table header of its own, or when a
            key that TOML parsing said is present cannot be located as a line
            we can safely edit. Both are fail-closed: the caller writes nothing.
    """
    lines = existing.split('\n')
    span = _find_codex_block_span(lines, name)
    if span is None:
        raise RuntimeError(
            f'could not locate a [mcp_servers.{name}] table header to edit; '
            'unsupported layout for --repair (edit the config by hand).'
        )
    start_idx, end_idx = span
    entry_header = f'[mcp_servers.{name}]'
    env_header = f'[mcp_servers.{name}.env]'
    pending = dict(renames)
    current_header = ''

    for i in range(start_idx, end_idx):
        raw = lines[i]
        stripped = raw.lstrip()
        if stripped.startswith('['):
            head, sep, _rest = stripped.partition(']')
            current_header = head + sep
            continue
        if not pending:
            break
        inline_env = current_header == entry_header and _CODEX_INLINE_ENV_RE.match(raw)
        if current_header != env_header and not inline_env:
            continue
        for legacy, current_key in list(pending.items()):
            if inline_env:
                new_line, hits = _rename_codex_env_key_inline(raw, legacy, current_key)
            else:
                new_line, hits = _rename_codex_env_key_line(raw, legacy, current_key)
            if hits:
                lines[i] = raw = new_line
                del pending[legacy]

    if pending:
        raise RuntimeError(
            f'could not locate env key(s) {sorted(pending)} as editable lines in '
            f'mcp_servers.{name}; unsupported layout for --repair (edit the config by hand).'
        )
    return '\n'.join(lines)


def repair_codex_cli(
    name: str = DEFAULT_REGISTRY_NAME,
    *,
    config_path: Path | None = None,
) -> str:
    """Overwrite retired identity env on an existing ``mcp_servers.<name>`` entry.

    Reads the entry straight out of the TOML (same file ``install_codex_cli``
    writes), applies :func:`_repair_identity_env` to its ``env`` table only,
    and rewrites *just the identity key tokens* in place — command, args,
    ``enabled``, ``startup_timeout_sec``, comments, formatting and every
    non-identity env value stay byte-for-byte what they were. The result is
    re-parsed and diffed against the intended document before anything is
    written, so a layout this editor mishandles fails closed instead of
    landing a broken config (Codex review B3 on #287).
    """
    target = config_path or _default_codex_config_path()
    if not target.is_file():
        return (
            f'error: {target} does not exist. Nothing to repair — '
            'run `kioku-mesh mcp install --client codex-cli` first.'
        )

    existing_text = target.read_text(encoding='utf-8')
    try:
        data = tomllib.loads(existing_text)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f'cannot parse {target} as TOML: {e}') from e

    entry = data.get('mcp_servers', {}).get(name)
    if entry is None:
        return (
            f'error: mcp_servers.{name} not found in {target}. Run `kioku-mesh mcp install --client codex-cli` first.'
        )

    env = entry.get('env', {}) or {}
    renames = _identity_env_renames(env)
    if not renames:
        return f'mcp_servers.{name} in {target} already uses the current KIOKU_MESH_* prefix; nothing to repair.'

    new_text = _rename_codex_env_keys(existing_text, name, renames)

    # Fail closed: the rewritten file must parse, and must differ from the
    # original in exactly the way we intended — nothing else.
    expected = copy.deepcopy(data)
    expected['mcp_servers'][name]['env'] = _repair_identity_env(env)
    try:
        actual = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f'refusing to write {target}: repaired content is not valid TOML: {e}') from e
    if actual != expected:
        raise RuntimeError(
            f'refusing to write {target}: repaired content does not match the intended '
            f'mcp_servers.{name} entry (unsupported layout for --repair).'
        )

    target.write_text(new_text, encoding='utf-8')
    return f'repaired identity env for mcp_servers.{name} in {target}'


@dataclass(frozen=True)
class ClaudeEntryLocation:
    """Where one Claude Code MCP registration physically lives.

    ``container`` is the key path of the ``mcpServers`` mapping that holds
    the entry inside ``path``'s JSON document, so a repair can walk back to
    the exact dict it read the entry from.
    """

    scope: str
    path: Path
    container: tuple[str, ...]
    document: dict[str, Any]
    raw_text: str

    def describe(self) -> str:
        """One-line, copy-pasteable description used in user-facing errors."""
        where = '.'.join(self.container)
        return f'{self.scope} scope ({self.path}, {where})'


def _claude_config_path() -> Path:
    """Return Claude Code's config JSON path.

    ``CLAUDE_CONFIG_DIR`` moves the whole file, not just a sibling of it
    (verified against Claude Code 2.1.227), so the fallback is ``$HOME``.
    """
    override = os.environ.get('CLAUDE_CONFIG_DIR')
    root = Path(override) if override else Path.home()
    return root / '.claude.json'


def _project_mcp_json_path(project_dir: Path) -> Path:
    """Return the ``project``-scope config path for ``project_dir``."""
    return project_dir / '.mcp.json'


def _reject_json_constant(token: str) -> Any:
    """``parse_constant`` hook that refuses JavaScript-only number tokens."""
    raise ValueError(f'non-standard JSON token {token!r}')


def _strict_json_loads(text: str) -> Any:
    """:func:`json.loads` without Python's ``NaN`` / ``Infinity`` extensions.

    Python's decoder accepts those tokens; Claude Code's does not, and rejects
    the whole file as corrupted when it meets one. Repair must therefore neither
    accept them as input nor emit them (Codex review B1 on #287).
    """
    return json.loads(text, parse_constant=_reject_json_constant)


def _load_json_document(path: Path) -> tuple[dict[str, Any], str] | None:
    """Read ``path`` as a JSON object. ``None`` when it doesn't exist.

    Raises:
        RuntimeError: when the file exists but isn't a JSON object, or is not
            strict JSON. Repair never guesses at a shape it doesn't recognize.
    """
    if not path.is_file():
        return None
    raw_text = path.read_text(encoding='utf-8')
    try:
        data = _strict_json_loads(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f'cannot parse {path} as JSON: {e}') from e
    if not isinstance(data, dict):
        raise RuntimeError(f'cannot repair {path}: expected a JSON object at the top level.')
    return data, raw_text


# --- Token-level JSON editing -------------------------------------------------
#
# The Claude Code repair rewrites the two identity env *key tokens* inside the
# raw file text and copies every other byte through, the same way the Codex CLI
# repair edits its TOML. Re-serializing the parsed document instead would
# normalize things the document is not ours to change: `1e400` becomes the
# non-standard `Infinity`, colon/comma spacing and compact containers get
# re-flowed, `\/` and `\uXXXX` escapes get rewritten (Codex review B1/B5 on
# #287).

_JSON_WHITESPACE = ' \t\n\r'

_JsonPath = tuple[Any, ...]


def _skip_json_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in _JSON_WHITESPACE:
        i += 1
    return i


def _scan_json_string(text: str, i: int) -> int:
    """Return the index just past the JSON string token starting at ``i``."""
    j = i + 1
    while True:
        char = text[j]
        if char == '\\':
            j += 2
            continue
        if char == '"':
            return j + 1
        j += 1


def _unsupported_json_layout(text: str, i: int) -> RuntimeError:
    return RuntimeError(f'unsupported JSON layout for --repair at offset {i}: {text[i : i + 20]!r}')


def _scan_json_value(text: str, i: int, path: _JsonPath, spans: dict[_JsonPath, tuple[int, int]]) -> int:
    i = _skip_json_ws(text, i)
    char = text[i]
    if char == '{':
        return _scan_json_object(text, i, path, spans)
    if char == '[':
        return _scan_json_array(text, i, path, spans)
    if char == '"':
        return _scan_json_string(text, i)
    j = i
    while j < len(text) and text[j] not in ',]}' and text[j] not in _JSON_WHITESPACE:
        j += 1
    if j == i:
        raise _unsupported_json_layout(text, i)
    return j


def _scan_json_object(text: str, i: int, path: _JsonPath, spans: dict[_JsonPath, tuple[int, int]]) -> int:
    i = _skip_json_ws(text, i + 1)
    if text[i] == '}':
        return i + 1
    while True:
        i = _skip_json_ws(text, i)
        if text[i] != '"':
            raise _unsupported_json_layout(text, i)
        key_start = i
        key_end = _scan_json_string(text, i)
        key = json.loads(text[key_start:key_end])
        member = path + (key,)
        if member in spans:
            # Duplicate keys are legal JSON but their meaning is reader-defined;
            # editing one of them is a guess, so fail closed instead.
            raise RuntimeError(
                f'refusing to edit a document with a duplicate JSON key '
                f'{".".join(str(part) for part in member)}; unsupported layout for --repair.'
            )
        spans[member] = (key_start, key_end)
        i = _skip_json_ws(text, key_end)
        if text[i] != ':':
            raise _unsupported_json_layout(text, i)
        i = _skip_json_ws(text, _scan_json_value(text, i + 1, member, spans))
        if text[i] == ',':
            i += 1
            continue
        if text[i] == '}':
            return i + 1
        raise _unsupported_json_layout(text, i)


def _scan_json_array(text: str, i: int, path: _JsonPath, spans: dict[_JsonPath, tuple[int, int]]) -> int:
    i = _skip_json_ws(text, i + 1)
    if text[i] == ']':
        return i + 1
    index = 0
    while True:
        i = _skip_json_ws(text, _scan_json_value(text, i, path + (index,), spans))
        if text[i] == ',':
            i += 1
            index += 1
            continue
        if text[i] == ']':
            return i + 1
        raise _unsupported_json_layout(text, i)


def _json_key_spans(text: str) -> dict[_JsonPath, tuple[int, int]]:
    """Map every object member's key path to its ``[start, end)`` span in ``text``.

    Array hops appear in a path as integer indices. ``text`` is assumed to have
    already parsed as strict JSON; anything this scanner cannot follow raises
    ``RuntimeError`` so the caller writes nothing.
    """
    spans: dict[_JsonPath, tuple[int, int]] = {}
    try:
        end = _scan_json_value(text, 0, (), spans)
        if _skip_json_ws(text, end) != len(text):
            raise RuntimeError('trailing content after the JSON document; unsupported layout for --repair.')
    except (IndexError, ValueError) as e:
        raise RuntimeError(f'cannot locate the JSON keys to edit: {e}') from e
    return spans


def _rename_claude_env_keys(raw_text: str, *, container: tuple[str, ...], name: str, renames: dict[str, str]) -> str:
    """Rewrite only the ``renames`` key tokens of ``<container>.<name>.env``.

    Every other byte of ``raw_text`` — indentation, spacing, escapes, number
    spelling, unknown fields, key order — comes through untouched, because
    nothing is re-serialized.

    Raises:
        RuntimeError: when a key the parsed document says is present cannot be
            located as an editable token. Fail closed: the caller writes nothing.
    """
    spans = _json_key_spans(raw_text)
    env_path: _JsonPath = tuple(container) + (name, 'env')
    edits: list[tuple[tuple[int, int], str]] = []
    for legacy, current in renames.items():
        span = spans.get(env_path + (legacy,))
        if span is None:
            raise RuntimeError(
                f'could not locate the {legacy!r} env key token to edit in place; '
                'unsupported layout for --repair (edit the config by hand).'
            )
        edits.append((span, json.dumps(current, ensure_ascii=False)))

    out = raw_text
    for (start, end), token in sorted(edits, key=lambda edit: edit[0][0], reverse=True):
        out = out[:start] + token + out[end:]
    return out


def _dig(document: dict[str, Any], container: tuple[str, ...]) -> dict[str, Any] | None:
    """Follow ``container`` through ``document``; ``None`` if any hop is missing."""
    node: Any = document
    for key in container:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def _require_dig(document: dict[str, Any], container: tuple[str, ...]) -> dict[str, Any]:
    """:func:`_dig` for a path a caller already knows exists.

    Raises ``RuntimeError`` rather than asserting so the guarantee holds under
    ``python -O`` too.
    """
    node = _dig(document, container)
    if node is None:
        raise RuntimeError(f'internal error: {".".join(container)} disappeared from the config document.')
    return node


def _find_claude_entries(
    name: str,
    *,
    config_path: Path,
    project_dir: Path,
) -> list[ClaudeEntryLocation]:
    """Locate every scope that registers ``name``, newest-to-oldest precedence.

    Scope layout (measured against Claude Code 2.1.227):

    - ``local``   -> ``<config>.projects["<cwd>"].mcpServers``
    - ``project`` -> ``<cwd>/.mcp.json`` ``.mcpServers``
    - ``user``    -> ``<config>.mcpServers``

    All three are searched even after a hit: a name registered in more than
    one scope is exactly the case the caller must refuse rather than guess
    at.
    """
    found: list[ClaudeEntryLocation] = []
    main = _load_json_document(config_path)
    if main is not None:
        document, raw_text = main
        for scope, container in (
            ('local', ('projects', str(project_dir), 'mcpServers')),
            ('user', ('mcpServers',)),
        ):
            servers = _dig(document, container)
            if servers is not None and name in servers:
                found.append(
                    ClaudeEntryLocation(
                        scope=scope,
                        path=config_path,
                        container=container,
                        document=document,
                        raw_text=raw_text,
                    )
                )

    project_path = _project_mcp_json_path(project_dir)
    project_doc = _load_json_document(project_path)
    if project_doc is not None:
        document, raw_text = project_doc
        servers = _dig(document, ('mcpServers',))
        if servers is not None and name in servers:
            found.append(
                ClaudeEntryLocation(
                    scope='project',
                    path=project_path,
                    container=('mcpServers',),
                    document=document,
                    raw_text=raw_text,
                )
            )
    return found


def _new_backup_path(path: Path) -> Path:
    """Return an unused ``<file>.bak-<UTC timestamp>`` sibling of ``path``.

    Timestamped rather than a plain ``<file>.bak``: a hand-made
    ``~/.claude.json.bak`` is a common thing to find in a real home directory,
    and a backup that overwrites the user's own backup is worse than none.
    The suffix counter only matters for two repairs inside one second.
    """
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    candidate = path.with_name(f'{path.name}.bak-{stamp}')
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f'{path.name}.bak-{stamp}-{counter}')
        counter += 1
    return candidate


def _resolve_config_target(path: Path) -> Path:
    """Return the real file behind ``path``, so a symlinked config keeps its link.

    ``os.replace`` on a symlink swaps the *link* for a regular file, which
    silently detaches the config the user actually maintains from the one the
    tool now writes (Codex review B3 on #287). Writing the referent instead
    leaves the link topology exactly as it was.

    Raises:
        RuntimeError: when the link does not lead to a regular file — a fifo or
            device is not something --repair should be replacing.
    """
    if not path.is_symlink():
        return path
    referent = Path(os.path.realpath(path))
    if not stat.S_ISREG(os.lstat(referent).st_mode):
        raise RuntimeError(f'refusing to repair {path}: it points at {referent}, which is not a regular file.')
    return referent


def _assert_config_unchanged(path: Path, original_text: str, *, stage: str) -> None:
    """Fail closed when ``path`` no longer holds the bytes --repair read.

    Claude Code rewrites its own config while it runs, so "read, edit, write
    back" without a concurrency check silently drops whatever the other writer
    added in between (Codex review B2 on #287). This is the optimistic half of
    a compare-and-swap: it is checked immediately before the backup and again
    immediately before the atomic replace.
    """
    try:
        current = path.read_text(encoding='utf-8')
    except OSError as e:
        raise RuntimeError(
            f'refusing to write {path}: it could not be re-read {stage} ({e}); nothing was written.'
        ) from e
    if current != original_text:
        raise RuntimeError(
            f'refusing to write {path}: it changed on disk {stage}, so writing would drop that '
            'change. Nothing was written — re-run --repair.'
        )


def _write_backup(backup: Path, text: str, *, mode: int) -> None:
    """Create ``backup`` exclusively at ``mode`` and fsync it.

    ``O_EXCL`` keeps two repairs in the same second from writing the same file,
    and the explicit mode keeps a 0600 config's secrets from being copied into a
    umask-wide 0644 backup (Codex review B4 on #287).
    """
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        backup.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry so the replaced name survives a power cut."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ENOTSUP and EOPNOTSUPP are the same number on Linux but not everywhere; ENOSYS
# is what a kernel without the syscall answers. All three mean "this filesystem
# or platform has no extended attributes", which is not a loss — there is
# nothing on the original to carry over.
_XATTR_UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})


def _copy_xattrs(source: Path, target_fd: int, *, destination: Path) -> None:
    """Carry ``source``'s extended attributes onto the staged replacement.

    :func:`os.replace` gives the destination the *staged* file's metadata, so
    anything carried as an xattr — POSIX ACLs (``system.posix_acl_access``),
    SELinux labels (``security.selinux``), user annotations — is dropped unless
    it is copied across first (Codex review B4 on #287).

    Call this *after* the mode is set: ``fchmod`` rewrites the ACL mask to the
    mode's group bits, so a chmod that lands on top of a freshly copied ACL
    narrows (or widens) every named entry it just restored.

    Fail closed rather than warn-and-continue when an attribute cannot be
    carried: an ACL that quietly disappears changes who may read a config
    holding identity env, and nothing in the output would say so. The one case
    that is *not* a loss is an attribute the kernel already put on the staged
    file with the same value — a ``security.selinux`` label inherited from the
    directory's default type transition is unsettable by an unprivileged
    process yet identical, so it is compared before giving up. Without that
    comparison ``--repair`` would fail on every SELinux host.

    Raises:
        RuntimeError: when ``source`` has an attribute the staged file neither
            has nor accepts. Raised before the replace, so nothing is written.
    """
    try:
        names = os.listxattr(source)
    except AttributeError:  # pragma: no cover - platform without xattr support
        return
    except OSError as e:
        if e.errno in _XATTR_UNSUPPORTED:
            return
        raise
    for name in names:
        try:
            value = os.getxattr(source, name)
        except OSError as e:
            if e.errno == errno.ENODATA:  # pragma: no cover - removed mid-repair
                continue
            raise
        try:
            os.setxattr(target_fd, name, value)
        except OSError as e:
            try:
                staged = os.getxattr(target_fd, name)
            except OSError:
                staged = None
            if staged == value:
                continue
            raise RuntimeError(
                f'refusing to write {destination}: its extended attribute {name!r} cannot be carried '
                f'onto the replacement ({e}), and replacing the file would drop it. Nothing was written '
                '— copy the attribute by hand, or remove it if it is no longer wanted, then re-run --repair.'
            ) from e


def _validate_pending_write(tmp_path: Path, new_text: str, expected: dict[str, Any], *, destination: Path) -> None:
    """Check the staged file *before* it lands, so no unverified config is ever live.

    Validating only after :func:`os.replace` leaves a crash window in which the
    destination holds bytes nothing has vouched for (Codex review B5 on #287).
    """
    staged = tmp_path.read_text(encoding='utf-8')
    if staged != new_text:
        raise RuntimeError(f'refusing to write {destination}: the staged file is not what --repair rendered.')
    try:
        parsed = _strict_json_loads(staged)
    except ValueError as e:
        raise RuntimeError(f'refusing to write {destination}: the repaired content is not strict JSON ({e}).') from e
    if parsed != expected:
        raise RuntimeError(
            f'refusing to write {destination}: the repaired content does not match the intended document.'
        )


def _write_json_atomically(path: Path, new_text: str, *, original_text: str, expected: dict[str, Any]) -> Path:
    """Back up ``path``, then replace it with ``new_text`` atomically.

    The new content goes to a temporary file in the same directory, is
    validated there, and lands via :func:`os.replace` followed by a directory
    fsync — so a crash leaves either the untouched original or a config that has
    already been checked. Returns the backup path.

    ``path`` must already be the real file (see :func:`_resolve_config_target`).
    On any failure before the replace, the original file and the caller's
    expectations are left untouched and the backup is cleaned up. Once the
    replace has happened the backup is *kept* whatever goes wrong afterwards:
    it is then the only copy of the pre-repair config, and the failure the
    caller is about to see is the reason someone might want it back.
    """
    _assert_config_unchanged(path, original_text, stage='after --repair read it')
    st = os.stat(path)
    original_mode = stat.S_IMODE(st.st_mode)

    backup = _new_backup_path(path)
    _write_backup(backup, original_text, mode=original_mode & 0o600)

    tmp_path: Path | None = None
    replaced = False
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
            # The replacement inherits the original's permissions and group
            # rather than mkstemp's 0600 and the caller's primary group.
            if os.fstat(handle.fileno()).st_gid != st.st_gid:
                os.fchown(handle.fileno(), -1, st.st_gid)
            os.fchmod(handle.fileno(), original_mode)
            # After the chmod rather than before, because fchmod rewrites the
            # ACL mask. Defensive rather than load-bearing here: the mode being
            # applied is the original's own, and the kernel already keeps that
            # in sync with the original's mask.
            _copy_xattrs(path, handle.fileno(), destination=path)
        _validate_pending_write(tmp_path, new_text, expected, destination=path)
        _assert_config_unchanged(path, original_text, stage='while --repair was staging the new file')
        os.replace(tmp_path, path)
        tmp_path = None
        replaced = True
        try:
            _fsync_directory(path.parent)
        except OSError as e:
            raise RuntimeError(
                f'{path} now holds the repaired content, but its directory entry could not be flushed '
                f'to disk ({e}), so the replacement is live yet not durably confirmed — a crash or power '
                f'cut could still bring the old name back. The pre-repair copy is kept at {backup}.'
            ) from e
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if not replaced:
            # Nothing landed, so the backup is a copy of a file that is still
            # there; keeping it would only litter the directory.
            backup.unlink(missing_ok=True)
        raise
    return backup


def _verify_written_document(path: Path, expected: dict[str, Any], *, backup: Path, written_text: str) -> None:
    """Re-read ``path`` and confirm it is exactly what was written, or raise.

    Declaring "nothing else changed" in a docstring is not a guarantee; reading
    the file back is. A file that is ours but wrong is rolled back; a file some
    *other* writer has since changed is left alone and reported, because putting
    a stale backup over a newer config would destroy that writer's update.
    """

    def restore(reason: str) -> None:
        try:
            os.replace(backup, path)
        except OSError as e:  # pragma: no cover - restore itself failing is rare
            raise RuntimeError(
                f'refusing to keep the repaired {path}: {reason}; restoring the original ALSO failed ({e}). '
                f'The pre-repair copy is at {backup}.'
            ) from e
        raise RuntimeError(f'refusing to keep the repaired {path}: {reason}; restored the original.')

    try:
        current = path.read_text(encoding='utf-8')
    except OSError as e:
        restore(f'it could not be read back ({e})')
        return  # pragma: no cover - restore always raises
    if current != written_text:
        raise RuntimeError(
            f'refusing to vouch for {path}: it changed again right after --repair replaced it, so it was '
            f'left as it is now. The pre-repair copy is kept at {backup}.'
        )
    try:
        written = _strict_json_loads(current)
    except ValueError as e:
        restore(f'it could not be read back ({e})')
        return  # pragma: no cover - restore always raises
    if written != expected:
        restore('the written document does not match the intended one')


def repair_claude_code(
    name: str = DEFAULT_REGISTRY_NAME,
    *,
    config_path: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Overwrite retired identity env on an existing Claude Code registration.

    Reads the entry out of Claude Code's own config JSON, renames only the
    keys :func:`_identity_env_renames` selects, and writes the document back.
    ``command``, ``args``, every other env var, unknown fields and key order
    all survive because nothing is re-derived — the loaded ``dict`` is
    handed back with two keys renamed inside one ``env`` mapping.

    The JSON is the authoritative store (an external edit is visible to
    ``claude mcp get`` immediately) and it is the *only* lossless source:
    ``claude mcp get``'s text output space-joins ``args`` and prints
    multi-line env continuations indistinguishably from unknown fields, so
    the previous ``get`` + ``remove`` + ``add`` route silently rewrote
    registrations whose args contained spaces. Editing in place also drops
    the window where the entry was removed but not yet re-added.

    The edit itself is made on the raw file text — only the identity key
    tokens are rewritten — so number spelling, escapes, spacing and compact
    containers stay byte-for-byte what the file had. Re-serializing the parsed
    document instead turned a valid ``1e400`` into the non-standard
    ``Infinity`` that Claude Code refuses to load, and reflowed formatting that
    was not ours to change.

    Safety: a symlinked config is written through to its referent so the link
    survives; the file is checked for concurrent modification both before the
    backup and immediately before the replace (fail closed, nothing written);
    the original is copied to ``<file>.bak-<timestamp>`` created exclusively at
    no wider than 0600; the new text is validated as strict JSON matching the
    intended document *while still on the temp file*, then lands via
    :func:`os.replace` (preserving the original's mode, group and extended
    attributes — ACLs included — or failing closed when an attribute cannot be
    carried over) followed by a directory fsync; and it is finally re-read to
    confirm what is live is what was written. Once the replace has happened the
    backup is kept whatever fails afterwards, so a durability error still
    leaves the pre-repair copy on disk.

    Raises:
        RuntimeError: when ``name`` is registered in more than one scope
            (fail closed — the right one is not guessable), when a config
            file is unparseable, or when the written document does not match
            the intended one.
    """
    target = config_path or _claude_config_path()
    cwd = project_dir or Path.cwd()

    locations = _find_claude_entries(name, config_path=target, project_dir=cwd)
    if not locations:
        return (
            f'error: {name!r} is not registered in {target} (user or local scope) '
            f'or {_project_mcp_json_path(cwd)} (project scope). '
            'Run `kioku-mesh mcp install --client claude-code` first.'
        )
    if len(locations) > 1:
        listed = '\n'.join(f'  - {loc.describe()}' for loc in locations)
        raise RuntimeError(
            f'refusing to repair {name!r}: it is registered in {len(locations)} scopes and '
            f'--repair will not guess which one you meant:\n{listed}\n'
            'Nothing was changed. Remove the registrations you do not want '
            f'(`claude mcp remove {name} -s <scope>`) so exactly one is left, then re-run '
            '--repair; or rename the MESH_MEM_* identity keys by hand in the file(s) above.'
        )

    location = locations[0]
    servers = _require_dig(location.document, location.container)
    entry = servers[name]
    if not isinstance(entry, dict):
        raise RuntimeError(
            f'refusing to repair {name!r} in {location.path}: expected a JSON object for the entry, '
            f'got {type(entry).__name__}.'
        )
    env = entry.get('env')
    if env is None:
        env = {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise RuntimeError(f'refusing to repair {name!r} in {location.path}: its "env" is not a mapping of strings.')

    renames = _identity_env_renames(env)
    if not renames:
        return (
            f'{name!r} in {location.path} ({location.scope} scope) already uses the current '
            'KIOKU_MESH_* prefix; nothing to repair.'
        )

    expected = copy.deepcopy(location.document)
    expected_servers = _require_dig(expected, location.container)
    expected_servers[name] = dict(entry)
    expected_servers[name]['env'] = _repair_identity_env(env)

    new_text = _rename_claude_env_keys(
        location.raw_text,
        container=location.container,
        name=name,
        renames=renames,
    )

    write_target = _resolve_config_target(location.path)
    backup = _write_json_atomically(write_target, new_text, original_text=location.raw_text, expected=expected)
    _verify_written_document(write_target, expected, backup=backup, written_text=new_text)
    return (
        f'repaired identity env for {name!r} in {location.path} ({location.scope} scope); '
        f'previous file kept at {backup}'
    )


def repair(
    client: MCPClient,
    *,
    name: str = DEFAULT_REGISTRY_NAME,
    config_path: Path | None = None,
    project_dir: Path | None = None,
) -> str:
    """Drive a client-specific identity-env repair with consistent error semantics.

    ``config_path`` is that client's config file (Claude Code's ``.claude.json``
    or Codex CLI's ``config.toml``); ``project_dir`` only matters to Claude
    Code, whose ``local`` / ``project`` scopes are keyed by the working
    directory.
    """
    if client is MCPClient.CLAUDE_CODE:
        return repair_claude_code(name, config_path=config_path, project_dir=project_dir)
    if client is MCPClient.CODEX_CLI:
        return repair_codex_cli(name, config_path=config_path)
    raise ValueError(f'unsupported client: {client!r}')  # pragma: no cover


def parse_env_pairs(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from --env flags; raise on malformed input."""
    out: dict[str, str] = {}
    for raw in pairs:
        if '=' not in raw:
            raise ValueError(f'--env value must be KEY=VALUE: {raw!r}')
        key, _, value = raw.partition('=')
        key = key.strip()
        if not key:
            raise ValueError(f'--env key cannot be empty: {raw!r}')
        out[key] = value
    return out

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
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from enum import Enum
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib
from typing import Callable


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
    path. They carry the defaults a fresh install uses; ``--repair`` fills
    them from the entry it read back so a re-registration reproduces the
    original registration instead of flattening it (Codex review B1 on #287).
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
class ClaudeEntry:
    """Everything ``claude mcp get`` tells us about one registration.

    Fields are ``None`` when the output didn't carry them at all, which is
    what :func:`_claude_entry_restore_blockers` turns into a refusal — a
    field we didn't read is a field we can't put back.
    """

    command: str
    scope: str | None
    transport: str | None
    args: tuple[str, ...] | None
    env: dict[str, str]
    problems: tuple[str, ...] = ()


# Claude Code prints ``Scope: User config (available in all your projects)``;
# the CLI flag wants the bare word. Anything else is unmapped on purpose.
_CLAUDE_SCOPE_WORDS = {'local': 'local', 'user': 'user', 'project': 'project'}

# Env entries are printed indented as ``KEY=VALUE``. A value containing
# newlines is printed verbatim, so its continuation lines land unindented at
# column 0 (both verified against Claude Code 2.1.227). Those two shapes are
# the only ones we know how to read back; see :func:`_classify_claude_env_line`.
_CLAUDE_ENV_KEY_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$')

_CLAUDE_GET_TRAILER = 'To remove this server, run:'


def _classify_claude_env_line(line: str, *, last_key: str | None) -> tuple[str, re.Match[str] | None]:
    """Decide what one line inside the ``Environment:`` section is.

    Returns ``('key', match)`` for an entry, ``('continuation', None)`` for a
    line that belongs to the previous value, or ``('unknown', None)`` for a
    shape we have never observed. Only the two shapes Claude Code actually
    prints are accepted; everything else is ``unknown`` and makes the caller
    stop *before* the destructive remove (Codex review B1-R1 on #287).

    Guessing the other way — "not a ``KEY=`` line, so it must be a
    continuation" — is what let a newly added indented field be appended to
    the preceding env value and written back by the repair's add.
    """
    match = _CLAUDE_ENV_KEY_RE.match(line)
    indented = line[:1].isspace()
    if match and indented:
        return 'key', match
    if match:
        # Column 0 is where continuation lines live, so a ``KEY=`` there is
        # only unambiguous while no value is open.
        return ('key', match) if last_key is None else ('unknown', None)
    if last_key is not None and not indented:
        return 'continuation', None
    return 'unknown', None


def _parse_claude_mcp_get(output: str) -> ClaudeEntry | None:
    """Parse ``claude mcp get <name>`` text output into a :class:`ClaudeEntry`.

    The format is indented ``Key: value`` lines (``Scope``, ``Type``,
    ``Command``, ``Args``) followed by an ``Environment:`` section whose
    children are bare ``KEY=VALUE`` lines; there is no ``--json`` output for
    this subcommand. Returns ``None`` when no ``Command:`` line is found — an
    unrecognized shape we should not guess about rather than silently repair
    against.

    The env section runs to the first blank line (or the ``To remove this
    server`` trailer). Inside it only the two shapes Claude Code is known to
    print are accepted — an indented ``KEY=VALUE`` entry and an unindented
    continuation of the previous value — which keeps values ending in ``:``
    and multi-line values intact (Codex review B1 on #287) without treating
    an unfamiliar line as value text (B1-R1). Anything else becomes a
    ``problem`` so the repair refuses before removing the registration.
    """
    command: str | None = None
    scope_raw: str | None = None
    transport: str | None = None
    args_raw: str | None = None
    env: dict[str, str] = {}
    problems: list[str] = []

    lines = output.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if stripped == 'Environment:':
            i += 1
            last_key: str | None = None
            while i < len(lines):
                cur = lines[i]
                if not cur.strip() or cur.lstrip().startswith(_CLAUDE_GET_TRAILER):
                    break
                kind, match = _classify_claude_env_line(cur, last_key=last_key)
                if kind == 'key' and match is not None:
                    last_key = match.group(1)
                    env[last_key] = match.group(2)
                elif kind == 'continuation' and last_key is not None:
                    env[last_key] += '\n' + cur
                else:
                    problems.append(f'unrecognized Environment line: {cur!r}')
                i += 1
            continue
        if stripped.startswith('Command:'):
            command = stripped[len('Command:') :].strip()
        elif stripped.startswith('Scope:'):
            scope_raw = stripped[len('Scope:') :].strip()
        elif stripped.startswith('Type:'):
            transport = stripped[len('Type:') :].strip()
        elif stripped.startswith('Args:'):
            args_raw = stripped[len('Args:') :].strip()
        i += 1

    if command is None:
        return None

    scope: str | None = None
    if scope_raw is not None:
        first_word = scope_raw.split()[0].lower() if scope_raw.split() else ''
        scope = _CLAUDE_SCOPE_WORDS.get(first_word)
        if scope is None:
            problems.append(f'unrecognized Scope: {scope_raw!r}')
    args = None if args_raw is None else tuple(args_raw.split())
    return ClaudeEntry(
        command=command,
        scope=scope,
        transport=transport,
        args=args,
        env=env,
        problems=tuple(problems),
    )


def _claude_entry_restore_blockers(entry: ClaudeEntry) -> list[str]:
    """Return reasons ``entry`` could not be re-registered exactly as it stands.

    ``--repair`` has to delete the registration before it can re-create it,
    so anything we cannot reproduce has to stop the operation *before* the
    remove rather than surface as a silently downgraded entry.
    """
    blockers = list(entry.problems)
    if not entry.command:
        blockers.append('Command is empty')
    if entry.scope is None:
        blockers.append('Scope is missing or unrecognized')
    if entry.transport is None:
        blockers.append('Type (transport) is missing')
    elif entry.transport != 'stdio':
        blockers.append(f'transport {entry.transport!r} is not stdio; re-registering it is not supported')
    if entry.args is None:
        blockers.append('Args line is missing')
    return blockers


def repair_claude_code(
    name: str = DEFAULT_REGISTRY_NAME,
    *,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Overwrite retired identity env on an existing Claude Code registration.

    Claude Code only exposes its MCP config through the ``claude mcp`` CLI
    (direct edits to ``~/.claude.json`` are not a supported path — see the
    module docstring), so repair reads the current entry via ``claude mcp
    get``, applies :func:`_repair_identity_env` to its env only, and
    re-registers with the merged env via remove+add — the same primitives
    ``install_claude_code``'s ``--force`` path already uses. Unlike
    ``--force``, the env sent to ``add`` is the *previous* env with only the
    identity keys renamed, so unrelated ``-e`` values the user set survive,
    and so do the entry's scope and args.

    There is no delete-free update route in the Claude CLI (``claude mcp``
    offers add / add-json / remove and no update), so the remove+add window
    is unavoidable. It is made survivable instead: the full pre-remove entry
    is kept and a failed ``add`` triggers a rollback ``add`` of the original
    (Codex review B2 on #287).
    """
    resolver = which or shutil.which
    claude = resolver('claude')
    if not claude:
        raise FileNotFoundError(
            'claude binary not on PATH. Install Claude Code first (https://docs.claude.com/en/docs/claude-code).'
        )
    runner = run or _default_subprocess_run

    get_result = runner([claude, 'mcp', 'get', name])
    if get_result.returncode != 0:
        stderr = (get_result.stderr or '').strip() or '(no stderr)'
        return (
            f'error: {name!r} is not registered with Claude Code ({stderr}). '
            'Run `kioku-mesh mcp install --client claude-code` first.'
        )

    entry = _parse_claude_mcp_get(get_result.stdout)
    if entry is None:
        raise RuntimeError(f'could not parse `claude mcp get {name}` output; unrecognized format for --repair.')

    if not _identity_env_renames(entry.env):
        return f'{name!r} already uses the current KIOKU_MESH_* prefix; nothing to repair.'

    blockers = _claude_entry_restore_blockers(entry)
    if blockers:
        raise RuntimeError(
            f'refusing to repair {name!r}: `claude mcp get {name}` output cannot be reproduced '
            f'losslessly ({"; ".join(blockers)}). Nothing was removed — rename the '
            'MESH_MEM_* identity env by hand, or re-run `kioku-mesh mcp install '
            '--client claude-code --force`.'
        )

    original_plan = InstallPlan(
        client=MCPClient.CLAUDE_CODE,
        name=name,
        command=entry.command,
        env=dict(entry.env),
        args=entry.args or (),
        scope=entry.scope or 'user',
        transport=entry.transport or 'stdio',
    )
    repaired_plan = replace(original_plan, env=_repair_identity_env(entry.env))

    remove_result = runner([claude, 'mcp', 'remove', name, '-s', original_plan.scope])
    if remove_result.returncode != 0:
        stderr = (remove_result.stderr or '').strip() or '(no stderr)'
        raise RuntimeError(f'claude mcp remove {name} failed (rc={remove_result.returncode}): {stderr}')

    add_result = runner(_build_claude_add_command(claude, repaired_plan))
    if add_result.returncode != 0:
        stderr = (add_result.stderr or '').strip() or '(no stderr)'
        rollback_argv = _build_claude_add_command(claude, original_plan)
        rollback_result = runner(rollback_argv)
        if rollback_result.returncode == 0:
            raise RuntimeError(
                f'claude mcp add failed (rc={add_result.returncode}): {stderr}. '
                f'The previous {name!r} entry was restored unchanged; nothing was lost.'
            )
        rollback_stderr = (rollback_result.stderr or '').strip() or '(no stderr)'
        restore_cmd = ' '.join(shlex.quote(part) for part in rollback_argv)
        raise RuntimeError(
            f'claude mcp add failed (rc={add_result.returncode}): {stderr}. '
            f'Rollback of the previous entry ALSO failed (rc={rollback_result.returncode}): '
            f'{rollback_stderr}. {name!r} is currently unregistered — restore it with: {restore_cmd}'
        )
    return f'repaired identity env for {name!r} in Claude Code'


def repair(
    client: MCPClient,
    *,
    name: str = DEFAULT_REGISTRY_NAME,
    config_path: Path | None = None,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Drive a client-specific identity-env repair with consistent error semantics."""
    if client is MCPClient.CLAUDE_CODE:
        return repair_claude_code(name, run=run, which=which)
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

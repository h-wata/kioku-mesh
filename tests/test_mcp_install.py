"""Tests for `mesh-mem mcp install` (#85).

Both client paths are driven by injecting probes (``which``, subprocess
``run``, config path) so the suite never reaches out to a real Claude Code
or Codex CLI install.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib
from typing import Callable

import pytest

from kioku_mesh import mcp_install
from kioku_mesh.__main__ import main as cli_main
from kioku_mesh.mcp_install import _build_claude_add_command
from kioku_mesh.mcp_install import _parse_claude_mcp_get
from kioku_mesh.mcp_install import _render_codex_toml_block
from kioku_mesh.mcp_install import _repair_identity_env
from kioku_mesh.mcp_install import _replace_codex_block
from kioku_mesh.mcp_install import build_install_plan
from kioku_mesh.mcp_install import DEFAULT_REGISTRY_NAME
from kioku_mesh.mcp_install import install_claude_code
from kioku_mesh.mcp_install import install_codex_cli
from kioku_mesh.mcp_install import InstallPlan
from kioku_mesh.mcp_install import MCPClient
from kioku_mesh.mcp_install import parse_env_pairs
from kioku_mesh.mcp_install import repair
from kioku_mesh.mcp_install import repair_claude_code
from kioku_mesh.mcp_install import repair_codex_cli

# -- parse_env_pairs ------------------------------------------------------------


def test_parse_env_pairs_basic() -> None:
    assert parse_env_pairs(['A=1', 'B=2']) == {'A': '1', 'B': '2'}


def test_parse_env_pairs_preserves_value_equals() -> None:
    assert parse_env_pairs(['A=1=2']) == {'A': '1=2'}


def test_parse_env_pairs_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match='KEY=VALUE'):
        parse_env_pairs(['nope'])


def test_parse_env_pairs_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match='empty'):
        parse_env_pairs(['=value'])


# -- build_install_plan ---------------------------------------------------------


def test_build_install_plan_defaults_per_client() -> None:
    plan = build_install_plan(MCPClient.CODEX_CLI, kioku_mesh_mcp_path='/x/mesh-mem-mcp')
    assert plan.client is MCPClient.CODEX_CLI
    assert plan.name == DEFAULT_REGISTRY_NAME
    assert plan.command == '/x/mesh-mem-mcp'
    assert plan.env['KIOKU_MESH_AGENT_FAMILY'] == 'codex'
    assert plan.env['KIOKU_MESH_CLIENT_ID'] == 'codex-cli'
    assert plan.env['ZENOH_CONNECT'] == 'tcp/127.0.0.1:7447'


def test_build_install_plan_claude_defaults() -> None:
    plan = build_install_plan(MCPClient.CLAUDE_CODE, kioku_mesh_mcp_path='/x/mesh-mem-mcp')
    assert plan.env['KIOKU_MESH_AGENT_FAMILY'] == 'claude'
    assert plan.env['KIOKU_MESH_CLIENT_ID'] == 'claude-code'


def test_build_install_plan_extra_env_overrides_default() -> None:
    plan = build_install_plan(
        MCPClient.CODEX_CLI,
        kioku_mesh_mcp_path='/x/mesh-mem-mcp',
        extra_env={'KIOKU_MESH_AGENT_FAMILY': 'custom', 'EXTRA': 'value'},
    )
    assert plan.env['KIOKU_MESH_AGENT_FAMILY'] == 'custom'
    assert plan.env['EXTRA'] == 'value'


def test_build_install_plan_raises_when_binary_missing() -> None:
    with pytest.raises(FileNotFoundError, match='kioku-mesh-mcp'):
        build_install_plan(MCPClient.CODEX_CLI, which=lambda _n: None)


@pytest.mark.parametrize(
    'bad_name',
    [
        'foo.bar',  # dot would split a Codex TOML table header
        'has space',  # spaces are not bare keys
        'with"quote',
        'has\\backslash',
        'has@symbol',
        '',
        '\t',
    ],
)
def test_build_install_plan_rejects_unsafe_registry_name(bad_name: str) -> None:
    """Codex review #97: TOML bare keys are [A-Za-z0-9_-]+. Anything else risks a silent rewrite."""
    with pytest.raises(ValueError, match='registry name'):
        build_install_plan(MCPClient.CODEX_CLI, name=bad_name, kioku_mesh_mcp_path='/x/mesh-mem-mcp')


@pytest.mark.parametrize('good_name', ['kioku_mesh', 'mesh-mem', 'foo-bar', 'X42', 'a'])
def test_build_install_plan_accepts_bare_key_names(good_name: str) -> None:
    """All TOML-spec bare keys must be accepted."""
    plan = build_install_plan(MCPClient.CODEX_CLI, name=good_name, kioku_mesh_mcp_path='/x/mesh-mem-mcp')
    assert plan.name == good_name


# -- Claude Code path -----------------------------------------------------------


def test_build_claude_add_command_includes_env_and_command() -> None:
    plan = InstallPlan(
        client=MCPClient.CLAUDE_CODE,
        name='kioku_mesh',
        command='/x/mesh-mem-mcp',
        env={'A': '1', 'B': '2'},
    )
    cmd = _build_claude_add_command('/usr/bin/claude', plan)
    assert cmd[:5] == ['/usr/bin/claude', 'mcp', 'add', 'kioku_mesh', '-s']
    assert '-e' in cmd and 'A=1' in cmd
    assert cmd[-2:] == ['--', '/x/mesh-mem-mcp']


def test_install_claude_code_dry_run_emits_command() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={'A': '1'})
    out = install_claude_code(plan, dry_run=True, which=lambda _n: '/usr/bin/claude')
    assert '/usr/bin/claude mcp add kioku_mesh' in out
    assert '-e A=1' in out


def test_install_claude_code_fresh_register_runs_add() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = install_claude_code(plan, run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert 'registered kioku_mesh' in msg
    # list called first, then add (no remove).
    assert calls[0][1:3] == ['mcp', 'list']
    assert calls[-1][1:4] == ['mcp', 'add', 'kioku_mesh']


def test_install_claude_code_refuses_when_already_registered() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='kioku_mesh: /old/path - Connected\n', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = install_claude_code(plan, run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert msg.startswith('error:')
    assert '--force' in msg


def test_install_claude_code_force_removes_then_adds() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    invocations: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        invocations.append(argv[1:])
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='kioku_mesh: /old - Connected\n', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = install_claude_code(plan, run=fake_run, force=True, which=lambda _n: '/usr/bin/claude')
    assert 'registered kioku_mesh' in msg
    # Order must be: list -> remove -> add.
    assert invocations[0][:2] == ['mcp', 'list']
    assert invocations[1][:3] == ['mcp', 'remove', 'kioku_mesh']
    assert invocations[2][:3] == ['mcp', 'add', 'kioku_mesh']


def test_install_claude_code_force_raises_when_remove_fails() -> None:
    """Codex review #97: a failed `claude mcp remove` must surface, not get masked by add failure."""
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='kioku_mesh: /old - Connected\n', stderr='')
        if argv[1:3] == ['mcp', 'remove']:
            return subprocess.CompletedProcess(argv, 1, stdout='', stderr='permission denied')
        # Any subsequent call (notably the would-be add) must not happen.
        raise AssertionError(f'unexpected call after failed remove: {argv}')

    with pytest.raises(RuntimeError, match='claude mcp remove kioku_mesh failed'):
        install_claude_code(plan, run=fake_run, force=True, which=lambda _n: '/usr/bin/claude')


def test_install_claude_code_raises_on_subprocess_failure() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='oops')

    with pytest.raises(RuntimeError, match='claude mcp add failed'):
        install_claude_code(plan, run=fake_run, which=lambda _n: '/usr/bin/claude')


def test_install_claude_code_missing_claude_binary() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    with pytest.raises(FileNotFoundError, match='claude binary'):
        install_claude_code(plan, which=lambda _n: None)


# -- Codex CLI path -------------------------------------------------------------


def test_render_codex_toml_block_shape() -> None:
    plan = InstallPlan(
        client=MCPClient.CODEX_CLI,
        name='kioku_mesh',
        command='/x/mesh-mem-mcp',
        env={'A': '1', 'B': '2'},
    )
    block = _render_codex_toml_block(plan)
    assert '[mcp_servers.kioku_mesh]' in block
    assert 'command = "/x/mesh-mem-mcp"' in block
    assert '[mcp_servers.kioku_mesh.env]' in block
    assert 'A = "1"' in block
    assert 'B = "2"' in block


def test_render_codex_toml_block_escapes_quotes_and_backslashes() -> None:
    r"""Values with `"` or `\\` must round-trip as valid TOML, not break the file."""
    plan = InstallPlan(
        client=MCPClient.CODEX_CLI,
        name='kioku_mesh',
        command='/tmp/a"b',
        env={'WINPATH': 'C:\\tools\\bin', 'QUOTED': 'say "hi"'},
    )
    block = _render_codex_toml_block(plan)
    entry = tomllib.loads(block)['mcp_servers']['kioku_mesh']  # must not raise
    assert entry['command'] == '/tmp/a"b'
    assert entry['env'] == {'WINPATH': 'C:\\tools\\bin', 'QUOTED': 'say "hi"'}


def test_install_codex_cli_writes_new_file(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x/mesh-mem-mcp', env={'A': '1'})
    target = tmp_path / 'sub' / 'config.toml'
    msg = install_codex_cli(plan, config_path=target)
    assert target.is_file()
    body = target.read_text()
    assert '[mcp_servers.kioku_mesh]' in body
    assert 'A = "1"' in body
    assert 'wrote mcp_servers.kioku_mesh' in msg


def test_install_codex_cli_appends_to_existing_file(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    target = tmp_path / 'config.toml'
    target.write_text('model = "gpt-5"\n[other_section]\nkey = "value"\n')
    install_codex_cli(plan, config_path=target)
    body = target.read_text()
    # Existing content preserved.
    assert 'model = "gpt-5"' in body
    assert '[other_section]' in body
    # New block appended.
    assert '[mcp_servers.kioku_mesh]' in body


def test_install_codex_cli_refuses_when_already_present(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/old/mesh-mem-mcp"\n\n[mcp_servers.kioku_mesh.env]\nOLD = "1"\n'
    )
    msg = install_codex_cli(plan, config_path=target)
    assert msg.startswith('error:')
    assert '--force' in msg
    # File must NOT have been touched.
    assert '/old/mesh-mem-mcp' in target.read_text()


def test_install_codex_cli_force_replaces_block(tmp_path: Path) -> None:
    plan = InstallPlan(
        client=MCPClient.CODEX_CLI,
        name='kioku_mesh',
        command='/new/mesh-mem-mcp',
        env={'NEW': '1'},
    )
    target = tmp_path / 'config.toml'
    target.write_text(
        'model = "gpt-5"\n\n'
        '[mcp_servers.kioku_mesh]\ncommand = "/old/mesh-mem-mcp"\n\n'
        '[mcp_servers.kioku_mesh.env]\nOLD = "1"\n\n'
        '[mcp_servers.codegraph]\ncommand = "codegraph"\n'
    )
    msg = install_codex_cli(plan, force=True, config_path=target)
    assert 'wrote mcp_servers.kioku_mesh' in msg
    body = target.read_text()
    # Old values gone, new values present.
    assert '/old/mesh-mem-mcp' not in body
    assert '/new/mesh-mem-mcp' in body
    assert 'OLD = "1"' not in body
    assert 'NEW = "1"' in body
    # Unrelated blocks preserved.
    assert 'model = "gpt-5"' in body
    assert '[mcp_servers.codegraph]' in body


def test_install_codex_cli_force_preserves_blocks_before_and_after(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/new/x', env={})
    target = tmp_path / 'config.toml'
    target.write_text(
        '[before]\nbk = "1"\n\n'
        '[mcp_servers.kioku_mesh]\ncommand = "/old"\n'
        '[mcp_servers.kioku_mesh.tools.foo]\napproval_mode = "approve"\n\n'
        '[after]\nak = "2"\n'
    )
    install_codex_cli(plan, force=True, config_path=target)
    body = target.read_text()
    assert '[before]' in body and 'bk = "1"' in body
    assert '[after]' in body and 'ak = "2"' in body
    # Nested-table line from old block should be gone.
    assert 'approval_mode' not in body


def test_install_codex_cli_dry_run_does_not_touch_file(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x', env={})
    target = tmp_path / 'config.toml'
    target.write_text('# existing\n')
    msg = install_codex_cli(plan, dry_run=True, config_path=target)
    assert 'would write to' in msg
    assert '[mcp_servers.kioku_mesh]' in msg
    assert target.read_text() == '# existing\n'  # untouched


def test_install_codex_cli_rejects_unparseable_toml(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x', env={})
    target = tmp_path / 'config.toml'
    target.write_text('garbage = [not, closed,\n')
    with pytest.raises(RuntimeError, match='cannot parse'):
        install_codex_cli(plan, config_path=target)


# -- _replace_codex_block direct tests -----------------------------------------


def test_replace_codex_block_no_existing_appends() -> None:
    existing = 'model = "gpt-5"\n'
    new = _replace_codex_block(existing, 'kioku_mesh', '[mcp_servers.kioku_mesh]\ncommand = "/x"')
    assert 'model = "gpt-5"' in new
    assert '[mcp_servers.kioku_mesh]' in new
    assert new.count('[mcp_servers.kioku_mesh]') == 1


def test_replace_codex_block_only_replaces_matching_name() -> None:
    existing = (
        '[mcp_servers.other]\ncommand = "/other"\n\n'
        '[mcp_servers.kioku_mesh]\ncommand = "/old"\n\n'
        '[mcp_servers.kioku_mesh.env]\nA = "1"\n'
    )
    new = _replace_codex_block(existing, 'kioku_mesh', '[mcp_servers.kioku_mesh]\ncommand = "/new"')
    assert '/other' in new
    assert '/new' in new
    assert '/old' not in new
    assert 'A = "1"' not in new  # nested table swept


# -- _repair_identity_env --------------------------------------------------------


def test_repair_identity_env_renames_legacy_key_without_current_twin() -> None:
    env = {'ZENOH_CONNECT': 'tcp/127.0.0.1:7447', 'MESH_MEM_AGENT_FAMILY': 'claude'}
    repaired = _repair_identity_env(env)
    assert repaired == {'ZENOH_CONNECT': 'tcp/127.0.0.1:7447', 'KIOKU_MESH_AGENT_FAMILY': 'claude'}


def test_repair_identity_env_renames_both_identity_keys() -> None:
    env = {'MESH_MEM_AGENT_FAMILY': 'codex', 'MESH_MEM_CLIENT_ID': 'codex-cli'}
    repaired = _repair_identity_env(env)
    assert repaired == {'KIOKU_MESH_AGENT_FAMILY': 'codex', 'KIOKU_MESH_CLIENT_ID': 'codex-cli'}


def test_repair_identity_env_leaves_unrelated_user_env_untouched() -> None:
    env = {'MY_CUSTOM_FLAG': 'on', 'ANOTHER_SETTING': '42'}
    assert _repair_identity_env(env) == env


def test_repair_identity_env_no_op_when_current_prefix_already_present() -> None:
    """A config already carrying both names is correct; repair keeps the inert legacy key."""
    env = {'MESH_MEM_AGENT_FAMILY': 'claude', 'KIOKU_MESH_AGENT_FAMILY': 'claude'}
    assert _repair_identity_env(env) == env


def test_repair_identity_env_no_op_when_nothing_legacy() -> None:
    env = {'KIOKU_MESH_AGENT_FAMILY': 'claude', 'KIOKU_MESH_CLIENT_ID': 'claude-code'}
    assert _repair_identity_env(env) == env


# -- repair_codex_cli -------------------------------------------------------------


def test_repair_codex_cli_renames_legacy_prefix_preserves_command_and_extra_env(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text(
        'model = "gpt-5"\n\n'
        '[mcp_servers.kioku_mesh]\n'
        'command = "/home/user/.local/bin/kioku-mesh-mcp"\n\n'
        '[mcp_servers.kioku_mesh.env]\n'
        'ZENOH_CONNECT = "tcp/127.0.0.1:7447"\n'
        'MESH_MEM_AGENT_FAMILY = "codex"\n'
        'MESH_MEM_CLIENT_ID = "codex-cli"\n'
        'MY_CUSTOM_TIMEOUT = "30"\n\n'
        '[mcp_servers.other]\n'
        'command = "/other"\n'
    )
    msg = repair_codex_cli(config_path=target)
    assert 'repaired' in msg
    body = target.read_text()
    assert 'MESH_MEM_AGENT_FAMILY' not in body
    assert 'MESH_MEM_CLIENT_ID' not in body
    assert 'KIOKU_MESH_AGENT_FAMILY = "codex"' in body
    assert 'KIOKU_MESH_CLIENT_ID = "codex-cli"' in body
    # Unrelated user env / command / other blocks untouched.
    assert 'MY_CUSTOM_TIMEOUT = "30"' in body
    assert 'command = "/home/user/.local/bin/kioku-mesh-mcp"' in body
    assert 'model = "gpt-5"' in body
    assert '[mcp_servers.other]' in body


def test_repair_codex_cli_no_op_when_already_current_prefix(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    original = (
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nKIOKU_MESH_AGENT_FAMILY = "codex"\n'
    )
    target.write_text(original)
    msg = repair_codex_cli(config_path=target)
    assert 'nothing to repair' in msg
    assert target.read_text() == original  # untouched


def test_repair_codex_cli_errors_when_entry_missing(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text('model = "gpt-5"\n')
    msg = repair_codex_cli(config_path=target)
    assert msg.startswith('error:')
    assert 'kioku_mesh' in msg


def test_repair_codex_cli_errors_when_file_missing(tmp_path: Path) -> None:
    target = tmp_path / 'missing.toml'
    msg = repair_codex_cli(config_path=target)
    assert msg.startswith('error:')


def test_repair_codex_cli_preserves_other_fields_and_comments_in_target_block(tmp_path: Path) -> None:
    """Only the identity key tokens change — args / enabled / timeout / comments stay."""
    target = tmp_path / 'config.toml'
    original = (
        '[mcp_servers.kioku_mesh]\n'
        'command = "/home/user/.local/bin/kioku-mesh-mcp"\n'
        'args = ["--verbose", "--flag"]\n'
        'enabled = true\n'
        'startup_timeout_sec = 45\n'
        '\n'
        '# keep this note about the env block\n'
        '[mcp_servers.kioku_mesh.env]\n'
        'MESH_MEM_AGENT_FAMILY = "codex"  # trailing comment\n'
        'MY_CUSTOM_TIMEOUT = "30"\n'
    )
    target.write_text(original)

    msg = repair_codex_cli(config_path=target)

    assert 'repaired' in msg
    body = target.read_text()
    assert 'args = ["--verbose", "--flag"]' in body
    assert 'enabled = true' in body
    assert 'startup_timeout_sec = 45' in body
    assert '# keep this note about the env block' in body
    assert 'KIOKU_MESH_AGENT_FAMILY = "codex"  # trailing comment' in body
    assert 'MESH_MEM_AGENT_FAMILY' not in body
    entry = tomllib.loads(body)['mcp_servers']['kioku_mesh']
    assert entry['args'] == ['--verbose', '--flag']
    assert entry['enabled'] is True
    assert entry['startup_timeout_sec'] == 45
    assert entry['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'codex', 'MY_CUSTOM_TIMEOUT': '30'}


def test_repair_codex_cli_keeps_quoted_values_valid_toml(tmp_path: Path) -> None:
    """Values that would need escaping survive because nothing is re-rendered."""
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\n'
        "command = '/tmp/a\"b'\n"
        '\n'
        '[mcp_servers.kioku_mesh.env]\n'
        "KEEP = 'a\"b'\n"
        'WINPATH = "C:\\\\tools\\\\bin"\n'
        'MESH_MEM_CLIENT_ID = "codex-cli"\n'
    )

    msg = repair_codex_cli(config_path=target)

    assert 'repaired' in msg
    entry = tomllib.loads(target.read_text())['mcp_servers']['kioku_mesh']  # must not raise
    assert entry['command'] == '/tmp/a"b'
    assert entry['env'] == {'KEEP': 'a"b', 'WINPATH': 'C:\\tools\\bin', 'KIOKU_MESH_CLIENT_ID': 'codex-cli'}


def test_repair_codex_cli_renames_key_inside_inline_env_table(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\nenv = { MESH_MEM_AGENT_FAMILY = "codex", OTHER = "1" }\n'
    )

    msg = repair_codex_cli(config_path=target)

    assert 'repaired' in msg
    body = target.read_text()
    assert body.endswith('env = { KIOKU_MESH_AGENT_FAMILY = "codex", OTHER = "1" }\n')
    assert tomllib.loads(body)['mcp_servers']['kioku_mesh']['env'] == {
        'KIOKU_MESH_AGENT_FAMILY': 'codex',
        'OTHER': '1',
    }


def test_repair_codex_cli_fails_closed_on_layout_it_cannot_edit(tmp_path: Path) -> None:
    """An entry with no table header of its own is refused, leaving the file alone."""
    target = tmp_path / 'config.toml'
    original = '[mcp_servers]\nkioku_mesh = { command = "/x", env = { MESH_MEM_AGENT_FAMILY = "codex" } }\n'
    target.write_text(original)

    with pytest.raises(RuntimeError, match='unsupported layout'):
        repair_codex_cli(config_path=target)
    assert target.read_text() == original


# -- repair_claude_code ------------------------------------------------------------

_CLAUDE_MCP_GET_OUTPUT = (
    'kioku_mesh:\n'
    '  Scope: User config (available in all your projects)\n'
    '  Status: ✔ Connected\n'
    '  Type: stdio\n'
    '  Command: /home/user/.local/bin/kioku-mesh-mcp\n'
    '  Args:\n'
    '  Environment:\n'
    '    ZENOH_CONNECT=tcp/127.0.0.1:7447\n'
    '    MESH_MEM_AGENT_FAMILY=claude\n'
    '    MESH_MEM_CLIENT_ID=claude-code\n'
    '\n'
    'To remove this server, run: claude mcp remove kioku_mesh -s user\n'
)


def test_repair_claude_code_renames_legacy_prefix_preserves_extra_env() -> None:
    invocations: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        invocations.append(argv[1:])
        if argv[1:3] == ['mcp', 'get']:
            return subprocess.CompletedProcess(argv, 0, stdout=_CLAUDE_MCP_GET_OUTPUT, stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert 'repaired' in msg
    # get -> remove -> add, in that order.
    assert invocations[0][:2] == ['mcp', 'get']
    assert invocations[1][:3] == ['mcp', 'remove', 'kioku_mesh']
    add_call = invocations[2]
    assert add_call[:3] == ['mcp', 'add', 'kioku_mesh']
    assert '-e' in add_call
    joined = ' '.join(add_call)
    assert 'KIOKU_MESH_AGENT_FAMILY=claude' in joined
    assert 'KIOKU_MESH_CLIENT_ID=claude-code' in joined
    assert 'MESH_MEM_AGENT_FAMILY' not in joined
    assert 'ZENOH_CONNECT=tcp/127.0.0.1:7447' in joined  # unrelated env preserved
    assert add_call[-2:] == ['--', '/home/user/.local/bin/kioku-mesh-mcp']  # command preserved


def test_repair_claude_code_no_op_when_already_current_prefix() -> None:
    output = 'kioku_mesh:\n  Command: /x\n  Environment:\n    KIOKU_MESH_AGENT_FAMILY=claude\n'

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')

    msg = repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert 'nothing to repair' in msg


def test_repair_claude_code_errors_when_not_registered() -> None:
    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='No MCP server found with name: kioku_mesh')

    msg = repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert msg.startswith('error:')


def test_repair_claude_code_missing_claude_binary() -> None:
    with pytest.raises(FileNotFoundError, match='claude binary'):
        repair_claude_code(which=lambda _n: None)


def _claude_get_output(
    *,
    scope: str = 'User config (available in all your projects)',
    transport: str | None = 'stdio',
    command: str | None = '/home/user/.local/bin/kioku-mesh-mcp',
    args: str | None = '',
    env_lines: str = '    MESH_MEM_AGENT_FAMILY=claude\n',
) -> str:
    """Build a `claude mcp get` fixture; ``None`` drops the line entirely."""
    out = 'kioku_mesh:\n'
    if scope is not None:
        out += f'  Scope: {scope}\n'
    out += '  Status: ✔ Connected\n'
    if transport is not None:
        out += f'  Type: {transport}\n'
    if command is not None:
        out += f'  Command: {command}\n'
    if args is not None:
        out += f'  Args: {args}\n' if args else '  Args:\n'
    out += '  Environment:\n' + env_lines
    return out + '\nTo remove this server, run: claude mcp remove kioku_mesh -s user\n'


def _recording_runner(
    output: str,
    *,
    add_rc: int = 0,
    rollback_rc: int = 0,
) -> tuple[list[list[str]], Callable[[list[str]], subprocess.CompletedProcess[str]]]:
    """Fake ``claude`` runner recording argv; ``add_rc`` fails the first add only."""
    invocations: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        invocations.append(argv[1:])
        if argv[1:3] == ['mcp', 'get']:
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')
        if argv[1:3] == ['mcp', 'add']:
            adds = [c for c in invocations if c[:2] == ['mcp', 'add']]
            if len(adds) == 1:
                return subprocess.CompletedProcess(argv, add_rc, stdout='', stderr='add boom')
            return subprocess.CompletedProcess(argv, rollback_rc, stdout='', stderr='rollback boom')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    return invocations, fake_run


@pytest.mark.parametrize(
    ('scope_line', 'expected_scope'),
    [
        ('Local config (private to you in this project)', 'local'),
        ('Project config (shared via .mcp.json)', 'project'),
        ('User config (available in all your projects)', 'user'),
    ],
)
def test_repair_claude_code_preserves_scope_and_args(scope_line: str, expected_scope: str) -> None:
    """B1: scope and non-empty args must survive the remove/add round trip."""
    output = _claude_get_output(scope=scope_line, args='--mode custom --flag')
    invocations, fake_run = _recording_runner(output)

    msg = repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')

    assert 'repaired' in msg
    remove_call, add_call = invocations[1], invocations[2]
    assert remove_call == ['mcp', 'remove', 'kioku_mesh', '-s', expected_scope]
    assert add_call[:6] == ['mcp', 'add', 'kioku_mesh', '-s', expected_scope, '-e']
    assert add_call[-5:] == ['--', '/home/user/.local/bin/kioku-mesh-mcp', '--mode', 'custom', '--flag']


def test_repair_claude_code_preserves_env_value_ending_with_colon() -> None:
    """B1: the old parser treated a trailing `:` as a section header and dropped the key."""
    output = _claude_get_output(
        env_lines='    KEEP=ends-with-colon:\n    MESH_MEM_AGENT_FAMILY=claude\n',
    )
    invocations, fake_run = _recording_runner(output)

    repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')

    assert 'KEEP=ends-with-colon:' in invocations[2]


def test_repair_claude_code_preserves_multiline_env_value() -> None:
    """B1: a value with a newline prints its continuation unindented at column 0."""
    output = _claude_get_output(
        env_lines='    MULTI=line one\nline two\n    MESH_MEM_CLIENT_ID=claude-code\n',
    )
    invocations, fake_run = _recording_runner(output)

    repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')

    add_call = invocations[2]
    assert 'MULTI=line one\nline two' in add_call
    assert 'KIOKU_MESH_CLIENT_ID=claude-code' in add_call


@pytest.mark.parametrize(
    ('kwargs', 'expected'),
    [
        ({'command': ''}, 'Command is empty'),
        ({'scope': None}, 'Scope is missing'),
        ({'scope': 'Workspace config (new in 3.x)'}, 'unrecognized Scope'),
        ({'transport': None}, 'transport) is missing'),
        ({'transport': 'http'}, "transport 'http' is not stdio"),
        ({'args': None}, 'Args line is missing'),
    ],
)
def test_repair_claude_code_fails_closed_before_remove(kwargs: dict[str, str | None], expected: str) -> None:
    """B1: anything we cannot reproduce stops the repair *before* the destructive remove."""
    invocations, fake_run = _recording_runner(_claude_get_output(**kwargs))

    with pytest.raises(RuntimeError) as excinfo:
        repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')

    message = str(excinfo.value)
    assert 'cannot be reproduced losslessly' in message
    assert expected in message  # the refusal names the field it could not reproduce
    assert [c[:2] for c in invocations] == [['mcp', 'get']]  # nothing was removed


def test_repair_claude_code_rolls_back_when_add_fails() -> None:
    """B2: a failed add must re-add the original entry rather than leave it deleted."""
    output = _claude_get_output(
        scope='Local config (private to you in this project)',
        args='--mode custom',
        env_lines='    ZENOH_CONNECT=tcp/127.0.0.1:7447\n    MESH_MEM_AGENT_FAMILY=claude\n',
    )
    invocations, fake_run = _recording_runner(output, add_rc=9)

    with pytest.raises(RuntimeError, match='was restored unchanged'):
        repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')

    assert [c[:2] for c in invocations] == [['mcp', 'get'], ['mcp', 'remove'], ['mcp', 'add'], ['mcp', 'add']]
    rollback = invocations[3]
    assert rollback[:5] == ['mcp', 'add', 'kioku_mesh', '-s', 'local']
    assert 'MESH_MEM_AGENT_FAMILY=claude' in rollback  # original identity env restored verbatim
    assert 'KIOKU_MESH_AGENT_FAMILY=claude' not in rollback
    assert 'ZENOH_CONNECT=tcp/127.0.0.1:7447' in rollback
    assert rollback[-4:] == ['--', '/home/user/.local/bin/kioku-mesh-mcp', '--mode', 'custom']


def test_repair_claude_code_reports_when_rollback_also_fails() -> None:
    """B2: if restore fails too, say the entry is gone and hand back the exact argv."""
    invocations, fake_run = _recording_runner(_claude_get_output(), add_rc=9, rollback_rc=7)

    with pytest.raises(RuntimeError, match='currently unregistered') as excinfo:
        repair_claude_code(run=fake_run, which=lambda _n: '/usr/bin/claude')

    message = str(excinfo.value)
    assert 'rollback boom' in message
    assert 'MESH_MEM_AGENT_FAMILY=claude' in message  # restore command is copy-pasteable
    assert len(invocations) == 4


def test_parse_claude_mcp_get_returns_none_without_command_line() -> None:
    assert _parse_claude_mcp_get('kioku_mesh:\n  Scope: User config\n') is None


# -- repair dispatch ----------------------------------------------------------------


def test_repair_dispatches_to_codex(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nMESH_MEM_AGENT_FAMILY = "codex"\n'
    )
    msg = repair(MCPClient.CODEX_CLI, config_path=target)
    assert 'repaired' in msg


def test_repair_dispatches_to_claude_code() -> None:
    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'get']:
            return subprocess.CompletedProcess(argv, 0, stdout=_CLAUDE_MCP_GET_OUTPUT, stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = repair(MCPClient.CLAUDE_CODE, run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert 'repaired' in msg


# -- CLI wiring -----------------------------------------------------------------


def test_cli_mcp_install_codex_writes_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    target = tmp_path / 'codex.toml'
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli'])
    assert rc == 0
    assert '[mcp_servers.kioku_mesh]' in target.read_text()
    assert 'wrote mcp_servers.kioku_mesh' in capsys.readouterr().out


def test_cli_mcp_install_codex_already_registered_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    target = tmp_path / 'codex.toml'
    target.write_text('[mcp_servers.kioku_mesh]\ncommand = "/old"\n')
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli'])
    assert rc == 1
    assert '--force' in capsys.readouterr().err


def test_cli_mcp_install_missing_mesh_mem_mcp_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda _name: None)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli'])
    assert rc == 2
    assert 'kioku-mesh-mcp' in capsys.readouterr().err


def test_cli_mcp_install_extra_env_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    target = tmp_path / 'codex.toml'
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(
        [
            'mcp',
            'install',
            '--client',
            'codex-cli',
            '-e',
            'ZENOH_CONNECT=tcp/192.168.1.5:7448',
        ]
    )
    assert rc == 0
    assert 'ZENOH_CONNECT = "tcp/192.168.1.5:7448"' in target.read_text()


def test_cli_mcp_install_rejects_malformed_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '-e', 'malformed'])
    assert rc == 2
    assert 'KEY=VALUE' in capsys.readouterr().err


def test_cli_mcp_install_repair_codex_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / 'codex.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nMESH_MEM_AGENT_FAMILY = "codex"\n'
    )
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '--repair'])
    assert rc == 0
    assert 'KIOKU_MESH_AGENT_FAMILY = "codex"' in target.read_text()
    assert 'repaired' in capsys.readouterr().out


def test_cli_mcp_install_repair_codex_missing_entry_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / 'codex.toml'
    target.write_text('model = "gpt-5"\n')
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '--repair'])
    assert rc == 1
    assert 'kioku_mesh' in capsys.readouterr().err


def test_cli_mcp_install_repair_does_not_require_kioku_mesh_mcp_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--repair edits an existing registration; it must not need `which('kioku-mesh-mcp')` to succeed."""
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda _name: None)
    target = tmp_path / 'codex.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nMESH_MEM_CLIENT_ID = "codex-cli"\n'
    )
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '--repair'])
    assert rc == 0
    assert 'KIOKU_MESH_CLIENT_ID = "codex-cli"' in target.read_text()

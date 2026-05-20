"""Tests for ``mesh-mem init``.

The init subcommand never opens a Zenoh session, so these tests run without
``zenohd``: they invoke ``cli_main`` directly and assert on file contents,
stdout/stderr, and exit codes.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from mesh_mem.__main__ import _default_init_path
from mesh_mem.__main__ import _normalize_endpoint
from mesh_mem.__main__ import main as cli_main


@pytest.fixture
def xdg_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG_CONFIG_HOME so init writes into the test sandbox."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    return tmp_path / 'xdg' / 'mesh-mem' / 'zenohd.json5'


def test_default_init_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'custom'))
    assert _default_init_path() == tmp_path / 'custom' / 'mesh-mem' / 'zenohd.json5'


def test_default_init_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    assert _default_init_path() == Path.home() / '.config' / 'mesh-mem' / 'zenohd.json5'


@pytest.mark.parametrize(
    ('spec', 'expected'),
    [
        ('127.0.0.1', 'tcp/127.0.0.1:7447'),
        ('192.168.1.5:7448', 'tcp/192.168.1.5:7448'),
        ('tcp/0.0.0.0:7447', 'tcp/0.0.0.0:7447'),
        ('udp/10.0.0.1:1234', 'udp/10.0.0.1:1234'),
    ],
)
def test_normalize_endpoint(spec: str, expected: str) -> None:
    assert _normalize_endpoint(spec) == expected


def test_normalize_endpoint_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _normalize_endpoint('   ')


def test_init_localhost_default_writes_xdg_path(xdg_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init'])
    assert rc == 0
    assert xdg_config.is_file()
    body = xdg_config.read_text()
    assert 'tcp/127.0.0.1:7447' in body
    assert 'memory: {}' in body
    assert 'replication' not in body
    captured = capsys.readouterr()
    assert str(xdg_config) in captured.out
    assert 'zenohd -c' in captured.out


def test_init_refuses_overwrite_without_force(xdg_config: Path) -> None:
    assert cli_main(['init']) == 0
    rc = cli_main(['init'])
    assert rc == 1


def test_init_force_overwrites(xdg_config: Path) -> None:
    assert cli_main(['init']) == 0
    rc = cli_main(['init', '--force', '--listen', '127.0.0.1:7448'])
    assert rc == 0
    assert 'tcp/127.0.0.1:7448' in xdg_config.read_text()


def test_init_print_emits_to_stdout_and_skips_file(xdg_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init', '--print'])
    assert rc == 0
    assert not xdg_config.exists()
    out = capsys.readouterr().out
    assert 'mode: "router"' in out
    assert 'memory: {}' in out


def test_init_out_override(tmp_path: Path) -> None:
    target = tmp_path / 'sub' / 'custom.json5'
    rc = cli_main(['init', '--out', str(target)])
    assert rc == 0
    assert target.is_file()
    assert 'memory: {}' in target.read_text()


def test_init_hub_mode_uses_rocksdb_and_replication(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / 'hub.json5'
    rc = cli_main(['init', '--mode', 'hub', '--listen', 'tcp/0.0.0.0:7447', '--out', str(target)])
    assert rc == 0
    body = target.read_text()
    assert 'rocksdb: {}' in body
    assert 'replication: {' in body
    assert 'tcp/0.0.0.0:7447' in body
    assert 'endpoints: []' in body  # hub has no connect targets by default
    err = capsys.readouterr().err
    # 127.0.0.1 not in listen → user must be told about ZENOH_CONNECT.
    assert 'ZENOH_CONNECT' in err


def test_init_hub_with_loopback_omits_zenoh_connect_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / 'hub.json5'
    rc = cli_main(
        [
            'init',
            '--mode',
            'hub',
            '--listen',
            '127.0.0.1',
            '--listen',
            '192.168.1.10',
            '--out',
            str(target),
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert 'ZENOH_CONNECT' not in err


def test_init_spoke_requires_connect(tmp_path: Path) -> None:
    rc = cli_main(
        [
            'init',
            '--mode',
            'spoke',
            '--listen',
            '127.0.0.1',
            '--out',
            str(tmp_path / 'spoke.json5'),
        ]
    )
    assert rc == 2


def test_init_spoke_with_connect(tmp_path: Path) -> None:
    target = tmp_path / 'spoke.json5'
    rc = cli_main(
        [
            'init',
            '--mode',
            'spoke',
            '--listen',
            '127.0.0.1',
            '--listen',
            '192.168.1.10',
            '--connect',
            '192.168.1.1',
            '--out',
            str(target),
        ]
    )
    assert rc == 0
    body = target.read_text()
    assert 'tcp/192.168.1.1:7447' in body
    assert 'tcp/192.168.1.10:7447' in body
    assert 'rocksdb: {}' in body


def test_init_localhost_rejects_connect(tmp_path: Path) -> None:
    rc = cli_main(
        [
            'init',
            '--mode',
            'localhost',
            '--connect',
            '1.2.3.4',
            '--out',
            str(tmp_path / 'should_not_exist.json5'),
        ]
    )
    assert rc == 2
    assert not (tmp_path / 'should_not_exist.json5').exists()


def test_init_non_interactive_hub_requires_listen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force non-TTY so the interactive picker is skipped.
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    rc = cli_main(['init', '--mode', 'hub', '--out', str(tmp_path / 'hub.json5')])
    assert rc == 2

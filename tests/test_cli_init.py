"""Tests for ``kioku-mesh init``.

The init subcommand never opens a Zenoh session, so these tests run without
``zenohd``: they invoke ``cli_main`` directly and assert on file contents,
stdout/stderr, and exit codes.
"""

from __future__ import annotations

from collections.abc import Callable
import io
from pathlib import Path
import subprocess

import pytest

from kioku_mesh.__main__ import _dedupe_endpoints
from kioku_mesh.__main__ import _default_init_path
from kioku_mesh.__main__ import _default_systemd_user_unit_path
from kioku_mesh.__main__ import _detect_local_ipv4
from kioku_mesh.__main__ import _LOCAL_IPV4_PROBES
from kioku_mesh.__main__ import _normalize_endpoint
from kioku_mesh.__main__ import _prompt_listen_endpoints
from kioku_mesh.__main__ import _render_systemd_unit
from kioku_mesh.__main__ import main as cli_main


@pytest.fixture
def xdg_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG_CONFIG_HOME so init writes into the test sandbox (zenohd.json5)."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    return tmp_path / 'xdg' / 'kioku-mesh' / 'zenohd.json5'


@pytest.fixture
def xdg_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG_CONFIG_HOME so `init --mode local` writes config.yaml in the sandbox."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    return tmp_path / 'xdg' / 'kioku-mesh' / 'config.yaml'


def test_default_init_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'custom'))
    assert _default_init_path() == tmp_path / 'custom' / 'kioku-mesh' / 'zenohd.json5'


def test_default_init_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    # Sandbox HOME so the legacy-path fallback (#128) does not pick up a real
    # ~/.config/mesh-mem on the dev machine running the suite.
    fake_home = tmp_path / 'home'
    fake_home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: fake_home))
    assert _default_init_path() == fake_home / '.config' / 'kioku-mesh' / 'zenohd.json5'


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


def test_init_default_writes_local_config(xdg_local_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init'])
    assert rc == 0
    assert xdg_local_config.is_file()
    assert 'backend: local' in xdg_local_config.read_text()
    out = capsys.readouterr().out
    assert str(xdg_local_config) in out
    assert 'local backend ready' in out


def test_init_local_refuses_overwrite_without_force(xdg_local_config: Path) -> None:
    assert cli_main(['init']) == 0
    rc = cli_main(['init'])
    assert rc == 1


def test_init_local_print_emits_to_stdout_and_skips_file(
    xdg_local_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_main(['init', '--print'])
    assert rc == 0
    assert not xdg_local_config.exists()
    assert 'backend: local' in capsys.readouterr().out


def test_init_hub_refuses_overwrite_without_force(xdg_config: Path) -> None:
    assert cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1']) == 0
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1'])
    assert rc == 1


def test_init_hub_force_overwrites(xdg_config: Path) -> None:
    assert cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1']) == 0
    rc = cli_main(['init', '--mode', 'hub', '--force', '--listen', '127.0.0.1:7448'])
    assert rc == 0
    assert 'tcp/127.0.0.1:7448' in xdg_config.read_text()


def test_init_hub_print_emits_to_stdout_and_skips_file(xdg_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--print'])
    assert rc == 0
    assert not xdg_config.exists()
    out = capsys.readouterr().out
    assert 'mode: "router"' in out
    assert 'rocksdb: {}' in out


def test_init_hub_out_override(tmp_path: Path) -> None:
    target = tmp_path / 'sub' / 'custom.json5'
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--out', str(target)])
    assert rc == 0
    assert target.is_file()
    assert 'rocksdb: {}' in target.read_text()


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


def test_init_hub_rejects_connect(tmp_path: Path) -> None:
    rc = cli_main(
        [
            'init',
            '--mode',
            'hub',
            '--listen',
            '127.0.0.1',
            '--connect',
            '1.2.3.4',
            '--out',
            str(tmp_path / 'should_not_exist.json5'),
        ]
    )
    assert rc == 2
    assert not (tmp_path / 'should_not_exist.json5').exists()


def test_init_local_prints_scale_up_hint(xdg_local_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'scale up' in out
    assert '--mode hub --force' in out


def test_init_hub_prints_spoke_invocation_with_detected_ip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / 'hub.json5'
    rc = cli_main(
        [
            'init',
            '--mode',
            'hub',
            '--listen',
            '127.0.0.1',
            '--listen',
            '192.168.7.42',
            '--out',
            str(target),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert 'on each spoke:' in out
    assert '--mode spoke' in out
    assert '--connect 192.168.7.42:7447' in out


def test_init_hub_export_hint_follows_legacy_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # On a host where only the legacy ~/.local/share/mesh-mem exists, the
    # ROCKSDB_ROOT hint must point there too, not at an empty kioku-mesh dir,
    # so zenohd does not orphan the existing RocksDB store (#128).
    fake_home = tmp_path / 'home'
    (fake_home / '.local' / 'share' / 'mesh-mem').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: fake_home))
    import kioku_mesh.paths as paths_mod

    paths_mod._warned.clear()
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--out', str(tmp_path / 'hub.json5')])
    assert rc == 0
    assert 'ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"' in capsys.readouterr().out


def test_init_hub_export_hint_uses_new_path_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_home = tmp_path / 'home'
    (fake_home / '.local' / 'share').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: fake_home))
    import kioku_mesh.paths as paths_mod

    paths_mod._warned.clear()
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--out', str(tmp_path / 'hub.json5')])
    assert rc == 0
    assert 'ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/kioku-mesh"' in capsys.readouterr().out


def test_init_spoke_prints_hub_side_reminder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / 'spoke.json5'
    rc = cli_main(
        [
            'init',
            '--mode',
            'spoke',
            '--listen',
            '127.0.0.1',
            '--connect',
            '192.168.1.1',
            '--out',
            str(target),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert 'on the hub:' in out
    assert '--listen' in out
    # Fresh spoke must be told to backfill the local index, else status/search show 0 (#38).
    assert '--rebuild status' in out


def test_init_non_interactive_hub_requires_listen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force non-TTY so the interactive picker is skipped.
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    rc = cli_main(['init', '--mode', 'hub', '--out', str(tmp_path / 'hub.json5')])
    assert rc == 2


def test_dedupe_endpoints_preserves_order_and_drops_repeats() -> None:
    assert _dedupe_endpoints(['a', 'b', 'a', 'c', 'b']) == ['a', 'b', 'c']
    assert _dedupe_endpoints([]) == []


def test_init_listen_duplicates_collapsed_in_output(tmp_path: Path) -> None:
    target = tmp_path / 'hub.json5'
    rc = cli_main(
        [
            'init',
            '--mode',
            'hub',
            '--listen',
            '127.0.0.1',
            '--listen',
            'tcp/127.0.0.1:7447',
            '--listen',
            '127.0.0.1:7447',
            '--out',
            str(target),
        ]
    )
    assert rc == 0
    body = target.read_text()
    assert body.count('"tcp/127.0.0.1:7447"') == 1


def test_init_connect_duplicates_collapsed(tmp_path: Path) -> None:
    target = tmp_path / 'spoke.json5'
    rc = cli_main(
        [
            'init',
            '--mode',
            'spoke',
            '--listen',
            '127.0.0.1',
            '--connect',
            '192.168.1.1',
            '--connect',
            'tcp/192.168.1.1:7447',
            '--out',
            str(target),
        ]
    )
    assert rc == 0
    body = target.read_text()
    assert body.count('"tcp/192.168.1.1:7447"') == 1


class _FakeUDPSocket:
    """Minimal stand-in for ``socket.socket(AF_INET, SOCK_DGRAM)``.

    Maps destination -> source IP using the ``routes`` map. A destination not
    in the map raises ``OSError`` to mirror an unreachable network.
    """

    def __init__(self, routes: dict[str, str]) -> None:
        self._routes = routes
        self._src: str | None = None

    def __enter__(self) -> '_FakeUDPSocket':
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        dest = addr[0]
        if dest not in self._routes:
            raise OSError('no route')
        self._src = self._routes[dest]

    def getsockname(self) -> tuple[str, int]:
        assert self._src is not None
        return (self._src, 0)


def _install_fake_socket(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, str],
    hostname_ips: list[str] | None = None,
) -> None:
    """Patch ``socket.socket`` and ``socket.getaddrinfo`` for detection tests."""
    import socket as real_socket

    def fake_socket(family: int, type_: int) -> _FakeUDPSocket:
        assert family == real_socket.AF_INET
        assert type_ == real_socket.SOCK_DGRAM
        return _FakeUDPSocket(routes)

    monkeypatch.setattr('kioku_mesh.__main__.socket.socket', fake_socket)
    monkeypatch.setattr('kioku_mesh.__main__.socket.gethostname', lambda: 'fake-host')

    def fake_getaddrinfo(host: str, *_args: object, **_kw: object) -> list[tuple]:
        assert host == 'fake-host'
        return [(real_socket.AF_INET, 0, 0, '', (ip, 0)) for ip in (hostname_ips or [])]

    monkeypatch.setattr('kioku_mesh.__main__.socket.getaddrinfo', fake_getaddrinfo)


def test_detect_local_ipv4_collects_one_ip_per_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        '8.8.8.8': '203.0.113.10',
        '192.168.1.1': '192.168.3.42',
        '100.64.0.1': '100.64.0.5',
    }
    _install_fake_socket(monkeypatch, routes)
    detected = _detect_local_ipv4()
    assert detected == ['203.0.113.10', '192.168.3.42', '100.64.0.5']


def test_detect_local_ipv4_skips_unreachable_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {'192.168.1.1': '192.168.3.42'}
    _install_fake_socket(monkeypatch, routes)
    assert _detect_local_ipv4() == ['192.168.3.42']


def test_detect_local_ipv4_deduplicates_across_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {'8.8.8.8': '192.168.3.42', '192.168.1.1': '192.168.3.42'}
    _install_fake_socket(monkeypatch, routes, hostname_ips=['192.168.3.42', '10.5.0.1'])
    assert _detect_local_ipv4() == ['192.168.3.42', '10.5.0.1']


def test_detect_local_ipv4_filters_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {'8.8.8.8': '127.0.0.1'}
    _install_fake_socket(monkeypatch, routes, hostname_ips=['127.0.1.1'])
    assert _detect_local_ipv4() == []


def test_detect_local_ipv4_returns_empty_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_socket(monkeypatch, routes={}, hostname_ips=[])
    assert _detect_local_ipv4() == []


def test_local_ipv4_probes_cover_internet_lan_and_cgnat() -> None:
    # Surface the documented coverage so a future trim is intentional, not accidental.
    assert '8.8.8.8' in _LOCAL_IPV4_PROBES
    assert any(p.startswith('192.168.') for p in _LOCAL_IPV4_PROBES)
    assert any(p.startswith('10.') for p in _LOCAL_IPV4_PROBES)
    assert any(p.startswith('172.') for p in _LOCAL_IPV4_PROBES)
    assert any(p.startswith('100.64.') for p in _LOCAL_IPV4_PROBES)


def _scripted_input(answers: list[str]) -> Callable[[str], str]:
    """Build a fake ``input()`` that returns answers in order."""
    queue = iter(answers)

    def _input(_prompt: str = '') -> str:
        try:
            return next(queue)
        except StopIteration as e:
            raise AssertionError('input() called more times than answers provided') from e

    return _input


def test_prompt_listen_endpoints_dedupes_repeated_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pick option 1 (loopback) twice and option 2 (first detected) once.
    monkeypatch.setattr('builtins.input', _scripted_input(['1,1,2']))
    picks = _prompt_listen_endpoints(['192.168.3.42'])
    assert picks == ['tcp/127.0.0.1:7447', 'tcp/192.168.3.42:7447']


def test_prompt_listen_endpoints_dedupes_custom_matching_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pick loopback then "custom" with the same endpoint typed.
    detected = ['192.168.3.42']
    last_option = 4  # 1=loopback, 2=detected, 3=0.0.0.0, 4=custom
    monkeypatch.setattr(
        'builtins.input',
        _scripted_input([f'1,{last_option}', '127.0.0.1:7447']),
    )
    picks = _prompt_listen_endpoints(detected)
    assert picks == ['tcp/127.0.0.1:7447']


def test_prompt_listen_endpoints_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('builtins.input', _scripted_input(['99']))
    with pytest.raises(ValueError, match='out of range'):
        _prompt_listen_endpoints([])


def test_prompt_listen_endpoints_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('builtins.input', _scripted_input(['oops']))
    with pytest.raises(ValueError, match='invalid selection'):
        _prompt_listen_endpoints([])


def test_prompt_listen_endpoints_rejects_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('builtins.input', _scripted_input(['']))
    with pytest.raises(ValueError, match='no selection made'):
        _prompt_listen_endpoints([])


# -- --install-systemd (#86) ----------------------------------------------------


@pytest.fixture
def force_systemd_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the host has systemctl + Linux platform + reachable user manager.

    Faking ``_default_systemctl_probe`` to return rc=0 ensures the two-stage
    detection in ``_detect_systemd_user`` (introduced by the Codex review of
    #95) sees a healthy user manager during tests.
    """
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr('kioku_mesh.__main__.shutil.which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(
        'kioku_mesh.__main__._default_systemctl_probe',
        lambda _argv: subprocess.CompletedProcess([], 0, stdout='', stderr=''),
    )


@pytest.fixture
def systemd_unit_under(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both XDG_CONFIG_HOME branches into the test sandbox."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    return tmp_path / 'xdg' / 'systemd' / 'user' / 'kioku-mesh-zenohd.service'


def test_default_systemd_unit_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'custom'))
    assert _default_systemd_user_unit_path() == tmp_path / 'custom' / 'systemd' / 'user' / 'kioku-mesh-zenohd.service'


def test_render_systemd_unit_bakes_absolute_paths() -> None:
    body = _render_systemd_unit(Path('/x/y/zenohd.json5'), '/opt/zenoh/bin/zenohd', '%h/.local/share/kioku-mesh')
    assert '[Unit]' in body
    assert 'Description=kioku-mesh zenohd router' in body
    # Paths are double-quoted so systemd's unquoted-whitespace splitter doesn't
    # break ExecStart when the path contains spaces (Codex review on #95).
    assert 'ExecStart="/opt/zenoh/bin/zenohd" -c "/x/y/zenohd.json5"' in body
    assert 'Environment=ZENOH_BACKEND_ROCKSDB_ROOT=%h/.local/share/kioku-mesh' in body
    assert 'ExecStartPre=/usr/bin/install -d %h/.local/share/kioku-mesh' in body
    assert 'WantedBy=default.target' in body


def test_render_systemd_unit_uses_given_rocksdb_root() -> None:
    # The legacy fallback resolves to mesh-mem on a partially-migrated host;
    # the unit must follow so an existing RocksDB store is not orphaned (#128).
    body = _render_systemd_unit(Path('/x/zenohd.json5'), '/usr/bin/zenohd', '%h/.local/share/mesh-mem')
    assert 'Environment=ZENOH_BACKEND_ROCKSDB_ROOT=%h/.local/share/mesh-mem' in body
    assert 'ExecStartPre=/usr/bin/install -d %h/.local/share/mesh-mem' in body
    assert 'kioku-mesh' not in body.split('ROCKSDB_ROOT=', 1)[1].splitlines()[0]


def test_render_systemd_unit_quotes_paths_with_whitespace() -> None:
    """Paths containing spaces / backslashes / quotes must survive systemd's splitter."""
    body = _render_systemd_unit(
        Path('/home/u/My Configs/zenohd.json5'),
        '/opt/zen oh/bin/zenohd',
        '%h/.local/share/kioku-mesh',
    )
    assert 'ExecStart="/opt/zen oh/bin/zenohd" -c "/home/u/My Configs/zenohd.json5"' in body


def test_render_systemd_unit_escapes_backslashes_and_quotes() -> None:
    body = _render_systemd_unit(Path('/p/has"quote/zenohd.json5'), '/p/has\\back/zenohd', '%h/.local/share/kioku-mesh')
    # Backslashes and double-quotes inside the value are backslash-escaped
    # per systemd's POSIX-shell-like quoting rules.
    assert 'ExecStart="/p/has\\\\back/zenohd" -c "/p/has\\"quote/zenohd.json5"' in body


def test_init_install_systemd_writes_unit(
    force_systemd_supported: None,
    systemd_unit_under: Path,
    xdg_config: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 0
    assert xdg_config.is_file()
    assert systemd_unit_under.is_file()
    body = systemd_unit_under.read_text()
    assert f'ExecStart="/usr/bin/zenohd" -c "{xdg_config}"' in body
    out = capsys.readouterr().out
    assert 'systemctl --user enable --now kioku-mesh-zenohd' in out


def test_init_install_systemd_refuses_overwrite_without_force(
    force_systemd_supported: None,
    systemd_unit_under: Path,
    xdg_config: Path,
) -> None:
    assert cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd']) == 0
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 1


def test_init_install_systemd_force_overwrites(
    force_systemd_supported: None,
    systemd_unit_under: Path,
    xdg_config: Path,
) -> None:
    assert cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd']) == 0
    rc = cli_main(['init', '--mode', 'hub', '--install-systemd', '--force', '--listen', '127.0.0.1:7448'])
    assert rc == 0
    assert 'tcp/127.0.0.1:7448' in xdg_config.read_text()
    # Unit body should reference the (unchanged) config path; --force makes the rewrite legal.
    assert systemd_unit_under.is_file()


def test_init_install_systemd_reuses_existing_config_without_force(
    force_systemd_supported: None,
    systemd_unit_under: Path,
    xdg_config: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--install-systemd against an existing config reuses it (no --force, no rewrite) and only adds the unit."""
    # A config the user already provisioned (custom port proves no rewrite).
    assert cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1:7459']) == 0
    before = xdg_config.read_text()
    assert '7459' in before
    capsys.readouterr()

    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 0
    # Config left byte-for-byte unchanged (not regenerated to the default port).
    assert xdg_config.read_text() == before
    assert systemd_unit_under.is_file()
    out = capsys.readouterr().out
    assert 'using existing config' in out
    assert 'systemctl --user enable --now kioku-mesh-zenohd' in out


def test_init_install_systemd_print_emits_both_bodies(
    force_systemd_supported: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd', '--print'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'mode: "router"' in out  # zenohd config
    assert '[Unit]' in out
    assert 'ExecStart="/usr/bin/zenohd" -c' in out
    # No file should have been written under --print.
    assert not (tmp_path / 'xdg' / 'kioku-mesh' / 'zenohd.json5').exists()
    assert not (tmp_path / 'xdg' / 'systemd' / 'user' / 'kioku-mesh-zenohd.service').exists()


def test_init_install_systemd_falls_back_when_zenohd_missing(
    systemd_unit_under: Path,
    xdg_config: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing zenohd binary should warn and use the documented fallback path, not abort."""
    monkeypatch.setattr('sys.platform', 'linux')

    def fake_which(name: str) -> str | None:
        return '/usr/bin/systemctl' if name == 'systemctl' else None

    monkeypatch.setattr('kioku_mesh.__main__.shutil.which', fake_which)
    # Probe success: even with zenohd missing, systemd-user itself is reachable.
    monkeypatch.setattr(
        'kioku_mesh.__main__._default_systemctl_probe',
        lambda _argv: subprocess.CompletedProcess([], 0, stdout='', stderr=''),
    )
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 0
    assert 'ExecStart="/usr/bin/zenohd"' in systemd_unit_under.read_text()
    err = capsys.readouterr().err
    assert 'zenohd not on PATH' in err


def test_init_install_systemd_rejects_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr('sys.platform', 'darwin')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 2
    # Neither file should be written when the platform check trips.
    assert not (tmp_path / 'xdg' / 'kioku-mesh' / 'zenohd.json5').exists()
    assert not (tmp_path / 'xdg' / 'systemd' / 'user' / 'kioku-mesh-zenohd.service').exists()


def test_init_install_systemd_rejects_missing_systemctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr('kioku_mesh.__main__.shutil.which', lambda _name: None)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 2
    assert not (tmp_path / 'xdg' / 'kioku-mesh' / 'zenohd.json5').exists()


def test_init_install_systemd_rejects_unreachable_user_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Handle the case where systemctl exists but the user manager is unreachable (WSL / non-systemd container)."""
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr('kioku_mesh.__main__.shutil.which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(
        'kioku_mesh.__main__._default_systemctl_probe',
        lambda _argv: subprocess.CompletedProcess([], 1, stdout='', stderr='Failed to connect to user bus'),
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 2
    err = capsys.readouterr().err
    assert 'systemctl --user show-environment failed' in err
    # Neither file should be written when the probe trips — Codex review on #95.
    assert not (tmp_path / 'xdg' / 'kioku-mesh' / 'zenohd.json5').exists()
    assert not (tmp_path / 'xdg' / 'systemd' / 'user' / 'kioku-mesh-zenohd.service').exists()


def test_init_install_systemd_rejects_probe_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A subprocess timeout (hanging user manager) must surface as a clean refusal."""
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr('kioku_mesh.__main__.shutil.which', lambda name: f'/usr/bin/{name}')

    def _timeout(_argv: list[str]) -> 'subprocess.CompletedProcess[str]':
        raise subprocess.TimeoutExpired(cmd=_argv, timeout=2.0)

    monkeypatch.setattr('kioku_mesh.__main__._default_systemctl_probe', _timeout)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    rc = cli_main(['init', '--mode', 'hub', '--listen', '127.0.0.1', '--install-systemd'])
    assert rc == 2
    err = capsys.readouterr().err
    assert 'systemctl --user probe failed' in err


def test_init_install_systemd_unit_survives_whitespace_config_path(
    force_systemd_supported: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: `--out` with whitespace must produce a unit that systemd can parse."""
    target_dir = tmp_path / 'My Configs'
    target_dir.mkdir()
    config_path = target_dir / 'zenohd.json5'
    unit_path = tmp_path / 'unit.service'
    rc = cli_main(
        [
            'init',
            '--mode',
            'hub',
            '--listen',
            '127.0.0.1',
            '--install-systemd',
            '--out',
            str(config_path),
            '--print',
        ]
    )
    assert rc == 0
    body = capsys.readouterr().out
    assert f'ExecStart="/usr/bin/zenohd" -c "{config_path}"' in body
    # Sanity: the quoted path is not split by whitespace in the unit body.
    # systemd would parse this as ONE argument to -c.
    assert 'My Configs/zenohd.json5"' in body
    # The file is not written under --print, so the path itself doesn't need
    # to exist. The point is that the unit body quotes the whitespace path.
    _ = unit_path

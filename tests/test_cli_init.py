"""Tests for ``mesh-mem init``.

The init subcommand never opens a Zenoh session, so these tests run without
``zenohd``: they invoke ``cli_main`` directly and assert on file contents,
stdout/stderr, and exit codes.
"""

from __future__ import annotations

from collections.abc import Callable
import io
from pathlib import Path

import pytest

from mesh_mem.__main__ import _dedupe_endpoints
from mesh_mem.__main__ import _default_init_path
from mesh_mem.__main__ import _detect_local_ipv4
from mesh_mem.__main__ import _LOCAL_IPV4_PROBES
from mesh_mem.__main__ import _normalize_endpoint
from mesh_mem.__main__ import _prompt_listen_endpoints
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


def test_dedupe_endpoints_preserves_order_and_drops_repeats() -> None:
    assert _dedupe_endpoints(['a', 'b', 'a', 'c', 'b']) == ['a', 'b', 'c']
    assert _dedupe_endpoints([]) == []


def test_init_listen_duplicates_collapsed_in_output(tmp_path: Path) -> None:
    target = tmp_path / 'localhost.json5'
    rc = cli_main(
        [
            'init',
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

    monkeypatch.setattr('mesh_mem.__main__.socket.socket', fake_socket)
    monkeypatch.setattr('mesh_mem.__main__.socket.gethostname', lambda: 'fake-host')

    def fake_getaddrinfo(host: str, *_args: object, **_kw: object) -> list[tuple]:
        assert host == 'fake-host'
        return [(real_socket.AF_INET, 0, 0, '', (ip, 0)) for ip in (hostname_ips or [])]

    monkeypatch.setattr('mesh_mem.__main__.socket.getaddrinfo', fake_getaddrinfo)


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

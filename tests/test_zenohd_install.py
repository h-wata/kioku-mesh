"""Tests for kioku_mesh.zenohd_install."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from kioku_mesh import zenohd_install
from kioku_mesh.zenohd_install import detect_target
from kioku_mesh.zenohd_install import download_and_verify
from kioku_mesh.zenohd_install import extract_binary

# ---------------------------------------------------------------------------
# detect_target
# ---------------------------------------------------------------------------


def test_detect_target_linux_gnu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    mock_result = MagicMock()
    mock_result.stdout = 'ldd (GNU libc) 2.35\n'
    mock_result.stderr = ''
    with patch('subprocess.run', return_value=mock_result):
        assert detect_target() == 'x86_64-unknown-linux-gnu'


def test_detect_target_linux_musl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    mock_result = MagicMock()
    mock_result.stdout = ''
    mock_result.stderr = 'musl libc (x86_64)\nVersion 1.2.3\n'
    with patch('subprocess.run', return_value=mock_result):
        assert detect_target() == 'x86_64-unknown-linux-musl'


def test_detect_target_linux_aarch64_gnu(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'aarch64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    mock_result = MagicMock()
    mock_result.stdout = 'ldd (GNU libc) 2.35\n'
    mock_result.stderr = ''
    with patch('subprocess.run', return_value=mock_result):
        assert detect_target() == 'aarch64-unknown-linux-gnu'


def test_detect_target_linux_aarch64_musl(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'aarch64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    mock_result = MagicMock()
    mock_result.stdout = 'musl libc\n'
    mock_result.stderr = ''
    with patch('subprocess.run', return_value=mock_result):
        assert detect_target() == 'aarch64-unknown-linux-musl'


def test_detect_target_darwin_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'arm64')
    monkeypatch.setattr('platform.system', lambda: 'Darwin')
    assert detect_target() == 'aarch64-apple-darwin'


def test_detect_target_darwin_x86(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('platform.system', lambda: 'Darwin')
    assert detect_target() == 'x86_64-apple-darwin'


def test_detect_target_windows_x86(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('platform.system', lambda: 'Windows')
    assert detect_target() == 'x86_64-pc-windows-msvc'


def test_detect_target_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    monkeypatch.setattr('platform.system', lambda: 'FreeBSD')
    with pytest.raises(RuntimeError, match='unsupported OS'):
        detect_target()


def test_detect_target_unsupported_arch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'riscv64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    with pytest.raises(RuntimeError, match='unsupported architecture'):
        detect_target()


# ---------------------------------------------------------------------------
# download_and_verify
# ---------------------------------------------------------------------------


def test_download_and_verify_ok(tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'fake zenohd binary data'
    expected_hash = hashlib.sha256(content).hexdigest()
    dest = tmp_path / 'zenohd.tar.gz'

    def mock_urlretrieve(url: str, dst: str | Path) -> tuple[str, object]:
        Path(dst).write_bytes(content)
        return str(dst), {}

    monkeypatch.setattr('urllib.request.urlretrieve', mock_urlretrieve)
    monkeypatch.setattr(
        'urllib.request.urlopen',
        lambda url: io.BytesIO(expected_hash.encode()),
    )

    result = download_and_verify(
        'http://example.com/zenohd.tar.gz',
        'http://example.com/zenohd.tar.gz.sha256',
        dest,
    )
    assert result == dest
    assert dest.read_bytes() == content


def test_download_and_verify_ok_gnu_checksum_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'fake binary'
    expected_hash = hashlib.sha256(content).hexdigest()
    dest = tmp_path / 'file.tar.gz'
    sha_line = f'{expected_hash}  file.tar.gz\n'

    monkeypatch.setattr('urllib.request.urlretrieve', lambda url, dst:
                        (Path(dst).write_bytes(content), {}))
    monkeypatch.setattr('urllib.request.urlopen',
                        lambda url: io.BytesIO(sha_line.encode()))

    result = download_and_verify('http://x/file.tar.gz',
                                 'http://x/file.tar.gz.sha256', dest)
    assert result == dest


def test_download_and_verify_bad_checksum(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'real binary data'
    dest = tmp_path / 'zenohd.tar.gz'

    monkeypatch.setattr('urllib.request.urlretrieve', lambda url, dst:
                        (Path(dst).write_bytes(content), {}))
    monkeypatch.setattr('urllib.request.urlopen',
                        lambda url: io.BytesIO(b'deadbeef00000000' * 4))

    with pytest.raises(ValueError, match='SHA-256 mismatch.*expected=.*got='):
        download_and_verify(
            'http://example.com/zenohd.tar.gz',
            'http://example.com/zenohd.tar.gz.sha256',
            dest,
        )


# ---------------------------------------------------------------------------
# extract_binary
# ---------------------------------------------------------------------------


def test_extract_binary_tar(tmp_path: Path) -> None:
    binary_content = b'#!/bin/sh\necho zenohd'
    archive_path = tmp_path / 'zenohd.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as tf:
        info = tarfile.TarInfo(name='zenohd')
        info.size = len(binary_content)
        tf.addfile(info, io.BytesIO(binary_content))

    dest_dir = tmp_path / 'bin'
    result = extract_binary(archive_path, 'zenohd', dest_dir)

    assert result == dest_dir / 'zenohd'
    assert result.read_bytes() == binary_content
    assert result.stat().st_mode & 0o111


def test_extract_binary_tar_nested(tmp_path: Path) -> None:
    binary_content = b'#!/bin/sh\necho hello'
    archive_path = tmp_path / 'zenohd.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as tf:
        info = tarfile.TarInfo(name='some/prefix/zenohd')
        info.size = len(binary_content)
        tf.addfile(info, io.BytesIO(binary_content))

    dest_dir = tmp_path / 'bin'
    result = extract_binary(archive_path, 'zenohd', dest_dir)
    assert result.read_bytes() == binary_content


def test_extract_binary_zip(tmp_path: Path) -> None:
    lib_content = b'\x7fELF fake so'
    archive_path = tmp_path / 'rocksdb.zip'
    with zipfile.ZipFile(archive_path, 'w') as zf:
        zf.writestr('libzenoh_backend_rocksdb.so', lib_content)

    dest_dir = tmp_path / 'bin'
    result = extract_binary(archive_path, 'libzenoh_backend_rocksdb.so',
                            dest_dir)
    assert result.read_bytes() == lib_content


def test_extract_binary_not_found_tar(tmp_path: Path) -> None:
    archive_path = tmp_path / 'empty.tar.gz'
    with tarfile.open(archive_path, 'w:gz'):
        pass

    with pytest.raises(FileNotFoundError, match='zenohd'):
        extract_binary(archive_path, 'zenohd', tmp_path / 'bin')


def test_extract_binary_not_found_zip(tmp_path: Path) -> None:
    archive_path = tmp_path / 'empty.zip'
    with zipfile.ZipFile(archive_path, 'w'):
        pass

    with pytest.raises(FileNotFoundError, match='zenohd'):
        extract_binary(archive_path, 'zenohd', tmp_path / 'bin')


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_zenohd_install_smoke() -> None:
    result = subprocess.run(
        ['kioku-mesh', 'zenohd', 'install', '--help'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert 'zenohd' in result.stdout


# ---------------------------------------------------------------------------
# release_urls
# ---------------------------------------------------------------------------


def test_release_urls_linux_gnu() -> None:
    urls = zenohd_install.release_urls('1.9.0', 'x86_64-unknown-linux-gnu')
    assert 'x86_64-unknown-linux-gnu' in urls['zenohd']
    assert urls['zenohd'].endswith('.tar.gz')
    assert urls['zenohd_sha256'].endswith('.sha256')
    assert 'x86_64-unknown-linux-gnu' in urls['zenoh_backend_rocksdb']
    assert urls['zenoh_backend_rocksdb'].endswith('.zip')


def test_release_urls_windows() -> None:
    urls = zenohd_install.release_urls('1.9.0', 'x86_64-pc-windows-msvc')
    assert urls['zenohd'].endswith('.zip')

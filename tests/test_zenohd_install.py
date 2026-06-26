"""Tests for kioku_mesh.zenohd_install."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from kioku_mesh import zenohd_install
from kioku_mesh.zenohd_install import _fetch_asset_digest
from kioku_mesh.zenohd_install import detect_target
from kioku_mesh.zenohd_install import download_and_verify
from kioku_mesh.zenohd_install import extract_binary

FAKE_RELEASE = {
    'assets': [
        {
            'name': 'zenoh-1.9.0-x86_64-unknown-linux-gnu-standalone.zip',
            'digest': 'sha256:f18081184b089e79e605f2c0cb3f7790fbf101ae94942988f716e19e1810a46e',
        },
        {
            'name': 'zenoh-backend-rocksdb-1.9.0-x86_64-unknown-linux-gnu-standalone.zip',
            'digest': 'sha256:638e8a5abc7a7ae8e455b879217990d5aae6845e739090ba1fed3418a8ff3151',
        },
    ]
}

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


def test_detect_target_linux_aarch64_gnu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'aarch64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    mock_result = MagicMock()
    mock_result.stdout = 'ldd (GNU libc) 2.35\n'
    mock_result.stderr = ''
    with patch('subprocess.run', return_value=mock_result):
        assert detect_target() == 'aarch64-unknown-linux-gnu'


def test_detect_target_linux_aarch64_musl(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_detect_target_unsupported_arch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('platform.machine', lambda: 'riscv64')
    monkeypatch.setattr('platform.system', lambda: 'Linux')
    with pytest.raises(RuntimeError, match='unsupported architecture'):
        detect_target()


# ---------------------------------------------------------------------------
# _fetch_asset_digest
# ---------------------------------------------------------------------------


def test_fetch_asset_digest_ok() -> None:
    asset_name = 'zenoh-1.9.0-x86_64-unknown-linux-gnu-standalone.zip'
    expected_digest = 'sha256:f18081184b089e79e605f2c0cb3f7790fbf101ae94942988f716e19e1810a46e'

    fake_resp = io.BytesIO(json.dumps(FAKE_RELEASE).encode())
    fake_resp.read = fake_resp.read  # BytesIO already has read()

    with patch('urllib.request.urlopen', return_value=fake_resp):
        digest = _fetch_asset_digest('eclipse-zenoh/zenoh', '1.9.0', asset_name)
    assert digest == expected_digest


def test_fetch_asset_digest_not_found() -> None:
    fake_resp = io.BytesIO(json.dumps(FAKE_RELEASE).encode())
    with patch('urllib.request.urlopen', return_value=fake_resp):
        with pytest.raises(ValueError, match='not found in release'):
            _fetch_asset_digest('eclipse-zenoh/zenoh', '1.9.0', 'nonexistent.zip')


# ---------------------------------------------------------------------------
# download_and_verify
# ---------------------------------------------------------------------------


def test_download_and_verify_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'fake zenohd binary data'
    expected_hash = hashlib.sha256(content).hexdigest()
    dest = tmp_path / 'zenohd.zip'
    asset_name = 'zenoh-1.9.0-x86_64-unknown-linux-gnu-standalone.zip'

    def mock_urlretrieve(url: str, dst: str | Path) -> tuple[str, object]:
        Path(dst).write_bytes(content)
        return str(dst), {}

    monkeypatch.setattr('urllib.request.urlretrieve', mock_urlretrieve)

    with patch('kioku_mesh.zenohd_install._fetch_asset_digest', return_value=f'sha256:{expected_hash}'):
        result = download_and_verify(
            'http://example.com/zenohd.zip',
            dest,
            'eclipse-zenoh/zenoh',
            '1.9.0',
            asset_name,
        )
    assert result == dest
    assert dest.read_bytes() == content


def test_download_and_verify_bad_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'real binary data'
    dest = tmp_path / 'zenohd.zip'
    asset_name = 'zenoh-1.9.0-x86_64-unknown-linux-gnu-standalone.zip'

    monkeypatch.setattr('urllib.request.urlretrieve', lambda url, dst: (Path(dst).write_bytes(content), {}))

    with patch(
        'kioku_mesh.zenohd_install._fetch_asset_digest',
        return_value='sha256:' + 'deadbeef00000000' * 4,
    ):
        with pytest.raises(ValueError, match='SHA-256 mismatch'):
            download_and_verify(
                'http://example.com/zenohd.zip',
                dest,
                'eclipse-zenoh/zenoh',
                '1.9.0',
                asset_name,
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
    result = extract_binary(archive_path, 'libzenoh_backend_rocksdb.so', dest_dir)
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
    assert urls['zenohd'].endswith('-standalone.zip')
    assert 'x86_64-unknown-linux-gnu' in urls['zenoh_backend_rocksdb']
    assert urls['zenoh_backend_rocksdb'].endswith('-standalone.zip')
    assert 'zenohd_sha256' not in urls
    assert 'zenoh_backend_rocksdb_sha256' not in urls


def test_release_urls_windows() -> None:
    urls = zenohd_install.release_urls('1.9.0', 'x86_64-pc-windows-msvc')
    assert urls['zenohd'].endswith('-standalone.zip')
    assert urls['zenoh_backend_rocksdb'].endswith('-standalone.zip')


def test_release_urls_regression_linux_gnu() -> None:
    """URL construction must match the exact upstream asset names for v1.9.0 linux-gnu."""
    urls = zenohd_install.release_urls('1.9.0', 'x86_64-unknown-linux-gnu')
    assert urls['zenohd_asset'] == 'zenoh-1.9.0-x86_64-unknown-linux-gnu-standalone.zip'
    assert urls['zenoh_backend_rocksdb_asset'] == 'zenoh-backend-rocksdb-1.9.0-x86_64-unknown-linux-gnu-standalone.zip'
    assert urls['zenohd'].endswith('/zenoh-1.9.0-x86_64-unknown-linux-gnu-standalone.zip')
    assert urls['zenoh_backend_rocksdb'].endswith(
        '/zenoh-backend-rocksdb-1.9.0-x86_64-unknown-linux-gnu-standalone.zip'
    )

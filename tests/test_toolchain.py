import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / ".github" / "toolchain.lock.yml"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _lock(tmp_path, *, asset, tool="demo", version="1.0.0", binary="demo"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_path = tmp_path / "toolchain.lock.yml"
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cache_root": "/opt/oe-image-tools",
                "tools": {
                    tool: {
                        "version": version,
                        "binary": binary,
                        "assets": {"x86_64": asset, "aarch64": asset},
                    }
                },
            }
        )
    )
    return lock_path


def _raw_asset(data=b"tool-binary"):
    return {
        "url": "memory://demo",
        "format": "raw",
        "sha256": _sha(data),
        "binary_sha256": _sha(data),
    }


def _tar_bytes(member_name="demo", content=b"tool-binary"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(content)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def test_repository_toolchain_lock_pins_required_tools_and_architectures():
    from scripts.lib.toolchain import ToolchainLock

    lock = ToolchainLock.load(LOCK_FILE)

    assert set(lock.tools) == {"dgoss", "goss", "hadolint", "jq", "opencode"}
    assert lock.tools["opencode"].version == "1.18.8"
    assert lock.tools["goss"].version == "0.4.10"
    assert lock.tools["hadolint"].version == "2.14.0"
    assert lock.tools["jq"].version == "1.8.2"
    for name in ("goss", "hadolint", "jq", "opencode"):
        assert set(lock.tools[name].assets) == {"aarch64", "x86_64"}
    assert set(lock.tools["dgoss"].assets) == {"common"}


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("aarch64", "aarch64"),
        ("arm64", "aarch64"),
    ],
)
def test_normalize_architecture(machine, expected):
    from scripts.lib.toolchain import normalize_architecture

    assert normalize_architecture(machine) == expected


def test_installer_verifies_and_caches_raw_binary(tmp_path):
    from scripts.lib.toolchain import ToolchainInstaller, ToolchainLock

    data = b"tool-binary"
    lock = ToolchainLock.load(_lock(tmp_path, asset=_raw_asset(data)))
    calls = []

    def download(url, destination):
        calls.append(url)
        destination.write_bytes(data)

    installer = ToolchainInstaller(
        lock, cache_root=tmp_path / "cache", downloader=download
    )
    first = installer.install("x86_64")
    second = installer.install("x86_64")

    assert first["demo"] == second["demo"]
    assert first["demo"].read_bytes() == data
    assert first["demo"].stat().st_mode & 0o111
    assert calls == ["memory://demo"]


def test_installer_extracts_tar_without_trusting_archive_mode(tmp_path):
    from scripts.lib.toolchain import ToolchainInstaller, ToolchainLock

    binary = b"archive-binary"
    archive = _tar_bytes(content=binary)
    asset = {
        "url": "memory://demo.tar.gz",
        "format": "tar.gz",
        "sha256": _sha(archive),
        "binary_sha256": _sha(binary),
    }
    lock = ToolchainLock.load(_lock(tmp_path, asset=asset))

    def download(url, destination):
        destination.write_bytes(archive)

    installed = ToolchainInstaller(
        lock, cache_root=tmp_path / "cache", downloader=download
    ).install("x86_64")

    assert installed["demo"].read_bytes() == binary
    assert installed["demo"].stat().st_mode & 0o111


def test_installer_rejects_download_checksum_mismatch(tmp_path):
    from scripts.lib.toolchain import (
        ToolchainError,
        ToolchainInstaller,
        ToolchainLock,
    )

    lock = ToolchainLock.load(_lock(tmp_path, asset=_raw_asset(b"expected")))

    def download(url, destination):
        destination.write_bytes(b"tampered")

    installer = ToolchainInstaller(
        lock, cache_root=tmp_path / "cache", downloader=download
    )

    with pytest.raises(ToolchainError, match="asset checksum"):
        installer.install("x86_64")
    assert not (tmp_path / "cache" / "demo" / "1.0.0" / "x86_64" / "demo").exists()


def test_installer_rejects_unsafe_tar_member(tmp_path):
    from scripts.lib.toolchain import (
        ToolchainError,
        ToolchainInstaller,
        ToolchainLock,
    )

    archive = _tar_bytes(member_name="../demo")
    asset = {
        "url": "memory://unsafe.tar.gz",
        "format": "tar.gz",
        "sha256": _sha(archive),
        "binary_sha256": _sha(b"tool-binary"),
    }
    lock = ToolchainLock.load(_lock(tmp_path, asset=asset))

    def download(url, destination):
        destination.write_bytes(archive)

    installer = ToolchainInstaller(
        lock, cache_root=tmp_path / "cache", downloader=download
    )

    with pytest.raises(ToolchainError, match="unsafe archive"):
        installer.install("x86_64")


def test_installer_keeps_versions_side_by_side(tmp_path):
    from scripts.lib.toolchain import ToolchainInstaller, ToolchainLock

    data = b"tool-binary"

    def download(url, destination):
        destination.write_bytes(data)

    first_lock = ToolchainLock.load(
        _lock(tmp_path / "first", asset=_raw_asset(data), version="1.0.0")
    )
    second_lock = ToolchainLock.load(
        _lock(tmp_path / "second", asset=_raw_asset(data), version="2.0.0")
    )
    cache = tmp_path / "cache"
    first = ToolchainInstaller(
        first_lock, cache_root=cache, downloader=download
    ).install("x86_64")
    second = ToolchainInstaller(
        second_lock, cache_root=cache, downloader=download
    ).install("x86_64")

    assert first["demo"].is_file()
    assert second["demo"].is_file()
    assert first["demo"] != second["demo"]

"""Versioned, checksum-verified Runner tool cache."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable


class ToolchainError(ValueError):
    """Raised when the tool lock or an installation is unsafe."""


class PreflightError(ValueError):
    """Raised with all unmet Runner requirements."""


GIB = 1024**3
MIN_CPU_COUNT = 4
MIN_MEMORY_AVAILABLE = 8 * GIB
# The floor a native build needs for image layers and the BuildKit cache. It
# doubles as the reserve the Agent scratch watchdog refuses to eat into, so
# lowering it hands that headroom to the Agent rather than to the build.
MIN_DISK_FREE = 5 * GIB
REQUIRED_TOOLS = ("hadolint", "jq", "opencode")


@dataclass(frozen=True)
class ToolAsset:
    url: str
    format: str
    sha256: str
    binary_sha256: str


@dataclass(frozen=True)
class ToolSpec:
    version: str
    binary: str
    assets: dict[str, ToolAsset]


@dataclass(frozen=True)
class ToolchainLock:
    schema_version: int
    cache_root: Path
    tools: dict[str, ToolSpec]

    @classmethod
    def load(cls, path: Path) -> "ToolchainLock":
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolchainError("toolchain lock must be valid JSON-compatible YAML") from exc

        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ToolchainError("unsupported toolchain lock schema")
        cache_root = Path(str(raw.get("cache_root", "")))
        if not cache_root.is_absolute():
            raise ToolchainError("toolchain cache_root must be absolute")

        raw_tools = raw.get("tools")
        if not isinstance(raw_tools, dict) or not raw_tools:
            raise ToolchainError("toolchain lock has no tools")

        tools: dict[str, ToolSpec] = {}
        for name, raw_tool in raw_tools.items():
            if not isinstance(raw_tool, dict):
                raise ToolchainError(f"{name}: invalid tool definition")
            version = str(raw_tool.get("version", "")).strip()
            binary = str(raw_tool.get("binary", "")).strip()
            raw_assets = raw_tool.get("assets")
            if not version or not binary or not isinstance(raw_assets, dict):
                raise ToolchainError(f"{name}: version, binary and assets are required")

            assets: dict[str, ToolAsset] = {}
            for architecture, raw_asset in raw_assets.items():
                if architecture not in {"aarch64", "common", "x86_64"}:
                    raise ToolchainError(f"{name}: unsupported architecture {architecture}")
                if not isinstance(raw_asset, dict):
                    raise ToolchainError(f"{name}/{architecture}: invalid asset")
                asset = ToolAsset(
                    url=str(raw_asset.get("url", "")).strip(),
                    format=str(raw_asset.get("format", "")).strip(),
                    sha256=str(raw_asset.get("sha256", "")).strip(),
                    binary_sha256=str(raw_asset.get("binary_sha256", "")).strip(),
                )
                if (
                    not asset.url.startswith(("https://", "memory://"))
                    or asset.format not in {"raw", "tar.gz"}
                    or not _is_sha256(asset.sha256)
                    or not _is_sha256(asset.binary_sha256)
                ):
                    raise ToolchainError(f"{name}/{architecture}: invalid locked asset")
                assets[architecture] = asset
            tools[str(name)] = ToolSpec(
                version=version,
                binary=binary,
                assets=assets,
            )
        return cls(schema_version=1, cache_root=cache_root, tools=tools)


@dataclass(frozen=True)
class RunnerSnapshot:
    architecture: str
    cpu_count: int
    memory_available_bytes: int
    disk_free_bytes: int
    docker_server_version: str
    buildx_version: str
    tools: dict[str, Path]


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_architecture(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"amd64", "x86_64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "aarch64"
    raise ToolchainError(f"unsupported Runner architecture: {machine}")


def parse_meminfo(content: str) -> int:
    for line in content.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    raise PreflightError("MemAvailable is missing from /proc/meminfo")


def load_tool_paths(path: Path) -> dict[str, Path]:
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("bootstrap toolchain output is invalid") from exc
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, dict):
        raise PreflightError("bootstrap toolchain output has no tools")
    paths: dict[str, Path] = {}
    for name, details in tools.items():
        if isinstance(details, dict) and details.get("path"):
            paths[str(name)] = Path(str(details["path"]))
    return paths


def evaluate_preflight(
    snapshot: RunnerSnapshot,
    *,
    expected_arch: str,
) -> dict[str, object]:
    actual_arch = normalize_architecture(snapshot.architecture)
    required_arch = normalize_architecture(expected_arch)
    failures: list[str] = []

    if actual_arch != required_arch:
        failures.append(
            f"native architecture mismatch: expected {required_arch}, got {actual_arch}"
        )
    if snapshot.cpu_count < MIN_CPU_COUNT:
        failures.append(f"Runner requires at least {MIN_CPU_COUNT} CPUs")
    if snapshot.memory_available_bytes < MIN_MEMORY_AVAILABLE:
        failures.append("Runner requires at least 8 GiB available memory")
    if snapshot.disk_free_bytes < MIN_DISK_FREE:
        failures.append(
            f"Runner requires at least {MIN_DISK_FREE // GIB} GiB free disk"
        )
    if not snapshot.docker_server_version.strip():
        failures.append("Docker daemon is unavailable")
    if not snapshot.buildx_version.strip():
        failures.append("Docker Buildx is unavailable")

    for name in REQUIRED_TOOLS:
        path = snapshot.tools.get(name)
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            failures.append(f"locked tool is missing or not executable: {name}")

    if failures:
        raise PreflightError("; ".join(failures))

    return {
        "status": "passed",
        "architecture": actual_arch,
        "resources": {
            "cpu_count": snapshot.cpu_count,
            "memory_available_bytes": snapshot.memory_available_bytes,
            "disk_free_bytes": snapshot.disk_free_bytes,
        },
        "docker_server_version": snapshot.docker_server_version,
        "buildx_version": snapshot.buildx_version,
        "tools": {name: str(snapshot.tools[name]) for name in REQUIRED_TOOLS},
    }


def _download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


class ToolchainInstaller:
    def __init__(
        self,
        lock: ToolchainLock,
        *,
        cache_root: Path | None = None,
        downloader: Callable[[str, Path], None] = _download,
    ) -> None:
        self.lock = lock
        self.cache_root = Path(cache_root or lock.cache_root)
        self.downloader = downloader

    def install(self, machine: str) -> dict[str, Path]:
        architecture = normalize_architecture(machine)
        installed: dict[str, Path] = {}
        for name in sorted(self.lock.tools):
            spec = self.lock.tools[name]
            asset_arch = architecture if architecture in spec.assets else "common"
            if asset_arch not in spec.assets:
                raise ToolchainError(f"{name}: no asset for {architecture}")
            asset = spec.assets[asset_arch]
            target = (
                self.cache_root
                / name
                / spec.version
                / asset_arch
                / spec.binary
            )
            if target.is_file() and _sha256(target) == asset.binary_sha256:
                target.chmod(0o755)
                installed[name] = target
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f".{name}-", dir=target.parent
            ) as temporary:
                temporary_dir = Path(temporary)
                downloaded = temporary_dir / "asset"
                self.downloader(asset.url, downloaded)
                if _sha256(downloaded) != asset.sha256:
                    raise ToolchainError(f"{name}: asset checksum mismatch")

                candidate = temporary_dir / spec.binary
                if asset.format == "raw":
                    shutil.copyfile(downloaded, candidate)
                else:
                    self._extract_binary(downloaded, candidate, spec.binary)

                if _sha256(candidate) != asset.binary_sha256:
                    raise ToolchainError(f"{name}: binary checksum mismatch")
                candidate.chmod(0o755)
                os.replace(candidate, target)
            installed[name] = target
        return installed

    @staticmethod
    def _extract_binary(archive_path: Path, destination: Path, binary: str) -> None:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            selected = None
            for member in archive.getmembers():
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or member.issym()
                    or member.islnk()
                ):
                    raise ToolchainError(
                        f"unsafe archive member: {member.name}"
                    )
                if member.isfile() and member_path.name == binary:
                    selected = member
            if selected is None:
                raise ToolchainError(f"archive does not contain {binary}")
            source = archive.extractfile(selected)
            if source is None:
                raise ToolchainError(f"archive cannot read {binary}")
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)

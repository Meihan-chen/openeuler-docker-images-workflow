"""Deterministic preflight policy for native architecture Runners."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from scripts.lib.toolchain import normalize_architecture


GIB = 1024**3
MIN_CPU_COUNT = 4
MIN_MEMORY_AVAILABLE = 8 * GIB
MIN_DISK_FREE = 15 * GIB
REQUIRED_TOOLS = ("dgoss", "goss", "hadolint", "jq", "opencode")


class PreflightError(ValueError):
    """Raised with all unmet Runner requirements."""


@dataclass(frozen=True)
class RunnerSnapshot:
    architecture: str
    cpu_count: int
    memory_available_bytes: int
    disk_free_bytes: int
    docker_server_version: str
    buildx_version: str
    tools: dict[str, Path]


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
    snapshot: RunnerSnapshot, *, expected_arch: str
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
        failures.append("Runner requires at least 15 GiB free disk")
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

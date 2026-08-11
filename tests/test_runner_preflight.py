import json
from pathlib import Path

import pytest


GIB = 1024**3


def _snapshot(tmp_path):
    from scripts.lib.toolchain import RunnerSnapshot

    tools = {}
    for name in ("hadolint", "jq", "opencode"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        tools[name] = path
    return RunnerSnapshot(
        architecture="x86_64",
        cpu_count=8,
        memory_available_bytes=14 * GIB,
        disk_free_bytes=32 * GIB,
        docker_server_version="27.5.1",
        buildx_version="github.com/docker/buildx v0.20.0",
        tools=tools,
    )


def test_preflight_accepts_ready_native_runner(tmp_path):
    from scripts.lib.toolchain import evaluate_preflight

    report = evaluate_preflight(_snapshot(tmp_path), expected_arch="x86_64")

    assert report["status"] == "passed"
    assert report["architecture"] == "x86_64"
    assert report["resources"]["disk_free_bytes"] == 32 * GIB


def test_preflight_rejects_cross_architecture_runner(tmp_path):
    from scripts.lib.toolchain import PreflightError, evaluate_preflight

    snapshot = _snapshot(tmp_path)

    with pytest.raises(PreflightError, match="expected aarch64"):
        evaluate_preflight(snapshot, expected_arch="aarch64")


def test_preflight_reports_all_missing_capabilities(tmp_path):
    from scripts.lib.toolchain import (
        PreflightError,
        RunnerSnapshot,
        evaluate_preflight,
    )

    snapshot = RunnerSnapshot(
        architecture="x86_64",
        cpu_count=2,
        memory_available_bytes=4 * GIB,
        disk_free_bytes=5 * GIB,
        docker_server_version="",
        buildx_version="",
        tools={},
    )

    with pytest.raises(PreflightError) as caught:
        evaluate_preflight(snapshot, expected_arch="x86_64")

    message = str(caught.value)
    for expected in (
        "at least 4 CPUs",
        "at least 8 GiB",
        "at least 10 GiB",
        "Docker daemon",
        "Buildx",
        "hadolint",
        "jq",
        "opencode",
    ):
        assert expected in message


def test_preflight_rejects_non_executable_tool(tmp_path):
    from scripts.lib.toolchain import PreflightError, evaluate_preflight

    snapshot = _snapshot(tmp_path)
    snapshot.tools["hadolint"].chmod(0o644)

    with pytest.raises(PreflightError, match="hadolint"):
        evaluate_preflight(snapshot, expected_arch="x86_64")


def test_parse_meminfo_uses_memavailable():
    from scripts.lib.toolchain import parse_meminfo

    value = parse_meminfo(
        """MemTotal:       16384000 kB
MemFree:         1000000 kB
MemAvailable:   14680064 kB
"""
    )

    assert value == 14680064 * 1024


def test_load_tool_paths_reads_bootstrap_output(tmp_path):
    from scripts.lib.toolchain import load_tool_paths

    tool = tmp_path / "hadolint"
    payload = tmp_path / "toolchain.json"
    payload.write_text(
        json.dumps(
            {
                "tools": {
                    "hadolint": {
                        "path": str(tool),
                        "version": "2.14.0",
                    }
                }
            }
        )
    )

    assert load_tool_paths(payload) == {"hadolint": tool}

#!/usr/bin/env python3
"""Collect and enforce native Runner capabilities before an image job."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.runner_preflight import (
    PreflightError,
    RunnerSnapshot,
    evaluate_preflight,
    load_tool_paths,
    parse_meminfo,
)


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def collect_snapshot(
    *,
    workspace: Path,
    toolchain_json: Path,
    meminfo_path: Path = Path("/proc/meminfo"),
    machine: str | None = None,
    cpu_count: int | None = None,
    command_output: Callable[[list[str]], str] = command_output,
    disk_free: Callable[[Path], int] | None = None,
) -> RunnerSnapshot:
    disk_free_fn = disk_free or (lambda path: shutil.disk_usage(path).free)
    return RunnerSnapshot(
        architecture=machine or platform.machine(),
        cpu_count=cpu_count if cpu_count is not None else (os.cpu_count() or 0),
        memory_available_bytes=parse_meminfo(meminfo_path.read_text()),
        disk_free_bytes=disk_free_fn(workspace),
        docker_server_version=command_output(
            ["docker", "version", "--format", "{{.Server.Version}}"]
        ),
        buildx_version=command_output(["docker", "buildx", "version"]),
        tools=load_tool_paths(toolchain_json),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-arch", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--toolchain-json", required=True, type=Path)
    parser.add_argument("--output-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshot = collect_snapshot(
            workspace=args.workspace,
            toolchain_json=args.toolchain_json,
        )
        report = evaluate_preflight(
            snapshot,
            expected_arch=args.expected_arch,
        )
    except (OSError, PreflightError) as exc:
        print(f"runner-preflight: error: {exc}", file=sys.stderr)
        return 2

    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        args.output_json.write_text(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

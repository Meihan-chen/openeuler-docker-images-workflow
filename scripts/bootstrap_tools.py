#!/usr/bin/env python3
"""Install the locked Runner toolchain into a versioned cache."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.toolchain import (
    ToolchainError,
    ToolchainInstaller,
    ToolchainLock,
    normalize_architecture,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=PROJECT_ROOT / ".github" / "toolchain.lock.yml",
    )
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--arch", default=platform.machine())
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--github-output", type=Path)
    return parser


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        architecture = normalize_architecture(args.arch)
        lock = ToolchainLock.load(args.lock)
        cache_root = args.cache_root or lock.cache_root
        installed = ToolchainInstaller(
            lock, cache_root=cache_root
        ).install(architecture)
    except (OSError, ToolchainError) as exc:
        print(f"bootstrap-tools: error: {exc}", file=sys.stderr)
        return 2

    tools = {
        name: {
            "path": str(path),
            "version": lock.tools[name].version,
        }
        for name, path in installed.items()
    }
    summary = {
        "architecture": architecture,
        "cache_root": str(cache_root),
        "tools": tools,
    }
    if args.output_json:
        _write_json(args.output_json, summary)
    if args.github_output:
        with args.github_output.open("a") as output:
            for name, details in sorted(tools.items()):
                output.write(f"{name}_path={details['path']}\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

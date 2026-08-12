#!/usr/bin/env python3
"""Copy only TaskSpec-declared native reports into a candidate layout."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.task_spec import TaskSpec  # noqa: E402


def copy_declared(
    *, task_spec: Path, source: Path, destination: Path
) -> list[str]:
    task = TaskSpec.from_json(task_spec.read_text())
    architectures = (
        task.architectures
        if task.schema_version == 2
        else ("x86_64", "aarch64")
    )
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for architecture in architectures:
        for suffix in ("json", "junit.xml"):
            name = f"{architecture}.{suffix}"
            path = source / name
            if not path.is_file():
                raise ValueError(f"declared native evidence is missing: {name}")
            shutil.copyfile(path, destination / name)
            copied.append(name)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-spec", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        for name in copy_declared(
            task_spec=args.task_spec,
            source=args.source,
            destination=args.destination,
        ):
            print(name)
    except (OSError, ValueError) as error:
        print(f"copy-declared-native-evidence: error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

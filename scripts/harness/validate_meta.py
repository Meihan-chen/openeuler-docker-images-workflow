#!/usr/bin/env python3
"""CLI facade for shared target-repository meta.yml validation."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.target_contract import (  # noqa: E402
    find_all_meta_files,
    validate_meta_file,
    validate_task_meta_file,
)


def main() -> None:
    repo_root = os.environ.get("TARGET_REPO_DIR") or os.environ.get(
        "GITHUB_WORKSPACE",
        os.getcwd(),
    )
    errors = []
    meta_files = find_all_meta_files(repo_root)
    for meta_path in sorted(meta_files):
        relative = os.path.relpath(meta_path, repo_root)
        errors.extend(
            f"{relative}: {message}"
            for message in validate_meta_file(meta_path)
        )

    if errors:
        print("::error::meta.yml validation errors found:")
        for error in errors:
            print(f"  {error}")
        raise SystemExit(1)
    print(f"Validated {len(meta_files)} meta.yml files: all OK")


if __name__ == "__main__":
    main()

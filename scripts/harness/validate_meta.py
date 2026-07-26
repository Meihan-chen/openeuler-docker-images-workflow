"""Validate meta.yml schema and consistency with actual file structure."""

import os
import sys
from pathlib import Path

import yaml


def validate_meta_file(meta_path: str) -> list[str]:
    """Validate a single meta.yml file. Returns list of error messages."""
    errors = []
    app_dir = os.path.dirname(meta_path)

    with open(meta_path) as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            return [f"Invalid YAML: {e}"]

    if not data:
        return ["meta.yml is empty"]

    for tag, entry in data.items():
        if not isinstance(entry, dict):
            errors.append(f"Tag '{tag}': entry must be a dict, got {type(entry).__name__}")
            continue

        # Validate tag format: {version}-oe{os_version}
        if "-oe" not in tag:
            errors.append(f"Tag '{tag}': must follow format '{{app-ver}}-oe{{os-ver}}'")

        # Validate path
        path = entry.get("path", "")
        if not path:
            errors.append(f"Tag '{tag}': missing 'path' field")
        else:
            full_path = os.path.join(app_dir, path)
            if not os.path.exists(full_path):
                errors.append(f"Tag '{tag}': path '{path}' does not exist")

        # Validate optional arch
        arch = entry.get("arch")
        if arch and arch not in ("x86_64", "aarch64"):
            errors.append(f"Tag '{tag}': arch must be 'x86_64' or 'aarch64', got '{arch}'")

    return errors


def find_all_meta_files(root: str) -> list[str]:
    """Find all meta.yml files under the repository root."""
    meta_files = []
    for dirpath, _, filenames in os.walk(root):
        if ".git" in dirpath:
            continue
        if "meta.yml" in filenames:
            meta_files.append(os.path.join(dirpath, "meta.yml"))
    return meta_files


def main() -> None:
    repo_root = os.environ.get("TARGET_REPO_DIR") or os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    meta_files = find_all_meta_files(repo_root)

    all_errors = []
    for meta_path in sorted(meta_files):
        rel = os.path.relpath(meta_path, repo_root)
        errors = validate_meta_file(meta_path)
        if errors:
            for e in errors:
                all_errors.append(f"{rel}: {e}")

    if all_errors:
        print("::error::meta.yml validation errors found:")
        for e in all_errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print(f"Validated {len(meta_files)} meta.yml files: all OK")


if __name__ == "__main__":
    main()
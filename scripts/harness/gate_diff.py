"""Enforce the "only add new files" constraint by checking the diff.

Compares HEAD against origin/master (the GitCode target repo default branch).
Since the generate job does a fresh clone + adds new files, the diff directly
reflects what the agent created.

Allowed: new files (A), appends to meta.yml / README.md / image-list.yml / image-info.yml
Forbidden: modifications to existing Dockerfiles, deletions, in-place edits.
"""

import os
import subprocess
import sys
from pathlib import Path


def _target_dir() -> Path:
    d = os.environ.get("TARGET_REPO_DIR")
    if not d:
        sys.exit("TARGET_REPO_DIR not set")
    return Path(d)


def get_changed_files(base: str = "origin/master") -> dict:
    """Get files changed in current branch vs base, categorized by change type."""
    result = subprocess.run(
        ["git", "diff", "--name-status", f"{base}...HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::error::git diff failed: {result.stderr}")
        sys.exit(1)

    changes = {"added": [], "modified": [], "deleted": [], "renamed": []}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0][0]
        if status == "A":
            changes["added"].append(parts[-1])
        elif status == "M":
            changes["modified"].append(parts[-1])
        elif status == "D":
            changes["deleted"].append(parts[-1])
        elif status.startswith("R"):
            changes["renamed"].append(parts[-1])

    return changes


def is_allowed_modification(path: str) -> bool:
    """Check if a file modification is allowed (append-only files)."""
    allowed = {"meta.yml", "README.md", "image-list.yml", "image-info.yml"}
    return os.path.basename(path) in allowed


def main() -> None:
    base = os.environ.get("GIT_BASE_REF", "origin/master")
    os.chdir(_target_dir())
    changes = get_changed_files(base)

    violations = []

    for path in changes["modified"]:
        if not is_allowed_modification(path):
            violations.append(f"MODIFIED (not allowed): {path}")

    for path in changes["deleted"]:
        violations.append(f"DELETED (not allowed): {path}")

    for path in changes["modified"]:
        if is_allowed_modification(path):
            result = subprocess.run(
                ["git", "diff", f"{base}...HEAD", "--", path],
                capture_output=True, text=True,
            )
            removed_lines = [
                l for l in result.stdout.split("\n")
                if l.startswith("-") and not l.startswith("---")
            ]
            if len(removed_lines) > 1:
                violations.append(
                    f"APPEND-ONLY violation: {path} removed {len(removed_lines)} lines"
                )

    if violations:
        print("::error::Diff gate violations:")
        for v in violations:
            print(f"  {v}")
        print("\nOnly new files may be added. Existing files may only be appended to.")
        sys.exit(1)

    added = len(changes["added"])
    appended = len(changes["modified"])
    print(f"Diff gate: {added} files added, {appended} files appended, 0 violations")


if __name__ == "__main__":
    main()

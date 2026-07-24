"""Enforce the "only add new files" constraint by checking the diff."""

import os
import subprocess
import sys


def get_changed_files(base: str = "origin/main") -> dict:
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
    filename = os.path.basename(path)
    return filename in allowed


def main() -> None:
    base = os.environ.get("GIT_BASE_REF", "origin/main")
    changes = get_changed_files(base)

    violations = []

    for path in changes["modified"]:
        if not is_allowed_modification(path):
            violations.append(f"MODIFIED (not allowed): {path}")

    for path in changes["deleted"]:
        violations.append(f"DELETED (not allowed): {path}")

    # Check that allowed files were only appended to, not modified in-place
    for path in changes["modified"]:
        if is_allowed_modification(path):
            result = subprocess.run(
                ["git", "diff", f"{base}...HEAD", "--", path],
                capture_output=True, text=True,
            )
            diff = result.stdout
            lines = diff.split("\n")
            removed_lines = [l for l in lines if l.startswith("-") and not l.startswith("---")]
            if len(removed_lines) > 1:
                violations.append(f"APPEND-ONLY violation: {path} removed {len(removed_lines)} lines")

    if violations:
        print("::error::Diff gate violations:")
        for v in violations:
            print(f"  {v}")
        print("\nOnly new files may be added. Existing files may only be appended to (meta.yml, README.md).")
        sys.exit(1)

    added = len(changes["added"])
    appended = len(changes["modified"])
    print(f"Diff gate: {added} files added, {appended} files appended, 0 violations")


if __name__ == "__main__":
    main()
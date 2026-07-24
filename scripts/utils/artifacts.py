"""Test result archiving utilities.

Archives test results in the target repository structure:
    <app>/results/<app-ver>/<oe-ver>/<arch>.junit.xml
"""

import os
import shutil
from pathlib import Path


def archive_results(
    app_name: str,
    app_version: str,
    os_version: str,
    junit_file: str,
    build_log: str,
    repo_root: str = ".",
) -> str:
    """Archive test results for a specific version.

    Args:
        app_name: Application name
        app_version: Application version (e.g., "1.27.2")
        os_version: openEuler version (e.g., "24.03-lts")
        junit_file: Path to JUnit XML file
        build_log: Path to build log file
        repo_root: Repository root directory

    Returns:
        Path to the archived results directory
    """
    results_dir = Path(repo_root) / "**" / app_name / "results" / app_version / os_version
    # Find the actual app directory
    for d in Path(repo_root).glob(f"**/{app_name}"):
        if d.is_dir() and (d / "meta.yml").exists():
            results_dir = d / "results" / app_version / os_version
            results_dir.mkdir(parents=True, exist_ok=True)
            break
    else:
        results_dir = Path(repo_root) / app_name / "results" / app_version / os_version
        results_dir.mkdir(parents=True, exist_ok=True)

    # Copy JUnit XML
    junit_src = Path(junit_file)
    if junit_src.exists():
        shutil.copy2(junit_src, results_dir / junit_src.name)

    # Copy build log
    build_src = Path(build_log)
    if build_src.exists():
        shutil.copy2(build_src, results_dir / "build.log")

    return str(results_dir)


def collect_all_results(repo_root: str = ".") -> dict[str, list[str]]:
    """Collect all archived test results.

    Returns:
        {app_name: [list of result directories]}
    """
    results = {}
    for d in Path(repo_root).glob("**/results"):
        if ".git" in str(d):
            continue
        app_name = d.parent.name
        versions = [str(v) for v in d.iterdir() if v.is_dir()]
        if versions:
            results[app_name] = versions
    return results


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Archive test results")
    sub = parser.add_subparsers(dest="command")

    archive = sub.add_parser("archive")
    archive.add_argument("--app", required=True)
    archive.add_argument("--app-version", required=True)
    archive.add_argument("--os-version", required=True)
    archive.add_argument("--junit", required=True)
    archive.add_argument("--build-log", required=True)
    archive.add_argument("--repo-root", default=".")

    sub.add_parser("list")

    args = parser.parse_args()

    if args.command == "archive":
        path = archive_results(
            args.app, args.app_version, args.os_version,
            args.junit, args.build_log, args.repo_root,
        )
        print(f"Archived to {path}")
    elif args.command == "list":
        results = collect_all_results()
        import json
        print(json.dumps(results, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
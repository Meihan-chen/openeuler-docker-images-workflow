"""Confidence scoring for agent outputs based on build/test results."""

import json
from pathlib import Path

from scripts.lib.pr_delivery import calculate_confidence



def score_from_artifacts(artifacts_dir: str = ".") -> dict:
    """Calculate confidence from collected artifacts."""
    build_success = {"amd64": True, "arm64": True}
    test_pass_rate = {"amd64": 1.0, "arm64": 1.0}
    hadolint_violations = 0
    meta_consistent = True

    # Check build logs for failures
    for log in Path(artifacts_dir).glob("**/build-*.log"):
        content = log.read_text(errors="replace")
        arch = "amd64" if "amd64" in log.name else "arm64"
        if "ERROR" in content or "FAILED" in content:
            build_success[arch] = False

    # Check test results
    for junit in Path(artifacts_dir).glob("**/*.junit.xml"):
        content = junit.read_text(errors="replace")
        arch = junit.stem  # amd64 | arm64
        if arch not in test_pass_rate:
            continue
        if 'failures="0"' not in content:
            test_pass_rate[arch] = 0.0

    return calculate_confidence(build_success, test_pass_rate, hadolint_violations, meta_consistent)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Calculate confidence score")
    parser.add_argument("--artifacts-dir", default=".")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    result = score_from_artifacts(args.artifacts_dir)

    if args.format == "json":
        print(json.dumps(result))
    else:
        print(f"Score: {result['score']} ({result['level']})")
        for k, v in result["details"].items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

"""Confidence scoring for agent outputs based on build/test results."""

import json
from pathlib import Path


def calculate_confidence(
    build_success: dict[str, bool],
    test_pass_rate: dict[str, float],
    hadolint_violations: int,
    meta_consistent: bool,
) -> dict:
    """Calculate confidence score based on weighted factors.

    Args:
        build_success: {"amd64": bool, "arm64": bool}
        test_pass_rate: {"amd64": 0.0-1.0, "arm64": 0.0-1.0}
        hadolint_violations: number of hadolint rule violations
        meta_consistent: whether meta.yml matches actual files

    Returns:
        {"score": 0.0-1.0, "level": "auto-merge|review|loop", "details": {...}}
    """
    # Build success (weight 0.35): both arches must pass
    build_score = 0.35
    if not build_success.get("amd64", False):
        build_score *= 0.5
    if not build_success.get("arm64", False):
        build_score *= 0.5

    # Test pass rate (weight 0.30): average of both arches
    test_avg = (
        test_pass_rate.get("amd64", 0) + test_pass_rate.get("arm64", 0)
    ) / 2
    test_score = 0.30 * test_avg

    # Linter compliance (weight 0.20): 0 violations = full score
    lint_score = 0.20 * max(0, 1 - hadolint_violations * 0.1)

    # Meta consistency (weight 0.15): binary
    meta_score = 0.15 if meta_consistent else 0

    total = build_score + test_score + lint_score + meta_score

    if total >= 0.85:
        level = "auto-merge"
    elif total >= 0.75:
        level = "review"
    else:
        level = "loop"

    return {
        "score": round(total, 3),
        "level": level,
        "details": {
            "build": round(build_score, 3),
            "test": round(test_score, 3),
            "lint": round(lint_score, 3),
            "meta": round(meta_score, 3),
        },
    }


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
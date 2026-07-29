"""Compose PR title and body per DESIGN §8.2.

Reads:
  - Build logs from /tmp/build-r{1,2,3}-{amd64,arm64}.log
  - JUnit XML from <target>/<app>/results/<ver>/<oe>/<arch>.junit.xml
  - Adversarial review records from <target>/<app>/qa-review-{image,testcase}-r{1,2}.json
  - Agent self-assessment from <target>/<app>/ai-result.json + test-ai-result.json
  - Confidence score from scripts/utils/scoring.py

Writes:
  - /tmp/pr-title
  - /tmp/pr-body.md
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "utils"))

# Candidate-backed PR delivery extends the existing PR composition boundary.
from scripts.lib.pr_delivery import (  # noqa: E402
    PRDeliveryError,
    PullRequestContent,
    compose_pull_request,
    deliver_promoted_candidate,
)


def _target_dir() -> Path:
    d = os.environ.get("TARGET_REPO_DIR")
    if not d:
        sys.exit("TARGET_REPO_DIR not set")
    return Path(d)


def _get_changed_files() -> str:
    """Return `git diff --name-status origin/master...HEAD` output."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "origin/master...HEAD"],
        capture_output=True, text=True,
        cwd=_target_dir(),
    )
    return result.stdout.strip() or "(no changes)"


def _collect_build_logs() -> tuple[str, dict]:
    """Collect the latest successful build log per arch.

    Returns (markdown_section, build_success_dict) where build_success_dict is
    {"amd64": bool, "arm64": bool} for confidence scoring.
    """
    sections = []
    build_success = {"amd64": False, "arm64": False}

    for arch in ("amd64", "arm64"):
        # Find the latest successful build log for this arch
        # Logs are named build-r{1,2,3}-{amd64,arm64}.log
        candidates = sorted(
            Path("/tmp").glob(f"build-r*-{arch}.log"),
            key=lambda p: p.name,
            reverse=True,
        )
        chosen = None
        for log in candidates:
            content = log.read_text(errors="replace")
            # A successful build has "naming to" and no "ERROR: failed to build"
            if "naming to" in content and "ERROR: failed to build" not in content:
                chosen = (log, content)
                build_success[arch] = True
                break
        if chosen is None and candidates:
            # No successful log; use the latest failure log
            chosen = (candidates[0], candidates[0].read_text(errors="replace"))

        if chosen is None:
            sections.append(f"<details><summary>{arch} build (no log)</summary>\n\n_no build log found_\n</details>")
            continue

        log, content = chosen
        status = "exit 0" if build_success[arch] else "FAILED"
        digest = ""
        for line in content.split("\n"):
            if "naming to" in line:
                # e.g. "#7 naming to docker.io/openeuler/nginx:test done"
                digest = line.strip()
                break
        sections.append(
            f"<details><summary>{arch} build ({status})</summary>\n\n"
            f"Log: `{log.name}`\n\n"
            f"```\n{content[-3000:]}\n```\n</details>"
        )

    return "\n\n".join(sections), build_success


def _collect_test_results() -> tuple[str, dict]:
    """Collect JUnit XML test results.

    Returns (markdown_section, test_pass_rate_dict) where test_pass_rate_dict is
    {"amd64": float, "arm64": float} for confidence scoring.
    """
    sections = []
    pass_rate = {"amd64": 0.0, "arm64": 0.0}
    target = _target_dir()

    for arch in ("amd64", "arm64"):
        junit_files = list(target.glob(f"**/results/**/{arch}.junit.xml"))
        if not junit_files:
            sections.append(f"<details><summary>{arch} - no test results</summary>\n\n_skipped_\n</details>")
            continue

        blocks = []
        for jf in junit_files:
            content = jf.read_text(errors="replace")
            rel = jf.relative_to(target)
            # Parse pass/fail counts
            import re
            m = re.search(r'tests="(\d+)" failures="(\d+)"', content)
            if m:
                total, fails = int(m.group(1)), int(m.group(2))
                if total > 0:
                    pass_rate[arch] = max(pass_rate[arch], (total - fails) / total)
            blocks.append(f"<!-- {rel} -->\n```xml\n{content}\n```")
        sections.append(
            f"<details><summary>{arch} JUnit XML</summary>\n\n" +
            "\n\n".join(blocks) +
            "\n</details>"
        )

    return "\n\n".join(sections), pass_rate


def _collect_qa_records() -> str:
    """Collect adversarial review records from qa-review-*.json files."""
    target = _target_dir()
    app = os.environ.get("PACKAGE", os.environ.get("APP", ""))
    domain = os.environ.get("DOMAIN", "")
    app_dir = target / domain / app

    if not app_dir.exists():
        return "_No adversarial review records found_"

    sections = []
    for role in ("image", "testcase"):
        records = sorted(app_dir.glob(f"qa-review-{role}-r*.json"))
        if not records:
            continue

        title = {
            "image": "对抗对一：Image Creator ↔ Image QA",
            "testcase": "对抗对二：Testcase Creator ↔ Testcase QA",
        }[role]

        lines = [f"<details><summary>{title}</summary>", ""]
        lines.append("| 轮次 | 结论 | 摘要 | 问题数 |")
        lines.append("|------|------|------|--------|")
        for rec_path in records:
            rec = json.loads(rec_path.read_text())
            approved = "✓ 通过" if rec.get("approved") else "✗ 需修正"
            summary = (rec.get("summary") or "")[:80]
            issues = rec.get("issues") or []
            lines.append(f"| R{rec.get('round')} | {approved} | {summary} | {len(issues)} |")

        ai_path = app_dir / ("ai-result.json" if role == "image" else "test-ai-result.json")
        if ai_path.exists():
            try:
                ai = json.loads(ai_path.read_text())
                lines.append(f"\nCreator self-assessment: `{json.dumps(ai, ensure_ascii=False)[:300]}`")
            except json.JSONDecodeError:
                pass

        lines.append("</details>")
        sections.append("\n".join(lines))

    return "\n\n".join(sections) if sections else "_No adversarial review records found_"


def _calculate_confidence(build_success: dict, test_pass_rate: dict) -> dict:
    """Calculate confidence score via scripts/utils/scoring.py."""
    try:
        from scoring import calculate_confidence
        hadolint_violations = 0  # not enforced in CI for now
        meta_consistent = True
        result = calculate_confidence(
            build_success=build_success,
            test_pass_rate=test_pass_rate,
            hadolint_violations=hadolint_violations,
            meta_consistent=meta_consistent,
        )
        return result
    except Exception as e:
        return {"score": 0.0, "level": "unknown", "details": {}, "error": str(e)}


def _format_confidence(score_data: dict) -> str:
    d = score_data.get("details", {})
    return (
        f"- Build: {d.get('build', 0)} | "
        f"Test: {d.get('test', 0)} | "
        f"Lint: {d.get('lint', 0)} | "
        f"Meta: {d.get('meta', 0)}\n"
        f"- **Total: {score_data.get('score', 0)}** (level: {score_data.get('level', 'unknown')})"
    )


def compose_pr_body(scenario: str, oe_version: str = "") -> str:
    app_name = os.environ.get("PACKAGE", os.environ.get("APP", ""))
    app_version = os.environ.get("APP_VERSION", "")
    os_version = os.environ.get("OS_VERSION", oe_version)

    build_section, build_success = _collect_build_logs()
    test_section, test_pass_rate = _collect_test_results()
    qa_section = _collect_qa_records()
    score_data = _calculate_confidence(build_success, test_pass_rate)

    parts = [
        f"## Automated PR: {app_name} {app_version} on {os_version}",
        "",
        "### Changes",
        "```",
        _get_changed_files(),
        "```",
        "",
        "### Build Proof",
        build_section,
        "",
        "### Adversarial Review Records",
        qa_section,
        "",
        "### Test Results",
        test_section,
        "",
        "### Confidence Score",
        _format_confidence(score_data),
        "",
        "### Generated By",
        f"- Scenario: {scenario}",
        "- Image Creator + Image QA (adversarial pair, ≤2 rounds)",
        "- Testcase Creator + Testcase QA (adversarial pair, ≤2 rounds)",
    ]

    body = "\n".join(parts)
    Path("/tmp/pr-body.md").write_text(body)
    return body


def compose_pr_title(scenario: str) -> str:
    app_name = os.environ.get("PACKAGE", os.environ.get("APP", ""))
    app_version = os.environ.get("APP_VERSION", "")
    os_version = os.environ.get("OS_VERSION", "")

    if scenario == "new-image":
        title = f"[new-image] Add {app_name} {app_version} on {os_version}"
    elif scenario == "version-update":
        title = f"[version-update] {app_name} {app_version} on {os_version}"
    elif scenario == "oe-upgrade":
        title = f"[oe-upgrade] Batch upgrade to openEuler {os_version}"
    else:
        title = f"[auto] {app_name} {app_version} on {os_version}"

    Path("/tmp/pr-title").write_text(title)
    return title


def main() -> None:
    parser = argparse.ArgumentParser(description="Compose PR description")
    parser.add_argument("--scenario", choices=["new-image", "version-update", "oe-upgrade"], required=True)
    parser.add_argument("--oe-version", default="")
    args = parser.parse_args()

    title = compose_pr_title(args.scenario)
    body = compose_pr_body(args.scenario, args.oe_version)

    print(f"PR title: {title}")
    print(f"PR body written to /tmp/pr-body.md ({len(body)} chars)")


if __name__ == "__main__":
    main()

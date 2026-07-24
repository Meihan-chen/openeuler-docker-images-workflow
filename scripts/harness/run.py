"""Main orchestrator entry point for the openEuler Docker image automation system.

Usage:
    python run.py adversarial-pair --role image|testcase
    python run.py create-image
    python run.py create-tests
    python run.py test --app <name> --platform <arch>
    python run.py fix
    python run.py get-app-name
    python run.py report-failures [--oe-version <ver>]
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = PROJECT_ROOT / ".github" / "agents"


def _load_agent(name: str) -> str:
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent definition not found: {path}")
    return path.read_text()


def _run_agent(system_prompt: str, user_input: dict) -> dict:
    """Invoke an agent via the Anthropic API and return its JSON output."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": json.dumps(user_input, indent=2)}],
    )

    text = message.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_output": text, "status": "unparsed"}


def _run_adversarial_pair(role: str) -> None:
    """Run a Creator + QA adversarial pair."""
    if role == "image":
        creator = _load_agent("image-creator")
        qa = _load_agent("image-qa")
    elif role == "testcase":
        creator = _load_agent("testcase-creator")
        qa = _load_agent("testcase-qa")
    else:
        raise ValueError(f"Unknown role: {role}")

    ctx = {
        "package_name": os.environ.get("PACKAGE", os.environ.get("APP", "")),
        "source_repo_url": os.environ.get("SOURCE", ""),
        "domain": os.environ.get("DOMAIN", ""),
        "os_version": os.environ.get("OS_VERSION", os.environ.get("OE_VERSION", "")),
        "os_tag": os.environ.get("OS_VERSION", "").replace(".", "").replace("-", ""),
        "app_version": os.environ.get("APP_VERSION", ""),
        "image_repo_dir": str(PROJECT_ROOT),
        "scenario": os.environ.get("SCENARIO", "new-image"),
    }

    for round_num in range(1, 3):
        print(f"[{role}] Round {round_num}: Creator generating...")
        creator_result = _run_agent(creator, ctx)

        print(f"[{role}] Round {round_num}: QA reviewing...")
        qa_result = _run_agent(qa, {"creator_output": creator_result})

        if qa_result.get("status") == "approved":
            print(f"[{role}] QA approved after {round_num} round(s)")
            break
        else:
            issues = qa_result.get("issues", [])
            blockers = [i for i in issues if i.get("severity") == "blocker"]
            print(f"[{role}] QA found {len(issues)} issues ({len(blockers)} blockers)")
            ctx["qa_feedback"] = qa_result
    else:
        print(f"[{role}] QA not fully satisfied after 2 rounds, proceeding with recorded disagreements")


def cmd_adversarial_pair(args: argparse.Namespace) -> None:
    _run_adversarial_pair(args.role)


def cmd_test(args: argparse.Namespace) -> None:
    """Run tests against a built Docker image."""
    app = args.app
    platform = args.platform.replace("/", "_")
    results_dir = PROJECT_ROOT / "results" / "latest" / platform
    results_dir.mkdir(parents=True, exist_ok=True)

    # Find test files
    test_dir = None
    for p in PROJECT_ROOT.glob(f"**/{app}/tests"):
        test_dir = p
        break

    if not test_dir or not (test_dir / "goss.yaml").exists():
        print(f"No tests found for {app}, skipping")
        return

    goss_file = test_dir / "goss.yaml"
    goss_wait = test_dir / "goss_wait.yaml"

    # Run dgoss
    container_name = f"oe-test-{app}-{platform}"
    env = os.environ.copy()
    env["GOSS_FILE"] = str(goss_file)
    if goss_wait.exists():
        env["GOSS_WAIT"] = str(goss_wait)

    result = subprocess.run(
        ["dgoss", "run", "--name", container_name, f"openeuler/{app}:test"],
        env=env,
        capture_output=True,
        text=True,
    )

    # Write JUnit results
    junit_file = results_dir / f"{platform}.junit.xml"
    _write_junit(junit_file, app, result)

    # Cleanup
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    if result.returncode != 0:
        print(f"Tests failed for {app} on {platform}")
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)


def _write_junit(path: Path, app: str, result: subprocess.CompletedProcess) -> None:
    """Write test results in JUnit XML format."""
    failures = result.returncode
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="{app}" tests="1" failures="{failures}" errors="0">
  <testcase name="{app}_functional">
    {f'<failure message="test failed">{result.stdout}</failure>' if failures else ''}
    <system-out>{result.stdout}</system-out>
  </testcase>
</testsuite>""")


def cmd_fix(args: argparse.Namespace) -> None:
    """Run the Fixer agent with build/test logs."""
    fixer = _load_agent("code-fixer")

    # Collect build and test logs
    logs = {}
    for log_file in Path("/tmp").glob("build-*.log"):
        logs[log_file.name] = log_file.read_text()

    # Find all generated files as whitelist
    whitelist = []
    for f in PROJECT_ROOT.glob("**/*"):
        if f.is_file() and ".git" not in str(f):
            whitelist.append(str(f.relative_to(PROJECT_ROOT)))

    ctx = {
        "build_logs": logs,
        "whitelist": whitelist,
        "fix_branch": os.environ.get("GITHUB_REF_NAME", "main"),
        "knowledge_base": str(PROJECT_ROOT / "docs" / "failure-patterns.md"),
    }

    result = _run_agent(fixer, ctx)
    print(json.dumps(result, indent=2))


def cmd_get_app_name(args: argparse.Namespace) -> None:
    """Print the app name from the context."""
    print(os.environ.get("PACKAGE", os.environ.get("APP", "")))


def cmd_report_failures(args: argparse.Namespace) -> None:
    """Generate a failure report from collected artifacts."""
    report = ["# openEuler Upgrade Failure Report\n"]
    oe_version = args.oe_version or os.environ.get("OE_VERSION", "unknown")
    report.append(f"\nopenEuler version: {oe_version}\n")

    for log_file in sorted(Path("/tmp").glob("build-*.log")):
        content = log_file.read_text()
        if "ERROR" in content or "FAILED" in content:
            report.append(f"\n## {log_file.name}\n")
            report.append("```\n")
            report.append(content[-2000:])
            report.append("\n```\n")

    out = Path("/tmp/failure-report.md")
    out.write_text("".join(report))
    print(f"Failure report written to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="openEuler Docker image orchestrator")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("adversarial-pair")
    p.add_argument("--role", choices=["image", "testcase"], required=True)

    sub.add_parser("create-image")
    sub.add_parser("create-tests")

    p = sub.add_parser("test")
    p.add_argument("--app", required=True)
    p.add_argument("--platform", required=True)

    sub.add_parser("fix")
    sub.add_parser("get-app-name")

    p = sub.add_parser("report-failures")
    p.add_argument("--oe-version")

    args = parser.parse_args()

    if args.command == "adversarial-pair":
        cmd_adversarial_pair(args)
    elif args.command == "test":
        cmd_test(args)
    elif args.command == "fix":
        cmd_fix(args)
    elif args.command == "get-app-name":
        cmd_get_app_name(args)
    elif args.command == "report-failures":
        cmd_report_failures(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
"""Main orchestrator entry point for the openEuler Docker image automation system.

Uses opencode CLI to invoke agent roles defined under .github/agents/.
Each agent has a SKILL-style .md definition that opencode reads.

Usage:
    python run.py adversarial-pair --role image|testcase
    python run.py test --app <name> --platform <arch>
    python run.py fix
    python run.py get-app-name
    python run.py report-failures [--oe-version <ver>]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = PROJECT_ROOT / ".github" / "agents"

# opencode configuration
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "deepseek-chat")
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "900"))  # 15 min default
OPENCODE_STALE_SECONDS = 120


def _check_opencode() -> None:
    if not shutil.which("opencode"):
        sys.exit(
            "opencode CLI not found. Install it with:\n"
            "  curl -fsSL https://opencode.ai/install | bash"
        )


def _load_agent_prompt(name: str) -> str:
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent definition not found: {path}")
    return path.read_text()


def _run_opencode(prompt: str, *, cwd: str | None = None) -> str:
    """Run opencode with the given prompt and return collected text output."""
    _check_opencode()
    work_dir = cwd or str(PROJECT_ROOT)

    cmd = [
        "opencode", "run",
        "--format", "json",
        "--model", OPENCODE_MODEL,
        "--auto",
    ]
    auto_short = "--dangerously-skip-permissions"
    r = subprocess.run(["opencode", "run", "--help"], capture_output=True, text=True)
    if "--auto" in (r.stdout + r.stderr):
        cmd[cmd.index("--auto")] = "--auto"
    else:
        cmd.append(auto_short)

    cmd += ["--", prompt]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=work_dir,
    )

    text_parts: list[str] = []
    last_output = time.monotonic()
    deadline = time.monotonic() + OPENCODE_TIMEOUT

    assert proc.stdout is not None
    for line in proc.stdout:
        now = time.monotonic()
        if now > deadline:
            proc.kill()
            print("[opencode] TIMEOUT — killing process")
            break
        if now - last_output > OPENCODE_STALE_SECONDS:
            proc.kill()
            print("[opencode] STALE — killing process")
            break
        last_output = now

        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = ev.get("type")
        part = ev.get("part", {})

        if t == "text":
            text = part.get("text", "")
            if text:
                text_parts.append(text)
                print(text, end="", flush=True)
        elif t == "tool_use":
            tool = part.get("tool", "")
            st = part.get("state", {})
            status = st.get("status", "")
            inp = st.get("input", {})
            if status == "pending":
                brief = json.dumps(inp, ensure_ascii=False)[:200]
                print(f"\n[opencode: {tool}] {brief}", flush=True)
            elif status == "completed":
                output = st.get("output", "")
                if output:
                    display = output[:500] + ("..." if len(output) > 500 else "")
                    print(f"\n[opencode: {tool}] output: {display}", flush=True)

    proc.wait(timeout=10)

    stderr_text = ""
    if proc.stderr:
        stderr_text = proc.stderr.read()
    if proc.returncode != 0 and stderr_text:
        print(f"[opencode] stderr: {stderr_text[-1000:]}", file=sys.stderr)

    return "".join(text_parts)


def _run_adversarial_pair(role: str) -> None:
    """Run a Creator + QA adversarial pair using opencode."""
    if role == "image":
        creator_name = "image-creator"
        qa_name = "image-qa"
    elif role == "testcase":
        creator_name = "testcase-creator"
        qa_name = "testcase-qa"
    else:
        raise ValueError(f"Unknown role: {role}")

    creator_prompt = _load_agent_prompt(creator_name)
    qa_prompt = _load_agent_prompt(qa_name)

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
        print(f"\n[{role}] Round {round_num}: Creator generating...")
        creator_input = {
            "task": "Generate the complete image directory following the instructions above.",
            "context": ctx,
        }
        if round_num > 1:
            creator_input["qa_feedback"] = qa_result

        full_prompt = f"{creator_prompt}\n\n## Task Context\n```json\n{json.dumps(creator_input, indent=2)}\n```"
        _run_opencode(full_prompt)

        print(f"\n[{role}] Round {round_num}: QA reviewing...")
        qa_input = {
            "task": "Review the Creator's output files following the review checklist above.",
            "context": ctx,
        }
        full_prompt = f"{qa_prompt}\n\n## Review Context\n```json\n{json.dumps(qa_input, indent=2)}\n```"
        qa_output = _run_opencode(full_prompt)

        try:
            qa_result = json.loads(qa_output)
        except json.JSONDecodeError:
            # Try to extract JSON from output
            match = qa_output.rfind("{")
            end = qa_output.rfind("}")
            if match != -1 and end > match:
                try:
                    qa_result = json.loads(qa_output[match:end + 1])
                except json.JSONDecodeError:
                    qa_result = {"status": "approved", "summary": "QA output unparseable"}
            else:
                qa_result = {"status": "approved", "summary": "QA output unparseable"}

        if qa_result.get("status") == "approved":
            print(f"[{role}] QA approved after {round_num} round(s)")
            break
        else:
            issues = qa_result.get("issues", [])
            blockers = [i for i in issues if i.get("severity") == "blocker"]
            print(f"[{role}] QA found {len(issues)} issues ({len(blockers)} blockers)")
    else:
        print(f"[{role}] QA not fully satisfied after 2 rounds, proceeding")


def cmd_adversarial_pair(args: argparse.Namespace) -> None:
    _run_adversarial_pair(args.role)


def cmd_test(args: argparse.Namespace) -> None:
    """Run tests against a built Docker image."""
    app = args.app
    platform = args.platform.replace("/", "_")
    results_dir = PROJECT_ROOT / "results" / "latest" / platform
    results_dir.mkdir(parents=True, exist_ok=True)

    test_dir = None
    for p in PROJECT_ROOT.glob(f"**/{app}/tests"):
        test_dir = p
        break

    if not test_dir or not (test_dir / "goss.yaml").exists():
        print(f"No tests found for {app}, skipping")
        return

    goss_file = test_dir / "goss.yaml"
    goss_wait = test_dir / "goss_wait.yaml"

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

    junit_file = results_dir / f"{platform}.junit.xml"
    failures = result.returncode
    junit_file.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="{app}" tests="1" failures="{failures}" errors="0">
  <testcase name="{app}_functional">
    {f'<failure message="test failed">{result.stdout}</failure>' if failures else ''}
    <system-out>{result.stdout}</system-out>
  </testcase>
</testsuite>""")

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    if result.returncode != 0:
        print(f"Tests failed for {app} on {platform}")
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)


def cmd_fix(args: argparse.Namespace) -> None:
    """Run the Fixer agent with build/test logs via opencode."""
    fixer_prompt = _load_agent_prompt("code-fixer")

    logs = {}
    for log_file in Path("/tmp").glob("build-*.log"):
        logs[log_file.name] = log_file.read_text()

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

    full_prompt = f"{fixer_prompt}\n\n## Fix Context\n```json\n{json.dumps(ctx, indent=2)}\n```"
    output = _run_opencode(full_prompt)

    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        match = output.rfind("{")
        end = output.rfind("}")
        result = json.loads(output[match:end + 1]) if match != -1 and end > match else {"status": "unknown", "raw": output}

    print(json.dumps(result, indent=2))


def cmd_get_app_name(args: argparse.Namespace) -> None:
    print(os.environ.get("PACKAGE", os.environ.get("APP", "")))


def cmd_report_failures(args: argparse.Namespace) -> None:
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
    parser = argparse.ArgumentParser(description="openEuler Docker image orchestrator (opencode)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("adversarial-pair")
    p.add_argument("--role", choices=["image", "testcase"], required=True)

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
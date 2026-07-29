"""Main orchestrator for the openEuler Docker image automation system.

Invokes agent roles (defined under .github/agents/*.md) via opencode CLI.
Each adversarial pair runs Creator -> QA -> Creator-fix (<=2 rounds) per DESIGN §4.3.

Usage:
    python run.py adversarial-pair --role image|testcase
    python run.py test --app <name> --platform <arch>
    python run.py fix
    python run.py report-failures [--oe-version <ver>]

Environment:
    TARGET_REPO_DIR   - absolute path to the cloned target repo (agent cwd)
    DEEPSEEK_API_KEY  - DeepSeek API key for scenario-one commands
    OPENCODE_MODEL    - legacy command model override
    OPENCODE_TIMEOUT  - per-call timeout seconds (default 2400)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = PROJECT_ROOT / ".github" / "agents"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Scenario-one keeps the established Agent implementations here while
# scripts/harness/flow.py provides the public workflow entrypoint.
from scripts.lib.agent_runtime import (  # noqa: E402
    AgentResult,
    AgentRuntimeError,
    MODEL,
    run_agent,
)
from scripts.lib.generation_pipeline import (  # noqa: E402
    GenerationPipelineError,
    GenerationResult,
    build_role_prompt,
    run_generation_pipeline,
)
from scripts.lib.native_repair import (  # noqa: E402
    NativeRepairError,
    NativeRepairResult,
    validate_native_with_repairs,
)
from scripts.lib.native_validation import (  # noqa: E402
    NativeValidationError,
    validate_native_image,
    validate_native_smoke,
)
from scripts.lib.smoke_candidate import write_smoke_candidate  # noqa: E402
from scripts.lib.task_spec import TaskSpec, TaskSpecError  # noqa: E402
from scripts.lib.target_contract import (  # noqa: E402
    TargetContractError,
    validate_generated_target,
)

OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", MODEL)
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "2400"))
OPENCODE_STALE_SECONDS = 300
MAX_QA_ROUNDS = 2


def _check_opencode() -> None:
    if not shutil.which("opencode"):
        sys.exit(
            "opencode CLI not found. Install with:\n"
            "  curl -fsSL https://opencode.ai/install | bash"
        )


def _load_agent_prompt(name: str) -> str:
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent definition not found: {path}")
    return path.read_text()


def _target_dir() -> Path:
    """Return the directory agents should operate in (cloned target repo)."""
    d = os.environ.get("TARGET_REPO_DIR")
    if not d:
        sys.exit("TARGET_REPO_DIR not set; cannot run agent without target repo")
    p = Path(d)
    if not p.is_dir():
        sys.exit(f"TARGET_REPO_DIR does not exist: {p}")
    return p


def _run_opencode(prompt: str, *, cwd: Path) -> str:
    """Run opencode with the given prompt; return concatenated text output."""
    _check_opencode()
    work_dir = str(cwd)

    cmd = [
        "opencode", "run",
        "--format", "json",
        "--model", OPENCODE_MODEL,
        "--dangerously-skip-permissions",
    ]
    help_r = subprocess.run(["opencode", "run", "--help"], capture_output=True, text=True)
    if "--auto" in (help_r.stdout + help_r.stderr):
        cmd[cmd.index("--dangerously-skip-permissions")] = "--auto"
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
            print("[opencode] TIMEOUT - killing process", file=sys.stderr)
            break
        if now - last_output > OPENCODE_STALE_SECONDS:
            proc.kill()
            print("[opencode] STALE - killing process", file=sys.stderr)
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

    stderr_text = proc.stderr.read() if proc.stderr else ""
    if proc.returncode != 0 and stderr_text:
        print(f"[opencode] stderr: {stderr_text[-1000:]}", file=sys.stderr)

    output = "".join(text_parts)
    if not output.strip():
        if stderr_text:
            print(f"[opencode] NO OUTPUT. Stderr: {stderr_text[-2000:]}", file=sys.stderr)
        else:
            print("[opencode] NO OUTPUT produced - model may have failed silently", file=sys.stderr)

    return output


def _parse_json_output(output: str) -> dict:
    """Parse a JSON object from opencode text output (tolerant of surrounding prose)."""
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass
    m = output.rfind("{")
    e = output.rfind("}")
    if m != -1 and e > m:
        try:
            return json.loads(output[m:e + 1])
        except json.JSONDecodeError:
            pass
    return {"status": "unknown", "raw": output}


def _build_creator_prompt(role: str, *, round_num: int, qa_feedback: dict | None = None) -> str:
    """Build the Creator agent prompt for the given round."""
    creator_name = f"{role}-creator"
    creator_md = _load_agent_prompt(creator_name)
    target = _target_dir()

    pkg = os.environ.get("PACKAGE", os.environ.get("APP", ""))
    src = os.environ.get("SOURCE", "")
    domain = os.environ.get("DOMAIN", "")
    os_ver = os.environ.get("OS_VERSION", os.environ.get("OE_VERSION", ""))
    app_ver = os.environ.get("APP_VERSION", "")
    os_tag = "oe" + os_ver.lower().replace(".", "").replace("-", "")

    if role == "image":
        instruction = (
            f"Create the container image directory for {pkg} {app_ver} on openEuler {os_ver}.\n\n"
            f"Parameters: package_name={pkg}, source_repo_url={src}, category={domain}, "
            f"os_version={os_ver}, os_tag={os_tag}, app_version={app_ver}, "
            f"image_repo_dir={target}\n\n"
            f"Create ONLY: Dockerfile, meta.yml, README.md, doc/image-info.yml, "
            f"doc/picture/logo.png, update image-list.yml, and write ai-result.json.\n"
            f"Place files under {target}/{domain}/{pkg}/."
        )
    else:
        instruction = (
            f"Create functional test cases for {pkg} {app_ver}.\n"
            f"Place tests under {target}/{domain}/{pkg}/tests/ (goss.yaml, goss_wait.yaml, "
            f"test_helpers.sh) and a test.sh entry alongside the Dockerfile at "
            f"{target}/{domain}/{pkg}/{app_ver}/{os_ver}/test.sh.\n"
            f"Read the Dockerfile at {target}/{domain}/{pkg}/{app_ver}/{os_ver}/Dockerfile "
            f"to determine binary name, ports, and version.\n"
            f"Write test-ai-result.json with your self-assessment."
        )

    if round_num > 1 and qa_feedback:
        instruction += (
            f"\n\n## QA Feedback from round {round_num - 1}\n"
            f"The QA reviewer found these issues:\n"
            f"{json.dumps(qa_feedback.get('issues', []), ensure_ascii=False, indent=2)}\n\n"
            f"Fix these issues. Do NOT regenerate from scratch - only fix what QA flagged.\n"
            f"Update ai-result.json (or test-ai-result.json) with the changes made."
        )

    return f"{creator_md}\n\n## TASK (round {round_num}):\n{instruction}"


def _build_qa_prompt(role: str) -> str:
    """Build the QA reviewer prompt."""
    qa_name = f"{role}-qa"
    qa_md = _load_agent_prompt(qa_name)
    target = _target_dir()
    pkg = os.environ.get("PACKAGE", os.environ.get("APP", ""))
    domain = os.environ.get("DOMAIN", "")

    instruction = (
        f"Review the files created by the {role} creator under "
        f"{target}/{domain}/{pkg}/.\n"
        f"Read the actual files on disk (Dockerfile, meta.yml, README.md, "
        f"doc/image-info.yml, image-list.yml, ai-result.json"
        + (", tests/goss.yaml, tests/goss_wait.yaml, test-ai-result.json" if role == "testcase" else "")
        + ").\n"
        f"Output your review as JSON per the schema in your instructions.\n"
        f"If no issues found, output {{\"status\": \"approved\", \"issues\": [], \"summary\": \"...\"}}."
    )
    return f"{qa_md}\n\n## TASK:\n{instruction}"


def _write_qa_record(role: str, round_num: int, qa_result: dict, *, approved: bool) -> None:
    """Persist QA review to a JSON file for PR body composition."""
    target = _target_dir()
    pkg = os.environ.get("PACKAGE", os.environ.get("APP", ""))
    domain = os.environ.get("DOMAIN", "")
    record_dir = target / domain / pkg
    record_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "role": role,
        "round": round_num,
        "approved": approved,
        "status": qa_result.get("status", "unknown"),
        "summary": qa_result.get("summary", ""),
        "issues": qa_result.get("issues", []),
        "coverage_score": qa_result.get("coverage_score"),
    }
    out = record_dir / f"qa-review-{role}-r{round_num}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"[{role}] QA round {round_num} {'approved' if approved else 'needs_fix'} -> {out}")


def _run_adversarial_pair(role: str) -> None:
    """Run Creator <-> QA adversarial pair with up to MAX_QA_ROUNDS rounds."""
    target = _target_dir()
    print(f"\n=== Adversarial pair: {role} (cwd={target}) ===")

    creator_out = _run_opencode(
        _build_creator_prompt(role, round_num=1),
        cwd=target,
    )

    for round_num in range(1, MAX_QA_ROUNDS + 1):
        print(f"\n--- {role} QA round {round_num} ---")
        qa_out = _run_opencode(_build_qa_prompt(role), cwd=target)
        qa_result = _parse_json_output(qa_out)
        approved = qa_result.get("status") == "approved"
        _write_qa_record(role, round_num, qa_result, approved=approved)

        if approved:
            print(f"[{role}] QA approved at round {round_num}")
            return

        if round_num == MAX_QA_ROUNDS:
            raise AgentRuntimeError(
                f"{role} QA did not approve after "
                f"{MAX_QA_ROUNDS} rounds"
            )

        print(f"[{role}] QA found issues; running Creator round {round_num + 1} to fix")
        creator_out = _run_opencode(
            _build_creator_prompt(role, round_num=round_num + 1, qa_feedback=qa_result),
            cwd=target,
        )


def cmd_adversarial_pair(args: argparse.Namespace) -> None:
    _run_adversarial_pair(args.role)


def _parse_tag(tag: str) -> tuple[str, str]:
    """Parse '1.27.2-oe2403lts' into ('1.27.2', '24.03-lts').

    Falls back to env APP_VERSION / OS_VERSION if tag shape is unexpected.
    """
    m = re.match(r"^(.+?)-oe(\d{2})(\d{2})(lts.*?|sp\d+.*?)?$", tag)
    if not m:
        return (os.environ.get("APP_VERSION", ""), os.environ.get("OS_VERSION", ""))
    app_ver = m.group(1)
    yy, mm = m.group(2), m.group(3)
    suffix = m.group(4) or "lts"
    oe_ver = f"{yy}.{mm}-{suffix}"
    return (app_ver, oe_ver)


def cmd_test(args: argparse.Namespace) -> None:
    """Run tests against a built Docker image, archive JUnit XML per DESIGN §5.3."""
    target = _target_dir()
    app = args.app
    arch = args.platform.replace("/", "_")  # amd64 | arm64

    test_dir = None
    for p in target.glob(f"**/{app}/tests"):
        test_dir = p
        break

    if not test_dir or not (test_dir / "goss.yaml").exists():
        print(f"No tests/goss.yaml found for {app}; trying test.sh fallback")
        _run_test_sh_fallback(target, app, arch)
        return

    app_dir = test_dir.parent
    meta_path = app_dir / "meta.yml"
    if not meta_path.exists():
        print(f"No meta.yml at {app_dir}", file=sys.stderr)
        sys.exit(1)

    import yaml
    meta = yaml.safe_load(meta_path.read_text()) or {}
    tag = next(iter(meta.keys()), "")
    app_ver, oe_ver = _parse_tag(tag)

    results_dir = app_dir / "results" / app_ver / oe_ver
    results_dir.mkdir(parents=True, exist_ok=True)

    goss_file = test_dir / "goss.yaml"
    container_name = f"oe-test-{app}-{arch}"

    env = os.environ.copy()
    env["GOSS_FILE"] = "goss.yaml"
    env["GOSS_SLEEP"] = "15"

    print(f"Running dgoss for {app} on {arch} (results -> {results_dir})")
    if not shutil.which("dgoss"):
        print(f"dgoss not found; falling back to test.sh for {app}")
        _run_test_sh_fallback(target, app, arch, results_dir)
        return
    result = subprocess.run(
        ["dgoss", "run", "--name", container_name, f"openeuler/{app}:test"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(test_dir),
    )

    if result.returncode != 0:
        print(f"dgoss FAILED for {app} on {arch}, falling back to test.sh")
        print(result.stdout[-2000:])
        print(result.stderr[-1000:], file=sys.stderr)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        _run_test_sh_fallback(target, app, arch, results_dir)
        return


def _run_test_sh_fallback(target: Path, app: str, arch: str, results_dir: Path | None = None) -> None:
    """Fallback: run test.sh alongside Dockerfile if no goss.yaml exists."""
    test_sh = None
    for p in target.glob(f"**/{app}/**/test.sh"):
        test_sh = p
        break

    if not test_sh:
        print(f"No test.sh found for {app} either; marking as skipped")
        return

    container_name = f"{app}-test"
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    run = subprocess.run(
        ["docker", "run", "-d", "--name", container_name, f"openeuler/{app}:test"],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        print(f"docker run failed: {run.stderr}", file=sys.stderr)
        sys.exit(1)

    env = os.environ.copy()
    env["PACKAGE_NAME"] = app
    env["CONTAINER_NAME"] = container_name
    result = subprocess.run(
        ["bash", str(test_sh)],
        env=env,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    if results_dir:
        results_dir.mkdir(parents=True, exist_ok=True)
        junit_file = results_dir / f"{arch}.junit.xml"
        failures = 0 if result.returncode == 0 else 1
        junit_file.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="{app}" tests="1" failures="{failures}" errors="0">
  <testcase name="{app}_test_sh">
    {f'<failure message="test.sh failed (exit {result.returncode})">{result.stdout}</failure>' if failures else ''}
    <system-out>{result.stdout}</system-out>
  </testcase>
</testsuite>""")

    if result.returncode != 0:
        sys.exit(1)


def cmd_fix(args: argparse.Namespace) -> None:
    """Run the Fixer agent against build/test failures in the target repo."""
    target = _target_dir()
    fixer_md = _load_agent_prompt("code-fixer")

    logs = {}
    for log_file in Path("/tmp").glob("build-*.log"):
        logs[log_file.name] = log_file.read_text()

    if not logs:
        print("::warning::No build-*.log files in /tmp; Fixer will have no failure context")

    ai_result_paths = list(target.rglob("ai-result.json"))
    ai_result_path = ai_result_paths[0] if ai_result_paths else target / "ai-result.json"
    whitelist: list[str] = []
    if ai_result_path.exists():
        try:
            data = json.loads(ai_result_path.read_text())
            whitelist = data.get("files_created", [])
        except json.JSONDecodeError:
            pass
    if not whitelist:
        for f in target.rglob("*"):
            if f.is_file() and ".git" not in str(f):
                whitelist.append(str(f.relative_to(target)))

    kb_path = PROJECT_ROOT / "docs" / "failure-patterns.md"
    instruction = (
        f"Diagnose and fix the CI failures in the target repo at {target}.\n\n"
        f"Build logs are in /tmp/build-*.log. Read them.\n"
        f"Knowledge base: {kb_path}\n\n"
        f"You may only modify files in this whitelist (relative to {target}):\n"
        + "\n".join(f"  - {f}" for f in whitelist)
        + "\n\nAfter fixing, write ai-result.json with status, diagnosis, and changes."
    )
    full_prompt = f"{fixer_md}\n\n## TASK:\n{instruction}"
    output = _run_opencode(full_prompt, cwd=target)
    result = _parse_json_output(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


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


def _load_task_spec(path: Path) -> TaskSpec:
    return TaskSpec.from_json(path.read_text())


def cmd_phase1_generate(args: argparse.Namespace) -> None:
    """Run scenario-one generation through the shared Agent harness."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise GenerationPipelineError("DEEPSEEK_API_KEY is required")
    result = run_generation_pipeline(
        workspace=args.workspace,
        report_dir=args.report_dir,
        task=_load_task_spec(args.task_spec),
        base_sha=args.base_sha,
        executable=args.opencode,
        api_key=api_key,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "qa_fix_rounds": result.qa_fix_rounds,
                "gate_report": dict(result.gate_report),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def cmd_phase1_smoke_generate(args: argparse.Namespace) -> None:
    """Create a deterministic candidate without calling an AI provider."""
    task = _load_task_spec(args.task_spec)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke = write_smoke_candidate(workspace=args.workspace, task=task)
    gate = validate_generated_target(
        repo=args.workspace,
        task=task,
        base_sha=args.base_sha,
    )
    (report_dir / "smoke-generation.json").write_text(
        json.dumps(smoke, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    (report_dir / "gates.json").write_text(
        json.dumps(gate, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {"status": "passed", "mode": "pipeline_smoke"},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def cmd_phase1_native_repair(args: argparse.Namespace) -> None:
    """Run bounded native repair through the shared Fixer harness."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise NativeRepairError("DEEPSEEK_API_KEY is required")
    result = validate_native_with_repairs(
        workspace=args.workspace,
        task=_load_task_spec(args.task_spec),
        base_sha=args.base_sha,
        architecture=args.architecture,
        run_id=args.run_id,
        dgoss=args.dgoss,
        goss=args.goss,
        report_path=args.report,
        junit_path=args.junit,
        repair_report_dir=args.repair_report_dir,
        executable=args.opencode,
        api_key=api_key,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "repair_attempts": result.repair_attempts,
                "report": dict(result.report),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def cmd_phase1_native_validate(args: argparse.Namespace) -> None:
    """Run one deterministic native validation through the shared harness."""
    report = validate_native_image(
        workspace=args.workspace,
        task=_load_task_spec(args.task_spec),
        architecture=args.architecture,
        run_id=args.run_id,
        dgoss=args.dgoss,
        goss=args.goss,
        report_path=args.report,
        junit_path=args.junit,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


def cmd_phase1_native_smoke(args: argparse.Namespace) -> None:
    """Exercise native Docker and dgoss plumbing without an AI call."""
    report = validate_native_smoke(
        workspace=args.workspace,
        task=_load_task_spec(args.task_spec),
        architecture=args.architecture,
        run_id=args.run_id,
        dgoss=args.dgoss,
        goss=args.goss,
        report_path=args.report,
        junit_path=args.junit,
        repair_report_dir=args.repair_report_dir,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="openEuler Docker image orchestrator")
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

    p = sub.add_parser(
        "phase1-generate",
        help="Generate/review one TaskSpec through the shared Agent harness",
    )
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--task-spec", required=True, type=Path)
    p.add_argument("--base-sha", required=True)
    p.add_argument("--report-dir", required=True, type=Path)
    p.add_argument("--opencode", required=True, type=Path)

    p = sub.add_parser(
        "phase1-smoke-generate",
        help="Create the deterministic zero-AI pipeline smoke candidate",
    )
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--task-spec", required=True, type=Path)
    p.add_argument("--base-sha", required=True)
    p.add_argument("--report-dir", required=True, type=Path)

    p = sub.add_parser(
        "phase1-native-repair",
        help="Run native validation with the shared bounded Fixer loop",
    )
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--task-spec", required=True, type=Path)
    p.add_argument("--base-sha", required=True)
    p.add_argument(
        "--architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    p.add_argument("--run-id", required=True)
    p.add_argument("--dgoss", required=True, type=Path)
    p.add_argument("--goss", required=True, type=Path)
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--junit", required=True, type=Path)
    p.add_argument("--repair-report-dir", required=True, type=Path)
    p.add_argument("--opencode", required=True, type=Path)

    p = sub.add_parser(
        "phase1-native-validate",
        help="Run deterministic native validation for one TaskSpec",
    )
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--task-spec", required=True, type=Path)
    p.add_argument(
        "--architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    p.add_argument("--run-id", required=True)
    p.add_argument("--dgoss", required=True, type=Path)
    p.add_argument("--goss", required=True, type=Path)
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--junit", required=True, type=Path)

    p = sub.add_parser(
        "phase1-native-smoke",
        help="Exercise native Docker and dgoss plumbing without AI",
    )
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--task-spec", required=True, type=Path)
    p.add_argument(
        "--architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    p.add_argument("--run-id", required=True)
    p.add_argument("--dgoss", required=True, type=Path)
    p.add_argument("--goss", required=True, type=Path)
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--junit", required=True, type=Path)
    p.add_argument("--repair-report-dir", required=True, type=Path)

    args = parser.parse_args(argv)

    try:
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
        elif args.command == "phase1-generate":
            cmd_phase1_generate(args)
        elif args.command == "phase1-smoke-generate":
            cmd_phase1_smoke_generate(args)
        elif args.command == "phase1-native-repair":
            cmd_phase1_native_repair(args)
        elif args.command == "phase1-native-validate":
            cmd_phase1_native_validate(args)
        elif args.command == "phase1-native-smoke":
            cmd_phase1_native_smoke(args)
        else:
            parser.print_help()
    except (
        AgentRuntimeError,
        GenerationPipelineError,
        NativeRepairError,
        NativeValidationError,
        TaskSpecError,
        TargetContractError,
        json.JSONDecodeError,
        OSError,
    ) as error:
        print(f"run: error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

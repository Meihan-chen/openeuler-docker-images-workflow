"""Native Docker build, runtime, persistence, evidence, and exact cleanup."""

from __future__ import annotations

import json
import os
import platform as runtime_platform
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable, Mapping, Sequence

from scripts.lib.progress import log, run_streaming
from scripts.lib.task_spec import TaskSpec


class NativeValidationError(RuntimeError):
    """Raised when native image validation fails."""


CommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], int],
    subprocess.CompletedProcess,
]

_PLATFORMS = {
    "x86_64": "linux/amd64",
    "aarch64": "linux/arm64",
}
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess:
    process_env = os.environ.copy()
    process_env.update(env)
    return run_streaming(
        command,
        cwd=cwd,
        env=process_env,
        timeout=timeout,
    )


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = runner(command, cwd, env or {}, timeout)
    if check and result.returncode != 0:
        detail = str(result.stderr or result.stdout or "command failed").strip()
        raise NativeValidationError(detail[:4000])
    return result


def _write_evidence(
    *,
    report_path: Path,
    junit_path: Path,
    report: dict[str, object],
    failure: str | None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    suite = ET.Element(
        "testsuite",
        {
            "name": f"kvrocks-{report['architecture']}",
            "tests": "1",
            "failures": "1" if failure else "0",
            "errors": "0",
            "time": str(report["duration_seconds"]),
        },
    )
    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "native-image-validation",
            "name": "build-runtime-persistence",
            "time": str(report["duration_seconds"]),
        },
    )
    if failure:
        node = ET.SubElement(case, "failure", {"message": failure[:500]})
        node.text = failure[:4000]
    ET.ElementTree(suite).write(
        junit_path,
        encoding="utf-8",
        xml_declaration=True,
    )


def _validate_tool(path: Path, name: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise NativeValidationError(f"{name} must be an absolute executable file")
    return path


def _os_name() -> str:
    try:
        values = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip().strip('"')
        return values.get("PRETTY_NAME") or values.get("NAME") or "unknown"
    except OSError:
        return "unknown"


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {
                "model name",
                "model",
                "hardware",
                "processor",
            }:
                candidate = value.strip()
                if candidate:
                    return candidate
    except OSError:
        pass
    return runtime_platform.processor() or "unknown"


def _environment_evidence(task: TaskSpec, architecture: str) -> dict[str, object]:
    try:
        numpy_version = metadata.version("numpy")
    except metadata.PackageNotFoundError:
        numpy_version = "not-installed"
    return {
        "test_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Model": os.environ.get("RUNNER_NAME", "self-hosted-native-runner"),
        "architecture": architecture,
        "kernel": runtime_platform.release(),
        "os": _os_name(),
        "cpu_model": _cpu_model(),
        "cpu_cores": os.cpu_count() or 0,
        "software_name": task.app,
        "software_version": task.version,
        "python_version": runtime_platform.python_version(),
        "numpy_version": numpy_version,
    }


def _wait_for_ping(
    *,
    runner: CommandRunner,
    workspace: Path,
    container: str,
    sleep: Callable[[float], None],
) -> None:
    last_detail = "Kvrocks did not become ready"
    for _ in range(30):
        result = _run(
            runner,
            [
                "docker",
                "exec",
                container,
                "redis-cli",
                "-p",
                "6666",
                "PING",
            ],
            cwd=workspace,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and "PONG" in str(result.stdout or ""):
            return
        last_detail = str(result.stderr or result.stdout or last_detail).strip()
        sleep(2)
    raise NativeValidationError(f"Kvrocks readiness failed: {last_detail}")


def validate_native_image(
    *,
    workspace: Path,
    task: TaskSpec,
    architecture: str,
    run_id: str,
    dgoss: Path,
    goss: Path,
    report_path: Path,
    junit_path: Path,
    runner: CommandRunner = _default_runner,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise NativeValidationError("run_id must be a positive integer")
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise NativeValidationError("target workspace does not exist")
    dgoss = _validate_tool(dgoss, "dgoss")
    goss = _validate_tool(goss, "goss")

    app_root = workspace / task.domain / task.app
    image_root = app_root / task.version / task.os_version
    tests_root = app_root / "tests"
    dockerfile = image_root / "Dockerfile"
    for required in (
        dockerfile,
        tests_root / "goss.yaml",
        tests_root / "goss_wait.yaml",
        tests_root / "test.sh",
    ):
        if not required.is_file():
            raise NativeValidationError(f"native validation input is missing: {required}")

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    prefix = f"oe-e2e-{run_id}-{slug}"
    builder = f"{prefix}-builder"
    dgoss_container = f"{prefix}-dgoss"
    container = f"{prefix}-persistence"
    volume = f"{prefix}-data"
    image = f"oe-autopilot/{task.app}:{task.version}-{run_id}-{slug}"
    persistence_value = f"run-{run_id}"
    start = time.monotonic()
    image_id = ""
    failure: str | None = None
    stage = f"native:{architecture}"
    log(stage, "START validation")

    try:
        log(stage, "START build")
        _run(
            runner,
            [
                "docker",
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
                "--use",
            ],
            cwd=workspace,
        )
        _run(
            runner,
            [
                "docker",
                "buildx",
                "build",
                "--builder",
                builder,
                "--load",
                "--progress",
                "plain",
                "--platform",
                platform,
                "--tag",
                image,
                "--file",
                str(dockerfile),
                str(image_root),
            ],
            cwd=workspace,
            timeout=3600,
        )
        log(stage, "PASS build")
        log(stage, "START dgoss")
        _run(
            runner,
            [
                str(dgoss),
                "run",
                "--name",
                dgoss_container,
                "--env",
                f"EXPECTED_VERSION={task.version}",
                image,
            ],
            cwd=workspace,
            env={
                "GOSS_PATH": str(goss),
                "GOSS_FILES_PATH": str(tests_root),
                "GOSS_FILE": "goss.yaml",
                "EXPECTED_VERSION": task.version,
            },
            timeout=300,
        )
        log(stage, "PASS dgoss")
        log(stage, "START persistence")
        _run(
            runner,
            ["docker", "volume", "create", volume],
            cwd=workspace,
        )
        _run(
            runner,
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--label",
                f"oe.autopilot.run={run_id}",
                "--volume",
                f"{volume}:/var/lib/kvrocks",
                "--volume",
                f"{tests_root}:/opt/oe-tests:ro",
                image,
            ],
            cwd=workspace,
        )
        _wait_for_ping(
            runner=runner,
            workspace=workspace,
            container=container,
            sleep=sleep,
        )
        _run(
            runner,
            [
                "docker",
                "exec",
                "--env",
                f"EXPECTED_VERSION={task.version}",
                container,
                "/opt/oe-tests/test.sh",
            ],
            cwd=workspace,
            timeout=300,
        )
        _run(
            runner,
            [
                "docker",
                "exec",
                container,
                "redis-cli",
                "-p",
                "6666",
                "SET",
                "oe-e2e-persistence",
                persistence_value,
            ],
            cwd=workspace,
        )
        _run(runner, ["docker", "stop", container], cwd=workspace)
        _run(runner, ["docker", "rm", container], cwd=workspace)
        _run(
            runner,
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--label",
                f"oe.autopilot.run={run_id}",
                "--volume",
                f"{volume}:/var/lib/kvrocks",
                image,
            ],
            cwd=workspace,
        )
        _wait_for_ping(
            runner=runner,
            workspace=workspace,
            container=container,
            sleep=sleep,
        )
        persisted = _run(
            runner,
            [
                "docker",
                "exec",
                container,
                "redis-cli",
                "-p",
                "6666",
                "GET",
                "oe-e2e-persistence",
            ],
            cwd=workspace,
        )
        if str(persisted.stdout or "").strip() != persistence_value:
            raise NativeValidationError("Kvrocks persistence value did not survive restart")
        inspected = _run(
            runner,
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            cwd=workspace,
        )
        image_id = str(inspected.stdout or "").strip()
        log(stage, "PASS persistence")
    except NativeValidationError as error:
        failure = str(error)
    finally:
        cleanup_commands = (
            ["docker", "rm", "--force", dgoss_container],
            ["docker", "rm", "--force", container],
            ["docker", "volume", "rm", "--force", volume],
            ["docker", "image", "rm", "--force", image],
            ["docker", "buildx", "rm", "--force", builder],
        )
        for command in cleanup_commands:
            _run(
                runner,
                command,
                cwd=workspace,
                timeout=300,
                check=False,
            )

    report: dict[str, object] = {
        "status": "failed" if failure else "passed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": platform,
        "image_id": image_id,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": {
            "native_build": failure is None,
            "dgoss": failure is None,
            "shared_tests": failure is None,
            "restart_persistence": failure is None,
        },
    }
    if failure:
        report["failure"] = failure
    _write_evidence(
        report_path=Path(report_path),
        junit_path=Path(junit_path),
        report=report,
        failure=failure,
    )
    if failure:
        log(stage, f"FAIL validation: {failure}")
        raise NativeValidationError(failure)
    log(stage, "PASS validation")
    return report


def validate_native_smoke(
    *,
    workspace: Path,
    task: TaskSpec,
    architecture: str,
    run_id: str,
    dgoss: Path,
    goss: Path,
    report_path: Path,
    junit_path: Path,
    repair_report_dir: Path,
    runner: CommandRunner = _default_runner,
) -> dict[str, object]:
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise NativeValidationError("run_id must be a positive integer")
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise NativeValidationError("target workspace does not exist")
    dgoss = _validate_tool(dgoss, "dgoss")
    goss = _validate_tool(goss, "goss")
    report_path = Path(report_path)
    junit_path = Path(junit_path)
    repair_report_dir = Path(repair_report_dir)

    context = report_path.parent / "pipeline-smoke-context"
    context.mkdir(parents=True, exist_ok=True)
    dockerfile = context / "Dockerfile"
    goss_file = context / "goss.yaml"
    dockerfile.write_text(
        f"FROM openeuler/openeuler:{task.os_version}\n"
        "RUN printf 'pipeline-smoke\\n' > /pipeline-smoke\n"
        'CMD ["sleep", "30"]\n'
    )
    goss_file.write_text(
        "file:\n"
        "  /pipeline-smoke:\n"
        "    exists: true\n"
        "    contains:\n"
        "      - pipeline-smoke\n"
    )

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    prefix = f"oe-smoke-{run_id}-{slug}"
    builder = f"{prefix}-builder"
    container = f"{prefix}-dgoss"
    image = f"oe-autopilot/pipeline-smoke:{run_id}-{slug}"
    stage = f"smoke:{architecture}"
    start = time.monotonic()
    image_id = ""
    failure: str | None = None
    log(stage, "START native plumbing")
    try:
        _run(
            runner,
            [
                "docker",
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
                "--use",
            ],
            cwd=workspace,
        )
        _run(
            runner,
            [
                "docker",
                "buildx",
                "build",
                "--builder",
                builder,
                "--load",
                "--progress",
                "plain",
                "--platform",
                platform,
                "--tag",
                image,
                str(context),
            ],
            cwd=workspace,
            timeout=1800,
        )
        _run(
            runner,
            [str(dgoss), "run", "--name", container, image],
            cwd=workspace,
            env={
                "GOSS_PATH": str(goss),
                "GOSS_FILES_PATH": str(context),
                "GOSS_FILE": "goss.yaml",
            },
            timeout=300,
        )
        inspected = _run(
            runner,
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            cwd=workspace,
        )
        image_id = str(inspected.stdout or "").strip()
    except NativeValidationError as error:
        failure = str(error)
    finally:
        for command in (
            ["docker", "rm", "--force", container],
            ["docker", "image", "rm", "--force", image],
            ["docker", "buildx", "rm", "--force", builder],
        ):
            _run(
                runner,
                command,
                cwd=workspace,
                timeout=300,
                check=False,
            )

    report: dict[str, object] = {
        "status": "failed" if failure else "passed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": platform,
        "image_id": image_id,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": {
            "native_build": failure is None,
            "dgoss": failure is None,
        },
    }
    if failure:
        report["failure"] = failure
    _write_evidence(
        report_path=report_path,
        junit_path=junit_path,
        report=report,
        failure=failure,
    )
    repair_report_dir.mkdir(parents=True, exist_ok=True)
    (repair_report_dir / f"native-repair-{architecture}.json").write_text(
        json.dumps(
            {
                "architecture": architecture,
                "mode": "pipeline_smoke",
                "repair_attempts": 0,
                "status": report["status"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if failure:
        log(stage, f"FAIL native plumbing: {failure}")
        raise NativeValidationError(failure)
    log(stage, "PASS native plumbing")
    return report

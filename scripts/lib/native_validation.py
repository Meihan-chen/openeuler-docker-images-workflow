"""Application-neutral native build, runtime tests, evidence, and cleanup."""

from __future__ import annotations

import hashlib
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
from scripts.lib.target_contract import validate_test_contract


class NativeValidationError(RuntimeError):
    """Raised when native image validation fails.

    Carries the structured failure so the caller can hand the Fixer a command,
    an exit code and both ends of the log instead of one opaque string.
    """

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, object] = dict(details or {})


CommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], int],
    subprocess.CompletedProcess,
]

_PLATFORMS = {
    "x86_64": "linux/amd64",
    "aarch64": "linux/arm64",
}
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
# The Fixer prompt asks for the earliest error because that is usually the root
# cause, so keeping only the tail hid exactly what it was told to look for.
_LOG_HEAD_CHARS = 2000
_LOG_TAIL_CHARS = 4000
_CONTAINER_LOG_LINES = "200"
_E2E_CHECKS = (
    "native_build",
    "dgoss",
    "shared_tests",
)
_SMOKE_CHECKS = _E2E_CHECKS


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


def _merged_output(result: subprocess.CompletedProcess) -> str:
    # run_streaming already folds stderr into stdout; injected runners may
    # still populate them separately, so read both rather than guessing.
    return "\n".join(
        part.strip()
        for part in (str(result.stdout or ""), str(result.stderr or ""))
        if part.strip()
    )


def _clip(text: str) -> tuple[str, str]:
    """Both ends of a failure log: the earliest error and the final error."""
    if len(text) <= _LOG_HEAD_CHARS + _LOG_TAIL_CHARS:
        return text, ""
    return text[:_LOG_HEAD_CHARS], text[-_LOG_TAIL_CHARS:]


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
        output = _merged_output(result) or "command failed"
        head, tail = _clip(output)
        omitted = len(output) - len(head) - len(tail)
        message = head if not tail else f"{head}\n...[{omitted} chars omitted]...\n{tail}"
        raise NativeValidationError(
            message,
            details={
                "command": list(command),
                "returncode": result.returncode,
                "stdout_head": head,
                "stdout_tail": tail,
            },
        )
    return result


def _container_evidence(
    runner: CommandRunner,
    *,
    workspace: Path,
    containers: Sequence[str],
) -> dict[str, object]:
    """Read what the containers themselves reported, before cleanup removes them.

    When an image builds but the application dies on startup, the container log
    is the only place the reason exists; the harness used to force-remove the
    container before anything read it.
    """
    evidence: dict[str, object] = {}
    for name in containers:
        inspected = _run(
            runner,
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}} {{.State.ExitCode}} {{.State.Error}}",
                name,
            ],
            cwd=workspace,
            timeout=60,
            check=False,
        )
        if inspected.returncode != 0:
            continue
        logs = _run(
            runner,
            ["docker", "logs", "--tail", _CONTAINER_LOG_LINES, name],
            cwd=workspace,
            timeout=60,
            check=False,
        )
        head, tail = _clip(_merged_output(logs))
        evidence[name] = {
            "state": _merged_output(inspected),
            "logs": head if not tail else f"{head}\n...\n{tail}",
        }
    return evidence


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
            "name": (
                f"{report['environment']['software_name']}-"
                f"{report['architecture']}"
            ),
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
            "name": "build-runtime-tests",
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


def validated_patch_digest(workspace: Path) -> str:
    """Digest the candidate content this workspace actually holds.

    The workspace is checked out at the immutable base SHA with the candidate
    patch applied and never committed, so diffing HEAD yields exactly the
    candidate. Recording it lets a later stage prove both architectures
    validated the same content instead of trusting job order.
    """
    workspace = Path(workspace)
    if not (workspace / ".git").is_dir():
        raise NativeValidationError(
            "target workspace must be a Git checkout to digest its candidate"
        )
    for arguments in (
        ["add", "--intent-to-add", "--", "."],
        ["diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--"],
    ):
        completed = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise NativeValidationError(
                (completed.stderr or b"").decode(errors="replace").strip()
                or "candidate digest failed"
            )
    return hashlib.sha256(completed.stdout).hexdigest()


def _builder_name(kind: str, run_id: str, architecture: str) -> str:
    return f"oe-{kind}-{run_id}-{architecture.replace('_', '-')}-builder"


def _ensure_builder(
    runner: CommandRunner,
    builder: str,
    *,
    cwd: Path,
) -> None:
    """Reuse this run's builder so later rounds keep their layer cache.

    The docker-container driver stores the BuildKit cache inside the builder
    container, so creating a fresh builder per validation would discard every
    cached layer and rebuild from source on each repair round.
    """
    existing = _run(
        runner,
        ["docker", "buildx", "inspect", builder],
        cwd=cwd,
        check=False,
    )
    if existing.returncode == 0:
        return
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
        ],
        cwd=cwd,
    )


def release_run_builders(
    *,
    run_id: str,
    architecture: str,
    workspace: Path,
    runner: CommandRunner = _default_runner,
) -> dict[str, object]:
    """Remove the builders this run owns on one architecture's runner."""
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise NativeValidationError("run_id must be a positive integer")
    listed = _run(
        runner,
        ["docker", "buildx", "ls", "--format", "{{.Name}}"],
        cwd=Path(workspace),
        timeout=300,
    )
    existing = {
        line.strip()
        for line in str(listed.stdout or "").splitlines()
        if line.strip()
    }
    released = []
    for kind in ("e2e", "smoke"):
        builder = _builder_name(kind, run_id, architecture)
        if builder not in existing:
            continue
        _run(
            runner,
            ["docker", "buildx", "rm", "--force", builder],
            cwd=Path(workspace),
            timeout=300,
        )
        released.append(builder)
    log(f"native:{architecture}", "PASS released run builders")
    return {
        "status": "passed",
        "architecture": architecture,
        "run_id": run_id,
        "released_builders": released,
    }


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


def _run_dgoss(
    runner: CommandRunner,
    *,
    workspace: Path,
    image: str,
    tests_root: Path,
    version: str,
    dgoss: Path,
    goss: Path,
    container: str,
    service_mode: bool,
) -> None:
    command = [str(dgoss), "run", "--name", container]
    if not service_mode:
        command.extend(("--entrypoint", "/bin/sh"))
    command.extend(("--env", f"EXPECTED_VERSION={version}", image))
    if not service_mode:
        command.extend(("-c", "sleep 300"))
    environment = {
        "GOSS_PATH": str(goss),
        "GOSS_FILES_PATH": str(tests_root),
        "GOSS_FILE": "goss.yaml",
        "EXPECTED_VERSION": version,
    }
    if service_mode:
        environment["GOSS_WAIT_OPTS"] = "-r 30s -s 1s"
    _run(
        runner,
        command,
        cwd=workspace,
        env=environment,
        timeout=300,
    )


def _run_shared_tests(
    runner: CommandRunner,
    *,
    workspace: Path,
    image: str,
    tests_root: Path,
    version: str,
    container: str,
    service_mode: bool,
    run_id: str,
) -> None:
    command = [
        "docker",
        "run",
        "--name",
        container,
        "--label",
        f"oe.autopilot.run={run_id}",
        "--volume",
        f"{tests_root}:/opt/oe-tests:ro",
    ]
    if service_mode:
        command.insert(2, "--detach")
        command.append(image)
        _run(runner, command, cwd=workspace)
        _run(
            runner,
            [
                "docker",
                "exec",
                "--env",
                f"EXPECTED_VERSION={version}",
                container,
                "/opt/oe-tests/test.sh",
            ],
            cwd=workspace,
            timeout=300,
        )
        return
    command.extend(
        (
            "--env",
            f"EXPECTED_VERSION={version}",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "exec /opt/oe-tests/test.sh",
        )
    )
    _run(runner, command, cwd=workspace, timeout=300)


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
    if not dockerfile.is_file():
        raise NativeValidationError(
            f"native validation input is missing: {dockerfile}"
        )
    test_contract = validate_test_contract(repo=workspace, task=task)
    service_mode = (tests_root / "goss_wait.yaml").is_file()

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    prefix = f"oe-e2e-{run_id}-{slug}"
    builder = f"{prefix}-builder"
    dgoss_container = f"{prefix}-dgoss"
    container = f"{prefix}-runtime"
    image = f"oe-autopilot/{task.app}:{task.version}-{run_id}-{slug}"
    validated_patch_sha256 = validated_patch_digest(workspace)
    start = time.monotonic()
    image_id = ""
    failure: str | None = None
    failure_details: dict[str, object] = {}
    container_evidence: dict[str, object] = {}
    # None means the check was never reached. Sharing one boolean across all
    # checks made a dgoss failure look like a build failure to the Fixer.
    checks: dict[str, bool | None] = {name: None for name in _E2E_CHECKS}
    current_check = ""
    stage = f"native:{architecture}"
    log(stage, "START validation")

    try:
        current_check = "native_build"
        log(stage, "START build")
        _ensure_builder(runner, builder, cwd=workspace)
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
        checks["native_build"] = True
        log(stage, "PASS build")
        inspected = _run(
            runner,
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            cwd=workspace,
        )
        image_id = str(inspected.stdout or "").strip()
        if test_contract["test_allowed"] is not True:
            current_check = "test_contract"
            raise NativeValidationError(
                "native test contract is not executable: "
                + "; ".join(str(error) for error in test_contract["errors"]),
                details={"findings": test_contract["findings"]},
            )
        current_check = "dgoss"
        log(stage, "START dgoss")
        _run_dgoss(
            runner,
            workspace=workspace,
            image=image,
            tests_root=tests_root,
            version=task.version,
            dgoss=dgoss,
            goss=goss,
            container=dgoss_container,
            service_mode=service_mode,
        )
        checks["dgoss"] = True
        log(stage, "PASS dgoss")
        current_check = "shared_tests"
        log(stage, "START shared tests")
        _run_shared_tests(
            runner,
            workspace=workspace,
            image=image,
            tests_root=tests_root,
            version=task.version,
            container=container,
            service_mode=service_mode,
            run_id=run_id,
        )
        checks["shared_tests"] = True
        log(stage, "PASS shared tests")
    except NativeValidationError as error:
        failure = str(error)
        failure_details = dict(error.details)
        if current_check in checks:
            checks[current_check] = False
        # Must run before the finally block force-removes the containers.
        container_evidence = _container_evidence(
            runner,
            workspace=workspace,
            containers=(dgoss_container, container),
        )
    finally:
        # The builder outlives this call on purpose: repair rounds re-enter
        # validation and must keep the cached builder stage. It is released
        # once per run by release_run_builders.
        cleanup_commands = (
            ["docker", "rm", "--force", dgoss_container],
            ["docker", "rm", "--force", container],
            ["docker", "image", "rm", "--force", image],
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
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": checks,
    }
    if failure:
        report["failure"] = failure
        report["failed_stage"] = current_check
        report["failure_details"] = failure_details
        if container_evidence:
            report["container_evidence"] = container_evidence
    _write_evidence(
        report_path=Path(report_path),
        junit_path=Path(junit_path),
        report=report,
        failure=failure,
    )
    if failure:
        log(stage, f"FAIL validation: {failure}")
        # Keep the structure a direct caller needs; only the report file had it.
        raise NativeValidationError(failure, details=failure_details)
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
    contexts: dict[str, Path] = {}
    for mode in ("service", "cli"):
        mode_root = context / mode
        mode_root.mkdir(parents=True, exist_ok=True)
        marker = f"/pipeline-smoke-{mode}"
        command = (
            'CMD ["sleep", "300"]\n'
            if mode == "service"
            else 'CMD ["unexpected-image-cmd"]\n'
        )
        (mode_root / "Dockerfile").write_text(
            f"FROM openeuler/openeuler:{task.os_version}\n"
            f"RUN printf 'pipeline-smoke-{mode}\\n' > {marker}\n"
            + command
        )
        (mode_root / "goss.yaml").write_text(
            "file:\n"
            f"  {marker}:\n"
            "    exists: true\n"
        )
        if mode == "service":
            (mode_root / "goss_wait.yaml").write_text(
                "process:\n  sleep:\n    running: true\n"
            )
        test_sh = mode_root / "test.sh"
        test_sh.write_text(
            "#!/bin/sh\nset -eu\n"
            "test \"$#\" -eq 0\n"
            f"test -f {marker}\n"
        )
        test_sh.chmod(0o755)
        contexts[mode] = mode_root

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    prefix = f"oe-smoke-{run_id}-{slug}"
    builder = f"{prefix}-builder"
    containers = [
        f"{prefix}-{mode}-{kind}"
        for mode in contexts
        for kind in ("dgoss", "runtime")
    ]
    images = {
        mode: f"oe-autopilot/pipeline-smoke-{mode}:{run_id}-{slug}"
        for mode in contexts
    }
    stage = f"smoke:{architecture}"
    validated_patch_sha256 = validated_patch_digest(workspace)
    start = time.monotonic()
    image_id = ""
    failure: str | None = None
    failure_details: dict[str, object] = {}
    checks: dict[str, bool | None] = {name: None for name in _SMOKE_CHECKS}
    current_check = ""
    log(stage, "START native plumbing")
    try:
        _ensure_builder(runner, builder, cwd=workspace)
        current_check = "native_build"
        for mode, mode_root in contexts.items():
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
                    images[mode],
                    str(mode_root),
                ],
                cwd=workspace,
                timeout=1800,
            )
        checks["native_build"] = True
        current_check = "dgoss"
        for mode, mode_root in contexts.items():
            _run_dgoss(
                runner,
                workspace=workspace,
                image=images[mode],
                tests_root=mode_root,
                version=task.version,
                dgoss=dgoss,
                goss=goss,
                container=f"{prefix}-{mode}-dgoss",
                service_mode=mode == "service",
            )
        checks["dgoss"] = True
        current_check = "shared_tests"
        for mode, mode_root in contexts.items():
            _run_shared_tests(
                runner,
                workspace=workspace,
                image=images[mode],
                tests_root=mode_root,
                version=task.version,
                container=f"{prefix}-{mode}-runtime",
                service_mode=mode == "service",
                run_id=run_id,
            )
        checks["shared_tests"] = True
        inspected = _run(
            runner,
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                images["service"],
            ],
            cwd=workspace,
        )
        image_id = str(inspected.stdout or "").strip()
    except NativeValidationError as error:
        failure = str(error)
        failure_details = dict(error.details)
        if current_check:
            checks[current_check] = False
    finally:
        # Released once per run by release_run_builders, like the e2e builder.
        cleanup = [
            ["docker", "rm", "--force", name] for name in containers
        ] + [
            ["docker", "image", "rm", "--force", image]
            for image in images.values()
        ]
        for command in cleanup:
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
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": checks,
    }
    if failure:
        report["failure"] = failure
        report["failed_stage"] = current_check
        report["failure_details"] = failure_details
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
        raise NativeValidationError(failure, details=failure_details)
    log(stage, "PASS native plumbing")
    return report

"""Application-neutral native build, runtime tests, evidence, and cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import platform as runtime_platform
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import deque
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
FormatValidator = Callable[..., Mapping[str, object]]

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
# An entrypoint that reports "export properties error" and exits has told the
# Fixer that something failed, not what. The reason is in a file the container
# wrote and docker logs never saw, so probe for those files by shape rather
# than by name: run 31106121623 spent 22 minutes rebuilding Kylin locally to
# read a shell.stderr that was sitting inside the container the whole time.
_PROBE_ROOTS = ("/opt", "/home", "/srv", "/app", "/usr/local", "/var/log")
_PROBE_NAMES = ("*.log", "*.out", "*.err", "*.stderr")
_PROBE_MAX_FILES = "20"
_PROBE_TAIL_LINES = "200"
# A Bigdata image carries tens of thousands of jars under these roots, so the
# walk is bounded three ways: it stays on one filesystem, it stops before the
# depth application logs are ever nested at, and it only considers files this
# run could have written. head then closes the pipe, which ends find early once
# the quota is met.
_PROBE_MAX_DEPTH = "6"
_PROBE_MAX_AGE_MINUTES = "180"
_E2E_CHECKS = (
    "native_build",
    "runtime_test",
)
_SMOKE_CHECKS = _E2E_CHECKS
_WAIT_STATUSES = frozenset(
    {
        "READY_HEALTH",
        "TERMINAL",
        "RUNNING_NO_PROBE",
        "PROBE_TIMEOUT",
        "RUNTIME_ERROR",
    }
)
_WAIT_RETURN_CODES = {
    "PROBE_TIMEOUT": 2,
    "RUNTIME_ERROR": 3,
}


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


def _run_optional_format_check(
    *,
    format_validator: FormatValidator | None,
    workspace: Path,
    architecture: str,
    report_path: Path,
) -> dict[str, object] | None:
    if format_validator is None:
        return None
    try:
        result = dict(
            format_validator(
                workspace=workspace,
                architecture=architecture,
                temp_root=report_path.parent / "upstream-format",
            )
        )
    except Exception as error:
        return {
            "status": "failed",
            "kind": "infra",
            "stage": "integration",
            "runner_architecture": architecture,
            "failure": str(error) or error.__class__.__name__,
        }
    if result.get("status") not in {"passed", "failed"}:
        return {
            **result,
            "status": "failed",
            "kind": "infra",
            "stage": "integration",
            "runner_architecture": architecture,
            "failure": "format validator returned an invalid status",
        }
    return result


def _format_failure(result: Mapping[str, object] | None) -> str | None:
    if result is None or result.get("status") == "passed":
        return None
    return str(
        result.get("failure")
        or result.get("output")
        or "upstream format check failed"
    )


def _format_failure_details(result: Mapping[str, object]) -> dict[str, object]:
    return {
        key: result[key]
        for key in ("kind", "stage", "commit_sha")
        if key in result
    }


def _merged_output(result: subprocess.CompletedProcess) -> str:
    # run_streaming already folds stderr into stdout; injected runners may
    # still populate them separately, so read both rather than guessing.
    return "\n".join(
        part.strip()
        for part in (str(result.stdout or ""), str(result.stderr or ""))
        if part.strip()
    )


def _raw_output(result: subprocess.CompletedProcess) -> str:
    stdout = str(result.stdout or "")
    stderr = str(result.stderr or "")
    if stdout and stderr and not stdout.endswith("\n"):
        return f"{stdout}\n{stderr}"
    return stdout + stderr


def _clip(text: str) -> tuple[str, str]:
    """Both ends of a failure log: the earliest error and the final error."""
    if len(text) <= _LOG_HEAD_CHARS + _LOG_TAIL_CHARS:
        return text, ""
    return text[:_LOG_HEAD_CHARS], text[-_LOG_TAIL_CHARS:]


def _container_log_summary(text: str) -> str:
    tail = "\n".join(text.splitlines()[-int(_CONTAINER_LOG_LINES) :])
    head, end = _clip(tail)
    return head if not end else f"{head}\n...\n{end}"


def _write_full_evidence(
    *,
    artifact_root: Path,
    diagnostics_dir: Path,
    name: str,
    suffix: str,
    content: str,
) -> dict[str, object]:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    path = diagnostics_dir / f"{name}.{suffix}"
    path.write_bytes(content.encode())
    return _file_metadata(path, artifact_root=artifact_root)


def _file_metadata(path: Path, *, artifact_root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(artifact_root).as_posix(),
        "size_bytes": path.stat().st_size,
    }


def _capture_status(
    metadata: dict[str, object],
    *,
    returncode: int,
) -> dict[str, object]:
    metadata["capture_status"] = {0: "complete", 124: "timeout"}.get(
        returncode,
        "failed",
    )
    return metadata


def _file_log_summary(path: Path) -> str:
    lines: deque[str] = deque(maxlen=int(_CONTAINER_LOG_LINES))
    with path.open(errors="replace") as stream:
        for line in stream:
            line = line.rstrip("\r\n")
            if len(line) > _LOG_HEAD_CHARS + _LOG_TAIL_CHARS:
                line = line[:_LOG_HEAD_CHARS] + line[-_LOG_TAIL_CHARS:]
            lines.append(line)
    return _container_log_summary("\n".join(lines))


def _stream_command_evidence(
    *,
    command: Sequence[str],
    cwd: Path,
    artifact_root: Path,
    path: Path,
    timeout: int,
) -> tuple[dict[str, object], str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("wb") as output:
            result = subprocess.run(
                list(command),
                cwd=cwd,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        returncode = 124
    metadata = _capture_status(
        _file_metadata(path, artifact_root=artifact_root),
        returncode=returncode,
    )
    return metadata, _file_log_summary(path)


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


def _run_with_full_evidence(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    artifact_root: Path,
    name: str,
    suffix: str,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
) -> tuple[subprocess.CompletedProcess, dict[str, object]]:
    """Run one streamed command and persist its unabridged output for repair."""
    result = runner(command, cwd, env or {}, timeout)
    try:
        metadata = _capture_status(
            _write_full_evidence(
                artifact_root=artifact_root,
                diagnostics_dir=artifact_root / "diagnostics",
                name=name,
                suffix=suffix,
                content=_raw_output(result),
            ),
            returncode=result.returncode,
        )
    except OSError as error:
        metadata = {
            "capture_status": "unavailable",
            "capture_error": str(error) or error.__class__.__name__,
        }
    if result.returncode != 0:
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
                "full_log": metadata,
            },
        )
    return result, metadata


def _probe_script() -> str:
    """Shell that reports what is running and dumps the logs docker never saw.

    Deliberately POSIX sh and best-effort throughout: a probe that fails on an
    unusual image must still return the part it managed to collect.
    """
    names = " -o ".join(f"-name '{pattern}'" for pattern in _PROBE_NAMES)
    roots = " ".join(_PROBE_ROOTS)
    return (
        "echo '### processes'\n"
        "ps -ef 2>/dev/null || ps aux 2>/dev/null || echo '(ps unavailable)'\n"
        f"for root in {roots}; do\n"
        '  [ -d "$root" ] || continue\n'
        f'  find "$root" -xdev -maxdepth {_PROBE_MAX_DEPTH} -type f'
        f" \\( {names} \\)"
        f" -mmin -{_PROBE_MAX_AGE_MINUTES} 2>/dev/null\n"
        "done"
        f" | head -n {_PROBE_MAX_FILES}"
        " | while IFS= read -r file; do\n"
        '  echo "### $file"\n'
        f'  tail -n {_PROBE_TAIL_LINES} "$file" 2>/dev/null\n'
        "done\n"
    )


def _container_evidence(
    runner: CommandRunner,
    *,
    workspace: Path,
    containers: Sequence[str],
    artifact_root: Path,
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
        log_command = ["docker", "logs", "--timestamps", name]
        if runner is _default_runner:
            log_metadata, log_summary = _stream_command_evidence(
                command=log_command,
                cwd=workspace,
                artifact_root=artifact_root,
                path=artifact_root / "diagnostics" / f"{name}.docker.log",
                timeout=60,
            )
        else:
            logs = _run(
                runner,
                log_command,
                cwd=workspace,
                timeout=60,
                check=False,
            )
            log_metadata = _capture_status(
                _write_full_evidence(
                    artifact_root=artifact_root,
                    diagnostics_dir=artifact_root / "diagnostics",
                    name=name,
                    suffix="docker.log",
                    content=_raw_output(logs),
                ),
                returncode=logs.returncode,
            )
            log_summary = _container_log_summary(_raw_output(logs))
        probe_command = ["docker", "exec", name, "sh", "-c", _probe_script()]
        if runner is _default_runner:
            probe_metadata, probe_summary = _stream_command_evidence(
                command=probe_command,
                cwd=workspace,
                artifact_root=artifact_root,
                path=artifact_root / "diagnostics" / f"{name}.probe.log",
                timeout=60,
            )
        else:
            probed = _run(
                runner,
                probe_command,
                cwd=workspace,
                timeout=60,
                check=False,
            )
            probe_metadata = _capture_status(
                _write_full_evidence(
                    artifact_root=artifact_root,
                    diagnostics_dir=artifact_root / "diagnostics",
                    name=name,
                    suffix="probe.log",
                    content=_raw_output(probed),
                ),
                returncode=probed.returncode,
            )
            probe_summary = _container_log_summary(_raw_output(probed))
        evidence[name] = {
            "state": _merged_output(inspected),
            "logs": log_summary,
            "full_logs": log_metadata,
            "probe": probe_summary,
            "full_probe": probe_metadata,
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


def write_infrastructure_failure_evidence(
    *,
    task: TaskSpec,
    architecture: str,
    failed_stage: str,
    failure: str,
    report_path: Path,
    junit_path: Path,
    attempts: int,
) -> dict[str, object]:
    """Record a pre-validation infrastructure failure for round evaluation."""
    if architecture not in _PLATFORMS:
        raise NativeValidationError(
            "architecture must be the native runner name x86_64 or aarch64"
        )
    report: dict[str, object] = {
        "status": "failed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": _PLATFORMS[architecture],
        "image_id": "",
        "validated_patch_sha256": "",
        "duration_seconds": 0.0,
        "environment": _environment_evidence(task, architecture),
        "checks": {name: None for name in _E2E_CHECKS},
        "failure": failure,
        "failed_stage": failed_stage,
        "failure_details": {
            "attempts": attempts,
            "retryable": True,
        },
    }
    if task.task_key:
        report["task_key"] = task.task_key
    _write_evidence(
        report_path=Path(report_path),
        junit_path=Path(junit_path),
        report=report,
        failure=failure,
    )
    return report


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


def _builder_name(
    kind: str,
    run_id: str,
    architecture: str,
    task_key: str = "",
) -> str:
    task_part = f"-{task_key}" if task_key else ""
    return (
        f"oe-{kind}-{run_id}{task_part}-"
        f"{architecture.replace('_', '-')}-builder"
    )


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
    task_key: str = "",
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
        builder = _builder_name(kind, run_id, architecture, task_key)
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
        "task_key": task_key,
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


def _parse_os_release(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z0-9_]+", key):
            raise NativeValidationError(
                f"image os-release line {number} is invalid"
            )
        try:
            tokens = shlex.split(raw_value, posix=True)
        except ValueError as error:
            raise NativeValidationError(
                f"image os-release line {number} has invalid quoting"
            ) from error
        if len(tokens) != 1:
            raise NativeValidationError(
                f"image os-release line {number} has an invalid value"
            )
        values[key] = tokens[0]
    return values


_OE_IDENTITY_VERSION_RE = re.compile(
    r"(?P<base>\d{2}\.\d{2})"
    r"(?:\s*(?:\(|-|\s)\s*LTS"
    r"(?:-?SP(?P<sp>\d+))?\s*\)?)?",
    re.IGNORECASE,
)


def _normalize_observed_oe(value: str) -> str:
    match = _OE_IDENTITY_VERSION_RE.search(value)
    if not match:
        return ""
    normalized = match.group("base").lower()
    matched = match.group(0).lower()
    if "lts" in matched:
        normalized += "-lts"
    if match.group("sp"):
        normalized += f"-sp{int(match.group('sp'))}"
    return normalized


def inspect_image_os_identity(
    *,
    runner: CommandRunner,
    workspace: Path,
    image_id: str,
    target_oe: str,
    container: str,
    report_dir: Path,
) -> dict[str, object]:
    """Read /etc/os-release through Docker without executing image code."""
    workspace = Path(workspace)
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    destination = report_dir / f"{container}.os-release"
    create = ["docker", "create", "--name", container, image_id]
    created = _run(runner, create, cwd=workspace, check=False)
    if created.returncode != 0:
        raise NativeValidationError(
            _merged_output(created) or "cannot create OS identity container",
            details={"command": create, "returncode": created.returncode},
        )
    try:
        copy = [
            "docker",
            "cp",
            f"{container}:/etc/os-release",
            str(destination),
        ]
        copied = _run(runner, copy, cwd=workspace, check=False)
        if copied.returncode != 0 or not destination.is_file():
            raise NativeValidationError(
                _merged_output(copied) or "image /etc/os-release is unavailable",
                details={"command": copy, "returncode": copied.returncode},
            )
        raw = destination.read_bytes()
        try:
            fields = _parse_os_release(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise NativeValidationError(
                "image /etc/os-release is not UTF-8"
            ) from error
        safe_fields = {
            name: fields[name]
            for name in ("ID", "VERSION_ID", "VERSION", "PRETTY_NAME")
            if name in fields
        }
        report: dict[str, object] = {
            "status": "passed",
            "expected_oe": target_oe,
            "observed_oe": _normalize_observed_oe(fields.get("VERSION_ID", "")),
            "fields": safe_fields,
            "os_release_sha256": hashlib.sha256(raw).hexdigest(),
        }
        errors: list[str] = []
        if fields.get("ID", "").lower() != "openeuler":
            errors.append("ID is not openEuler")
        if report["observed_oe"] != target_oe:
            errors.append(
                f"expected {target_oe} but found "
                f"{report['observed_oe'] or fields.get('VERSION_ID', 'unknown')}"
            )
        display_fields = [
            name for name in ("VERSION", "PRETTY_NAME") if fields.get(name)
        ]
        if not display_fields:
            errors.append("VERSION or PRETTY_NAME is required")
        for name in display_fields:
            observed = _normalize_observed_oe(fields[name])
            if observed != target_oe:
                errors.append(
                    f"{name} expected {target_oe} but found {observed or 'unknown'}"
                )
        if errors:
            report["status"] = "failed"
            report["errors"] = errors
            raise NativeValidationError(
                "; ".join(errors),
                details={"report": report},
            )
        return report
    finally:
        _run(
            runner,
            ["docker", "rm", "--force", container],
            cwd=workspace,
            timeout=300,
            check=False,
        )


def _image_has_healthcheck(
    runner: CommandRunner,
    *,
    workspace: Path,
    image_id: str,
) -> bool:
    inspected = _run(
        runner,
        ["docker", "image", "inspect", image_id],
        cwd=workspace,
    )
    try:
        config = json.loads(str(inspected.stdout or ""))[0]["Config"]
        healthcheck = config.get("Healthcheck")
        test = healthcheck.get("Test", []) if isinstance(healthcheck, dict) else []
        has_healthcheck = bool(test) and test != ["NONE"]
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise NativeValidationError(
            "docker image inspect returned invalid runtime configuration",
            details={"stage": "default_start", "error": str(error)},
        ) from error
    return has_healthcheck


def _supports_health_start_interval(
    runner: CommandRunner,
    *,
    workspace: Path,
) -> bool:
    version = _run(
        runner,
        ["docker", "version", "--format", "{{.Client.APIVersion}}"],
        cwd=workspace,
        timeout=30,
        check=False,
    )
    if version.returncode != 0:
        return False
    match = re.fullmatch(
        r"([0-9]+)\.([0-9]+)",
        str(version.stdout or "").strip(),
    )
    return match is not None and tuple(map(int, match.groups())) >= (1, 44)


def _inspect_runtime_state(
    runner: CommandRunner,
    *,
    workspace: Path,
    container: str,
) -> dict[str, object]:
    inspected = _run(
        runner,
        ["docker", "inspect", "--format", "{{json .State}}", container],
        cwd=workspace,
        check=False,
    )
    if inspected.returncode != 0:
        raise NativeValidationError(
            _merged_output(inspected) or "docker inspect failed",
            details={
                "stage": "post_inspect",
                "command": ["docker", "inspect", container],
                "returncode": inspected.returncode,
            },
        )
    try:
        state = json.loads(str(inspected.stdout or ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise NativeValidationError(
            "docker inspect returned invalid container state",
            details={"stage": "post_inspect", "error": str(error)},
        ) from error
    if not isinstance(state, dict):
        raise NativeValidationError(
            "docker inspect returned invalid container state",
            details={"stage": "post_inspect"},
        )
    return state


def _runtime_failure(
    stage: str,
    message: str,
    **details: object,
) -> dict[str, object]:
    return {
        "stage": stage,
        "failure": message,
        "failure_details": details,
    }


def _error_details(error: NativeValidationError) -> dict[str, object]:
    return {
        key: value
        for key, value in error.details.items()
        if key != "stage"
    }


def _wait_for_runtime_event(
    runner: CommandRunner,
    *,
    workspace: Path,
    waiter: Path,
    container: str,
    mode: str,
    wait_timeout: int,
) -> tuple[str, dict[str, object] | None, int | None]:
    command = [str(waiter), container, str(wait_timeout), mode]
    waiter_stderr = ""
    try:
        waited = _run(
            runner,
            command,
            cwd=workspace,
            timeout=wait_timeout + 10,
            check=False,
        )
    except (NativeValidationError, subprocess.SubprocessError, OSError) as error:
        status = "RUNTIME_ERROR"
        output = str(error) or error.__class__.__name__
        returncode = 124 if isinstance(error, subprocess.TimeoutExpired) else 1
    else:
        output = _merged_output(waited)
        waiter_stderr = str(waited.stderr or "")
        returncode = waited.returncode
        status = next(
            (
                line.strip()
                for line in str(waited.stdout or "").splitlines()
                if line.strip()
            ),
            "RUNTIME_ERROR",
        )
    if status not in _WAIT_STATUSES:
        status = "RUNTIME_ERROR"
    if returncode != _WAIT_RETURN_CODES.get(status, 0):
        status = "RUNTIME_ERROR"

    time_to_healthy = None
    if status == "READY_HEALTH":
        elapsed = re.search(
            r"^time_to_healthy_seconds=([0-9]+)$",
            waiter_stderr,
            flags=re.MULTILINE,
        )
        if elapsed is not None:
            time_to_healthy = int(elapsed.group(1))

    stage = "wait_healthcheck" if mode == "health" else "default_start"
    if status == "PROBE_TIMEOUT":
        return (
            status,
            _runtime_failure(
                stage,
                f"explicit {mode} probe did not become ready within "
                f"{wait_timeout} seconds",
                wait_status=status,
                output=output,
            ),
            time_to_healthy,
        )
    if status == "RUNTIME_ERROR":
        return (
            status,
            _runtime_failure(
                stage,
                "runtime readiness observation failed",
                wait_status=status,
                output=output,
                returncode=returncode,
            ),
            time_to_healthy,
        )
    return status, None, time_to_healthy


def _runtime_test_command(
    *,
    carrier: str,
    image_id: str,
    tests_root: Path,
    version: str,
    container: str,
    test_container: str,
    run_id: str,
    target_os_version: str = "",
    architecture: str = "",
) -> list[str]:
    environment = [f"EXPECTED_VERSION={version}"]
    if target_os_version:
        environment.append(f"EXPECTED_OS_VERSION={target_os_version}")
    if architecture:
        environment.append(f"TARGET_ARCH={architecture}")
    if carrier == "default":
        command = ["docker", "exec"]
        for value in environment:
            command.extend(("--env", value))
        return command + [container, "/bin/bash", "/opt/oe-tests/test.sh"]
    command = [
        "docker",
        "run",
        "--name",
        test_container,
        "--label",
        f"oe.autopilot.run={run_id}",
        "--volume",
        f"{tests_root}:/opt/oe-tests:ro",
    ]
    for value in environment:
        command.extend(("--env", value))
    return command + [
        "--entrypoint",
        "/bin/bash",
        image_id,
        "/opt/oe-tests/test.sh",
    ]


def _runtime_post_failure(
    *,
    wait_status: str,
    state: Mapping[str, object],
) -> dict[str, object] | None:
    if state.get("OOMKilled") is True or state.get("Error"):
        return _runtime_failure(
            "post_inspect",
            "container reports OOMKilled or State.Error",
            state=dict(state),
        )
    health = state.get("Health") or {}
    if wait_status == "READY_HEALTH" and (
        state.get("Status") != "running"
        or (
            isinstance(health, Mapping)
            and health.get("Status") == "unhealthy"
        )
    ):
        return _runtime_failure(
            "post_inspect",
            "ready container became unhealthy or stopped after test.sh",
            state=dict(state),
        )
    if wait_status == "RUNNING_NO_PROBE" and not (
        state.get("Status") == "running"
        or (state.get("Status") == "exited" and state.get("ExitCode") == 0)
    ):
        return _runtime_failure(
            "post_inspect",
            "no-probe container did not remain running or exit cleanly",
            state=dict(state),
        )
    return None


def _run_runtime_test(
    runner: CommandRunner,
    *,
    workspace: Path,
    image_id: str,
    tests_root: Path,
    version: str,
    container: str,
    test_container: str,
    run_id: str,
    waiter: Path,
    wait_timeout: int = 120,
    target_os_version: str = "",
    architecture: str = "",
) -> dict[str, object]:
    """Run test.sh once after readiness or lifecycle observation completes."""
    failures: list[dict[str, object]] = []

    def fail(
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        failures.append(_runtime_failure(stage, message, **details))

    runtime_config_available = True
    try:
        has_healthcheck = _image_has_healthcheck(
            runner,
            workspace=workspace,
            image_id=image_id,
        )
    except NativeValidationError as error:
        runtime_config_available = False
        has_healthcheck = False
        fail(
            "default_start",
            str(error),
            **_error_details(error),
        )
    create = [
        "docker",
        "create",
        "--name",
        container,
        "--label",
        f"oe.autopilot.run={run_id}",
        "--volume",
        f"{tests_root}:/opt/oe-tests:ro",
    ]
    if has_healthcheck:
        create.append("--health-interval=1s")
        if _supports_health_start_interval(runner, workspace=workspace):
            create.append("--health-start-interval=1s")
    create.append(image_id)
    mode = "health" if has_healthcheck else "none"
    wait_status = "RUNTIME_ERROR"
    time_to_healthy = None
    default_started = False
    created = (
        _run(
            runner,
            create,
            cwd=workspace,
            check=False,
        )
        if runtime_config_available
        else None
    )
    if created is not None:
        if created.returncode != 0:
            fail(
                "default_start",
                _merged_output(created) or "docker create failed",
                command=create,
                returncode=created.returncode,
            )
        else:
            start_command = ["docker", "start", container]
            started = _run(
                runner,
                start_command,
                cwd=workspace,
                check=False,
            )
            if started.returncode != 0:
                fail(
                    "default_start",
                    _merged_output(started) or "docker start failed",
                    command=start_command,
                    returncode=started.returncode,
                )
            else:
                default_started = True
                wait_status, wait_failure, time_to_healthy = _wait_for_runtime_event(
                    runner,
                    workspace=workspace,
                    waiter=waiter,
                    container=container,
                    mode=mode,
                    wait_timeout=wait_timeout,
                )
                if wait_failure is not None:
                    failures.append(wait_failure)

    carrier = "fresh"
    post_state: dict[str, object] = {}
    if default_started:
        try:
            state = _inspect_runtime_state(
                runner,
                workspace=workspace,
                container=container,
            )
        except NativeValidationError as error:
            fail(
                "default_start",
                str(error),
                **_error_details(error),
            )
        else:
            carrier = "default" if state.get("Status") == "running" else "fresh"
    test_command = _runtime_test_command(
        carrier=carrier,
        image_id=image_id,
        tests_root=tests_root,
        version=version,
        container=container,
        test_container=test_container,
        run_id=run_id,
        target_os_version=target_os_version,
        architecture=architecture,
    )
    tested = _run(
        runner,
        test_command,
        cwd=workspace,
        timeout=600,
        check=False,
    )
    if tested.returncode != 0:
        fail(
            "test_sh",
            _merged_output(tested) or "test.sh failed",
            command=test_command,
            returncode=tested.returncode,
        )
    if default_started:
        try:
            post_state = _inspect_runtime_state(
                runner,
                workspace=workspace,
                container=container,
            )
        except NativeValidationError as error:
            fail(
                "post_inspect",
                str(error),
                **_error_details(error),
            )
        else:
            post_failure = _runtime_post_failure(
                wait_status=wait_status,
                state=post_state,
            )
            if post_failure is not None:
                failures.append(post_failure)
    if failures:
        summary = "; ".join(
            f"{failure['stage']}: {failure['failure']}" for failure in failures
        )
        details: dict[str, object] = {
            "failures": failures,
            "wait_status": wait_status,
            "carrier": carrier,
            "test_attempted": True,
            "readiness_mode": mode,
            "has_healthcheck": has_healthcheck,
        }
        if time_to_healthy is not None:
            details["time_to_healthy"] = time_to_healthy
        raise NativeValidationError(
            summary,
            details=details,
        )
    outcome: dict[str, object] = {
        "wait_status": wait_status,
        "carrier": carrier,
        "state": post_state,
        "readiness_mode": mode,
        "has_healthcheck": has_healthcheck,
    }
    if time_to_healthy is not None:
        outcome["time_to_healthy"] = time_to_healthy
    return outcome


def validate_native_image(
    *,
    workspace: Path,
    task: TaskSpec,
    architecture: str,
    run_id: str,
    report_path: Path,
    junit_path: Path,
    runner: CommandRunner = _default_runner,
    format_validator: FormatValidator | None = None,
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
    report_path = Path(report_path)
    junit_path = Path(junit_path)
    start = time.monotonic()
    format_check = _run_optional_format_check(
        format_validator=format_validator,
        workspace=workspace,
        architecture=architecture,
        report_path=report_path,
    )
    app_root = workspace / (task.mdu_path or f"{task.domain}/{task.app}")
    image_root = app_root / task.version / task.os_version
    tests_root = app_root / "tests"
    dockerfile = image_root / "Dockerfile"
    if not dockerfile.is_file():
        raise NativeValidationError(
            f"native validation input is missing: {dockerfile}"
        )
    test_contract = validate_test_contract(repo=workspace, task=task)
    waiter = _validate_tool(
        Path(__file__).resolve().parents[1] / "harness" / "wait_ready.sh",
        "runtime waiter",
    )

    platform = _PLATFORMS[architecture]
    slug = architecture.replace("_", "-")
    task_part = f"-{task.task_key}" if task.task_key else ""
    prefix = f"oe-e2e-{run_id}{task_part}-{slug}"
    builder = f"{prefix}-builder"
    container = f"{prefix}-runtime"
    test_container = f"{prefix}-runtime-test"
    image_name = task.image_name or task.app
    image = (
        f"oe-autopilot/{image_name}:"
        f"{task.version}-{run_id}{task_part}-{slug}"
    )
    validated_patch_sha256 = validated_patch_digest(workspace)
    image_id = ""
    native_build_evidence: dict[str, object] = {}
    failures: list[dict[str, object]] = []
    container_evidence: dict[str, object] = {}
    runtime_observation: dict[str, object] = {}
    image_os_identity: Mapping[str, object] | None = None
    # None distinguishes a check that was never reached from a failed check.
    required_checks = (
        ("native_build", "os_identity", "runtime_test")
        if task.scenario == "oe-upgrade"
        else _E2E_CHECKS
    )
    checks: dict[str, bool | None] = {name: None for name in required_checks}
    stage = f"native:{architecture}"
    log(stage, "START validation")

    def record_failure(
        check: str,
        error: NativeValidationError,
        *,
        failed_stage: str | None = None,
    ) -> None:
        checks[check] = False
        if check == "runtime_test":
            for key in (
                "readiness_mode",
                "has_healthcheck",
                "time_to_healthy",
            ):
                if key in error.details:
                    runtime_observation[key] = error.details[key]
        substage_failures = error.details.get("failures")
        if check == "runtime_test" and isinstance(substage_failures, list):
            for substage in substage_failures:
                if not isinstance(substage, Mapping):
                    continue
                failures.append(
                    {
                        "stage": str(substage.get("stage") or check),
                        "check": check,
                        "failure": str(substage.get("failure") or error),
                        "failure_details": dict(
                            substage.get("failure_details") or {}
                        ),
                    }
                )
            if failures:
                log(stage, f"FAIL {check}: {error}")
                return
        failures.append(
            {
                "stage": failed_stage or check,
                "check": check,
                "failure": str(error),
                "failure_details": dict(error.details),
            }
        )
        log(stage, f"FAIL {check}: {error}")

    def run_check(check: str, action: Callable[[], object]) -> object | None:
        log(stage, f"START {check}")
        try:
            result = action()
        except NativeValidationError as error:
            record_failure(check, error)
            return None
        else:
            checks[check] = True
            log(stage, f"PASS {check}")
            return result

    def contract_error(check: str) -> NativeValidationError:
        findings = [
            finding
            for finding in test_contract["findings"]
            if finding.get("check") == check
        ]
        return NativeValidationError(
            "native test contract is not executable: "
            + "; ".join(str(finding["message"]) for finding in findings),
            details={"findings": findings},
        )

    try:
        try:
            log(stage, "START build")
            _ensure_builder(runner, builder, cwd=workspace)
            _, native_build_evidence = _run_with_full_evidence(
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
                artifact_root=report_path.parent,
                name="native_build",
                suffix="buildx.log",
                timeout=7200,
            )
            inspected = _run(
                runner,
                ["docker", "image", "inspect", "--format", "{{.Id}}", image],
                cwd=workspace,
            )
            image_id = str(inspected.stdout or "").strip()
        except NativeValidationError as error:
            full_log = error.details.get("full_log")
            if isinstance(full_log, Mapping):
                native_build_evidence = dict(full_log)
            record_failure("native_build", error)
        else:
            checks["native_build"] = True
            log(stage, "PASS build")
            if task.scenario == "oe-upgrade":
                image_os_identity = run_check(
                    "os_identity",
                    lambda: inspect_image_os_identity(
                        runner=runner,
                        workspace=workspace,
                        image_id=image_id,
                        target_oe=task.os_version,
                        container=f"{prefix}-os-identity",
                        report_dir=report_path.parent,
                    ),
                )
            if test_contract["runtime_test_allowed"] is True:
                runtime_outcome = run_check(
                    "runtime_test",
                    lambda: _run_runtime_test(
                        runner,
                        workspace=workspace,
                        image_id=image_id,
                        tests_root=tests_root,
                        version=task.version,
                        container=container,
                        test_container=test_container,
                        run_id=run_id,
                        waiter=waiter,
                        target_os_version=(
                            task.os_version if task.scenario == "oe-upgrade" else ""
                        ),
                        architecture=(
                            architecture if task.scenario == "oe-upgrade" else ""
                        ),
                    ),
                )
                if isinstance(runtime_outcome, Mapping):
                    runtime_state = runtime_outcome.get("state")
                    if isinstance(runtime_state, Mapping):
                        state_summary = " ".join(
                            str(runtime_state.get(key, ""))
                            for key in ("Status", "ExitCode", "Error")
                        ).strip()
                    else:
                        state_summary = ""
                    runtime_evidence: dict[str, object] = {
                        "state": state_summary,
                        "probe": (
                            f"wait_status={runtime_outcome.get('wait_status')} "
                            f"carrier={runtime_outcome.get('carrier')}"
                        ),
                        "readiness_mode": runtime_outcome.get("readiness_mode"),
                        "has_healthcheck": runtime_outcome.get("has_healthcheck"),
                    }
                    if runtime_outcome.get("time_to_healthy") is not None:
                        runtime_evidence["time_to_healthy"] = runtime_outcome[
                            "time_to_healthy"
                        ]
                    container_evidence[container] = runtime_evidence
            else:
                record_failure(
                    "runtime_test",
                    contract_error("runtime_test"),
                    failed_stage="test_contract",
                )
        if failures:
            # Must run before the finally block force-removes the containers.
            try:
                container_evidence = _container_evidence(
                    runner,
                    workspace=workspace,
                    containers=(container, test_container),
                    artifact_root=report_path.parent,
                )
            except Exception as error:
                capture_error = str(error) or error.__class__.__name__
                container_evidence = {"capture_error": capture_error}
                log(stage, f"WARN container evidence: {capture_error}")
            if runtime_observation:
                evidence = container_evidence.setdefault(container, {})
                if isinstance(evidence, dict):
                    evidence.update(runtime_observation)
    finally:
        # The builder outlives this call on purpose: repair rounds re-enter
        # validation and must keep the cached builder stage. It is released
        # once per run by release_run_builders.
        cleanup_commands = (
            ["docker", "rm", "--force", container],
            ["docker", "rm", "--force", test_container],
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

    format_failure = _format_failure(format_check)
    first_failure = failures[0] if failures else None
    failure = str(first_failure["failure"]) if first_failure else None
    overall_failure = failure or format_failure
    report: dict[str, object] = {
        "status": "failed" if overall_failure else "passed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": platform,
        "image_id": image_id,
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": checks,
    }
    if task.task_key:
        report["task_key"] = task.task_key
    if task.scenario == "oe-upgrade" and image_os_identity is not None:
        report["image_os_identity"] = dict(image_os_identity)
    if format_check is not None:
        report["format_check"] = format_check
    if native_build_evidence:
        report["native_build_evidence"] = native_build_evidence
    if first_failure:
        report["failure"] = failure
        report["failed_stage"] = first_failure["stage"]
        report["failure_details"] = first_failure["failure_details"]
        report["failures"] = failures
    elif format_failure:
        report["failure"] = format_failure
        report["failed_stage"] = "upstream_format"
        report["failure_details"] = _format_failure_details(format_check or {})
    if container_evidence:
        report["container_evidence"] = container_evidence
    _write_evidence(
        report_path=report_path,
        junit_path=junit_path,
        report=report,
        failure=overall_failure,
    )
    if overall_failure:
        failure_summary = "\n".join(
            f"{item['check']}: {item['failure']}" for item in failures
        ) or str(overall_failure)
        log(stage, f"FAIL validation: {failure_summary}")
        # Keep the structure a direct caller needs; only the report file had it.
        details = (
            dict(first_failure["failure_details"])
            if first_failure
            else _format_failure_details(format_check or {})
        )
        raise NativeValidationError(failure_summary, details=details)
    log(stage, "PASS validation")
    return report


def validate_native_smoke(
    *,
    workspace: Path,
    task: TaskSpec,
    architecture: str,
    run_id: str,
    report_path: Path,
    junit_path: Path,
    repair_report_dir: Path,
    runner: CommandRunner = _default_runner,
    format_validator: FormatValidator | None = None,
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
    waiter = _validate_tool(
        Path(__file__).resolve().parents[1] / "harness" / "wait_ready.sh",
        "runtime waiter",
    )
    report_path = Path(report_path)
    junit_path = Path(junit_path)
    repair_report_dir = Path(repair_report_dir)
    start = time.monotonic()
    format_check = _run_optional_format_check(
        format_validator=format_validator,
        workspace=workspace,
        architecture=architecture,
        report_path=report_path,
    )

    context = report_path.parent / "pipeline-smoke-context"
    contexts: dict[str, Path] = {}
    modes = {
        "health-ready": (
            'HEALTHCHECK CMD test -f /pipeline-smoke-health-ready\n'
            'CMD ["sleep", "300"]\n'
        ),
        "terminal": 'CMD ["/bin/bash", "-c", "exit 7"]\n',
        "exposed-no-health": 'EXPOSE 8080\nCMD ["sleep", "300"]\n',
        "no-probe": 'CMD ["sleep", "300"]\n',
        "probe-timeout": (
            'HEALTHCHECK CMD test -f /never-ready\n'
            'CMD ["sleep", "300"]\n'
        ),
    }
    for mode, docker_runtime in modes.items():
        mode_root = context / mode
        mode_root.mkdir(parents=True, exist_ok=True)
        marker = f"/pipeline-smoke-{mode}"
        (mode_root / "Dockerfile").write_text(
            f"FROM openeuler/openeuler:{task.os_version}\n"
            f"RUN printf 'pipeline-smoke-{mode}\\n' > {marker}\n"
            + docker_runtime
        )
        test_sh = mode_root / "test.sh"
        test_sh.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            "test \"$#\" -eq 0\n"
            f"test \"$EXPECTED_VERSION\" = {task.version!r}\n"
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
        for kind in ("runtime", "runtime-test")
    ]
    images = {
        mode: f"oe-autopilot/pipeline-smoke-{mode}:{run_id}-{slug}"
        for mode in contexts
    }
    stage = f"smoke:{architecture}"
    validated_patch_sha256 = validated_patch_digest(workspace)
    image_id = ""
    image_ids: dict[str, str] = {}
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
            inspected = _run(
                runner,
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    images[mode],
                ],
                cwd=workspace,
            )
            image_ids[mode] = str(inspected.stdout or "").strip()
        checks["native_build"] = True
        image_id = image_ids["health-ready"]
        current_check = "runtime_test"
        for mode, mode_root in contexts.items():
            try:
                _run_runtime_test(
                    runner,
                    workspace=workspace,
                    image_id=image_ids[mode],
                    tests_root=mode_root,
                    version=task.version,
                    container=f"{prefix}-{mode}-runtime",
                    test_container=f"{prefix}-{mode}-runtime-test",
                    run_id=run_id,
                    waiter=waiter,
                    wait_timeout=(
                        5 if mode in {"health-ready", "terminal"} else 0
                    ),
                )
            except NativeValidationError as error:
                expected_timeout = (
                    mode == "probe-timeout"
                    and error.details.get("test_attempted") is True
                    and error.details.get("wait_status") == "PROBE_TIMEOUT"
                    and any(
                        isinstance(item, Mapping)
                        and item.get("stage") == "wait_healthcheck"
                        and isinstance(item.get("failure_details"), Mapping)
                        and item["failure_details"].get("wait_status")
                        == "PROBE_TIMEOUT"
                        for item in error.details.get("failures", [])
                    )
                )
                if not expected_timeout:
                    raise
            else:
                if mode == "probe-timeout":
                    raise NativeValidationError(
                        "pipeline smoke expected explicit probe timeout"
                    )
        checks["runtime_test"] = True
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

    format_failure = _format_failure(format_check)
    overall_failure = failure or format_failure
    report: dict[str, object] = {
        "status": "failed" if overall_failure else "passed",
        "task_id": task.task_id,
        "architecture": architecture,
        "platform": platform,
        "image_id": image_id,
        "validated_patch_sha256": validated_patch_sha256,
        "duration_seconds": round(time.monotonic() - start, 3),
        "environment": _environment_evidence(task, architecture),
        "checks": checks,
    }
    if format_check is not None:
        report["format_check"] = format_check
    if failure:
        report["failure"] = failure
        report["failed_stage"] = current_check
        report["failure_details"] = failure_details
    elif format_failure:
        report["failure"] = format_failure
        report["failed_stage"] = "upstream_format"
        report["failure_details"] = _format_failure_details(format_check or {})
    _write_evidence(
        report_path=report_path,
        junit_path=junit_path,
        report=report,
        failure=overall_failure,
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
    if overall_failure:
        log(stage, f"FAIL native plumbing: {overall_failure}")
        details = (
            failure_details
            if failure
            else _format_failure_details(format_check or {})
        )
        raise NativeValidationError(overall_failure, details=details)
    log(stage, "PASS native plumbing")
    return report

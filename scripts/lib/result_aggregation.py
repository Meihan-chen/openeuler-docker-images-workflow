"""Aggregate two native validation reports into bounded in-repository evidence."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping

from scripts.lib.task_spec import TaskSpec


class ResultAggregationError(ValueError):
    """Raised when native evidence is incomplete, failed, or unsafe to archive."""


_ARCHITECTURES = ("x86_64", "aarch64")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_FIELDS = {
    "test_time",
    "Model",
    "architecture",
    "kernel",
    "os",
    "cpu_model",
    "cpu_cores",
    "software_name",
    "software_version",
    "python_version",
    "numpy_version",
}
_MAX_RESULT_BYTES = 20 * 1024


def _load_report(
    path: Path,
    architecture: str,
    task: TaskSpec,
    *,
    allow_legacy_evidence: bool = False,
) -> dict[str, object]:
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ResultAggregationError(
            f"{architecture} native report is invalid"
        ) from error
    if not isinstance(report, dict):
        raise ResultAggregationError(f"{architecture} native report must be an object")
    if report.get("status") != "passed":
        raise ResultAggregationError(f"{architecture} native validation did not pass")
    if report.get("task_id") != task.task_id:
        raise ResultAggregationError(f"{architecture} report belongs to another task")
    if report.get("architecture") != architecture:
        raise ResultAggregationError(f"{architecture} report architecture is inconsistent")
    if not str(report.get("image_id", "")).startswith("sha256:"):
        raise ResultAggregationError(f"{architecture} report has no image ID")
    recorded = str(report.get("validated_patch_sha256", ""))
    if not _SHA256_RE.fullmatch(recorded) and not (
        allow_legacy_evidence and not recorded
    ):
        raise ResultAggregationError(
            f"{architecture} report does not record the candidate it validated"
        )
    checks = report.get("checks")
    if not isinstance(checks, dict) or not checks or not all(
        value is True for value in checks.values()
    ):
        raise ResultAggregationError(f"{architecture} report checks are incomplete")
    environment = report.get("environment")
    if not isinstance(environment, dict) or set(environment) != _ENVIRONMENT_FIELDS:
        raise ResultAggregationError(
            f"{architecture} report environment must contain 11 fields"
        )
    if (
        environment.get("architecture") != architecture
        or environment.get("software_name") != task.app
        or environment.get("software_version") != task.version
    ):
        raise ResultAggregationError(
            f"{architecture} report environment does not match TaskSpec"
        )
    return report


def _load_junit(path: Path, architecture: str) -> bytes:
    try:
        content = path.read_bytes()
        root = ET.fromstring(content)
    except (OSError, ET.ParseError) as error:
        raise ResultAggregationError(f"{architecture} JUnit is invalid") from error
    if root.tag != "testsuite":
        raise ResultAggregationError(f"{architecture} JUnit root must be testsuite")
    try:
        failures = int(root.attrib.get("failures", "0"))
        errors = int(root.attrib.get("errors", "0"))
    except ValueError as error:
        raise ResultAggregationError(
            f"{architecture} JUnit failure count is invalid"
        ) from error
    if failures or errors:
        raise ResultAggregationError(f"{architecture} JUnit contains failures")
    return content


def _combine(
    reports: Mapping[str, Mapping[str, object]],
    field: str,
) -> str:
    return "; ".join(
        f"{architecture}={reports[architecture]['environment'][field]}"
        for architecture in _ARCHITECTURES
    )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def aggregate_native_results(
    *,
    workspace: Path,
    task: TaskSpec,
    run_id: str,
    run_url: str,
    report_dir: Path,
    allow_legacy_evidence: bool = False,
) -> dict[str, object]:
    """Aggregate two native reports.

    ``allow_legacy_evidence`` accepts reports produced before validations
    recorded the candidate they validated. It exists only for the one-time
    recovery of an already reviewed run, whose report bytes are pinned by
    SHA256 in the workflow, and must be removed with that recovery path.
    """
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ResultAggregationError("run_id must be a positive integer")
    if not run_url.startswith("https://"):
        raise ResultAggregationError("run_url must be an HTTPS URL")
    workspace = Path(workspace)
    report_dir = Path(report_dir)
    app_root = workspace / task.domain / task.app
    if not app_root.is_dir():
        raise ResultAggregationError("generated application directory is missing")
    result_dir = app_root / "results" / task.version / task.os_version
    if result_dir.exists():
        raise ResultAggregationError(
            f"result directory already exists and cannot be overwritten: {result_dir}"
        )

    reports = {
        architecture: _load_report(
            report_dir / f"{architecture}.json",
            architecture,
            task,
            allow_legacy_evidence=allow_legacy_evidence,
        )
        for architecture in _ARCHITECTURES
    }
    validated = {
        str(reports[architecture].get("validated_patch_sha256", ""))
        for architecture in _ARCHITECTURES
    }
    if validated != {""} and len(validated) != 1:
        # Job order alone cannot prove this: a repair on one architecture
        # silently invalidates the other architecture's earlier pass.
        raise ResultAggregationError(
            "x86_64 and aarch64 validated different candidate content: "
            + ", ".join(
                f"{architecture}="
                f"{reports[architecture].get('validated_patch_sha256', '')}"
                for architecture in _ARCHITECTURES
            )
        )
    junit = {
        architecture: _load_junit(
            report_dir / f"{architecture}.junit.xml",
            architecture,
        )
        for architecture in _ARCHITECTURES
    }
    environments = {
        architecture: reports[architecture]["environment"]
        for architecture in _ARCHITECTURES
    }
    version_info = {
        "test_time": max(
            str(environments[architecture]["test_time"])
            for architecture in _ARCHITECTURES
        ),
        "Model": _combine(reports, "Model"),
        "architecture": "x86_64,aarch64",
        "kernel": _combine(reports, "kernel"),
        "os": _combine(reports, "os"),
        "cpu_model": _combine(reports, "cpu_model"),
        "cpu_cores": sum(
            int(environments[architecture]["cpu_cores"])
            for architecture in _ARCHITECTURES
        ),
        "software_name": task.app,
        "software_version": task.version,
        "python_version": _combine(reports, "python_version"),
        "numpy_version": _combine(reports, "numpy_version"),
    }
    results = {
        "schema_version": 1,
        "status": "passed",
        "task_id": task.task_id,
        "validated_run_id": run_id,
        "artifact_url": run_url,
        "architectures": {
            architecture: {
                key: reports[architecture][key]
                for key in (
                    "platform",
                    "image_id",
                    "duration_seconds",
                    "checks",
                    "environment",
                )
            }
            for architecture in _ARCHITECTURES
        },
    }
    files = {
        "x86_64.junit.xml": junit["x86_64"],
        "aarch64.junit.xml": junit["aarch64"],
        "version_info.json": _json_bytes(version_info),
        "results.json": _json_bytes(results),
    }
    total_bytes = sum(len(content) for content in files.values())
    if total_bytes > _MAX_RESULT_BYTES:
        raise ResultAggregationError(
            f"in-repository result evidence exceeds {_MAX_RESULT_BYTES} bytes"
        )

    result_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (result_dir / name).write_bytes(content)
    return {
        "status": "passed",
        "task_id": task.task_id,
        "result_dir": result_dir.relative_to(workspace).as_posix(),
        "files": sorted(files),
        "total_bytes": total_bytes,
    }

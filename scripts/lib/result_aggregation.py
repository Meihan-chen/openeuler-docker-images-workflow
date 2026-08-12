"""Aggregate two native validation reports into bounded in-repository evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import junit_pass_rate, native_checks_pass


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
    if not _SHA256_RE.fullmatch(recorded):
        raise ResultAggregationError(
            f"{architecture} report does not record the candidate it validated"
        )
    checks = report.get("checks")
    if not native_checks_pass(
        checks,
        oe_upgrade=task.scenario == "oe-upgrade",
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
        pass_rate = junit_pass_rate(content)
    except (OSError, ValueError) as error:
        raise ResultAggregationError(f"{architecture} JUnit is invalid") from error
    if pass_rate != 1.0:
        raise ResultAggregationError(f"{architecture} JUnit contains failures")
    return content


def _combine(
    reports: Mapping[str, Mapping[str, object]],
    field: str,
    architectures: tuple[str, ...] = _ARCHITECTURES,
) -> str:
    return "; ".join(
        f"{architecture}={reports[architecture]['environment'][field]}"
        for architecture in architectures
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
    results_output: Path,
) -> dict[str, object]:
    """Aggregate native reports into target evidence and a production result."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ResultAggregationError("run_id must be a positive integer")
    if not run_url.startswith("https://"):
        raise ResultAggregationError("run_url must be an HTTPS URL")
    workspace = Path(workspace)
    report_dir = Path(report_dir)
    results_output = Path(results_output)
    app_root = workspace / (task.mdu_path or f"{task.domain}/{task.app}")
    if not app_root.is_dir():
        raise ResultAggregationError("generated application directory is missing")
    result_dir = app_root / "results" / task.version / task.os_version
    if result_dir.exists():
        raise ResultAggregationError(
            f"result directory already exists and cannot be overwritten: {result_dir}"
        )
    if results_output.exists():
        raise ResultAggregationError(
            f"production results already exist and cannot be overwritten: {results_output}"
        )

    architectures = (
        task.architectures if task.schema_version == 2 else _ARCHITECTURES
    )
    reports = {
        architecture: _load_report(
            report_dir / f"{architecture}.json",
            architecture,
            task,
        )
        for architecture in architectures
    }
    if task.schema_version == 2:
        wrong_tasks = [
            architecture
            for architecture in architectures
            if reports[architecture].get("task_key") != task.task_key
        ]
        if wrong_tasks:
            raise ResultAggregationError(
                "native reports have a mismatched task_key: "
                + ", ".join(wrong_tasks)
            )
    validated = {
        str(reports[architecture].get("validated_patch_sha256", ""))
        for architecture in architectures
    }
    if len(validated) != 1:
        # Job order alone cannot prove this: a repair on one architecture
        # silently invalidates the other architecture's earlier pass.
        raise ResultAggregationError(
            "x86_64 and aarch64 validated different candidate content: "
            + ", ".join(
                f"{architecture}="
                f"{reports[architecture].get('validated_patch_sha256', '')}"
                for architecture in architectures
            )
        )
    junit = {
        architecture: _load_junit(
            report_dir / f"{architecture}.junit.xml",
            architecture,
        )
        for architecture in architectures
    }
    environments = {
        architecture: reports[architecture]["environment"]
        for architecture in architectures
    }
    version_info = {
        "test_time": max(
            str(environments[architecture]["test_time"])
            for architecture in architectures
        ),
        "Model": _combine(reports, "Model", architectures),
        "architecture": ",".join(architectures),
        "kernel": _combine(reports, "kernel", architectures),
        "os": _combine(reports, "os", architectures),
        "cpu_model": _combine(reports, "cpu_model", architectures),
        "cpu_cores": sum(
            int(environments[architecture]["cpu_cores"])
            for architecture in architectures
        ),
        "software_name": task.app,
        "software_version": task.version,
        "python_version": _combine(reports, "python_version", architectures),
        "numpy_version": _combine(reports, "numpy_version", architectures),
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
            for architecture in architectures
        },
    }
    if task.task_key:
        results["task_key"] = task.task_key
    files = {
        **{
            f"{architecture}.junit.xml": junit[architecture]
            for architecture in architectures
        },
        "version_info.json": _json_bytes(version_info),
    }
    total_bytes = sum(len(content) for content in files.values())
    if total_bytes > _MAX_RESULT_BYTES:
        raise ResultAggregationError(
            f"in-repository result evidence exceeds {_MAX_RESULT_BYTES} bytes"
        )

    result_dir.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (result_dir / name).write_bytes(content)
    results_output.parent.mkdir(parents=True, exist_ok=True)
    results_output.write_bytes(_json_bytes(results))
    return {
        "status": "passed",
        "task_id": task.task_id,
        "result_dir": result_dir.relative_to(workspace).as_posix(),
        "files": sorted(files),
        "total_bytes": total_bytes,
        "results_file": str(results_output),
    }

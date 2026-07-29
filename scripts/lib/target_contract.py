"""Task-scoped target repository contract for generated image content."""

from __future__ import annotations

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from scripts.lib.task_spec import TaskSpec


class TargetContractError(ValueError):
    """Raised when generated target content is not safe or merge-ready."""


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_RESULT_FILES = {
    "x86_64.junit.xml",
    "aarch64.junit.xml",
    "version_info.json",
    "results.json",
}
_VERSION_INFO_FIELDS = {
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


def validate_meta_file(meta_path: str | Path) -> list[str]:
    """Validate the shared meta.yml schema against its local Dockerfiles."""
    path = Path(meta_path)
    errors = []
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        return [f"Invalid YAML: {error}"]

    if not data:
        return ["meta.yml is empty"]

    for tag, entry in data.items():
        if not isinstance(entry, dict):
            errors.append(
                f"Tag '{tag}': entry must be a dict, "
                f"got {type(entry).__name__}"
            )
            continue
        if "-oe" not in tag:
            errors.append(
                f"Tag '{tag}': must follow format '{{app-ver}}-oe{{os-ver}}'"
            )

        relative = entry.get("path", "")
        if not relative:
            errors.append(f"Tag '{tag}': missing 'path' field")
        elif not (path.parent / relative).exists():
            errors.append(f"Tag '{tag}': path '{relative}' does not exist")

        architecture = entry.get("arch")
        if architecture and architecture not in ("x86_64", "aarch64"):
            errors.append(
                f"Tag '{tag}': arch must be 'x86_64' or 'aarch64', "
                f"got '{architecture}'"
            )
    return errors


def validate_task_meta_file(
    meta_path: str | Path,
    task: object,
) -> list[str]:
    """Apply the shared schema and exact TaskSpec meta.yml contracts."""
    path = Path(meta_path)
    errors = validate_meta_file(path)
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return errors
    if not isinstance(data, dict):
        return errors

    version = str(getattr(task, "version"))
    os_version = str(getattr(task, "os_version"))
    expected_tag = (
        f"{version}-oe"
        f"{os_version.replace('.', '').replace('-lts-sp', 'sp')}"
    )
    if set(data) != {expected_tag}:
        errors.append(f"meta.yml must contain only tag {expected_tag}")
        return errors
    entry = data[expected_tag]
    expected_path = f"{version}/{os_version}/Dockerfile"
    if not isinstance(entry, dict) or entry.get("path") != expected_path:
        errors.append(f"meta.yml path must be {expected_path}")
        return errors
    if "arch" in entry:
        errors.append(
            "meta.yml must omit arch for dual-architecture publication"
        )
    return errors


def find_all_meta_files(root: str | Path) -> list[str]:
    """Find all meta.yml files below a target repository root."""
    meta_files = []
    for directory, _, filenames in os.walk(root):
        if ".git" in directory:
            continue
        if "meta.yml" in filenames:
            meta_files.append(os.path.join(directory, "meta.yml"))
    return meta_files


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if check and result.returncode != 0:
        raise TargetContractError(
            result.stderr.strip() or f"git {' '.join(args)} failed"
        )
    return result


def _load_yaml(path: Path, label: str) -> object:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise TargetContractError(f"{label} is not valid YAML") from error


def _changed_files(repo: Path, base_sha: str) -> list[tuple[str, str]]:
    _git(repo, "add", "--intent-to-add", "--", ".")
    result = _git(
        repo,
        "diff",
        "--name-status",
        "--no-renames",
        base_sha,
        "--",
    )
    changes = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise TargetContractError("git diff returned an invalid name-status line")
        changes.append((fields[0], fields[1]))
    return changes


def _required_paths(task: TaskSpec) -> tuple[str, list[str]]:
    app_root = f"{task.domain}/{task.app}"
    image_root = f"{app_root}/{task.version}/{task.os_version}"
    return app_root, [
        f"{app_root}/README.md",
        f"{app_root}/meta.yml",
        f"{app_root}/doc/image-info.yml",
        f"{app_root}/doc/picture/logo.png",
        f"{image_root}/Dockerfile",
        f"{image_root}/test.sh",
        f"{app_root}/tests/goss.yaml",
        f"{app_root}/tests/goss_wait.yaml",
        f"{app_root}/tests/test_helpers.sh",
        f"{app_root}/tests/test.sh",
    ]


def _validate_image_list(
    repo: Path,
    *,
    task: TaskSpec,
    base_sha: str,
    errors: list[str],
) -> str:
    relative = f"{task.domain}/image-list.yml"
    path = repo / relative
    try:
        before_text = _git(repo, "show", f"{base_sha}:{relative}").stdout
        before = yaml.safe_load(before_text)
        after = _load_yaml(path, relative)
    except (TargetContractError, yaml.YAMLError) as error:
        errors.append(f"image-list validation failed: {error}")
        return relative
    if not isinstance(before, dict) or not isinstance(before.get("images"), dict):
        errors.append("base image-list.yml has no images mapping")
        return relative
    if not isinstance(after, dict) or not isinstance(after.get("images"), dict):
        errors.append("image-list.yml has no images mapping")
        return relative
    expected = dict(before["images"])
    expected[task.app] = task.app
    if after["images"] != expected or set(after) != set(before):
        errors.append(
            "image-list.yml must preserve every existing entry and add only "
            f"{task.app}: {task.app}"
        )
    return relative


def _validate_meta(repo: Path, task: TaskSpec, app_root: str, errors: list[str]) -> None:
    errors.extend(
        validate_task_meta_file(repo / app_root / "meta.yml", task)
    )


def _validate_dockerfile(
    repo: Path,
    task: TaskSpec,
    app_root: str,
    errors: list[str],
) -> None:
    path = repo / app_root / task.version / task.os_version / "Dockerfile"
    text = path.read_text()
    required_fragments = {
        f"ARG BASE=openeuler/openeuler:{task.os_version}": "locked openEuler base",
        "FROM ${BASE} AS builder": "multi-stage builder",
        f"ARG VERSION={task.version}": "locked application version",
        "./x.py build": "official Kvrocks build command",
        "-j 4": "bounded Kvrocks build parallelism (-j 4)",
        "FROM ${BASE}": "openEuler runtime stage",
        "USER 999": "non-root runtime user",
        "EXPOSE 6666": "Kvrocks port",
        "HEALTHCHECK": "runtime health check",
        "redis-cli -p 6666 PING": "Redis protocol health probe",
        "ENTRYPOINT": "Kvrocks entrypoint",
    }
    for fragment, label in required_fragments.items():
        if fragment not in text:
            errors.append(f"Dockerfile is missing {label}: {fragment}")
    if not any(
        fragment in text
        for fragment in (
            '--branch "v${VERSION}"',
            "--branch v${VERSION}",
            "refs/tags/v${VERSION}.tar.gz",
        )
    ):
        errors.append(
            "Dockerfile must lock the upstream source to v${VERSION}"
        )
    if "latest" in text.lower():
        errors.append("Dockerfile must not use an unpinned latest source or image")
    if "$(nproc)" in text or "-j$(nproc)" in text:
        errors.append("Dockerfile must use -j 4, not unbounded nproc parallelism")
    if text.count("FROM ") < 2:
        errors.append("Dockerfile must use separate builder and runtime stages")


def _validate_tests(
    repo: Path,
    task: TaskSpec,
    app_root: str,
    errors: list[str],
) -> None:
    shared = repo / app_root / "tests"
    entry = repo / app_root / task.version / task.os_version / "test.sh"
    entry_text = entry.read_text()
    shared_text = (shared / "test.sh").read_text()
    goss_text = (shared / "goss.yaml").read_text()
    version_assignment = re.compile(
        rf"EXPECTED_VERSION=(?:\"{re.escape(task.version)}\"|"
        rf"'{re.escape(task.version)}'|{re.escape(task.version)})"
    )
    if not version_assignment.search(entry_text):
        errors.append("Dockerfile-level test.sh must inject the expected version")
    if not any(
        fragment in entry_text
        for fragment in ("../../tests/test.sh", "$SHARED_DIR/test.sh")
    ):
        errors.append("Dockerfile-level test.sh must call the app-level shared tests")
    if task.version in shared_text:
        errors.append("app-level shared tests must not hardcode one application version")
    for fragment in ("${EXPECTED_VERSION", "kvrocks --version", "redis-cli", "id -u"):
        if fragment not in shared_text:
            errors.append(f"shared test.sh is missing assertion: {fragment}")
    for fragment in ("tcp:6666", ".Env.EXPECTED_VERSION", "PING", "PONG"):
        if fragment not in goss_text:
            errors.append(f"goss.yaml is missing assertion: {fragment}")
    for script in (entry, shared / "test.sh", shared / "test_helpers.sh"):
        if not script.stat().st_mode & 0o111:
            errors.append(f"{script.relative_to(repo)} must be executable")


def _validate_docs(
    repo: Path,
    task: TaskSpec,
    app_root: str,
    errors: list[str],
) -> None:
    readme = (repo / app_root / "README.md").read_text()
    for heading in (
        "# Quick reference",
        "# Supported tags and respective Dockerfile links",
        "# Usage",
        "# Question and answering",
    ):
        if heading not in readme:
            errors.append(f"README.md is missing section: {heading}")
    title = re.compile(
        rf"^# .*{re.escape(task.app)}.* \| openEuler\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    if not title.search(readme):
        errors.append(
            f"README.md is missing the {task.app} openEuler title"
        )
    if f"{task.version}-oe2403sp4" not in readme:
        errors.append("README.md is missing the generated image tag")

    info = _load_yaml(repo / app_root / "doc" / "image-info.yml", "image-info.yml")
    required_keys = {
        "name",
        "category",
        "description",
        "environment",
        "tags",
        "download",
        "usage",
        "license",
        "similar_packages",
        "dependency",
        "homepage",
        "upstream",
    }
    if not isinstance(info, dict) or not required_keys.issubset(info):
        errors.append("image-info.yml is missing required metadata fields")
    else:
        if info["name"] != task.app or info["category"] != task.domain.lower():
            errors.append("image-info.yml name/category do not match TaskSpec")
        if not isinstance(info["similar_packages"], list) or len(
            info["similar_packages"]
        ) < 3:
            errors.append("image-info.yml must list at least three similar packages")

    logo = (repo / app_root / "doc" / "picture" / "logo.png").read_bytes()
    if not logo.startswith(b"\x89PNG\r\n\x1a\n"):
        errors.append("doc/picture/logo.png must be a non-empty PNG")


def validate_generated_target(
    *,
    repo: Path,
    task: TaskSpec,
    base_sha: str,
) -> dict[str, object]:
    repo = Path(repo)
    if not _SHA_RE.fullmatch(base_sha):
        raise TargetContractError("base_sha must be a full lowercase Git SHA")
    if not (repo / ".git").is_dir():
        raise TargetContractError("target repository is not a Git workspace")

    app_root, required = _required_paths(task)
    if _git(
        repo,
        "cat-file",
        "-e",
        f"{base_sha}:{app_root}",
        check=False,
    ).returncode == 0:
        raise TargetContractError(f"{app_root} already exists at the target base")

    errors: list[str] = []
    changes = _changed_files(repo, base_sha)
    image_list = _validate_image_list(
        repo,
        task=task,
        base_sha=base_sha,
        errors=errors,
    )
    modified_files = []
    added_files = []
    for status, relative in changes:
        if status == "A" and relative.startswith(f"{app_root}/"):
            added_files.append(relative)
        elif status == "M" and relative == image_list:
            modified_files.append(relative)
        else:
            errors.append(
                f"change outside task scope or wrong status: {status} {relative}"
            )
    if modified_files != [image_list]:
        errors.append(f"{image_list} must be the only modified existing file")

    for relative in required:
        path = repo / relative
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"required generated file is missing or empty: {relative}")
    for path in (repo / app_root).rglob("*"):
        if path.is_symlink():
            errors.append(f"generated symlink is forbidden: {path.relative_to(repo)}")

    if not any(error.startswith("required generated file") for error in errors):
        _validate_meta(repo, task, app_root, errors)
        _validate_dockerfile(repo, task, app_root, errors)
        _validate_tests(repo, task, app_root, errors)
        _validate_docs(repo, task, app_root, errors)

    if errors:
        raise TargetContractError("\n".join(errors))
    return {
        "status": "passed",
        "task_id": task.task_id,
        "base_sha": base_sha,
        "added_files": len(added_files),
        "modified_files": modified_files,
        "details": json.dumps(
            {"app_root": app_root, "required_files": required},
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def validate_final_target(
    *,
    repo: Path,
    task: TaskSpec,
    base_sha: str,
    expected_run_id: str,
) -> dict[str, object]:
    if not _RUN_ID_RE.fullmatch(expected_run_id):
        raise TargetContractError("expected_run_id must be a positive integer")
    generated = validate_generated_target(
        repo=repo,
        task=task,
        base_sha=base_sha,
    )
    result_dir = (
        Path(repo)
        / task.domain
        / task.app
        / "results"
        / task.version
        / task.os_version
    )
    if not result_dir.is_dir():
        raise TargetContractError("final app result directory is missing")
    actual_files = {
        path.name for path in result_dir.iterdir() if path.is_file()
    }
    if actual_files != _RESULT_FILES or any(
        not path.is_file() for path in result_dir.iterdir()
    ):
        missing = sorted(_RESULT_FILES - actual_files)
        extra = sorted(actual_files - _RESULT_FILES)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise TargetContractError(
            "final result files are incomplete: " + "; ".join(detail)
        )

    total_bytes = sum((result_dir / name).stat().st_size for name in _RESULT_FILES)
    if total_bytes > 20 * 1024:
        raise TargetContractError("final in-repository results exceed 20 KiB")
    for architecture in ("x86_64", "aarch64"):
        path = result_dir / f"{architecture}.junit.xml"
        try:
            suite = ET.parse(path).getroot()
            failures = int(suite.attrib.get("failures", "0"))
            errors = int(suite.attrib.get("errors", "0"))
        except (ET.ParseError, ValueError) as error:
            raise TargetContractError(
                f"{architecture} JUnit is invalid"
            ) from error
        if suite.tag != "testsuite" or failures or errors:
            raise TargetContractError(
                f"{architecture} JUnit does not prove a passed validation"
            )

    try:
        version_info = json.loads((result_dir / "version_info.json").read_text())
        results = json.loads((result_dir / "results.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise TargetContractError("final JSON result evidence is invalid") from error
    if (
        not isinstance(version_info, dict)
        or set(version_info) != _VERSION_INFO_FIELDS
        or version_info.get("architecture") != "x86_64,aarch64"
        or version_info.get("software_name") != task.app
        or version_info.get("software_version") != task.version
    ):
        raise TargetContractError(
            "version_info.json must contain the 11-field dual-architecture evidence"
        )
    if (
        not isinstance(results, dict)
        or results.get("status") != "passed"
        or results.get("task_id") != task.task_id
        or results.get("validated_run_id") != expected_run_id
        or not str(results.get("artifact_url", "")).startswith("https://")
    ):
        raise TargetContractError("results.json does not match the validated run")
    architectures = results.get("architectures")
    if not isinstance(architectures, dict) or set(architectures) != {
        "x86_64",
        "aarch64",
    }:
        raise TargetContractError(
            "results.json must contain x86_64 and aarch64 evidence"
        )
    for architecture, evidence in architectures.items():
        checks = evidence.get("checks") if isinstance(evidence, dict) else None
        if not isinstance(checks, dict) or not checks or not all(
            value is True for value in checks.values()
        ):
            raise TargetContractError(
                f"results.json {architecture} checks did not all pass"
            )

    return {
        **generated,
        "validated_run_id": expected_run_id,
        "result_dir": result_dir.relative_to(repo).as_posix(),
        "result_bytes": total_bytes,
    }

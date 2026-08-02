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

    def __init__(
        self,
        message: str,
        *,
        findings: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.findings = list(findings or ())


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_RESULT_FILES = {
    "x86_64.junit.xml",
    "aarch64.junit.xml",
    "version_info.json",
    "results.json",
}
_OPENEULER_GITEE_URL_RE = re.compile(
    r"https?://(?:www\.)?gitee\.com/(?:openeuler|src-openeuler)"
    r"(?=/|[\s?#)]|$)",
    re.IGNORECASE,
)
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


def _finding(
    code: str,
    *,
    owner: str,
    level: str,
    message: str,
) -> dict[str, str]:
    if code.startswith("tests."):
        source = "scenario_one_test_protocol"
    elif code.startswith("links."):
        source = "confirmed_migration_policy"
    elif code.startswith(("scope.", "required.")):
        source = "task_boundary"
    else:
        source = "target_repository_contract"
    return {
        "code": code,
        "level": level,
        "owner": owner,
        "source": source,
        "message": message,
    }


def _hard_finding(code: str, message: str) -> dict[str, str]:
    return _finding(
        code,
        owner="workflow",
        level="hard_stop",
        message=message,
    )


def _delivery_finding(
    code: str,
    message: str,
    *,
    owner: str,
) -> dict[str, str]:
    return _finding(
        code,
        owner=owner,
        level="delivery_stop",
        message=message,
    )


def _advisory_finding(
    code: str,
    message: str,
    *,
    owner: str,
) -> dict[str, str]:
    return _finding(
        code,
        owner=owner,
        level="advisory",
        message=message,
    )


def _image_tag(version: str, os_version: str) -> str:
    suffix = os_version.replace("-lts-sp", "sp").replace("-lts", "lts")
    return f"{version}-oe{suffix.replace('.', '').replace('-', '')}"


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
    expected_tag = _image_tag(version, os_version)
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


def _required_paths(
    task: TaskSpec,
) -> tuple[str, list[str]]:
    app_root = f"{task.domain}/{task.app}"
    image_root = f"{app_root}/{task.version}/{task.os_version}"
    image_paths = [
        f"{app_root}/README.md",
        f"{app_root}/meta.yml",
        f"{image_root}/Dockerfile",
    ]
    return app_root, image_paths


def _validate_image_list(
    repo: Path,
    *,
    task: TaskSpec,
    base_sha: str,
    hard_findings: list[dict[str, str]],
    findings: list[dict[str, str]],
) -> str:
    relative = f"{task.domain}/image-list.yml"
    path = repo / relative
    try:
        before_text = _git(repo, "show", f"{base_sha}:{relative}").stdout
        before = yaml.safe_load(before_text)
        after = _load_yaml(path, relative)
    except (TargetContractError, yaml.YAMLError) as error:
        hard_findings.append(
            _hard_finding(
                "image_list.invalid_yaml",
                f"image-list validation failed: {error}",
            )
        )
        return relative
    if not isinstance(before, dict) or not isinstance(before.get("images"), dict):
        hard_findings.append(
            _hard_finding(
                "image_list.invalid_base",
                "base image-list.yml has no images mapping",
            )
        )
        return relative
    if not isinstance(after, dict) or not isinstance(after.get("images"), dict):
        hard_findings.append(
            _hard_finding(
                "image_list.invalid_generated",
                "image-list.yml has no images mapping",
            )
        )
        return relative
    if set(after) != set(before) or any(
        after["images"].get(name) != value
        for name, value in before["images"].items()
    ):
        hard_findings.append(
            _hard_finding(
                "image_list.existing_entries_changed",
                "image-list.yml must preserve every existing entry",
            )
        )
        return relative
    added_names = set(after["images"]) - set(before["images"])
    if added_names - {task.app}:
        hard_findings.append(
            _hard_finding(
                "image_list.out_of_scope_entry",
                "image-list.yml adds entries outside the requested application",
            )
        )
    if added_names != {task.app} or after["images"].get(task.app) != task.app:
        findings.append(
            _delivery_finding(
                "image_list.registration",
                f"image-list.yml must add {task.app}: {task.app}",
                owner="image_creator",
            )
        )
    return relative


def _validate_meta(
    repo: Path,
    task: TaskSpec,
    app_root: str,
    findings: list[dict[str, str]],
) -> None:
    for message in validate_task_meta_file(repo / app_root / "meta.yml", task):
        if "omit arch" in message:
            code = "meta.dual_arch"
        elif "path must be" in message or "does not exist" in message:
            code = "meta.path"
        elif "only tag" in message or "format" in message:
            code = "meta.tag"
        else:
            code = "meta.schema"
        findings.append(
            _delivery_finding(code, message, owner="image_creator")
        )


def _validate_tests(
    repo: Path,
    app_root: str,
    findings: list[dict[str, str]],
) -> None:
    shared = repo / app_root / "tests"
    goss_path = shared / "goss.yaml"
    if goss_path.is_file() and goss_path.stat().st_size:
        try:
            goss_data = yaml.safe_load(goss_path.read_text())
        except yaml.YAMLError:
            goss_data = None
            findings.append(
                _delivery_finding(
                    "tests.goss_yaml",
                    "goss.yaml must be valid YAML",
                    owner="testcase_creator",
                )
            )
        if isinstance(goss_data, dict):
            commands = goss_data.get("command", {})
            if isinstance(commands, dict):
                for name, assertion in commands.items():
                    if not isinstance(assertion, dict) or "stdout" not in assertion:
                        continue
                    if not isinstance(assertion["stdout"], (str, list)):
                        findings.append(
                            _delivery_finding(
                                "tests.goss_stdout_schema",
                                f"goss command {name} stdout must be a string or YAML list",
                                owner="testcase_creator",
                            )
                        )
        elif goss_data is not None:
            findings.append(
                _delivery_finding(
                    "tests.goss_schema",
                    "goss.yaml must contain a YAML mapping",
                    owner="testcase_creator",
                )
            )

    goss_wait_path = shared / "goss_wait.yaml"
    if goss_wait_path.is_file() and goss_wait_path.stat().st_size:
        try:
            goss_wait_data = yaml.safe_load(goss_wait_path.read_text())
        except yaml.YAMLError:
            goss_wait_data = None
            findings.append(
                _delivery_finding(
                    "tests.wait_yaml",
                    "goss_wait.yaml must be valid YAML",
                    owner="testcase_creator",
                )
            )
        if isinstance(goss_wait_data, dict):
            port = goss_wait_data.get("port", {})
            if port and not isinstance(port, dict):
                findings.append(
                    _delivery_finding(
                        "tests.wait_port_schema",
                        "goss_wait.yaml port resource must be a YAML mapping",
                        owner="testcase_creator",
                    )
                )
            elif isinstance(port, dict):
                for name, assertion in port.items():
                    if not isinstance(assertion, dict):
                        findings.append(
                            _delivery_finding(
                                "tests.wait_port_schema",
                                f"goss_wait.yaml port {name} must be a YAML mapping",
                                owner="testcase_creator",
                            )
                        )
                    elif "timeout" in assertion:
                        findings.append(
                            _delivery_finding(
                                "tests.wait_timeout",
                                f"goss_wait.yaml port {name} does not support timeout; the native harness controls the readiness retry",
                                owner="testcase_creator",
                            )
                        )
        elif goss_wait_data is not None:
            findings.append(
                _delivery_finding(
                    "tests.wait_schema",
                    "goss_wait.yaml must contain a YAML mapping",
                    owner="testcase_creator",
                )
            )
    shared_entry = shared / "test.sh"
    if shared_entry.is_file() and not shared_entry.stat().st_mode & 0o111:
        findings.append(
            _delivery_finding(
                "tests.entrypoint_executable",
                f"{shared_entry.relative_to(repo)} must be executable",
                owner="testcase_creator",
            )
        )


def validate_test_contract(
    *,
    repo: Path,
    task: TaskSpec,
) -> dict[str, object]:
    """Validate test assets without requiring the full delivery contract."""
    repo = Path(repo)
    app_root = f"{task.domain}/{task.app}"
    findings: list[dict[str, str]] = []
    for relative in (
        f"{app_root}/tests/goss.yaml",
        f"{app_root}/tests/test.sh",
    ):
        path = repo / relative
        if not path.is_file() or path.stat().st_size == 0:
            findings.append(
                _delivery_finding(
                    "tests.required",
                    f"required generated file is missing or empty: {relative}",
                    owner="testcase_creator",
                )
            )
    _validate_tests(repo, app_root, findings)
    blocking = [
        finding
        for finding in findings
        if finding["level"] == "delivery_stop"
    ]
    return {
        "status": "passed" if not blocking else "failed",
        "test_allowed": not blocking,
        "findings": findings,
        "errors": [finding["message"] for finding in blocking],
    }


def _validate_link_hosts(
    repo: Path,
    app_root: str,
    findings: list[dict[str, str]],
) -> None:
    """Report only old Gitee URLs owned by openEuler, not the whole host."""
    for path in sorted((repo / app_root).rglob("*")):
        if not path.is_file():
            continue
        if path.relative_to(repo / app_root).parts[0] == "results":
            continue
        content = path.read_bytes().decode("utf-8", errors="ignore")
        if _OPENEULER_GITEE_URL_RE.search(content):
            findings.append(
                _delivery_finding(
                    "links.openeuler_gitee",
                    f"{path.relative_to(repo).as_posix()} links to an "
                    "openEuler repository on gitee.com; use gitcode.com",
                    owner="image_creator",
                )
            )


def _validate_docs(
    repo: Path,
    task: TaskSpec,
    app_root: str,
    findings: list[dict[str, str]],
) -> None:
    readme_path = repo / app_root / "README.md"
    if readme_path.is_file() and readme_path.stat().st_size:
        readme = readme_path.read_text()
        for heading in (
            "# Quick reference",
            "# Supported tags and respective Dockerfile links",
            "# Usage",
            "# Question and answering",
        ):
            if heading not in readme:
                findings.append(
                    _delivery_finding(
                        "readme.section",
                        f"README.md is missing section: {heading}",
                        owner="image_creator",
                    )
                )
        title = re.compile(
            rf"^# .*{re.escape(task.app)}.* \| openEuler\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        if not title.search(readme):
            findings.append(
                _delivery_finding(
                    "readme.title",
                    f"README.md is missing the {task.app} openEuler title",
                    owner="image_creator",
                )
            )
        if _image_tag(task.version, task.os_version) not in readme:
            findings.append(
                _delivery_finding(
                    "readme.tag",
                    "README.md is missing the generated image tag",
                    owner="image_creator",
                )
            )

    doc_root = repo / app_root / "doc"
    doc_files = (
        [path for path in doc_root.rglob("*") if path.is_file()]
        if doc_root.is_dir()
        else []
    )
    if not doc_files:
        return
    info_path = doc_root / "image-info.yml"
    if not info_path.is_file() or not info_path.stat().st_size:
        findings.append(
            _delivery_finding(
                "doc.incomplete",
                "generated doc is missing doc/image-info.yml",
                owner="image_creator",
            )
        )
        return
    try:
        info = yaml.safe_load(info_path.read_text())
    except yaml.YAMLError:
        findings.append(
            _delivery_finding(
                "doc.invalid_yaml",
                "doc/image-info.yml must be valid YAML",
                owner="image_creator",
            )
        )
        return
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
    }
    if not isinstance(info, dict) or not required_keys.issubset(info):
        findings.append(
            _delivery_finding(
                "doc.required_metadata",
                "image-info.yml is missing required metadata fields",
                owner="image_creator",
            )
        )
    else:
        if info["name"] != task.app or info["category"] != task.domain.lower():
            findings.append(
                _delivery_finding(
                    "doc.task_identity",
                    "image-info.yml name/category do not match TaskSpec",
                    owner="image_creator",
                )
            )
        if not isinstance(info["similar_packages"], list) or len(
            info["similar_packages"]
        ) < 3:
            findings.append(
                _advisory_finding(
                    "doc.similar_packages",
                    "image-info.yml should list at least three similar packages",
                    owner="image_creator",
                )
            )
        if "homepage" not in info or "upstream" not in info:
            findings.append(
                _advisory_finding(
                    "doc.upstream_metadata",
                    "image-info.yml should include homepage and upstream metadata",
                    owner="image_creator",
                )
            )

    picture_root = doc_root / "picture"
    pictures = (
        [path for path in picture_root.rglob("*") if path.is_file()]
        if picture_root.is_dir()
        else []
    )
    if not pictures:
        findings.append(
            _delivery_finding(
                "doc.picture_required",
                "generated doc must include at least one doc/picture asset",
                owner="image_creator",
            )
        )
        return
    for picture in pictures:
        content = picture.read_bytes()
        if not content:
            findings.append(
                _delivery_finding(
                    "doc.picture_empty",
                    f"{picture.relative_to(repo).as_posix()} must not be empty",
                    owner="image_creator",
                )
            )
        elif picture.suffix.lower() == ".png" and not content.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            findings.append(
                _delivery_finding(
                    "doc.picture_format",
                    f"{picture.relative_to(repo).as_posix()} must be a valid PNG",
                    owner="image_creator",
                )
            )


def validate_generated_target(
    *,
    repo: Path,
    task: TaskSpec,
    base_sha: str,
    phase: str = "full",
) -> dict[str, object]:
    repo = Path(repo)
    if phase not in {"image", "full"}:
        raise TargetContractError("generated target phase must be image or full")
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

    hard_findings: list[dict[str, str]] = []
    findings: list[dict[str, str]] = []
    changes = _changed_files(repo, base_sha)
    image_list = _validate_image_list(
        repo,
        task=task,
        base_sha=base_sha,
        hard_findings=hard_findings,
        findings=findings,
    )
    modified_files = []
    added_files = []
    for status, relative in changes:
        if status == "A" and relative.startswith(f"{app_root}/"):
            added_files.append(relative)
        elif status == "M" and relative == image_list:
            modified_files.append(relative)
        else:
            hard_findings.append(
                _hard_finding(
                    "scope.outside_task",
                    f"change outside task scope or wrong status: {status} {relative}",
                )
            )
    if modified_files != [image_list]:
        findings.append(
            _delivery_finding(
                "image_list.not_modified",
                f"{image_list} must be the only modified existing file",
                owner="image_creator",
            )
        )

    for relative in required:
        path = repo / relative
        if not path.is_file() or path.stat().st_size == 0:
            message = f"required generated file is missing or empty: {relative}"
            if relative.endswith(("/meta.yml", "/Dockerfile")):
                hard_findings.append(
                    _hard_finding("required.build_input", message)
                )
            else:
                owner = (
                    "testcase_creator"
                    if "/tests/" in relative
                    else "image_creator"
                )
                findings.append(
                    _delivery_finding(
                        "tests.required"
                        if owner == "testcase_creator"
                        else "readme.required",
                        message,
                        owner=owner,
                    )
                )
    for path in (repo / app_root).rglob("*"):
        if path.is_symlink():
            hard_findings.append(
                _hard_finding(
                    "scope.symlink",
                    f"generated symlink is forbidden: {path.relative_to(repo)}",
                )
            )

    meta_path = repo / app_root / "meta.yml"
    if meta_path.is_file() and meta_path.stat().st_size:
        try:
            meta = yaml.safe_load(meta_path.read_text())
        except (OSError, yaml.YAMLError):
            hard_findings.append(
                _hard_finding(
                    "meta.invalid_yaml",
                    "meta.yml is not valid YAML",
                )
            )
        else:
            if not isinstance(meta, dict) or not meta:
                hard_findings.append(
                    _hard_finding(
                        "meta.invalid_schema",
                        "meta.yml must contain a non-empty YAML mapping",
                    )
                )
            else:
                _validate_meta(repo, task, app_root, findings)

    _validate_link_hosts(repo, app_root, findings)
    if _OPENEULER_GITEE_URL_RE.search(task.source_url):
        findings.append(
            _delivery_finding(
                "links.openeuler_gitee",
                "TaskSpec source_url points to an openEuler repository on "
                "gitee.com; use gitcode.com",
                owner="image_creator",
            )
        )
    _validate_docs(repo, task, app_root, findings)
    if phase == "full":
        findings.extend(
            validate_test_contract(repo=repo, task=task)["findings"]
        )

    if hard_findings:
        raise TargetContractError(
            "\n".join(finding["message"] for finding in hard_findings),
            findings=hard_findings,
        )
    delivery_findings = [
        finding
        for finding in findings
        if finding["level"] == "delivery_stop"
    ]
    test_allowed = not any(
        finding["owner"] == "testcase_creator"
        for finding in delivery_findings
    )
    return {
        "status": "passed",
        "build_allowed": True,
        "delivery_allowed": not delivery_findings,
        "test_allowed": test_allowed,
        "findings": findings,
        "errors": [finding["message"] for finding in delivery_findings],
        "task_id": task.task_id,
        "base_sha": base_sha,
        "phase": phase,
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
    if not generated.get("delivery_allowed", True):
        messages = generated.get("errors", [])
        raise TargetContractError(
            "final delivery contract did not pass: "
            + "; ".join(str(message) for message in messages)
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

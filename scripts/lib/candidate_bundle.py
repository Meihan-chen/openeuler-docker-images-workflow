"""Integrity contract between validate-only and PR delivery runs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.lib.git_workspace import TargetWorkspace
from scripts.lib.task_spec import TaskSpec, TaskSpecError
from scripts.lib.target_contract import (
    junit_pass_rate as _strict_junit_pass_rate,
    native_checks_pass,
)


class CandidateBundleError(ValueError):
    """Raised when a candidate cannot be trusted for promotion."""


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_COMMON_REQUIRED_PAYLOAD = (
    "changes.patch",
    "reports/gates.json",
    "reports/hadolint.txt",
    "reports/results.json",
    "task-spec.json",
)
_COMMON_REQUIRED_REPORTS = {
    "gates": "reports/gates.json",
    "generation gates": "reports/generation-gates.json",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_digest(files: dict[str, str]) -> str:
    encoded = json.dumps(
        files, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _payload_files(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise CandidateBundleError("candidate root must be a directory")

    payload: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CandidateBundleError(
                f"symlink payload is forbidden: {path.relative_to(root)}"
            )
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        payload[relative] = path
    return payload


def _validate_sha(value: str, field: str) -> None:
    if not _SHA_RE.fullmatch(value):
        raise CandidateBundleError(f"{field} must be a full lowercase Git SHA")


def junit_pass_rate(path: Path, architecture: str) -> float:
    try:
        return _strict_junit_pass_rate(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CandidateBundleError(
            f"{architecture} JUnit evidence is invalid"
        ) from exc


def _architectures(task: TaskSpec) -> tuple[str, ...]:
    return task.architectures if task.schema_version == 2 else (
        "x86_64",
        "aarch64",
    )


def _validate_reports(root: Path, *, task: TaskSpec) -> None:
    reports = {
        **_COMMON_REQUIRED_REPORTS,
        **{
            architecture: f"reports/{architecture}.json"
            for architecture in _architectures(task)
        },
    }
    for name, relative in reports.items():
        path = root / relative
        if not path.is_file():
            raise CandidateBundleError(f"{name} validation report is required")
        try:
            report = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise CandidateBundleError(f"{name} validation report is invalid") from exc
        if not isinstance(report, dict) or report.get("status") != "passed":
            raise CandidateBundleError(f"{name} validation did not pass")
        if (
            name in {"gates", "generation gates"}
            and report.get("delivery_allowed") is not True
        ):
            raise CandidateBundleError(f"{name} delivery contract did not pass")
        if name in {"x86_64", "aarch64"} and not native_checks_pass(
            report.get("checks"),
            oe_upgrade=task.scenario == "oe-upgrade",
        ):
            raise CandidateBundleError(f"{name} validation checks are incomplete")
        if name in {"x86_64", "aarch64"} and junit_pass_rate(
            root / "reports" / f"{name}.junit.xml",
            name,
        ) != 1.0:
            raise CandidateBundleError(f"{name} JUnit evidence did not pass")
        if (
            task.schema_version == 2
            and name in _architectures(task)
            and report.get("task_key") != task.task_key
        ):
            raise CandidateBundleError(f"{name} validation belongs to another task")


def _validate_results(root: Path, *, task: TaskSpec, run_id: str) -> None:
    path = root / "reports" / "results.json"
    try:
        results = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise CandidateBundleError("results.json is invalid") from exc
    if not isinstance(results, dict) or results.get("schema_version") != 1:
        raise CandidateBundleError("results.json schema version is unsupported")
    if results.get("status") != "passed":
        raise CandidateBundleError("results.json does not record a passed result")
    if results.get("task_id") != task.task_id:
        raise CandidateBundleError("results.json belongs to another task")
    if results.get("validated_run_id") != run_id:
        raise CandidateBundleError("results.json run ID does not match candidate")
    if not str(results.get("artifact_url", "")).startswith("https://"):
        raise CandidateBundleError("results.json artifact URL must use HTTPS")
    architectures = results.get("architectures")
    required_architectures = set(_architectures(task))
    if not isinstance(architectures, dict) or set(architectures) != required_architectures:
        raise CandidateBundleError(
            "results.json must contain exactly the declared architecture evidence"
        )
    if task.schema_version == 2 and results.get("task_key") != task.task_key:
        raise CandidateBundleError("results.json task key does not match candidate")
    for architecture, evidence in architectures.items():
        checks = evidence.get("checks") if isinstance(evidence, dict) else None
        if not native_checks_pass(
            checks,
            oe_upgrade=task.scenario == "oe-upgrade",
        ):
            raise CandidateBundleError(
                f"results.json {architecture} checks are incomplete"
            )


@dataclass(frozen=True)
class CandidateManifest:
    schema_version: int
    validated_run_id: str
    task_id: str
    base_sha: str
    files: dict[str, str]
    content_sha256: str
    task_key: str | None = None
    architectures: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateBundle:
    root: Path
    task: TaskSpec
    manifest: CandidateManifest

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        task: TaskSpec,
        base_sha: str,
        validated_run_id: str,
    ) -> "CandidateBundle":
        root = Path(root)
        _validate_sha(base_sha, "base_sha")
        if not _RUN_ID_RE.fullmatch(validated_run_id):
            raise CandidateBundleError("validated_run_id must be a positive integer")

        (root / "task-spec.json").write_text(task.to_json() + "\n")
        _validate_reports(root, task=task)
        _validate_results(
            root,
            task=task,
            run_id=validated_run_id,
        )

        payload = _payload_files(root)
        required_payload = set(_COMMON_REQUIRED_PAYLOAD)
        for architecture in _architectures(task):
            required_payload.update(
                {
                    f"reports/{architecture}.json",
                    f"reports/{architecture}.junit.xml",
                }
            )
        missing = sorted(required_payload - set(payload))
        if missing:
            raise CandidateBundleError(
                "candidate payload is incomplete: " + ", ".join(missing)
            )
        if payload["changes.patch"].stat().st_size == 0:
            raise CandidateBundleError("changes.patch must not be empty")

        files = {relative: _sha256(path) for relative, path in payload.items()}
        manifest = CandidateManifest(
            schema_version=1,
            validated_run_id=validated_run_id,
            task_id=task.task_id,
            base_sha=base_sha,
            files=files,
            content_sha256=_content_digest(files),
            task_key=task.task_key,
            architectures=_architectures(task),
        )
        (root / "manifest.json").write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        return cls(root=root, task=task, manifest=manifest)

    @classmethod
    def verify(
        cls, root: Path, *, expected_run_id: str | None = None
    ) -> "CandidateBundle":
        root = Path(root)
        manifest_path = root / "manifest.json"
        try:
            raw_manifest = json.loads(manifest_path.read_text())
            if isinstance(raw_manifest, dict) and isinstance(
                raw_manifest.get("architectures"), list
            ):
                raw_manifest["architectures"] = tuple(
                    raw_manifest["architectures"]
                )
            manifest = CandidateManifest(**raw_manifest)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise CandidateBundleError("candidate manifest is invalid") from exc

        if manifest.schema_version != 1:
            raise CandidateBundleError("candidate schema version is unsupported")
        _validate_sha(manifest.base_sha, "base_sha")
        if expected_run_id is not None and manifest.validated_run_id != expected_run_id:
            raise CandidateBundleError("validated run ID does not match candidate")

        payload = _payload_files(root)
        if set(payload) != set(manifest.files):
            raise CandidateBundleError("candidate file set does not match manifest")

        for relative, path in payload.items():
            if _sha256(path) != manifest.files[relative]:
                raise CandidateBundleError(f"checksum mismatch: {relative}")
        if _content_digest(manifest.files) != manifest.content_sha256:
            raise CandidateBundleError("candidate content checksum is invalid")

        try:
            task = TaskSpec.from_json((root / "task-spec.json").read_text())
        except (OSError, json.JSONDecodeError, TaskSpecError) as exc:
            raise CandidateBundleError("candidate TaskSpec is invalid") from exc
        if task.task_id != manifest.task_id:
            raise CandidateBundleError("candidate TaskSpec does not match manifest")
        if task.schema_version == 2 and (
            manifest.task_key != task.task_key
            or manifest.architectures != task.architectures
        ):
            raise CandidateBundleError("candidate TaskSpec does not match manifest")
        _validate_reports(root, task=task)
        _validate_results(
            root,
            task=task,
            run_id=manifest.validated_run_id,
        )

        return cls(root=root, task=task, manifest=manifest)


@dataclass(frozen=True)
class CandidatePromotion:
    branch: str
    base_sha: str
    commit_sha: str
    candidate_sha256: str
    validated_run_id: str


def promote_candidate(
    *,
    candidate_dir: Path,
    expected_run_id: str,
    workspace: TargetWorkspace,
    branch: str | None = None,
) -> CandidatePromotion:
    bundle = CandidateBundle.verify(
        candidate_dir,
        expected_run_id=expected_run_id,
    )

    workspace.apply_patch(bundle.root / "changes.patch")
    delivery_branch = branch or bundle.task.branch
    commit_sha = workspace.commit_candidate(
        branch=delivery_branch,
        message=(
            f"feat: add {bundle.task.app} {bundle.task.version} image "
            f"for openEuler {bundle.task.os_version}"
        ),
    )
    return CandidatePromotion(
        branch=delivery_branch,
        base_sha=bundle.manifest.base_sha,
        commit_sha=commit_sha,
        candidate_sha256=bundle.manifest.content_sha256,
        validated_run_id=bundle.manifest.validated_run_id,
    )

"""Checkpoint and sanitize Agent changes for openEuler upgrade candidates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping

from scripts.lib.oe_upgrade_candidate import extract_source_identity
from scripts.lib.task_spec import TaskSpec
from scripts.lib.target_contract import (
    is_agent_control_path,
    validate_add_version_target,
)


class SanitizationError(RuntimeError):
    """Raised when an Agent checkpoint or workspace cannot be restored safely."""


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


@dataclass(frozen=True)
class CheckpointManifest:
    schema_version: int
    checkpoint_id: str
    task_key: str
    base_sha: str
    round: int
    agent_role: str
    candidate_patch_sha256: str
    source_identity_sha256: str
    source_identity: dict[str, object]
    allowed_paths: tuple[str, ...]
    files: dict[str, dict[str, str]]
    root: Path = field(compare=False, repr=False)

    def to_dict(self, *, include_id: bool = True) -> dict[str, object]:
        value = asdict(self)
        value.pop("root", None)
        value["allowed_paths"] = list(self.allowed_paths)
        if not include_id:
            value.pop("checkpoint_id", None)
        return value


@dataclass(frozen=True)
class SanitizationReport:
    schema_version: int
    task_key: str
    checkpoint_id: str
    agent_role: str
    allowed_paths: tuple[str, ...]
    retained_changes: tuple[str, ...]
    actions: tuple[dict[str, str], ...]
    hard_stop_reasons: tuple[str, ...]
    clean: bool

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in ("allowed_paths", "retained_changes", "actions", "hard_stop_reasons"):
            value[key] = list(value[key])
        return value


def _git(repo: Path, *args: str, binary: bool = False, check: bool = True):
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=not binary,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise SanitizationError(detail.strip() or f"git {' '.join(args)} failed")
    return completed


def _changed_paths(repo: Path, base_sha: str) -> tuple[str, ...]:
    _git(repo, "add", "--intent-to-add", "--", ".")
    output = _git(
        repo,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        base_sha,
        "--",
        binary=True,
    ).stdout
    return tuple(sorted(path.decode() for path in output.split(b"\0") if path))


def _candidate_patch(repo: Path, base_sha: str) -> bytes:
    _git(repo, "add", "--intent-to-add", "--", ".")
    return _git(
        repo,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-renames",
        base_sha,
        "--",
        binary=True,
    ).stdout


def _mode(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISREG(metadata.st_mode):
        return "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
    if stat.S_ISLNK(metadata.st_mode):
        return "120000"
    return "special"


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SanitizationError(f"unsafe changed path: {value!r}")
    return path


def _assert_safe_parents(workspace: Path, relative: PurePosixPath) -> None:
    current = workspace
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SanitizationError(
                f"changed path has a symbolic-link parent: {relative}"
            )


def allowed_agent_paths(task: TaskSpec, role: str) -> tuple[str, ...]:
    """Return the only scenario-three paths an Agent role may modify."""
    assert task.mdu_path
    if role == "code-fixer":
        return (
            f"{task.mdu_path}/{task.version}/{task.os_version}/**",
            f"{task.mdu_path}/tests/**",
            f"{task.mdu_path}/meta.yml",
            f"{task.mdu_path}/README.md",
            f"{task.mdu_path}/doc/**",
        )
    if role == "testcase-creator":
        return (f"{task.mdu_path}/tests/**",)
    raise SanitizationError(f"unsupported Agent role: {role}")


def _matches_allowed(relative: str, allowed: tuple[str, ...]) -> bool:
    return any(
        relative.startswith(pattern.removesuffix("**"))
        if pattern.endswith("/**")
        else relative == pattern
        for pattern in allowed
    )


def _source_identity(workspace: Path, task: TaskSpec) -> dict[str, object]:
    assert task.mdu_path
    target = workspace / task.mdu_path / task.version / task.os_version
    return extract_source_identity(target, task.version)


def create_checkpoint(
    *,
    workspace: Path,
    base_sha: str,
    task: TaskSpec,
    destination: Path,
    round_number: int,
    agent_role: str,
) -> CheckpointManifest:
    workspace = Path(workspace).resolve()
    destination = Path(destination).resolve()
    if destination == workspace or workspace in destination.parents:
        raise SanitizationError("checkpoint destination must be outside workspace")
    validate_add_version_target(repo=workspace, task=task, base_sha=base_sha)
    if not task.task_key:
        raise SanitizationError("TaskSpec has no task_key")
    if destination.exists():
        raise SanitizationError("checkpoint destination already exists")
    destination.mkdir(parents=True)
    snapshot_root = destination / "files"
    files: dict[str, dict[str, str]] = {}
    for value in _changed_paths(workspace, base_sha):
        relative = _safe_relative(value)
        path = workspace / relative
        _assert_safe_parents(workspace, relative)
        if not path.exists() and not path.is_symlink():
            continue
        if _mode(path) not in {"100644", "100755"}:
            raise SanitizationError(f"checkpoint candidate has unsafe file: {value}")
        snapshot = snapshot_root / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        snapshot.write_bytes(data)
        snapshot.chmod(0o755 if _mode(path) == "100755" else 0o644)
        files[value] = {
            "sha256": _sha256(data),
            "mode": _mode(path),
            "snapshot": snapshot.relative_to(destination).as_posix(),
        }
    identity = _source_identity(workspace, task)
    draft: dict[str, object] = {
        "schema_version": 1,
        "task_key": task.task_key,
        "base_sha": base_sha,
        "round": round_number,
        "agent_role": agent_role,
        "candidate_patch_sha256": _sha256(_candidate_patch(workspace, base_sha)),
        "source_identity_sha256": _sha256(_canonical(identity)),
        "source_identity": identity,
        "allowed_paths": list(allowed_agent_paths(task, agent_role)),
        "files": files,
    }
    checkpoint_id = _sha256(_canonical(draft))
    payload = {**draft, "checkpoint_id": checkpoint_id}
    (destination / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return load_checkpoint(destination)


def load_checkpoint(destination: Path) -> CheckpointManifest:
    destination = Path(destination).resolve()
    try:
        payload = json.loads((destination / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SanitizationError("checkpoint manifest is missing or invalid") from error
    if not isinstance(payload, dict):
        raise SanitizationError("checkpoint manifest must be an object")
    supplied_id = str(payload.pop("checkpoint_id", ""))
    expected_id = _sha256(_canonical(payload))
    if supplied_id != expected_id:
        raise SanitizationError("checkpoint_id does not match manifest contents")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise SanitizationError("checkpoint files must be a mapping")
    for relative, raw in files.items():
        _safe_relative(relative)
        if not isinstance(raw, Mapping):
            raise SanitizationError("checkpoint file entry is invalid")
        snapshot = destination / str(raw.get("snapshot", ""))
        if not snapshot.is_file() or _sha256(snapshot.read_bytes()) != raw.get("sha256"):
            raise SanitizationError(f"checkpoint snapshot is invalid: {relative}")
    try:
        return CheckpointManifest(
            schema_version=int(payload["schema_version"]),
            checkpoint_id=supplied_id,
            task_key=str(payload["task_key"]),
            base_sha=str(payload["base_sha"]),
            round=int(payload["round"]),
            agent_role=str(payload["agent_role"]),
            candidate_patch_sha256=str(payload["candidate_patch_sha256"]),
            source_identity_sha256=str(payload["source_identity_sha256"]),
            source_identity=dict(payload["source_identity"]),
            allowed_paths=tuple(payload["allowed_paths"]),
            files={str(key): dict(value) for key, value in files.items()},
            root=destination,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SanitizationError("checkpoint manifest fields are invalid") from error


def _restore_snapshot(
    workspace: Path, checkpoint: CheckpointManifest, relative: str
) -> None:
    raw = checkpoint.files[relative]
    source = checkpoint.root / raw["snapshot"]
    target = workspace / relative
    if target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    target.chmod(0o755 if raw["mode"] == "100755" else 0o644)


def _base_file(repo: Path, base_sha: str, relative: str) -> tuple[bytes, str] | None:
    record = _git(
        repo, "ls-tree", base_sha, "--", relative, check=False
    ).stdout.strip()
    if not record:
        return None
    metadata, _separator, _path = record.partition("\t")
    mode, kind, _sha = metadata.split(" ", 2)
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise SanitizationError(f"base path is not a restorable file: {relative}")
    data = _git(repo, "show", f"{base_sha}:{relative}", binary=True).stdout
    return data, mode


def _restore_base(repo: Path, base_sha: str, relative: str) -> None:
    value = _base_file(repo, base_sha, relative)
    if value is None:
        raise SanitizationError(f"base path is missing: {relative}")
    data, mode = value
    target = repo / relative
    if target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o755 if mode == "100755" else 0o644)


def _remove_unauthorized(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def sanitize_agent_changes(
    *,
    workspace: Path,
    base_sha: str,
    task: TaskSpec,
    checkpoint: CheckpointManifest,
    report_path: Path,
) -> SanitizationReport:
    workspace = Path(workspace).resolve()
    if checkpoint.base_sha != base_sha or checkpoint.task_key != task.task_key:
        raise SanitizationError("checkpoint does not match task/base_sha")
    verified = load_checkpoint(checkpoint.root)
    if verified.checkpoint_id != checkpoint.checkpoint_id:
        raise SanitizationError("checkpoint_id changed since loading")
    allowed = checkpoint.allowed_paths
    actions: list[dict[str, str]] = []
    retained: list[str] = []
    for value in _changed_paths(workspace, base_sha):
        relative = _safe_relative(value)
        _assert_safe_parents(workspace, relative)
        path = workspace / relative
        if _matches_allowed(value, allowed) and not is_agent_control_path(value):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise SanitizationError(f"allowed path became unsafe: {value}")
            checkpoint_info = checkpoint.files.get(value)
            current_digest = _sha256(path.read_bytes()) if path.is_file() else "missing"
            if checkpoint_info is None or current_digest != checkpoint_info["sha256"]:
                retained.append(value)
            continue
        agent_digest = _sha256(path.read_bytes()) if path.is_file() else "missing"
        if value in checkpoint.files:
            _restore_snapshot(workspace, checkpoint, value)
            actions.append(
                {
                    "path": value,
                    "action": "restore-checkpoint",
                    "agent_sha256": agent_digest,
                    "restored_sha256": checkpoint.files[value]["sha256"],
                }
            )
        elif _base_file(workspace, base_sha, value) is not None:
            _restore_base(workspace, base_sha, value)
            actions.append(
                {
                    "path": value,
                    "action": "restore-base",
                    "agent_sha256": agent_digest,
                    "restored_sha256": _sha256((workspace / value).read_bytes()),
                }
            )
        else:
            _remove_unauthorized(path)
            actions.append(
                {
                    "path": value,
                    "action": "remove-unauthorized",
                    "agent_sha256": agent_digest,
                }
            )
    current_identity = _source_identity(workspace, task)
    if _sha256(_canonical(current_identity)) != checkpoint.source_identity_sha256:
        raise SanitizationError("source identity changed inside the Agent whitelist")
    validate_add_version_target(repo=workspace, task=task, base_sha=base_sha)
    report = SanitizationReport(
        schema_version=1,
        task_key=task.task_key or "",
        checkpoint_id=checkpoint.checkpoint_id,
        agent_role=checkpoint.agent_role,
        allowed_paths=allowed,
        retained_changes=tuple(sorted(retained)),
        actions=tuple(sorted(actions, key=lambda item: item["path"])),
        hard_stop_reasons=(),
        clean=True,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    return report

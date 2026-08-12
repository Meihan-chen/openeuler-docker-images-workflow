"""Deterministic planner for openEuler major-version image upgrades."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from scripts.lib.oe_upgrade_contract import UpgradeRequest, normalize_oe_version
from scripts.lib.task_spec import TaskSpec


class UpgradePlannerError(RuntimeError):
    """Raised when the pinned target repository cannot be planned safely."""


_TAG_RE = re.compile(
    r"^(?P<app>.+)-oe(?P<year>\d{2})(?P<month>\d{2})"
    r"(?:(?P<lts>lts)|sp(?P<sp>\d+))$",
    re.IGNORECASE,
)
_STABLE_VERSION_RE = re.compile(r"^v?(?P<numbers>\d+(?:\.\d+)*)$", re.IGNORECASE)
_SUPPORTED_ARCHES = ("x86_64", "aarch64")


@dataclass(frozen=True)
class PublishedEntry:
    mdu_path: PurePosixPath
    tag: str
    app_version: str
    oe_version: str
    dockerfile_path: PurePosixPath
    architectures: tuple[str, ...]
    original_index: int


@dataclass(frozen=True)
class UpgradePlan:
    schema_version: int
    request: UpgradeRequest
    tasks: tuple[TaskSpec, ...]
    planning_failures: tuple[dict[str, str], ...]
    warnings: tuple[dict[str, str], ...]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request": self.request.to_dict(),
            "tasks": [task.to_dict() for task in self.tasks],
            "planning_failures": list(self.planning_failures),
            "warnings": list(self.warnings),
            "summary": self.summary,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=not binary,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"" if binary else "")
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise UpgradePlannerError(f"git {' '.join(args)} failed: {detail}") from error
    return completed.stdout


def _tree(repo: Path, base_sha: str) -> tuple[set[str], tuple[str, ...]]:
    raw = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        base_sha,
        binary=True,
    )
    assert isinstance(raw, bytes)
    ordinary_files: set[str] = set()
    all_paths: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, kind, _sha = metadata.decode().split(" ", 2)
            path = path_bytes.decode()
        except (UnicodeDecodeError, ValueError) as error:
            raise UpgradePlannerError("git tree contains an invalid entry") from error
        all_paths.append(path)
        if kind == "blob" and mode in {"100644", "100755"}:
            ordinary_files.add(path)
    return ordinary_files, tuple(sorted(all_paths))


def _read_yaml(repo: Path, base_sha: str, path: str) -> Mapping[str, object]:
    raw = _git(repo, "show", f"{base_sha}:{path}")
    assert isinstance(raw, str)
    try:
        value = yaml.safe_load(raw) or {}
    except yaml.YAMLError as error:
        raise UpgradePlannerError(f"{path}: invalid YAML: {error}") from error
    if not isinstance(value, Mapping):
        raise UpgradePlannerError(f"{path}: expected a YAML mapping")
    return value


def _warning(
    mdu_path: str,
    *,
    entry: str = "",
    path: str = "",
    reason: str,
) -> dict[str, str]:
    return {
        "mdu_path": mdu_path,
        "entry": entry,
        "path": path,
        "reason": reason,
    }


def _tag_parts(tag: str) -> tuple[str, str] | None:
    match = _TAG_RE.fullmatch(tag)
    if not match:
        return None
    suffix = "-lts"
    if match.group("sp") is not None:
        suffix = f"-lts-sp{int(match.group('sp'))}"
    return (
        match.group("app"),
        normalize_oe_version(
            f"{match.group('year')}.{match.group('month')}{suffix}"
        ),
    )


def _architectures(value: object) -> tuple[str, ...] | None:
    if value is None or str(value).strip() == "":
        return _SUPPORTED_ARCHES
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw = [str(part).strip() for part in value]
    else:
        return None
    if not raw or len(set(raw)) != len(raw) or any(
        part not in _SUPPORTED_ARCHES for part in raw
    ):
        return None
    return tuple(part for part in _SUPPORTED_ARCHES if part in raw)


def _parse_entry(
    *,
    domain: str,
    mdu_path: str,
    tag: str,
    value: object,
    original_index: int,
    ordinary_files: set[str],
) -> tuple[PublishedEntry | None, dict[str, str] | None]:
    path_value = value.get("path") if isinstance(value, Mapping) else None
    path_text = str(path_value).strip() if path_value is not None else ""
    if not isinstance(value, Mapping) or not path_text:
        return None, _warning(
            mdu_path, entry=tag, path=path_text, reason="meta entry has no path"
        )
    if path_text.startswith("/"):
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="absolute meta path rejected",
        )
    path = PurePosixPath(path_text)
    if (
        "\\" in path_text
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != path_text
    ):
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="non-normalized meta path rejected",
        )
    if path.parts and path.parts[0] == domain:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="repository-relative meta path rejected",
        )
    if len(path.parts) != 3 or path.parts[-1] != "Dockerfile":
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="meta path must match <app-version>/<oe-version>/Dockerfile",
        )
    tag_parts = _tag_parts(tag)
    if tag_parts is None:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="meta tag has unsupported openEuler suffix",
        )
    app_version, tag_oe = tag_parts
    if app_version != path.parts[0]:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="tag application version does not match meta path",
        )
    try:
        path_oe = normalize_oe_version(path.parts[1])
    except ValueError:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="meta path has unsupported openEuler version",
        )
    if tag_oe != path_oe:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="tag openEuler version does not match meta path",
        )
    dockerfile = f"{mdu_path}/{path_text}"
    if dockerfile not in ordinary_files:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="Dockerfile is missing from the fixed Git tree",
        )
    architectures = _architectures(value.get("arch"))
    if architectures is None:
        return None, _warning(
            mdu_path,
            entry=tag,
            path=path_text,
            reason="meta architecture is unsupported",
        )
    return (
        PublishedEntry(
            mdu_path=PurePosixPath(mdu_path),
            tag=tag,
            app_version=app_version,
            oe_version=tag_oe,
            dockerfile_path=path,
            architectures=architectures,
            original_index=original_index,
        ),
        None,
    )


def _version_key(version: str) -> tuple[int, ...] | None:
    match = _STABLE_VERSION_RE.fullmatch(version)
    if not match:
        return None
    return tuple(int(part) for part in match.group("numbers").split("."))


def _oe_key(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(
        r"(?P<year>\d{2})\.(?P<month>\d{2})(?:-lts)?(?:-sp(?P<sp>\d+))?",
        version,
    )
    if not match:
        raise UpgradePlannerError(f"unsupported normalized openEuler version {version}")
    return (
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("sp") or -1),
    )


def _plan_mdu(
    *,
    repo: Path,
    request: UpgradeRequest,
    domain: str,
    image_name: str,
    mdu_path: str,
    ordinary_files: set[str],
) -> tuple[TaskSpec | None, list[dict[str, str]], str | None]:
    meta_path = f"{mdu_path}/meta.yml"
    if meta_path not in ordinary_files:
        return None, [], "meta.yml is missing"
    meta = _read_yaml(repo, request.base_sha, meta_path)
    entries: list[PublishedEntry] = []
    warnings: list[dict[str, str]] = []
    for index, (raw_tag, value) in enumerate(meta.items()):
        tag = str(raw_tag)
        entry, warning = _parse_entry(
            domain=domain,
            mdu_path=mdu_path,
            tag=tag,
            value=value,
            original_index=index,
            ordinary_files=ordinary_files,
        )
        if entry is not None:
            entries.append(entry)
        if warning is not None:
            warnings.append(warning)
    if not entries:
        return None, warnings, "no valid published entry"

    versions: dict[str, tuple[int, ...]] = {}
    for version in sorted({entry.app_version for entry in entries}):
        key = _version_key(version)
        if key is not None:
            versions[version] = key
    if not versions:
        return None, warnings, "no comparable stable application version"
    selected_version = max(versions, key=lambda value: (versions[value], value))
    candidates = [
        entry
        for entry in entries
        if entry.app_version == selected_version
        and entry.oe_version != request.oe_version
    ]
    if not candidates:
        return None, warnings, "no source openEuler version available"
    selected_oe = max(_oe_key(entry.oe_version) for entry in candidates)
    selected = [entry for entry in candidates if _oe_key(entry.oe_version) == selected_oe]
    identities = {
        (str(entry.dockerfile_path), entry.architectures) for entry in selected
    }
    if len(identities) != 1:
        return None, warnings, "conflicting entries for selected source openEuler version"
    source = min(selected, key=lambda entry: entry.original_index)
    task = TaskSpec.from_workflow_dispatch(
        {
            "schema_version": 2,
            "scenario": "oe-upgrade",
            "app": image_name,
            "image_name": image_name,
            "version": selected_version,
            "os_version": request.oe_version,
            "domain": domain,
            "source_url": "",
            "mdu_path": mdu_path,
            "derive_from": f"{selected_version}/{source.oe_version}",
            "architectures": list(source.architectures),
        }
    )
    return task, warnings, None


def _safe_index_path(domain: str, value: object) -> str:
    text = str(value).strip()
    path = PurePosixPath(text)
    if (
        not text
        or text.startswith("/")
        or "\\" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != text
    ):
        raise UpgradePlannerError(
            f"{domain}/image-list.yml contains unsafe MDU path {text!r}"
        )
    return f"{domain}/{path}"


def plan_upgrade(repo: Path, request: UpgradeRequest) -> UpgradePlan:
    repo = Path(repo)
    if not repo.is_dir():
        raise UpgradePlannerError(f"target repository does not exist: {repo}")
    ordinary_files, tree_paths = _tree(repo, request.base_sha)
    tasks: list[TaskSpec] = []
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    indexed_paths: set[str] = set()

    for domain in request.scope:
        image_list_path = f"{domain}/image-list.yml"
        if image_list_path not in ordinary_files:
            raise UpgradePlannerError(f"{image_list_path}: missing from fixed Git tree")
        image_list = _read_yaml(repo, request.base_sha, image_list_path)
        images = image_list.get("images")
        if not isinstance(images, Mapping):
            raise UpgradePlannerError(f"{image_list_path}: images must be a mapping")
        for raw_name, raw_path in images.items():
            image_name = str(raw_name).strip().lower()
            mdu_path = _safe_index_path(domain, raw_path)
            if mdu_path in indexed_paths:
                raise UpgradePlannerError(f"duplicate indexed MDU path: {mdu_path}")
            indexed_paths.add(mdu_path)
            task, entry_warnings, failure = _plan_mdu(
                repo=repo,
                request=request,
                domain=domain,
                image_name=image_name,
                mdu_path=mdu_path,
                ordinary_files=ordinary_files,
            )
            warnings.extend(entry_warnings)
            if task is not None:
                tasks.append(task)
            else:
                failures.append({"mdu_path": mdu_path, "reason": str(failure)})

    scope_prefixes = tuple(f"{domain}/" for domain in request.scope)
    meta_paths = {
        path.removesuffix("/meta.yml")
        for path in tree_paths
        if path.endswith("/meta.yml") and path.startswith(scope_prefixes)
    }
    for orphan in sorted(meta_paths - indexed_paths):
        warnings.append(
            _warning(
                orphan,
                path=f"{orphan}/meta.yml",
                reason="meta.yml is not indexed by image-list.yml",
            )
        )

    tasks.sort(key=lambda task: task.mdu_path or "")
    failures.sort(key=lambda failure: (failure["mdu_path"], failure["reason"]))
    warnings.sort(
        key=lambda warning: (
            warning["mdu_path"],
            warning["entry"],
            warning["path"],
            warning["reason"],
        )
    )
    summary = {
        "mdu_count": len(indexed_paths),
        "task_count": len(tasks),
        "planning_failed_count": len(failures),
        "warning_count": len(warnings),
    }
    if summary["mdu_count"] != summary["task_count"] + summary["planning_failed_count"]:
        raise UpgradePlannerError("planner accounting invariant failed")
    return UpgradePlan(
        schema_version=1,
        request=request,
        tasks=tuple(tasks),
        planning_failures=tuple(failures),
        warnings=tuple(warnings),
        summary=summary,
    )

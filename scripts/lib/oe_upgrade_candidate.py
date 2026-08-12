"""Deterministically derive one openEuler add-version candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

import yaml

from scripts.lib.task_spec import TaskSpec


class CandidateDerivationError(RuntimeError):
    """Raised when a pinned source cannot be copied and rewritten safely."""


@dataclass(frozen=True)
class DerivationReport:
    schema_version: int
    task_key: str
    source_directory: str
    target_directory: str
    source_tree_sha256: str
    source_dockerfile_sha256: str
    copied_files: tuple[str, ...]
    dockerfile_rewrites: tuple[dict[str, str], ...]
    meta_entry: dict[str, object]
    readme: dict[str, object]
    source_identity: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["copied_files"] = list(self.copied_files)
        value["dockerfile_rewrites"] = list(self.dockerfile_rewrites)
        return value


def _git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise CandidateDerivationError(
            completed.stderr.strip() or f"git {' '.join(args)} failed"
        )
    return completed.stdout.strip()


def _target_tag(task: TaskSpec) -> str:
    suffix = task.os_version.replace("-lts-sp", "sp").replace("-lts", "lts")
    return f"{task.version}-oe{suffix.replace('.', '').replace('-', '')}"


def _logical_instructions(content: str) -> list[tuple[int, int, str]]:
    lines = content.splitlines(keepends=True)
    instructions: list[tuple[int, int, str]] = []
    offset = 0
    start = 0
    parts: list[str] = []
    for line in lines:
        if not parts:
            start = offset
        stripped_newline = line.rstrip("\r\n")
        continued = stripped_newline.rstrip().endswith("\\")
        part = stripped_newline.rstrip()
        if continued:
            part = part[:-1]
        parts.append(part)
        offset += len(line)
        if not continued:
            instructions.append((start, offset, " ".join(parts)))
            parts = []
    if parts:
        instructions.append((start, len(content), " ".join(parts)))
    return instructions


def _resolve_image(image: str, arguments: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain") or ""
        return arguments.get(name, match.group(0))

    return re.sub(
        r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
        r"\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)",
        replace,
        image,
    )


def rewrite_dockerfile_base(
    content: str,
    *,
    source_oe: str,
    target_oe: str,
    relative_path: str,
) -> tuple[str, tuple[dict[str, str], ...]]:
    """Rewrite only FROM instructions resolving to the pinned OE base."""
    expected = f"openeuler/openeuler:{source_oe}"
    replacement = f"openeuler/openeuler:{target_oe}"
    arguments: dict[str, str] = {}
    argument_spans: dict[str, list[tuple[int, int, str, str]]] = {}
    direct_edits: list[tuple[int, int, str, str]] = []
    required_arguments: set[str] = set()

    for start, end, logical in _logical_instructions(content):
        stripped = logical.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, _, remainder = stripped.partition(" ")
        if keyword.upper() == "ARG":
            assignment = remainder.strip()
            name, separator, value = assignment.partition("=")
            name = name.strip()
            if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                value = value.strip()
                arguments[name] = value
                argument_spans.setdefault(name, []).append((start, end, value, logical))
            continue
        if keyword.upper() != "FROM":
            continue
        tokens = remainder.split()
        while tokens and tokens[0].startswith("--"):
            tokens.pop(0)
        if not tokens:
            continue
        image = tokens[0]
        if _resolve_image(image, arguments) != expected:
            continue
        variables = {
            match.group("braced") or match.group("plain")
            for match in re.finditer(
                r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
                r"\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)",
                image,
            )
        }
        if variables:
            required_arguments.update(variables)
        else:
            block = content[start:end]
            if block.count(image) != 1:
                raise CandidateDerivationError(
                    f"{relative_path}: FROM image cannot be rewritten uniquely"
                )
            direct_edits.append((start, end, image, replacement))

    edits = list(direct_edits)
    for name in sorted(required_arguments):
        definitions = argument_spans.get(name, [])
        if len(definitions) != 1:
            raise CandidateDerivationError(
                f"{relative_path}: ARG {name} is not defined exactly once"
            )
        start, end, value, _logical = definitions[0]
        if value == expected:
            new_value = replacement
        elif value == source_oe:
            new_value = target_oe
        else:
            raise CandidateDerivationError(
                f"{relative_path}: ARG {name} does not uniquely identify source OE"
            )
        edits.append((start, end, value, new_value))

    records: list[dict[str, str]] = []
    rewritten = content
    for start, end, old_value, new_value in sorted(edits, reverse=True):
        block = rewritten[start:end]
        if block.count(old_value) != 1:
            raise CandidateDerivationError(
                f"{relative_path}: instruction value cannot be rewritten uniquely"
            )
        new_block = block.replace(old_value, new_value, 1)
        rewritten = rewritten[:start] + new_block + rewritten[end:]
        records.append(
            {
                "file": relative_path,
                "instruction": " ".join(block.split()),
                "old": old_value,
                "new": new_value,
            }
        )
    records.reverse()
    return rewritten, tuple(records)


def _tree_inventory(root: Path) -> tuple[tuple[str, int, str], ...]:
    inventory: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise CandidateDerivationError(
                f"source tree contains a symbolic link: {relative}"
            )
        if path.is_dir():
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise CandidateDerivationError(
                f"source tree contains a special file: {relative}"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        inventory.append((relative, stat.S_IMODE(metadata.st_mode), digest))
    return tuple(inventory)


def _tree_digest(inventory: tuple[tuple[str, int, str], ...]) -> str:
    serialized = json.dumps(inventory, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()


def _copy_tree(source: Path, target: Path) -> tuple[str, ...]:
    source_inventory = _tree_inventory(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        for relative, mode, _digest in source_inventory:
            source_file = source / relative
            target_file = temporary / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target_file, follow_symlinks=False)
            target_file.chmod(mode)
        if _tree_inventory(temporary) != source_inventory:
            raise CandidateDerivationError("copied source tree does not match its input")
        temporary.rename(target)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return tuple(relative for relative, _mode, _digest in source_inventory)


def extract_source_identity(source: Path, version: str) -> dict[str, object]:
    """Extract application/source identity from one candidate image directory."""
    dockerfile_path = source / "Dockerfile"
    if not dockerfile_path.is_file() or dockerfile_path.is_symlink():
        raise CandidateDerivationError("candidate Dockerfile is missing or unsafe")
    dockerfile = dockerfile_path.read_text()
    version_args: dict[str, str] = {}
    for match in re.finditer(
        r"^\s*ARG\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*VERSION[A-Za-z0-9_]*)"
        r"=(?P<value>\S+)\s*$",
        dockerfile,
        re.IGNORECASE | re.MULTILINE,
    ):
        version_args[match.group("name")] = match.group("value")
    urls = sorted(set(re.findall(r"https?://[^\s'\"\\]+", dockerfile)))
    revisions = sorted(
        set(
            match.group(1)
            for match in re.finditer(
                r"(?:--branch|--tag|checkout)\s+([^\s;&|]+)", dockerfile
            )
        )
    )
    checksums = sorted(
        set(re.findall(r"(?i)(?:sha256:)?[0-9a-f]{64}", dockerfile))
    )
    local_files = {
        path.relative_to(source).as_posix(): "sha256:" + hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(source.rglob("*"))
        if path.is_file() and path.name != "Dockerfile"
    }
    return {
        "app_version": version,
        "version_args": version_args,
        "source_urls": urls,
        "git_revisions": revisions,
        "checksums": checksums,
        "local_files": local_files,
    }


def _append_meta(meta: Path, task: TaskSpec) -> dict[str, object]:
    before_bytes = meta.read_bytes()
    try:
        before = yaml.safe_load(before_bytes) or {}
    except yaml.YAMLError as error:
        raise CandidateDerivationError("meta.yml is invalid") from error
    if not isinstance(before, Mapping):
        raise CandidateDerivationError("meta.yml must contain a mapping")
    tag = _target_tag(task)
    target_path = f"{task.version}/{task.os_version}/Dockerfile"
    entry: dict[str, str] = {"path": target_path}
    if len(task.architectures) == 1:
        entry["arch"] = task.architectures[0]
    if tag in before:
        if before[tag] != entry:
            raise CandidateDerivationError(f"meta.yml target tag conflicts: {tag}")
        return {
            "tag": tag,
            "path": target_path,
            "architectures": list(task.architectures),
        }
    if any(
        isinstance(value, Mapping) and value.get("path") == target_path
        for value in before.values()
    ):
        raise CandidateDerivationError(f"meta.yml target path conflicts: {target_path}")
    separator = b"" if not before_bytes or before_bytes.endswith(b"\n") else b"\n"
    fragment = yaml.safe_dump({tag: entry}, sort_keys=False).encode()
    meta.write_bytes(before_bytes + separator + fragment)
    after = yaml.safe_load(meta.read_bytes()) or {}
    if not isinstance(after, Mapping) or any(after.get(key) != value for key, value in before.items()):
        raise CandidateDerivationError("meta.yml historical entries changed")
    return {
        "tag": tag,
        "path": target_path,
        "architectures": list(task.architectures),
    }


def _append_readme(readme: Path, task: TaskSpec, source_oe: str) -> dict[str, object]:
    relative = f"{task.mdu_path}/README.md"
    if not readme.is_file():
        return {
            "path": relative,
            "row_added": False,
            "reason": "readme-update-skipped: file is missing",
        }
    content = readme.read_text()
    source_tag_suffix = source_oe.replace("-lts-sp", "sp").replace("-lts", "lts")
    source_tag = f"{task.version}-oe{source_tag_suffix.replace('.', '').replace('-', '')}"
    target_tag = _target_tag(task)
    source_dockerfile = f"{task.version}/{source_oe}/Dockerfile"
    target_dockerfile = f"{task.version}/{task.os_version}/Dockerfile"
    lines = content.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith("|")
        and source_tag in line
        and source_oe in line
        and source_dockerfile in line
    ]
    if len(matches) != 1:
        return {
            "path": relative,
            "row_added": False,
            "reason": "readme-update-skipped: source tag table row is ambiguous",
        }
    new_row = lines[matches[0]].replace(source_tag, target_tag).replace(
        source_dockerfile, target_dockerfile
    )
    # README display text frequently uses upper-case LTS/SP while paths use
    # the normalized lower-case form.  Replace both representations without
    # touching any historical row.
    new_row = re.sub(
        re.escape(source_oe), task.os_version, new_row, flags=re.IGNORECASE
    )
    existing_target = [line for line in lines if target_tag in line]
    if existing_target:
        if existing_target == [new_row]:
            return {"path": relative, "row_added": False, "reason": "already-present"}
        raise CandidateDerivationError("README target tag row conflicts")
    insert_at = matches[0] + 1
    while insert_at < len(lines) and lines[insert_at].lstrip().startswith("|"):
        insert_at += 1
    lines.insert(insert_at, new_row)
    readme.write_text("".join(lines))
    return {"path": relative, "row_added": True, "reason": ""}


def prepare_upgrade_candidate(
    *,
    workspace: Path,
    task: TaskSpec,
    base_sha: str,
    report_dir: Path,
) -> DerivationReport:
    workspace = Path(workspace)
    if task.schema_version != 2 or task.scenario != "oe-upgrade":
        raise CandidateDerivationError("oe-upgrade preparation requires TaskSpec v2")
    if _git(workspace, "rev-parse", "HEAD") != base_sha:
        raise CandidateDerivationError("workspace HEAD does not match base_sha")
    if _git(workspace, "status", "--porcelain"):
        raise CandidateDerivationError("workspace must be clean before derivation")
    assert task.mdu_path and task.derive_from and task.task_key
    mdu = workspace / task.mdu_path
    source = mdu / task.derive_from
    target = mdu / task.version / task.os_version
    try:
        source.relative_to(mdu)
        target.relative_to(mdu)
    except ValueError as error:
        raise CandidateDerivationError("source or target escapes the MDU") from error
    if not source.is_dir():
        raise CandidateDerivationError("derive_from directory is missing")
    if target.exists() or target.is_symlink():
        raise CandidateDerivationError("target openEuler directory already exists")
    for parent in (mdu, *source.parents):
        if parent == workspace.parent:
            break
        if parent.is_symlink():
            raise CandidateDerivationError("derive_from has a symbolic link parent")
        if parent == workspace:
            break
    source_inventory = _tree_inventory(source)
    dockerfile = source / "Dockerfile"
    if not dockerfile.is_file():
        raise CandidateDerivationError("derive_from Dockerfile is missing")
    source_dockerfile = dockerfile.read_text()
    source_identity = extract_source_identity(source, task.version)
    copied_files = _copy_tree(source, target)
    source_oe = task.derive_from.split("/", 1)[1]
    target_dockerfile = target / "Dockerfile"
    rewritten, rewrites = rewrite_dockerfile_base(
        target_dockerfile.read_text(),
        source_oe=source_oe,
        target_oe=task.os_version,
        relative_path=f"{task.mdu_path}/{task.version}/{task.os_version}/Dockerfile",
    )
    target_dockerfile.write_text(rewritten)
    meta_entry = _append_meta(mdu / "meta.yml", task)
    readme_report = _append_readme(mdu / "README.md", task, source_oe)
    report = DerivationReport(
        schema_version=1,
        task_key=task.task_key,
        source_directory=source.relative_to(workspace).as_posix(),
        target_directory=target.relative_to(workspace).as_posix(),
        source_tree_sha256=_tree_digest(source_inventory),
        source_dockerfile_sha256="sha256:" + hashlib.sha256(
            source_dockerfile.encode()
        ).hexdigest(),
        copied_files=copied_files,
        dockerfile_rewrites=rewrites,
        meta_entry=meta_entry,
        readme=readme_report,
        source_identity=source_identity,
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "derivation-report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    return report

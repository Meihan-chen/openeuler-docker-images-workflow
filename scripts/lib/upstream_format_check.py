"""Run EulerPublisher's live format gate without vendoring its source."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import logging
import os
import subprocess
import sys
import tempfile
import types
import uuid
from pathlib import Path
from typing import Iterator


EULERPUBLISHER_REPOSITORY = (
    "https://gitcode.com/openeuler/eulerpublisher.git"
)
EULERPUBLISHER_BRANCH = "master"
FORMAT_CHECK_PATH = Path("update/container/app/format.py")
_SUPPORTED_ARCHITECTURES = {"x86_64", "aarch64"}
_OUTPUT_LIMIT = 12000


class _CheckerImportError(RuntimeError):
    pass


class _X86PlatformAdapter:
    """Override only machine(); preserve any other upstream platform calls."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    @staticmethod
    def machine() -> str:
        return "x86_64"

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


def _run_git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


def _changed_files(workspace: Path) -> list[str]:
    tracked = _run_git(
        "diff",
        "--name-only",
        "--diff-filter=ACMRT",
        "HEAD",
        "--",
        cwd=workspace,
    ).splitlines()
    untracked = _run_git(
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        cwd=workspace,
    ).splitlines()
    return sorted({path for path in (*tracked, *untracked) if path})


def _failure_text(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        details = str(error.stderr or error.stdout or "").strip()
        if details:
            return details[-_OUTPUT_LIMIT:]
    return str(error) or error.__class__.__name__


@contextlib.contextmanager
def _eulerpublisher_import_stubs() -> Iterator[None]:
    """Provide the logger imported by the standalone upstream module."""
    package = types.ModuleType("eulerpublisher")
    publisher = types.ModuleType("eulerpublisher.publisher")
    publisher.logger = logging.getLogger("eulerpublisher.format")
    package.publisher = publisher
    previous = {
        name: sys.modules.get(name)
        for name in ("eulerpublisher", "eulerpublisher.publisher")
    }
    sys.modules["eulerpublisher"] = package
    sys.modules["eulerpublisher.publisher"] = publisher
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _load_checker(source: Path):
    module_name = f"_eulerpublisher_format_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load upstream checker: {source}")
    module = importlib.util.module_from_spec(spec)
    with _eulerpublisher_import_stubs():
        spec.loader.exec_module(module)
    return module


def _execute_checker(
    *,
    source: Path,
    workspace: Path,
    changed_files: list[str],
) -> tuple[int, str]:
    try:
        checker = _load_checker(source)
    except Exception as error:
        raise _CheckerImportError(_failure_text(error)) from error
    # format.py currently returns without checking on non-x86 machines. Make
    # the requested native-runner identity deterministic, while replacing only
    # this module's reference (never global platform.machine()).
    if hasattr(checker, "platform"):
        checker.platform = _X86PlatformAdapter(checker.platform)
    output = io.StringIO()
    previous_cwd = Path.cwd()
    try:
        os.chdir(workspace)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            head, body, fail_count = checker.check_report(changed_files)
    finally:
        os.chdir(previous_cwd)
    if not isinstance(fail_count, int) or fail_count < 0:
        raise TypeError("format.py returned an invalid failure count")
    rendered = "\n".join(
        part.strip()
        for part in (str(head or ""), str(body or ""), output.getvalue())
        if part.strip()
    )
    return fail_count, rendered[-_OUTPUT_LIMIT:]


def run_upstream_format_check(
    *,
    workspace: Path,
    architecture: str,
    temp_root: Path,
    repository: str = EULERPUBLISHER_REPOSITORY,
) -> dict[str, object]:
    """Sparse-clone and execute the current EulerPublisher format checker.

    Operational failures are returned as structured evidence so this advisory
    pre-check never prevents the native Docker checks from running.
    """
    if architecture not in _SUPPORTED_ARCHITECTURES:
        raise ValueError("architecture must be x86_64 or aarch64")
    workspace = Path(workspace).resolve()
    temp_root = Path(temp_root).resolve()
    base: dict[str, object] = {
        "repository": repository,
        "file": FORMAT_CHECK_PATH.as_posix(),
        "runner_architecture": architecture,
        "compatibility_override": architecture == "aarch64",
    }
    try:
        changed_files = _changed_files(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            **base,
            "status": "failed",
            "kind": "infra",
            "stage": "changed_files",
            "failure": _failure_text(error),
        }
    base["changed_files"] = changed_files
    try:
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="eulerpublisher-format-",
            dir=temp_root,
        ) as checkout_text:
            checkout = Path(checkout_text)
            _run_git(
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--no-checkout",
                "--no-tags",
                "--branch",
                EULERPUBLISHER_BRANCH,
                repository,
                str(checkout),
            )
            _run_git("sparse-checkout", "init", "--no-cone", cwd=checkout)
            _run_git(
                "sparse-checkout",
                "set",
                FORMAT_CHECK_PATH.as_posix(),
                cwd=checkout,
            )
            _run_git("checkout", "--detach", "HEAD", cwd=checkout)
            commit_sha = _run_git("rev-parse", "HEAD", cwd=checkout)
            source = checkout / FORMAT_CHECK_PATH
            if not source.is_file():
                raise FileNotFoundError(
                    f"sparse clone did not contain {FORMAT_CHECK_PATH}"
                )
            base["commit_sha"] = commit_sha
            try:
                fail_count, output = _execute_checker(
                    source=source,
                    workspace=workspace,
                    changed_files=changed_files,
                )
            except _CheckerImportError as error:
                return {
                    **base,
                    "status": "failed",
                    "kind": "infra",
                    "stage": "import",
                    "failure": _failure_text(error),
                }
            except Exception as error:
                return {
                    **base,
                    "status": "failed",
                    "kind": "candidate",
                    "stage": "execute",
                    "failure": _failure_text(error),
                }
    except (OSError, subprocess.SubprocessError) as error:
        return {
            **base,
            "status": "failed",
            "kind": "infra",
            "stage": "clone",
            "failure": _failure_text(error),
        }

    result: dict[str, object] = {
        **base,
        "status": "failed" if fail_count else "passed",
        "kind": "candidate",
        "stage": "execute",
        "fail_count": fail_count,
        "output": output,
    }
    if fail_count:
        result["failure"] = (
            f"upstream format check reported {fail_count} failure(s)"
        )
    return result

"""Route a failure to the action that can resolve it.

The Fixer received free-text evidence and had to infer both what broke and what
to do about it. That inference went wrong in ways the harness already knew how
to answer: a Goss config parse error was read as a UID permission problem
(Run 30480464176), and one unpacked upstream tarball was expressed as 496
out-of-scope file errors whose obvious repair — reverting candidate files — was
the opposite of the correct one (Run 30567356119).

Classification here is deterministic and derived only from evidence the harness
produces itself, so it stays trustworthy when the model is not.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

_SCOPE_ERROR_RE = re.compile(
    r"change outside task scope or wrong status: (?P<status>\S+) (?P<path>\S+)"
)
# Only a newly added path can be research output. Telling an Agent to delete a
# modified or deleted tracked file because it looked unfamiliar would destroy
# real content, so anything but an addition rules hygiene out.
_ADDED_STATUS = "A"
# Goss and YAML reject a malformed file before any assertion runs, so these
# never mean the image misbehaved. The same text can appear while a build step
# processes upstream YAML, which is a build failure, so the stage has to agree.
_CONFIG_PARSE_MARKERS = (
    "invalid attribute for",
    "error unmarshalling",
    "yaml: line",
    "yaml: unmarshal errors",
    "cannot unmarshal",
)
_CONFIG_PARSE_STAGE = "dgoss"
_TIMEOUT_RETURNCODE = 124
_RUNTIME_STAGES = ("dgoss", "shared_tests", "restart_persistence")

_GUIDANCE = {
    "workspace-hygiene": (
        "{paths} is research output that was written into the target "
        "repository, not candidate content. Delete or move it under the "
        "scratch directory; do not revert candidate files."
    ),
    "candidate-scope": (
        "The candidate changed paths this task does not own. Restrict the "
        "change to the task's own files."
    ),
    "image-contract": (
        "The deterministic repository contract assigned these findings to "
        "Image Creator. Repair only the listed image-owned content."
    ),
    "test-contract": (
        "The deterministic test contract assigned these findings to "
        "Testcase Creator. Repair only the listed test-owned content."
    ),
    "config-parse": (
        "A Goss or YAML file failed to parse before any assertion ran, so the "
        "image behaviour is still unknown. Fix the test configuration syntax "
        "and do not change the Dockerfile for this failure."
    ),
    "lint-advisory": (
        "Hadolint produced advisory diagnostics. Record them for review; do "
        "not request an automatic Creator repair."
    ),
    "build-error": (
        "The image did not build. Fix the build definition; runtime "
        "assertions were never reached."
    ),
    "runtime-error": (
        "The image built but did not behave as the tests expect. Decide "
        "whether the image or the assertion is wrong before editing either."
    ),
    "infra": (
        "This failure came from the execution environment, not the candidate. "
        "Do not modify any file; the run needs to be retried."
    ),
    "unclassified": (
        "The harness could not classify this failure from the evidence it "
        "captured. Report insufficient evidence rather than guessing."
    ),
}


def _stray_roots(
    errors: Sequence[object],
    allowed_roots: Sequence[str],
) -> list[str]:
    """Top-level paths that exist only because something wrote research output.

    Every error must be an addition outside the task's own roots. One modified
    or deleted tracked file, or one error of a different shape, means the
    candidate itself reached out of scope and nothing here may be deleted.
    """
    allowed = set(allowed_roots)
    roots: list[str] = []
    for error in errors:
        match = _SCOPE_ERROR_RE.search(str(error))
        if not match or match.group("status") != _ADDED_STATUS:
            return []
        root = match.group("path").split("/", 1)[0]
        if root in allowed:
            return []
        if root not in roots:
            roots.append(root)
    return roots


def classify_failure(
    *,
    report: Mapping[str, object] | None = None,
    gate: Mapping[str, object] | None = None,
    lint: Mapping[str, object] | None = None,
    allowed_roots: Sequence[str] = (),
) -> dict[str, object]:
    """Name the failure category and the action it calls for.

    `report` is a native validation report, `gate` a deterministic target
    contract report, `lint` a Hadolint report. The first failing one decides;
    none failing means the evidence was too thin to classify, which is itself
    worth saying out loud rather than inventing a repair.
    """
    category = "unclassified"
    stray: list[str] = []
    owner = ""
    finding_codes: list[str] = []

    structured_findings = []
    if gate is not None and isinstance(gate.get("findings"), list):
        structured_findings = [
            finding
            for finding in gate["findings"]
            if isinstance(finding, Mapping)
            and finding.get("level") == "delivery_stop"
        ]
    if structured_findings:
        owners = {str(finding.get("owner", "")) for finding in structured_findings}
        if owners == {"image_creator"}:
            category = "image-contract"
            owner = "image_creator"
        elif owners == {"testcase_creator"}:
            category = "test-contract"
            owner = "testcase_creator"
        else:
            category = "candidate-scope"
        finding_codes = [
            str(finding.get("code", ""))
            for finding in structured_findings
            if finding.get("code")
        ]
    elif gate is not None and gate.get("status") != "passed":
        errors = gate.get("errors")
        errors = list(errors) if isinstance(errors, list) else []
        if errors:
            stray = _stray_roots(errors, allowed_roots)
            category = "workspace-hygiene" if stray else "candidate-scope"
    elif lint is not None and lint.get("blocking") is True:
        category = "infra"
    elif lint is not None and (
        lint.get("diagnostic_status") == "findings"
        or lint.get("status") != "passed"
    ):
        category = "lint-advisory"
    elif report is not None and report.get("status") != "passed":
        details = report.get("failure_details")
        details = details if isinstance(details, Mapping) else {}
        failure = str(report.get("failure", "")).lower()
        stage = str(report.get("failed_stage", ""))
        if details.get("returncode") == _TIMEOUT_RETURNCODE:
            category = "infra"
        elif stage == _CONFIG_PARSE_STAGE and any(
            marker in failure for marker in _CONFIG_PARSE_MARKERS
        ):
            category = "config-parse"
        elif stage == "native_build":
            category = "build-error"
        elif stage in _RUNTIME_STAGES:
            category = "runtime-error"

    guidance = _GUIDANCE[category]
    if stray:
        guidance = guidance.format(
            paths=", ".join(f"`{root}/`" for root in stray)
        )
    result: dict[str, object] = {"category": category, "guidance": guidance}
    if owner:
        result["owner"] = owner
    if finding_codes:
        result["finding_codes"] = finding_codes
    if stray:
        result["stray_paths"] = stray
    return result

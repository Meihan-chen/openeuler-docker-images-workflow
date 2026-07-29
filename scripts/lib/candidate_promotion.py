"""Promotion of one immutable validation bundle into one local Git commit."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.lib.candidate_bundle import CandidateBundle
from scripts.lib.git_workspace import TargetWorkspace


class CandidatePromotionError(RuntimeError):
    """Raised when a validated candidate cannot be reused safely."""


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
) -> CandidatePromotion:
    bundle = CandidateBundle.verify(
        candidate_dir,
        expected_run_id=expected_run_id,
    )
    if bundle.promotion_action(workspace.base_sha) != "reuse":
        raise CandidatePromotionError(
            "target master changed after validation; run validate_only again"
        )

    workspace.apply_patch(bundle.root / "changes.patch")
    commit_sha = workspace.commit_candidate(
        branch=bundle.task.branch,
        message=(
            f"feat: add {bundle.task.app} {bundle.task.version} image "
            f"for openEuler {bundle.task.os_version}"
        ),
    )
    return CandidatePromotion(
        branch=bundle.task.branch,
        base_sha=bundle.manifest.base_sha,
        commit_sha=commit_sha,
        candidate_sha256=bundle.manifest.content_sha256,
        validated_run_id=bundle.manifest.validated_run_id,
    )

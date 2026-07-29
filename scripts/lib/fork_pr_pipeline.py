"""Promote one immutable validation artifact into one test-fork pull request."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from scripts.harness.compose_pr import (
    compose_pull_request,
    deliver_promoted_candidate,
)
from scripts.lib.candidate_bundle import CandidateBundle
from scripts.lib.candidate_promotion import promote_candidate
from scripts.lib.delivery_config import DeliveryConfig
from scripts.lib.git_workspace import TargetWorkspace
from scripts.utils.gitcode import GitCodeClient


class ForkPRPipelineError(RuntimeError):
    """Raised when fork delivery is not configured safely."""


TARGET_SOURCE = (
    "https://gitcode.com/openeuler/openeuler-docker-images.git"
)


def deliver_validated_candidate(
    *,
    candidate_dir: Path,
    expected_run_id: str,
    workspace_dir: Path,
    target_source: str,
    config: DeliveryConfig,
    username: str,
    token: str,
    clone: Callable[..., TargetWorkspace] = TargetWorkspace.clone,
    promote: Callable[..., Any] = promote_candidate,
    client_factory: Callable[..., Any] = GitCodeClient,
    deliver: Callable[..., Any] = deliver_promoted_candidate,
) -> Any:
    if config.environment != "test" or config.delivery_mode != "fork_pr":
        raise ForkPRPipelineError(
            "validated candidate delivery requires test fork_pr mode"
        )
    if not token:
        raise ForkPRPipelineError("GitCode token is required")
    if not username:
        raise ForkPRPipelineError("GitCode username is required")

    bundle = CandidateBundle.verify(
        candidate_dir,
        expected_run_id=expected_run_id,
    )
    workspace = clone(
        target_source,
        workspace_dir,
        branch=config.target_branch,
    )
    if workspace.base_sha != bundle.manifest.base_sha:
        raise ForkPRPipelineError(
            "target master changed after validation; run validate_only again"
        )
    promotion = promote(
        candidate_dir=candidate_dir,
        expected_run_id=expected_run_id,
        workspace=workspace,
    )
    content = compose_pull_request(bundle)
    client = client_factory(token=token)
    return deliver(
        repo=workspace.path,
        config=config,
        promotion=promotion,
        username=username,
        token=token,
        title=content.title,
        body=content.body,
        client=client,
    )

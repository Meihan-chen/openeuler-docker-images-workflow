#!/usr/bin/env python3
"""Single public CLI for the phase-one new-image workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.agent_runtime import AgentRuntimeError
from scripts.lib.candidate_bundle import (
    CandidateBundle,
    CandidateBundleError,
)
from scripts.lib.generation_pipeline import (
    GenerationPipelineError,
    lint_dockerfile,
    run_generation_pipeline,
    write_smoke_candidate,
)
from scripts.lib.git_workspace import GitWorkspaceError, TargetWorkspace
from scripts.lib.gitcode_client import (
    DeliveryConfig,
    DeliveryConfigError,
    GitCodeClient,
    GitCodeClientError,
)
from scripts.lib.issue_lifecycle import (
    IssueLifecycleError,
    run_controlled_issue_probe,
)
from scripts.lib.native_repair import (
    NativeRepairError,
    validate_native_with_repairs,
)
from scripts.lib.native_validation import (
    NativeValidationError,
    release_run_builders,
    validate_native_image,
    validate_native_smoke,
)
from scripts.lib.pr_delivery import (
    ForkPRPipelineError,
    GitDeliveryError,
    PRDeliveryError,
    TARGET_SOURCE,
    deliver_validated_candidate,
)
from scripts.lib.task_spec import TaskSpec, TaskSpecError
from scripts.lib.target_contract import (
    TargetContractError,
    validate_generated_target,
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _load_task(path: Path) -> TaskSpec:
    return TaskSpec.from_json(path.read_text())


def _task_spec(args: argparse.Namespace) -> None:
    task = TaskSpec.from_workflow_dispatch(
        {
            "app": args.app,
            "version": args.version,
            "os_version": args.os_version,
            "domain": args.domain,
            "source_url": args.source_url,
        }
    )
    _write_json(args.output, task.to_dict())
    _print_json(
        {
            "branch": task.branch,
            "output": str(args.output),
            "task_id": task.task_id,
        }
    )


def _delivery_config(args: argparse.Namespace) -> None:
    config = DeliveryConfig.from_mapping(
        {
            "environment": args.environment,
            "delivery_mode": args.delivery_mode,
            "target_repo": args.target_repo,
            "push_repo": args.push_repo,
            "target_branch": args.target_branch,
            "duplicate_pr_guard": args.duplicate_pr_guard,
        }
    )
    summary = {
        **asdict(config),
        "allows_branch_push": config.allows_branch_push,
        "allows_pr_create": config.allows_pr_create,
    }
    _write_json(args.output, summary)
    _print_json(summary)


def _candidate_create(args: argparse.Namespace) -> None:
    bundle = CandidateBundle.create(
        args.candidate_dir,
        task=_load_task(args.task_spec),
        base_sha=args.base_sha,
        validated_run_id=args.validated_run_id,
    )
    _print_json(
        {
            "candidate_dir": str(bundle.root),
            "content_sha256": bundle.manifest.content_sha256,
            "task_id": bundle.manifest.task_id,
            "validated_run_id": bundle.manifest.validated_run_id,
        }
    )


def _candidate_verify(args: argparse.Namespace) -> None:
    bundle = CandidateBundle.verify(
        args.candidate_dir,
        expected_run_id=args.expected_run_id,
    )
    _print_json(
        {
            "content_sha256": bundle.manifest.content_sha256,
            "task_id": bundle.manifest.task_id,
            "validated_run_id": bundle.manifest.validated_run_id,
        }
    )


def _fork_deliver(args: argparse.Namespace) -> None:
    token = os.environ.get("GITCODE_TOKEN", "")
    if not token:
        raise ForkPRPipelineError("GITCODE_TOKEN is required")
    config = DeliveryConfig.from_mapping(
        {
            "environment": "test",
            "delivery_mode": "fork_pr",
            "target_repo": "openeuler/openeuler-docker-images",
            "push_repo": "qq_42020325/openeuler-docker-images",
            "target_branch": "master",
        }
    )
    resource = deliver_validated_candidate(
        candidate_dir=args.candidate_dir,
        expected_run_id=args.expected_run_id,
        workspace_dir=args.workspace,
        target_source=TARGET_SOURCE,
        config=config,
        username=args.gitcode_username,
        token=token,
        delivery_run_id=args.delivery_run_id,
        delivery_run_attempt=args.delivery_run_attempt,
    )
    _print_json(
        {
            "number": resource.number,
            "url": resource.url,
            "validated_run_id": args.expected_run_id,
        }
    )


def _issue_contract_test(args: argparse.Namespace) -> None:
    token = os.environ.get("GITCODE_TOKEN", "")
    if not token:
        raise IssueLifecycleError("GITCODE_TOKEN is required")
    resource = run_controlled_issue_probe(
        client=GitCodeClient(token=token),
        target_repo="openeuler/openeuler-docker-images",
        task=_load_task(args.task_spec),
        environment="test",
        operation="failure_issue_contract_test",
        github_run_id=args.github_run_id,
        failure_stage=args.failure_stage,
    )
    _print_json(
        {
            "number": resource.number,
            "state": "closed",
            "url": resource.url,
        }
    )


def _target_clone(args: argparse.Namespace) -> None:
    if args.expected_sha:
        workspace = TargetWorkspace.clone_at_sha(
            args.source,
            args.destination,
            branch=args.branch,
            expected_sha=args.expected_sha,
        )
    else:
        workspace = TargetWorkspace.clone(
            args.source,
            args.destination,
            branch=args.branch,
        )
    _print_json(
        {
            "workspace": str(workspace.path),
            "branch": workspace.branch,
            "base_sha": workspace.base_sha,
        }
    )


def _target_create_patch(args: argparse.Namespace) -> None:
    workspace = TargetWorkspace.open_existing(
        args.workspace,
        branch=args.branch,
        base_sha=args.base_sha,
    )
    workspace.create_patch(args.output)
    _print_json(
        {
            "workspace": str(workspace.path),
            "base_sha": workspace.base_sha,
            "patch": str(args.output),
            "patch_bytes": args.output.stat().st_size,
        }
    )


def _target_apply_patch(args: argparse.Namespace) -> None:
    workspace = TargetWorkspace.open_existing(
        args.workspace,
        branch=args.branch,
        base_sha=args.base_sha,
    )
    workspace.apply_patch(args.patch)
    _print_json(
        {
            "workspace": str(workspace.path),
            "base_sha": workspace.base_sha,
            "patch": str(args.patch),
        }
    )


def _target_apply_recovered_patch(args: argparse.Namespace) -> None:
    workspace = TargetWorkspace.open_existing(
        args.workspace,
        branch=args.branch,
        base_sha=args.current_base_sha,
    )
    evidence = workspace.apply_recovered_patch(
        args.patch,
        validated_base_sha=args.validated_base_sha,
    )
    payload = evidence.to_dict()
    _write_json(args.output, payload)
    _print_json(payload)


def cmd_phase1_generate(args: argparse.Namespace) -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise GenerationPipelineError("DEEPSEEK_API_KEY is required")
    result = run_generation_pipeline(
        workspace=args.workspace,
        report_dir=args.report_dir,
        task=_load_task(args.task_spec),
        base_sha=args.base_sha,
        executable=args.opencode,
        api_key=api_key,
        image_linter=lambda dockerfile: lint_dockerfile(
            executable=args.hadolint,
            dockerfile=dockerfile,
        ),
    )
    _print_json(
        {
            "status": result.status,
            "qa_fix_rounds": result.qa_fix_rounds,
            "gate_report": dict(result.gate_report),
        }
    )


def cmd_phase1_smoke_generate(args: argparse.Namespace) -> None:
    task = _load_task(args.task_spec)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    smoke = write_smoke_candidate(workspace=args.workspace, task=task)
    gate = validate_generated_target(
        repo=args.workspace,
        task=task,
        base_sha=args.base_sha,
    )
    _write_json(args.report_dir / "smoke-generation.json", smoke)
    _write_json(args.report_dir / "gates.json", gate)
    _print_json({"status": "passed", "mode": "pipeline_smoke"})


def cmd_phase1_native_repair(args: argparse.Namespace) -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise NativeRepairError("DEEPSEEK_API_KEY is required")
    result = validate_native_with_repairs(
        workspace=args.workspace,
        task=_load_task(args.task_spec),
        base_sha=args.base_sha,
        architecture=args.architecture,
        run_id=args.run_id,
        dgoss=args.dgoss,
        goss=args.goss,
        report_path=args.report,
        junit_path=args.junit,
        repair_report_dir=args.repair_report_dir,
        executable=args.opencode,
        api_key=api_key,
    )
    _print_json(
        {
            "status": result.status,
            "repair_attempts": result.repair_attempts,
            "report": dict(result.report),
        }
    )


def cmd_phase1_native_validate(args: argparse.Namespace) -> None:
    report = validate_native_image(
        workspace=args.workspace,
        task=_load_task(args.task_spec),
        architecture=args.architecture,
        run_id=args.run_id,
        dgoss=args.dgoss,
        goss=args.goss,
        report_path=args.report,
        junit_path=args.junit,
    )
    _print_json(report)


def cmd_phase1_native_smoke(args: argparse.Namespace) -> None:
    report = validate_native_smoke(
        workspace=args.workspace,
        task=_load_task(args.task_spec),
        architecture=args.architecture,
        run_id=args.run_id,
        dgoss=args.dgoss,
        goss=args.goss,
        report_path=args.report,
        junit_path=args.junit,
        repair_report_dir=args.repair_report_dir,
    )
    _print_json(report)


def cmd_phase1_native_release(args: argparse.Namespace) -> None:
    _print_json(
        release_run_builders(
            run_id=args.run_id,
            architecture=args.architecture,
            workspace=args.workspace,
        )
    )


def _add_task_commands(commands: argparse._SubParsersAction) -> None:
    task = commands.add_parser("task-spec")
    task.add_argument("--app", required=True)
    task.add_argument("--version", required=True)
    task.add_argument("--os-version", required=True)
    task.add_argument("--domain", required=True)
    task.add_argument("--source-url", required=True)
    task.add_argument("--output", required=True, type=Path)
    task.set_defaults(handler=_task_spec)

    delivery = commands.add_parser("delivery-config")
    delivery.add_argument("--environment", required=True)
    delivery.add_argument("--delivery-mode", required=True)
    delivery.add_argument("--target-repo", required=True)
    delivery.add_argument("--push-repo", required=True)
    delivery.add_argument("--target-branch", required=True)
    delivery.add_argument("--duplicate-pr-guard", default="")
    delivery.add_argument("--output", required=True, type=Path)
    delivery.set_defaults(handler=_delivery_config)


def _add_candidate_commands(commands: argparse._SubParsersAction) -> None:
    create = commands.add_parser("candidate-create")
    create.add_argument("--candidate-dir", required=True, type=Path)
    create.add_argument("--task-spec", required=True, type=Path)
    create.add_argument("--base-sha", required=True)
    create.add_argument("--validated-run-id", required=True)
    create.set_defaults(handler=_candidate_create)

    verify = commands.add_parser("candidate-verify")
    verify.add_argument("--candidate-dir", required=True, type=Path)
    verify.add_argument("--expected-run-id", required=True)
    verify.add_argument("--current-base-sha", required=True)
    verify.set_defaults(handler=_candidate_verify)

    clone = commands.add_parser("target-clone")
    clone.add_argument("--source", required=True)
    clone.add_argument("--destination", required=True, type=Path)
    clone.add_argument("--branch", required=True)
    clone.add_argument("--expected-sha", default="")
    clone.set_defaults(handler=_target_clone)

    create_patch = commands.add_parser("target-create-patch")
    create_patch.add_argument("--workspace", required=True, type=Path)
    create_patch.add_argument("--branch", required=True)
    create_patch.add_argument("--base-sha", required=True)
    create_patch.add_argument("--output", required=True, type=Path)
    create_patch.set_defaults(handler=_target_create_patch)

    apply_patch = commands.add_parser("target-apply-patch")
    apply_patch.add_argument("--workspace", required=True, type=Path)
    apply_patch.add_argument("--branch", required=True)
    apply_patch.add_argument("--base-sha", required=True)
    apply_patch.add_argument("--patch", required=True, type=Path)
    apply_patch.set_defaults(handler=_target_apply_patch)

    recover_patch = commands.add_parser("target-apply-recovered-patch")
    recover_patch.add_argument("--workspace", required=True, type=Path)
    recover_patch.add_argument("--branch", required=True)
    recover_patch.add_argument("--current-base-sha", required=True)
    recover_patch.add_argument("--validated-base-sha", required=True)
    recover_patch.add_argument("--patch", required=True, type=Path)
    recover_patch.add_argument("--output", required=True, type=Path)
    recover_patch.set_defaults(handler=_target_apply_recovered_patch)


def _add_delivery_commands(commands: argparse._SubParsersAction) -> None:
    fork = commands.add_parser(
        "fork-deliver",
        description=(
            "Promote one exact validate_only artifact and create its GitCode "
            "fork PR. Authentication is read from GITCODE_TOKEN."
        ),
    )
    fork.add_argument("--candidate-dir", required=True, type=Path)
    fork.add_argument("--expected-run-id", required=True)
    fork.add_argument("--delivery-run-id", required=True)
    fork.add_argument("--delivery-run-attempt", required=True)
    fork.add_argument("--workspace", required=True, type=Path)
    fork.add_argument(
        "--gitcode-username",
        default="qq_42020325",
        help="GitCode bot username (default: qq_42020325)",
    )
    fork.set_defaults(handler=_fork_deliver)

    issue_probe = commands.add_parser(
        "issue-contract-test",
        description=(
            "Explicit test-only GitCode Issue probe: create, update, comment "
            "and close one [E2E TEST] Issue. Authentication is read from "
            "GITCODE_TOKEN."
        ),
    )
    issue_probe.add_argument("--task-spec", required=True, type=Path)
    issue_probe.add_argument("--github-run-id", required=True)
    issue_probe.add_argument("--failure-stage", required=True)
    issue_probe.set_defaults(handler=_issue_contract_test)


def _add_generation_commands(commands: argparse._SubParsersAction) -> None:
    generate = commands.add_parser(
        "phase1-generate",
        help="Generate and review one TaskSpec",
    )
    generate.add_argument("--workspace", required=True, type=Path)
    generate.add_argument("--task-spec", required=True, type=Path)
    generate.add_argument("--base-sha", required=True)
    generate.add_argument("--report-dir", required=True, type=Path)
    generate.add_argument("--opencode", required=True, type=Path)
    generate.add_argument("--hadolint", required=True, type=Path)
    generate.set_defaults(handler=cmd_phase1_generate)

    smoke = commands.add_parser(
        "phase1-smoke-generate",
        help="Create the deterministic zero-AI candidate",
    )
    smoke.add_argument("--workspace", required=True, type=Path)
    smoke.add_argument("--task-spec", required=True, type=Path)
    smoke.add_argument("--base-sha", required=True)
    smoke.add_argument("--report-dir", required=True, type=Path)
    smoke.set_defaults(handler=cmd_phase1_smoke_generate)


def _add_native_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-spec", required=True, type=Path)
    parser.add_argument(
        "--architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dgoss", required=True, type=Path)
    parser.add_argument("--goss", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--junit", required=True, type=Path)


def _add_native_commands(commands: argparse._SubParsersAction) -> None:
    repair = commands.add_parser(
        "phase1-native-repair",
        help="Run native validation with the bounded Fixer loop",
    )
    _add_native_arguments(repair)
    repair.add_argument("--base-sha", required=True)
    repair.add_argument("--repair-report-dir", required=True, type=Path)
    repair.add_argument("--opencode", required=True, type=Path)
    repair.set_defaults(handler=cmd_phase1_native_repair)

    validate = commands.add_parser(
        "phase1-native-validate",
        help="Run deterministic native validation",
    )
    _add_native_arguments(validate)
    validate.set_defaults(handler=cmd_phase1_native_validate)

    smoke = commands.add_parser(
        "phase1-native-smoke",
        help="Exercise native Docker and dgoss plumbing without AI",
    )
    _add_native_arguments(smoke)
    smoke.add_argument("--repair-report-dir", required=True, type=Path)
    smoke.set_defaults(handler=cmd_phase1_native_smoke)

    release = commands.add_parser(
        "phase1-native-release",
        help="Release the builders this run owns on one architecture",
    )
    release.add_argument("--workspace", required=True, type=Path)
    release.add_argument(
        "--architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    release.add_argument("--run-id", required=True)
    release.set_defaults(handler=cmd_phase1_native_release)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flow",
        description="Phase-one openEuler new-image workflow",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_task_commands(commands)
    _add_candidate_commands(commands)
    _add_delivery_commands(commands)
    _add_generation_commands(commands)
    _add_native_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (
        AgentRuntimeError,
        CandidateBundleError,
        DeliveryConfigError,
        ForkPRPipelineError,
        GenerationPipelineError,
        GitCodeClientError,
        GitDeliveryError,
        GitWorkspaceError,
        IssueLifecycleError,
        NativeRepairError,
        NativeValidationError,
        PRDeliveryError,
        TargetContractError,
        TaskSpecError,
        json.JSONDecodeError,
        OSError,
    ) as error:
        print(f"flow: error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from scripts.lib.evidence_resolver import freeze_creator_evidence
from scripts.lib.generation_pipeline import (
    GenerationPipelineError,
    lint_dockerfile,
    run_generation_pipeline,
    write_smoke_candidate,
)
from scripts.lib.git_workspace import (
    GitWorkspaceError,
    TargetWorkspace,
    TransientGitWorkspaceError,
)
from scripts.lib.gitcode_client import (
    DeliveryConfig,
    DeliveryConfigError,
    GitCodeClient,
    GitCodeClientError,
)
from scripts.lib.issue_lifecycle import (
    ClaimedIssue,
    IssueLifecycleError,
    claim_new_image_issue,
    dispatch_github_workflow,
    finalize_new_image_issue,
    run_controlled_issue_probe,
)
from scripts.lib.native_repair import (
    NativeRepairError,
    decide_round,
    validate_native_with_repairs,
)
from scripts.lib.native_validation import (
    NativeValidationError,
    release_run_builders,
    validate_native_image,
    validate_native_smoke,
    write_infrastructure_failure_evidence,
)
from scripts.lib.oe_upgrade_contract import (
    UpgradeContractError,
    UpgradeRequest,
    parse_upgrade_issue,
)
from scripts.lib.oe_upgrade_candidate import (
    CandidateDerivationError,
    prepare_upgrade_candidate,
)
from scripts.lib.oe_upgrade_planner import UpgradePlannerError, plan_upgrade
from scripts.lib.oe_upgrade_sanitizer import (
    SanitizationError,
    create_checkpoint,
    load_checkpoint,
    sanitize_agent_changes,
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
    validate_add_version_target,
    validate_generated_target,
    validate_new_image_target_base,
)
from scripts.lib.upstream_format_check import run_upstream_format_check
from scripts.harness.parse_issue import parse_issue_request


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "REDACTED") if secret else value


def _print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _load_task(path: Path) -> TaskSpec:
    return TaskSpec.from_json(path.read_text())


def _oe_upgrade_request(args: argparse.Namespace) -> None:
    options = parse_upgrade_issue(
        args.issue_number,
        args.title,
        args.body_file.read_text(),
        mode=args.mode,
    )
    request = UpgradeRequest.create(
        tracking_issue_number=options.tracking_issue_number,
        oe_version=options.oe_version,
        scope=options.scope,
        base_sha=args.base_sha,
    )
    _write_json(args.output, request.to_dict())
    _print_json(
        {
            "mode": options.mode,
            "output": str(args.output),
            "request_key": request.request_key,
        }
    )


def _oe_upgrade_plan(args: argparse.Namespace) -> None:
    request = UpgradeRequest.from_json(args.request.read_text())
    plan = plan_upgrade(args.workspace, request)
    _write_json(args.output, plan.to_dict())
    if args.summary_output is not None:
        summary = plan.summary
        args.summary_output.write_text(
            "## openEuler upgrade plan\n\n"
            f"- MDU: {summary['mdu_count']}\n"
            f"- Tasks: {summary['task_count']}\n"
            f"- Planning failures: {summary['planning_failed_count']}\n"
            f"- Warnings: {summary['warning_count']}\n"
        )
    _print_json({"output": str(args.output), **plan.summary})


def _oe_upgrade_prepare(args: argparse.Namespace) -> None:
    task = _load_task(args.task_spec)
    report = prepare_upgrade_candidate(
        workspace=args.workspace,
        task=task,
        base_sha=args.base_sha,
        report_dir=args.report_dir,
    )
    gates = validate_add_version_target(
        repo=args.workspace,
        task=task,
        base_sha=args.base_sha,
    )
    _write_json(args.report_dir / "add-version-gates.json", gates)
    _print_json(
        {
            "status": gates["status"],
            "task_key": report.task_key,
            "target_directory": report.target_directory,
        }
    )


def _oe_upgrade_checkpoint(args: argparse.Namespace) -> None:
    checkpoint = create_checkpoint(
        workspace=args.workspace,
        base_sha=args.base_sha,
        task=_load_task(args.task_spec),
        destination=args.destination,
        round_number=args.round,
        agent_role=args.agent_role,
    )
    _print_json(
        {
            "checkpoint_id": checkpoint.checkpoint_id,
            "destination": str(checkpoint.root),
        }
    )


def _oe_upgrade_sanitize(args: argparse.Namespace) -> None:
    report = sanitize_agent_changes(
        workspace=args.workspace,
        base_sha=args.base_sha,
        task=_load_task(args.task_spec),
        checkpoint=load_checkpoint(args.checkpoint),
        report_path=args.report,
    )
    _print_json(
        {
            "clean": report.clean,
            "checkpoint_id": report.checkpoint_id,
            "report": str(args.report),
        }
    )


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
    # TODO(production-delivery): this is the single reason no entry can deliver
    # in production. The config below is hard-coded to the test pair, and
    # deliver_validated_candidate refuses anything else, so scenario_one is a
    # production orchestration entry with test-mode delivery. The production
    # pair (environment=production, delivery_mode=direct_branch_pr, defined in
    # scripts/lib/gitcode_client.DeliveryConfig) is unreachable from any
    # workflow, and test fork_pr deliberately skips the duplicate PR guard.
    # Rollout order: lift these five values into CLI arguments, re-enable the
    # duplicate PR guard, then relax the mode check in
    # pr_delivery.deliver_validated_candidate.
    # Left as-is on purpose while phase-one testing continues.
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
        source_issue_number=args.source_issue_number,
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
        github_run_url=args.github_run_url,
        failure_stage=args.failure_stage,
    )
    _print_json(
        {
            "number": resource.number,
            "state": "closed",
            "url": resource.url,
        }
    )


def _issue_watch(args: argparse.Namespace) -> None:
    gitcode_token = os.environ.get("GITCODE_TOKEN", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not gitcode_token:
        raise IssueLifecycleError("GITCODE_TOKEN is required")
    if not github_token:
        raise IssueLifecycleError("GITHUB_TOKEN is required")

    def dispatch(inputs: dict[str, str]) -> None:
        dispatch_github_workflow(
            github_token=github_token,
            github_repository=args.github_repository,
            workflow=args.workflow,
            ref=args.github_ref,
            inputs=inputs,
        )

    def parse_task(issue: dict[str, object]) -> TaskSpec:
        fields = parse_issue_request(
            str(issue.get("title", "")),
            str(issue.get("body", "") or ""),
        )
        return TaskSpec.from_workflow_dispatch(
            {
                "app": fields["package_name"],
                "version": fields["app_version"],
                "os_version": fields["os_version"],
                "domain": fields["domain"],
                "source_url": fields["source_repo_url"],
            }
        )

    if args.issue_number is None and args.max_issues < 1:
        raise IssueLifecycleError("--max-issues must be a positive integer")
    limit = args.max_issues if args.issue_number is None else 1
    claimed: list[ClaimedIssue] = []
    for _ in range(limit):
        # Each pass re-lists open issues so an accepted one is skipped and the
        # next new request is claimed; a failure stops the round and leaves
        # the backlog to the next poll.
        result = claim_new_image_issue(
            client=GitCodeClient(token=gitcode_token),
            target_repo=args.target_repo,
            issue_number=args.issue_number,
            dispatch=dispatch,
            parse_task=parse_task,
        )
        if result is None:
            break
        claimed.append(result)
    _print_json(
        {
            "dispatched": bool(claimed),
            "issues": [
                {"issue_number": item.number, "issue_url": item.url}
                for item in claimed
            ],
        }
    )


def _issue_finalize(args: argparse.Namespace) -> None:
    token = os.environ.get("GITCODE_TOKEN", "")
    if not token:
        raise IssueLifecycleError("GITCODE_TOKEN is required")
    finalize_args = {
        "client": GitCodeClient(token=token),
        "target_repo": args.target_repo,
        "issue_number": args.issue_number,
        "outcome": args.outcome,
        "run_url": args.run_url,
        "pr_url": args.pr_url,
        "failure_summary": args.failure_summary,
    }
    failure_evidence_dir = getattr(args, "failure_evidence_dir", None)
    if failure_evidence_dir is not None:
        finalize_args["failure_evidence_dir"] = failure_evidence_dir
    finalize_new_image_issue(**finalize_args)
    _print_json(
        {
            "issue_number": args.issue_number,
            "outcome": args.outcome,
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


def _target_new_image_precheck(args: argparse.Namespace) -> None:
    try:
        report = validate_new_image_target_base(
            repo=args.workspace,
            task=_load_task(args.task_spec),
            base_sha=args.base_sha,
        )
    except TargetContractError as error:
        if args.report_dir is not None:
            args.report_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                args.report_dir / "generation-failure.json",
                {
                    "status": "failed",
                    "stage": "scenario_one_precheck",
                    "role": "workflow",
                    "error": str(error),
                },
            )
        raise
    _print_json(report)


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
        evidence_resolver=freeze_creator_evidence,
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
        report_path=args.report,
        junit_path=args.junit,
        format_validator=run_upstream_format_check,
    )
    _print_json(report)


def cmd_phase1_infra_evidence(args: argparse.Namespace) -> None:
    report = write_infrastructure_failure_evidence(
        task=_load_task(args.task_spec),
        architecture=args.architecture,
        failed_stage=args.failed_stage,
        failure=args.failure_log.read_text(errors="replace").strip(),
        report_path=args.report,
        junit_path=args.junit,
        attempts=args.attempts,
    )
    _print_json(report)


def cmd_phase1_native_smoke(args: argparse.Namespace) -> None:
    report = validate_native_smoke(
        workspace=args.workspace,
        task=_load_task(args.task_spec),
        architecture=args.architecture,
        run_id=args.run_id,
        report_path=args.report,
        junit_path=args.junit,
        repair_report_dir=args.repair_report_dir,
        format_validator=run_upstream_format_check,
    )
    _print_json(report)


def cmd_phase1_decide(args: argparse.Namespace) -> None:
    args.report_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    try:
        task = _load_task(args.task_spec)
        report_paths = {
            "x86_64": args.x86_report,
            "aarch64": args.arm_report,
        }
        required_architectures = (
            task.architectures
            if task.schema_version == 2
            else ("x86_64", "aarch64")
        )
        reports = {
            architecture: json.loads(report_paths[architecture].read_text())
            for architecture in required_architectures
            if report_paths[architecture] is not None
        }
        evidence_roots = {
            architecture: diagnostics.resolve()
            for architecture, report_path in report_paths.items()
            if report_path is not None
            if (diagnostics := report_path.resolve().parent / "diagnostics").is_dir()
        }
        decision = decide_round(
            workspace=args.workspace,
            task=task,
            base_sha=args.base_sha,
            round_number=args.round,
            max_rounds=args.max_rounds,
            reports=reports,
            report_dir=args.report_dir,
            executable=args.opencode,
            api_key=api_key,
            evidence_roots=evidence_roots,
        )
    except Exception as error:
        error_message = _redact_secret(str(error), api_key)
        try:
            _write_json(
                args.report_dir / f"decision-error-{args.round}.json",
                {
                    "status": "error",
                    "round": args.round,
                    "error_type": type(error).__name__,
                    "error": error_message,
                },
            )
        except Exception as evidence_error:
            print(
                "flow: warning: failed to write decision evidence: "
                f"{_redact_secret(str(evidence_error), api_key)}",
                file=sys.stderr,
            )
        raise
    summary = {
        "converged": decision.converged,
        "round": decision.round_number,
        "repair_attempts": decision.repair_attempts,
        "validated_patch_sha256": decision.validated_patch_sha256,
        "terminal_status": decision.terminal_status,
    }
    _write_json(
        args.report_dir / f"round-decision-{args.round}.json",
        summary,
    )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(
                f"converged={'true' if decision.converged else 'false'}\n"
            )
            stream.write(f"terminal_status={decision.terminal_status}\n")
    _print_json(summary)


def cmd_phase1_native_release(args: argparse.Namespace) -> None:
    _print_json(
        release_run_builders(
            run_id=args.run_id,
            architecture=args.architecture,
            workspace=args.workspace,
            task_key=args.task_key,
        )
    )


def _add_task_commands(commands: argparse._SubParsersAction) -> None:
    request = commands.add_parser(
        "oe-upgrade-request",
        help="Parse and pin one openEuler upgrade request",
    )
    request.add_argument("--issue-number", required=True, type=int)
    request.add_argument("--title", required=True)
    request.add_argument("--body-file", required=True, type=Path)
    request.add_argument("--mode", required=True, choices=("plan", "deliver"))
    request.add_argument("--base-sha", required=True)
    request.add_argument("--output", required=True, type=Path)
    request.set_defaults(handler=_oe_upgrade_request)

    plan = commands.add_parser(
        "oe-upgrade-plan",
        help="Generate a deterministic plan from a pinned request",
    )
    plan.add_argument("--workspace", required=True, type=Path)
    plan.add_argument("--request", required=True, type=Path)
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--summary-output", type=Path)
    plan.set_defaults(handler=_oe_upgrade_plan)

    prepare = commands.add_parser(
        "oe-upgrade-prepare",
        help="Derive and gate one deterministic add-version candidate",
    )
    prepare.add_argument("--workspace", required=True, type=Path)
    prepare.add_argument("--task-spec", required=True, type=Path)
    prepare.add_argument("--base-sha", required=True)
    prepare.add_argument("--report-dir", required=True, type=Path)
    prepare.set_defaults(handler=_oe_upgrade_prepare)

    checkpoint = commands.add_parser(
        "oe-upgrade-checkpoint",
        help="Snapshot a gated candidate before an Agent modifies it",
    )
    checkpoint.add_argument("--workspace", required=True, type=Path)
    checkpoint.add_argument("--task-spec", required=True, type=Path)
    checkpoint.add_argument("--base-sha", required=True)
    checkpoint.add_argument("--destination", required=True, type=Path)
    checkpoint.add_argument("--round", required=True, type=int)
    checkpoint.add_argument(
        "--agent-role",
        required=True,
        choices=("code-fixer", "testcase-creator"),
    )
    checkpoint.set_defaults(handler=_oe_upgrade_checkpoint)

    sanitize = commands.add_parser(
        "oe-upgrade-sanitize",
        help="Restore changes outside an Agent role whitelist",
    )
    sanitize.add_argument("--workspace", required=True, type=Path)
    sanitize.add_argument("--task-spec", required=True, type=Path)
    sanitize.add_argument("--base-sha", required=True)
    sanitize.add_argument("--checkpoint", required=True, type=Path)
    sanitize.add_argument("--report", required=True, type=Path)
    sanitize.set_defaults(handler=_oe_upgrade_sanitize)

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
    verify.set_defaults(handler=_candidate_verify)

    clone = commands.add_parser("target-clone")
    clone.add_argument("--source", required=True)
    clone.add_argument("--destination", required=True, type=Path)
    clone.add_argument("--branch", required=True)
    clone.add_argument("--expected-sha", default="")
    clone.set_defaults(handler=_target_clone)

    precheck = commands.add_parser("target-new-image-precheck")
    precheck.add_argument("--workspace", required=True, type=Path)
    precheck.add_argument("--task-spec", required=True, type=Path)
    precheck.add_argument("--base-sha", required=True)
    precheck.add_argument("--report-dir", type=Path)
    precheck.set_defaults(handler=_target_new_image_precheck)

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
    fork.add_argument("--source-issue-number", type=int)
    fork.add_argument(
        "--gitcode-username",
        required=True,
        help="GitCode bot username",
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
    issue_probe.add_argument("--github-run-url", required=True)
    issue_probe.add_argument("--failure-stage", required=True)
    issue_probe.set_defaults(handler=_issue_contract_test)

    issue_watch = commands.add_parser(
        "issue-watch",
        description=(
            "Claim new GitCode image requests and dispatch scenario_one; "
            "without --issue-number, scans for new requests up to --max-issues."
        ),
    )
    issue_watch.add_argument("--target-repo", required=True)
    issue_watch.add_argument("--issue-number", type=int)
    issue_watch.add_argument(
        "--max-issues",
        type=int,
        default=1,
        help=(
            "Maximum new Issues claimed per scan run; ignored when "
            "--issue-number is set (default: 1)"
        ),
    )
    issue_watch.add_argument("--github-repository", required=True)
    issue_watch.add_argument("--github-ref", required=True)
    issue_watch.add_argument("--workflow", default="create_new_images.yml")
    issue_watch.set_defaults(handler=_issue_watch)

    issue_finalize = commands.add_parser(
        "issue-finalize",
        description="Write a scenario_one result back to its source Issue.",
    )
    issue_finalize.add_argument("--target-repo", required=True)
    issue_finalize.add_argument("--issue-number", required=True, type=int)
    issue_finalize.add_argument(
        "--outcome",
        required=True,
        choices=("success", "failure", "needs-human-review"),
    )
    issue_finalize.add_argument("--run-url", required=True)
    issue_finalize.add_argument("--pr-url", default="")
    issue_finalize.add_argument("--failure-summary", default="")
    issue_finalize.add_argument("--failure-evidence-dir", type=Path)
    issue_finalize.set_defaults(handler=_issue_finalize)


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

    infra = commands.add_parser(
        "phase1-infra-evidence",
        help="Record a retry-exhausted pre-validation infrastructure failure",
    )
    infra.add_argument("--task-spec", required=True, type=Path)
    infra.add_argument(
        "--architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    infra.add_argument(
        "--failed-stage",
        required=True,
        choices=("target_clone",),
    )
    infra.add_argument("--failure-log", required=True, type=Path)
    infra.add_argument("--report", required=True, type=Path)
    infra.add_argument("--junit", required=True, type=Path)
    infra.add_argument("--attempts", required=True, type=int)
    infra.set_defaults(handler=cmd_phase1_infra_evidence)

    smoke = commands.add_parser(
        "phase1-native-smoke",
        help="Exercise native Docker validation plumbing without AI",
    )
    _add_native_arguments(smoke)
    smoke.add_argument("--repair-report-dir", required=True, type=Path)
    smoke.set_defaults(handler=cmd_phase1_native_smoke)

    decide = commands.add_parser(
        "phase1-decide",
        help="Converge one parallel round, or repair once for the next",
    )
    decide.add_argument("--workspace", required=True, type=Path)
    decide.add_argument("--task-spec", required=True, type=Path)
    decide.add_argument("--base-sha", required=True)
    decide.add_argument("--round", required=True, type=int)
    decide.add_argument("--max-rounds", required=True, type=int)
    decide.add_argument("--x86-report", type=Path)
    decide.add_argument("--arm-report", type=Path)
    decide.add_argument("--report-dir", required=True, type=Path)
    decide.add_argument("--opencode", required=True, type=Path)
    decide.add_argument("--github-output", type=Path)
    decide.set_defaults(handler=cmd_phase1_decide)

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
    release.add_argument("--task-key", default="")
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
    except TransientGitWorkspaceError as error:
        print(f"flow: error: {error}", file=sys.stderr)
        return 75
    except (
        AgentRuntimeError,
        CandidateBundleError,
        CandidateDerivationError,
        DeliveryConfigError,
        ForkPRPipelineError,
        GenerationPipelineError,
        GitCodeClientError,
        GitDeliveryError,
        GitWorkspaceError,
        IssueLifecycleError,
        NativeRepairError,
        NativeValidationError,
        UpgradeContractError,
        UpgradePlannerError,
        PRDeliveryError,
        SanitizationError,
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

"""Secret-safe OpenCode process boundary and structured result parser."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from scripts.lib.progress import log, run_streaming


class AgentRuntimeError(RuntimeError):
    """Raised when OpenCode fails or violates its output contract."""


class AgentTimeoutError(AgentRuntimeError):
    """Raised when an Agent overran its budget without completing normally."""

    def __init__(self, *, role: str, elapsed: float) -> None:
        super().__init__(f"OpenCode {role} timed out after {elapsed:.1f}s")
        self.role = role
        self.elapsed = elapsed


MODEL = "deepseek/deepseek-v4-flash"
_AGENT_HEARTBEAT_SECONDS = 60.0
# Research downloads are the one Agent action that can quietly consume a whole
# budget, so cap the scratch directory instead of trusting the prompt alone.
_SCRATCH_LIMIT_MB = 3000
_MESSAGE_DETAIL_LIMIT = 4000
_ACTION_DETAIL_LIMIT = 240
_VISIBLE_ACTION_TOOLS = {
    "bash",
    "edit",
    "invalid",
    "task",
    "webfetch",
    "write",
}
_WRITE_ROLES = {"image_creator", "testcase_creator", "fixer"}
_READ_ONLY_ROLES = {"image_qa", "testcase_qa"}
_ROLES = _WRITE_ROLES | _READ_ONLY_ROLES
SCRATCH_DIR = ".oe-scratch"
# The two event types emit() counts toward the ACTIVITY summary.
_ACTIVITY_EVENTS = {"text", "tool_use"}
AgentRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], int],
    subprocess.CompletedProcess,
]


@dataclass(frozen=True)
class AgentResult:
    role: str
    payload: dict[str, object]


def _default_runner(
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess:
    process_env = os.environ.copy()
    process_env.update(env)
    role = env.get("OE_AGENT_ROLE", "unknown")
    secret = env.get("DEEPSEEK_API_KEY", "")
    scratch = Path(env.get("OE_AGENT_SCRATCH", cwd / SCRATCH_DIR))
    started = time.monotonic()
    last_output = [started]
    last_action = ["none"]
    message_count = [0]
    action_counts: dict[str, int] = {}
    scratch_mb = [0]
    completed = threading.Event()

    def safe_detail(value: object, *, limit: int) -> str:
        if isinstance(value, str):
            detail = value
        else:
            detail = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if secret:
            detail = detail.replace(secret, "REDACTED")
        return " ".join(detail.split())[:limit]

    def safe_path(value: object) -> str:
        detail = safe_detail(value, limit=_ACTION_DETAIL_LIMIT)
        try:
            return str(Path(detail).resolve().relative_to(cwd.resolve()))
        except ValueError:
            return detail

    def tool_summary(tool: str, state: object) -> str:
        if not isinstance(state, dict):
            return f"tool={tool}"
        tool_input = state.get("input", {})
        metadata = state.get("metadata", {})
        fields = [f"tool={tool}"]
        if isinstance(tool_input, dict):
            file_path = tool_input.get("filePath") or tool_input.get("path")
            if file_path:
                fields.append(f"path={safe_path(file_path)}")
            elif tool == "bash" and tool_input.get("command"):
                command = safe_detail(
                    tool_input["command"],
                    limit=_ACTION_DETAIL_LIMIT,
                )
                fields.append(
                    f"command={json.dumps(command, ensure_ascii=False)}"
                )
            elif tool in {"grep", "glob"} and tool_input.get("pattern"):
                pattern = safe_detail(
                    tool_input["pattern"],
                    limit=_ACTION_DETAIL_LIMIT,
                )
                fields.append(
                    f"pattern={json.dumps(pattern, ensure_ascii=False)}"
                )
            elif tool == "webfetch" and tool_input.get("url"):
                fields.append(
                    "url="
                    + safe_detail(
                        tool_input["url"],
                        limit=_ACTION_DETAIL_LIMIT,
                    )
                )
            elif tool == "task" and tool_input.get("description"):
                description = safe_detail(
                    tool_input["description"],
                    limit=_ACTION_DETAIL_LIMIT,
                )
                fields.append(
                    f"description={json.dumps(description, ensure_ascii=False)}"
                )
            elif tool == "todowrite":
                todos = tool_input.get("todos", [])
                count = len(todos) if isinstance(todos, list) else 0
                fields.append(f"items={count}")
        status = state.get("status")
        if status:
            fields.append(f"status={status}")
        if isinstance(metadata, dict) and metadata.get("exit") is not None:
            fields.append(f"exit={metadata['exit']}")
        return " ".join(fields)

    def emit(line: str) -> None:
        safe_line = line.replace(secret, "REDACTED") if secret else line
        try:
            event = json.loads(safe_line)
        except json.JSONDecodeError:
            detail = safe_line.strip()
            if detail:
                last_output[0] = time.monotonic()
                log(
                    f"agent:{role}",
                    safe_detail(detail, limit=_MESSAGE_DETAIL_LIMIT),
                )
            return
        event_type = str(event.get("type", "unknown"))
        part = event.get("part")
        if event_type == "text" and isinstance(part, dict):
            last_output[0] = time.monotonic()
            message_count[0] += 1
            message = safe_detail(
                part.get("text", ""),
                limit=_MESSAGE_DETAIL_LIMIT,
            )
            log(f"agent:{role}", f"MESSAGE {message}")
        elif event_type == "tool_use" and isinstance(part, dict):
            tool = str(part.get("tool", "unknown"))
            state = part.get("state", part)
            summary = tool_summary(tool, state)
            last_output[0] = time.monotonic()
            last_action[0] = summary
            action_counts[tool] = action_counts.get(tool, 0) + 1
            status = state.get("status") if isinstance(state, dict) else None
            metadata = state.get("metadata", {}) if isinstance(state, dict) else {}
            failed_command = (
                isinstance(metadata, dict)
                and metadata.get("exit") not in {None, 0}
            )
            if (
                tool in _VISIBLE_ACTION_TOOLS
                or status in {"error", "failed"}
                or failed_command
            ):
                log(f"agent:{role}", f"ACTION {summary}")

    def heartbeat() -> None:
        while not completed.wait(_AGENT_HEARTBEAT_SECONDS):
            now = time.monotonic()
            silence = now - last_output[0]
            if silence < _AGENT_HEARTBEAT_SECONDS:
                continue
            log(
                f"agent:{role}",
                f"WAIT elapsed={now - started:.1f}s "
                f"silence={silence:.1f}s "
                f"last_action={last_action[0]} "
                f"timeout={float(timeout):g}s "
                f"scratch={scratch_mb[0]}MB",
            )

    def scratch_watchdog() -> str | None:
        scratch_mb[0] = _directory_size_mb(scratch)
        if scratch_mb[0] <= _SCRATCH_LIMIT_MB:
            return None
        log(
            f"agent:{role}",
            f"ABORT reason=scratch_over_limit "
            f"scratch={scratch_mb[0]}MB limit={_SCRATCH_LIMIT_MB}MB",
        )
        return "scratch_over_limit"

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"agent-heartbeat-{role}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        result = run_streaming(
            command,
            cwd=cwd,
            env=process_env,
            timeout=timeout,
            emit=emit,
            watchdog=scratch_watchdog,
        )
    finally:
        completed.set()
        heartbeat_thread.join(timeout=1)
    tools = ",".join(
        f"{tool}:{count}" for tool, count in sorted(action_counts.items())
    )
    scratch_mb[0] = max(scratch_mb[0], _directory_size_mb(scratch))
    log(
        f"agent:{role}",
        f"ACTIVITY messages={message_count[0]} "
        f"actions={sum(action_counts.values())} tools={tools or 'none'} "
        f"scratch={scratch_mb[0]}MB",
    )
    return result


def _directory_size_mb(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total // (1024 * 1024)


def prepare_scratch(workspace: Path) -> Path:
    """Give the Agent a writable place that never reaches the candidate.

    OpenCode denies external_directory, so the only writable location was the
    target repo itself. Excluding the scratch path through .git/info/exclude
    keeps it out of `git add --intent-to-add`, and therefore out of both the
    deterministic gate and the candidate patch, without touching any tracked
    ignore file in the target repository.
    """
    scratch = workspace / SCRATCH_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    exclude = workspace / ".git" / "info" / "exclude"
    if not (workspace / ".git").is_dir():
        return scratch
    entry = f"/{SCRATCH_DIR}/"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.is_file() else ""
    if entry not in existing.splitlines():
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(f"{existing}{separator}{entry}\n")
    return scratch


def _scan_json(text: str) -> Iterator[object]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        yield value


def _saw_any_activity(stdout: str) -> bool:
    """Whether the Agent did anything, using the same events ACTIVITY counts.

    Lifecycle events such as step_start say the process started, not that the
    model responded, so counting them would let a provider that hangs right
    after one suppress the retry. Restricting this to text and tool calls also
    makes retrying a write role safe: without a tool call there is no
    half-finished edit in the workspace to repeat.
    """
    return any(
        isinstance(event, dict) and event.get("type") in _ACTIVITY_EVENTS
        for event in _scan_json(stdout)
    )


def _parse_contract(
    stdout: str,
    required_keys: tuple[str, ...],
) -> dict[str, object]:
    matches = []
    for event in _scan_json(stdout):
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            continue
        for candidate in _scan_json(part["text"]):
            if (
                isinstance(candidate, dict)
                and all(key in candidate for key in required_keys)
            ):
                matches.append(candidate)
    if not matches:
        raise AgentRuntimeError(
            "OpenCode completed without the required JSON contract: "
            + ", ".join(required_keys)
        )
    return matches[-1]


_COMMAND_EVIDENCE_FIELDS = ("command", "semantics")
_IDENTITY_MODES = {"dynamic", "fixed", "reuse_existing"}


def qa_requests_repair(payload: Mapping[str, object]) -> bool:
    """Use only the candidate-issue channel, never evidence reviews, for repair."""
    issues = payload.get("issues")
    return (
        payload.get("status") == "needs_fix"
        and isinstance(issues, list)
        and bool(issues)
    )


def _validate_command_evidence(evidence: object) -> None:
    """Require a semantics claim and Harness evidence reference per command."""
    if not isinstance(evidence, list) or not evidence:
        raise AgentRuntimeError(
            "Agent contract command_evidence must be a non-empty list"
        )
    for entry in evidence:
        if not isinstance(entry, Mapping):
            raise AgentRuntimeError(
                "Agent contract command_evidence entries must be objects"
            )
        for field in _COMMAND_EVIDENCE_FIELDS:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AgentRuntimeError(
                    "Agent contract command_evidence entries must set a "
                    f"non-empty {field}"
                )


def _validate_identity_decision(decision: object) -> None:
    """Validate the application-neutral runtime identity contract."""
    if not isinstance(decision, Mapping):
        raise AgentRuntimeError("Agent contract identity_decision must be an object")
    mode = decision.get("mode")
    if mode not in _IDENTITY_MODES:
        raise AgentRuntimeError(
            "Agent contract identity_decision mode must be dynamic, fixed, "
            "or reuse_existing"
        )
    for field in ("user", "group"):
        value = decision.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AgentRuntimeError(
                f"Agent contract identity_decision {field} must be non-empty"
            )
    uid = decision.get("uid")
    gid = decision.get("gid")
    if mode == "fixed":
        if (
            isinstance(uid, bool)
            or isinstance(gid, bool)
            or not isinstance(uid, int)
            or not isinstance(gid, int)
            or uid <= 0
            or gid <= 0
        ):
            raise AgentRuntimeError(
                "Agent contract fixed identity must set positive numeric uid and gid"
            )
    elif uid is not None or gid is not None:
        raise AgentRuntimeError(
            f"Agent contract {mode} identity must leave uid and gid null"
        )


def _validate_contract(
    payload: Mapping[str, object],
    required_keys: tuple[str, ...],
) -> None:
    if "success" in payload and not isinstance(payload["success"], bool):
        raise AgentRuntimeError("Agent contract success must be a boolean")
    for key in ("files_created", "changes", "issues", "assumptions"):
        if key in payload and not isinstance(payload[key], list):
            raise AgentRuntimeError(f"Agent contract {key} must be a list")
    if "command_evidence" in required_keys:
        _validate_command_evidence(payload["command_evidence"])
    if "identity_decision" in required_keys:
        _validate_identity_decision(payload["identity_decision"])
    if "status" in required_keys:
        status = payload["status"]
        if not isinstance(status, str) or status not in {
            "approved",
            "needs_fix",
        }:
            raise AgentRuntimeError(
                "Agent contract status must be approved or needs_fix"
            )
    if "summary" in payload and not isinstance(payload["summary"], str):
        raise AgentRuntimeError("Agent contract summary must be a string")
    if "coverage_score" in required_keys:
        score = payload["coverage_score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
        ):
            raise AgentRuntimeError(
                "Agent contract coverage_score must be between 0 and 1"
            )


def validate_agent_payload(
    payload: Mapping[str, object],
    *,
    required_keys: tuple[str, ...],
) -> None:
    """Apply the shared structured contract outside the OpenCode runner."""
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise AgentRuntimeError(
            "Agent payload is missing required keys: " + ", ".join(missing)
        )
    _validate_contract(payload, required_keys)


def _permission_config(role: str) -> str:
    writable = role in _WRITE_ROLES
    permission = {
        "read": "allow" if writable else "deny",
        "edit": "allow" if writable else "deny",
        "bash": "allow" if writable else "deny",
        "webfetch": "allow" if writable else "deny",
        "task": "deny",
        "external_directory": "deny",
    }
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "permission": permission,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def run_agent(
    *,
    executable: Path,
    role: str,
    prompt: str,
    workspace: Path,
    api_key: str,
    required_keys: tuple[str, ...],
    runner: AgentRunner = _default_runner,
    timeout: int = 1800,
) -> AgentResult:
    executable = Path(executable)
    workspace = Path(workspace)
    if role not in _ROLES:
        raise AgentRuntimeError(f"unsupported Agent role: {role}")
    if not executable.is_absolute() or not executable.is_file():
        raise AgentRuntimeError("OpenCode executable must be an absolute file path")
    if not os.access(executable, os.X_OK):
        raise AgentRuntimeError("OpenCode executable is not executable")
    if not workspace.is_dir():
        raise AgentRuntimeError("Agent workspace does not exist")
    if not api_key:
        raise AgentRuntimeError("DEEPSEEK_API_KEY is required")
    if not prompt.strip():
        raise AgentRuntimeError("Agent prompt is required")
    if not required_keys:
        raise AgentRuntimeError("Agent JSON contract must require at least one key")

    command = [
        str(executable),
        "run",
        "--model",
        MODEL,
        "--format",
        "json",
        "--auto",
        "--dir",
        str(workspace),
        "--",
        prompt,
    ]
    env = {
        "DEEPSEEK_API_KEY": api_key,
        "OE_AGENT_ROLE": role,
        "OE_AGENT_SCRATCH": str(prepare_scratch(workspace)),
        "OPENCODE_CONFIG_CONTENT": _permission_config(role),
    }
    started = time.monotonic()
    log(
        f"agent:{role}",
        f"START model={MODEL} timeout={float(timeout):g}s "
        f"prompt_chars={len(prompt)} workspace={workspace}",
    )
    result = runner(command, workspace, env, timeout)
    if result.returncode == 124 and not _saw_any_activity(
        str(result.stdout or "")
    ):
        # Nothing was ever attempted, so the provider hung rather than the
        # Agent overrunning its boundary. Failing here made the run repay the
        # Creator calls that had already succeeded.
        log(
            f"agent:{role}",
            f"RETRY reason=no_output elapsed={time.monotonic() - started:.1f}s",
        )
        started = time.monotonic()
        result = runner(command, workspace, env, timeout)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        if result.returncode == 124:
            log(f"agent:{role}", f"TIMEOUT elapsed={elapsed:.1f}s")
            raise AgentTimeoutError(role=role, elapsed=elapsed)
        log(
            f"agent:{role}",
            f"FAIL exit_code={result.returncode} elapsed={elapsed:.1f}s",
        )
        detail = str(result.stderr or result.stdout or "OpenCode failed")
        detail = detail.replace(api_key, "REDACTED").strip()[:2000]
        raise AgentRuntimeError(f"OpenCode {role} failed: {detail}")
    try:
        payload = _parse_contract(str(result.stdout or ""), required_keys)
        validate_agent_payload(payload, required_keys=required_keys)
    except AgentRuntimeError as error:
        message = str(error).replace(api_key, "REDACTED")
        raise AgentRuntimeError(message) from error
    log(f"agent:{role}", f"PASS elapsed={elapsed:.1f}s")
    return AgentResult(role=role, payload=payload)

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


MODEL = "deepseek/deepseek-v4-flash"
_AGENT_HEARTBEAT_SECONDS = 60.0
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
    started = time.monotonic()
    last_output = [started]
    last_action = ["none"]
    message_count = [0]
    action_counts: dict[str, int] = {}
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
                f"timeout={float(timeout):g}s",
            )

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
        )
    finally:
        completed.set()
        heartbeat_thread.join(timeout=1)
    tools = ",".join(
        f"{tool}:{count}" for tool, count in sorted(action_counts.items())
    )
    log(
        f"agent:{role}",
        f"ACTIVITY messages={message_count[0]} "
        f"actions={sum(action_counts.values())} tools={tools or 'none'}",
    )
    return result


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


def _nested_candidates(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _nested_candidates(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _nested_candidates(nested)
    elif isinstance(value, str):
        for decoded in _scan_json(value):
            yield from _nested_candidates(decoded)


def _parse_contract(
    stdout: str,
    required_keys: tuple[str, ...],
) -> dict[str, object]:
    matches = []
    decoded_values = list(_scan_json(stdout))
    for value in decoded_values:
        for candidate in _nested_candidates(value):
            if all(key in candidate for key in required_keys):
                matches.append(candidate)
    if not matches:
        raise AgentRuntimeError(
            "OpenCode completed without the required JSON contract: "
            + ", ".join(required_keys)
        )
    return matches[-1]


def _permission_config(role: str) -> str:
    writable = role in _WRITE_ROLES
    permission = {
        "read": "allow",
        "edit": "allow" if writable else "deny",
        "bash": "allow" if writable else "deny",
        "webfetch": "allow" if writable else "deny",
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
        "OPENCODE_CONFIG_CONTENT": _permission_config(role),
    }
    started = time.monotonic()
    log(
        f"agent:{role}",
        f"START model={MODEL} timeout={float(timeout):g}s "
        f"prompt_chars={len(prompt)} workspace={workspace}",
    )
    result = runner(command, workspace, env, timeout)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        if result.returncode == 124:
            log(f"agent:{role}", f"TIMEOUT elapsed={elapsed:.1f}s")
            raise AgentRuntimeError(
                f"OpenCode {role} timed out after {elapsed:.1f}s"
            )
        log(
            f"agent:{role}",
            f"FAIL exit_code={result.returncode} elapsed={elapsed:.1f}s",
        )
        detail = str(result.stderr or result.stdout or "OpenCode failed")
        detail = detail.replace(api_key, "REDACTED").strip()[:2000]
        raise AgentRuntimeError(f"OpenCode {role} failed: {detail}")
    try:
        payload = _parse_contract(str(result.stdout or ""), required_keys)
    except AgentRuntimeError as error:
        message = str(error).replace(api_key, "REDACTED")
        raise AgentRuntimeError(message) from error
    log(f"agent:{role}", f"PASS elapsed={elapsed:.1f}s")
    return AgentResult(role=role, payload=payload)

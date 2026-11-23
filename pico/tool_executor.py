"""Structured tool execution for the agent runtime."""

from dataclasses import dataclass
import re

from .features.memory import file_freshness
from .run_store import SHELL_LOG_READ_CHARS, SHELL_LOG_READ_MARKER, tool_output_reference
from .task_state import snapshot_fingerprint
from .workspace import MAX_TOOL_OUTPUT, clip


SHELL_PREVIEW_CHARS = 480
SHELL_ERROR_CHARS = 180
_ERROR_LINE = re.compile(r"\b(?:error|fatal|exception|failed|traceback|panic)\b|\w+(?:Error|Exception):", re.I)


def _log_excerpt(lines, *, fallback=False):
    last_line = None
    for number, line in lines:
        line = line.strip()
        if not line or line in {"stdout:", "stderr:", "(empty)"} or line.startswith("exit_code:"):
            continue
        if last_line is None:
            last_line = {"line": number, "text": line[-SHELL_ERROR_CHARS:]}
        match = _ERROR_LINE.search(line)
        if match:
            start = max(0, match.start() - 24)
            return {"line": number, "text": line[start:start + SHELL_ERROR_CHARS]}
    return last_line if fallback else None


def _shell_preview(output, excerpt):
    if excerpt is None:
        return clip(output, SHELL_PREVIEW_CHARS)
    suffix = f"\n...\nLog line {excerpt['line']}: {excerpt['text']}"
    return output[:SHELL_PREVIEW_CHARS - len(suffix)] + suffix


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, name, args, *, run_id=None):
        agent = self.agent
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            )

        tool = agent.tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            )

        try:
            agent.validate_tool(name, args)
        except Exception as exc:
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        if agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        if tool["risky"] and not agent.approve(name, args):
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level="high",
                    read_only=False,
                ),
            )

        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            artifact_read = name == "read_file" and agent.run_store.is_shell_output(agent.path(args["path"]))
            if artifact_read:
                output = tool["run"](args, max_chars=SHELL_LOG_READ_CHARS - len(SHELL_LOG_READ_MARKER))
            else:
                output = tool["run"](args)
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            evidence = {}
            if name == "run_shell":
                match = re.match(r"exit_code:\s*(-?\d+)", output)
                exit_code = int(match.group(1)) if match else 0
                if match:
                    evidence["exit_code"] = exit_code
                    evidence["verification_snapshot"] = snapshot_fingerprint(after_snapshot)
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
                redacted_output = agent.redact_artifact(output)
                if run_id and max(len(output), len(redacted_output)) > MAX_TOOL_OUTPUT:
                    preview_output = redacted_output
                    try:
                        artifact = agent.run_store.write_shell_output(run_id, redacted_output, agent.root)
                    except (OSError, ValueError, RuntimeError) as exc:
                        evidence["artifact_error"] = agent.redact_text(str(exc))
                        reference = "artifact unavailable (save failed)"
                    else:
                        evidence["artifact"] = artifact
                        reference = f"read_file: {tool_output_reference(artifact)}"
                        if exit_code != 0 and artifact["truncated"]:
                            try:
                                preview_output = (agent.root / artifact["path"]).read_text(encoding="utf-8")
                            except OSError:
                                preview_output = ""
                    excerpt = None
                    if exit_code != 0:
                        excerpt = _log_excerpt(reversed(list(enumerate(preview_output.splitlines(), 1))), fallback=True)
                    if excerpt and "artifact" in evidence:
                        evidence["log_excerpt"] = excerpt
                    output = f"status: {tool_status}\n{reference}\n{_shell_preview(redacted_output, excerpt)}"
                else:
                    output = redacted_output
            elif name == "read_file":
                path = agent.memory.canonical_path(args["path"])
                evidence["read_file_version"] = {"path": path, "freshness": file_freshness(path, agent.root)}
                if artifact_read:
                    evidence["artifact_read"] = True
                    rows = (re.match(r"\s*(\d+): (.*)", line) for line in reversed(output.splitlines()))
                    excerpt = _log_excerpt((int(row[1]), row[2]) for row in rows if row)
                    if excerpt:
                        evidence["log_excerpt"] = excerpt
            content = clip(output)
            if not artifact_read:
                agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            metadata.update(evidence)
            metadata["changed_file_freshness"] = {path: after_snapshot.get(path) for path in affected_paths}
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=content, metadata=metadata)
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            metadata["changed_file_freshness"] = {path: after_snapshot.get(path) for path in affected_paths}
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)

"""跨轮任务证据与单次 ask() 的运行状态。

TaskContext 随 session 保存。TaskState 写入 task_state.json，记录一次运行的
进度、工具次数和停机原因。
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from uuid import uuid4

from .workspace import clip

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_STEP_LIMIT_REACHED = "step_limit_reached"
STOP_REASON_RETRY_LIMIT_REACHED = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_DELEGATE_FAILED = "delegate_failed"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"

_CONTINUE = re.compile(
    r"(?i)(?:please\s+)?(?:continue|resume)(?:\s+(?:the (?:current |previous )?task|working|where we left off))?"
    r"|(?:请)?(?:继续|接着做|接着上次|继续上一个任务|继续当前任务|继续之前的任务)"
)
_NEW_TASK = re.compile(r"(?i)^(?:new task|switch tasks?|新任务|切换任务)\s*[:：]\s*")
_DIRECTIVE = re.compile(
    r"(?i)^(?:[-*]\s+)?(?:please\s+|请)?(?:now\s+|现在|这次)?\s*"
    r"(?P<mode>do not\s+|don't\s+|never\s+|must not\s+|must\s+|only\s+|"
    r"you (?:may|can)\s+|(?:may|can)\s+|不要|不得|禁止|不能|不许|必须|只能|只允许|允许|可以)"
    r"\s*(?P<action>.+)$"
)
_CLAUSE_BREAK = re.compile(
    r"[。；;]+|(?<=\.)\s+|(?<=[!?！？])\s*|[，,]\s*(?=(?:(?:请|现在|这次)\s*)?"
    r"(?:不要|不得|禁止|不能|不许|必须|只能|只允许|允许|可以|"
    r"(?:please\s+)?(?:do not|don't|never|must|only|you (?:may|can))\b))",
    re.IGNORECASE,
)
_ALLOW = {"you may", "you can", "may", "can", "允许", "可以"}
_PUBLIC_API_CHANGE = re.compile(
    r"(?i)(?:modify|change|edit|alter|update) (?:the )?public apis?"
    r"|(?:修改|更改|改动|变更|调整)\s*(?:公共|公开)\s*api"
)
_RECENT_LIMIT = 5


def snapshot_fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass
class TaskContext:
    """Evidence carried across asks in one session."""

    goal: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)
    open_errors: dict = field(default_factory=dict)
    verifications: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        return cls(**{key: dict(data.get(key, {})) for key in cls.__dataclass_fields__})

    def to_dict(self):
        return asdict(self)

    def record_request(self, request, source):
        request = request.strip()
        if _NEW_TASK.match(request):
            self.goal.clear()
            self.constraints.clear()
            self.open_errors.clear()
            self.verifications.clear()
            self.changes.clear()
            request = _NEW_TASK.sub("", request, count=1)
        goal_parts = []
        fenced = False
        for line in request.splitlines():
            # Quoted examples and fenced code are handled by the outer history, not as directives.
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced or line.lstrip().startswith(">"):
                continue
            for part in _CLAUSE_BREAK.split(line):
                clause = part.strip(" \t.，,")
                if not clause or _CONTINUE.fullmatch(clause.rstrip("!！")):
                    continue
                directive = _DIRECTIVE.fullmatch(clause)
                if not directive or clause.endswith(("?", "？")):
                    goal_parts.append(clause)
                    continue
                mode = directive["mode"].strip().casefold()
                action = " ".join(directive["action"].rstrip("!！").replace("`", "").casefold().split())
                if _PUBLIC_API_CHANGE.fullmatch(action):
                    action = "public-api-change"
                if mode not in _ALLOW or action in self.constraints:
                    self.constraints[action] = {"text": clause, "source": source}
        if goal_parts:
            self.goal = {"text": " ".join(goal_parts), "source": source}

    def record_tool(self, name, args, result, source, redact):
        metadata = result.metadata
        status = metadata.get("tool_status")
        if not status or metadata.get("tool_error_code") == "repeated_identical_call":
            return
        if name == "run_shell":
            operation = str(args.get("command", "")).strip()
            key = snapshot_fingerprint([name, operation])
        else:
            operation = f"{name} {args.get('path', '')}".strip()
            key = snapshot_fingerprint([name, args])
        evidence = {
            "operation": redact(operation),
            "status": status,
            "source": source,
        }
        if "artifact" in metadata:
            evidence["artifact"] = metadata["artifact"]
        if "exit_code" in metadata:
            evidence["exit_code"] = metadata["exit_code"]
            evidence["workspace_snapshot"] = metadata["verification_snapshot"]
        if "read_file_version" in metadata:
            evidence["read_file_version"] = metadata["read_file_version"]
        excerpt = metadata.get("log_excerpt")
        if excerpt:
            excerpt = {**excerpt, "text": redact(excerpt["text"])}
        if metadata.get("artifact_read") and excerpt:
            path = metadata["read_file_version"]["path"]
            for error in self.open_errors.values():
                if error.get("artifact", {}).get("path") == path:
                    error["log_excerpt"] = excerpt
        if status == "ok":
            previous = self.open_errors.pop(key, None)
            if previous:
                evidence["resolves"] = previous["source"]
        else:
            evidence["detail"] = clip(redact(result.content).replace("\n", " ").strip(), 160)
            if excerpt:
                evidence["log_excerpt"] = excerpt
            self.open_errors.pop(key, None)
            self.open_errors[key] = evidence
        if "exit_code" in evidence or "resolves" in evidence:
            self._remember(self.verifications, key, evidence)
        for path, freshness in metadata.get("changed_file_freshness", {}).items():
            self._remember(self.changes, path, {"path": path, "freshness": freshness, "source": source})

    @staticmethod
    def _remember(records, key, evidence):
        records.pop(key, None)
        records[key] = evidence
        if len(records) > _RECENT_LIMIT:
            del records[next(iter(records))]


@dataclass
class TaskState:
    run_id: str
    task_id: str
    user_request: str
    status: str = STATUS_RUNNING
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""
    checkpoint_id: str = ""
    resume_status: str = ""

    @classmethod
    def create(cls, task_id, user_request, run_id=""):
        if not run_id:
            run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(run_id=run_id, task_id=task_id, user_request=user_request)

    @classmethod
    def from_dict(cls, data):
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            user_request=str(data.get("user_request", "")),
            status=str(data.get("status", STATUS_RUNNING)),
            tool_steps=int(data.get("tool_steps", 0)),
            attempts=int(data.get("attempts", 0)),
            last_tool=str(data.get("last_tool", "")),
            stop_reason=str(data.get("stop_reason", "")),
            final_answer=str(data.get("final_answer", "")),
            checkpoint_id=str(data.get("checkpoint_id", "")),
            resume_status=str(data.get("resume_status", "")),
        )

    def record_attempt(self):
        # attempt 统计的是“模型被调用了几轮”，不等于 tool_steps。
        self.attempts += 1
        return self

    def record_tool(self, name):
        # tool_steps 只统计真正进入执行阶段的工具调用次数。
        self.tool_steps += 1
        self.last_tool = str(name or "")
        return self

    def stop(self, stop_reason, status=STATUS_STOPPED, final_answer=""):
        # stop_reason 和 status 分开存，是为了区分“怎么停的”和“停下时是什么状态”。
        self.status = status
        self.stop_reason = stop_reason
        if final_answer != "":
            self.final_answer = final_answer
        return self

    def stop_step_limit(self, final_answer=""):
        return self.stop(STOP_REASON_STEP_LIMIT_REACHED, final_answer=final_answer)

    def stop_retry_limit(self, final_answer=""):
        return self.stop(STOP_REASON_RETRY_LIMIT_REACHED, final_answer=final_answer)

    def stop_model_error(self, final_answer=""):
        return self.stop(STOP_REASON_MODEL_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def finish_success(self, final_answer):
        self.status = STATUS_COMPLETED
        self.stop_reason = STOP_REASON_FINAL_ANSWER_RETURNED
        self.final_answer = str(final_answer)
        return self

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
        }

"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

import json
import tempfile
from pathlib import Path
from uuid import uuid4


MAX_SHELL_ARTIFACT_BYTES = 256 * 1024
SHELL_LOG_READ_CHARS = 800
SHELL_LOG_READ_MARKER = "[tool:read_file]\n"
SHELL_ARTIFACT_TRUNCATION = b"\n...[artifact truncated at byte limit; middle omitted]...\n"


def tool_output_reference(artifact):
    truncation = ", truncated" if artifact["truncated"] else ""
    return f'{artifact["path"]} ({artifact["lines"]} lines{truncation})'


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def is_shell_output(self, resolved_path):
        root = self.root.resolve()
        if not resolved_path.is_relative_to(root):
            return False
        parts = resolved_path.relative_to(root).parts
        return len(parts) == 3 and parts[1] == "tool_outputs" and resolved_path.suffix == ".log"

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, task_state.to_dict())
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        return path

    def write_shell_output(self, run_id, redacted_output, workspace_root):
        path = (self.run_dir(run_id) / "tool_outputs" / f"{uuid4().hex}.log").resolve()
        workspace_root = Path(workspace_root).resolve()
        if not path.is_relative_to(workspace_root):
            raise ValueError("artifact path escapes workspace")

        data = redacted_output.encode("utf-8")
        truncated = len(data) > MAX_SHELL_ARTIFACT_BYTES
        if truncated:
            available = MAX_SHELL_ARTIFACT_BYTES - len(SHELL_ARTIFACT_TRUNCATION)
            head_size = available // 2
            tail_size = available - head_size
            head = data[:head_size].decode("utf-8", errors="ignore")
            tail = data[-tail_size:].decode("utf-8", errors="ignore")
            data = head.encode("utf-8") + SHELL_ARTIFACT_TRUNCATION + tail.encode("utf-8")

        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent)
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(data)
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
        return {
            "path": path.relative_to(workspace_root).as_posix(),
            "lines": len(data.decode("utf-8").splitlines()),
            "truncated": truncated,
        }

    def load_task_state(self, task_id):
        return json.loads(self.task_state_path(task_id).read_text(encoding="utf-8"))

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)

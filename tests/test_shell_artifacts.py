import json
import os
import re
import subprocess
import sys

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_manager import ContextManager
from pico.run_store import MAX_SHELL_ARTIFACT_BYTES, SHELL_ARTIFACT_TRUNCATION, RunStore


def tool_call(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def build_agent(root, outputs):
    root.mkdir(exist_ok=True)
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(root, repo_root_override=root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        approval_policy="auto",
    )


def shell_call(root, source):
    (root / "long_log.py").write_text(source, encoding="utf-8")
    return tool_call("run_shell", command=f'"{sys.executable}" long_log.py')


def test_long_shell_artifact_is_readable_from_compact_history_after_resume(tmp_path, monkeypatch):
    secret = "phase-two-test-secret-value"
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    failure = "ERROR E482: migration checksum mismatch"
    call = shell_call(
        tmp_path,
        f"import sys\nprint({secret!r})\n"
        "for index in range(1200):\n    print(f'progress item {index}')\n"
        f"print('x' * 6000 + {failure!r}, file=sys.stderr)\n"
        "sys.exit(3)\n",
    )
    agent = build_agent(tmp_path, [call, "<final>Command failed. See the log.</final>"])
    assert agent.ask("Run the migration.") == "Command failed. See the log."
    first_run = agent.current_task_state.run_id
    shell_event = next(item for item in agent.session["history"] if item["role"] == "tool")
    artifact = shell_event["artifact"]
    assert artifact["path"].startswith(f".pico/runs/{first_run}/tool_outputs/")
    assert not artifact["truncated"]
    assert shell_event["tool_status"] == "error"
    assert "long_log.py" in shell_event["args"]["command"]
    assert "exit_code: 3" in shell_event["content"]
    assert "progress item 0" in shell_event["content"]
    assert failure not in agent.model_client.prompts[-1]
    assert "progress item 1000" not in agent.model_client.prompts[-1]
    assert artifact["path"] in agent.model_client.prompts[-1]
    assert secret not in agent.model_client.prompts[-1]

    persisted = (tmp_path / artifact["path"]).read_text(encoding="utf-8")
    assert failure in persisted
    assert "progress item 1000" in persisted
    assert secret not in persisted
    assert "<redacted>" in persisted
    trace = [json.loads(line) for line in agent.run_store.trace_path(first_run).read_text(encoding="utf-8").splitlines()]
    assert next(event for event in trace if event["event"] == "tool_executed")["artifact"] == artifact

    class LogRecoveryModel(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                assert failure not in prompt
                assert "progress item 1000" not in prompt
                assert "error | read_file:" in prompt
                match = re.search(r"read_file: (\S+\.log) \((\d+) lines\)", prompt)
                assert match
                path, last_line = match.groups()
                return tool_call("read_file", path=path, start=int(last_line), end=int(last_line), start_column=6001)
            match = re.search(r"ERROR E\d+: [^\n]+", prompt)
            assert match
            return f"<final>{match.group()}</final>"

    resumed = Pico.from_session(
        model_client=LogRecoveryModel([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        read_only=True,
        allowed_tools=["read_file"],
    )
    resumed.context_manager = ContextManager(resumed, section_budgets={"history": 390})
    assert resumed.ask("Recover the exact error from the previous run.") == failure
    assert resumed.current_task_state.run_id != first_run
    read = next(item for item in resumed.session["history"] if item.get("name") == "read_file")
    assert read["args"]["path"] == artifact["path"]
    assert failure in read["content"]

    (tmp_path / artifact["path"]).unlink()
    missing = resumed.execute_tool("read_file", read["args"])
    assert "file not found" in missing.content
    assert missing.metadata["tool_status"] == "rejected"


def test_oversized_shell_artifact_retains_ends_and_marks_omission(tmp_path):
    call = shell_call(tmp_path, "print('first line')\nprint('x' * 300_000)\nprint('last line')\n")
    agent = build_agent(tmp_path, [call, "<final>Done.</final>"])
    assert agent.ask("Run the command.") == "Done."
    event = next(item for item in agent.session["history"] if item["role"] == "tool")
    artifact = event["artifact"]
    path = tmp_path / artifact["path"]
    data = path.read_bytes()
    assert len(data) == MAX_SHELL_ARTIFACT_BYTES
    assert data.count(SHELL_ARTIFACT_TRUNCATION) == 1
    assert b"first line" in data and b"last line" in data
    assert artifact["truncated"]
    assert "lines, truncated)" in event["content"]
    result = agent.run_tool("read_file", {"path": artifact["path"], "start": artifact["lines"] - 2})
    assert "last line" in result


def test_artifact_limit_keeps_multibyte_output_valid(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    artifact = store.write_shell_output("run_utf8", "首" * MAX_SHELL_ARTIFACT_BYTES + "\n结尾", tmp_path)
    path = tmp_path / artifact["path"]
    assert path.stat().st_size <= MAX_SHELL_ARTIFACT_BYTES
    assert path.read_text(encoding="utf-8").endswith("结尾")


@pytest.mark.parametrize("reference_kind", ["parent", "absolute", "linked_directory"])
def test_artifact_read_rejects_paths_outside_workspace(tmp_path, reference_kind):
    root = tmp_path / "workspace"
    agent = build_agent(root, [])
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "outside.log"
    outside.write_text("external log contents", encoding="utf-8")
    link = None
    if reference_kind == "parent":
        path = "../external/outside.log"
    elif reference_kind == "absolute":
        path = str(outside)
    else:
        link = root / ".pico" / "runs" / "run_fake" / "tool_outputs"
        link.parent.mkdir(parents=True)
        if os.name == "nt":
            junction = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
            if junction.returncode != 0:
                pytest.skip("directory junctions unavailable")
        else:
            link.symlink_to(external, target_is_directory=True)
        path = str((link / outside.name).relative_to(root))

    try:
        result = agent.execute_tool("read_file", {"path": path})
    finally:
        if link and os.name == "nt":
            link.rmdir()
    assert result.metadata["security_event_type"] == "path_escape"
    assert "external log contents" not in result.content


def test_shell_artifact_save_failure_keeps_exit_status_and_preview(tmp_path, monkeypatch):
    call = shell_call(tmp_path, "print('x' * 6000)\nraise SystemExit(2)\n")
    agent = build_agent(tmp_path, [call, "<final>Command failed.</final>"])

    def fail(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(agent.run_store, "write_shell_output", fail)
    assert agent.ask("Run the command.") == "Command failed."
    event = next(item for item in agent.session["history"] if item["role"] == "tool")
    assert "artifact unavailable (save failed)" in event["content"]
    assert "exit_code: 2" in event["content"]
    assert "artifact" not in event
    assert agent._last_tool_result_metadata["tool_status"] == "error"

import json
import sys

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.checkpoint import load_task_context
from pico.context_manager import ContextManager


def tool(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def build_agent(root, responses, session_id=None):
    options = {
        "model_client": FakeModelClient(responses),
        "workspace": WorkspaceContext.build(root, repo_root_override=root),
        "session_store": SessionStore(root / ".pico" / "sessions"),
        "approval_policy": "auto",
    }
    agent = Pico.from_session(session_id=session_id, **options) if session_id else Pico(**options)
    agent.context_manager = ContextManager(
        agent, total_budget=1700,
        section_budgets={"prefix": 550, "memory": 160, "relevant_memory": 140, "history": 180},
    )
    return agent


def priority_memory(prompt):
    return prompt.split("\n\nMemory:\n", 1)[1].split("\n\nRelevant memory:", 1)[0]


def test_request_and_tool_event_are_saved_with_checkpoint_before_next_model_call(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [tool("write_file", path="example.txt", content="example\n")])
    session_id = agent.session["id"]
    complete = agent.model_client.complete
    save = agent.session_store.save
    observed_roles = []

    def check_session_write(session):
        state = session["checkpoints"]
        checkpoint = state["items"][state["current_id"]]
        assert checkpoint["history_count"] == len(session["history"])
        assert checkpoint["task_context"] == session["task_context"]
        return save(session)

    def interrupt_after_tool(*args, **kwargs):
        saved = agent.session_store.load(session_id)
        state = saved["checkpoints"]
        checkpoint = state["items"][state["current_id"]]
        assert checkpoint["history_count"] == len(saved["history"])
        assert checkpoint["task_context"] == saved["task_context"]
        observed_roles.append(saved["history"][-1]["role"])
        if observed_roles[-1] == "tool":
            assert "example.txt" in checkpoint["task_context"]["changes"]
            raise KeyboardInterrupt
        return complete(*args, **kwargs)

    monkeypatch.setattr(agent.model_client, "complete", interrupt_after_tool)
    monkeypatch.setattr(agent.session_store, "save", check_session_write)
    with pytest.raises(KeyboardInterrupt):
        agent.ask("Write an example. Do not change the public API.")

    assert observed_roles == ["user", "tool"]
    saved = agent.session_store.load(session_id)
    saved["task_context"] = {}
    save(saved)
    resumed = build_agent(tmp_path, [], session_id)
    task_context = load_task_context(resumed)
    assert task_context.goal["text"] == "Write an example"
    assert task_context.constraints["public-api-change"]["text"] == "Do not change the public API"
    assert "example.txt" in task_context.changes


def test_legacy_progress_and_stale_file_survive_interruption_after_request(tmp_path, monkeypatch):
    path = tmp_path / "example.txt"
    path.write_text("original\n", encoding="utf-8")
    agent = build_agent(tmp_path, [tool("read_file", path="example.txt"), "<final>Read it.</final>"])
    agent.ask("Inspect the example.")
    session_id = agent.session["id"]
    legacy_checkpoint = agent.current_checkpoint()
    legacy_checkpoint.pop("history_count")
    context = load_task_context(agent)
    context.record_request("Do not delete tests.", {"history_index": len(agent.session["history"])})
    agent.session["task_context"] = context.to_dict()
    agent.record({"role": "user", "content": "Do not delete tests."})
    path.write_text("edited externally\n", encoding="utf-8")

    def interrupt_before_prompt(*args, **kwargs):
        raise KeyboardInterrupt

    resumed = build_agent(tmp_path, [], session_id)
    monkeypatch.setattr(resumed.run_store, "start_run", interrupt_before_prompt)
    with pytest.raises(KeyboardInterrupt):
        resumed.ask("Continue. Do not change the public API.")

    restarted = build_agent(tmp_path, [], session_id)
    task_context = load_task_context(restarted)
    assert set(task_context.constraints) == {"delete tests", "public-api-change"}
    assert restarted.current_checkpoint()["resume_baseline_id"] == legacy_checkpoint["checkpoint_id"]
    assert restarted.resume_state["status"] == "partial-stale"
    assert restarted.resume_state["stale_paths"] == ["example.txt"]


def test_task_context_survives_tools_and_resume_then_updates_from_evidence(tmp_path):
    target = tmp_path / "check.py"
    target.write_text(
        "print('obsolete-file-observation')\nprint('x' * 7000)\nprint('ERROR: check failed')\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" check.py'
    check = tool("run_shell", command=command)
    agent = build_agent(tmp_path, [
        tool("read_file", path="check.py"),
        check,
        tool("list_files", path="."),
        tool("search", pattern="never-appears-in-the-workspace", path="check.py"),
        "<final>Tests passed.</final>",
    ])
    agent.ask("修复检查脚本。不要修改公共 API。不要删除测试。")
    session_id = agent.session["id"]
    initial = agent.session["task_context"]
    assert initial["goal"]["text"] == "修复检查脚本"
    assert initial["goal"]["source"]["history_index"] == 0
    failure, = initial["open_errors"].values()
    assert failure["exit_code"] == 2
    assert failure["source"]["tool_step"] == 2
    reference = failure["artifact"]["path"]
    assert (tmp_path / reference).is_file()
    memory = priority_memory(agent.model_client.prompts[-1])
    assert "不要修改公共 API" in memory
    assert "Unresolved error" in memory
    assert reference in memory
    assert "log line 5: ERROR: check failed" in memory

    resumed = build_agent(tmp_path, ["<final>继续检查。</final>"], session_id)
    resumed.ask("继续")
    memory = priority_memory(resumed.model_client.prompts[0])
    assert "修复检查脚本" in memory
    assert "不要修改公共 API" in memory
    assert reference in memory
    assert resumed.session["memory"]["working"]["task_summary"] == "继续"
    assert resumed.current_checkpoint()["task_context"] == initial

    fixed = build_agent(tmp_path, [
        tool("write_file", path="check.py", content="print('check passed')\n"),
        tool("run_shell", command=f'"{sys.executable}" -c "print(123)"'),
        tool("run_shell", command=command, timeout=30),
        "<final>验证通过。</final>",
    ], session_id)
    fixed.ask("继续。现在允许更改公共 API。")
    before, after = map(priority_memory, fixed.model_client.prompts[-2:])
    assert "现在允许更改公共 API" in after
    assert "不要修改公共 API" not in after
    assert "不要删除测试" in after
    assert "Unresolved error" in before
    assert "Unresolved error" not in after
    assert "ERROR: check failed" not in after
    assert "Recorded verification" in after and "exit_code: 0" in after
    assert "Recorded change: check.py" in after
    progress = fixed.session["task_context"]
    assert progress["open_errors"] == {}
    resolution = next(item for item in progress["verifications"].values() if item.get("resolves"))
    assert resolution["resolves"] == failure["source"]
    assert resolution["source"]["run_id"] == fixed.current_task_state.run_id

    target.write_text("print('external edit')\n", encoding="utf-8")
    stale = build_agent(tmp_path, ["<final>需要重验。</final>"], session_id)
    stale.context_manager = ContextManager(stale)
    stale.ask("继续")
    prompt = stale.model_client.prompts[0]
    memory = priority_memory(prompt)
    assert "Stale verification (rerun)" in memory
    assert "Changed since recorded (re-read): check.py" in memory
    assert "exit_code: 0" not in memory
    assert "(stale read; re-read the file)" in prompt
    assert "check.py" not in stale.session["memory"]["file_summaries"]
    assert stale.memory.retrieval_candidates("obsolete-file-observation") == []


def test_task_context_legacy_history_and_budget_keep_explicit_constraints(tmp_path):
    agent = build_agent(tmp_path, [])
    constraint = "Do not change the public API"
    agent.record({"role": "user", "content": f"Fix the integration. {constraint}."})
    agent.record({"role": "assistant", "content": "You may change the public API."})
    resumed = build_agent(tmp_path, ["<final>Done.</final>"], agent.session["id"])
    resumed.context_manager = ContextManager(
        resumed, total_budget=50,
        section_budgets={"prefix": 20, "memory": 20, "relevant_memory": 20, "history": 20},
    )
    resumed.ask("Continue.")
    prompt = resumed.model_client.prompts[0]
    assert constraint in prompt
    assert prompt.endswith("Current user request:\nContinue.")
    assert resumed.session["task_context"]["goal"]["text"] == "Fix the integration"
    metadata = resumed.last_prompt_metadata
    assert metadata["prompt_over_budget"]
    assert [row["kind"] for row in metadata["task_state"]["omitted"]] == ["goal"]
    assert metadata["section_order"] == ["prefix", "memory", "relevant_memory", "history", "current_request"]

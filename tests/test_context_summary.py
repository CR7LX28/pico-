import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext, build_agent, build_arg_parser
from pico.agent_loop import AgentLoop
from pico.context_budget import estimate_input_tokens


SUMMARY = {
    "goal": "Fix the parser",
    "constraints": ["Keep the public API"],
    "completed_changes": ["Updated parser.py"],
    "failed_attempts": ["Reverting did not fix the test"],
    "open_issues": [],
    "verification_results": ["test_parser passed"],
    "references": ["parser.py", ".pico/runs/demo/artifacts/test.log"],
    "next_step": "Run the suite",
}


class ScriptedClient(FakeModelClient):
    def complete(self, prompt, max_new_tokens, **kwargs):
        self.last_completion_metadata = {"input_tokens": 91, "output_tokens": 21}
        response = super().complete(prompt, max_new_tokens, **kwargs)
        if isinstance(response, Exception):
            raise response
        return response


def make_agent(root, outputs, **options):
    return Pico(
        ScriptedClient(outputs), WorkspaceContext.build(root), SessionStore(root / ".pico" / "sessions"),
        **options,
    )


def history(count):
    return [{"role": "assistant", "content": f"Original detail {index:02d}"} for index in range(count)]


def trace_events(agent):
    trace_path = agent.run_store.trace_path(agent.current_task_state)
    return [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]


def test_short_session_estimates_input_without_summary_and_keeps_provider_measurement(tmp_path):
    agent = make_agent(tmp_path, ["<final>Done</final>"], context_window_tokens=8192)

    assert agent.ask("你好，检查 parser.py") == "Done"

    assert len(agent.model_client.prompts) == 1
    metadata = agent.last_prompt_metadata
    assert metadata["input_budget_tokens"] == 8192 - agent.max_new_tokens
    assert metadata["input_tokens_estimated"] > 0
    assert metadata["input_tokens_estimate_source"] == "heuristic"
    assert metadata["input_tokens"] == 91
    assert metadata["input_tokens_source"] == "provider"


def test_summary_persists_boundary_keeps_recent_originals_and_resumes(tmp_path):
    agent = make_agent(tmp_path, [json.dumps(SUMMARY), "<final>Done</final>"], context_summary_threshold=3)
    original = history(8)
    agent.session["history"] = original.copy()

    assert agent.ask("Continue") == "Done"

    summary_prompt, action_prompt = agent.model_client.prompts
    assert "Original detail 02" in summary_prompt
    assert "Original detail 03" not in summary_prompt
    assert "Original detail 02" not in action_prompt
    assert "Original detail 03" in action_prompt
    assert "Original detail 07" in action_prompt
    assert "test_parser passed" in action_prompt
    assert agent.session["history"][:8] == original
    assert agent.session["context_summary"]["covered_history_count"] == 3
    event = next(event for event in trace_events(agent) if event["event"] == "context_summary_saved")
    assert event["trigger_reason"] == "old_history_threshold"
    assert event["input_tokens"] == 91
    assert event["duration_ms"] >= 0

    resumed = Pico.from_session(
        ScriptedClient(["<final>Resumed</final>"]), agent.workspace, agent.session_store, agent.session["id"],
        context_summary_threshold=3,
    )
    assert resumed.ask("Continue") == "Resumed"
    assert len(resumed.model_client.prompts) == 1
    assert "test_parser passed" in resumed.model_client.prompts[0]
    assert resumed.last_prompt_metadata["context_budget_source"] == "character_fallback"
    resumed.reset()
    assert "context_summary" not in resumed.session


@pytest.mark.parametrize(
    ("response", "reason"),
    [(TimeoutError("provider timed out"), "model_error"), ("", "empty_summary"), ("{}", "invalid_summary")],
)
def test_failed_summary_keeps_previous_summary_and_history(tmp_path, response, reason):
    agent = make_agent(tmp_path, [response, "<final>Done</final>"], context_summary_threshold=3)
    original = history(11)
    previous = {"covered_history_count": 3, "fields": SUMMARY}
    agent.session.update(history=original.copy(), context_summary=previous)

    assert agent.ask("Continue") == "Done"

    summary_prompt, action_prompt = agent.model_client.prompts
    assert "Prior summary" in summary_prompt
    assert "Original detail 03" in summary_prompt
    assert "Original detail 02" not in summary_prompt
    assert "test_parser passed" in action_prompt
    saved = agent.session_store.load(agent.session["id"])
    assert saved["context_summary"] == previous
    assert saved["history"][:11] == original
    failure = next(event for event in trace_events(agent) if event["event"] == "context_summary_failed")
    assert failure["failure_reason"] == reason


def test_same_failed_segment_is_not_sent_again_without_new_history(tmp_path):
    agent = make_agent(tmp_path, ["{}", "<final>Done</final>"], context_summary_threshold=3)
    agent.session["history"] = history(8)
    agent.ask("Continue")
    agent.session["history"].pop()  # Return to the exact history at the failed attempt.

    AgentLoop(agent)._prepare_prompt(agent.current_task_state, "Continue")

    assert len(agent.model_client.prompts) == 2


def test_oversized_protected_request_returns_clear_error_without_sending_it(tmp_path):
    agent = make_agent(tmp_path, [], context_window_tokens=1400)
    original = history(20)
    agent.session["history"] = original.copy()
    request = "测试" * 700

    with pytest.raises(RuntimeError, match="Context window exceeded.*available 888"):
        agent.ask(request)

    assert agent.model_client.prompts == []
    assert agent.session["history"][:20] == original
    assert agent.session["history"][-1]["content"] == request
    assert any(event["event"] == "context_window_exceeded" for event in trace_events(agent))


def test_budget_check_includes_finalization_notice(tmp_path):
    agent = make_agent(tmp_path, [
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>Done</final>",
    ], max_steps=1)

    assert agent.ask("List files") == "Done"

    finalization = agent.model_client.prompts[-1]
    assert "the tool budget is exhausted" in finalization
    assert agent.last_prompt_metadata["input_tokens_estimated"] == estimate_input_tokens(finalization)


def test_cli_context_options(tmp_path):
    args = build_arg_parser().parse_args([
        "--cwd", str(tmp_path), "--provider", "ollama", "--context-window-tokens", "8192",
        "--context-summary-threshold", "0",
    ])
    agent = build_agent(args)
    assert agent.context_window_tokens == 8192
    assert agent.context_summary_threshold == 0

"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
    load_task_context,
)
from .context_budget import completion_usage, estimate_input_tokens
from .context_summary import RECENT_HISTORY_ENTRIES, parse_summary, plan_summary
from .task_state import TaskState
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def _prepare_prompt(self, task_state, user_message, *, notice=""):
        agent = self.agent
        prompt, metadata = agent._build_prompt_and_metadata(user_message, notice=notice)
        if metadata["history"].get("artifact_read_chars"):
            return prompt, metadata
        plan = plan_summary(agent, metadata)
        if plan is None:
            return prompt, metadata

        started_at = time.monotonic()
        failure_reason = None
        error = ""
        try:
            raw = agent.model_client.complete(plan.prompt, agent.max_new_tokens)
        except Exception as exc:
            failure_reason = "model_error"
            error = clip(agent.redact_text(str(exc)), 300)
        else:
            summary = parse_summary(raw, plan.end, agent.redact_text)
            if summary is None:
                failure_reason = "empty_summary" if isinstance(raw, str) and not raw.strip() else "invalid_summary"
        finally:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            usage = completion_usage(getattr(agent.model_client, "last_completion_metadata", {}) or {})

        if failure_reason is None:
            candidate_prompt, candidate = agent._build_prompt_and_metadata(
                user_message, notice=notice, context_summary=summary,
            )
            recent_to_keep = min(RECENT_HISTORY_ENTRIES, metadata["history"]["recent_count"])
            if (candidate["history"]["summary_covered_history_count"] != plan.end
                or candidate["history"]["recent_count"] < recent_to_keep
                or candidate["context_window_exceeded"]
                or candidate["prompt_over_budget"]):
                failure_reason = "summary_does_not_fit"
            else:
                session = {**agent.session, "context_summary": summary}
                session.pop("context_summary_failed_range", None)
                try:
                    agent.session_path = agent.session_store.save(session)
                except OSError as exc:
                    failure_reason = "persistence_error"
                    error = clip(agent.redact_text(str(exc)), 300)
                else:
                    agent.session["context_summary"] = summary
                    agent.session.pop("context_summary_failed_range", None)
                    prompt, metadata = candidate_prompt, candidate

        trace = {
            "trigger_reason": plan.reason,
            "history_start_index": plan.start,
            "covered_history_count": plan.end,
            "duration_ms": duration_ms,
            "input_tokens_estimated": estimate_input_tokens(plan.prompt),
            "input_tokens_estimate_source": "heuristic",
            **usage,
        }
        if failure_reason:
            agent.session["context_summary_failed_range"] = [plan.start, plan.end, plan.eligible_end]
            trace.update(failure_reason=failure_reason, error=error)
        agent.emit_trace(task_state, "context_summary_failed" if failure_reason else "context_summary_saved", trace)
        return prompt, metadata

    def _persist_model_failure(self, task_state, user_message, exc, run_started_at, prompt_metadata):
        agent = self.agent
        error_text = agent.redact_text(str(exc))
        final = f"Model request failed: {error_text}"
        task_state.stop_model_error(final)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="model_error")
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "model_failed",
            {
                "error": error_text,
                "completion_metadata": dict(agent.last_completion_metadata),
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.last_prompt_metadata = dict(prompt_metadata)
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))

    def _request_model(self, task_state, user_message, prompt, prompt_metadata, run_started_at, purpose):
        agent = self.agent
        if prompt_metadata["history"].get("artifact_read_chars") and prompt_metadata.get("prompt_over_budget"):
            exc = RuntimeError("Prompt budget exceeded while reserving the latest log read.")
            agent.last_completion_metadata = {}
            agent.emit_trace(task_state, "log_read_budget_exceeded", {"purpose": purpose})
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise exc
        if prompt_metadata.get("context_window_exceeded"):
            estimated = prompt_metadata["input_tokens_estimated"]
            budget = prompt_metadata["input_budget_tokens"]
            exc = RuntimeError(f"Context window exceeded: estimated input {estimated} tokens, available {budget}.")
            agent.last_completion_metadata = {}
            agent.emit_trace(task_state, "context_window_exceeded", {"purpose": purpose, "input_budget_tokens": budget})
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise exc
        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                "purpose": purpose,
            },
        )
        prompt_cache_key = None
        prompt_cache_retention = None
        if getattr(agent.model_client, "supports_prompt_cache", False):
            prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            prompt_cache_retention = "in_memory"
        model_started_at = time.monotonic()
        try:
            raw = agent.model_client.complete(
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except Exception as exc:
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            completion_metadata.update(completion_usage(completion_metadata))
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        completion_metadata.update(completion_usage(completion_metadata))
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        kind, payload = agent.parse(raw)
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": kind,
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                "purpose": purpose,
            },
        )
        return raw, kind, payload

    def _finish_success(self, task_state, user_message, final, run_started_at):
        agent = self.agent
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        task_state.finish_success(final)
        agent.promote_durable_memory(user_message, final)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": "run_finished",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

    def run(self, user_message):
        agent = self.agent
        # 记录本轮 run 的起始时间，用于最后统计总耗时
        run_started_at = time.monotonic()
        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_context = load_task_context(agent)
        source = {"run_id": task_state.run_id, "history_index": len(agent.session["history"])}
        task_context.record_request(user_message, source)
        # 最新请求短文本与跨轮任务状态各自更新。
        agent.memory.set_task_summary(user_message)
        request_checkpoint = agent.create_checkpoint(
            task_state, user_message, trigger="user_request",
            event={"role": "user", "content": user_message, "created_at": now()},
            task_context=task_context.to_dict(),
        )

        # [4] 标记恢复状态（新 run 为 no-checkpoint，恢复时为 full-valid / partial-stale）
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        # [5] 挂到 agent 上，后续主循环每轮都读写它
        agent.current_task_state = task_state
        # [6] 在 .pico/runs/<run_id>/ 下创建本轮目录
        agent.current_run_dir = agent.run_store.start_run(task_state)
        # [7] 追加第一条 trace 事件，标记 run 正式开始
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )
        agent.emit_trace(
            task_state, "checkpoint_created",
            {"checkpoint_id": request_checkpoint["checkpoint_id"], "trigger": "user_request"},
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        # ================================================================
        # 主循环：感知 → 决策 → 行动 → 记录
        # 两个退出条件：工具步数用尽（too many actions）或重试次数用尽（too many malformed responses）
        # ================================================================
        while tool_steps < agent.max_steps and attempts < max_attempts:
            # ---------- 阶段一：感知 ----------
            attempts += 1                    # 本轮模型调用计数 +1
            task_state.record_attempt()      # 把 attempt 计数同步到 TaskState
            agent.run_store.write_task_state(task_state)  # 写出 task_state.json，让外部看到进度

            prompt_started_at = time.monotonic()
            # 重建 prompt：history + memory + workspace 每轮都可能变，所以必须重新投影
            prompt, prompt_metadata = self._prepare_prompt(task_state, user_message)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,  # 记录这轮 prompt 的"成分表"
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )

            # ---------- 检查点预处理：在调模型之前检查三种异常 ----------
            # 条件1：从检查点恢复后，文件摘要过期了
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            # 条件2：换机器/换用户了，仓库身份不匹配
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            # 条件3：prompt 超预算，主动裁减了一部分内容（用 elif 是互斥的，但 budget_reductions 和上面条件可能同时发生，所以用独立的 if）
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            # ---------- 阶段二：决策（调模型） ----------
            # 统一走 _request_model：内部负责 trace、prompt cache、usage 合并与失败持久化
            raw, kind, payload = self._request_model(
                task_state,
                user_message,
                prompt,
                prompt_metadata,
                run_started_at,
                purpose="action",
            )

            # ================================================================
            # 分支 A：模型要调工具 → 执行 + 落证据 → 回到循环顶部
            # ================================================================
            if kind == "tool":
                tool_steps += 1                   # 工具步数 +1（只在真正执行时计数）
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)      # 更新 last_tool 并累加 tool_steps

                tool_started_at = time.monotonic()
                tool_result = agent.execute_tool(name, args, run_id=task_state.run_id)
                result = tool_result.content
                # 三处落盘，同一份结果三种视角：
                # ① session history 保存模型可见的结果，长命令日志带运行工件引用。
                item = {"role": "tool", "name": name, "args": args, "content": result, "created_at": now()}
                if "artifact" in tool_result.metadata:
                    item["artifact"] = tool_result.metadata["artifact"]
                    item["tool_status"] = tool_result.metadata["tool_status"]
                if "read_file_version" in tool_result.metadata:
                    item["read_file_version"] = tool_result.metadata["read_file_version"]
                if tool_result.metadata.get("artifact_read"):
                    item["artifact_read"] = True
                source = {
                    "run_id": task_state.run_id,
                    "tool_step": task_state.tool_steps,
                    "history_index": len(agent.session["history"]),
                }
                task_context.record_tool(name, args, tool_result, source, agent.redact_text)
                checkpoint = agent.create_checkpoint(
                    task_state, user_message, trigger="tool_executed", event=item,
                    task_context=task_context.to_dict(),
                )
                # ② write_task_state → task_state.json：只更新计数（tool_steps / last_tool），给监控看
                agent.run_store.write_task_state(task_state)
                # ③ emit_trace → trace.jsonl：裁剪后摘要 + 耗时 + 工具元数据，给人排错看
                agent.emit_trace(
                    task_state, "tool_executed",
                    {
                        "name": name, "args": args, "tool_step": task_state.tool_steps,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),  # 工具返回的安全级别、错误码等
                    },
                )
                agent.emit_trace(
                    task_state, "checkpoint_created",
                    {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "tool_executed"},
                )
                continue  # 回到循环顶部，下一轮 prompt 会带上工具结果

            # ================================================================
            # 分支 B：模型输出格式不合法 → 写重试提示 → 回到循环顶部
            # ================================================================
            if kind == "retry":
                # payload 是 retry_notice 生成的提示文本，写进 history 让模型下轮看到
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                continue  # 不做任何工具动作，直接进下一轮

            # ================================================================
            # 分支 C：模型给出最终答案 → 收尾 + 落盘 → return
            # ================================================================

            final = (payload or raw).strip()
            return self._finish_success(task_state, user_message, final, run_started_at)

        if tool_steps >= agent.max_steps:
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            notice = (
                "\n\nRuntime notice: the tool budget is exhausted. Do not call another tool. "
                "Use the evidence already present in the tool history and return exactly one "
                "non-empty <final>...</final> answer."
            )
            prompt, prompt_metadata = self._prepare_prompt(task_state, user_message, notice=notice)
            prompt_metadata["finalization"] = True
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                    "purpose": "finalization",
                },
            )
            raw, kind, payload = self._request_model(
                task_state,
                user_message,
                prompt,
                prompt_metadata,
                run_started_at,
                purpose="finalization",
            )
            if kind == "final":
                final = (payload or raw).strip()
                return self._finish_success(task_state, user_message, final, run_started_at)

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

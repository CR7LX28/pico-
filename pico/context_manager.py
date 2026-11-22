"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .context_budget import estimate_input_tokens, input_budget
from .context_summary import render_summary
from .features.memory import file_freshness
from .run_store import tool_output_reference
from .task_state import snapshot_fingerprint


DEFAULT_TOTAL_BUDGET = 12000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}
# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3
HISTORY_HEADER = "Transcript:"
TASK_STATE_HEADER = "Memory:\nActive task state:"
FIXED_HISTORY_WINDOW = 6
OLDER_MESSAGE_CHARS = 60
SHORT_TOOL_CHARS = 180


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
        history_strategy="dynamic",
    ):
        if history_strategy not in ("fixed", "dynamic"):
            raise ValueError("history_strategy must be 'fixed' or 'dynamic'")
        self.agent = agent
        self.history_strategy = history_strategy
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message, *, notice="", context_summary=None):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}{notice}",
        }
        history = self._prompt_history()
        summary = context_summary if context_summary is not None else getattr(self.agent, "session", {}).get("context_summary")
        window = getattr(self.agent, "context_window_tokens", None)
        if window is None:
            window = getattr(getattr(self.agent, "model_client", None), "context_window_tokens", None)
        reserve = getattr(self.agent, "max_new_tokens", 0)
        token_budget = input_budget(window, reserve)
        task_rows = self._task_rows() if memory_enabled else []
        protected_chars = self._task_chars([row for row in task_rows if row["protected"]])
        task_chars = self._task_chars(task_rows)
        order = self.reduction_order
        if task_rows and "history" in order:
            order = ("history", *(section for section in order if section != "history"))
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = section_texts["prefix"] + "\n\n" + checkpoint_text
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, history, task_rows, selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                reduction_order=order,
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            metadata.update(self._token_metadata(prompt, window, reserve, token_budget))
            return prompt, metadata

        budgets = dict(self.section_budgets)
        reduction_log = []
        if task_rows:
            # Give priority rows first use of the memory allocation, then borrow from history.
            reserved = max(protected_chars, min(task_chars, self.total_budget))
            extra = max(0, reserved - budgets["memory"])
            budgets["memory"] += extra
            before = budgets["history"]
            budgets["history"] = min(before, max(len(HISTORY_HEADER), before - extra))
            if before != budgets["history"]:
                reduction_log.append({
                    "section": "history", "before_chars": before,
                    "after_chars": budgets["history"], "reason": "task_state_reservation",
                })
        rendered = self._render_sections(section_texts, budgets, history, task_rows, selected_notes, summary)
        prompt = self._assemble_prompt(rendered)
        floors = dict(self.section_floors)
        if task_rows:
            floors["history"] = min(floors["history"], len(HISTORY_HEADER))
            floors["memory"] = max(floors["memory"], reserved)

        # Preserve task rows while other sections can shrink. In the last pass,
        # only active constraints and the current request are mandatory.
        while True:
            token_overflow = 0 if token_budget is None else estimate_input_tokens(prompt) - token_budget
            overflow = max(len(prompt) - self.total_budget, token_overflow)
            if overflow <= 0:
                break
            reduced = False
            for section in order:
                floor = int(floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                usable = min(current_budget, rendered[section].rendered_chars) if token_overflow > 0 else current_budget
                new_budget = max(floor, usable - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append({
                    "section": section,
                    "before_chars": current_budget,
                    "after_chars": new_budget,
                    "overflow_chars": max(0, len(prompt) - self.total_budget),
                    "overflow_tokens_estimated": max(0, token_overflow),
                })
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, history, task_rows, selected_notes, summary)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                if not task_rows or "memory" not in order or floors["memory"] == protected_chars:
                    break
                floors["memory"] = protected_chars

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            reduction_order=order,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
        )
        metadata.update(self._token_metadata(prompt, window, reserve, token_budget))
        return prompt, metadata

    @staticmethod
    def _token_metadata(prompt, window, reserve, budget):
        estimated = estimate_input_tokens(prompt)
        return {
            "input_tokens_estimated": estimated,
            "input_tokens_estimate_source": "heuristic",
            "context_window_tokens": window,
            "output_reserve_tokens": reserve,
            "input_budget_tokens": budget,
            "context_budget_source": "model_window" if budget is not None else "character_fallback",
            "context_window_exceeded": budget is not None and estimated > budget,
        }

    def _render_sections_without_reduction(self, section_texts, history, task_rows, selected_notes):
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history_raw = self._raw_history_text(history)
        memory = section_texts["memory"]
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": self._render_memory(memory, len(memory) + self._task_chars(task_rows), task_rows),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(
                raw=history_raw,
                budget=len(history_raw),
                rendered=history_raw,
                details={
                    "strategy": "unreduced",
                    "retained_count": len(history),
                    "recent_count": 0,
                    "shortened_entries": [],
                    "omitted_entries": [],
                },
            ),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, history, task_rows, selected_notes, summary=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0), history, summary)
            elif section == "memory":
                rendered[section] = self._render_memory(section_texts[section], int(budget or 0), task_rows)
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _task_rows(self):
        state = getattr(self.agent, "session", {}).get("task_context") or {}
        rows = []

        def add(kind, evidence, text, protected=False, shortened=False):
            rows.append({
                "kind": kind, "source": evidence["source"], "text": "- " + text,
                "protected": protected, "shortened": shortened,
            })

        for constraint in state.get("constraints", {}).values():
            add("constraint", constraint, f"Constraint: {constraint['text']}", protected=True)
        if goal := state.get("goal"):
            text = goal["text"]
            add("goal", goal, f"Goal: {_tail_clip(text, 360)}", shortened=len(text) > 360)
        for error in reversed(list(state.get("open_errors", {}).values())):
            operation = error["operation"]
            detail = self._task_evidence_detail(error)
            add("error", error, f"Unresolved error ({error['status']}): {_tail_clip(operation, 160)}{detail}",
                shortened=len(operation) > 160)
        verifications = [item for item in state.get("verifications", {}).values() if item["status"] == "ok"]
        workspace_snapshot = None
        if any("workspace_snapshot" in item for item in verifications):
            workspace_snapshot = snapshot_fingerprint(self.agent.capture_workspace_snapshot())
        for item in reversed(verifications):
            operation = item["operation"]
            stale = (
                "workspace_snapshot" in item and item["workspace_snapshot"] != workspace_snapshot
                or self._stale_read(item.get("read_file_version"))
            )
            label = "Stale verification (rerun)" if stale else "Recorded verification"
            if stale:
                artifact = item.get("artifact")
                result = f" | historical log, read_file: {tool_output_reference(artifact)}" if artifact else ""
            else:
                result = self._task_evidence_detail(item) or " | ok"
            add("stale_verification" if stale else "verification", item,
                f"{label}: {_tail_clip(operation, 160)}{result}", shortened=len(operation) > 160)
        for item in reversed(list(state.get("changes", {}).values())):
            path = item["path"]
            stale = file_freshness(path, self.agent.root) != item["freshness"]
            label = "Changed since recorded (re-read)" if stale else "Recorded change"
            add("stale_change" if stale else "change", item, f"{label}: {path}")
        return rows

    @staticmethod
    def _task_evidence_detail(evidence):
        parts = []
        if "exit_code" in evidence:
            parts.append(f"exit_code: {evidence['exit_code']}")
        if artifact := evidence.get("artifact"):
            parts.append(f"read_file: {tool_output_reference(artifact)}")
        elif "detail" in evidence:
            parts.append(evidence["detail"])
        return "".join(f" | {part}" for part in parts)

    @staticmethod
    def _task_chars(rows):
        return len(TASK_STATE_HEADER) + 2 + sum(len(row["text"]) + 1 for row in rows) if rows else 0

    def _render_memory(self, raw, budget, task_rows):
        if not task_rows:
            return SectionRender(raw, budget, _tail_clip(raw, budget), {})
        task_raw = TASK_STATE_HEADER + "\n" + "\n".join(row["text"] for row in task_rows)
        lines = [TASK_STATE_HEADER]
        used = len(TASK_STATE_HEADER)
        details = {"retained": [], "omitted": [], "shortened": []}
        for row in task_rows:
            size = len(row["text"]) + 1
            entry = {"kind": row["kind"], "source": row["source"]}
            if not row["protected"] and used + size > budget:
                details["omitted"].append(entry)
                continue
            lines.append(row["text"])
            used += size
            details["retained"].append(entry)
            if row["shortened"]:
                details["shortened"].append(entry)
        details["protected_chars"] = self._task_chars([row for row in task_rows if row["protected"]])
        working = raw.removeprefix("Memory:\n")
        if len(lines) == 1:
            rendered = _tail_clip(raw, budget)
        else:
            memory = _tail_clip(working, budget - used - 2)
            rendered = "\n".join(lines)
            if memory:
                rendered += "\n\n" + memory
        return SectionRender(task_raw + "\n\n" + working, budget, rendered, {"task_state": details})

    def _stale_read(self, version):
        return bool(version and version["freshness"] != file_freshness(version["path"], self.agent.root))

    def _prompt_history(self):
        history = getattr(self.agent, "session", {}).get("history", [])
        stale_paths = getattr(self.agent, "resume_state", {}).get("stale_paths", [])
        projected = []
        freshness = {}
        for item in history:
            if item.get("name") == "read_file":
                version = item.get("read_file_version")
                if version:
                    path = version["path"]
                    if path not in freshness:
                        freshness[path] = file_freshness(path, self.agent.root)
                    stale = version["freshness"] != freshness[path]
                else:
                    stale = item["args"].get("path") in stale_paths
                if stale:
                    item = {**item, "content": "(stale read; re-read the file)", "stale_read": True}
            projected.append(item)
        return projected

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget, history=None, summary=None):
        if history is None:
            history = self._prompt_history()
        header = HISTORY_HEADER
        covered = 0
        if summary_text := render_summary(summary, len(history)):
            if len(summary_text) + 1 <= budget // 2:
                header += "\n" + summary_text
                covered = summary["covered_history_count"]
                history = history[covered:]
        full_entries = [self._render_history_item(item) for item in history]
        raw = "\n".join([header, *(full_entries or ["- empty"])])
        remaining = budget - len(header)
        retained = []
        shortened = []
        omitted = []
        details = {
            "strategy": self.history_strategy,
            "summary_covered_history_count": covered,
            "shortened_entries": shortened,
            "omitted_entries": omitted,
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }
        fixed = self.history_strategy == "fixed"
        first_recent = max(0, len(history) - FIXED_HISTORY_WINDOW) if fixed else 0
        recent_start = len(history)
        for index in range(len(history) - 1, first_recent - 1, -1):
            entry = full_entries[index]
            if len(entry) + 1 > remaining:
                if not fixed:
                    break
                entry = self._short_history_item(history[index], remaining - 1)
                if entry:
                        shortened.append({"index": index + covered, "reason": "budget"})
            recent_start = index
            if entry:
                retained.append(entry)
                remaining -= len(entry) + 1
            else:
                omitted.append({"index": index + covered, "reason": "budget"})
        details["recent_count"] = len(retained)

        # 动态窗口遇到首条放不下的记录时，先尝试它的短表示，再向更早的记录推进。
        for index, reason, result in self._compressed_history_entries(history, recent_start):
            if reason == "duplicate_read":
                details["collapsed_duplicate_reads"] += 1
                omitted.append({"index": index + covered, "reason": reason})
                continue
            entry = self._short_history_item(history[index], remaining - 1, result)
            if not entry:
                omitted.append({"index": index + covered, "reason": "budget"})
                continue
            retained.append(entry)
            remaining -= len(entry) + 1
            if entry != full_entries[index]:
                shortened.append({"index": index + covered, "reason": reason})
            if history[index]["role"] == "tool":
                details["older_entries_count"] += 1
                counter = "reused_file_summary_count" if reason == "file_summary" else "summarized_tool_count"
                details[counter] += 1

        details["retained_count"] = len(retained)
        shortened.reverse()
        omitted.reverse()
        if history:
            header = header if budget >= len(header) else ""
            rendered = "\n".join([header, *reversed(retained)])
        else:
            rendered = _tail_clip(raw, budget)
        return SectionRender(raw=raw, budget=budget, rendered=rendered, details=details)

    def _compressed_history_entries(self, history, recent_start):
        seen_older_reads = set()
        for index in range(recent_start - 1, -1, -1):
            item = history[index]
            if item["role"] != "tool":
                yield index, "older_history", None
                continue

            if item["name"] == "read_file" and not item.get("stale_read"):
                path = str(item["args"].get("path", "")).strip()
                if path:
                    if path in seen_older_reads:
                        yield index, "duplicate_read", None
                        continue
                    seen_older_reads.add(path)
                    summary = self._reusable_file_summary(path)
                    if summary:
                        yield index, "file_summary", summary
                        continue
            yield index, "tool_summary", None

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _raw_history_text(self, history):
        entries = [self._render_history_item(item) for item in history]
        return "\n".join([HISTORY_HEADER, *(entries or ["- empty"])])

    def _render_history_item(self, item):
        if item["role"] == "tool":
            content = item["content"]
            if self._is_unversioned_read(item):
                content = "Historical read (version unverified):\n" + content
            return f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}\n{content}"
        return f"[{item['role']}] {item['content']}"

    @staticmethod
    def _is_unversioned_read(item):
        return item["name"] == "read_file" and not item.get("read_file_version") and not item.get("stale_read")

    def _short_history_item(self, item, budget, result=None):
        if item["role"] != "tool":
            prefix = f"[{item['role']}] "
            content = str(item["content"])
            available = min(OLDER_MESSAGE_CHARS, budget - len(prefix))
            if available < min(len(content), 8) or available < 0:
                return ""
            return prefix + _tail_clip(content, available)

        prefix = f"[tool:{item['name']}] "
        separator = " -> "
        args = json.dumps(item["args"], sort_keys=True)
        available = min(budget, SHORT_TOOL_CHARS) - len(prefix) - len(separator)
        if artifact := item.get("artifact"):
            reference = f"{item['tool_status']} | read_file: {tool_output_reference(artifact)}"
            args_budget = available - len(reference)
            if args_budget < min(len(args), 16):
                return ""
            return prefix + _tail_clip(args, args_budget) + separator + reference
        if result is None:
            lines = [line.strip() for line in str(item["content"]).splitlines() if line.strip()]
            result = " | ".join(lines[:3]) or "(empty)"
            if self._is_unversioned_read(item):
                result = "(historical read) " + result
            if len(lines) > 3:
                result += " ..."
        if available < min(len(args), 16) + min(len(result), 12):
            return ""
        # 两部分各自留出可读预览，短的一方用不完的空间交给另一方。
        args_budget = min(len(args), max(min(len(args), 16), available // 2, available - len(result)))
        return prefix + _tail_clip(args, args_budget) + separator + _tail_clip(result, available - args_budget)

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).lstrip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, reduction_order, selected_notes, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(reduction_order),
            "task_state": rendered["memory"].details.get("task_state", {}),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "budget_chars": rendered["history"].budget,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": 0,
                "collapsed_duplicate_reads": 0,
                "reused_file_summary_count": 0,
                "summarized_tool_count": 0,
                **rendered["history"].details,
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }

# Agent Harness v1 Overview

Agent Harness v1 is Pico's current runtime shape: a local control loop around a model, repository context, constrained tools, task state, memory, and auditable run artifacts.

## Runtime Flow

1. Build workspace context and runtime prefix.
2. Create the per-run `TaskState`.
3. Save the user request, updated task context, working memory, and request checkpoint in a single session write.
4. Assemble `prefix → memory → relevant_memory → history → current_request` within a character budget.
5. Request the model response.
6. Parse the response into a tool call, retry notice, or final answer.
7. Execute tools through runtime policy.
8. Save each tool event, updated task context, and checkpoint in a single session write. Write the run snapshots, trace events, and final report separately.

## Session State and Recovery

| State | Role |
| --- | --- |
| `session.history` | Ordered user, assistant, and tool events. Prompt compression only changes their presentation. |
| `memory.working.task_summary` | The latest user request, clipped to a short preview. A request such as “继续” updates this field. |
| Working memory | Recent file paths, freshness-checked file summaries, and notes for lightweight retrieval. |
| `session.task_context` | The task goal, active explicit constraints, open tool errors, recent verification results and file changes, and Phase 2 log references. Evidence points to a history index and, for new records, a run ID and tool step where applicable. |
| Checkpoint | A point-in-time copy of task context. A request checkpoint keeps a reference to the prior checkpoint used for runtime identity and key-file freshness checks. |

Resume prefers the task context from a checkpoint written with the request or tool event. For an older session, the saved `session.task_context` can contain updates made after its checkpoint, so it takes precedence. If both are absent, user history and a compatible checkpoint goal can seed the task context.

A bare continuation preserves the previous goal. Supported explicit directives update the matching constraint. Tool errors close when the same operation succeeds. Shell operations match by command text, and other tools match by name and arguments. An assistant's final answer has no effect on that evidence.

The `memory` section gives active constraints and task evidence priority over ordinary history. It keeps the current request intact and records omitted task rows or remaining overflow in prompt metadata. A changed workspace invalidates recorded shell verification for current use. Changed file observations prompt a re-read, and log references continue to point to the run that produced them.

## State Artifacts

- `task_state.json` records attempts, tool steps, status, stop reason, and final answer.
- `trace.jsonl` records the event timeline for prompt, model, tool, checkpoint, and finish phases.
- `report.json` records the review summary, prompt metadata, durable memory changes, and execution metadata.

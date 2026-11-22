# Agent Harness v1 Overview

Agent Harness v1 is Pico's current runtime shape: a local control loop around a model, repository context, constrained tools, task state, memory, and auditable run artifacts.

## Runtime Flow

1. Build workspace context and runtime prefix.
2. Create the per-run `TaskState`.
3. Record the user request in session history, update the session's task context, and store the latest request preview in working memory.
4. Assemble `prefix → memory → relevant_memory → history → current_request` within a character budget.
5. Request the model response.
6. Parse the response into a tool call, retry notice, or final answer.
7. Execute tools through runtime policy.
8. Record tool evidence in session history and task context, then write run snapshots, trace events, checkpoints, and the report.

## Session State and Recovery

| State | Role |
| --- | --- |
| `session.history` | Ordered user, assistant, and tool events. Prompt compression only changes their presentation. |
| `memory.working.task_summary` | The latest user request, clipped to a short preview. A request such as “继续” updates this field. |
| Working memory | Recent file paths, freshness-checked file summaries, and notes for lightweight retrieval. |
| `session.task_context` | The task goal, active explicit constraints, open tool errors, recent verification results and file changes, and Phase 2 log references. Evidence points to a history index and, for new records, a run ID and tool step where applicable. |
| Checkpoint | A point-in-time copy of task context with the runtime identity and key-file fingerprints. Resume loads the current session state and checks freshness. |

A bare continuation preserves the previous goal. Supported explicit directives update the matching constraint. Legacy sessions seed goals and constraints from user history and can fall back to a compatible checkpoint goal. Tool errors close when the same operation succeeds. Shell operations match by command text, and other tools match by name and arguments. An assistant's final answer has no effect on that evidence.

The `memory` section gives active constraints and task evidence priority over ordinary history. It keeps the current request intact and records omitted task rows or remaining overflow in prompt metadata. A changed workspace invalidates recorded shell verification for current use. Changed file observations prompt a re-read, and log references continue to point to the run that produced them.

## State Artifacts

- `task_state.json` records attempts, tool steps, status, stop reason, and final answer.
- `trace.jsonl` records the event timeline for prompt, model, tool, checkpoint, and finish phases.
- `report.json` records the review summary, prompt metadata, durable memory changes, and execution metadata.

import {
  DispatchPipelineSchema,
  DispatchIterativeDevSchema,
  DispatchResearchOnlySchema,
  DispatchQaBatchSchema,
  DispatchInfraTaskSchema,
  GetRunStatusSchema,
  ListActiveRunsSchema,
  CancelRunSchema,
  ListGraphsSchema,
} from "../schemas.js";
import {
  createThreadAndRun,
  getThread,
  getThreadState,
  listActiveThreads,
  cancelRun,
  listAssistants,
} from "../clients/langgraph.js";
import type { ToolResult } from "../types.js";

// ---------------------------------------------------------------------------
// Pipeline tools — LangGraph Platform backend
// ---------------------------------------------------------------------------

export async function dispatchPipeline(rawArgs: unknown): Promise<ToolResult> {
  const args = DispatchPipelineSchema.parse(rawArgs);

  const result = await createThreadAndRun(
    args.assistant_id,
    args.input,
    args.config,
    args.metadata,
  );

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            thread_id: result.thread_id,
            run_id: result.run_id,
            note:
              "Pipeline dispatched. Use get_run_status with thread_id to poll progress. " +
              "Use run_id with get_run_trace for Langfuse observability.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Typed dispatchers — one per graph
//
// These exist so the MCP surface enforces what each graph requires. The
// generic dispatch_pipeline is permissive (any input shape goes), which let
// callers — including Archie — accidentally omit required fields like the
// per-ticket executor tag (the AFT-1 bug, 2026-04-23). The typed dispatchers
// validate at the Zod layer; bad calls fail before they reach LangGraph.
// ---------------------------------------------------------------------------

function dispatchedNote(threadId: string, runId: string): string {
  return JSON.stringify(
    {
      thread_id: threadId,
      run_id: runId,
      note:
        "Pipeline dispatched. Use get_run_status with thread_id to poll progress. " +
        "Use run_id with get_run_trace for Langfuse observability.",
    },
    null,
    2,
  );
}

export async function dispatchIterativeDev(rawArgs: unknown): Promise<ToolResult> {
  const args = DispatchIterativeDevSchema.parse(rawArgs);

  const input: Record<string, unknown> = {
    tickets: args.tickets,
    repo: args.repo,
    project_key: args.project_key,
  };

  const result = await createThreadAndRun(
    "iterative_dev",
    input,
    args.config,
    args.metadata,
  );

  return {
    content: [{ type: "text", text: dispatchedNote(result.thread_id, result.run_id) }],
  };
}

export async function dispatchResearchOnly(rawArgs: unknown): Promise<ToolResult> {
  const args = DispatchResearchOnlySchema.parse(rawArgs);

  const input: Record<string, unknown> = {
    question: args.question,
    research_mode: args.research_mode,
    project_key: args.project_key,
  };

  const result = await createThreadAndRun(
    "research_only",
    input,
    args.config,
    args.metadata,
  );

  return {
    content: [{ type: "text", text: dispatchedNote(result.thread_id, result.run_id) }],
  };
}

export async function dispatchQaBatch(rawArgs: unknown): Promise<ToolResult> {
  const args = DispatchQaBatchSchema.parse(rawArgs);

  const input: Record<string, unknown> = {
    spec: args.spec,
    project_key: args.project_key,
    repo: args.repo,
  };
  if (args.design_output_location !== undefined) {
    input["design_output_location"] = args.design_output_location;
  }

  const result = await createThreadAndRun(
    "qa_batch",
    input,
    args.config,
    args.metadata,
  );

  return {
    content: [{ type: "text", text: dispatchedNote(result.thread_id, result.run_id) }],
  };
}

export async function dispatchInfraTask(rawArgs: unknown): Promise<ToolResult> {
  const args = DispatchInfraTaskSchema.parse(rawArgs);

  const input: Record<string, unknown> = {
    infra_task_description: args.infra_task_description,
  };
  if (args.infra_context !== undefined) {
    input["infra_context"] = args.infra_context;
  }

  const result = await createThreadAndRun(
    "infra_task",
    input,
    args.config,
    args.metadata,
  );

  return {
    content: [{ type: "text", text: dispatchedNote(result.thread_id, result.run_id) }],
  };
}

export async function getRunStatus(rawArgs: unknown): Promise<ToolResult> {
  const args = GetRunStatusSchema.parse(rawArgs);

  const [thread, state] = await Promise.all([
    getThread(args.thread_id),
    getThreadState(args.thread_id),
  ]);

  const interrupted = thread.status === "interrupted";
  const currentNodes = state.next;
  const tasks = state.tasks ?? [];

  const interruptDetails = interrupted
    ? tasks.flatMap((t) => t.interrupts ?? []).map((i) => i.value)
    : [];

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            thread_id: args.thread_id,
            status: thread.status,
            interrupted,
            current_nodes: currentNodes,
            interrupt_payloads: interruptDetails,
            last_updated: thread.updated_at,
            hint: interrupted
              ? "Pipeline is paused at a human gate. Use get_interrupt_state for full details, then resume_run to continue."
              : thread.status === "error"
                ? "Pipeline encountered an error. Check interrupt_payloads for details."
                : thread.status === "idle"
                  ? "Pipeline has completed."
                  : "Pipeline is running.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

export async function listActiveRuns(rawArgs: unknown): Promise<ToolResult> {
  const args = ListActiveRunsSchema.parse(rawArgs);
  const threads = await listActiveThreads(args.limit);

  if (threads.length === 0) {
    return {
      content: [
        {
          type: "text",
          text: "No active runs found. All threads are idle, errored, or no threads exist yet.",
        },
      ],
    };
  }

  const summary = threads.map((t) => ({
    thread_id: t.thread_id,
    status: t.status,
    updated_at: t.updated_at,
    metadata: t.metadata,
  }));

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify({ active_threads: summary, count: summary.length }, null, 2),
      },
    ],
  };
}

export async function cancelPipelineRun(rawArgs: unknown): Promise<ToolResult> {
  const args = CancelRunSchema.parse(rawArgs);
  await cancelRun(args.thread_id, args.run_id);

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            cancelled: true,
            thread_id: args.thread_id,
            run_id: args.run_id,
            note: "Cancel request sent. Use get_run_status to confirm the thread reaches 'idle' or 'error' status.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

export async function listGraphs(rawArgs: unknown): Promise<ToolResult> {
  const args = ListGraphsSchema.parse(rawArgs);
  const assistants = await listAssistants(args.limit);

  if (assistants.length === 0) {
    return {
      content: [
        {
          type: "text",
          text:
            "No graphs/assistants are deployed on LangGraph Platform. " +
            "AppFactory pipeline graphs will appear here once Phase 3 deployment is complete.",
        },
      ],
    };
  }

  const summary = assistants.map((a) => ({
    assistant_id: a.assistant_id,
    graph_id: a.graph_id,
    name: a.name,
    created_at: a.created_at,
  }));

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            graphs: summary,
            count: summary.length,
            note: "Use assistant_id as the assistant_id argument when calling dispatch_pipeline.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

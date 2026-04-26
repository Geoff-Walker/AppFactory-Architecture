import {
  GetInterruptStateSchema,
  UpdateThreadStateSchema,
  ResumeRunSchema,
} from "../schemas.js";
import {
  getThread,
  getThreadState,
  patchThreadState,
  resumeRun,
} from "../clients/langgraph.js";
import type { ToolResult } from "../types.js";

// ---------------------------------------------------------------------------
// Interrupt tools — LangGraph Platform backend
// These tools handle the human-in-the-loop gate pattern.
// ---------------------------------------------------------------------------

export async function getInterruptState(rawArgs: unknown): Promise<ToolResult> {
  const args = GetInterruptStateSchema.parse(rawArgs);

  const [thread, state] = await Promise.all([
    getThread(args.thread_id),
    getThreadState(args.thread_id),
  ]);

  if (thread.status !== "interrupted") {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              thread_id: args.thread_id,
              status: thread.status,
              warning:
                `Thread is not currently interrupted (status: ${thread.status}). ` +
                "This tool is only meaningful when a pipeline is paused at a human gate.",
            },
            null,
            2,
          ),
        },
      ],
    };
  }

  const interrupts = state.tasks.flatMap((t) => t.interrupts ?? []);

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            thread_id: args.thread_id,
            status: "interrupted",
            interrupt_payloads: interrupts.map((i) => ({
              value: i.value,
              resumable: i.resumable,
              node: i.ns,
              when: i.when,
            })),
            current_state: state.values,
            next_nodes: state.next,
            hint:
              "Review the interrupt payloads and current state. " +
              "Optionally call update_thread_state to inject context before resuming. " +
              "Then call resume_run with action 'approve' or 'reject'.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

export async function updateThreadState(rawArgs: unknown): Promise<ToolResult> {
  const args = UpdateThreadStateSchema.parse(rawArgs);

  const updatedState = await patchThreadState(
    args.thread_id,
    args.values,
    args.as_node,
  );

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            thread_id: args.thread_id,
            updated: true,
            new_state_values: updatedState.values,
            next_nodes: updatedState.next,
            note:
              "State updated. The injected values will be visible to the next node when the run resumes. " +
              "Call resume_run when ready to continue.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

export async function resumePipelineRun(rawArgs: unknown): Promise<ToolResult> {
  const args = ResumeRunSchema.parse(rawArgs);

  // The decision object is passed verbatim as the Command resume payload —
  // it becomes the return value of interrupt() in the paused graph node.
  const run = await resumeRun(args.thread_id, args.assistant_id, args.decision);

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            thread_id: args.thread_id,
            run_id: run.run_id,
            decision: args.decision,
            resumed: true,
            note:
              "Pipeline resumed. Use get_run_status with thread_id to poll progress.",
          },
          null,
          2,
        ),
      },
    ],
  };
}

import { z } from "zod";

// ---------------------------------------------------------------------------
// Pipeline tool schemas
// ---------------------------------------------------------------------------

/**
 * Generic dispatch — kept as a fallback for graphs that do not yet have a
 * typed schema. New work should use one of the four typed dispatchers below
 * (dispatch_iterative_dev, dispatch_research_only, dispatch_qa_batch,
 * dispatch_infra_task), which validate required fields at the schema layer
 * and stop callers from accidentally omitting (e.g.) the executor tag.
 */
export const DispatchPipelineSchema = z.object({
  assistant_id: z
    .string()
    .describe("The graph/assistant ID to run, e.g. 'appfactory_pipeline'"),
  input: z
    .record(z.unknown())
    .describe("Input payload passed to the pipeline as the initial state"),
  config: z
    .record(z.unknown())
    .optional()
    .describe("Optional LangGraph run configuration (recursion_limit, etc.)"),
  metadata: z
    .record(z.unknown())
    .optional()
    .describe("Optional metadata tags attached to this run for Langfuse lookup"),
});

// ---------------------------------------------------------------------------
// Typed dispatch schemas — one per graph
//
// Every typed dispatcher accepts the same optional config + metadata pair as
// the generic dispatcher; only the input shape differs by graph. The tool
// handler assembles the validated fields into the LangGraph input payload.
// ---------------------------------------------------------------------------

const ExecutorTagSchema = z
  .enum(["haiku", "claude-dev", "executor:haiku", "executor:claude-dev"])
  .describe(
    "Executor selector. The bare tag (haiku | claude-dev) is canonical; the " +
      "executor: prefix form is the raw Jira label and is normalised by the pipeline.",
  );

const RepoSlugSchema = z
  .string()
  .regex(/^[^/\s]+\/[^/\s]+$/, "Must be 'owner/repo' (no slashes in either side, no whitespace)")
  .describe("GitHub repo slug, owner/repo format, e.g. 'owner/my-project'");

const ProjectKeySchema = z
  .string()
  .min(2)
  .describe("Jira project key, e.g. 'AFT', 'WAL', 'FCB'");

export const DispatchIterativeDevSchema = z.object({
  tickets: z
    .array(
      z.object({
        id: z.string().min(1).describe("Jira ticket ID, e.g. 'AFT-1'"),
        executor: ExecutorTagSchema,
      }),
    )
    .min(1)
    .describe(
      "Ordered list of tickets to build. Each entry carries its own executor tag, " +
        "so mixed batches (haiku + claude-dev in the same batch) route correctly.",
    ),
  repo: RepoSlugSchema,
  project_key: ProjectKeySchema,
  config: z
    .record(z.unknown())
    .optional()
    .describe("Optional LangGraph run configuration (recursion_limit, etc.)"),
  metadata: z
    .record(z.unknown())
    .optional()
    .describe("Optional metadata tags attached to this run for Langfuse lookup"),
});

export const DispatchResearchOnlySchema = z.object({
  question: z
    .string()
    .min(1)
    .describe("The research question to investigate"),
  research_mode: z
    .enum(["quick_hit", "deep_research"])
    .describe(
      "quick_hit = single Research agent run with bounded budget; " +
        "deep_research = exhaustive investigation, requires the operator's interrupt approval at the gate",
    ),
  project_key: ProjectKeySchema,
  config: z
    .record(z.unknown())
    .optional()
    .describe("Optional LangGraph run configuration"),
  metadata: z
    .record(z.unknown())
    .optional()
    .describe("Optional metadata tags for Langfuse lookup"),
});

export const DispatchQaBatchSchema = z.object({
  spec: z
    .string()
    .min(1)
    .describe("The product/feature specification QA is being asked to break into tickets"),
  design_output_location: z
    .string()
    .optional()
    .describe(
      "Optional path or URL to a Design agent handoff folder containing screen specs " +
        "and component inventory. Required only when the spec depends on a prior design output.",
    ),
  project_key: ProjectKeySchema,
  repo: RepoSlugSchema,
  config: z
    .record(z.unknown())
    .optional()
    .describe("Optional LangGraph run configuration"),
  metadata: z
    .record(z.unknown())
    .optional()
    .describe("Optional metadata tags for Langfuse lookup"),
});

export const DispatchInfraTaskSchema = z.object({
  infra_task_description: z
    .string()
    .min(1)
    .describe("What infrastructure operation is being requested. The Infra agent assesses tier from this."),
  infra_context: z
    .string()
    .optional()
    .describe("Additional context — host, prior state, related services, anything the agent needs."),
  config: z
    .record(z.unknown())
    .optional()
    .describe("Optional LangGraph run configuration"),
  metadata: z
    .record(z.unknown())
    .optional()
    .describe("Optional metadata tags for Langfuse lookup"),
});

export const GetRunStatusSchema = z.object({
  thread_id: z
    .string()
    .describe("The thread ID returned by dispatch_pipeline"),
});

export const ListActiveRunsSchema = z.object({
  limit: z
    .number()
    .int()
    .min(1)
    .max(100)
    .optional()
    .default(20)
    .describe("Maximum number of active threads to return (default 20, max 100)"),
});

export const CancelRunSchema = z.object({
  thread_id: z
    .string()
    .describe("The thread ID of the run to cancel"),
  run_id: z
    .string()
    .describe("The run ID to cancel within the thread"),
});

export const ListGraphsSchema = z.object({
  limit: z
    .number()
    .int()
    .min(1)
    .max(100)
    .optional()
    .default(20)
    .describe("Maximum number of assistants/graphs to return (default 20)"),
});

// ---------------------------------------------------------------------------
// Interrupt tool schemas
// ---------------------------------------------------------------------------

export const GetInterruptStateSchema = z.object({
  thread_id: z
    .string()
    .describe("The thread ID paused at a human gate"),
});

export const UpdateThreadStateSchema = z.object({
  thread_id: z
    .string()
    .describe("The thread ID whose state should be updated"),
  values: z
    .record(z.unknown())
    .describe(
      "Key-value pairs to merge into the thread state. These values will be available to the next node when the run resumes.",
    ),
  as_node: z
    .string()
    .optional()
    .describe(
      "Optional — treat the update as if it came from this node name. Useful for injecting context mid-graph.",
    ),
});

export const ResumeRunSchema = z.object({
  thread_id: z
    .string()
    .describe("The thread ID paused at a human gate"),
  assistant_id: z
    .string()
    .describe("The assistant/graph ID that owns this thread"),
  decision: z
    .record(z.unknown())
    .describe(
      "Decision payload — passed verbatim to the pipeline as the interrupt() return value. " +
      "Must match what the interrupt hint asked for. Examples by graph:\n" +
      "  iterative_dev escalate / merge_failed: {\"action\": \"park\"} or {\"action\": \"abort\"}\n" +
      "  qa_batch gate: {\"action\": \"chain\"} or {\"action\": \"stop\"}\n" +
      "  research_only deep_research_needed: {\"action\": \"deep_research\"} or {\"action\": \"accept_partial\"}\n" +
      "  research_only blocked: {\"action\": \"abort\"} or {\"action\": \"continue\", \"instruction\": \"...\"}\n" +
      "  infra_task plan_gate: {\"action\": \"approve\", \"stages\": [1, 2]} or {\"action\": \"reject\"}\n" +
      "  infra_task stage_failed: {\"action\": \"retry\"} | {\"action\": \"manual_and_retry\"} | {\"action\": \"abort\"}\n" +
      "Call get_interrupt_state first to read the exact hint text for this thread.",
    ),
});

// ---------------------------------------------------------------------------
// Observability tool schemas
// ---------------------------------------------------------------------------

export const GetRunTraceSchema = z.object({
  run_id: z
    .string()
    .describe("The run ID from dispatch_pipeline, used as the Langfuse trace ID"),
});

export const GetTokenSpendSchema = z.object({
  run_id: z
    .string()
    .optional()
    .describe("Scope to a specific run. Mutually exclusive with date range."),
  from_date: z
    .string()
    .optional()
    .describe("ISO 8601 date string for range start, e.g. '2026-04-01'"),
  to_date: z
    .string()
    .optional()
    .describe("ISO 8601 date string for range end, e.g. '2026-04-30'"),
}).refine(
  (data) =>
    data.run_id !== undefined ||
    (data.from_date !== undefined && data.to_date !== undefined),
  {
    message: "Provide either run_id or both from_date and to_date",
  },
);

// ---------------------------------------------------------------------------
// Knowledge tool schemas
// ---------------------------------------------------------------------------

export const SearchKnowledgeSchema = z.object({
  query: z
    .string()
    .min(1)
    .describe("Natural language query to embed and search against the operational knowledge base"),
  top_k: z
    .number()
    .int()
    .min(1)
    .max(20)
    .optional()
    .default(5)
    .describe("Number of results to return (default 5, max 20)"),
  filter_agent: z
    .string()
    .optional()
    .describe(
      "Optional — filter results to a specific agent name (e.g. 'design_agent', 'qa_agent')",
    ),
});

// ---------------------------------------------------------------------------
// Inferred TypeScript types from schemas
// ---------------------------------------------------------------------------

export type DispatchPipelineInput = z.infer<typeof DispatchPipelineSchema>;
export type DispatchIterativeDevInput = z.infer<typeof DispatchIterativeDevSchema>;
export type DispatchResearchOnlyInput = z.infer<typeof DispatchResearchOnlySchema>;
export type DispatchQaBatchInput = z.infer<typeof DispatchQaBatchSchema>;
export type DispatchInfraTaskInput = z.infer<typeof DispatchInfraTaskSchema>;
export type GetRunStatusInput = z.infer<typeof GetRunStatusSchema>;
export type ListActiveRunsInput = z.infer<typeof ListActiveRunsSchema>;
export type CancelRunInput = z.infer<typeof CancelRunSchema>;
export type ListGraphsInput = z.infer<typeof ListGraphsSchema>;
export type GetInterruptStateInput = z.infer<typeof GetInterruptStateSchema>;
export type UpdateThreadStateInput = z.infer<typeof UpdateThreadStateSchema>;
export type ResumeRunInput = z.infer<typeof ResumeRunSchema>;
export type GetRunTraceInput = z.infer<typeof GetRunTraceSchema>;
export type GetTokenSpendInput = z.infer<typeof GetTokenSpendSchema>;
export type SearchKnowledgeInput = z.infer<typeof SearchKnowledgeSchema>;

import "dotenv/config";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { zodToJsonSchema } from "zod-to-json-schema";

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
  GetInterruptStateSchema,
  UpdateThreadStateSchema,
  ResumeRunSchema,
  GetRunTraceSchema,
  GetTokenSpendSchema,
  SearchKnowledgeSchema,
} from "./schemas.js";

import {
  dispatchPipeline,
  dispatchIterativeDev,
  dispatchResearchOnly,
  dispatchQaBatch,
  dispatchInfraTask,
  getRunStatus,
  listActiveRuns,
  cancelPipelineRun,
  listGraphs,
} from "./tools/pipeline.js";
import { getInterruptState, updateThreadState, resumePipelineRun } from "./tools/interrupts.js";
import { getRunTrace, getTokenSpend } from "./tools/observability.js";
import { searchKnowledgeTool } from "./tools/knowledge.js";

// ---------------------------------------------------------------------------
// Convert Zod schemas to MCP inputSchema once at startup
// ---------------------------------------------------------------------------

const inputSchemas = {
  dispatch_pipeline: zodToJsonSchema(DispatchPipelineSchema),
  dispatch_iterative_dev: zodToJsonSchema(DispatchIterativeDevSchema),
  dispatch_research_only: zodToJsonSchema(DispatchResearchOnlySchema),
  dispatch_qa_batch: zodToJsonSchema(DispatchQaBatchSchema),
  dispatch_infra_task: zodToJsonSchema(DispatchInfraTaskSchema),
  get_run_status: zodToJsonSchema(GetRunStatusSchema),
  list_active_runs: zodToJsonSchema(ListActiveRunsSchema),
  cancel_run: zodToJsonSchema(CancelRunSchema),
  list_graphs: zodToJsonSchema(ListGraphsSchema),
  get_interrupt_state: zodToJsonSchema(GetInterruptStateSchema),
  update_thread_state: zodToJsonSchema(UpdateThreadStateSchema),
  resume_run: zodToJsonSchema(ResumeRunSchema),
  get_run_trace: zodToJsonSchema(GetRunTraceSchema),
  get_token_spend: zodToJsonSchema(GetTokenSpendSchema),
  search_knowledge: zodToJsonSchema(SearchKnowledgeSchema),
} as const;

// ---------------------------------------------------------------------------
// Server setup
// ---------------------------------------------------------------------------

const server = new Server(
  { name: "langgraph-mcp", version: "1.0.0" },
  { capabilities: { tools: {} } },
);

// ---------------------------------------------------------------------------
// Tool registry
// ---------------------------------------------------------------------------

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    // --- Pipeline tools ---
    {
      name: "dispatch_iterative_dev",
      description:
        "Dispatch the iterative_dev graph — build an ordered batch of Jira tickets. " +
        "Each ticket carries its own executor tag, so mixed batches (haiku + claude-dev) " +
        "route correctly. Required: tickets [{id, executor}], repo (owner/repo), project_key. " +
        "Prefer this over dispatch_pipeline — required fields are validated at the schema layer.",
      inputSchema: inputSchemas.dispatch_iterative_dev,
    },
    {
      name: "dispatch_research_only",
      description:
        "Dispatch the research_only graph — investigate a single question. " +
        "Required: question, research_mode (quick_hit | deep_research), project_key. " +
        "deep_research requires the operator's interrupt approval at the gate. " +
        "Prefer this over dispatch_pipeline.",
      inputSchema: inputSchemas.dispatch_research_only,
    },
    {
      name: "dispatch_qa_batch",
      description:
        "Dispatch the qa_batch graph — break a spec into ordered, executor-tagged Jira tickets. " +
        "Required: spec, project_key, repo. Optional: design_output_location (path/URL to a " +
        "Design agent handoff). Prefer this over dispatch_pipeline.",
      inputSchema: inputSchemas.dispatch_qa_batch,
    },
    {
      name: "dispatch_infra_task",
      description:
        "Dispatch the infra_task graph — execute a TrueNAS / aidev VM operation under the " +
        "tiered approval model. Required: infra_task_description. Optional: infra_context. " +
        "Tier 2 operations always interrupt for plan approval before any command runs. " +
        "Prefer this over dispatch_pipeline.",
      inputSchema: inputSchemas.dispatch_infra_task,
    },
    {
      name: "dispatch_pipeline",
      description:
        "GENERIC FALLBACK — dispatch any LangGraph assistant by ID with a free-form input dict. " +
        "Use the typed dispatchers above (dispatch_iterative_dev / dispatch_research_only / " +
        "dispatch_qa_batch / dispatch_infra_task) for the standard graphs — they validate " +
        "required fields and prevent missing-tag classes of bug. Use this only for graphs " +
        "that do not yet have a typed schema. " +
        "Returns thread_id and run_id. The pipeline runs asynchronously — poll with get_run_status.",
      inputSchema: inputSchemas.dispatch_pipeline,
    },
    {
      name: "get_run_status",
      description:
        "Get the current status of a pipeline thread. Returns status (idle/busy/interrupted/error), " +
        "current nodes, and interrupt payloads if paused at a human gate. " +
        "Poll this after dispatch_pipeline until status is 'idle' or 'interrupted'.",
      inputSchema: inputSchemas.get_run_status,
    },
    {
      name: "list_active_runs",
      description:
        "List all pipeline threads currently in a non-terminal state (busy or interrupted). " +
        "Use this to see what is running or waiting for human input across all pipelines.",
      inputSchema: inputSchemas.list_active_runs,
    },
    {
      name: "cancel_run",
      description:
        "Cancel a running pipeline thread. Sends a cancel signal — poll get_run_status to confirm termination. " +
        "Use this if a pipeline is stuck, producing bad output, or no longer needed.",
      inputSchema: inputSchemas.cancel_run,
    },
    {
      name: "list_graphs",
      description:
        "List all pipeline graphs (assistants) deployed on LangGraph Platform. " +
        "Use this to discover available assistant_ids before calling dispatch_pipeline. " +
        "Returns an empty list if LangGraph Platform is not yet deployed (Phase 3).",
      inputSchema: inputSchemas.list_graphs,
    },
    // --- Interrupt tools ---
    {
      name: "get_interrupt_state",
      description:
        "Get the full state of a pipeline thread paused at a human gate. " +
        "Returns the interrupt payload (what the pipeline is asking), the current state dict, " +
        "and which nodes are waiting. Call this when get_run_status reports status='interrupted'.",
      inputSchema: inputSchemas.get_interrupt_state,
    },
    {
      name: "update_thread_state",
      description:
        "Inject or modify values in a paused pipeline thread's state before resuming. " +
        "Use this to correct scope, add missing context, or remove items. " +
        "The injected values are available to the next node after resume_run is called.",
      inputSchema: inputSchemas.update_thread_state,
    },
    {
      name: "resume_run",
      description:
        "Resume a pipeline thread paused at a human gate. " +
        "Pass a 'decision' object that matches what the interrupt's hint asked for — " +
        "it is forwarded verbatim as the interrupt() return value in the paused node. " +
        "Call get_interrupt_state first to read the hint. " +
        "Example decisions: {action:'park'}, {action:'chain'}, {action:'approve', stages:[1,2]}, " +
        "{action:'continue', instruction:'...'}.",
      inputSchema: inputSchemas.resume_run,
    },
    // --- Observability tools ---
    {
      name: "get_run_trace",
      description:
        "Aggregate every Langfuse trace for a pipeline run, identified by run_id. " +
        "The pipeline sets Langfuse sessionId = run_id on each @observe() node, so this tool " +
        "queries by sessionId and rolls up node spans, LLM calls, token counts, and cost across " +
        "all traces for that run. Returns a clear message when no traces match the run_id.",
      inputSchema: inputSchemas.get_run_trace,
    },
    {
      name: "get_token_spend",
      description:
        "Query token usage and estimated cost from Langfuse. " +
        "Provide run_id for per-run cost, or from_date + to_date for a date range summary. " +
        "Returns totals and a per-model breakdown.",
      inputSchema: inputSchemas.get_token_spend,
    },
    // --- Knowledge tool ---
    {
      name: "search_knowledge",
      description:
        "Semantic search over the AppFactory operational knowledge base in Qdrant. " +
        "Embeds the query via OpenAI text-embedding-3-small and returns the top-k most similar entries. " +
        "Each result includes the agent, task description, output summary, run_id, and relevance score. " +
        "Returns a clear message if the collection does not exist yet (expected before Phase 4).",
      inputSchema: inputSchemas.search_knowledge,
    },
  ],
}));

// ---------------------------------------------------------------------------
// Tool dispatch
// ---------------------------------------------------------------------------

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case "dispatch_pipeline":
        return await dispatchPipeline(args);
      case "dispatch_iterative_dev":
        return await dispatchIterativeDev(args);
      case "dispatch_research_only":
        return await dispatchResearchOnly(args);
      case "dispatch_qa_batch":
        return await dispatchQaBatch(args);
      case "dispatch_infra_task":
        return await dispatchInfraTask(args);
      case "get_run_status":
        return await getRunStatus(args);
      case "list_active_runs":
        return await listActiveRuns(args);
      case "cancel_run":
        return await cancelPipelineRun(args);
      case "list_graphs":
        return await listGraphs(args);
      case "get_interrupt_state":
        return await getInterruptState(args);
      case "update_thread_state":
        return await updateThreadState(args);
      case "resume_run":
        return await resumePipelineRun(args);
      case "get_run_trace":
        return await getRunTrace(args);
      case "get_token_spend":
        return await getTokenSpend(args);
      case "search_knowledge":
        return await searchKnowledgeTool(args);
      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return {
      content: [{ type: "text", text: `Error: ${message}` }],
      isError: true,
    };
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const transport = new StdioServerTransport();
await server.connect(transport);

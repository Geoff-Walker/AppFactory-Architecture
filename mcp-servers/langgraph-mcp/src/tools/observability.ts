import { GetRunTraceSchema, GetTokenSpendSchema } from "../schemas.js";
import { getRunTraceSummary, getUsageMetrics } from "../clients/langfuse.js";
import type { ToolResult } from "../types.js";

// ---------------------------------------------------------------------------
// Observability tools — Langfuse backend
// Traces are keyed by LangGraph run_id via Langfuse sessionId — the pipeline
// calls apply_run_id_to_trace() at node entry to set session.id on each span.
// All tools handle missing/empty data gracefully.
// ---------------------------------------------------------------------------

export async function getRunTrace(rawArgs: unknown): Promise<ToolResult> {
  const args = GetRunTraceSchema.parse(rawArgs);

  const summary = await getRunTraceSummary(args.run_id);

  if (!summary) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              run_id: args.run_id,
              found: false,
              note:
                `No Langfuse traces found for run_id '${args.run_id}'. ` +
                "Check that the pipeline ran recently, that Langfuse is configured on the VM, " +
                "and that apply_run_id_to_trace() is being called from the pipeline nodes " +
                "(every @observe()-decorated node should set session.id = run_id).",
            },
            null,
            2,
          ),
        },
      ],
    };
  }

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            run_id: summary.run_id,
            trace_count: summary.trace_count,
            trace_ids: summary.trace_ids,
            observation_count: summary.observations.length,
            total_input_tokens: summary.total_input_tokens,
            total_output_tokens: summary.total_output_tokens,
            total_tokens: summary.total_tokens,
            estimated_cost_usd: summary.estimated_cost_usd,
            observations: summary.observations,
          },
          null,
          2,
        ),
      },
    ],
  };
}

export async function getTokenSpend(rawArgs: unknown): Promise<ToolResult> {
  const args = GetTokenSpendSchema.parse(rawArgs);

  // If run_id provided, aggregate all traces for that run (sessionId match).
  if (args.run_id !== undefined) {
    const summary = await getRunTraceSummary(args.run_id);

    if (!summary) {
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              {
                run_id: args.run_id,
                found: false,
                note:
                  `No Langfuse traces found for run_id '${args.run_id}'. ` +
                  "Token spend is only available after a pipeline run has emitted " +
                  "traces with session.id = run_id (set by apply_run_id_to_trace()).",
              },
              null,
              2,
            ),
          },
        ],
      };
    }

    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(summary, null, 2),
        },
      ],
    };
  }

  // Date range query
  const metrics = await getUsageMetrics(args.from_date, args.to_date);

  if (metrics.length === 0) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              from_date: args.from_date,
              to_date: args.to_date,
              found: false,
              note:
                "No usage metrics found for the requested date range. " +
                "This is expected if no pipelines have run yet.",
            },
            null,
            2,
          ),
        },
      ],
    };
  }

  const totals = metrics.reduce(
    (acc, m) => {
      acc.total_input_tokens += m.inputUsage;
      acc.total_output_tokens += m.outputUsage;
      acc.total_tokens += m.totalUsage;
      acc.total_cost_usd += m.totalCost;
      return acc;
    },
    { total_input_tokens: 0, total_output_tokens: 0, total_tokens: 0, total_cost_usd: 0 },
  );

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            from_date: args.from_date,
            to_date: args.to_date,
            summary: totals,
            by_model: metrics,
          },
          null,
          2,
        ),
      },
    ],
  };
}

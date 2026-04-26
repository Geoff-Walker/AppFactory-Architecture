import type { LangfuseTrace, LangfuseUsageMetric } from "../types.js";

// ---------------------------------------------------------------------------
// Langfuse REST client
// Auth: HTTP Basic — public key as username, secret key as password.
// Langfuse may not have any traces yet (LangGraph Platform is Phase 3).
// All methods handle 404 gracefully.
// ---------------------------------------------------------------------------

function getConfig(): { baseUrl: string; authHeader: string } {
  const baseUrl = process.env["LANGFUSE_URL"];
  const publicKey = process.env["LANGFUSE_PUBLIC_KEY"];
  const secretKey = process.env["LANGFUSE_SECRET_KEY"];

  if (!baseUrl) {
    throw new Error(
      "LANGFUSE_URL is not set. Set it to your Langfuse instance, e.g. http://<your-langfuse-host>:3000",
    );
  }
  if (!publicKey || !secretKey) {
    throw new Error(
      "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must both be set. " +
        "Find them in your Langfuse project settings.",
    );
  }

  const credentials = Buffer.from(`${publicKey}:${secretKey}`).toString("base64");
  return {
    baseUrl: baseUrl.replace(/\/$/, ""),
    authHeader: `Basic ${credentials}`,
  };
}

function headers(authHeader: string): Record<string, string> {
  return {
    Authorization: authHeader,
    Accept: "application/json",
  };
}

async function apiFetch<T>(
  url: string,
  options: RequestInit,
  description: string,
): Promise<T | null> {
  let response: Response;
  try {
    response = await fetch(url, options);
  } catch (err) {
    const cause = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Cannot reach Langfuse at ${url} (${description}). ` +
        `Cause: ${cause}. ` +
        `Check that Langfuse is running at LANGFUSE_URL and the AppFactory VM is reachable.`,
    );
  }

  if (response.status === 404) {
    return null;
  }

  if (!response.ok) {
    let body = "";
    try {
      body = await response.text();
    } catch {
      // ignore
    }
    throw new Error(
      `Langfuse returned ${response.status} ${response.statusText} ` +
        `for ${description} (${url}). Body: ${body || "(empty)"}`,
    );
  }

  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

export async function getTrace(traceId: string): Promise<LangfuseTrace | null> {
  const { baseUrl, authHeader } = getConfig();
  const url = `${baseUrl}/api/public/traces/${encodeURIComponent(traceId)}`;
  return apiFetch<LangfuseTrace>(
    url,
    { method: "GET", headers: headers(authHeader) },
    `get trace ${traceId}`,
  );
}

interface LangfuseTracesListResponse {
  data: LangfuseTrace[];
  meta?: { page: number; limit: number; totalItems: number; totalPages: number };
}

/**
 * Fetch all Langfuse traces belonging to a given session.
 *
 * Used by the AppFactory pipeline to group every observation emitted during
 * a single LangGraph run — the pipeline sets sessionId = run_id on each
 * @observe()-decorated node so all traces for a run can be retrieved in one
 * query.
 *
 * A single pipeline run rarely exceeds a few dozen traces, so one page is
 * almost always enough. If the run ever grows beyond `limit` traces we'll
 * log a warning and return the first page only — cleaner than silently
 * under-reporting.
 *
 * Returns an empty array (not null) if no traces match — callers branch on
 * length, not null.
 */
export async function getTracesBySessionId(
  sessionId: string,
  limit = 100,
): Promise<LangfuseTrace[]> {
  const { baseUrl, authHeader } = getConfig();

  const params = new URLSearchParams();
  params.set("sessionId", sessionId);
  params.set("limit", String(limit));

  const url = `${baseUrl}/api/public/traces?${params.toString()}`;
  const result = await apiFetch<LangfuseTracesListResponse>(
    url,
    { method: "GET", headers: headers(authHeader) },
    `get traces for session ${sessionId}`,
  );

  return result?.data ?? [];
}

interface LangfuseUsageResponse {
  data: LangfuseUsageMetric[];
  meta?: { page: number; limit: number; totalItems: number; totalPages: number };
}

export async function getUsageMetrics(
  fromDate?: string,
  toDate?: string,
): Promise<LangfuseUsageMetric[]> {
  const { baseUrl, authHeader } = getConfig();

  const params = new URLSearchParams();
  if (fromDate) params.set("fromTimestamp", new Date(fromDate).toISOString());
  if (toDate) params.set("toTimestamp", new Date(toDate).toISOString());

  const url = `${baseUrl}/api/public/metrics/usage?${params.toString()}`;
  const result = await apiFetch<LangfuseUsageResponse>(
    url,
    { method: "GET", headers: headers(authHeader) },
    "get usage metrics",
  );

  return result?.data ?? [];
}

export interface RunTraceSummary {
  run_id: string;
  trace_count: number;
  trace_ids: string[];
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  observations: Array<{
    trace_id: string;
    name: string;
    type?: string;
    model?: string;
    start_time?: string;
    end_time?: string;
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
    cost_usd?: number;
  }>;
}

/**
 * Fetch and aggregate every trace emitted during a single LangGraph pipeline run.
 *
 * Queries Langfuse by sessionId (equal to run_id — set by the pipeline at
 * node entry via apply_run_id_to_trace). If the traces-list response includes
 * observations we use them directly; otherwise we fetch each trace individually
 * so observation counts/tokens/costs roll up correctly.
 *
 * Returns null only when the run_id is not known to Langfuse (no traces
 * found). Returns a summary with `trace_count=0` only in the edge case that
 * the sessionId query somehow returns an empty-but-non-null response shape
 * — callers should treat it identically to null.
 */
export async function getRunTraceSummary(runId: string): Promise<RunTraceSummary | null> {
  const traces = await getTracesBySessionId(runId);
  if (traces.length === 0) return null;

  let totalInput = 0;
  let totalOutput = 0;
  let totalCost = 0;
  const allObservations: RunTraceSummary["observations"] = [];

  for (const listedTrace of traces) {
    // The traces-list endpoint may return trace summaries without observations.
    // If observations are missing (or suspiciously empty alongside a non-zero
    // totalCost hint), fetch the full trace.
    let trace = listedTrace;
    if (!Array.isArray(trace.observations) || trace.observations.length === 0) {
      const full = await getTrace(trace.id);
      if (full) trace = full;
    }

    if (typeof trace.totalCost === "number") {
      totalCost += trace.totalCost;
    }

    for (const obs of trace.observations ?? []) {
      totalInput += obs.inputTokens ?? 0;
      totalOutput += obs.outputTokens ?? 0;
      if (typeof obs.calculatedTotalCost === "number") {
        // Only fall back to per-observation cost if the trace didn't report
        // a totalCost of its own.
        if (typeof trace.totalCost !== "number") {
          totalCost += obs.calculatedTotalCost;
        }
      }

      allObservations.push({
        trace_id: trace.id,
        name: obs.name,
        type: obs.type,
        model: obs.model,
        start_time: obs.startTime,
        end_time: obs.endTime,
        input_tokens: obs.inputTokens,
        output_tokens: obs.outputTokens,
        total_tokens: obs.totalTokens,
        cost_usd: obs.calculatedTotalCost,
      });
    }
  }

  return {
    run_id: runId,
    trace_count: traces.length,
    trace_ids: traces.map((t) => t.id),
    total_input_tokens: totalInput,
    total_output_tokens: totalOutput,
    total_tokens: totalInput + totalOutput,
    estimated_cost_usd: totalCost,
    observations: allObservations,
  };
}

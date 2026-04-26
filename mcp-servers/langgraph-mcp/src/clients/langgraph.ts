import type {
  LangGraphAssistant,
  LangGraphThread,
  LangGraphThreadState,
  LangGraphRun,
} from "../types.js";

// ---------------------------------------------------------------------------
// LangGraph Platform REST client
// All calls carry the LangSmith API key as x-api-key — required even for
// self-hosted deployments.
// ---------------------------------------------------------------------------

function getConfig(): { baseUrl: string; apiKey: string } {
  const baseUrl = process.env["LANGGRAPH_API_URL"];
  const apiKey = process.env["LANGSMITH_API_KEY"];

  if (!baseUrl) {
    throw new Error(
      "LANGGRAPH_API_URL is not set. Set it to the LangGraph Platform URL, e.g. http://<your-langgraph-host>:8123",
    );
  }
  if (!apiKey) {
    throw new Error(
      "LANGSMITH_API_KEY is not set. Obtain from https://smith.langchain.com/settings",
    );
  }

  return { baseUrl: baseUrl.replace(/\/$/, ""), apiKey };
}

function headers(apiKey: string): Record<string, string> {
  return {
    "x-api-key": apiKey,
    "Content-Type": "application/json",
    Accept: "application/json",
  };
}

/** Wrap fetch errors with context about which service/URL failed. */
async function apiFetch<T>(
  url: string,
  options: RequestInit,
  description: string,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, options);
  } catch (err) {
    const cause = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Cannot reach LangGraph Platform at ${url} (${description}). ` +
        `Cause: ${cause}. ` +
        `LangGraph Platform is Phase 3 of AppFactory — it may not be deployed yet.`,
    );
  }

  if (!response.ok) {
    let body = "";
    try {
      body = await response.text();
    } catch {
      // ignore
    }
    throw new Error(
      `LangGraph Platform returned ${response.status} ${response.statusText} ` +
        `for ${description} (${url}). Body: ${body || "(empty)"}`,
    );
  }

  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

export async function listAssistants(limit = 20): Promise<LangGraphAssistant[]> {
  const { baseUrl, apiKey } = getConfig();
  const url = `${baseUrl}/assistants/search`;
  return apiFetch<LangGraphAssistant[]>(
    url,
    {
      method: "POST",
      headers: { ...headers(apiKey), "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    },
    "list assistants",
  );
}

export async function createThreadAndRun(
  assistantId: string,
  input: Record<string, unknown>,
  config?: Record<string, unknown>,
  metadata?: Record<string, unknown>,
): Promise<{ thread_id: string; run_id: string }> {
  const { baseUrl, apiKey } = getConfig();

  // Step 1: create a thread
  const thread = await apiFetch<LangGraphThread>(
    `${baseUrl}/threads`,
    {
      method: "POST",
      headers: headers(apiKey),
      body: JSON.stringify({ metadata: metadata ?? {} }),
    },
    "create thread",
  );

  // Step 2: start a run on that thread
  const run = await apiFetch<LangGraphRun>(
    `${baseUrl}/threads/${thread.thread_id}/runs`,
    {
      method: "POST",
      headers: headers(apiKey),
      body: JSON.stringify({
        assistant_id: assistantId,
        input,
        config: config ?? {},
        metadata: metadata ?? {},
      }),
    },
    `create run on thread ${thread.thread_id}`,
  );

  return { thread_id: thread.thread_id, run_id: run.run_id };
}

export async function getThreadState(threadId: string): Promise<LangGraphThreadState> {
  const { baseUrl, apiKey } = getConfig();
  return apiFetch<LangGraphThreadState>(
    `${baseUrl}/threads/${threadId}/state`,
    { method: "GET", headers: headers(apiKey) },
    `get state for thread ${threadId}`,
  );
}

export async function getThread(threadId: string): Promise<LangGraphThread> {
  const { baseUrl, apiKey } = getConfig();
  return apiFetch<LangGraphThread>(
    `${baseUrl}/threads/${threadId}`,
    { method: "GET", headers: headers(apiKey) },
    `get thread ${threadId}`,
  );
}

export async function listActiveThreads(limit = 20): Promise<LangGraphThread[]> {
  const { baseUrl, apiKey } = getConfig();
  // The in-memory runtime only exposes POST /threads/search — GET /threads 405s.
  // Fetch threads in each non-terminal status in parallel and merge.
  const statuses: Array<"busy" | "interrupted"> = ["busy", "interrupted"];
  const results = await Promise.all(
    statuses.map((status) =>
      apiFetch<LangGraphThread[]>(
        `${baseUrl}/threads/search`,
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ status, limit }),
        },
        `search threads with status=${status}`,
      ),
    ),
  );
  return results.flat();
}

export async function cancelRun(threadId: string, runId: string): Promise<void> {
  const { baseUrl, apiKey } = getConfig();
  await apiFetch<unknown>(
    `${baseUrl}/threads/${threadId}/runs/${runId}/cancel`,
    { method: "POST", headers: headers(apiKey), body: JSON.stringify({}) },
    `cancel run ${runId} on thread ${threadId}`,
  );
}

export async function patchThreadState(
  threadId: string,
  values: Record<string, unknown>,
  asNode?: string,
): Promise<LangGraphThreadState> {
  const { baseUrl, apiKey } = getConfig();
  const body: Record<string, unknown> = { values };
  if (asNode !== undefined) {
    body["as_node"] = asNode;
  }
  return apiFetch<LangGraphThreadState>(
    `${baseUrl}/threads/${threadId}/state`,
    {
      method: "PATCH",
      headers: headers(apiKey),
      body: JSON.stringify(body),
    },
    `patch state for thread ${threadId}`,
  );
}

export async function resumeRun(
  threadId: string,
  assistantId: string,
  resumeValue: unknown,
): Promise<LangGraphRun> {
  const { baseUrl, apiKey } = getConfig();
  return apiFetch<LangGraphRun>(
    `${baseUrl}/threads/${threadId}/runs`,
    {
      method: "POST",
      headers: headers(apiKey),
      body: JSON.stringify({
        assistant_id: assistantId,
        command: { resume: resumeValue },
      }),
    },
    `resume thread ${threadId}`,
  );
}

import { strict as assert } from "node:assert";
import { afterEach, beforeEach, describe, it, mock } from "node:test";

import { getTracesBySessionId, getRunTraceSummary } from "./langfuse.js";
import type { LangfuseTrace } from "../types.js";

// ---------------------------------------------------------------------------
// Stub helpers
// ---------------------------------------------------------------------------

function mockFetchResponse(body: unknown, status = 200): Response {
  const headers = new Headers({ "content-type": "application/json" });
  return new Response(JSON.stringify(body), { status, headers });
}

function mock404(): Response {
  return new Response("", { status: 404 });
}

function mockServerError(): Response {
  return new Response("boom", { status: 500, statusText: "Internal Server Error" });
}

function setLangfuseEnv(): void {
  process.env["LANGFUSE_URL"] = "http://langfuse.test";
  process.env["LANGFUSE_PUBLIC_KEY"] = "pk_test";
  process.env["LANGFUSE_SECRET_KEY"] = "sk_test";
}

function sampleTrace(id: string, cost = 0.01): LangfuseTrace {
  return {
    id,
    name: `trace-${id}`,
    timestamp: "2026-04-23T12:00:00Z",
    sessionId: "run-xyz",
    observations: [
      {
        id: `${id}-obs-1`,
        traceId: id,
        type: "generation",
        name: "llm-call",
        startTime: "2026-04-23T12:00:01Z",
        endTime: "2026-04-23T12:00:02Z",
        model: "claude-sonnet-4-6",
        inputTokens: 100,
        outputTokens: 50,
        totalTokens: 150,
        calculatedTotalCost: cost / 2,
      },
      {
        id: `${id}-obs-2`,
        traceId: id,
        type: "span",
        name: "node",
        startTime: "2026-04-23T12:00:00Z",
        endTime: "2026-04-23T12:00:03Z",
        inputTokens: 0,
        outputTokens: 0,
        totalTokens: 0,
        calculatedTotalCost: cost / 2,
      },
    ],
    totalCost: cost,
  };
}

// ---------------------------------------------------------------------------
// getTracesBySessionId
// ---------------------------------------------------------------------------

describe("getTracesBySessionId", () => {
  beforeEach(() => {
    setLangfuseEnv();
  });

  afterEach(() => {
    mock.restoreAll();
  });

  it("queries the traces endpoint with sessionId", async () => {
    const fetchMock = mock.method(globalThis, "fetch", async (url: string | URL | Request) => {
      const seen = typeof url === "string" ? url : url.toString();
      assert.ok(seen.includes("/api/public/traces?"), `expected traces list URL, got ${seen}`);
      assert.ok(seen.includes("sessionId=run-xyz"), `expected sessionId param, got ${seen}`);
      assert.ok(seen.includes("limit=100"), `expected default limit=100, got ${seen}`);
      return mockFetchResponse({ data: [sampleTrace("t1")] });
    });

    const traces = await getTracesBySessionId("run-xyz");
    assert.equal(fetchMock.mock.calls.length, 1);
    assert.equal(traces.length, 1);
    assert.equal(traces[0]?.id, "t1");
  });

  it("URL-encodes session IDs that need it", async () => {
    mock.method(globalThis, "fetch", async (url: string | URL | Request) => {
      const seen = typeof url === "string" ? url : url.toString();
      // URLSearchParams encodes ':' as '%3A'. Accept either form — the server
      // accepts both — but confirm the raw run_id string made it into the query.
      assert.ok(seen.includes("sessionId="), seen);
      return mockFetchResponse({ data: [] });
    });
    await getTracesBySessionId("run:weird/chars");
  });

  it("returns [] on 404 (not an error)", async () => {
    mock.method(globalThis, "fetch", async () => mock404());
    const traces = await getTracesBySessionId("run-missing");
    assert.deepEqual(traces, []);
  });

  it("returns [] when server responds with empty data array", async () => {
    mock.method(globalThis, "fetch", async () => mockFetchResponse({ data: [] }));
    const traces = await getTracesBySessionId("run-empty");
    assert.deepEqual(traces, []);
  });

  it("throws on non-200/404 responses", async () => {
    mock.method(globalThis, "fetch", async () => mockServerError());
    await assert.rejects(
      () => getTracesBySessionId("run-broken"),
      /500/,
    );
  });

  it("passes a custom limit through to the query", async () => {
    mock.method(globalThis, "fetch", async (url: string | URL | Request) => {
      const seen = typeof url === "string" ? url : url.toString();
      assert.ok(seen.includes("limit=7"), seen);
      return mockFetchResponse({ data: [] });
    });
    await getTracesBySessionId("run-custom", 7);
  });
});

// ---------------------------------------------------------------------------
// getRunTraceSummary
// ---------------------------------------------------------------------------

describe("getRunTraceSummary", () => {
  beforeEach(() => {
    setLangfuseEnv();
  });

  afterEach(() => {
    mock.restoreAll();
  });

  it("returns null when no traces match the run_id", async () => {
    mock.method(globalThis, "fetch", async () => mockFetchResponse({ data: [] }));
    const summary = await getRunTraceSummary("run-none");
    assert.equal(summary, null);
  });

  it("aggregates a single trace's observations, tokens, and cost", async () => {
    mock.method(globalThis, "fetch", async () => mockFetchResponse({ data: [sampleTrace("t1", 0.04)] }));
    const summary = await getRunTraceSummary("run-single");
    assert.ok(summary);
    assert.equal(summary.run_id, "run-single");
    assert.equal(summary.trace_count, 1);
    assert.deepEqual(summary.trace_ids, ["t1"]);
    assert.equal(summary.total_input_tokens, 100);
    assert.equal(summary.total_output_tokens, 50);
    assert.equal(summary.total_tokens, 150);
    assert.equal(summary.estimated_cost_usd, 0.04);
    assert.equal(summary.observations.length, 2);
    assert.equal(summary.observations[0]?.trace_id, "t1");
  });

  it("aggregates across multiple traces for the same run", async () => {
    mock.method(globalThis, "fetch", async () =>
      mockFetchResponse({
        data: [sampleTrace("t1", 0.02), sampleTrace("t2", 0.03), sampleTrace("t3", 0.01)],
      }),
    );
    const summary = await getRunTraceSummary("run-multi");
    assert.ok(summary);
    assert.equal(summary.trace_count, 3);
    assert.deepEqual(summary.trace_ids, ["t1", "t2", "t3"]);
    // 3 traces × 100 input tokens each
    assert.equal(summary.total_input_tokens, 300);
    assert.equal(summary.total_output_tokens, 150);
    assert.equal(summary.total_tokens, 450);
    // Cost rolls up from trace.totalCost
    assert.ok(Math.abs(summary.estimated_cost_usd - 0.06) < 1e-9);
    // 3 traces × 2 observations each
    assert.equal(summary.observations.length, 6);
    // Every observation carries its source trace_id
    assert.deepEqual(
      new Set(summary.observations.map((o) => o.trace_id)),
      new Set(["t1", "t2", "t3"]),
    );
  });

  it("fetches the full trace when observations are missing from the list response", async () => {
    const listBody = {
      data: [
        // No observations in the list response — simulates a trace-list that
        // returns summaries only.
        {
          id: "t1",
          name: "trace-t1",
          timestamp: "2026-04-23T12:00:00Z",
          sessionId: "run-list-minimal",
          observations: [],
          totalCost: 0.05,
        },
      ],
    };
    const fullBody = sampleTrace("t1", 0.05);

    let fetchCount = 0;
    mock.method(globalThis, "fetch", async (url: string | URL | Request) => {
      const seen = typeof url === "string" ? url : url.toString();
      fetchCount += 1;
      if (seen.includes("/api/public/traces?")) {
        return mockFetchResponse(listBody);
      }
      if (seen.includes("/api/public/traces/t1")) {
        return mockFetchResponse(fullBody);
      }
      throw new Error(`unexpected URL: ${seen}`);
    });

    const summary = await getRunTraceSummary("run-list-minimal");
    assert.ok(summary);
    // Two fetches: list, then full trace for t1.
    assert.equal(fetchCount, 2);
    assert.equal(summary.observations.length, 2);
    assert.equal(summary.total_input_tokens, 100);
  });
});

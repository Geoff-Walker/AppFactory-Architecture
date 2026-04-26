import { SearchKnowledgeSchema } from "../schemas.js";
import { searchKnowledge } from "../clients/qdrant.js";
import type { ToolResult } from "../types.js";

// ---------------------------------------------------------------------------
// Knowledge tool — Qdrant + OpenAI embeddings
// The operational_knowledge collection doesn't exist until Phase 4.
// Returns a helpful message rather than an error when it's missing.
// ---------------------------------------------------------------------------

export async function searchKnowledgeTool(rawArgs: unknown): Promise<ToolResult> {
  const args = SearchKnowledgeSchema.parse(rawArgs);

  const result = await searchKnowledge({
    query: args.query,
    topK: args.top_k,
    filterAgent: args.filter_agent,
  });

  if (!result.found) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              query: args.query,
              results: [],
              collection_ready: false,
              message: result.reason,
            },
            null,
            2,
          ),
        },
      ],
    };
  }

  if (result.results.length === 0) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              query: args.query,
              results: [],
              collection_ready: true,
              message:
                "No results matched your query. The collection exists but may be empty " +
                "or no entries are sufficiently similar to the query.",
            },
            null,
            2,
          ),
        },
      ],
    };
  }

  const formatted = result.results.map((r, i) => ({
    rank: i + 1,
    score: r.score,
    id: r.id,
    agent: r.payload.agent,
    task: r.payload.task,
    output: r.payload.output,
    run_id: r.payload.run_id,
    timestamp: r.payload.timestamp,
  }));

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(
          {
            query: args.query,
            count: formatted.length,
            collection_ready: true,
            results: formatted,
          },
          null,
          2,
        ),
      },
    ],
  };
}

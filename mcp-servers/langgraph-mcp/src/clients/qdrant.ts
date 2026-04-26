import { QdrantClient } from "@qdrant/js-client-rest";
import OpenAI from "openai";
import type { KnowledgeSearchResult } from "../types.js";

// ---------------------------------------------------------------------------
// Qdrant client + OpenAI embedding calls
//
// The operational_knowledge collection does not exist yet (Phase 4).
// search() handles this gracefully — callers receive a descriptive message
// rather than an exception.
//
// Embedding model: text-embedding-3-small (1536 dims)
// When the server GPU is live with Ollama, swap EMBEDDING_MODEL to
// nomic-embed-text (768 dims) and rebuild the collection. The abstraction
// here means that swap is a config change only.
// ---------------------------------------------------------------------------

const COLLECTION_NAME = "operational_knowledge";
const EMBEDDING_DIMS = 1536;

function getQdrantClient(): QdrantClient {
  const url = process.env["QDRANT_URL"];
  if (!url) {
    throw new Error(
      "QDRANT_URL is not set. Set it to your Qdrant instance, e.g. http://<your-qdrant-host>:6333",
    );
  }
  return new QdrantClient({ url });
}

function getOpenAIClient(): OpenAI {
  const apiKey = process.env["OPENAI_API_KEY"];
  if (!apiKey) {
    throw new Error(
      "OPENAI_API_KEY is not set. Required for generating embeddings via text-embedding-3-small.",
    );
  }
  return new OpenAI({ apiKey });
}

// ---------------------------------------------------------------------------
// Embedding abstraction — swap model here when GPU is live
// ---------------------------------------------------------------------------

async function embedQuery(text: string): Promise<number[]> {
  const openai = getOpenAIClient();
  const response = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: text,
    dimensions: EMBEDDING_DIMS,
  });

  const embedding = response.data[0]?.embedding;
  if (!embedding) {
    throw new Error(
      "OpenAI embeddings API returned no embedding data. " +
        "Check that OPENAI_API_KEY is valid and has Model capabilities: Write permission.",
    );
  }

  return embedding;
}

// ---------------------------------------------------------------------------
// Collection existence check — returns false rather than throwing
// ---------------------------------------------------------------------------

async function collectionExists(client: QdrantClient, name: string): Promise<boolean> {
  try {
    const collections = await client.getCollections();
    return collections.collections.some((c) => c.name === name);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface SearchKnowledgeOptions {
  query: string;
  topK: number;
  filterAgent?: string;
}

export async function searchKnowledge(
  options: SearchKnowledgeOptions,
): Promise<
  | { found: true; results: KnowledgeSearchResult[] }
  | { found: false; reason: string }
> {
  const client = getQdrantClient();

  // Check collection exists before embedding (saves OpenAI API call)
  const exists = await collectionExists(client, COLLECTION_NAME);
  if (!exists) {
    return {
      found: false,
      reason:
        `The '${COLLECTION_NAME}' Qdrant collection does not exist yet. ` +
        `It will be created in Phase 4 of AppFactory when the pipeline begins ` +
        `indexing operational knowledge. No results available.`,
    };
  }

  // Embed the query
  let vector: number[];
  try {
    vector = await embedQuery(options.query);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`Failed to embed search query: ${msg}`);
  }

  // Build optional filter
  const filter =
    options.filterAgent !== undefined
      ? {
          must: [
            {
              key: "agent",
              match: { value: options.filterAgent },
            },
          ],
        }
      : undefined;

  // Search Qdrant
  const searchResponse = await client.search(COLLECTION_NAME, {
    vector,
    limit: options.topK,
    with_payload: true,
    filter,
  });

  const results: KnowledgeSearchResult[] = searchResponse.map((hit) => ({
    id: hit.id,
    score: hit.score,
    payload: (hit.payload ?? {}) as KnowledgeSearchResult["payload"],
  }));

  return { found: true, results };
}

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)

_COLLECTION = "operational_knowledge"
_EMBED_MODEL = "text-embedding-3-small"


def embed_and_store(entry: dict) -> None:
    """
    Embed entry["task"] + entry["output"] and upsert into Qdrant operational_knowledge.

    Required entry keys: agent, task, output, run_id, graph_id

    Optional metadata keys (project-scoped KB schema added 2026-04-22):
      - project_key   str  — Jira project key, e.g. "WAL", "TOM", "AF". Default "".
                             Enables per-project scoped search via the search_knowledge MCP tool.
      - kind          str  — entry classification. Expected values:
                              "research"              — Research agent finding
                              "ticket_summary"        — post-merge ticket implementation summary
                              "batch_summary"         — post-batch summary (iterative_dev close)
                              "architecture_decision" — pinned design/architecture note
                              "incident_note"         — post-incident learning
                             Default "unknown" — flagged so unclassified entries are findable for cleanup.
      - ticket_id     str  — Jira ticket ID this entry relates to (e.g. "WAL-42"). Default None.
      - superseded_by str  — UUID of a newer entry that replaces this one. Default None.
                             Never set at creation time; populated when a later entry obsoletes this one.
      - timestamp     str  — ISO datetime. Default now UTC.

    No-op (with warning) if QDRANT_URL or OPENAI_API_KEY are not set.
    Errors during embedding or upsert are logged and swallowed — never crash the pipeline.
    """
    qdrant_url = os.environ.get("QDRANT_URL")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not qdrant_url or not openai_key:
        logger.warning(
            "embed_and_store: QDRANT_URL or OPENAI_API_KEY not set — skipping store"
        )
        return

    try:
        import openai as _openai
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct
    except ImportError as exc:
        logger.warning("embed_and_store: missing dependency %s — skipping store", exc)
        return

    text = f"{entry.get('task', '')}\n\n{entry.get('output', '')}"

    try:
        oai = _openai.OpenAI(api_key=openai_key)
        embedding = oai.embeddings.create(
            model=_EMBED_MODEL,
            input=text,
        ).data[0].embedding
    except Exception as exc:
        logger.error("embed_and_store: OpenAI embedding failed — %s", exc)
        return

    try:
        qdrant = QdrantClient(url=qdrant_url)
        qdrant.upsert(
            collection_name=_COLLECTION,
            points=[
                PointStruct(
                    id=str(uuid4()),
                    vector=embedding,
                    payload={
                        "agent": entry.get("agent", ""),
                        "task": entry.get("task", ""),
                        "output": entry.get("output", ""),
                        "run_id": entry.get("run_id", ""),
                        "graph_id": entry.get("graph_id", ""),
                        "timestamp": entry.get(
                            "timestamp",
                            datetime.now(timezone.utc).isoformat(),
                        ),
                        # Project-scoped KB schema (added 2026-04-22).
                        # See module docstring for field semantics.
                        "project_key": entry.get("project_key", ""),
                        "kind": entry.get("kind", "unknown"),
                        "ticket_id": entry.get("ticket_id"),
                        "superseded_by": entry.get("superseded_by"),
                    },
                )
            ],
        )
        logger.info(
            "embed_and_store: stored to %s | agent=%s run_id=%s",
            _COLLECTION,
            entry.get("agent", ""),
            entry.get("run_id", ""),
        )
    except Exception as exc:
        logger.error("embed_and_store: Qdrant upsert failed — %s", exc)

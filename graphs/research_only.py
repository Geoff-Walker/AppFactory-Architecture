"""
research_only pipeline — Phase 2, first graph.

Purpose:
  1. Validates the full dispatch → headless Claude Code → Qdrant write → MCP status readable chain.
  2. Used by Archie on the laptop to fire async research tasks via the LangGraph MCP.

Stub behaviour (current):
  - research_node returns hardcoded findings; does NOT spawn headless Claude Code.
  To test interrupt paths, change the returned research_status in research_node.

Real behaviour (future):
  - research_node: subprocess.run(["claude", "--print", task], cwd=planning_repo_path)
    then reads output.md written by the Research agent.

store_node is fully implemented — embeds findings and upserts into Qdrant operational_knowledge.
Gracefully no-ops if QDRANT_URL or OPENAI_API_KEY are not set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from graphs.knowledge import embed_and_store
from graphs.state import PipelineState
from graphs.tracing import apply_run_id_to_trace, observe

logger = logging.getLogger(__name__)


def _extract_run_id(config: Optional[RunnableConfig]) -> str:
    """Pull the LangGraph-supplied run_id out of RunnableConfig.

    Mirrors the helper in ``iterative_dev.py``. Kept local here to avoid a
    cross-module import cycle; the two helpers share a trivial implementation.
    """
    if not config:
        return ""
    metadata = config.get("metadata") or {}
    configurable = config.get("configurable") or {}
    run_id = metadata.get("run_id") or configurable.get("run_id") or ""
    return str(run_id) if run_id else ""


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@observe()
def research_node(state: PipelineState, config: Optional[RunnableConfig] = None) -> dict:
    """
    STUB — simulates headless Claude Code Research agent.

    Real impl: spawn `claude --print <task>` as a subprocess pointed at the
    Research agent CLAUDE.md in the Planning repo, then read output.md.

    Status options to test interrupt paths:
        "COMPLETED"            → normal path
        "DEEP_RESEARCH_NEEDED" → fires approval interrupt
        "BLOCKED"              → fires blocked interrupt

    Populates ``run_id`` on first entry (preserves Langfuse trace linkage).
    On the deep_research loop-back the node runs again and would overwrite
    the existing value with the same one — harmless idempotency.
    """
    mode = state.get("research_mode", "quick_hit")
    question = state.get("question", "")

    logger.info("[STUB] research_node — mode=%s question=%.80s", mode, question)

    # Carry run_id forward. Only set if we have one; otherwise leave whatever
    # was already in state (could have been injected by a parent dispatch).
    run_id = _extract_run_id(config) or state.get("run_id", "")
    apply_run_id_to_trace(run_id)

    if mode == "deep_research":
        return {
            "research_findings": f"[STUB] Deep findings for: {question}",
            "research_gaps": [],
            "research_status": "COMPLETED",
            "run_id": run_id,
        }

    return {
        "research_findings": f"[STUB] Quick-hit findings for: {question}",
        "research_gaps": [],
        "research_status": "COMPLETED",
        "run_id": run_id,
    }


def check_result_node(state: PipelineState) -> dict:
    """
    Routes on research_status.
    Fires an interrupt for DEEP_RESEARCH_NEEDED (requires Geoff approval) or BLOCKED.
    Only Geoff can authorise Deep Research — no self-escalation ever.
    """
    status = state.get("research_status", "COMPLETED")

    if status == "DEEP_RESEARCH_NEEDED":
        decision = interrupt({
            "type": "deep_research_needed",
            "message": (
                f"Research agent exhausted Quick Hit budget on: {state.get('question', '')}. "
                f"Gaps identified: {state.get('research_gaps', [])}"
            ),
            "hint": (
                'Authorise deeper research or accept partial findings.\n'
                'Reply with: {"action": "deep_research"} or {"action": "accept_partial"}'
            ),
        })
        action = (decision or {}).get("action", "accept_partial")
        if action == "deep_research":
            # Loop back to research_node in deep mode.
            # research_status=None signals route_after_check to go to "research".
            return {"research_mode": "deep_research", "research_status": None}
        # accept_partial — treat partial findings as complete
        return {"research_status": "COMPLETED"}

    if status == "BLOCKED":
        decision = interrupt({
            "type": "blocked",
            "message": (
                f"Research agent blocked on: {state.get('question', '')}. "
                f"Reason: {state.get('blocked_reason', 'unspecified')}"
            ),
            "hint": (
                'Provide instructions to unblock, or abort.\n'
                'Reply with: {"action": "abort"} or '
                '{"action": "continue", "instruction": "..."}'
            ),
        })
        action = (decision or {}).get("action", "abort")
        if action == "abort":
            return {"research_status": "ABORTED"}
        return {
            "research_status": None,
            "research_context": (decision or {}).get("instruction", ""),
        }

    return {}


def route_after_check(
    state: PipelineState,
) -> Literal["research", "store", "__end__"]:
    status = state.get("research_status")
    if status == "ABORTED":
        return "__end__"
    if status is None:
        return "research"   # deep_research loop or continue-after-blocked
    return "store"


@observe()
def store_node(state: PipelineState) -> dict:
    """
    Embeds research findings and upserts into Qdrant operational_knowledge.
    Gracefully no-ops if QDRANT_URL or OPENAI_API_KEY are not set.

    Populates the project-scoped KB schema (added 2026-04-22):
      - kind="research" (this graph only ever produces research findings)
      - project_key from pipeline state
      - ticket_id from current_ticket_id if research was triggered for a
        specific ticket (e.g. via the research_gate from iterative_dev),
        otherwise None for standalone research dispatches.
      - superseded_by is never set at creation.
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    embed_and_store({
        "agent": "research",
        "task": state.get("question", ""),
        "output": state.get("research_findings", ""),
        "run_id": state.get("run_id", ""),
        "graph_id": "research_only",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_key": state.get("project_key", ""),
        "kind": "research",
        "ticket_id": state.get("current_ticket_id"),
    })
    return {}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

_builder = StateGraph(PipelineState)
_builder.add_node("research", research_node)
_builder.add_node("check_result", check_result_node)
_builder.add_node("store", store_node)

_builder.set_entry_point("research")
_builder.add_edge("research", "check_result")
_builder.add_conditional_edges(
    "check_result",
    route_after_check,
    {
        "research": "research",
        "store": "store",
        "__end__": END,
    },
)
_builder.add_edge("store", END)

graph = _builder.compile()

"""
research_gate — pipeline-level research budget enforcement.

Replaces per-agent direct search quotas with a centralised pipeline gate.
Any pipeline node that signals RESEARCH_NEEDED is routed here. The gate
checks the remaining budget for that node type, dispatches a research_only
subgraph run if budget is available, or converts to BLOCKED if exhausted.

Budget is expressed as the number of quick_hit research dispatches permitted
per node type per pipeline run. Deep Research always requires Geoff's approval
via interrupt — the gate never self-escalates to deep mode.

Quotas mirror the per-agent direct search limits defined in each agent's
CLAUDE.md, translated from raw search counts to dispatch allowances:

  Development:    1 quick_hit dispatch  (was: 1 WebSearch, 1 WebFetch)
  QA:             1 quick_hit dispatch  (was: 1 WebSearch, 0 WebFetch)
  Infrastructure: 1 quick_hit dispatch  (was: 1 WebSearch, 1 WebFetch)

Design, RiskEthics, and Ventures are laptop-side interactive agents —
not in scope for server-side pipeline enforcement.
"""

from __future__ import annotations

import logging
from typing import Callable

from graphs.state import PipelineState

logger = logging.getLogger(__name__)

RESEARCH_QUOTAS: dict[str, dict[str, int]] = {
    "development":    {"quick_hit": 1},
    "qa":             {"quick_hit": 1},
    "infrastructure": {"quick_hit": 1},
}


def make_research_gate_node(node_type: str) -> Callable[[PipelineState], dict]:
    """
    Returns a research gate node function configured for the given agent type.

    Usage in a graph:
        from graphs.research_gate import make_research_gate_node
        dev_research_gate = make_research_gate_node("development")
        builder.add_node("research_gate", dev_research_gate)
    """
    quota = RESEARCH_QUOTAS.get(node_type, {}).get("quick_hit", 0)

    def research_gate_node(state: PipelineState) -> dict:
        dispatches = dict(state.get("research_dispatches") or {})
        used = dispatches.get(node_type, 0)
        question = state.get("research_needed_question", "unspecified")

        if used >= quota:
            logger.info(
                "[research_gate] %s — budget exhausted (used %d/%d). "
                "Converting RESEARCH_NEEDED → BLOCKED. question=%.80s",
                node_type, used, quota, question,
            )
            return {
                "research_gate_result": "budget_exhausted",
                "ticket_status": "BLOCKED",
                "blocked_reason": (
                    f"Research quota exhausted for {node_type} "
                    f"(limit: {quota} quick-hit dispatch(es)). "
                    f"Question: {question}"
                ),
            }

        # Budget available — dispatch research.
        logger.info(
            "[STUB] research_gate — dispatching quick_hit for %s (dispatch %d/%d). "
            "question=%.80s",
            node_type, used + 1, quota, question,
        )

        # Real impl: invoke research_only subgraph synchronously:
        #
        #   from graphs.research_only import graph as research_only_graph
        #   result = research_only_graph.invoke({
        #       "question": question,
        #       "research_mode": "quick_hit",
        #       "run_id": state.get("run_id", ""),
        #   })
        #   findings = result.get("research_findings", "")
        #
        # Findings are stored in Qdrant operational_knowledge by research_only's
        # store_node and also injected directly into research_context for the
        # executor to use when the ticket is re-dispatched.

        dispatches[node_type] = used + 1
        return {
            "research_gate_result": "dispatched",
            "research_context": f"[STUB] Research findings for: {question}",
            "research_dispatches": dispatches,
            "ticket_status": None,  # cleared so executor re-runs cleanly
        }

    research_gate_node.__name__ = f"research_gate_{node_type}"
    return research_gate_node

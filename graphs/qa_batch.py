"""
qa_batch pipeline — Phase 2 Step 4.

Takes a project spec and design output, runs the QA agent to produce an
ordered, tagged batch of Jira tickets, then either chains to iterative_dev
or stops — based on Geoff's decision at the gate.

The gate ALWAYS fires. chain_to_dev sets the default hint in the interrupt
payload; it does not bypass the gate.

Stub behaviour (current):
  - qa_node: returns a hardcoded two-ticket batch; does NOT spawn Claude Code
    or create Jira tickets
  - chain_node: logs the iterative_dev subgraph invocation that would happen;
    does NOT actually invoke it

Real behaviour (future):
  - qa_node: subprocess.run(["claude", "--print", task], cwd=planning_repo_path)
    QA agent reads spec + design output, creates Jira tickets (ordered,
    self-contained, given/when/then ACs, executor-tagged, dependency-linked),
    writes output.md: ordered ticket ID list, summaries, executor tags,
    dependency map
  - chain_node: invoke iterative_dev subgraph directly:
      from graphs.iterative_dev import graph as iterative_dev_graph
      iterative_dev_graph.invoke({
          "tickets": state["tickets"],
          "project_key": state["project_key"],
          "repo": state["repo"],
          "sprint_number": 1,
      })

QA agent responsibilities (enforced by QA CLAUDE.md):
  - Tag every ticket: executor:haiku or executor:claude-dev
  - Order tickets in dependency-safe sequence (schema before API, API before frontend)
  - Use Jira blocks/is-blocked-by for explicit dependency linking
  - Document execution sequence and dependency map in output.md
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from graphs.state import PipelineState
from graphs.tracing import apply_run_id_to_trace, observe

logger = logging.getLogger(__name__)


def _extract_run_id(config: Optional[RunnableConfig]) -> str:
    """Pull the LangGraph-supplied run_id out of RunnableConfig."""
    if not config:
        return ""
    metadata = config.get("metadata") or {}
    configurable = config.get("configurable") or {}
    run_id = metadata.get("run_id") or configurable.get("run_id") or ""
    return str(run_id) if run_id else ""


# ---------------------------------------------------------------------------
# QA node
# ---------------------------------------------------------------------------

@observe()
def qa_node(state: PipelineState, config: Optional[RunnableConfig] = None) -> dict:
    """
    STUB — simulates headless Claude Code QA agent.

    Real impl: spawn `claude --print <task>` pointed at QA agent CLAUDE.md,
    passing spec, design_output_location, and project_key. Agent creates Jira
    tickets and writes output.md with ordered ticket IDs, summaries, executor
    tags, and dependency map.

    Output shape:
      - ``tickets``: ordered list of ``{"id", "executor"}`` dicts — the input
        shape consumed by the ``iterative_dev`` graph. Each ticket carries
        its own executor tag so mixed batches route correctly.
      - ``qa_ticket_summaries``: richer list with summaries and blocked_by
        used by the gate interrupt payload.

    Executor vocabulary: ``haiku`` or ``claude-dev`` (the ``executor:`` Jira
    label prefix is not emitted here — the raw tag is the canonical form).
    """
    spec = state.get("spec", "")
    design_loc = state.get("design_output_location", "")
    project_key = state.get("project_key", "PROJ")

    logger.info(
        "[STUB] qa_node — project=%s spec=%.60s design_output=%.60s",
        project_key, spec, design_loc,
    )

    # Stub output: two ordered tickets representing a realistic minimal batch.
    ticket_summaries = [
        {
            "id": f"{project_key}-1",
            "summary": "Add database schema migration",
            "executor_tag": "claude-dev",
            "blocked_by": [],
        },
        {
            "id": f"{project_key}-2",
            "summary": "Implement API endpoint",
            "executor_tag": "haiku",
            "blocked_by": [f"{project_key}-1"],
        },
    ]
    tickets = [
        {"id": t["id"], "executor": t["executor_tag"]} for t in ticket_summaries
    ]

    run_id = _extract_run_id(config) or state.get("run_id", "")
    apply_run_id_to_trace(run_id)

    return {
        "tickets": tickets,
        "qa_ticket_summaries": ticket_summaries,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Gate node — always interrupts
# ---------------------------------------------------------------------------

def gate_node(state: PipelineState) -> dict:
    """
    Always fires an interrupt. Geoff reviews the ticket batch before any
    Dev work begins — even on an overnight chain_to_dev run.

    chain_to_dev sets the default hint, not a bypass.
    """
    summaries = state.get("qa_ticket_summaries", [])
    chain = state.get("chain_to_dev", False)
    n = len(summaries)

    hint = (
        f"{n} ticket(s) created. Proceed to build overnight? "
        'Reply {"action": "chain"} to dispatch iterative_dev, '
        'or {"action": "stop"} to leave tickets in Jira for manual dispatch.'
        if chain else
        f"{n} ticket(s) created. Dispatch iterative_dev manually when ready. "
        'Reply {"action": "chain"} to dispatch now, '
        'or {"action": "stop"} to end here.'
    )

    decision = interrupt({
        "type": "qa_gate",
        "message": f"QA complete. {n} ticket(s) ready for review.",
        "tickets": summaries,
        "chain_to_dev_default": chain,
        "hint": hint,
    })

    action = (decision or {}).get("action", "stop")

    if action == "chain":
        logger.info("gate_node — approved. Chaining to iterative_dev.")
        return {"qa_gate_decision": "chain"}

    logger.info("gate_node — stopped. Tickets remain in Jira for manual dispatch.")
    return {"qa_gate_decision": "stop"}


def route_after_gate(state: PipelineState) -> Literal["chain", "__end__"]:
    if state.get("qa_gate_decision") == "chain":
        return "chain"
    return "__end__"


# ---------------------------------------------------------------------------
# Chain node
# ---------------------------------------------------------------------------

def chain_node(state: PipelineState) -> dict:
    """
    STUB — invokes iterative_dev subgraph with the QA-produced ticket batch.

    Real impl:
        from graphs.iterative_dev import graph as iterative_dev_graph
        iterative_dev_graph.invoke({
            "tickets": state["tickets"],
            "project_key": state["project_key"],
            "repo": state["repo"],
            "sprint_number": 1,
        })
    """
    tickets = state.get("tickets", [])
    logger.info(
        "[STUB] chain_node — would invoke iterative_dev with %d ticket(s): %s",
        len(tickets), [t.get("id") for t in tickets],
    )
    return {}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

_builder = StateGraph(PipelineState)

_builder.add_node("qa", qa_node)
_builder.add_node("gate", gate_node)
_builder.add_node("chain", chain_node)

_builder.set_entry_point("qa")
_builder.add_edge("qa", "gate")
_builder.add_conditional_edges(
    "gate",
    route_after_gate,
    {"chain": "chain", "__end__": END},
)
_builder.add_edge("chain", END)

graph = _builder.compile()

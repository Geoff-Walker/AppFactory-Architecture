"""
infra_task pipeline — Phase 2 Step 5.

Executes infrastructure operations on TrueNAS / aidev VM with the tiered
approval model preserved. Archie on the laptop is the primary approval
interface via LangGraph MCP tools. Telegram is the fallback.

Tiers:
  Tier 1 — read-only observation. No plan required. Execute → Verify → Done.
  Tier 2 — state-modifying. Full staged plan required. Plan gate always fires.
            Geoff approves a stage range before any command runs. Hard-stop on
            any stage failure — no workarounds, no continued execution.
  Tier 3 — pre-approved playbook. No plan gate. Execute → Verify → Done.

Stage range approval:
  Geoff approves a range at the plan gate, e.g. stages [1, 2]. Execute runs
  those stages in order. After the approved range is complete, the pipeline
  proceeds to verify. If further stages remain (not yet approved), a second
  plan_gate interrupt fires before they can run.

  This implements the "measure twice, cut once" principle: Geoff sees the
  full plan once, then approves it in increments as confidence builds.

Stub behaviour (current):
  - assess_node: returns Tier 2 with a two-stage stub plan
  - plan_node: logs plan that would be written; returns stub plan
  - execute_node: logs stage execution; returns PASSED immediately
  - playbook_node: logs playbook invocation; returns immediately
  - verify_node: logs health checks; returns stub outcome
  - done_node: logs documentation update; does NOT write files or commit

Real behaviour (future):
  - assess_node: subprocess → Infrastructure agent reads task, determines tier
  - plan_node: subprocess → Infrastructure agent writes full staged plan
  - execute_node: subprocess → Infrastructure agent runs approved stages via SSH
  - verify_node: subprocess → Infrastructure agent runs health checks
  - done_node: subprocess → Infrastructure agent updates running-services.md
    or server.md and commits
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
# Assess
# ---------------------------------------------------------------------------

@observe()
def assess_node(state: PipelineState, config: Optional[RunnableConfig] = None) -> dict:
    """
    STUB — spawns headless Claude Code Infrastructure agent to determine tier.

    Real impl: agent reads infra_task_description + infra_context, applies the
    tier rules from its CLAUDE.md, writes tier + reasoning + proposed actions
    to output.md.

    Populates ``run_id`` from the LangGraph ``RunnableConfig`` so every
    downstream node sees the same ID that Langfuse keys its trace on.

    Tier rules (from Infrastructure agent CLAUDE.md):
      Tier 1 — read-only: docker ps, zpool status, health checks, log reads
      Tier 2 — state-modifying: docker compose up/down, zfs create, file edits,
                any command that changes server state
      Tier 3 — pre-approved playbook: named operation in playbooks/ with
                status: Pre-approved
    """
    task = state.get("infra_task_description", "")
    logger.info("[STUB] assess_node — task=%.80s", task)
    run_id = _extract_run_id(config) or state.get("run_id", "")
    apply_run_id_to_trace(run_id)
    return {
        "infra_tier": 2,
        "infra_tier_reasoning": "[STUB] Task modifies server state — Tier 2.",
        "infra_total_stages": 2,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Tier routing
# ---------------------------------------------------------------------------

def tier_check_node(state: PipelineState) -> dict:
    """Pass-through — routing logic lives in route_after_tier_check."""
    return {}


def route_after_tier_check(
    state: PipelineState,
) -> Literal["execute", "plan", "playbook"]:
    tier = state.get("infra_tier", 2)
    if tier == 1:
        return "execute"
    if tier == 3:
        return "playbook"
    return "plan"  # Tier 2 and any unexpected value


# ---------------------------------------------------------------------------
# Plan (Tier 2 only)
# ---------------------------------------------------------------------------

@observe()
def plan_node(state: PipelineState) -> dict:
    """
    STUB — spawns Infrastructure agent to write the full staged plan.

    Real impl: agent writes plan in the standard Infrastructure CLAUDE.md format:
      Goal, reversibility assessment
      Per stage: commands, why, risk, what failure looks like

    Plan is written to state so plan_gate_node can include it in the interrupt
    payload for Geoff to review in the Archie conversation on the laptop.
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    task = state.get("infra_task_description", "")
    logger.info("[STUB] plan_node — writing stub plan for: %.80s", task)
    stub_plan = (
        "## Infrastructure Plan: [STUB]\n\n"
        "**Goal:** Execute stub infrastructure task.\n"
        "**Reversible:** Yes — all changes can be rolled back.\n\n"
        "### Stage 1 — Preparation\n"
        "**Commands:** echo 'stage 1'\n"
        "**Why:** Stub preparation step.\n"
        "**Risk:** None — additive/read-only.\n\n"
        "### Stage 2 — Execution\n"
        "**Commands:** echo 'stage 2'\n"
        "**Why:** Stub execution step.\n"
        "**Risk:** None — stub only.\n"
    )
    return {
        "infra_plan": stub_plan,
        "infra_total_stages": 2,
    }


# ---------------------------------------------------------------------------
# Plan gate (Tier 2 — always fires)
# ---------------------------------------------------------------------------

def plan_gate_node(state: PipelineState) -> dict:
    """
    Always interrupts for Tier 2. Geoff reads the full plan in the Archie
    conversation via get_interrupt_state MCP tool, then approves a stage range
    or rejects.

    Approval sets approved_stage_range — execute only runs those stages.
    Rejection ends the pipeline; plan is preserved in state for the audit log.
    """
    plan = state.get("infra_plan", "")
    total = state.get("infra_total_stages", 0)

    decision = interrupt({
        "type": "infra_plan_gate",
        "message": f"Infrastructure plan ready. {total} stage(s) total. Review and approve.",
        "plan": plan,
        "total_stages": total,
        "hint": (
            f'Approve a stage range to begin execution.\n'
            f'Reply with: {{"action": "approve", "stages": [1]}} to run stage 1 only, '
            f'or {{"action": "approve", "stages": {list(range(1, total + 1))}}} '
            f'to approve all {total} stage(s), '
            f'or {{"action": "reject"}} to end without executing.'
        ),
    })

    action = (decision or {}).get("action", "reject")

    if action == "approve":
        stages = (decision or {}).get("stages", [])
        if not stages:
            stages = list(range(1, total + 1))
        logger.info("plan_gate_node — approved stages %s", stages)
        return {
            "plan_gate_decision": "approved",
            "approved_stage_range": sorted(stages),
            "current_stage": min(sorted(stages)),
            "stage_outcomes": [],
        }

    logger.info("plan_gate_node — rejected.")
    return {"plan_gate_decision": "rejected"}


def route_after_plan_gate(
    state: PipelineState,
) -> Literal["execute", "__end__"]:
    if state.get("plan_gate_decision") == "approved":
        return "execute"
    return "__end__"


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

@observe()
def execute_node(state: PipelineState) -> dict:
    """
    Runs the current stage. Hard-stops on failure — no workarounds, no
    continued execution without explicit Geoff approval.

    On success: advances current_stage. Route function determines whether to
    loop (more approved stages remain) or proceed to verify (approved range
    complete).

    On failure: fires interrupt immediately. Geoff decides:
      retry            — re-run the same stage from scratch
      manual_and_retry — Geoff intervenes externally, then pipeline retries
      abort            — pipeline ends; stage_outcomes preserved for audit

    Real impl: subprocess → Infrastructure agent runs the stage commands via
    SSH and writes per-stage output to state (exit code, stdout, stderr).
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    stage = state.get("current_stage", 1)
    tier = state.get("infra_tier", 2)
    task = state.get("infra_task_description", "")

    logger.info("[STUB] execute_node — tier=%d stage=%d task=%.60s", tier, stage, task)

    # Stub: stage always passes.
    # To test failure path in real usage, set stub_fail = True.
    stub_fail = False
    stage_output = f"[STUB] Stage {stage} passed."

    if stub_fail:
        decision = interrupt({
            "type": "stage_failed",
            "stage": stage,
            "message": f"Stage {stage} failed. Hard stop.",
            "error": "[STUB] Simulated stage failure.",
            "hint": (
                'Do not continue. Investigate the error above.\n'
                'Reply with: {"action": "retry"} to re-run this stage, '
                '{"action": "manual_and_retry"} if you have intervened manually and want to retry, '
                'or {"action": "abort"} to end the pipeline.'
            ),
        })
        action = (decision or {}).get("action", "abort")
        if action in ("retry", "manual_and_retry"):
            return {"infra_execute_decision": "retry"}
        return {"infra_execute_decision": "abort"}

    outcomes = list(state.get("stage_outcomes") or [])
    outcomes.append({"stage": stage, "status": "PASSED", "output": stage_output})

    return {
        "infra_execute_decision": "continue",
        "current_stage": stage + 1,
        "stage_outcomes": outcomes,
    }


def route_after_execute(
    state: PipelineState,
) -> Literal["execute", "verify", "__end__"]:
    decision = state.get("infra_execute_decision")
    if decision == "abort":
        return "__end__"
    if decision == "retry":
        return "execute"

    # Stage passed — check if more approved stages remain.
    current = state.get("current_stage", 1)
    approved = state.get("approved_stage_range") or []
    if approved and current <= max(approved):
        return "execute"
    return "verify"


# ---------------------------------------------------------------------------
# Playbook (Tier 3)
# ---------------------------------------------------------------------------

@observe()
def playbook_node(state: PipelineState) -> dict:
    """
    STUB — executes a pre-approved playbook. No plan gate required.

    Real impl: Infrastructure agent identifies the playbook by name from
    infra_task_description, validates its PENDING OWNER REVIEW / Pre-approved
    status, and runs the defined steps. A playbook that is not Pre-approved is
    escalated to Tier 2 treatment.
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    task = state.get("infra_task_description", "")
    logger.info("[STUB] playbook_node — would execute playbook for: %.80s", task)
    outcomes = [{"stage": 1, "status": "PASSED", "output": "[STUB] Playbook executed."}]
    return {
        "stage_outcomes": outcomes,
        "current_stage": 2,
        "approved_stage_range": [1],
    }


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

@observe()
def verify_node(state: PipelineState) -> dict:
    """
    STUB — health checks appropriate to the operation.

    Real impl: Infrastructure agent runs checks relevant to what was executed:
      docker compose → docker ps + curl health endpoint
      zfs operation  → zpool status + zfs list
      file edit      → cat the file, confirm expected content

    Outcome written to verify_outcome: "PASSED" | "FAILED" + detail.
    On FAILED: interrupt fires — Geoff decides whether to accept, retry, or
    manually investigate (not implemented in this stub iteration).
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    outcomes = state.get("stage_outcomes", [])
    tier = state.get("infra_tier", 2)
    logger.info("[STUB] verify_node — tier=%d stages_completed=%d", tier, len(outcomes))
    return {"verify_outcome": "[STUB] All health checks passed."}


# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

@observe()
def done_node(state: PipelineState) -> dict:
    """
    STUB — updates Infrastructure reference files and commits.

    Real impl: Infrastructure agent updates running-services.md or server.md
    (whichever is relevant to the operation) and commits in the same git
    operation. Stale reference files cause the next agent session to start
    from a wrong baseline — documentation update is not optional.
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    task = state.get("infra_task_description", "")
    verify = state.get("verify_outcome", "")
    logger.info(
        "[STUB] done_node — would update docs and commit | task=%.60s | verify=%s",
        task, verify,
    )
    return {"infra_docs_updated": True}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

_builder = StateGraph(PipelineState)

_builder.add_node("assess", assess_node)
_builder.add_node("tier_check", tier_check_node)
_builder.add_node("plan", plan_node)
_builder.add_node("plan_gate", plan_gate_node)
_builder.add_node("execute", execute_node)
_builder.add_node("playbook", playbook_node)
_builder.add_node("verify", verify_node)
_builder.add_node("done", done_node)

_builder.set_entry_point("assess")
_builder.add_edge("assess", "tier_check")
_builder.add_conditional_edges(
    "tier_check",
    route_after_tier_check,
    {"execute": "execute", "plan": "plan", "playbook": "playbook"},
)
_builder.add_edge("plan", "plan_gate")
_builder.add_conditional_edges(
    "plan_gate",
    route_after_plan_gate,
    {"execute": "execute", "__end__": END},
)
_builder.add_conditional_edges(
    "execute",
    route_after_execute,
    {"execute": "execute", "verify": "verify", "__end__": END},
)
_builder.add_edge("playbook", "verify")
_builder.add_edge("verify", "done")
_builder.add_edge("done", END)

graph = _builder.compile()

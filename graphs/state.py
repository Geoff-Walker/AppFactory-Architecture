from __future__ import annotations
from typing import Any, Optional, TypedDict


class PipelineState(TypedDict, total=False):
    # --- research_only ---
    question: str
    research_mode: str          # "quick_hit" | "deep_research"
    research_findings: str
    research_gaps: list[str]
    research_status: Optional[str]  # "COMPLETED" | "DEEP_RESEARCH_NEEDED" | "BLOCKED" | "ABORTED" | None
    research_context: str           # findings injected back after a research_gate dispatch
    blocked_reason: str

    # --- research_gate (shared across pipelines) ---
    research_dispatches: dict       # {"development": 1, "qa": 0} — dispatch count per node_type
    research_needed_question: str   # specific question signalled by an executor node
    research_gate_result: str       # "dispatched" | "budget_exhausted" — set by research_gate_node

    # --- iterative_dev ---
    # Per-ticket input: each entry is {"id": str, "executor": "haiku" | "claude-dev"}.
    # The ``executor`` value may also arrive as the raw Jira label form
    # ``"executor:haiku"`` / ``"executor:claude-dev"``; ``pick_next_ticket_node``
    # strips the prefix. This lets dispatchers pass the label verbatim.
    tickets: list[dict]
    current_ticket_index: int
    current_ticket_id: str
    executor_tag: str               # "haiku" | "claude-dev" — set per ticket by pick_next_ticket_node
    current_executor: str           # "haiku" | "claude_dev" — which executor node is running now
    ticket_status: str              # "COMPLETED" | "BLOCKED" | "RESEARCH_NEEDED" — from executor
    ticket_pr_url: str              # PR URL returned by executor on COMPLETED
    sprint_number: int              # N in batch/sprint-N branch name
    integration_branch: str
    completed_prs: list[str]
    skipped_tickets: list[str]
    batch_pr_url: str
    escalation_attempted: bool
    escalation_decision: str        # "retry_with_claude_dev" | "park_and_continue" | "abort"

    # --- infra_task ---
    infra_task_description: str     # what to do, passed at dispatch
    infra_context: str              # additional context passed at dispatch
    infra_tier: int                 # 1 (read-only) | 2 (state-modifying) | 3 (playbook)
    infra_tier_reasoning: str       # assess_node's stated reasoning for tier assignment
    infra_plan: str                 # full staged plan (Tier 2 only)
    infra_total_stages: int         # total number of stages in the plan
    approved_stage_range: list[int] # stages approved for execution, e.g. [1, 2, 3]
    current_stage: int              # stage currently being executed (1-indexed)
    stage_outcomes: list            # [{stage, status, output}] accumulated per run
    infra_execute_decision: str     # "continue" | "retry" | "abort" — from stage-fail interrupt
    plan_gate_decision: str         # "approved" | "rejected"
    verify_outcome: str             # result of post-execution health checks
    infra_docs_updated: bool        # True once done_node has committed docs update

    # --- qa_batch ---
    chain_to_dev: bool
    qa_output_location: str
    spec: str
    design_output_location: str
    qa_ticket_summaries: list       # [{id, summary, executor_tag, blocked_by}] — for gate payload
    qa_gate_decision: str           # "chain" | "stop"

    # --- human-in-the-loop (shared) ---
    interrupt_state: Optional[Any]
    interrupt_hint: str

    # --- pipeline metadata (shared) ---
    run_id: str
    project_key: str
    repo: str

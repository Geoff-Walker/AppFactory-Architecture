"""
iterative_dev pipeline — Phase 2 Step 3.

Executes a defined, ordered batch of Jira tickets. Single concern — no QA,
no design, no decisions. Takes an explicit ordered ticket list, builds each
in sequence, opens one batch PR when done.

Batch is always defined at dispatch. The pipeline never queries Jira for new
work and never runs indefinitely.

Executors:
  haiku_node    — Claude Code headless, model: claude-haiku-4-5 (fast, cheap)
                  For well-scoped tickets with clear ACs and known file paths.
  claude_dev_node — Claude Code headless, model: claude-sonnet-4-6 (capable)
                  For complex, architectural, or cross-cutting tickets.

  Escalation: haiku BLOCKED → retry with claude_dev (sonnet). Still BLOCKED → interrupt.

Stub behaviour (current):
  - setup_node: logs branch name; does not call GitHub API
  - haiku_node: logs subprocess that would be spawned; returns COMPLETED immediately
  - claude_dev_node: logs subprocess that would be spawned; returns COMPLETED immediately
  - merge_node: logs CI wait + merge; no actual GitHub API call
  - batch_close_node: logs PR + staging deploy; no actual calls

Real behaviour (future):
  - setup_node: GitHub REST API — create branch from main
  - haiku_node: subprocess.run(["claude", "--model", "claude-haiku-4-5-20251001",
      "--print", task], cwd=repo_path), reads output.md for status + PR URL
  - claude_dev_node: subprocess.run(["claude", "--model", "claude-sonnet-4-6",
      "--print", task], cwd=repo_path), reads output.md for status + PR URL
  - merge_node: GitHub API — wait for CI, merge PR into integration branch
  - batch_close_node: GitHub API PR, TrueNAS staging deploy, Telegram notify

Research rule: only the Research agent may search the web. When a dev node
needs external information, it signals RESEARCH_NEEDED. The pipeline routes
to research_gate, which dispatches a research_only run within the node type's
quota. Budget exhausted → BLOCKED. Deep Research always requires Geoff's
interrupt approval.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

import requests

from graphs.github_api import (
    MergeBlocked,
    MergeTimeout,
    create_branch,
    get_branch_sha,
    get_pr,
    merge_pr,
    next_sprint_number,
    open_pr,
    parse_pr_number,
    wait_for_mergeable,
)
from graphs.knowledge import embed_and_store
from graphs.research_gate import make_research_gate_node
from graphs.staging_deploy import DeployFailed, deploy_staging, staging_path_for_repo
from graphs.state import PipelineState
from graphs.tracing import apply_run_id_to_trace, observe
from graphs.workspace import archive_artefacts, cleanup, prepare_workspace

logger = logging.getLogger(__name__)

# Maximum time a single dev subprocess may run before the pipeline kills it.
# Long Claude sessions can run for tens of minutes on complex tickets; one
# hour is the upper bound after which something is wrong rather than slow.
DEV_SUBPROCESS_TIMEOUT_SECONDS = 60 * 60


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _extract_run_id(config: Optional[RunnableConfig]) -> str:
    """Pull the LangGraph-supplied run_id out of RunnableConfig.

    LangGraph Platform surfaces the run_id on the ``metadata`` sub-dict of the
    config object passed to every node. We also check ``configurable`` as a
    defensive fallback for any call path that stashes it there (older LG
    versions; direct ``graph.invoke`` with hand-assembled config).

    Returning an empty string rather than synthesising a UUID is deliberate:
    a blank run_id fails the executor pre-flight fast with a clear reason,
    which is preferable to silently diverging from Langfuse trace linkage.
    """
    if not config:
        return ""
    metadata = config.get("metadata") or {}
    configurable = config.get("configurable") or {}
    run_id = metadata.get("run_id") or configurable.get("run_id") or ""
    return str(run_id) if run_id else ""


def setup_node(state: PipelineState, config: Optional[RunnableConfig] = None) -> dict:
    """Create the integration branch ``batch/sprint-N`` from ``main`` via GitHub API.

    Sprint N is derived by scanning existing ``batch/sprint-*`` branches in the
    repo and incrementing the highest. Caller may override by passing
    ``sprint_number`` explicitly in state (e.g. to retry a specific sprint).

    State requirements:
        repo (required): ``owner/repo`` slug, e.g. ``"Geoff-Walker/FamilyCookbook"``.
        sprint_number (optional): explicit sprint number; auto-derived if absent.

    The LangGraph-supplied ``run_id`` is read from ``config`` and written to
    state so every downstream node (executor pre-flight, KB writes, staging
    deploy) sees the same ID Langfuse keys its trace on.

    Guards:
        - Branch name must start with ``batch/``
        - Branch name must not be ``main`` or ``master``
        - ``create_branch`` itself enforces the same — defence in depth.

    Raises:
        ValueError: if ``state['repo']`` is missing.
        requests.HTTPError: on GitHub API failure (caller decides handling).
    """
    repo = state.get("repo", "")
    if not repo:
        raise ValueError(
            "setup_node requires state['repo'] (e.g. 'Geoff-Walker/FamilyCookbook')."
        )

    sprint_number = state.get("sprint_number") or next_sprint_number(repo)
    branch = f"batch/sprint-{sprint_number}"

    # Code-level guards. These are never expected to fire in normal flow
    # because the branch is constructed from a fixed prefix; they exist to
    # make any future refactor that breaks the invariant fail loudly.
    assert branch.startswith("batch/"), (
        f"setup_node refuses to create non-batch branch: {branch}"
    )
    assert branch not in ("main", "master"), (
        f"setup_node refuses to target protected branch: {branch}"
    )

    main_sha = get_branch_sha(repo, "main")
    create_branch(repo, branch, main_sha)

    run_id = _extract_run_id(config)

    logger.info(
        "setup_node: created %s in %s from main (sha %s), run_id=%s",
        branch, repo, main_sha[:8], run_id or "(missing)",
    )

    return {
        "integration_branch": branch,
        "sprint_number": sprint_number,
        "current_ticket_index": 0,
        "completed_prs": [],
        "skipped_tickets": [],
        "research_dispatches": {},
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Ticket selection
# ---------------------------------------------------------------------------

def _normalise_executor_tag(raw: str) -> str:
    """Strip the ``executor:`` Jira-label prefix if present; return the tag.

    QA tags tickets with literal Jira labels like ``executor:haiku`` or
    ``executor:claude-dev``. Dispatchers may pass the raw label through to the
    pipeline rather than pre-processing it. This helper normalises both forms
    to the bare tag (``haiku`` / ``claude-dev``).
    """
    if not raw:
        return ""
    tag = raw.strip()
    if tag.startswith("executor:"):
        tag = tag[len("executor:"):]
    return tag


def pick_next_ticket_node(state: PipelineState) -> dict:
    """Selects the next unprocessed ticket and resets per-ticket state.

    Reads ``state["tickets"]`` — a list of ``{"id", "executor"}`` dicts — and
    writes the current ticket's ID and normalised executor tag into state.
    ``executor_tag`` is the per-ticket value the routing node reads; it
    changes on every iteration of the loop, supporting mixed batches.
    """
    index = state.get("current_ticket_index", 0)
    tickets = state.get("tickets", [])
    ticket = tickets[index]
    ticket_id = ticket.get("id", "")
    raw_executor = ticket.get("executor", "")
    executor_tag = _normalise_executor_tag(raw_executor)
    logger.info(
        "pick_next_ticket — index %d → %s (executor_tag=%s)",
        index, ticket_id, executor_tag or "(missing)",
    )
    return {
        "current_ticket_id": ticket_id,
        "executor_tag": executor_tag,
        "ticket_status": None,
        "ticket_pr_url": None,
        "current_executor": None,
        "escalation_attempted": False,
        "blocked_reason": None,
        "research_needed_question": None,
        "research_gate_result": None,
        "research_context": None,
    }


# ---------------------------------------------------------------------------
# Routing — executor selection
# ---------------------------------------------------------------------------

def route_node(state: PipelineState) -> dict:
    """
    Reads ``executor_tag`` from state (written per-ticket by
    ``pick_next_ticket_node``) and records which executor node runs next.

    Architecture: the dispatcher supplies per-ticket executor tags in the
    ``tickets`` input list. The pipeline is Jira-agnostic for routing — it
    never fetches ticket labels itself. If ``executor_tag`` is missing or
    unrecognised, we default to ``claude_dev`` (the more capable executor)
    rather than failing, but ``pick_next_ticket_node`` should always populate
    it from the per-ticket dict.
    """
    tag = state.get("executor_tag", "claude-dev")
    executor = "haiku" if tag == "haiku" else "claude_dev"
    logger.info("route_node — ticket %s → executor: %s", state.get("current_ticket_id"), executor)
    return {"current_executor": executor}


def route_after_route_node(
    state: PipelineState,
) -> Literal["haiku", "claude_dev"]:
    return state.get("current_executor", "claude_dev")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dev executor — common implementation for haiku_node and claude_dev_node
# ---------------------------------------------------------------------------

def _build_prompt(
    ticket_id: str,
    integration_branch: str,
    research_context: str | None,
) -> str:
    """Construct the prompt sent to the headless Dev agent.

    The Mode B contract (output.json schema, field rules, mandate to write it
    before exit) is embedded inline so the prompt is self-contained. This avoids
    relying on Claude Code finding the Dev agent's CLAUDE.md, which lives in
    the AppFactory repo on the VM but is not in the workspace's parent chain.

    The 2026-04-22 smoke test surfaced this gap: the original prompt referred
    the agent to "the schema in your CLAUDE.md" but the agent had no way to
    find that file from the workspace, so the schema was never seen and
    output.json was never written.

    APPFACTORY_PIPELINE=1 in the env still signals Mode B for any agent that
    DOES manage to load the Dev CLAUDE.md (e.g. via ~/.claude or a sibling
    project), but the prompt no longer depends on that path.
    """
    parts = [
        "# AppFactory pipeline — Mode B (headless dev executor)",
        "",
        "## Ticket",
        f"- ID: `{ticket_id}`",
        f"- Integration branch: `{integration_branch}`",
        "",
        "## Task",
        f"Implement `{ticket_id}` against the repository in your current working directory.",
        f"- Cut a feature branch from `{integration_branch}` (NOT `main`).",
        f"- Open your PR with base = `{integration_branch}` (NOT `main`).",
        "- The workspace has a pre-push hook that refuses any push to `main`/`master`. "
        "Do not use `--no-verify` to bypass it — that is a deliberate trust check.",
        "",
        "## REQUIRED FINAL ACTION — DO NOT SKIP",
        "",
        "Before exiting, write `./output.json` in the current working directory "
        "containing exactly this shape:",
        "",
        "```json",
        "{",
        '  "status": "COMPLETED" | "BLOCKED" | "RESEARCH_NEEDED",',
        '  "pr_url": "<URL of opened PR, or null>",',
        '  "blocked_reason": "<specific reason if BLOCKED, else null>",',
        '  "research_needed_question": "<single specific question if RESEARCH_NEEDED, else null>"',
        "}",
        "```",
        "",
        "Field rules — these are not advisory:",
        "- `status` is REQUIRED and must be exactly one of the three values above.",
        '- `pr_url` MUST be a real GitHub PR URL when `status` is `"COMPLETED"`. Never null in that case.',
        "- `blocked_reason` MUST be specific. "
        'Acceptable: `"Cannot find Jira project named SANDBOX — Atlassian search returned 0 projects matching that key."` '
        'NOT acceptable: `"could not complete"`, `"hit an error"`, `"unclear"`.',
        "- `research_needed_question` MUST be a single, specific, answerable question. "
        "Vague questions like `\"how does Spotify work?\"` are not acceptable.",
        "",
        "**If you cannot complete the implementation for ANY reason — missing context, "
        "ambiguous spec, blocked dependency, anything — write `status=\"BLOCKED\"` with a "
        "specific `blocked_reason` and exit cleanly. Do NOT exit without writing "
        "`output.json`.** The pipeline treats a missing or malformed `output.json` as a "
        "reporting failure and interrupts to Geoff.",
    ]
    if research_context:
        parts.extend(
            [
                "",
                "## Research context (provided by pipeline)",
                "",
                research_context,
            ]
        )
    return "\n".join(parts)


def _repo_clone_url(repo: str) -> str:
    """Compose an HTTPS clone URL with the AppFactory PAT embedded for auth.

    Falls back to the bare URL if the PAT is not set — the clone will then
    fail with a clearer error than a silent unauthenticated attempt.
    """
    token = os.environ.get("GITHUB_APPFACTORY_PAT", "")
    if token:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def _parse_output_json(workspace: Path) -> dict:
    """Read and validate ``./output.json`` from the workspace.

    Returns a dict shaped like the agent's output schema. If the file is
    missing, malformed, or missing required fields, returns a synthetic
    BLOCKED result so the pipeline can surface the failure.
    """
    output_path = workspace / "output.json"
    if not output_path.exists():
        return {
            "status": "BLOCKED",
            "blocked_reason": "agent did not write output.json before exiting",
        }

    try:
        data = json.loads(output_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "status": "BLOCKED",
            "blocked_reason": f"output.json was not readable JSON: {exc}",
        }

    if "status" not in data:
        return {
            "status": "BLOCKED",
            "blocked_reason": "output.json missing required 'status' field",
        }

    return data


def _run_dev_executor(state: PipelineState, model: str) -> dict:
    """Common impl behind haiku_node and claude_dev_node.

    Lifecycle per dispatch:
      1. Validate required state.
      2. Prepare workspace (clone repo, install pre-push hook).
      3. Write the prompt to ``workspace/prompt.txt`` for archival.
      4. Spawn ``claude --model <model> --print <prompt>`` with cwd=workspace
         and ``APPFACTORY_PIPELINE=1`` in the env so the agent enters Mode B.
      5. Read ``workspace/output.json`` after the subprocess exits.
      6. Archive artefacts to the run-artefacts directory; clean up workspace.
      7. Return ticket_status / ticket_pr_url / blocked_reason /
         research_needed_question shaped from the agent's output.

    Failure modes are mapped to BLOCKED with a specific blocked_reason:
      - missing state fields, workspace prep failure, claude binary absent,
        subprocess non-zero exit, subprocess timeout, missing/invalid output.json.
    """
    run_id = state.get("run_id", "")
    repo = state.get("repo", "")
    integration_branch = state.get("integration_branch", "")
    ticket_id = state.get("current_ticket_id", "")
    research_context = state.get("research_context")

    if not all([run_id, repo, integration_branch, ticket_id]):
        return {
            "ticket_status": "BLOCKED",
            "blocked_reason": (
                f"executor missing required state — run_id={bool(run_id)}, "
                f"repo={bool(repo)}, integration_branch={bool(integration_branch)}, "
                f"current_ticket_id={bool(ticket_id)}"
            ),
        }

    # Prepare workspace
    try:
        workspace = prepare_workspace(run_id, _repo_clone_url(repo), integration_branch)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() if hasattr(exc, "stderr") else ""
        return {
            "ticket_status": "BLOCKED",
            "blocked_reason": f"workspace clone failed: {stderr or str(exc)}",
        }
    except (ValueError, OSError) as exc:
        return {
            "ticket_status": "BLOCKED",
            "blocked_reason": f"workspace preparation failed: {exc}",
        }

    prompt = _build_prompt(ticket_id, integration_branch, research_context)

    try:
        try:
            (workspace / "prompt.txt").write_text(prompt)
        except OSError as exc:
            # Prompt archival is best-effort — log but continue.
            logger.warning("could not write prompt.txt to workspace — %s", exc)

        subprocess_env = {**os.environ, "APPFACTORY_PIPELINE": "1"}

        logger.info(
            "_run_dev_executor: spawning claude --model %s for ticket %s in %s",
            model, ticket_id, workspace,
        )

        # --dangerously-skip-permissions: required for headless `claude --print`
        # to actually call tools (Bash, Edit, Write, ...). Without it, every tool
        # call is rejected with "requires approval" — which is fine in interactive
        # mode but fatal here. The 2026-04-22 smoke test surfaced this: the agent
        # kept getting denied, eventually gave up, exited with no output.json.
        #
        # The "danger" is that the agent has full tool access. In our model that's
        # acceptable because the security boundary is elsewhere:
        #   - Workspace is an ephemeral /tmp clone, deleted on exit (graphs/workspace.py)
        #   - Pre-push hook in workspace refuses pushes to main/master
        #   - Fine-grained PAT is scoped to specific repos only
        #   - Code-level guards in setup/merge/batch_close refuse main targets
        #   - Subprocess runs as the unprivileged `geoff` user (no sudo)
        # The Dev agent is sandboxed by infrastructure, not by a tool deny-list.
        # If we ever want a tighter allow-list, swap this flag for --allowedTools.
        try:
            result = subprocess.run(
                [
                    "claude",
                    "--model", model,
                    "--print",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                cwd=str(workspace),
                env=subprocess_env,
                capture_output=True,
                text=True,
                timeout=DEV_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {
                "ticket_status": "BLOCKED",
                "blocked_reason": (
                    f"claude subprocess timed out after "
                    f"{DEV_SUBPROCESS_TIMEOUT_SECONDS}s"
                ),
            }
        except FileNotFoundError:
            return {
                "ticket_status": "BLOCKED",
                "blocked_reason": "claude binary not found in PATH on the VM",
            }

        if result.returncode != 0:
            stderr_excerpt = (result.stderr or "").strip()[:500] or "no stderr"
            return {
                "ticket_status": "BLOCKED",
                "blocked_reason": (
                    f"claude subprocess exited {result.returncode}: {stderr_excerpt}"
                ),
            }

        parsed = _parse_output_json(workspace)
        return {
            "ticket_status": parsed.get("status"),
            "ticket_pr_url": parsed.get("pr_url"),
            "blocked_reason": parsed.get("blocked_reason"),
            "research_needed_question": parsed.get("research_needed_question"),
        }
    finally:
        # Always preserve artefacts for the 7-day debug window, then free disk.
        archive_artefacts(workspace, run_id)
        cleanup(workspace)


@observe()
def haiku_node(state: PipelineState) -> dict:
    """Headless Claude Code with Haiku — fast, cheap, well-scoped tickets.

    Routed by ``executor_tag == "haiku"`` from the Jira ticket label. On
    BLOCKED the pipeline escalates to ``claude_dev_node`` (Sonnet) once,
    then interrupts to Geoff if Sonnet also blocks.
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    return _run_dev_executor(state, "claude-haiku-4-5-20251001")


@observe()
def claude_dev_node(state: PipelineState) -> dict:
    """Headless Claude Code with Sonnet — complex / architectural tickets.

    Routed by ``executor_tag == "claude-dev"`` or as the escalation target
    after Haiku blocks. On BLOCKED the pipeline interrupts to Geoff
    (park / abort decision).
    """
    apply_run_id_to_trace(state.get("run_id", ""))
    return _run_dev_executor(state, "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Result routing
# ---------------------------------------------------------------------------

def check_result_node(state: PipelineState) -> dict:
    """Pass-through — routing logic lives in route_after_check_result."""
    return {}


def route_after_check_result(
    state: PipelineState,
) -> Literal["merge", "research_gate", "escalate"]:
    status = state.get("ticket_status")
    if status == "COMPLETED":
        return "merge"
    if status == "RESEARCH_NEEDED":
        return "research_gate"
    return "escalate"


# ---------------------------------------------------------------------------
# Research gate (development quota)
# ---------------------------------------------------------------------------

_research_gate_node = make_research_gate_node("development")


def route_after_research_gate(
    state: PipelineState,
) -> Literal["haiku", "claude_dev", "escalate"]:
    if state.get("research_gate_result") == "budget_exhausted":
        return "escalate"
    # Re-route to whichever executor was running when RESEARCH_NEEDED fired.
    executor = state.get("current_executor", "claude_dev")
    return executor  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def escalate_node(state: PipelineState) -> dict:
    """
    Handles BLOCKED tickets.

    Copilot failure: retry once with Claude Dev (from scratch — never attempt
    to pick up partial Copilot work).

    Claude Dev failure (or post-escalation failure): interrupt. Geoff decides
    whether to park the ticket and continue the batch, or abort entirely.
    """
    executor = state.get("current_executor", "claude_dev")
    escalated = state.get("escalation_attempted", False)

    if executor == "haiku" and not escalated:
        logger.info(
            "escalate_node — Haiku BLOCKED on %s. Retrying with Claude Dev (Sonnet).",
            state.get("current_ticket_id"),
        )
        return {
            "current_executor": "claude_dev",
            "escalation_attempted": True,
            "ticket_status": None,
            "ticket_pr_url": None,
            "escalation_decision": "retry_with_claude_dev",
        }

    # Claude Dev is blocked (or Copilot already escalated and still blocked).
    decision = interrupt({
        "type": "ticket_blocked",
        "ticket_id": state.get("current_ticket_id"),
        "executor": executor,
        "escalation_attempted": escalated,
        "reason": state.get("blocked_reason", "unspecified"),
        "message": (
            f"{state.get('current_ticket_id')} blocked"
            + (" after escalation to Claude Dev" if escalated else "")
            + f". Reason: {state.get('blocked_reason', 'unspecified')}"
        ),
        "hint": (
            'Reply with: {"action": "park"} to skip this ticket and continue the batch, '
            'or {"action": "abort"} to end the pipeline. '
            'Completed PRs so far: '
            + str(state.get("completed_prs", []))
        ),
    })

    action = (decision or {}).get("action", "abort")

    if action == "park":
        ticket = state.get("current_ticket_id", "")
        skipped = list(state.get("skipped_tickets") or [])
        skipped.append(ticket)
        logger.info("escalate_node — parking %s, continuing batch.", ticket)
        return {
            "skipped_tickets": skipped,
            "escalation_decision": "park_and_continue",
        }

    logger.info("escalate_node — aborting pipeline on %s.", state.get("current_ticket_id"))
    return {"escalation_decision": "abort"}


def route_after_escalate(
    state: PipelineState,
) -> Literal["claude_dev", "loop_check", "__end__"]:
    decision = state.get("escalation_decision")
    if decision == "retry_with_claude_dev":
        return "claude_dev"
    if decision == "park_and_continue":
        return "loop_check"
    return "__end__"


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _merge_failure_interrupt(state: PipelineState, reason: str) -> dict:
    """Surface a merge_node failure to Geoff and return state per his decision.

    Mirrors the escalate_node interrupt pattern: park (skip ticket, continue
    batch — PR remains open for manual handling) or abort (end pipeline).
    """
    decision = interrupt({
        "type": "merge_failed",
        "ticket_id": state.get("current_ticket_id"),
        "pr_url": state.get("ticket_pr_url"),
        "integration_branch": state.get("integration_branch"),
        "reason": reason,
        "message": (
            f"Merge of {state.get('current_ticket_id')} failed: {reason}"
        ),
        "hint": (
            'Reply with: {"action": "park"} to skip this ticket and continue '
            'the batch (PR remains open for you to handle manually), or '
            '{"action": "abort"} to end the pipeline. '
            f'Completed PRs so far: {state.get("completed_prs", [])}'
        ),
    })

    action = (decision or {}).get("action", "abort")

    if action == "park":
        ticket = state.get("current_ticket_id", "")
        skipped = list(state.get("skipped_tickets") or [])
        skipped.append(ticket)
        logger.info("merge_node: parking %s, continuing batch.", ticket)
        return {
            "skipped_tickets": skipped,
            # NOT setting escalation_decision so route_after_merge → loop_check
        }

    logger.info("merge_node: aborting pipeline on %s.", state.get("current_ticket_id"))
    return {"escalation_decision": "abort"}


def merge_node(state: PipelineState) -> dict:
    """Wait for the PR to be mergeable, merge into the integration branch, write KB summary.

    Lifecycle:
      1. Validate state (pr_url, integration_branch, repo, ticket_id present).
      2. Code-level guards: integration_branch starts with batch/, never main/master.
      3. Parse PR number from pr_url; fetch full PR via GitHub API.
      4. Verify PR.base.ref == integration_branch (refuses any other target).
      5. Poll ``mergeable_state`` until a terminal value is reached. Proceed on
         ``clean`` or ``has_hooks``; interrupt on ``unstable``/``blocked``/
         ``behind``/``dirty``/``draft``; keep polling on ``unknown``.
      6. Squash-merge via GitHub API; merge_pr() asserts base != main/master.
      7. Write a ``ticket_summary`` entry to operational_knowledge.
      8. Append PR URL to ``completed_prs`` and return.

    Failures (merge blocked, polling timeout, merge API failed, target mismatch,
    parse failures) interrupt to Geoff with park/abort options. KB write
    failures are logged but never block the merge — the merge is what matters.
    """
    pr_url = state.get("ticket_pr_url", "")
    integration_branch = state.get("integration_branch", "")
    repo = state.get("repo", "")
    ticket_id = state.get("current_ticket_id", "")

    if not all([pr_url, integration_branch, repo, ticket_id]):
        return _merge_failure_interrupt(
            state,
            f"merge_node missing required state — pr_url={bool(pr_url)}, "
            f"integration_branch={bool(integration_branch)}, "
            f"repo={bool(repo)}, ticket_id={bool(ticket_id)}",
        )

    # Code-level guards — never merge into main/master via this node.
    assert integration_branch.startswith("batch/"), (
        f"merge_node refuses to merge into non-batch branch: {integration_branch}"
    )
    assert integration_branch not in ("main", "master"), (
        f"merge_node refuses to merge into protected branch: {integration_branch}"
    )

    pr_number = parse_pr_number(pr_url)
    if pr_number is None:
        return _merge_failure_interrupt(
            state, f"could not parse PR number from URL: {pr_url}"
        )

    # Fetch PR to verify target + get head SHA for CI polling
    try:
        pr = get_pr(repo, pr_number)
    except requests.HTTPError as exc:
        return _merge_failure_interrupt(state, f"failed to fetch PR info: {exc}")

    actual_target = pr.get("base", {}).get("ref", "")
    if actual_target != integration_branch:
        return _merge_failure_interrupt(
            state,
            f"PR targets {actual_target!r}, expected {integration_branch!r} — refusing to merge",
        )

    head_sha = pr.get("head", {}).get("sha", "")
    if not head_sha:
        return _merge_failure_interrupt(state, "PR head SHA missing from API response")

    # Wait for GitHub to report a terminal mergeable_state on the PR. This
    # implicitly waits on CI (GitHub folds check results into mergeable_state)
    # without requiring the "Checks: Read" PAT permission, which fine-grained
    # PATs on personal accounts cannot grant. See note above wait_for_mergeable.
    try:
        wait_for_mergeable(repo, pr_number)
    except MergeBlocked as exc:
        reason_by_state = {
            "unstable": "non-required check(s) failing",
            "blocked": "required checks failing or branch protection blocking merge",
            "behind": "head branch is out of date with base — cannot auto-update",
            "dirty": "merge conflict — not resolvable by the pipeline",
            "draft": "PR is still a draft",
        }
        detail = reason_by_state.get(exc.state, "unspecified blocking state")
        return _merge_failure_interrupt(
            state,
            f"merge blocked by PR state {exc.state!r} — {detail}",
        )
    except MergeTimeout as exc:
        return _merge_failure_interrupt(
            state,
            f"mergeable_state polling timed out (last state: {exc.state!r})",
        )
    except requests.HTTPError as exc:
        return _merge_failure_interrupt(state, f"mergeable_state polling failed: {exc}")

    # Merge — merge_pr also enforces the never-main guard
    try:
        merge_sha = merge_pr(
            repo,
            pr_number,
            base_branch=integration_branch,
            commit_title=f"{ticket_id}: {pr.get('title', '').lstrip(f'[{ticket_id}] ').lstrip(f'{ticket_id}: ').strip()}",
        )
    except requests.HTTPError as exc:
        return _merge_failure_interrupt(state, f"merge API call failed: {exc}")
    except ValueError as exc:
        # merge_pr's internal guard fired — should be caught by the assertion
        # above, but defence in depth.
        return _merge_failure_interrupt(state, f"merge guard refused operation: {exc}")

    logger.info(
        "merge_node: merged %s#%d into %s as %s",
        repo, pr_number, integration_branch, merge_sha[:8],
    )

    # Write a ticket_summary entry to operational_knowledge — best effort.
    # KB write failures are logged but never block the merge result.
    try:
        embed_and_store({
            "agent": "merge_node",
            "task": f"{ticket_id}: {pr.get('title', '')}",
            "output": pr.get("body") or "(no PR description)",
            "run_id": state.get("run_id", ""),
            "graph_id": "iterative_dev",
            "project_key": state.get("project_key", ""),
            "kind": "ticket_summary",
            "ticket_id": ticket_id,
        })
    except Exception as exc:
        logger.warning("merge_node: KB write failed for %s — %s", ticket_id, exc)

    completed = list(state.get("completed_prs") or [])
    completed.append(pr_url)
    return {"completed_prs": completed}


def route_after_merge(
    state: PipelineState,
) -> Literal["loop_check", "__end__"]:
    """Route after merge_node — abort ends the pipeline; everything else
    proceeds to the next ticket via loop_check.

    Reuses ``escalation_decision`` (set by escalate_node or by merge_node's
    interrupt path) so there's a single sentinel for "end the pipeline now".
    """
    if state.get("escalation_decision") == "abort":
        return "__end__"
    return "loop_check"


# ---------------------------------------------------------------------------
# Loop control
# ---------------------------------------------------------------------------

def loop_check_node(state: PipelineState) -> dict:
    """Advances the ticket index after a ticket is resolved (completed or parked)."""
    next_index = (state.get("current_ticket_index") or 0) + 1
    return {"current_ticket_index": next_index}


def route_after_loop_check(
    state: PipelineState,
) -> Literal["pick_next_ticket", "batch_close"]:
    index = state.get("current_ticket_index", 0)
    tickets = state.get("tickets", [])
    if index >= len(tickets):
        return "batch_close"
    return "pick_next_ticket"


# ---------------------------------------------------------------------------
# Batch close
# ---------------------------------------------------------------------------

def _build_batch_pr_body(
    sprint_number: int | str,
    completed: list[str],
    skipped: list[str],
) -> str:
    """Render the batch PR body listing completed and skipped tickets."""
    lines = [
        f"## Batch summary — sprint {sprint_number}",
        "",
        f"**Completed tickets ({len(completed)}):**",
    ]
    if completed:
        lines.extend(f"- {url}" for url in completed)
    else:
        lines.append("- (none)")

    if skipped:
        lines.extend(["", f"**Parked / skipped tickets ({len(skipped)}):**"])
        lines.extend(f"- {ticket}" for ticket in skipped)

    lines.extend([
        "",
        "---",
        "Opened by AppFactory pipeline. **Do not merge unless Geoff has reviewed staging.**",
        "Telegram notification deferred to Phase 5.5.",
    ])
    return "\n".join(lines)


def batch_close_node(state: PipelineState) -> dict:
    """Open the batch PR (integration_branch → main, **opened only, never merged**),
    trigger staging deploy, and write a batch_summary entry to operational_knowledge.

    Lifecycle:
      1. Validate state (integration_branch, repo present).
      2. Code-level guard: integration_branch starts with batch/, never main.
      3. Build PR title and body from sprint number, completed_prs, skipped_tickets.
      4. Open PR via GitHub API. ``open_pr`` ensures head is not main/master.
         The pipeline NEVER merges this PR — only Geoff merges to main.
      5. Trigger staging deploy via SSH. Best effort: a deploy failure is logged
         and the batch_pr_url is still returned so Geoff sees the PR.
      6. Write a kind="batch_summary" KB entry. Best effort.

    Returns ``{"batch_pr_url": <url or None>}``. None signals that the batch
    PR could not be opened — Geoff should be notified, but the pipeline does
    not interrupt here (the batch is over either way).
    """
    integration_branch = state.get("integration_branch", "")
    repo = state.get("repo", "")
    completed = state.get("completed_prs", [])
    skipped = state.get("skipped_tickets", [])
    sprint_number = state.get("sprint_number", "?")

    if not integration_branch or not repo:
        logger.error(
            "batch_close_node missing required state — integration_branch=%r, repo=%r",
            integration_branch, repo,
        )
        return {"batch_pr_url": None}

    # Code-level guards — never source the batch PR from main.
    assert integration_branch.startswith("batch/"), (
        f"batch_close_node would open PR from non-batch branch: {integration_branch}"
    )
    assert integration_branch not in ("main", "master"), (
        f"batch_close_node would open PR sourced from protected branch: {integration_branch}"
    )

    title = (
        f"AppFactory batch — sprint {sprint_number} "
        f"({len(completed)} ticket{'s' if len(completed) != 1 else ''})"
    )
    body = _build_batch_pr_body(sprint_number, list(completed), list(skipped))

    # Open the PR — opened only, never merged
    batch_pr_url: str | None = None
    try:
        batch_pr_url = open_pr(repo, integration_branch, "main", title, body)
        logger.info("batch_close_node: opened batch PR %s", batch_pr_url)
    except requests.HTTPError as exc:
        logger.error("batch_close_node: failed to open batch PR — %s", exc)
        # Continue anyway — staging deploy and KB write may still be useful
    except ValueError as exc:
        logger.error("batch_close_node: open_pr guard refused operation — %s", exc)

    # Staging deploy — best effort. Failure does not block batch close.
    try:
        staging_path = staging_path_for_repo(repo)
        deploy_staging(staging_path)
        logger.info("batch_close_node: staging deployed at %s", staging_path)
    except (ValueError, DeployFailed) as exc:
        logger.warning(
            "batch_close_node: staging deploy did not complete for %s — %s",
            repo, exc,
        )

    # KB write — best effort
    try:
        embed_and_store({
            "agent": "batch_close_node",
            "task": title,
            "output": body,
            "run_id": state.get("run_id", ""),
            "graph_id": "iterative_dev",
            "project_key": state.get("project_key", ""),
            "kind": "batch_summary",
            "ticket_id": None,
        })
    except Exception as exc:
        logger.warning("batch_close_node: KB write failed — %s", exc)

    return {"batch_pr_url": batch_pr_url}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

_builder = StateGraph(PipelineState)

_builder.add_node("setup", setup_node)
_builder.add_node("pick_next_ticket", pick_next_ticket_node)
_builder.add_node("route", route_node)
_builder.add_node("haiku", haiku_node)
_builder.add_node("claude_dev", claude_dev_node)
_builder.add_node("check_result", check_result_node)
_builder.add_node("research_gate", _research_gate_node)
_builder.add_node("escalate", escalate_node)
_builder.add_node("merge", merge_node)
_builder.add_node("loop_check", loop_check_node)
_builder.add_node("batch_close", batch_close_node)

_builder.set_entry_point("setup")
_builder.add_edge("setup", "pick_next_ticket")
_builder.add_edge("pick_next_ticket", "route")
_builder.add_conditional_edges("route", route_after_route_node, {"haiku": "haiku", "claude_dev": "claude_dev"})
_builder.add_edge("haiku", "check_result")
_builder.add_edge("claude_dev", "check_result")
_builder.add_conditional_edges(
    "check_result",
    route_after_check_result,
    {"merge": "merge", "research_gate": "research_gate", "escalate": "escalate"},
)
_builder.add_conditional_edges(
    "research_gate",
    route_after_research_gate,
    {"haiku": "haiku", "claude_dev": "claude_dev", "escalate": "escalate"},
)
_builder.add_conditional_edges(
    "escalate",
    route_after_escalate,
    {"claude_dev": "claude_dev", "loop_check": "loop_check", "__end__": END},
)
_builder.add_conditional_edges(
    "merge",
    route_after_merge,
    {"loop_check": "loop_check", "__end__": END},
)
_builder.add_conditional_edges(
    "loop_check",
    route_after_loop_check,
    {"pick_next_ticket": "pick_next_ticket", "batch_close": "batch_close"},
)
_builder.add_edge("batch_close", END)

graph = _builder.compile()

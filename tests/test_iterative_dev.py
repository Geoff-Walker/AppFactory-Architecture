"""
Unit tests for graphs/iterative_dev.py and graphs/research_gate.py

All state machine logic is tested without LLM calls or API calls.
interrupt() is mocked via unittest.mock.patch where needed.

Coverage:
  - route_node / route_after_route_node — executor selection
  - check_result routing — COMPLETED / RESEARCH_NEEDED / BLOCKED
  - research_gate — budget available dispatch, budget exhausted → BLOCKED
  - route_after_research_gate — dispatched → correct executor, exhausted → escalate
  - escalate_node — haiku→claude_dev retry, claude_dev→interrupt park/abort
  - route_after_escalate — all three outcomes
  - loop_check — index advance, done vs continue routing
  - merge_node — completed_prs accumulation
  - setup_node — branch name, state initialisation
  - pick_next_ticket_node — correct ticket selection, per-ticket state reset
  - batch_close_node — stub returns batch_pr_url
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from graphs.iterative_dev import (
    batch_close_node,
    check_result_node,
    claude_dev_node,
    haiku_node,
    escalate_node,
    loop_check_node,
    merge_node,
    pick_next_ticket_node,
    route_after_check_result,
    route_after_escalate,
    route_after_loop_check,
    route_after_research_gate,
    route_after_route_node,
    route_node,
    setup_node,
)
from graphs.github_api import MergeBlocked, MergeTimeout
from graphs.research_gate import make_research_gate_node
from graphs.state import PipelineState


# ---------------------------------------------------------------------------
# setup_node
# ---------------------------------------------------------------------------

class TestSetupNode:
    """setup_node now hits the GitHub API; all external calls are mocked here."""

    def test_creates_integration_branch_with_explicit_sprint(self):
        """When the caller passes sprint_number, that value is used directly (no scan)."""
        state: PipelineState = {"sprint_number": 3, "repo": "Geoff-Walker/FamilyCookbook"}
        with patch("graphs.iterative_dev.get_branch_sha", return_value="abc123def456"), \
             patch("graphs.iterative_dev.create_branch") as mock_create, \
             patch("graphs.iterative_dev.next_sprint_number") as mock_next:
            result = setup_node(state)

        assert result["integration_branch"] == "batch/sprint-3"
        assert result["sprint_number"] == 3
        # next_sprint_number must not be called when sprint_number is provided
        mock_next.assert_not_called()
        # create_branch was called with the right repo, branch, and sha
        mock_create.assert_called_once_with("Geoff-Walker/FamilyCookbook", "batch/sprint-3", "abc123def456")

    def test_derives_sprint_from_existing_branches_when_omitted(self):
        """Without explicit sprint_number, the function scans the repo via next_sprint_number."""
        state: PipelineState = {"repo": "Geoff-Walker/tom"}
        with patch("graphs.iterative_dev.next_sprint_number", return_value=7) as mock_next, \
             patch("graphs.iterative_dev.get_branch_sha", return_value="sha"), \
             patch("graphs.iterative_dev.create_branch"):
            result = setup_node(state)

        mock_next.assert_called_once_with("Geoff-Walker/tom")
        assert result["integration_branch"] == "batch/sprint-7"
        assert result["sprint_number"] == 7

    def test_initialises_tracking_lists(self):
        state: PipelineState = {"sprint_number": 1, "repo": "Geoff-Walker/tom"}
        with patch("graphs.iterative_dev.get_branch_sha", return_value="sha"), \
             patch("graphs.iterative_dev.create_branch"):
            result = setup_node(state)

        assert result["completed_prs"] == []
        assert result["skipped_tickets"] == []
        assert result["research_dispatches"] == {}
        assert result["current_ticket_index"] == 0

    def test_run_id_extracted_from_config_metadata(self):
        """LangGraph's run_id (passed via config.metadata) is written to state."""
        state: PipelineState = {"sprint_number": 1, "repo": "Geoff-Walker/tom"}
        config = {"metadata": {"run_id": "lg-run-7777"}}
        with patch("graphs.iterative_dev.get_branch_sha", return_value="sha"), \
             patch("graphs.iterative_dev.create_branch"):
            result = setup_node(state, config)
        assert result["run_id"] == "lg-run-7777"

    def test_run_id_extracted_from_config_configurable_fallback(self):
        """Defensive fallback: run_id may live in config.configurable on some paths."""
        state: PipelineState = {"sprint_number": 1, "repo": "Geoff-Walker/tom"}
        config = {"configurable": {"run_id": "lg-run-cfg-1"}}
        with patch("graphs.iterative_dev.get_branch_sha", return_value="sha"), \
             patch("graphs.iterative_dev.create_branch"):
            result = setup_node(state, config)
        assert result["run_id"] == "lg-run-cfg-1"

    def test_run_id_empty_when_config_omitted(self):
        """No config → empty run_id (executor pre-flight will fail fast with a clear reason)."""
        state: PipelineState = {"sprint_number": 1, "repo": "Geoff-Walker/tom"}
        with patch("graphs.iterative_dev.get_branch_sha", return_value="sha"), \
             patch("graphs.iterative_dev.create_branch"):
            result = setup_node(state)
        assert result["run_id"] == ""

    def test_missing_repo_raises_value_error(self):
        """The repo slug is required — failing fast is the right call."""
        with pytest.raises(ValueError, match="repo"):
            setup_node({"sprint_number": 1})

    def test_create_branch_invoked_with_main_sha(self):
        """The branch is cut from main's current SHA — verified by the chain of calls."""
        state: PipelineState = {"sprint_number": 5, "repo": "Geoff-Walker/appfactory"}
        with patch("graphs.iterative_dev.get_branch_sha", return_value="abcd1234") as mock_sha, \
             patch("graphs.iterative_dev.create_branch") as mock_create:
            setup_node(state)

        mock_sha.assert_called_once_with("Geoff-Walker/appfactory", "main")
        mock_create.assert_called_once_with("Geoff-Walker/appfactory", "batch/sprint-5", "abcd1234")


# ---------------------------------------------------------------------------
# pick_next_ticket_node
# ---------------------------------------------------------------------------

class TestPickNextTicketNode:
    def test_selects_correct_ticket(self):
        state: PipelineState = {
            "tickets": [
                {"id": "WAL-10", "executor": "haiku"},
                {"id": "WAL-11", "executor": "claude-dev"},
                {"id": "WAL-12", "executor": "haiku"},
            ],
            "current_ticket_index": 1,
        }
        result = pick_next_ticket_node(state)
        assert result["current_ticket_id"] == "WAL-11"

    def test_writes_executor_tag_for_haiku_ticket(self):
        state: PipelineState = {
            "tickets": [{"id": "WAL-10", "executor": "haiku"}],
            "current_ticket_index": 0,
        }
        result = pick_next_ticket_node(state)
        assert result["executor_tag"] == "haiku"

    def test_writes_executor_tag_for_claude_dev_ticket(self):
        state: PipelineState = {
            "tickets": [{"id": "WAL-10", "executor": "claude-dev"}],
            "current_ticket_index": 0,
        }
        result = pick_next_ticket_node(state)
        assert result["executor_tag"] == "claude-dev"

    def test_normalises_executor_prefix_haiku(self):
        """Raw Jira labels (``executor:haiku``) are stripped to the bare tag."""
        state: PipelineState = {
            "tickets": [{"id": "WAL-10", "executor": "executor:haiku"}],
            "current_ticket_index": 0,
        }
        result = pick_next_ticket_node(state)
        assert result["executor_tag"] == "haiku"

    def test_normalises_executor_prefix_claude_dev(self):
        state: PipelineState = {
            "tickets": [{"id": "WAL-10", "executor": "executor:claude-dev"}],
            "current_ticket_index": 0,
        }
        result = pick_next_ticket_node(state)
        assert result["executor_tag"] == "claude-dev"

    def test_mixed_batch_writes_tag_per_ticket(self):
        """The executor_tag changes on each loop iteration for mixed batches."""
        tickets = [
            {"id": "AFT-1", "executor": "haiku"},
            {"id": "AFT-2", "executor": "claude-dev"},
            {"id": "AFT-3", "executor": "executor:haiku"},
        ]
        # Iteration 0 — first ticket
        r0 = pick_next_ticket_node({"tickets": tickets, "current_ticket_index": 0})
        assert r0["current_ticket_id"] == "AFT-1"
        assert r0["executor_tag"] == "haiku"
        # Iteration 1 — second ticket, different executor
        r1 = pick_next_ticket_node({"tickets": tickets, "current_ticket_index": 1})
        assert r1["current_ticket_id"] == "AFT-2"
        assert r1["executor_tag"] == "claude-dev"
        # Iteration 2 — third ticket, raw-label form
        r2 = pick_next_ticket_node({"tickets": tickets, "current_ticket_index": 2})
        assert r2["current_ticket_id"] == "AFT-3"
        assert r2["executor_tag"] == "haiku"

    def test_resets_per_ticket_state(self):
        state: PipelineState = {
            "tickets": [{"id": "WAL-10", "executor": "haiku"}],
            "current_ticket_index": 0,
            "ticket_status": "BLOCKED",
            "escalation_attempted": True,
            "research_context": "some prior findings",
        }
        result = pick_next_ticket_node(state)
        assert result["ticket_status"] is None
        assert result["escalation_attempted"] is False
        assert result["research_context"] is None
        assert result["blocked_reason"] is None


# ---------------------------------------------------------------------------
# route_node / route_after_route_node
# ---------------------------------------------------------------------------

class TestRouteNode:
    def test_haiku_tag_sets_haiku_executor(self):
        result = route_node({"executor_tag": "haiku"})
        assert result["current_executor"] == "haiku"

    def test_claude_dev_tag_sets_claude_dev_executor(self):
        result = route_node({"executor_tag": "claude-dev"})
        assert result["current_executor"] == "claude_dev"

    def test_missing_tag_defaults_to_claude_dev(self):
        result = route_node({})
        assert result["current_executor"] == "claude_dev"


class TestRouteAfterRouteNode:
    def test_routes_haiku(self):
        assert route_after_route_node({"current_executor": "haiku"}) == "haiku"

    def test_routes_claude_dev(self):
        assert route_after_route_node({"current_executor": "claude_dev"}) == "claude_dev"

    def test_defaults_to_claude_dev(self):
        assert route_after_route_node({}) == "claude_dev"


class TestPerTicketRouting:
    """End-to-end: pick_next_ticket_node → route_node → route_after_route_node.

    Locks in the contract that each ticket in a mixed batch routes to the
    executor declared on its own dict entry — not whatever single value
    happened to be passed at dispatch time.
    """

    def _route_for(self, ticket: dict) -> str:
        state = {"tickets": [ticket], "current_ticket_index": 0}
        picked = pick_next_ticket_node(state)
        # Forward picked state into route_node (TypedDict total=False → merge by hand)
        merged = {**state, **{k: v for k, v in picked.items() if v is not None}}
        routed = route_node(merged)
        merged.update(routed)
        return route_after_route_node(merged)

    def test_single_ticket_haiku_routes_to_haiku(self):
        assert self._route_for({"id": "AFT-1", "executor": "haiku"}) == "haiku"

    def test_single_ticket_claude_dev_routes_to_claude_dev(self):
        assert self._route_for({"id": "AFT-1", "executor": "claude-dev"}) == "claude_dev"

    def test_raw_label_executor_haiku_routes_to_haiku(self):
        assert self._route_for({"id": "AFT-1", "executor": "executor:haiku"}) == "haiku"

    def test_raw_label_executor_claude_dev_routes_to_claude_dev(self):
        assert self._route_for({"id": "AFT-1", "executor": "executor:claude-dev"}) == "claude_dev"

    def test_mixed_batch_routes_each_ticket_to_its_own_executor(self):
        """The whole point of per-ticket mapping: each iteration picks up the right executor."""
        tickets = [
            {"id": "AFT-1", "executor": "haiku"},
            {"id": "AFT-2", "executor": "claude-dev"},
            {"id": "AFT-3", "executor": "executor:haiku"},
        ]
        routed = []
        for i in range(len(tickets)):
            state = {"tickets": tickets, "current_ticket_index": i}
            picked = pick_next_ticket_node(state)
            merged = {**state, **{k: v for k, v in picked.items() if v is not None}}
            routed.append(route_after_route_node({**merged, **route_node(merged)}))
        assert routed == ["haiku", "claude_dev", "haiku"]


# ---------------------------------------------------------------------------
# Dev executors — haiku_node + claude_dev_node (real impl, subprocess + workspace)
# ---------------------------------------------------------------------------

class TestDevExecutor:
    """Tests for the shared _run_dev_executor logic via haiku_node/claude_dev_node.

    All external boundaries are mocked: workspace prep, subprocess, output.json
    parsing, artefact archiving, cleanup. The point of these tests is to lock
    in the failure-mode → BLOCKED mapping that the rest of the pipeline depends
    on (escalate_node etc).
    """

    def _required_state(self, **overrides) -> dict:
        """Minimal state that satisfies the executor's required-fields check."""
        base = {
            "run_id": "run-test-1",
            "repo": "Geoff-Walker/FamilyCookbook",
            "integration_branch": "batch/sprint-3",
            "current_ticket_id": "WAL-42",
        }
        base.update(overrides)
        return base

    # --- happy path ---

    def test_completed_status_returns_pr_url(self, tmp_path):
        """Subprocess succeeds, output.json valid → status COMPLETED with pr_url."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text(
            '{"status": "COMPLETED", "pr_url": "https://github.com/x/y/pull/42",'
            ' "blocked_reason": null, "research_needed_question": null}'
        )

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "COMPLETED"
        assert result["ticket_pr_url"] == "https://github.com/x/y/pull/42"
        assert result["blocked_reason"] is None
        assert result["research_needed_question"] is None

    def test_research_needed_status_passes_question_through(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text(
            '{"status": "RESEARCH_NEEDED", "pr_url": null, "blocked_reason": null,'
            ' "research_needed_question": "What rate limit does Spotify enforce?"}'
        )

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            result = claude_dev_node(self._required_state())

        assert result["ticket_status"] == "RESEARCH_NEEDED"
        assert result["research_needed_question"] == "What rate limit does Spotify enforce?"

    # --- model selection ---

    def test_haiku_node_invokes_haiku_model(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            haiku_node(self._required_state())

        # Subprocess args:
        # ["claude", "--model", "<model>", "--print", "--dangerously-skip-permissions", "<prompt>"]
        args = mock_run.call_args[0][0]
        assert args[0] == "claude"
        assert args[1] == "--model"
        assert args[2] == "claude-haiku-4-5-20251001"
        assert "--dangerously-skip-permissions" in args
        assert "--print" in args

    def test_claude_dev_node_invokes_sonnet_model(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            claude_dev_node(self._required_state())

        args = mock_run.call_args[0][0]
        assert args[2] == "claude-sonnet-4-6"

    # --- prompt construction ---

    def test_prompt_includes_ticket_and_branch(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            haiku_node(self._required_state(current_ticket_id="WAL-99",
                                            integration_branch="batch/sprint-7"))

        prompt = mock_run.call_args[0][0][5]
        assert "WAL-99" in prompt
        assert "batch/sprint-7" in prompt
        assert "output.json" in prompt
        assert "NOT `main`" in prompt

    def test_prompt_includes_research_context_when_present(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        state = self._required_state(research_context="Spotify limits = 100 req/min")

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            haiku_node(state)

        prompt = mock_run.call_args[0][0][5]
        assert "Research context" in prompt
        assert "Spotify limits = 100 req/min" in prompt

    # --- prompt is self-contained for output.json contract (Mode B inline) ---

    def test_prompt_embeds_output_json_schema(self, tmp_path):
        """The Mode B output.json contract must be in the prompt itself, not via CLAUDE.md.

        The 2026-04-22 smoke test surfaced this: the agent never sees a
        Dev-agent CLAUDE.md from the workspace, so the schema must be inline.
        """
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            haiku_node(self._required_state())

        prompt = mock_run.call_args[0][0][5]
        # All four fields of the schema named explicitly
        assert '"status"' in prompt
        assert '"pr_url"' in prompt
        assert '"blocked_reason"' in prompt
        assert '"research_needed_question"' in prompt
        # Status enum values present
        assert '"COMPLETED"' in prompt
        assert '"BLOCKED"' in prompt
        assert '"RESEARCH_NEEDED"' in prompt
        # Mandate to write before exit, in unambiguous terms
        assert "output.json" in prompt
        assert "Do NOT exit without writing" in prompt or "do not exit without writing" in prompt.lower()

    def test_prompt_warns_against_pre_push_bypass(self, tmp_path):
        """The agent must be told explicitly not to use --no-verify."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            haiku_node(self._required_state())

        prompt = mock_run.call_args[0][0][5]
        assert "--no-verify" in prompt
        assert "pre-push hook" in prompt

    # --- env injection ---

    def test_subprocess_env_includes_appfactory_pipeline_flag(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            haiku_node(self._required_state())

        env = mock_run.call_args.kwargs["env"]
        assert env["APPFACTORY_PIPELINE"] == "1"

    # --- failure modes ---

    def test_missing_required_state_returns_blocked(self):
        """No subprocess is spawned if required state is missing."""
        with patch("graphs.iterative_dev.prepare_workspace") as mock_ws, \
             patch("graphs.iterative_dev.subprocess.run") as mock_run:
            result = haiku_node({"run_id": "x"})  # no repo, branch, ticket

        assert result["ticket_status"] == "BLOCKED"
        assert "missing required state" in result["blocked_reason"]
        mock_ws.assert_not_called()
        mock_run.assert_not_called()

    def test_workspace_clone_failure_returns_blocked(self, tmp_path):
        """git clone failure surfaces as BLOCKED with the stderr in the reason."""
        from subprocess import CalledProcessError

        with patch(
            "graphs.iterative_dev.prepare_workspace",
            side_effect=CalledProcessError(128, "git clone", stderr="repo not found"),
        ):
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "workspace clone failed" in result["blocked_reason"]
        assert "repo not found" in result["blocked_reason"]

    def test_subprocess_nonzero_exit_returns_blocked(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 137
            mock_run.return_value.stderr = "killed by OOM"
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "exited 137" in result["blocked_reason"]
        assert "killed by OOM" in result["blocked_reason"]

    def test_subprocess_timeout_returns_blocked(self, tmp_path):
        from subprocess import TimeoutExpired

        ws = tmp_path / "ws"
        ws.mkdir()

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch(
                 "graphs.iterative_dev.subprocess.run",
                 side_effect=TimeoutExpired(cmd="claude", timeout=3600),
             ), \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "timed out" in result["blocked_reason"]

    def test_claude_binary_missing_returns_blocked(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run", side_effect=FileNotFoundError("claude")), \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "claude binary not found" in result["blocked_reason"]

    def test_missing_output_json_returns_blocked(self, tmp_path):
        """Subprocess succeeded but didn't write output.json — pipeline fails clearly."""
        ws = tmp_path / "ws"
        ws.mkdir()
        # Note: NO output.json written

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "did not write output.json" in result["blocked_reason"]

    def test_malformed_output_json_returns_blocked(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text("this is not json {{")

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "not readable JSON" in result["blocked_reason"]

    def test_output_json_missing_status_field_returns_blocked(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"pr_url": "https://x"}')  # no status

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts"), \
             patch("graphs.iterative_dev.cleanup"):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            result = haiku_node(self._required_state())

        assert result["ticket_status"] == "BLOCKED"
        assert "missing required 'status'" in result["blocked_reason"]

    # --- artefact lifecycle ---

    def test_artefacts_archived_and_workspace_cleaned_on_success(self, tmp_path):
        """archive_artefacts and cleanup are always called once the workspace exists."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts") as mock_archive, \
             patch("graphs.iterative_dev.cleanup") as mock_cleanup:
            mock_run.return_value.returncode = 0
            haiku_node(self._required_state())

        mock_archive.assert_called_once_with(ws, "run-test-1")
        mock_cleanup.assert_called_once_with(ws)

    def test_artefacts_archived_and_workspace_cleaned_on_subprocess_failure(self, tmp_path):
        """Even when the subprocess fails, artefacts are still preserved for debugging."""
        ws = tmp_path / "ws"
        ws.mkdir()

        with patch("graphs.iterative_dev.prepare_workspace", return_value=ws), \
             patch("graphs.iterative_dev.subprocess.run") as mock_run, \
             patch("graphs.iterative_dev.archive_artefacts") as mock_archive, \
             patch("graphs.iterative_dev.cleanup") as mock_cleanup:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            haiku_node(self._required_state())

        mock_archive.assert_called_once_with(ws, "run-test-1")
        mock_cleanup.assert_called_once_with(ws)


# ---------------------------------------------------------------------------
# check_result routing
# ---------------------------------------------------------------------------

class TestRouteAfterCheckResult:
    def test_completed_routes_to_merge(self):
        assert route_after_check_result({"ticket_status": "COMPLETED"}) == "merge"

    def test_research_needed_routes_to_research_gate(self):
        assert route_after_check_result({"ticket_status": "RESEARCH_NEEDED"}) == "research_gate"

    def test_blocked_routes_to_escalate(self):
        assert route_after_check_result({"ticket_status": "BLOCKED"}) == "escalate"

    def test_none_status_routes_to_escalate(self):
        assert route_after_check_result({}) == "escalate"


# ---------------------------------------------------------------------------
# research_gate — make_research_gate_node
# ---------------------------------------------------------------------------

class TestResearchGateNode:
    def test_dispatches_when_budget_available(self):
        gate = make_research_gate_node("development")
        state: PipelineState = {
            "research_dispatches": {},
            "research_needed_question": "How does Qdrant HNSW indexing work?",
        }
        result = gate(state)
        assert result["research_gate_result"] == "dispatched"
        assert result["research_dispatches"]["development"] == 1
        assert result["ticket_status"] is None
        assert "research_context" in result

    def test_budget_exhausted_converts_to_blocked(self):
        gate = make_research_gate_node("development")
        state: PipelineState = {
            "research_dispatches": {"development": 1},  # quota is 1
            "research_needed_question": "Something else",
        }
        result = gate(state)
        assert result["research_gate_result"] == "budget_exhausted"
        assert result["ticket_status"] == "BLOCKED"
        assert "quota exhausted" in result["blocked_reason"].lower()

    def test_dispatches_up_to_quota(self):
        gate = make_research_gate_node("qa")
        state: PipelineState = {
            "research_dispatches": {},
            "research_needed_question": "What is the correct Jira API endpoint?",
        }
        result = gate(state)
        assert result["research_dispatches"]["qa"] == 1

        # Second call — budget exhausted
        state2: PipelineState = {
            "research_dispatches": result["research_dispatches"],
            "research_needed_question": "Another question",
        }
        result2 = gate(state2)
        assert result2["research_gate_result"] == "budget_exhausted"

    def test_unknown_node_type_has_zero_quota(self):
        gate = make_research_gate_node("design")  # not in server-side quotas
        state: PipelineState = {
            "research_dispatches": {},
            "research_needed_question": "Trend research",
        }
        result = gate(state)
        assert result["research_gate_result"] == "budget_exhausted"

    def test_dispatches_inject_findings_into_research_context(self):
        gate = make_research_gate_node("development")
        state: PipelineState = {
            "research_dispatches": {},
            "research_needed_question": "Redis pub/sub patterns",
        }
        result = gate(state)
        assert result.get("research_context") is not None
        assert len(result["research_context"]) > 0


class TestRouteAfterResearchGate:
    def test_dispatched_routes_to_haiku(self):
        state: PipelineState = {
            "research_gate_result": "dispatched",
            "current_executor": "haiku",
        }
        assert route_after_research_gate(state) == "haiku"

    def test_dispatched_routes_to_claude_dev(self):
        state: PipelineState = {
            "research_gate_result": "dispatched",
            "current_executor": "claude_dev",
        }
        assert route_after_research_gate(state) == "claude_dev"

    def test_budget_exhausted_routes_to_escalate(self):
        state: PipelineState = {
            "research_gate_result": "budget_exhausted",
            "current_executor": "claude_dev",
        }
        assert route_after_research_gate(state) == "escalate"

    def test_dispatched_defaults_to_claude_dev_when_executor_missing(self):
        state: PipelineState = {"research_gate_result": "dispatched"}
        assert route_after_research_gate(state) == "claude_dev"


# ---------------------------------------------------------------------------
# escalate_node
# ---------------------------------------------------------------------------

class TestEscalateNode:
    def test_haiku_first_block_retries_with_claude_dev(self):
        state: PipelineState = {
            "current_executor": "haiku",
            "escalation_attempted": False,
            "current_ticket_id": "WAL-42",
            "blocked_reason": "PR timeout",
        }
        result = escalate_node(state)
        assert result["current_executor"] == "claude_dev"
        assert result["escalation_attempted"] is True
        assert result["escalation_decision"] == "retry_with_claude_dev"
        assert result["ticket_status"] is None

    def test_claude_dev_block_fires_interrupt_park(self):
        state: PipelineState = {
            "current_executor": "claude_dev",
            "escalation_attempted": False,
            "current_ticket_id": "WAL-43",
            "blocked_reason": "Missing schema migration",
            "completed_prs": ["https://github.com/stub/pulls/1"],
            "skipped_tickets": [],
        }
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "park"}):
            result = escalate_node(state)
        assert result["escalation_decision"] == "park_and_continue"
        assert "WAL-43" in result["skipped_tickets"]

    def test_claude_dev_block_fires_interrupt_abort(self):
        state: PipelineState = {
            "current_executor": "claude_dev",
            "escalation_attempted": False,
            "current_ticket_id": "WAL-44",
            "blocked_reason": "Cannot proceed",
            "completed_prs": [],
            "skipped_tickets": [],
        }
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "abort"}):
            result = escalate_node(state)
        assert result["escalation_decision"] == "abort"

    def test_haiku_escalated_then_blocked_fires_interrupt(self):
        state: PipelineState = {
            "current_executor": "claude_dev",
            "escalation_attempted": True,  # already retried
            "current_ticket_id": "WAL-45",
            "blocked_reason": "Dependency missing",
            "completed_prs": [],
            "skipped_tickets": [],
        }
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "park"}):
            result = escalate_node(state)
        assert result["escalation_decision"] == "park_and_continue"

    def test_interrupt_no_action_defaults_to_abort(self):
        state: PipelineState = {
            "current_executor": "claude_dev",
            "escalation_attempted": False,
            "current_ticket_id": "WAL-46",
            "completed_prs": [],
            "skipped_tickets": [],
        }
        with patch("graphs.iterative_dev.interrupt", return_value={}):
            result = escalate_node(state)
        assert result["escalation_decision"] == "abort"

    def test_park_accumulates_skipped_tickets(self):
        state: PipelineState = {
            "current_executor": "claude_dev",
            "escalation_attempted": False,
            "current_ticket_id": "WAL-47",
            "completed_prs": [],
            "skipped_tickets": ["WAL-40"],  # already one parked
        }
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "park"}):
            result = escalate_node(state)
        assert result["skipped_tickets"] == ["WAL-40", "WAL-47"]

    def test_interrupt_payload_contains_ticket_and_reason(self):
        state: PipelineState = {
            "current_executor": "claude_dev",
            "escalation_attempted": False,
            "current_ticket_id": "WAL-48",
            "blocked_reason": "Cannot find config file",
            "completed_prs": [],
            "skipped_tickets": [],
        }
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "abort"}) as mock_interrupt:
            escalate_node(state)
        payload = mock_interrupt.call_args[0][0]
        assert payload["ticket_id"] == "WAL-48"
        assert "Cannot find config file" in payload["message"]
        assert payload["type"] == "ticket_blocked"


class TestRouteAfterEscalate:
    def test_retry_routes_to_claude_dev(self):
        assert route_after_escalate({"escalation_decision": "retry_with_claude_dev"}) == "claude_dev"

    def test_park_routes_to_loop_check(self):
        assert route_after_escalate({"escalation_decision": "park_and_continue"}) == "loop_check"

    def test_abort_routes_to_end(self):
        assert route_after_escalate({"escalation_decision": "abort"}) == "__end__"


# ---------------------------------------------------------------------------
# merge_node
# ---------------------------------------------------------------------------

class TestMergeNode:
    """Real-impl tests. github_api boundaries + interrupt are mocked."""

    def _state(self, **overrides) -> dict:
        base = {
            "ticket_pr_url": "https://github.com/Geoff-Walker/FamilyCookbook/pull/42",
            "integration_branch": "batch/sprint-3",
            "repo": "Geoff-Walker/FamilyCookbook",
            "current_ticket_id": "WAL-42",
            "run_id": "run-1",
            "project_key": "WAL",
            "completed_prs": [],
            "skipped_tickets": [],
        }
        base.update(overrides)
        return base

    def _pr_response(self, base_ref: str = "batch/sprint-3", title: str = "WAL-42 add cuisine") -> dict:
        return {
            "head": {"sha": "abc123def456"},
            "base": {"ref": base_ref},
            "title": title,
            "body": "Adds the cuisine field to the recipes table.",
        }

    # --- happy path ---

    def test_successful_merge_appends_to_completed(self):
        state = self._state()
        with patch("graphs.iterative_dev.get_pr", return_value=self._pr_response()), \
             patch("graphs.iterative_dev.wait_for_mergeable"), \
             patch("graphs.iterative_dev.merge_pr", return_value="merge-sha-456") as mock_merge, \
             patch("graphs.iterative_dev.embed_and_store") as mock_kb:
            result = merge_node(state)

        assert state["ticket_pr_url"] in result["completed_prs"]
        assert "escalation_decision" not in result  # not aborting
        mock_merge.assert_called_once()
        mock_kb.assert_called_once()

    def test_kb_write_payload_uses_ticket_summary_kind(self):
        state = self._state()
        with patch("graphs.iterative_dev.get_pr", return_value=self._pr_response()), \
             patch("graphs.iterative_dev.wait_for_mergeable"), \
             patch("graphs.iterative_dev.merge_pr", return_value="sha"), \
             patch("graphs.iterative_dev.embed_and_store") as mock_kb:
            merge_node(state)

        entry = mock_kb.call_args[0][0]
        assert entry["kind"] == "ticket_summary"
        assert entry["ticket_id"] == "WAL-42"
        assert entry["project_key"] == "WAL"
        assert entry["agent"] == "merge_node"
        assert entry["graph_id"] == "iterative_dev"

    def test_merge_called_with_integration_branch_as_base(self):
        state = self._state(integration_branch="batch/sprint-7")
        pr = self._pr_response(base_ref="batch/sprint-7")
        with patch("graphs.iterative_dev.get_pr", return_value=pr), \
             patch("graphs.iterative_dev.wait_for_mergeable"), \
             patch("graphs.iterative_dev.merge_pr", return_value="sha") as mock_merge, \
             patch("graphs.iterative_dev.embed_and_store"):
            merge_node(state)

        kwargs = mock_merge.call_args.kwargs
        assert kwargs["base_branch"] == "batch/sprint-7"

    def test_kb_write_failure_does_not_block_merge(self):
        """Merge result is what matters — a KB write failure is logged but the merge still counts."""
        state = self._state()
        with patch("graphs.iterative_dev.get_pr", return_value=self._pr_response()), \
             patch("graphs.iterative_dev.wait_for_mergeable"), \
             patch("graphs.iterative_dev.merge_pr", return_value="sha"), \
             patch("graphs.iterative_dev.embed_and_store", side_effect=RuntimeError("Qdrant down")):
            result = merge_node(state)

        # PR was still added to completed
        assert state["ticket_pr_url"] in result["completed_prs"]

    # --- failure paths → interrupt ---

    def test_pr_target_mismatch_interrupts(self):
        """If the PR targets a branch other than integration_branch, refuse to merge."""
        state = self._state(integration_branch="batch/sprint-3")
        pr = self._pr_response(base_ref="main")  # WRONG TARGET
        with patch("graphs.iterative_dev.get_pr", return_value=pr), \
             patch("graphs.iterative_dev.merge_pr") as mock_merge, \
             patch(
                 "graphs.iterative_dev.interrupt",
                 return_value={"action": "park"},
             ) as mock_interrupt:
            result = merge_node(state)

        # Merge must NOT have been called
        mock_merge.assert_not_called()
        payload = mock_interrupt.call_args[0][0]
        assert payload["type"] == "merge_failed"
        assert "main" in payload["reason"]
        assert "batch/sprint-3" in payload["reason"]
        # park action: ticket added to skipped, no escalation_decision
        assert "WAL-42" in result["skipped_tickets"]
        assert "escalation_decision" not in result

    def test_merge_blocked_unstable_interrupts_with_park(self):
        """mergeable_state=unstable → non-required check failing → interrupt to Geoff."""
        state = self._state()
        pr = self._pr_response()
        with patch("graphs.iterative_dev.get_pr", return_value=pr), \
             patch(
                 "graphs.iterative_dev.wait_for_mergeable",
                 side_effect=MergeBlocked(state="unstable", pr=pr),
             ), \
             patch("graphs.iterative_dev.merge_pr") as mock_merge, \
             patch("graphs.iterative_dev.interrupt", return_value={"action": "park"}) as mock_interrupt:
            result = merge_node(state)

        mock_merge.assert_not_called()
        payload = mock_interrupt.call_args[0][0]
        assert "unstable" in payload["reason"]
        assert "non-required check" in payload["reason"]
        assert "WAL-42" in result["skipped_tickets"]

    def test_merge_blocked_dirty_interrupts(self):
        """mergeable_state=dirty → merge conflict → interrupt."""
        state = self._state()
        pr = self._pr_response()
        with patch("graphs.iterative_dev.get_pr", return_value=pr), \
             patch(
                 "graphs.iterative_dev.wait_for_mergeable",
                 side_effect=MergeBlocked(state="dirty", pr=pr),
             ), \
             patch("graphs.iterative_dev.merge_pr") as mock_merge, \
             patch("graphs.iterative_dev.interrupt", return_value={"action": "abort"}) as mock_interrupt:
            result = merge_node(state)

        mock_merge.assert_not_called()
        payload = mock_interrupt.call_args[0][0]
        assert "dirty" in payload["reason"]
        assert "merge conflict" in payload["reason"]
        assert result["escalation_decision"] == "abort"

    def test_merge_timeout_interrupts(self):
        state = self._state()
        pr = self._pr_response()
        with patch("graphs.iterative_dev.get_pr", return_value=pr), \
             patch(
                 "graphs.iterative_dev.wait_for_mergeable",
                 side_effect=MergeTimeout(state="unknown", pr={}),
             ), \
             patch("graphs.iterative_dev.interrupt", return_value={"action": "abort"}) as mock_interrupt:
            result = merge_node(state)

        payload = mock_interrupt.call_args[0][0]
        assert "timed out" in payload["reason"]
        assert "unknown" in payload["reason"]
        assert result["escalation_decision"] == "abort"

    def test_merge_api_failure_interrupts(self):
        state = self._state()
        import requests as _requests
        with patch("graphs.iterative_dev.get_pr", return_value=self._pr_response()), \
             patch("graphs.iterative_dev.wait_for_mergeable"), \
             patch("graphs.iterative_dev.merge_pr", side_effect=_requests.HTTPError("405 not mergeable")), \
             patch("graphs.iterative_dev.interrupt", return_value={"action": "park"}) as mock_interrupt:
            result = merge_node(state)

        payload = mock_interrupt.call_args[0][0]
        assert "merge API call failed" in payload["reason"]
        assert "WAL-42" in result["skipped_tickets"]

    def test_unparseable_pr_url_interrupts(self):
        state = self._state(ticket_pr_url="not-a-github-url")
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "abort"}) as mock_interrupt:
            result = merge_node(state)

        payload = mock_interrupt.call_args[0][0]
        assert "could not parse PR number" in payload["reason"]
        assert result["escalation_decision"] == "abort"

    def test_missing_state_interrupts(self):
        """If required state is absent, merge_node refuses to proceed."""
        with patch("graphs.iterative_dev.interrupt", return_value={"action": "abort"}) as mock_interrupt:
            result = merge_node({"completed_prs": [], "skipped_tickets": []})

        payload = mock_interrupt.call_args[0][0]
        assert "missing required state" in payload["reason"]
        assert result["escalation_decision"] == "abort"

    def test_no_action_in_interrupt_response_defaults_to_abort(self):
        """If the interrupt response is empty, default to abort — fail-safe."""
        state = self._state()
        with patch("graphs.iterative_dev.get_pr", return_value=self._pr_response(base_ref="main")), \
             patch("graphs.iterative_dev.interrupt", return_value={}):
            result = merge_node(state)

        assert result["escalation_decision"] == "abort"

    # --- code-level guards (defence in depth) ---

    def test_main_integration_branch_assertion_fires(self):
        """Belt-and-braces — should never reach this state, but the assertion exists.

        'main' fails the batch-prefix guard first; 'protected branch' guard
        catches edge cases like a branch literally named 'main' under batch/
        (not exercised here).
        """
        state = self._state(integration_branch="main")
        with pytest.raises(AssertionError, match="non-batch branch"):
            merge_node(state)

    def test_non_batch_integration_branch_assertion_fires(self):
        state = self._state(integration_branch="develop")
        with pytest.raises(AssertionError, match="non-batch branch"):
            merge_node(state)


# ---------------------------------------------------------------------------
# route_after_merge
# ---------------------------------------------------------------------------

class TestRouteAfterMerge:
    def test_abort_routes_to_end(self):
        from graphs.iterative_dev import route_after_merge
        assert route_after_merge({"escalation_decision": "abort"}) == "__end__"

    def test_default_routes_to_loop_check(self):
        from graphs.iterative_dev import route_after_merge
        assert route_after_merge({}) == "loop_check"

    def test_park_routes_to_loop_check(self):
        """park doesn't set escalation_decision — flow continues."""
        from graphs.iterative_dev import route_after_merge
        assert route_after_merge({"skipped_tickets": ["WAL-1"]}) == "loop_check"


# ---------------------------------------------------------------------------
# loop_check_node / route_after_loop_check
# ---------------------------------------------------------------------------

class TestLoopCheckNode:
    def test_increments_ticket_index(self):
        result = loop_check_node({"current_ticket_index": 0})
        assert result["current_ticket_index"] == 1

    def test_increments_from_any_index(self):
        result = loop_check_node({"current_ticket_index": 4})
        assert result["current_ticket_index"] == 5


class TestRouteAfterLoopCheck:
    def test_routes_to_next_ticket_when_more_remain(self):
        state: PipelineState = {
            "current_ticket_index": 1,
            "tickets": [
                {"id": "WAL-10", "executor": "haiku"},
                {"id": "WAL-11", "executor": "haiku"},
                {"id": "WAL-12", "executor": "haiku"},
            ],
        }
        assert route_after_loop_check(state) == "pick_next_ticket"

    def test_routes_to_batch_close_when_all_done(self):
        state: PipelineState = {
            "current_ticket_index": 3,
            "tickets": [
                {"id": "WAL-10", "executor": "haiku"},
                {"id": "WAL-11", "executor": "haiku"},
                {"id": "WAL-12", "executor": "haiku"},
            ],
        }
        assert route_after_loop_check(state) == "batch_close"

    def test_routes_to_batch_close_for_single_ticket_batch(self):
        state: PipelineState = {
            "current_ticket_index": 1,
            "tickets": [{"id": "WAL-10", "executor": "haiku"}],
        }
        assert route_after_loop_check(state) == "batch_close"


# ---------------------------------------------------------------------------
# batch_close_node
# ---------------------------------------------------------------------------

class TestBatchCloseNode:
    """Real-impl tests. open_pr / deploy_staging / embed_and_store are mocked."""

    def _state(self, **overrides) -> dict:
        base = {
            "integration_branch": "batch/sprint-3",
            "repo": "Geoff-Walker/FamilyCookbook",
            "sprint_number": 3,
            "completed_prs": [
                "https://github.com/Geoff-Walker/FamilyCookbook/pull/41",
                "https://github.com/Geoff-Walker/FamilyCookbook/pull/42",
            ],
            "skipped_tickets": ["WAL-99"],
            "run_id": "run-abc",
            "project_key": "WAL",
        }
        base.update(overrides)
        return base

    # --- happy path ---

    def test_opens_pr_with_correct_args(self):
        state = self._state()
        with patch(
            "graphs.iterative_dev.open_pr",
            return_value="https://github.com/x/y/pull/100",
        ) as mock_open, \
             patch("graphs.iterative_dev.deploy_staging"), \
             patch("graphs.iterative_dev.embed_and_store"):
            result = batch_close_node(state)

        # open_pr called with head=integration_branch, base=main
        kwargs = mock_open.call_args
        # open_pr signature: (repo, head, base, title, body)
        args = kwargs.args
        assert args[0] == "Geoff-Walker/FamilyCookbook"
        assert args[1] == "batch/sprint-3"
        assert args[2] == "main"
        assert "sprint 3" in args[3]  # title
        assert "completed" in args[4].lower()  # body
        assert result["batch_pr_url"] == "https://github.com/x/y/pull/100"

    def test_pr_body_lists_completed_and_skipped(self):
        state = self._state()
        with patch("graphs.iterative_dev.open_pr", return_value="https://x/pull/1") as mock_open, \
             patch("graphs.iterative_dev.deploy_staging"), \
             patch("graphs.iterative_dev.embed_and_store"):
            batch_close_node(state)

        body = mock_open.call_args.args[4]
        assert "https://github.com/Geoff-Walker/FamilyCookbook/pull/41" in body
        assert "https://github.com/Geoff-Walker/FamilyCookbook/pull/42" in body
        assert "WAL-99" in body
        assert "Do not merge" in body  # explicit reminder Geoff merges

    def test_kb_write_uses_batch_summary_kind(self):
        state = self._state()
        with patch("graphs.iterative_dev.open_pr", return_value="https://x/pull/1"), \
             patch("graphs.iterative_dev.deploy_staging"), \
             patch("graphs.iterative_dev.embed_and_store") as mock_kb:
            batch_close_node(state)

        entry = mock_kb.call_args[0][0]
        assert entry["kind"] == "batch_summary"
        assert entry["project_key"] == "WAL"
        assert entry["graph_id"] == "iterative_dev"
        assert entry["agent"] == "batch_close_node"
        assert entry["ticket_id"] is None  # batch summaries are not per-ticket

    def test_staging_deploy_invoked_with_correct_path(self):
        state = self._state(repo="Geoff-Walker/tom")
        with patch("graphs.iterative_dev.open_pr", return_value="https://x/pull/1"), \
             patch("graphs.iterative_dev.deploy_staging") as mock_deploy, \
             patch("graphs.iterative_dev.embed_and_store"):
            batch_close_node(state)

        mock_deploy.assert_called_once_with("/mnt/pool/apps/tom-staging")

    # --- failure handling — never blocks the batch close ---

    def test_open_pr_failure_returns_none_url_but_continues(self):
        """If GitHub API rejects the batch PR, log + return None — don't crash."""
        import requests as _requests
        state = self._state()
        with patch(
            "graphs.iterative_dev.open_pr",
            side_effect=_requests.HTTPError("422 already exists"),
        ), \
             patch("graphs.iterative_dev.deploy_staging") as mock_deploy, \
             patch("graphs.iterative_dev.embed_and_store") as mock_kb:
            result = batch_close_node(state)

        assert result["batch_pr_url"] is None
        # Other steps still attempted
        mock_deploy.assert_called_once()
        mock_kb.assert_called_once()

    def test_staging_deploy_failure_does_not_block(self):
        """Staging deploy failures are logged; batch_pr_url still returned."""
        state = self._state()
        from graphs.staging_deploy import DeployFailed
        with patch("graphs.iterative_dev.open_pr", return_value="https://x/pull/1"), \
             patch(
                 "graphs.iterative_dev.deploy_staging",
                 side_effect=DeployFailed("ssh refused"),
             ), \
             patch("graphs.iterative_dev.embed_and_store") as mock_kb:
            result = batch_close_node(state)

        assert result["batch_pr_url"] == "https://x/pull/1"
        # KB write still attempted
        mock_kb.assert_called_once()

    def test_kb_write_failure_does_not_block(self):
        state = self._state()
        with patch("graphs.iterative_dev.open_pr", return_value="https://x/pull/1"), \
             patch("graphs.iterative_dev.deploy_staging"), \
             patch("graphs.iterative_dev.embed_and_store", side_effect=RuntimeError("Qdrant down")):
            result = batch_close_node(state)

        assert result["batch_pr_url"] == "https://x/pull/1"

    def test_missing_state_returns_none_url_silently(self):
        """No interrupt — the batch is over either way; just signal None."""
        with patch("graphs.iterative_dev.open_pr") as mock_open:
            result = batch_close_node({})
        assert result["batch_pr_url"] is None
        mock_open.assert_not_called()

    # --- code-level guards (defence in depth) ---

    def test_main_integration_branch_assertion_fires(self):
        state = self._state(integration_branch="main")
        with pytest.raises(AssertionError, match="non-batch branch"):
            batch_close_node(state)

    def test_non_batch_integration_branch_assertion_fires(self):
        state = self._state(integration_branch="develop")
        with pytest.raises(AssertionError, match="non-batch branch"):
            batch_close_node(state)

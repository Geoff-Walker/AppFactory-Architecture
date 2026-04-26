"""
Unit tests for graphs/qa_batch.py

Coverage:
  - qa_node: produces ordered ticket list with executor tags
  - gate_node: always fires interrupt regardless of chain_to_dev value;
    chain and stop paths; payload structure
  - route_after_gate: chain → "chain", stop → "__end__"
  - chain_node: stub returns empty dict without raising
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from graphs.qa_batch import (
    chain_node,
    gate_node,
    qa_node,
    route_after_gate,
)
from graphs.state import PipelineState


# ---------------------------------------------------------------------------
# qa_node
# ---------------------------------------------------------------------------

class TestQaNode:
    def test_returns_tickets(self):
        state: PipelineState = {
            "spec": "Build a recipe CRUD page",
            "design_output_location": "/handoffs/design-output/",
            "project_key": "WAL",
        }
        result = qa_node(state)
        assert "tickets" in result
        assert len(result["tickets"]) > 0

    def test_ticket_ids_match_summaries(self):
        result = qa_node({"project_key": "TST"})
        ids = [t["id"] for t in result["tickets"]]
        summary_ids = [t["id"] for t in result["qa_ticket_summaries"]]
        assert ids == summary_ids

    def test_uses_project_key_prefix(self):
        result = qa_node({"project_key": "FCB"})
        for ticket in result["tickets"]:
            assert ticket["id"].startswith("FCB-")

    def test_summaries_contain_required_fields(self):
        result = qa_node({"project_key": "WAL"})
        for summary in result["qa_ticket_summaries"]:
            assert "id" in summary
            assert "summary" in summary
            assert "executor_tag" in summary
            assert "blocked_by" in summary

    def test_tickets_contain_id_and_executor(self):
        """The new ``tickets`` shape is the input the iterative_dev graph consumes."""
        result = qa_node({"project_key": "WAL"})
        for ticket in result["tickets"]:
            assert "id" in ticket
            assert "executor" in ticket

    def test_executor_tags_are_valid(self):
        """Executor vocabulary is ``haiku`` or ``claude-dev`` — copilot is gone."""
        result = qa_node({"project_key": "WAL"})
        valid_tags = {"haiku", "claude-dev"}
        for summary in result["qa_ticket_summaries"]:
            assert summary["executor_tag"] in valid_tags
        for ticket in result["tickets"]:
            assert ticket["executor"] in valid_tags

    def test_tickets_ordered_dependency_safe(self):
        result = qa_node({"project_key": "WAL"})
        summaries = result["qa_ticket_summaries"]
        # Each ticket's blocked_by list should only reference tickets
        # that appear earlier in the ordered list.
        seen_ids = set()
        for ticket in summaries:
            for dep in ticket["blocked_by"]:
                assert dep in seen_ids, (
                    f"{ticket['id']} depends on {dep} which hasn't been seen yet"
                )
            seen_ids.add(ticket["id"])

    def test_does_not_raise_on_empty_state(self):
        result = qa_node({})
        assert "tickets" in result


# ---------------------------------------------------------------------------
# gate_node — always fires regardless of chain_to_dev
# ---------------------------------------------------------------------------

class TestGateNodeAlwaysFires:
    def test_fires_when_chain_to_dev_true(self):
        state: PipelineState = {
            "qa_ticket_summaries": [{"id": "W-1", "summary": "s", "executor_tag": "haiku", "blocked_by": []}],
            "chain_to_dev": True,
        }
        with patch("graphs.qa_batch.interrupt", return_value={"action": "chain"}) as mock_interrupt:
            gate_node(state)
        mock_interrupt.assert_called_once()

    def test_fires_when_chain_to_dev_false(self):
        state: PipelineState = {
            "qa_ticket_summaries": [{"id": "W-1", "summary": "s", "executor_tag": "haiku", "blocked_by": []}],
            "chain_to_dev": False,
        }
        with patch("graphs.qa_batch.interrupt", return_value={"action": "stop"}) as mock_interrupt:
            gate_node(state)
        mock_interrupt.assert_called_once()

    def test_fires_when_chain_to_dev_absent(self):
        state: PipelineState = {
            "qa_ticket_summaries": [{"id": "W-1", "summary": "s", "executor_tag": "haiku", "blocked_by": []}],
        }
        with patch("graphs.qa_batch.interrupt", return_value={"action": "stop"}) as mock_interrupt:
            gate_node(state)
        mock_interrupt.assert_called_once()


class TestGateNodeChainPath:
    def test_chain_action_sets_decision(self):
        state: PipelineState = {
            "qa_ticket_summaries": [],
            "chain_to_dev": True,
        }
        with patch("graphs.qa_batch.interrupt", return_value={"action": "chain"}):
            result = gate_node(state)
        assert result["qa_gate_decision"] == "chain"

    def test_no_action_defaults_to_stop(self):
        state: PipelineState = {
            "qa_ticket_summaries": [],
            "chain_to_dev": True,
        }
        with patch("graphs.qa_batch.interrupt", return_value={}):
            result = gate_node(state)
        assert result["qa_gate_decision"] == "stop"


class TestGateNodeStopPath:
    def test_stop_action_sets_decision(self):
        state: PipelineState = {
            "qa_ticket_summaries": [],
            "chain_to_dev": False,
        }
        with patch("graphs.qa_batch.interrupt", return_value={"action": "stop"}):
            result = gate_node(state)
        assert result["qa_gate_decision"] == "stop"


class TestGateNodePayload:
    def test_payload_contains_ticket_list(self):
        summaries = [
            {"id": "WAL-1", "summary": "Schema migration", "executor_tag": "claude-dev", "blocked_by": []},
            {"id": "WAL-2", "summary": "API endpoint", "executor_tag": "haiku", "blocked_by": ["WAL-1"]},
        ]
        state: PipelineState = {"qa_ticket_summaries": summaries, "chain_to_dev": True}
        with patch("graphs.qa_batch.interrupt", return_value={"action": "stop"}) as mock_interrupt:
            gate_node(state)
        payload = mock_interrupt.call_args[0][0]
        assert payload["tickets"] == summaries
        assert payload["type"] == "qa_gate"

    def test_payload_reflects_chain_to_dev_default(self):
        state: PipelineState = {"qa_ticket_summaries": [], "chain_to_dev": True}
        with patch("graphs.qa_batch.interrupt", return_value={"action": "stop"}) as mock_interrupt:
            gate_node(state)
        payload = mock_interrupt.call_args[0][0]
        assert payload["chain_to_dev_default"] is True

    def test_payload_ticket_count_in_message(self):
        summaries = [
            {"id": "W-1", "summary": "s1", "executor_tag": "haiku", "blocked_by": []},
            {"id": "W-2", "summary": "s2", "executor_tag": "haiku", "blocked_by": []},
            {"id": "W-3", "summary": "s3", "executor_tag": "claude-dev", "blocked_by": []},
        ]
        state: PipelineState = {"qa_ticket_summaries": summaries, "chain_to_dev": False}
        with patch("graphs.qa_batch.interrupt", return_value={"action": "stop"}) as mock_interrupt:
            gate_node(state)
        payload = mock_interrupt.call_args[0][0]
        assert "3" in payload["message"]


# ---------------------------------------------------------------------------
# route_after_gate
# ---------------------------------------------------------------------------

class TestRouteAfterGate:
    def test_chain_decision_routes_to_chain(self):
        assert route_after_gate({"qa_gate_decision": "chain"}) == "chain"

    def test_stop_decision_routes_to_end(self):
        assert route_after_gate({"qa_gate_decision": "stop"}) == "__end__"

    def test_missing_decision_routes_to_end(self):
        assert route_after_gate({}) == "__end__"


# ---------------------------------------------------------------------------
# chain_node
# ---------------------------------------------------------------------------

class TestChainNode:
    def test_returns_empty_dict(self):
        state: PipelineState = {
            "tickets": [
                {"id": "WAL-1", "executor": "haiku"},
                {"id": "WAL-2", "executor": "claude-dev"},
            ],
            "project_key": "WAL",
            "repo": "FamilyCookbook",
        }
        result = chain_node(state)
        assert result == {}

    def test_does_not_raise_on_empty_state(self):
        result = chain_node({})
        assert result == {}

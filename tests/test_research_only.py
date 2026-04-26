"""
Unit tests for graphs/research_only.py

Tests cover all state transitions. No LLM calls are made — agent outputs
are mocked via patch on langgraph.types.interrupt.

Routes under test:
  COMPLETED            → check_result → route: store → END
  DEEP_RESEARCH_NEEDED → interrupt → approve deep  → route: research (loop)
  DEEP_RESEARCH_NEEDED → interrupt → accept_partial → route: store → END
  BLOCKED              → interrupt → abort          → route: __end__ (ABORTED)
  BLOCKED              → interrupt → continue       → route: research (loop)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from graphs.research_only import (
    check_result_node,
    research_node,
    route_after_check,
    store_node,
)
from graphs.state import PipelineState


# ---------------------------------------------------------------------------
# research_node (stub — always returns COMPLETED)
# ---------------------------------------------------------------------------

class TestResearchNode:
    def test_quick_hit_default(self):
        state: PipelineState = {"question": "What is Redis?"}
        result = research_node(state)
        assert result["research_status"] == "COMPLETED"
        assert "Quick-hit" in result["research_findings"]
        assert result["research_gaps"] == []

    def test_deep_research_mode(self):
        state: PipelineState = {"question": "Explain LangGraph", "research_mode": "deep_research"}
        result = research_node(state)
        assert result["research_status"] == "COMPLETED"
        assert "Deep findings" in result["research_findings"]
        assert result["research_gaps"] == []

    def test_missing_question_does_not_raise(self):
        result = research_node({})
        assert result["research_status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# route_after_check (pure routing function)
# ---------------------------------------------------------------------------

class TestRouteAfterCheck:
    def test_completed_routes_to_store(self):
        assert route_after_check({"research_status": "COMPLETED"}) == "store"

    def test_aborted_routes_to_end(self):
        assert route_after_check({"research_status": "ABORTED"}) == "__end__"

    def test_none_routes_to_research(self):
        assert route_after_check({"research_status": None}) == "research"

    def test_missing_status_defaults_to_store(self):
        # state.get("research_status") returns None for a missing key → "research"
        # This matches the loopback behaviour for an uninitialised state.
        assert route_after_check({}) == "research"


# ---------------------------------------------------------------------------
# check_result_node — COMPLETED (no interrupt)
# ---------------------------------------------------------------------------

class TestCheckResultCompleted:
    def test_completed_returns_empty_dict(self):
        state: PipelineState = {"research_status": "COMPLETED"}
        result = check_result_node(state)
        assert result == {}

    def test_missing_status_returns_empty_dict(self):
        result = check_result_node({})
        assert result == {}


# ---------------------------------------------------------------------------
# check_result_node — DEEP_RESEARCH_NEEDED paths
# ---------------------------------------------------------------------------

class TestCheckResultDeepResearch:
    def test_approve_deep_research_sets_mode_and_clears_status(self):
        state: PipelineState = {
            "research_status": "DEEP_RESEARCH_NEEDED",
            "question": "What are LangGraph Platform limits?",
            "research_gaps": ["rate limits", "pricing"],
        }
        with patch("graphs.research_only.interrupt", return_value={"action": "deep_research"}):
            result = check_result_node(state)

        assert result["research_mode"] == "deep_research"
        assert result["research_status"] is None  # signals route_after_check → "research"

    def test_accept_partial_marks_completed(self):
        state: PipelineState = {
            "research_status": "DEEP_RESEARCH_NEEDED",
            "question": "What are LangGraph Platform limits?",
            "research_gaps": ["rate limits"],
        }
        with patch("graphs.research_only.interrupt", return_value={"action": "accept_partial"}):
            result = check_result_node(state)

        assert result["research_status"] == "COMPLETED"

    def test_no_action_defaults_to_accept_partial(self):
        state: PipelineState = {"research_status": "DEEP_RESEARCH_NEEDED"}
        with patch("graphs.research_only.interrupt", return_value={}):
            result = check_result_node(state)

        assert result["research_status"] == "COMPLETED"

    def test_interrupt_payload_contains_question_and_gaps(self):
        state: PipelineState = {
            "research_status": "DEEP_RESEARCH_NEEDED",
            "question": "How does Qdrant HNSW work?",
            "research_gaps": ["index tuning"],
        }
        with patch("graphs.research_only.interrupt", return_value={"action": "accept_partial"}) as mock_interrupt:
            check_result_node(state)

        payload = mock_interrupt.call_args[0][0]
        assert "How does Qdrant HNSW work?" in payload["message"]
        assert "index tuning" in str(payload["message"])
        assert payload["type"] == "deep_research_needed"


# ---------------------------------------------------------------------------
# check_result_node — BLOCKED paths
# ---------------------------------------------------------------------------

class TestCheckResultBlocked:
    def test_abort_sets_aborted_status(self):
        state: PipelineState = {
            "research_status": "BLOCKED",
            "question": "Find Ollama GPU docs",
            "blocked_reason": "WebSearch returned no results",
        }
        with patch("graphs.research_only.interrupt", return_value={"action": "abort"}):
            result = check_result_node(state)

        assert result["research_status"] == "ABORTED"

    def test_continue_clears_status_and_injects_context(self):
        state: PipelineState = {
            "research_status": "BLOCKED",
            "question": "Find Ollama GPU docs",
            "blocked_reason": "rate limited",
        }
        with patch(
            "graphs.research_only.interrupt",
            return_value={"action": "continue", "instruction": "Try the Ollama GitHub wiki"},
        ):
            result = check_result_node(state)

        assert result["research_status"] is None
        assert result["research_context"] == "Try the Ollama GitHub wiki"

    def test_no_action_defaults_to_abort(self):
        state: PipelineState = {"research_status": "BLOCKED"}
        with patch("graphs.research_only.interrupt", return_value={}):
            result = check_result_node(state)

        assert result["research_status"] == "ABORTED"

    def test_interrupt_payload_contains_question_and_reason(self):
        state: PipelineState = {
            "research_status": "BLOCKED",
            "question": "Langfuse pricing",
            "blocked_reason": "paywall on docs",
        }
        with patch("graphs.research_only.interrupt", return_value={"action": "abort"}) as mock_interrupt:
            check_result_node(state)

        payload = mock_interrupt.call_args[0][0]
        assert "Langfuse pricing" in payload["message"]
        assert "paywall on docs" in payload["message"]
        assert payload["type"] == "blocked"


# ---------------------------------------------------------------------------
# store_node (stub — just logs, returns {})
# ---------------------------------------------------------------------------

class TestStoreNode:
    def test_returns_empty_dict(self):
        state: PipelineState = {
            "question": "How does Redis pub/sub work?",
            "research_findings": "Redis pub/sub uses channels...",
        }
        result = store_node(state)
        assert result == {}

    def test_does_not_raise_on_empty_state(self):
        result = store_node({})
        assert result == {}

    # --- project-scoped KB schema (added 2026-04-22) ---

    def test_passes_full_payload_to_embed_and_store(self):
        """store_node forwards research findings + project-scoped metadata to the KB write path."""
        state: PipelineState = {
            "question": "How does pgvector handle 1536-dim vectors?",
            "research_findings": "pgvector stores vectors as fixed-size arrays...",
            "run_id": "run-abc-123",
            "project_key": "WAL",
            "current_ticket_id": "WAL-42",
        }
        with patch("graphs.research_only.embed_and_store") as mock_store:
            store_node(state)

        assert mock_store.called
        entry = mock_store.call_args[0][0]
        assert entry["agent"] == "research"
        assert entry["task"] == "How does pgvector handle 1536-dim vectors?"
        assert entry["output"] == "pgvector stores vectors as fixed-size arrays..."
        assert entry["run_id"] == "run-abc-123"
        assert entry["graph_id"] == "research_only"
        assert entry["project_key"] == "WAL"
        assert entry["kind"] == "research"
        assert entry["ticket_id"] == "WAL-42"

    def test_no_ticket_id_when_research_is_standalone(self):
        """Standalone research dispatches (not triggered by an iterative_dev ticket) leave ticket_id None."""
        state: PipelineState = {
            "question": "What is LangGraph Platform?",
            "research_findings": "LangGraph Platform is a managed runtime...",
            "project_key": "AF",
        }
        with patch("graphs.research_only.embed_and_store") as mock_store:
            store_node(state)

        entry = mock_store.call_args[0][0]
        assert entry["ticket_id"] is None
        assert entry["project_key"] == "AF"
        assert entry["kind"] == "research"

    def test_missing_project_key_defaults_to_empty_string(self):
        """When no project_key is in state, store_node passes empty string (KB write path defaults the same way)."""
        state: PipelineState = {
            "question": "What?",
            "research_findings": "Things.",
        }
        with patch("graphs.research_only.embed_and_store") as mock_store:
            store_node(state)

        entry = mock_store.call_args[0][0]
        assert entry["project_key"] == ""
        assert entry["kind"] == "research"
        assert entry["ticket_id"] is None

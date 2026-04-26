"""
Unit tests for graphs/knowledge.py — the operational_knowledge Qdrant write path.

These tests cover the structural contract of embed_and_store, particularly the
project-scoped KB schema fields added 2026-04-22 (project_key, kind, ticket_id,
superseded_by). The actual OpenAI embedding call and Qdrant upsert are mocked —
no network calls.

Test boundaries:
  - Payload construction — what ends up in the Qdrant upsert call
  - Default-value behaviour when optional fields are absent
  - Graceful no-op when QDRANT_URL or OPENAI_API_KEY are not set
  - Error swallowing — embedding/upsert failures are logged but do not raise
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_openai_client() -> MagicMock:
    """Mock the OpenAI client to return a fixed 1536-dim embedding."""
    client = MagicMock()
    client.embeddings.create.return_value.data = [MagicMock(embedding=[0.1] * 1536)]
    return client


def _capture_payload(mock_qdrant_client: MagicMock) -> dict:
    """Pull the payload dict out of the captured Qdrant upsert call."""
    assert mock_qdrant_client.upsert.called, "upsert was never called"
    points = mock_qdrant_client.upsert.call_args.kwargs["points"]
    assert len(points) == 1, "expected exactly one point per upsert"
    return points[0].payload


# ---------------------------------------------------------------------------
# Payload construction — full entry
# ---------------------------------------------------------------------------

class TestEmbedAndStoreFullEntry:
    @pytest.fixture(autouse=True)
    def env_set(self, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://test-qdrant:6333")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def test_full_entry_produces_complete_payload(self):
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "research",
                "task": "How does pgvector work?",
                "output": "pgvector stores vectors as fixed-size arrays...",
                "run_id": "run-1",
                "graph_id": "research_only",
                "project_key": "WAL",
                "kind": "research",
                "ticket_id": "WAL-42",
            })

        payload = _capture_payload(mock_qdrant)

        # Original fields
        assert payload["agent"] == "research"
        assert payload["task"] == "How does pgvector work?"
        assert payload["output"] == "pgvector stores vectors as fixed-size arrays..."
        assert payload["run_id"] == "run-1"
        assert payload["graph_id"] == "research_only"
        assert "timestamp" in payload  # defaulted to now UTC

        # Project-scoped KB schema fields
        assert payload["project_key"] == "WAL"
        assert payload["kind"] == "research"
        assert payload["ticket_id"] == "WAL-42"
        assert payload["superseded_by"] is None  # never set at creation

    def test_targets_operational_knowledge_collection(self):
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "research",
                "task": "x",
                "output": "y",
                "run_id": "r",
                "graph_id": "g",
            })

        assert mock_qdrant.upsert.call_args.kwargs["collection_name"] == "operational_knowledge"


# ---------------------------------------------------------------------------
# Default values for optional KB schema fields
# ---------------------------------------------------------------------------

class TestEmbedAndStoreDefaults:
    @pytest.fixture(autouse=True)
    def env_set(self, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://test-qdrant:6333")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def test_minimal_entry_applies_defaults(self):
        """When project_key/kind/ticket_id absent, sensible defaults are written so the schema remains uniform."""
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "research",
                "task": "x",
                "output": "y",
                "run_id": "r",
                "graph_id": "g",
            })

        payload = _capture_payload(mock_qdrant)
        assert payload["project_key"] == ""
        assert payload["kind"] == "unknown"   # explicit signal that classification was missing
        assert payload["ticket_id"] is None
        assert payload["superseded_by"] is None

    def test_kind_unknown_default_is_explicit_string(self):
        """`unknown` is the documented sentinel — must not be empty string or None."""
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "x", "task": "x", "output": "x", "run_id": "x", "graph_id": "x",
            })

        payload = _capture_payload(mock_qdrant)
        assert payload["kind"] == "unknown"


# ---------------------------------------------------------------------------
# Schema variants — non-research kinds for future callers (merge_node etc.)
# ---------------------------------------------------------------------------

class TestEmbedAndStoreSchemaVariants:
    @pytest.fixture(autouse=True)
    def env_set(self, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://test-qdrant:6333")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def test_ticket_summary_kind_is_passed_through(self):
        """Future caller: merge_node writes kind='ticket_summary' after a successful integration-branch merge."""
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "merge_node",
                "task": "WAL-42 implementation",
                "output": "Added recipes.cuisine column with EF migration...",
                "run_id": "run-2",
                "graph_id": "iterative_dev",
                "project_key": "WAL",
                "kind": "ticket_summary",
                "ticket_id": "WAL-42",
            })

        payload = _capture_payload(mock_qdrant)
        assert payload["kind"] == "ticket_summary"
        assert payload["ticket_id"] == "WAL-42"
        assert payload["agent"] == "merge_node"
        assert payload["graph_id"] == "iterative_dev"

    def test_architecture_decision_kind_is_passed_through(self):
        """Future caller: manual MCP write tool for pinning design decisions."""
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "archie",
                "task": "Decision: use Option B clone-per-run workspaces",
                "output": "Each pipeline dispatch clones the target repo fresh...",
                "run_id": "manual",
                "graph_id": "n/a",
                "project_key": "AF",
                "kind": "architecture_decision",
            })

        payload = _capture_payload(mock_qdrant)
        assert payload["kind"] == "architecture_decision"
        assert payload["project_key"] == "AF"
        assert payload["ticket_id"] is None

    def test_superseded_by_pointer_is_passed_through(self):
        """When an entry is later marked as superseded, the pointer to the newer entry is stored."""
        from graphs.knowledge import embed_and_store

        mock_qdrant = MagicMock()
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=mock_qdrant):
            embed_and_store({
                "agent": "research",
                "task": "old finding",
                "output": "old content",
                "run_id": "r",
                "graph_id": "g",
                "project_key": "AF",
                "kind": "research",
                "superseded_by": "uuid-of-newer-entry",
            })

        payload = _capture_payload(mock_qdrant)
        assert payload["superseded_by"] == "uuid-of-newer-entry"


# ---------------------------------------------------------------------------
# Graceful no-op when env vars are missing
# ---------------------------------------------------------------------------

class TestEmbedAndStoreNoOp:
    def test_no_qdrant_url_is_noop(self, monkeypatch):
        from graphs.knowledge import embed_and_store

        monkeypatch.delenv("QDRANT_URL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        # Must not raise. No Qdrant call should be attempted.
        with patch("openai.OpenAI") as mock_openai_cls, \
             patch("qdrant_client.QdrantClient") as mock_qdrant_cls:
            embed_and_store({
                "agent": "x", "task": "x", "output": "x", "run_id": "x", "graph_id": "x",
            })

        mock_openai_cls.assert_not_called()
        mock_qdrant_cls.assert_not_called()

    def test_no_openai_key_is_noop(self, monkeypatch):
        from graphs.knowledge import embed_and_store

        monkeypatch.setenv("QDRANT_URL", "http://test:6333")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with patch("openai.OpenAI") as mock_openai_cls, \
             patch("qdrant_client.QdrantClient") as mock_qdrant_cls:
            embed_and_store({
                "agent": "x", "task": "x", "output": "x", "run_id": "x", "graph_id": "x",
            })

        mock_openai_cls.assert_not_called()
        mock_qdrant_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Error swallowing — never crash the pipeline
# ---------------------------------------------------------------------------

class TestEmbedAndStoreErrorSwallowing:
    @pytest.fixture(autouse=True)
    def env_set(self, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://test-qdrant:6333")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def test_openai_error_does_not_raise(self):
        """An OpenAI embedding failure is logged and swallowed — pipeline continues."""
        from graphs.knowledge import embed_and_store

        bad_openai = MagicMock()
        bad_openai.embeddings.create.side_effect = Exception("OpenAI down")
        with patch("openai.OpenAI", return_value=bad_openai), \
             patch("qdrant_client.QdrantClient") as mock_qdrant_cls:
            embed_and_store({
                "agent": "x", "task": "x", "output": "x", "run_id": "x", "graph_id": "x",
            })

        # Qdrant should never be touched if embedding failed
        mock_qdrant_cls.assert_not_called()

    def test_qdrant_error_does_not_raise(self):
        """A Qdrant upsert failure is logged and swallowed — pipeline continues."""
        from graphs.knowledge import embed_and_store

        bad_qdrant = MagicMock()
        bad_qdrant.upsert.side_effect = Exception("Qdrant unreachable")
        with patch("openai.OpenAI", return_value=_mock_openai_client()), \
             patch("qdrant_client.QdrantClient", return_value=bad_qdrant):
            embed_and_store({
                "agent": "x", "task": "x", "output": "x", "run_id": "x", "graph_id": "x",
            })

        # If we reached here without raising, the test passes.

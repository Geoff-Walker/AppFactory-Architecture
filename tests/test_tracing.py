"""
Unit tests for graphs/tracing.py

Coverage:
  - apply_run_id_to_trace:
      - sets session.id + tags when run_id is a non-empty string
      - no-op when run_id is empty
      - no-op when no active OTEL span (mock returns None)
      - no-op when span is not recording
      - swallows exceptions silently (tracing must not break the pipeline)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import graphs.tracing as tracing
from graphs.tracing import apply_run_id_to_trace


class TestApplyRunIdToTrace:
    def test_sets_session_id_and_tags(self):
        span = MagicMock()
        span.is_recording.return_value = True

        with patch.object(tracing, "_LANGFUSE_AVAILABLE", True), \
                patch("opentelemetry.trace.get_current_span", return_value=span):
            apply_run_id_to_trace("run-abc-123")

        # session.id should be set on the current span
        calls = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
        assert calls.get("session.id") == "run-abc-123"
        # tag marker for secondary discovery
        assert "langfuse.trace.tags" in calls
        tags_value = calls["langfuse.trace.tags"]
        assert "run_id:run-abc-123" in tags_value

    def test_no_op_when_run_id_empty(self):
        span = MagicMock()
        span.is_recording.return_value = True

        with patch.object(tracing, "_LANGFUSE_AVAILABLE", True), \
                patch("opentelemetry.trace.get_current_span", return_value=span):
            apply_run_id_to_trace("")

        span.set_attribute.assert_not_called()

    def test_no_op_when_langfuse_unavailable(self):
        with patch.object(tracing, "_LANGFUSE_AVAILABLE", False), \
                patch("opentelemetry.trace.get_current_span") as mock_span:
            apply_run_id_to_trace("run-abc-123")

        # Should not even query for a current span when langfuse isn't available.
        mock_span.assert_not_called()

    def test_no_op_when_no_active_span(self):
        with patch.object(tracing, "_LANGFUSE_AVAILABLE", True), \
                patch("opentelemetry.trace.get_current_span", return_value=None):
            # Must not raise.
            apply_run_id_to_trace("run-abc-123")

    def test_no_op_when_span_not_recording(self):
        span = MagicMock()
        span.is_recording.return_value = False

        with patch.object(tracing, "_LANGFUSE_AVAILABLE", True), \
                patch("opentelemetry.trace.get_current_span", return_value=span):
            apply_run_id_to_trace("run-abc-123")

        span.set_attribute.assert_not_called()

    def test_swallows_exceptions(self):
        span = MagicMock()
        span.is_recording.return_value = True
        span.set_attribute.side_effect = RuntimeError("otel exploded")

        with patch.object(tracing, "_LANGFUSE_AVAILABLE", True), \
                patch("opentelemetry.trace.get_current_span", return_value=span):
            # Must not raise — tracing failures are swallowed.
            apply_run_id_to_trace("run-abc-123")

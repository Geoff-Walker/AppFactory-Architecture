"""
Langfuse tracing utilities — shared across all pipeline graphs.

Uses the @observe() decorator (Langfuse v4+ public API).
Instruments individual node functions regardless of how the graph is
invoked. Works with both direct Python invocation and the LangGraph
Platform REST API.

Import `observe` from this module — it degrades gracefully to a no-op
if langfuse is not installed or credentials are not configured.

Usage:
    from graphs.tracing import observe

    @observe()
    def my_node(state: PipelineState) -> dict:
        ...

Environment variables (set on the LangGraph service host):
   LANGFUSE_HOST        — e.g. http://<your-langfuse-host>:3000 (deprecated in v4, still read)
   LANGFUSE_BASE_URL    — preferred in v4; takes precedence over LANGFUSE_HOST
   LANGFUSE_PUBLIC_KEY  — from Langfuse project settings
   LANGFUSE_SECRET_KEY  — from Langfuse project settings

Langfuse v4 uses OpenTelemetry internally. Calling Langfuse() registers
the OTEL tracer provider — without it, @observe() spans are created but
have no exporter and are silently dropped. Initialisation happens here at
import time so every graph that imports tracing.py gets a live exporter.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Langfuse client init + @observe decorator — no-op fallback if absent
# ---------------------------------------------------------------------------

try:
    from langfuse import Langfuse as _Langfuse
    from langfuse import observe as _observe  # v4+ public API

    _required = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    if all(os.environ.get(k) for k in _required):
        try:
            _langfuse_client = _Langfuse()
            logger.info(
                "Langfuse client initialised — host=%s",
                os.environ.get("LANGFUSE_BASE_URL")
                or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except Exception as _exc:
            logger.warning("Langfuse client init failed — tracing disabled: %s", _exc)
    else:
        logger.debug(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set — @observe() is a no-op."
        )

    _LANGFUSE_AVAILABLE = True

except ImportError:
    _LANGFUSE_AVAILABLE = False

    def _observe(*args, **kwargs):  # type: ignore[misc]
        """No-op fallback when langfuse is not installed."""
        def decorator(func):
            return func
        if args and callable(args[0]):
            return args[0]
        return decorator

observe = _observe


# ---------------------------------------------------------------------------
# Trace-to-run_id linking via Langfuse session_id
# ---------------------------------------------------------------------------

# Langfuse v4 OTEL attribute names — see langfuse._client.attributes.
# We set these directly on the active OTEL span instead of using
# `langfuse.propagate_attributes()` because the latter is a context manager
# and we want an inline no-op-safe call at the top of each @observe() node.
_TRACE_SESSION_ID_ATTR = "session.id"
_TRACE_TAGS_ATTR = "langfuse.trace.tags"


def apply_run_id_to_trace(run_id: str) -> None:
    """Link the current Langfuse trace to a LangGraph run_id via session_id.

    Call as the first statement inside any @observe()-decorated node, after
    reading ``run_id`` from state. Safe no-op if Langfuse is not configured,
    no OTEL span is active, or the run_id is empty.

    This exists because @observe() creates Langfuse traces with
    OTEL-generated trace IDs that do not match the LangGraph run_id. Setting
    session_id on the active trace lets the MCP ``get_run_trace`` tool query
    by ``?sessionId=<run_id>`` and aggregate observations across all traces
    for a pipeline run. A ``run_id:<uuid>`` tag is added as a secondary
    discovery aid.

    Tracing failures never propagate — any exception is swallowed and logged
    at debug level. The pipeline must not break because of telemetry.
    """
    if not run_id:
        return
    if not _LANGFUSE_AVAILABLE:
        return

    try:
        from opentelemetry import trace as _otel_trace

        span = _otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return

        span.set_attribute(_TRACE_SESSION_ID_ATTR, run_id)
        span.set_attribute(_TRACE_TAGS_ATTR, (f"run_id:{run_id}",))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("apply_run_id_to_trace failed — tracing error swallowed: %s", exc)

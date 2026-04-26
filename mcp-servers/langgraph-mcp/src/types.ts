// ---------------------------------------------------------------------------
// LangGraph Platform API response types
// ---------------------------------------------------------------------------

export type ThreadStatus = "idle" | "busy" | "interrupted" | "error";

export interface LangGraphThread {
  thread_id: string;
  created_at: string;
  updated_at: string;
  status: ThreadStatus;
  metadata: Record<string, unknown>;
  values: Record<string, unknown>;
}

export interface LangGraphRun {
  run_id: string;
  thread_id: string;
  assistant_id: string;
  created_at: string;
  updated_at: string;
  status: string;
  metadata: Record<string, unknown>;
}

export interface LangGraphAssistant {
  assistant_id: string;
  graph_id: string;
  name: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
  config: Record<string, unknown>;
}

export interface LangGraphInterrupt {
  value: unknown;
  resumable: boolean;
  ns: string[];
  when: string;
}

export interface LangGraphThreadState {
  values: Record<string, unknown>;
  next: string[];
  tasks: Array<{
    id: string;
    name: string;
    error?: string;
    interrupts: LangGraphInterrupt[];
    state?: Record<string, unknown>;
  }>;
  metadata: Record<string, unknown>;
  created_at: string;
  parent_config?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Langfuse API response types
// ---------------------------------------------------------------------------

export interface LangfuseObservation {
  id: string;
  traceId: string;
  type: string;
  name: string;
  startTime: string;
  endTime?: string;
  model?: string;
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  calculatedInputCost?: number;
  calculatedOutputCost?: number;
  calculatedTotalCost?: number;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
}

export interface LangfuseTrace {
  id: string;
  name?: string;
  timestamp: string;
  sessionId?: string;
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
  tags?: string[];
  observations: LangfuseObservation[];
  totalCost?: number;
}

export interface LangfuseUsageMetric {
  date: string;
  model: string;
  inputUsage: number;
  outputUsage: number;
  totalUsage: number;
  inputCost: number;
  outputCost: number;
  totalCost: number;
}

// ---------------------------------------------------------------------------
// Qdrant search result types
// ---------------------------------------------------------------------------

export interface OperationalKnowledgePayload {
  agent?: string;
  task?: string;
  output?: string;
  run_id?: string;
  timestamp?: string;
  [key: string]: unknown;
}

export interface KnowledgeSearchResult {
  id: string | number;
  score: number;
  payload: OperationalKnowledgePayload;
}

// ---------------------------------------------------------------------------
// Internal tool result type — every tool handler returns this
// ---------------------------------------------------------------------------

export interface ToolResult {
  [key: string]: unknown;
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
}

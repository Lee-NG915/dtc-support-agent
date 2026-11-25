export type Strategy = "direct" | "single" | "multi" | "agent" | "refuse";

export interface Citation {
  document_id: string;
  title: string;
  chunk_id: string;
  section: string | null;
}

export interface ActionProposal {
  action_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk: string;
  summary: string;
}

export interface ChatResponse {
  answer: string;
  session_id: string;
  trace_id: string;
  intent: string | null;
  complexity: string | null;
  strategy: Strategy | null;
  citations: Citation[];
  pending_action: ActionProposal | null;
  refused: boolean;
  error_code: string | null;
  latency: { total_ms: number };
}

export interface ChatRequest {
  message: string;
  session_id?: string;
  user_id: string;
  confirm_action_id?: string;
}

export interface Capabilities {
  database: string;
  cache: string;
  vector_store: string;
  retriever: string;
  reranker: string;
  llm: string;
  degraded: boolean;
}

export type StreamEvent =
  | { event: "started"; trace_id: string }
  | { event: "node"; node: string; intent?: string; strategy?: Strategy }
  | { event: "token"; text: string }
  | { event: "answer"; data: ChatResponse }
  | { event: "done"; trace_id: string };

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  state: "complete" | "streaming" | "error" | "stopped";
  citations?: Citation[];
  pendingAction?: ActionProposal | null;
  strategy?: Strategy | null;
  traceId?: string;
  latencyMs?: number;
}

export interface Conversation {
  id: string;
  title: string;
  updatedAt: number;
  sessionId?: string;
  messages: Message[];
}

export interface OrderItem {
  sku: string;
  name: string;
  quantity: number;
  unit_price_cents: number;
  customized: boolean;
}

export interface Order {
  order_id: string;
  status: string;
  total_cents: number;
  created_at: string;
  paid_at: string | null;
  shipped_at: string | null;
  delivered_at: string | null;
  tracking_no: string | null;
  receiver_city: string | null;
  items: OrderItem[];
}

export interface ShipmentEvent {
  status: string;
  at: string;
  tracking_no?: string;
}

export interface ShipmentInfo {
  order_id: string;
  tracking_no: string | null;
  receiver_city: string | null;
  events: ShipmentEvent[];
}

export interface RefundItem {
  refund_id: string;
  order_id: string;
  status: string;
  amount_cents: number;
  reason: string;
  created_at: string;
}

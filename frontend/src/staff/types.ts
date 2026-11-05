/** DTOs mirrored from app/schemas/after_sales*.py. Internal staff API only. */

export type Market = "au" | "sg" | "us";
export type StaffRole = "support" | "approver" | "auditor" | "operator";
export type RequestType = "damaged" | "wrong_item" | "return" | "other";
export type RequestStatus = "pending" | "ticket_created";
export type TicketAction = "reship" | "return" | "refund" | "reject" | "other";
export type TicketStatus = "open" | "processing" | "closed";
export type CaseState =
  | "pending"
  | "processing"
  | "needs_info"
  | "waiting_approval"
  | "executing"
  | "reconciliation_required"
  | "completed"
  | "failed";
export type DraftStatus =
  | "editing"
  | "pending_approval"
  | "approved"
  | "executing"
  | "consumed"
  | "rejected"
  | "invalidated"
  | "superseded";
export type ApprovalStatus =
  | "pending"
  | "approved"
  | "executing"
  | "consumed"
  | "rejected"
  | "expired"
  | "invalidated";
export type OperationStatus =
  | "prepared"
  | "dispatching"
  | "awaiting_receipt"
  | "retry_wait"
  | "reconciliation_required"
  | "succeeded"
  | "failed";
export type MaterialType =
  | "manual_verification"
  | "delivery_verification"
  | "customer_contact"
  | "internal_note"
  | "evidence";
export type ReconciliationState =
  | "required"
  | "checking"
  | "matched"
  | "same_key_retry_eligible"
  | "manual_review";
export type TicketVerificationState = "matched_operation" | "external_only";

export interface StaffIdentity {
  staff_id: string;
  display_name: string;
  roles: StaffRole[];
  allowed_markets: Market[];
}

export interface ErrorBody {
  error_code?: string;
  message?: string;
  trace_id?: string;
  details?: { errors?: Array<{ type: string; loc: Array<string | number>; msg: string }> } | null;
}

export interface CaseSummary {
  case_id: number;
  market: Market;
  request_id: string;
  order_id: string;
  request_type: RequestType;
  state: CaseState;
  revision: number;
  attachment_count: number;
  remote_status: RequestStatus;
  remote_ticket_id: string | null;
  updated_at: string;
  has_pending_approval: boolean;
  has_prepared_operation: boolean;
}

export interface CaseListResponse {
  items: CaseSummary[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  last_success_at: string | null;
}

export interface SyncResponse {
  market: Market;
  processed: number;
  created: number;
  updated: number;
  stale_ignored: number;
  has_more: boolean;
  high_watermark: string;
  last_success_at: string;
}

export interface AttachmentView {
  attachment_id: string;
  file_name: string;
  media_type: string;
  size_bytes: number;
  status: string;
  created_at: string;
  expires_at: string;
}

export interface MaterialView {
  material_id: string;
  material_type: MaterialType;
  source: string;
  content: string;
  applies_to_sku: string | null;
  delivered_at: string | null;
  created_by_staff_id: string;
  created_at: string;
}

export interface EvidenceView {
  chunk_id: string;
  document_id: string;
  title: string;
  section: string | null;
  content: string;
  policy_version: string;
  source_path: string;
  source_label: string;
  source_type: string;
  policy_status: string;
  is_demo: boolean;
  retrieved_at?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  content_sha256: string;
}

export interface DraftView {
  draft_id: string;
  version: number;
  action: TicketAction;
  internal_summary: string;
  consumer_reply: string;
  evidence: EvidenceView[];
  evidence_sha256: string;
  policy_version: string | null;
  order_version: string | null;
  material_revision: number;
  status: DraftStatus;
  author_staff_id: string;
  created_at: string;
  submitted_at: string | null;
  decided_at: string | null;
}

export interface TrustedCategoryView {
  sku: string;
  category: string;
  source: string;
}

export interface CaseEvidenceResponse {
  case_id: number;
  market: Market;
  as_of: string;
  queried_at: string;
  trusted_categories: TrustedCategoryView[];
  chunks: EvidenceView[];
  policy_version: string | null;
  evidence_sha256: string;
  corpus_revision: string;
  missing_reasons: string[];
}

export interface RecipientSummary {
  masked_name: string;
  country_code: "AU" | "SG" | "US";
  region: string;
  postal_code_masked: string;
}

export interface ServiceOrderItem {
  order_item_id: string;
  sku: string;
  name: string;
  quantity: number;
  unit_price_minor: number;
  line_total_minor: number;
}

export interface ServiceOrder {
  order_id: string;
  market: Market;
  currency: string;
  items: ServiceOrderItem[];
  subtotal_minor: number;
  shipping_minor: number;
  tax_minor: number;
  discount_minor: number;
  total_minor: number;
  status: "paid" | "fulfilling" | "fulfilled" | "cancelled";
  created_at: string;
  order_version: string;
  recipient_summary: RecipientSummary;
}

export interface ShipmentEvent {
  event_id: string;
  status: "pending" | "in_transit" | "delivered" | "exception";
  description: string;
  occurred_at: string;
  location_summary: string | null;
}

export interface ServiceShipment {
  shipment_id: string;
  order_id: string;
  status: "pending" | "in_transit" | "delivered" | "exception";
  carrier: string;
  events: ShipmentEvent[];
}

export interface TicketReceipt {
  receipt_id: string;
  ticket_id: string;
  request_id: string;
  action: TicketAction;
  request_status: "ticket_created";
  created_at: string;
}

export interface ServiceTicket {
  ticket_id: string;
  request_id: string;
  order_id: string;
  market: Market;
  action: TicketAction;
  reply: string;
  status: TicketStatus;
  draft_version: string;
  approval_id: string;
  policy_version: string;
  order_version: string;
  receipt: TicketReceipt;
  created_at: string;
  updated_at: string;
}

export interface CaseTicketResponse {
  case_id: number;
  market: Market;
  ticket: ServiceTicket;
  queried_at: string;
  verification_state: TicketVerificationState;
  operation_id: string | null;
}

export interface AttachmentFingerprintView {
  attachment_id: string;
  status: string;
  media_type: string;
  size_bytes: number;
  expires_at: string;
}

export interface ApprovalCaseSnapshotView {
  case_id: number;
  market: Market;
  request_id: string;
  order_id: string;
  request_type: RequestType;
}

export interface ApprovalRequestSnapshotView {
  request_id: string;
  sequence: string;
  status: RequestStatus;
  ticket_id: string | null;
  type: RequestType;
  description_sha256: string;
  attachments: AttachmentFingerprintView[];
  updated_at: string;
}

export interface ApprovalDraftSnapshotView {
  draft_id: string;
  version: number;
  action: TicketAction;
  internal_summary: string;
  consumer_reply: string;
  evidence_sha256: string;
}

export interface ApprovalMaterialSnapshotView {
  material_id: string;
  material_type: string;
  source: string;
  content: string;
  content_sha256: string;
  applies_to_sku: string | null;
  delivered_at: string | null;
  created_by_staff_id: string;
  created_at: string;
}

export interface ApprovalMaterialsSnapshotView {
  revision: number;
  items: ApprovalMaterialSnapshotView[];
}

export interface ApprovalEvidenceSnapshotView {
  chunks: EvidenceView[];
  policy_version: string | null;
  evidence_sha256: string;
  corpus_revision: string;
  as_of: string | null;
}

export interface ApprovalRequesterSnapshotView {
  staff_id: string;
  roles: string[];
  allowed_markets: string[];
}

export interface ApprovalWindowSnapshotView {
  requested_at: string;
  expires_at: string;
}

export interface ApprovalDeliveryProofView {
  kind: "shipment" | "manual_verification";
  shipment_id: string | null;
  event_id: string | null;
  delivered_at: string | null;
  material_id: string | null;
  material_type: string | null;
  applies_to_sku: string | null;
  source_description: string | null;
  content_sha256: string | null;
  recorded_by_staff_id: string | null;
}

export interface ApprovalSnapshotView {
  schema_version: number;
  case: ApprovalCaseSnapshotView;
  order: ServiceOrder;
  request: ApprovalRequestSnapshotView;
  shipment: ServiceShipment | null;
  delivery_proof: ApprovalDeliveryProofView | null;
  materials: ApprovalMaterialsSnapshotView;
  draft: ApprovalDraftSnapshotView;
  evidence: ApprovalEvidenceSnapshotView;
  trusted_categories: TrustedCategoryView[];
  requester: ApprovalRequesterSnapshotView;
  approval: ApprovalWindowSnapshotView;
}

export interface ApprovalView {
  approval_id: string;
  draft_id: string;
  status: ApprovalStatus;
  requester_staff_id: string;
  decided_by_staff_id: string | null;
  expires_at: string;
  snapshot_sha256: string;
  snapshot: ApprovalSnapshotView | null;
  decision_reason: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface OperationView {
  operation_id: string;
  approval_id: string;
  draft_id: string;
  idempotency_key: string;
  status: OperationStatus;
  attempt: number;
  payload: Record<string, unknown>;
  payload_sha256: string;
  dispatched_at: string | null;
  lease_owner: string | null;
  lease_epoch: number;
  lease_until: string | null;
  next_attempt_at: string | null;
  receipt: Record<string, unknown> | null;
  last_error_code: string | null;
  failure_reason: string | null;
  lookup_count: number;
  reconciliation_state: string | null;
  reconcile_started_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface EventView {
  seq: number;
  event_type: string;
  actor_staff_id: string | null;
  old_state: string | null;
  new_state: string | null;
  entity_type: string | null;
  entity_id: string | null;
  reason: string | null;
  created_at: string;
}

export interface EventListResponse {
  items: EventView[];
  after_seq: number;
  limit: number;
  has_more: boolean;
}

export interface CaseDetail {
  case_id: number;
  market: Market;
  request_id: string;
  order_id: string;
  request_type: RequestType;
  description: string;
  state: CaseState;
  revision: number;
  material_revision: number;
  remote_status: RequestStatus;
  remote_ticket_id: string | null;
  request_created_at: string;
  request_updated_at: string;
  attachments: AttachmentView[];
  order: ServiceOrder | null;
  order_queried_at: string | null;
  current_draft: DraftView | null;
  drafts: DraftView[];
  materials: MaterialView[];
  approvals: ApprovalView[];
  operations: OperationView[];
  created_at: string;
  updated_at: string;
}

export interface WorkerOperationSummary {
  operation_id: string;
  case_id: number;
  market: Market;
  status: OperationStatus;
  case_state: CaseState;
  attempt: number;
  lookup_count: number;
  reconciliation_state: ReconciliationState | null;
  remote_ticket_id: string | null;
  last_error_code: string | null;
  failure_reason: string | null;
  dispatched_at: string | null;
  next_attempt_at: string | null;
  updated_at: string;
}

export interface WorkerOperationListResponse {
  items: WorkerOperationSummary[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface WorkerOperationEvent {
  seq: number;
  event_type: string;
  old_state: string | null;
  new_state: string | null;
  reason: string | null;
  created_at: string;
}

export interface WorkerOperationDetail extends WorkerOperationSummary {
  reconcile_started_at: string | null;
  events: WorkerOperationEvent[];
}

export interface MaterialCreateInput {
  expected_revision: number;
  material_type: MaterialType;
  source: string;
  content: string;
  applies_to_sku?: string;
  delivered_at?: string;
}

export interface DraftCreateInput {
  expected_revision: number;
  action: TicketAction;
  internal_summary: string;
  consumer_reply: string;
  evidence_chunk_ids: string[];
}

export interface AttachmentBlob {
  blob: Blob;
  mediaType: string;
}

export interface StaffChatPayload {
  message: string;
  user_id: string;
  brand: "epig";
  market: Market;
  session_id?: string;
}

export interface StaffChatAnswer {
  answer: string;
  session_id: string;
  trace_id: string;
  citations: Array<{
    document_id: string;
    title: string;
    chunk_id: string;
    section: string | null;
  }>;
  refused: boolean;
  error_code: string | null;
}

export type StaffStreamEvent =
  | { event: "started"; trace_id: string }
  | { event: "node"; node: string; intent?: string; strategy?: string }
  | { event: "token"; text: string }
  | { event: "answer"; data: StaffChatAnswer }
  | { event: "done"; trace_id: string };

import { act } from "react";
import type { ReactElement } from "react";
import { createRoot } from "react-dom/client";
import type { StaffApi } from "./api";
import { StaffContext } from "./context";
import type { StaffContextValue } from "./context";
import type {
  ApprovalSnapshotView,
  ApprovalView,
  CaseDetail,
  CaseTicketResponse,
  DraftView,
  EvidenceView,
  ServiceOrder,
  WorkerOperationDetail,
  WorkerOperationListResponse,
} from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export interface Rendered {
  container: HTMLElement;
  rerender: (element: ReactElement) => Promise<void>;
  unmount: () => Promise<void>;
}

export async function renderUi(element: ReactElement): Promise<Rendered> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(element);
  });
  return {
    container,
    async rerender(next: ReactElement) {
      await act(async () => {
        root.render(next);
      });
    },
    async unmount() {
      await act(async () => {
        root.unmount();
      });
      container.remove();
    },
  };
}

export async function renderStaff(
  element: ReactElement,
  context: StaffContextValue,
): Promise<Rendered> {
  const rendered = await renderUi(
    <StaffContext.Provider value={context}>{element}</StaffContext.Provider>,
  );
  return {
    ...rendered,
    async rerender(next: ReactElement) {
      await rendered.rerender(
        <StaffContext.Provider value={context}>{next}</StaffContext.Provider>,
      );
    },
  };
}

export async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

export async function flushMicrotasks(rounds = 20): Promise<void> {
  for (let index = 0; index < rounds; index += 1) await Promise.resolve();
}

/** Pump pending promise chains through React without depending on timers. */
export async function pump(rounds = 12): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await act(async () => {
      await flushMicrotasks(20);
    });
  }
}

export async function waitFor(
  predicate: () => boolean,
  label: string,
  rounds = 100,
): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    if (predicate()) return;
    await act(async () => {
      await flushMicrotasks(10);
    });
  }
  if (predicate()) return;
  throw new Error(`timed out waiting for: ${label}`);
}

export async function setControlValue(
  element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement,
  value: string,
): Promise<void> {
  const proto = element instanceof HTMLSelectElement
    ? HTMLSelectElement.prototype
    : element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", {
      bubbles: true,
    }));
    await flushMicrotasks();
  });
}

export async function submitLogin(container: HTMLElement, token: string): Promise<void> {
  const input = container.querySelector('input[name="token"]') as HTMLInputElement | null;
  const form = container.querySelector("form") as HTMLFormElement | null;
  if (!input || !form) throw new Error("login form not found");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(input, token);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flushMicrotasks();
  });
  await pump(4);
}

export async function click(container: HTMLElement, label: string): Promise<void> {
  const button = [...container.querySelectorAll("button")]
    .find((item) => item.textContent?.includes(label));
  if (!button) throw new Error(`button not found: ${label}`);
  await act(async () => {
    button.click();
  });
  await settle();
}

export function textOf(container: HTMLElement): string {
  return container.textContent ?? "";
}

export function createStaffContext(
  overrides: Partial<StaffContextValue> = {},
): StaffContextValue {
  return {
    api: {} as StaffApi,
    identity: {
      staff_id: "me",
      display_name: "测试员工",
      roles: ["support", "approver"],
      allowed_markets: ["au", "sg"],
    },
    market: "au",
    generation: 1,
    isCurrent: () => true,
    run: (async () => ({ ok: true, stale: false, value: undefined })) as unknown as StaffContextValue["run"],
    reportSessionEnding: () => {},
    markRead: () => {},
    canSupport: true,
    canApprove: true,
    canAudit: false,
    canOperate: false,
    ...overrides,
  };
}

export function evidenceFixture(chunkId = "chunk-1"): EvidenceView {
  return {
    chunk_id: chunkId,
    document_id: "doc-1",
    title: "退货政策",
    section: "质量问题",
    content: "演示政策原文：质量问题可在规定期限内申请退货。",
    policy_version: "epig-demo-v1",
    source_path: "knowledge/epig/au/returns.md",
    source_label: "AU 退货政策",
    source_type: "policy",
    policy_status: "demo",
    is_demo: true,
    retrieved_at: "2026-11-06T00:00:00Z",
    effective_from: "2026-01-01",
    effective_to: null,
    content_sha256: "not-rendered-hash",
  };
}

export function orderFixture(overrides: Partial<ServiceOrder> = {}): ServiceOrder {
  return {
    order_id: "order-au-1",
    market: "au",
    currency: "AUD",
    items: [{
      order_item_id: "item-1",
      sku: "SKU-1",
      name: "测试商品",
      quantity: 1,
      unit_price_minor: 1299,
      line_total_minor: 1299,
    }],
    subtotal_minor: 1299,
    shipping_minor: 0,
    tax_minor: 130,
    discount_minor: 0,
    total_minor: 1429,
    status: "fulfilled",
    created_at: "2026-11-01T00:00:00Z",
    order_version: "order-v1",
    recipient_summary: {
      masked_name: "王*",
      country_code: "AU",
      region: "NSW",
      postal_code_masked: "20**",
    },
    ...overrides,
  };
}

export function draftFixture(overrides: Partial<DraftView> = {}): DraftView {
  return {
    draft_id: "draft-3",
    version: 3,
    action: "refund",
    internal_summary: "内部摘要-当前",
    consumer_reply: "消费者回复-当前",
    evidence: [evidenceFixture()],
    evidence_sha256: "not-rendered",
    policy_version: "epig-demo-v1",
    order_version: "order-v1",
    material_revision: 0,
    status: "editing",
    author_staff_id: "me",
    created_at: "2026-11-06T01:00:00Z",
    submitted_at: null,
    decided_at: null,
    ...overrides,
  };
}

export function snapshotFixture(
  overrides: Partial<ApprovalSnapshotView> = {},
): ApprovalSnapshotView {
  return {
    schema_version: 1,
    case: {
      case_id: 1,
      market: "au",
      request_id: "req-1",
      order_id: "order-au-1",
      request_type: "damaged",
    },
    order: orderFixture(),
    request: {
      request_id: "req-1",
      sequence: "4",
      status: "pending",
      ticket_id: null,
      type: "damaged",
      description_sha256: "not-rendered",
      attachments: [],
      updated_at: "2026-11-06T00:00:00Z",
    },
    shipment: null,
    delivery_proof: null,
    materials: { revision: 0, items: [] },
    draft: {
      draft_id: "draft-2",
      version: 2,
      action: "refund",
      internal_summary: "内部摘要-冻结",
      consumer_reply: "消费者回复-冻结快照",
      evidence_sha256: "not-rendered",
    },
    evidence: {
      chunks: [evidenceFixture()],
      policy_version: "epig-demo-v1",
      evidence_sha256: "not-rendered",
      corpus_revision: "corpus-1",
      as_of: "2026-11-06",
    },
    trusted_categories: [{ sku: "SKU-1", category: "apparel", source: "config" }],
    requester: {
      staff_id: "requester",
      roles: ["support"],
      allowed_markets: ["au"],
    },
    approval: {
      requested_at: "2026-11-06T01:00:00Z",
      expires_at: "2099-01-01T00:00:00Z",
    },
    ...overrides,
  };
}

export function approvalFixture(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return {
    approval_id: "approval-1",
    draft_id: "draft-2",
    status: "pending",
    requester_staff_id: "requester",
    decided_by_staff_id: null,
    expires_at: "2099-01-01T00:00:00Z",
    snapshot_sha256: "not-rendered",
    snapshot: snapshotFixture(),
    decision_reason: null,
    created_at: "2026-11-06T01:00:00Z",
    decided_at: null,
    ...overrides,
  };
}

export function detailFixture(overrides: Partial<CaseDetail> = {}): CaseDetail {
  const current = draftFixture();
  return {
    case_id: 1,
    market: "au",
    request_id: "req-1",
    order_id: "order-au-1",
    request_type: "damaged",
    description: "消费者描述：商品有破损。",
    state: "processing",
    revision: 7,
    material_revision: 0,
    remote_status: "pending",
    remote_ticket_id: null,
    request_created_at: "2026-11-05T00:00:00Z",
    request_updated_at: "2026-11-06T00:00:00Z",
    attachments: [],
    order: orderFixture(),
    order_queried_at: "2026-11-06T00:00:00Z",
    current_draft: current,
    drafts: [current],
    materials: [],
    approvals: [],
    operations: [],
    created_at: "2026-11-05T00:00:00Z",
    updated_at: "2026-11-06T00:00:00Z",
    ...overrides,
  };
}

export function ticketFixture(): CaseTicketResponse {
  return {
    case_id: 1,
    market: "au",
    queried_at: "2026-11-06T02:00:00Z",
    verification_state: "matched_operation",
    operation_id: "op-1",
    ticket: {
      ticket_id: "ticket-1",
      request_id: "req-1",
      order_id: "order-au-1",
      market: "au",
      action: "refund",
      reply: "已经为您处理退款。",
      status: "processing",
      draft_version: "3",
      approval_id: "approval-1",
      policy_version: "epig-demo-v1",
      order_version: "order-v1",
      receipt: {
        receipt_id: "receipt-1",
        ticket_id: "ticket-1",
        request_id: "req-1",
        action: "refund",
        request_status: "ticket_created",
        created_at: "2026-11-06T01:30:00Z",
      },
      created_at: "2026-11-06T01:30:00Z",
      updated_at: "2026-11-06T01:40:00Z",
    },
  };
}

export function operationListFixture(): WorkerOperationListResponse {
  return {
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
  };
}

export function operationDetailFixture(): WorkerOperationDetail {
  return {
    operation_id: "op-1",
    case_id: 1,
    market: "au",
    status: "awaiting_receipt",
    case_state: "executing",
    attempt: 1,
    lookup_count: 0,
    reconciliation_state: "checking",
    remote_ticket_id: null,
    last_error_code: null,
    failure_reason: null,
    dispatched_at: "2026-11-06T01:00:00Z",
    next_attempt_at: null,
    updated_at: "2026-11-06T01:00:00Z",
    reconcile_started_at: "2026-11-06T01:00:00Z",
    events: [],
  };
}

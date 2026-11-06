import { SseDecoder } from "../lib/sse";
import type {
  AttachmentBlob,
  CaseDetail,
  CaseEvidenceResponse,
  CaseListResponse,
  CaseTicketResponse,
  DemoLoginResponse,
  DraftCreateInput,
  EventListResponse,
  LoginOptionsResponse,
  MaterialCreateInput,
  Market,
  ServiceOrder,
  ServiceShipment,
  StaffChatPayload,
  StaffIdentity,
  StaffStreamEvent,
  SyncResponse,
  WorkerOperationDetail,
  WorkerOperationListResponse,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const PREFIX = "/api/v1/after-sales";

export class StaffApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly traceId: string | null;

  constructor(status: number, message: string, code: string | null, traceId: string | null) {
    super(message);
    this.name = "StaffApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
  }
}

export function isSessionEndingError(error: unknown): boolean {
  if (!(error instanceof StaffApiError)) return false;
  if (error.status === 401) return true;
  // A self-approval rejection is a business rule, not a revoked session. The
  // server remains the final authority and the UI must keep the user signed in.
  return error.status === 403 && error.code !== "SELF_APPROVAL_FORBIDDEN";
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
}

function encodeQuery(params: Record<string, string | number | boolean | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

function friendlyError(status: number): string {
  if (status === 401) return "登录已失效，请重新登录。";
  if (status === 403) return "当前身份或市场没有该操作的权限。";
  if (status === 404) return "未找到请求的资源。";
  if (status === 409) return "数据已被其他人更新或当前状态不允许该操作。";
  if (status === 410) return "附件已过期，无法读取。";
  if (status === 422) return "提交内容不符合要求，请检查后重试。";
  if (status === 429) return "请求过于频繁，请稍后再试。";
  if (status >= 500) return "服务暂时不可用，请稍后重试。";
  return `请求失败（HTTP ${status}）。`;
}

async function parseError(response: Response): Promise<StaffApiError> {
  let message = friendlyError(response.status);
  let code: string | null = null;
  let traceId: string | null = response.headers.get("x-trace-id");
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const record = body as Record<string, unknown>;
      if (typeof record.message === "string" && record.message.trim()) {
        // Application errors are stable English sentences; show a Chinese
        // action-oriented message while keeping the code for diagnostics.
        if (response.status >= 500) message = friendlyError(response.status);
      }
      if (typeof record.error_code === "string") code = record.error_code;
      if (typeof record.trace_id === "string") traceId = record.trace_id;
    }
  } catch {
    /* Keep the status-based message when no JSON body is available. */
  }
  if (response.status === 409 && code === "REVISION_CONFLICT") {
    message = "数据版本已变化，你的输入已保留。请显式读取最新版本后再提交。";
  } else if (response.status === 409 && code === "STATE_CONFLICT") {
    message = "当前状态已变化，你的输入已保留。请读取最新状态后再操作。";
  } else if (response.status === 403 && code === "SELF_APPROVAL_FORBIDDEN") {
    message = "不能审批自己提交的草稿，请由其他审批人处理。";
  } else if (response.status === 503 && code === "LLM_PROVIDER_UNAVAILABLE") {
    message = "AI 建议未配置或暂时不可用，可继续人工撰写草稿。";
  }
  return new StaffApiError(response.status, message, code, traceId);
}

async function request<T>(token: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    Authorization: `Bearer ${token}`,
  };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${PREFIX}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new StaffApiError(0, "网络连接失败，请检查网络后重试。", null, null);
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function getLoginOptions(signal: AbortSignal): Promise<LoginOptionsResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${PREFIX}/login-options`, {
      headers: { Accept: "application/json" },
      signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new StaffApiError(0, "网络连接失败，请检查网络后重试。", null, null);
  }
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as LoginOptionsResponse;
}

/**
 * Exchange the shared demo password for a short-lived signed session. The
 * password is only sent in this one request body; it never becomes a header,
 * URL value, or persisted browser state.
 */
export async function demoLogin(
  staffId: string,
  password: string,
  signal: AbortSignal,
): Promise<DemoLoginResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${PREFIX}/demo-login`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ staff_id: staffId, password }),
      signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new StaffApiError(0, "网络连接失败，请检查网络后重试。", null, null);
  }
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as DemoLoginResponse;
}

export async function loginStaff(
  token: string,
  signal: AbortSignal,
): Promise<StaffIdentity> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    Authorization: `Bearer ${token}`,
  };
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${PREFIX}/me`, { headers, signal, cache: "no-store" });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new StaffApiError(0, "网络连接失败，请检查网络后重试。", null, null);
  }
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as StaffIdentity;
}

export class StaffApi {
  private readonly token: string;

  constructor(token: string) {
    this.token = token;
  }

  me(signal: AbortSignal): Promise<StaffIdentity> {
    return loginStaff(this.token, signal);
  }

  listCases(
    market: Market,
    params: {
      status?: string;
      request_type?: string;
      updated_from?: string;
      updated_to?: string;
      limit?: number;
      offset?: number;
    },
    signal: AbortSignal,
  ): Promise<CaseListResponse> {
    return request(
      this.token,
      `/${market}/cases${encodeQuery(params)}`,
      { signal },
    );
  }

  sync(market: Market, signal: AbortSignal): Promise<SyncResponse> {
    return request(this.token, `/${market}/cases/sync`, { method: "POST", signal });
  }

  getCase(market: Market, caseId: number, signal: AbortSignal): Promise<CaseDetail> {
    return request(this.token, `/${market}/cases/${caseId}`, { signal });
  }

  getEvents(
    market: Market,
    caseId: number,
    afterSeq: number,
    signal: AbortSignal,
  ): Promise<EventListResponse> {
    return request(
      this.token,
      `/${market}/cases/${caseId}/events${encodeQuery({ after_seq: afterSeq, limit: 50 })}`,
      { signal },
    );
  }

  getEvidence(
    market: Market,
    caseId: number,
    signal: AbortSignal,
  ): Promise<CaseEvidenceResponse> {
    return request(this.token, `/${market}/cases/${caseId}/evidence`, { signal });
  }

  getTicket(market: Market, caseId: number, signal: AbortSignal): Promise<CaseTicketResponse> {
    return request(this.token, `/${market}/cases/${caseId}/ticket`, { signal });
  }

  getOrder(market: Market, orderId: string, signal: AbortSignal): Promise<ServiceOrder> {
    return request(this.token, `/${market}/orders/${encodeURIComponent(orderId)}`, { signal });
  }

  getShipment(market: Market, orderId: string, signal: AbortSignal): Promise<ServiceShipment> {
    return request(
      this.token,
      `/${market}/orders/${encodeURIComponent(orderId)}/shipment`,
      { signal },
    );
  }

  getAttachment(
    market: Market,
    attachmentId: string,
    signal: AbortSignal,
  ): Promise<AttachmentBlob> {
    return this.attachment(market, attachmentId, signal);
  }

  addMaterial(
    market: Market,
    caseId: number,
    body: MaterialCreateInput,
    signal: AbortSignal,
  ): Promise<CaseDetail> {
    return request(this.token, `/${market}/cases/${caseId}/materials`, {
      method: "POST",
      body,
      signal,
    });
  }

  createDraft(
    market: Market,
    caseId: number,
    body: DraftCreateInput,
    signal: AbortSignal,
  ): Promise<CaseDetail> {
    return request(this.token, `/${market}/cases/${caseId}/drafts`, {
      method: "POST",
      body,
      signal,
    });
  }

  submitDraft(
    market: Market,
    caseId: number,
    version: number,
    expectedRevision: number,
    signal: AbortSignal,
  ): Promise<CaseDetail> {
    return request(this.token, `/${market}/cases/${caseId}/drafts/${version}/submit`, {
      method: "POST",
      body: { expected_revision: expectedRevision },
      signal,
    });
  }

  suggest(
    market: Market,
    caseId: number,
    expectedRevision: number,
    signal: AbortSignal,
  ): Promise<CaseDetail> {
    return request(this.token, `/${market}/cases/${caseId}/suggestion`, {
      method: "POST",
      body: { expected_revision: expectedRevision },
      signal,
    });
  }

  approve(
    market: Market,
    caseId: number,
    approvalId: string,
    expectedRevision: number,
    signal: AbortSignal,
  ): Promise<CaseDetail> {
    return request(
      this.token,
      `/${market}/cases/${caseId}/approvals/${encodeURIComponent(approvalId)}/approve`,
      { method: "POST", body: { expected_revision: expectedRevision }, signal },
    );
  }

  reject(
    market: Market,
    caseId: number,
    approvalId: string,
    expectedRevision: number,
    reason: string,
    signal: AbortSignal,
  ): Promise<CaseDetail> {
    return request(
      this.token,
      `/${market}/cases/${caseId}/approvals/${encodeURIComponent(approvalId)}/reject`,
      { method: "POST", body: { expected_revision: expectedRevision, reason }, signal },
    );
  }

  listOperations(
    market: Market,
    params: { status?: string; limit?: number; offset?: number },
    signal: AbortSignal,
  ): Promise<WorkerOperationListResponse> {
    return request(this.token, `/${market}/operations${encodeQuery(params)}`, { signal });
  }

  getOperation(
    market: Market,
    operationId: string,
    signal: AbortSignal,
  ): Promise<WorkerOperationDetail> {
    return request(
      this.token,
      `/${market}/operations/${encodeURIComponent(operationId)}`,
      { signal },
    );
  }

  lookupOperation(
    market: Market,
    operationId: string,
    signal: AbortSignal,
  ): Promise<WorkerOperationDetail> {
    return request(
      this.token,
      `/${market}/operations/${encodeURIComponent(operationId)}/lookup`,
      { method: "POST", signal },
    );
  }

  async streamChat(
    payload: StaffChatPayload,
    onEvent: (event: StaffStreamEvent) => void,
    signal: AbortSignal,
  ): Promise<void> {
    let response: Response;
    try {
      response = await fetch(`${API_BASE}/api/v1/chat/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          Authorization: `Bearer ${this.token}`,
        },
        body: JSON.stringify(payload),
        signal,
        cache: "no-store",
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new StaffApiError(0, "网络连接失败，请检查网络后重试。", null, null);
    }
    if (!response.ok) throw await parseError(response);
    if (!response.body) throw new StaffApiError(0, "浏览器不支持流式响应。", null, null);
    const reader = response.body.getReader();
    const decoder = new SseDecoder();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const event of decoder.feed(value)) onEvent(event as StaffStreamEvent);
      }
      for (const event of decoder.finish()) onEvent(event as StaffStreamEvent);
    } finally {
      reader.releaseLock();
    }
  }

  private async attachment(
    market: Market,
    attachmentId: string,
    signal: AbortSignal,
  ): Promise<AttachmentBlob> {
    let response: Response;
    try {
      response = await fetch(
        `${API_BASE}${PREFIX}/${market}/attachments/${encodeURIComponent(attachmentId)}/content`,
        {
          headers: { Authorization: `Bearer ${this.token}`, Accept: "image/*" },
          signal,
          cache: "no-store",
        },
      );
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new StaffApiError(0, "附件下载失败，请检查网络后重试。", null, null);
    }
    if (!response.ok) throw await parseError(response);
    return {
      blob: await response.blob(),
      mediaType: response.headers.get("content-type") ?? "application/octet-stream",
    };
  }
}

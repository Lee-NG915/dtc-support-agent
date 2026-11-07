import { StaffApiError } from "./api";
import type {
  ApprovalStatus,
  CaseState,
  DraftStatus,
  MaterialType,
  OperationStatus,
  ReconciliationState,
  RequestStatus,
  RequestType,
  StaffRole,
  TicketAction,
  TicketStatus,
} from "./types";

export const ROLE_LABELS: Record<StaffRole, string> = {
  support: "客服",
  approver: "审批人",
  auditor: "审计",
  operator: "执行运营",
};

export const MARKET_LABELS = {
  au: "澳大利亚 (AU)",
  sg: "新加坡 (SG)",
  us: "美国 (US)",
} as const;

export const REQUEST_TYPE_LABELS: Record<RequestType, string> = {
  damaged: "商品破损",
  wrong_item: "错发或漏发",
  return: "退货或换货",
  other: "其他问题",
};

export const REQUEST_STATUS_LABELS: Record<RequestStatus, string> = {
  pending: "E-pig 待处理",
  ticket_created: "E-pig 已建单",
};

export const CASE_STATE_LABELS: Record<CaseState, string> = {
  pending: "待处理",
  processing: "处理中",
  needs_info: "需补充资料",
  waiting_approval: "待审批",
  executing: "执行中",
  reconciliation_required: "结果待核对",
  completed: "已完成",
  failed: "失败",
};

export const DRAFT_STATUS_LABELS: Record<DraftStatus, string> = {
  editing: "编辑中",
  pending_approval: "待审批",
  approved: "已批准",
  executing: "执行中",
  consumed: "已建单",
  rejected: "已驳回",
  invalidated: "已失效",
  superseded: "已被新版本替代",
};

export const APPROVAL_STATUS_LABELS: Record<ApprovalStatus, string> = {
  pending: "待审批",
  approved: "已批准，等待执行",
  executing: "执行中",
  consumed: "已建单",
  rejected: "内部驳回",
  expired: "已过期",
  invalidated: "已失效",
};

export const OPERATION_STATUS_LABELS: Record<OperationStatus, string> = {
  prepared: "已准备，等待执行",
  dispatching: "正在派遣",
  awaiting_receipt: "等待真实回执",
  retry_wait: "有限退避重试",
  reconciliation_required: "结果未知，待对账",
  succeeded: "回执已核验",
  failed: "未产生外部效果",
};

export const RECONCILIATION_LABELS: Record<ReconciliationState, string> = {
  required: "需要继续查询",
  checking: "正在查询",
  matched: "已匹配",
  same_key_retry_eligible: "仅允许原键原参数恢复",
  manual_review: "转人工复核",
};

export const MATERIAL_TYPE_LABELS: Record<MaterialType, string> = {
  manual_verification: "人工核验材料",
  delivery_verification: "签收核验材料",
  customer_contact: "消费者沟通记录",
  internal_note: "内部备注",
  evidence: "补充证据",
};

export const TICKET_ACTION_LABELS: Record<TicketAction, string> = {
  reship: "补发",
  return: "退货",
  refund: "退款",
  reject: "正式拒绝",
  other: "其他处理",
};

export const TICKET_STATUS_LABELS: Record<TicketStatus, string> = {
  open: "开放",
  processing: "处理中",
  closed: "已关闭",
};

export const ORDER_STATUS_LABELS = {
  paid: "已支付",
  fulfilling: "履约中",
  fulfilled: "已履约",
  cancelled: "已取消",
} as const;

export const SHIPMENT_STATUS_LABELS = {
  pending: "待发货",
  in_transit: "运输中",
  delivered: "已送达",
  exception: "物流异常",
} as const;

export const POLICY_STATUS_LABELS: Record<string, string> = {
  active: "有效",
  demo: "演示",
  superseded: "已被替代",
  expired: "已过期",
  draft: "草稿",
};

export function policyStatusLabel(value: string): string {
  return POLICY_STATUS_LABELS[value] ?? "状态已记录";
}

export const MISSING_REASON_LABELS: Record<string, string> = {
  order_unavailable: "无法读取订单，无法派生可信商品范围",
  missing_sku_category: "订单商品未映射到可信政策类别",
  missing_policy_evidence: "没有匹配的政策证据",
  missing_policy_version: "缺少政策版本",
  conflicting_policy: "政策证据冲突，需要人工判断",
  attachment_expired: "消费者附件已过期",
  missing_order_or_request: "订单或消费者诉求暂不可读",
  delivery_proof_missing: "缺少可核验的签收证明",
  delivery_proof_changed: "签收证明已变化",
  policy_changed: "政策版本或内容已变化",
  materials_changed: "人工材料已变化",
  order_changed: "订单版本已变化",
  requester_not_current: "申请人权限或状态已变化",
};

const EVENT_LABELS: Record<string, string> = {
  case_received: "接收诉求",
  draft_created: "保存草稿",
  candidate_draft_created: "保存执行未决候选草稿",
  draft_submitted: "提交审批",
  approval_granted: "审批通过，操作已准备",
  approval_rejected: "内部驳回",
  approval_invalidated: "审批失效",
  approval_expired: "审批过期",
  material_added: "补充内部材料",
  operation_prepared: "操作已准备，等待 Worker",
  operation_dispatched: "Worker 已派遣",
  operation_deferred: "执行延后",
  operation_retry_wait: "进入有限退避重试",
  operation_succeeded: "回执核验成功",
  operation_failed: "执行失败且无外部效果",
  case_completed: "诉求完成",
  case_failed: "诉求失败",
  case_needs_info: "需要补充资料",
  manual_review_required: "转人工复核",
  reconciliation_pending: "等待结果对账",
  remote_ticket_unverified: "发现未核验的远端工单",
};

const REASON_LABELS: Record<string, string> = {
  submitted_for_approval: "送审",
  approved: "审批通过",
  internal_rejection: "内部驳回，不创建工单",
  superseded_by_new_draft: "被新草稿替代",
  materials_changed: "材料变化",
  policy_changed: "政策变化",
  active_operation_unresolved: "原执行结果尚未确认",
  awaiting_worker: "等待 Worker 执行",
  missing_sku_category: "缺少商品类别映射",
  missing_policy_evidence: "缺少政策证据",
  missing_order_or_request: "订单或诉求不可用",
  ai_suggestion: "AI 建议",
  service_poll: "后台同步",
};

export function eventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? "状态更新";
}

export function reasonLabel(reason: string | null): string | null {
  if (!reason) return null;
  const direct = REASON_LABELS[reason];
  if (direct) return direct;
  if (reason.startsWith("service_poll:")) return "后台同步";
  return null;
}

export function missingReasonLabel(reason: string): string {
  return MISSING_REASON_LABELS[reason] ?? "存在未满足的业务前提";
}

export function errorMessage(error: unknown): string {
  if (error instanceof StaffApiError) {
    const trace = error.traceId ? `（追踪标识：${error.traceId}）` : "";
    return `${error.message}${trace}`;
  }
  return "操作失败，请稍后重试。";
}

export function isConflict(error: unknown): boolean {
  return error instanceof StaffApiError && error.status === 409;
}

export function isAiUnavailable(error: unknown): boolean {
  if (!(error instanceof StaffApiError)) return false;
  if (error.status !== 502 && error.status !== 503) return false;
  return (
    error.code === "LLM_PROVIDER_UNAVAILABLE" ||
    error.code === "LLM_TIMEOUT" ||
    error.code === "LLM_RATE_LIMITED" ||
    error.code === "LLM_OUTPUT_INVALID"
  );
}

const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "未提供";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间格式不可用";
  return dateTimeFormatter.format(date);
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "尚无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间格式不可用";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

export function formatMoney(minor: number, currency: string): string {
  const amount = minor / 100;
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${currency} ${amount.toFixed(2)}`;
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

export function maskId(value: string): string {
  if (value.length <= 10) return value;
  return `${value.slice(0, 4)}…${value.slice(-4)}`;
}

export function nowIso(): string {
  return new Date().toISOString();
}

export function toLocalDateTimeInputValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const ROLE_ORDER: StaffRole[] = ["support", "approver", "auditor", "operator"];

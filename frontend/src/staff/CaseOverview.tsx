import {
  AlertCircle,
  CheckCircle2,
  Info,
  ShieldAlert,
} from "lucide-react";
import {
  REQUEST_STATUS_LABELS,
  formatMoney,
  missingReasonLabel,
} from "./format";
import { Badge, Notice, TimeValue } from "./StaffBits";
import type { Tone } from "./StaffBits";
import type {
  ApprovalView,
  CaseDetail,
  CaseEvidenceResponse,
  OperationView,
  StaffIdentity,
} from "./types";

const APPROVAL_EXPIRY_SOON_MS = 24 * 60 * 60 * 1000;

interface NextStep {
  tone: Tone;
  title: string;
  detail: string;
}

export interface CaseOverviewProps {
  detail: CaseDetail;
  evidence: CaseEvidenceResponse | null;
  evidenceLoading: boolean;
  evidenceError: string | null;
  identity: StaffIdentity;
  canSupport: boolean;
  canApprove: boolean;
  canAudit: boolean;
  readAt?: string | null;
  /** Injectable clock for deterministic tests. */
  now?: number;
}

function timeOf(value: string): number {
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

function approvalCanBeDecided(
  approval: ApprovalView,
  identity: StaffIdentity,
  canApprove: boolean,
  nowMs: number,
): boolean {
  return canApprove &&
    approval.status === "pending" &&
    approval.requester_staff_id !== identity.staff_id &&
    approval.snapshot !== null &&
    timeOf(approval.expires_at) > nowMs;
}

function waitingApprovalStep(
  detail: CaseDetail,
  identity: StaffIdentity,
  canSupport: boolean,
  canApprove: boolean,
  canAudit: boolean,
  nowMs: number,
): NextStep {
  const pending = detail.approvals.filter((approval) => approval.status === "pending");
  const actionable = pending.find((approval) =>
    approvalCanBeDecided(approval, identity, canApprove, nowMs));
  if (actionable) {
    const version = detail.current_draft ? ` v${detail.current_draft.version}` : "";
    return {
      tone: "warning",
      title: "需要你审批",
      detail: `草稿${version}已送审并冻结快照；请核对后批准或内部驳回。批准后仍需 Worker 与真实回执，尚未创建工单。`,
    };
  }

  const missingSnapshot = pending.some((approval) => approval.snapshot === null);
  if (!actionable && missingSnapshot) {
    return {
      tone: "danger",
      title: "审批冻结快照缺失，无法审批",
      detail: "待审批项缺少送审时冻结的 snapshot，当前诉求不能批准；请联系管理员检查数据完整性后再处理。",
    };
  }

  const ownApproval = pending.find(
    (approval) => approval.requester_staff_id === identity.staff_id,
  );
  if (canApprove && ownApproval) {
    return {
      tone: "info",
      title: "需另一位审批人",
      detail: "你是本次送审的申请人，不能审批自己的草稿；请等待另一位审批人处理。",
    };
  }

  const allExpired = pending.length > 0 &&
    pending.every((approval) => timeOf(approval.expires_at) <= nowMs);
  if (allExpired) {
    return {
      tone: "warning",
      title: "审批已过期，需要重新送审",
      detail: "当前待审批项已超过有效期；请客服读取最新版本后重新送审，尚未创建工单。",
    };
  }

  if (canSupport) {
    return {
      tone: "info",
      title: "已送审，等待审批人处理",
      detail: "草稿已提交送审，正在等待审批人处理；尚未创建工单，也未执行。",
    };
  }
  if (canAudit) {
    return {
      tone: "info",
      title: "已送审，等待审批人处理",
      detail: "当前是审计只读视图；审批人尚未作出决定，尚未创建工单。",
    };
  }
  return {
    tone: "info",
    title: "等待审批人处理",
    detail: "当前诉求已送审，等待审批人处理；尚未创建工单，也未执行。",
  };
}

function deriveNextStep(
  detail: CaseDetail,
  identity: StaffIdentity,
  canSupport: boolean,
  canApprove: boolean,
  canAudit: boolean,
  nowMs: number,
): NextStep {
  if (detail.state === "waiting_approval") {
    return waitingApprovalStep(detail, identity, canSupport, canApprove, canAudit, nowMs);
  }
  if (detail.state === "needs_info") {
    return {
      tone: "warning",
      title: "补充材料或证据",
      detail: "先补齐消费者材料或内部证据，再重新核对政策证据并形成草稿。",
    };
  }
  if (detail.state === "executing") {
    return {
      tone: "info",
      title: "执行中，等待真实回执",
      detail: "已批准操作正在执行；这不代表工单已完成，需等待 Worker 与真实回执核验。",
    };
  }
  if (detail.state === "reconciliation_required") {
    return {
      tone: "warning",
      title: "结果待核对",
      detail: "执行结果尚未确认，不能当作已完成；请查询真实回执或转人工核对。",
    };
  }
  if (detail.state === "completed") {
    return {
      tone: "success",
      title: "已完成",
      detail: "服务端已记录该诉求完成；以下内容仅供查看审计证据与历史版本。",
    };
  }
  if (detail.state === "failed") {
    return {
      tone: "danger",
      title: "执行失败",
      detail: "服务端记录为未产生外部效果；如需继续处理，必须重新形成草稿和审批。",
    };
  }

  const current = detail.current_draft;
  if (current?.status === "pending_approval") {
    return {
      tone: "info",
      title: "已送审，等待审批人处理",
      detail: "草稿已提交送审，正在等待审批人处理；尚未创建工单，也未执行。",
    };
  }
  if (current?.status === "editing") {
    return {
      tone: "warning",
      title: "核对草稿并送审",
      detail: canSupport
        ? "草稿仍在编辑中；核对内部摘要、消费者回复与引用证据后提交审批。送审不会创建工单。"
        : "客服草稿仍在编辑中；当前角色只读，请等待客服核对并送审。",
    };
  }
  return {
    tone: "warning",
    title: "形成处理草稿",
    detail: canSupport
      ? "尚未形成可用草稿；先核对订单与政策证据，再人工撰写或生成草稿。"
      : "尚未形成可用草稿；等待客服补齐材料并形成处理草稿。",
  };
}

function needsManualReview(operation: OperationView): boolean {
  return operation.status === "reconciliation_required" ||
    operation.reconciliation_state === "manual_review" ||
    operation.reconciliation_state === "required";
}

function NextStepIcon({ tone }: { tone: Tone }) {
  const Icon = tone === "success" ? CheckCircle2
    : tone === "danger" ? AlertCircle
      : tone === "warning" ? ShieldAlert
        : Info;
  return <Icon size={18} aria-hidden="true" />;
}

export function CaseOverview({
  detail,
  evidence,
  evidenceLoading,
  evidenceError,
  identity,
  canSupport,
  canApprove,
  canAudit,
  readAt,
  now,
}: CaseOverviewProps) {
  const nowMs = now ?? Date.now();
  const step = deriveNextStep(detail, identity, canSupport, canApprove, canAudit, nowMs);
  const order = detail.order;
  const productCount = order?.items.reduce((total, item) => total + item.quantity, 0) ?? 0;
  const missingReasons = evidence?.missing_reasons ?? [];
  const pendingApprovals = detail.approvals.filter(
    (approval) => approval.status === "pending",
  );
  const expiringApprovals = pendingApprovals.filter((approval) => {
    const expires = timeOf(approval.expires_at);
    return expires - nowMs <= APPROVAL_EXPIRY_SOON_MS;
  });
  const expiredApprovalCount = expiringApprovals.filter(
    (approval) => timeOf(approval.expires_at) <= nowMs,
  ).length;
  const soonExpiringCount = expiringApprovals.length - expiredApprovalCount;
  const missingSnapshotApprovals = pendingApprovals.filter(
    (approval) => approval.snapshot === null,
  );
  const manualReviewOperations = detail.operations.filter(needsManualReview);
  const policyVersion = evidence?.policy_version
    ?? detail.current_draft?.policy_version
    ?? null;

  const evidenceCount = evidenceLoading
    ? "读取中"
    : evidenceError
      ? "读取失败"
      : evidence
        ? `${evidence.chunks.length} 条`
        : "尚未读取";
  const evidenceHint = evidenceLoading
    ? "正在读取政策证据…"
    : evidenceError
      ? "证据读取失败，不能据此送审"
      : evidence
        ? `版本 ${evidence.policy_version ?? "未提供"} · 缺资料 ${missingReasons.length} 项`
        : "服务端尚未返回证据";

  return (
    <section className="staff-panel staff-overview" aria-label="下一步摘要">
      <div className={`staff-overview-next staff-overview-next-${step.tone}`}>
        <div className="staff-overview-next-head">
          <Badge tone={step.tone}>下一步</Badge>
          <NextStepIcon tone={step.tone} />
        </div>
        <h3>{step.title}</h3>
        <p>{step.detail}</p>
      </div>

      <div className="staff-overview-words">
        <div className="staff-subhead">
          <h4>客户原话</h4>
          <TimeValue value={detail.request_created_at} prefix="消费者提交" />
        </div>
        <p className="staff-description">{detail.description}</p>
        <dl className="staff-kv staff-overview-kv">
          <div><dt>E-pig 状态</dt><dd>{REQUEST_STATUS_LABELS[detail.remote_status]}</dd></div>
          <div><dt>远端更新时间</dt><dd><TimeValue value={detail.request_updated_at} /></dd></div>
        </dl>
      </div>

      <dl className="staff-overview-facts" aria-label="关键事实">
        <div className="staff-fact">
          <dt>订单及商品</dt>
          <dd>
            {order ? `${productCount} 件商品` : "订单快照未提供"}
            <small>
              {order
                ? order.items.map((item) => `${item.name} × ${item.quantity}`).join("、")
                : "订单可能暂不可读取"}
            </small>
          </dd>
        </div>
        <div className="staff-fact">
          <dt>订单金额</dt>
          <dd>
            {order ? formatMoney(order.total_minor, order.currency) : "未提供"}
            <small>订单 {detail.order_id}</small>
          </dd>
        </div>
        <div className="staff-fact">
          <dt>客户附件数</dt>
          <dd>
            {detail.attachments.length} 个
            <small>
              {detail.attachments.length > 0
                ? "在“附件与内部材料”中查看"
                : "消费者未上传附件"}
            </small>
          </dd>
        </div>
        <div className="staff-fact">
          <dt>政策证据数</dt>
          <dd>
            {evidenceCount}
            <small>{evidenceHint}</small>
          </dd>
        </div>
      </dl>

      {missingSnapshotApprovals.length > 0 ? (
        <Notice tone="danger" title="待审批项的冻结快照缺失">
          {missingSnapshotApprovals.length} 个待审批项没有送审时冻结的 snapshot，当前诉求不能批准；请管理员检查数据完整性。
        </Notice>
      ) : null}

      {expiringApprovals.length > 0 ? (
        <Notice tone="warning" title="审批临近过期或已过期">
          {expiredApprovalCount > 0
            ? `${expiredApprovalCount} 个待审批项已超过有效期，需要客服重新送审。`
            : null}
          {soonExpiringCount > 0
            ? `${soonExpiringCount} 个待审批项将在 24 小时内过期，请尽快处理。`
            : null}
          <ul className="staff-compact-list">
            {expiringApprovals.map((approval) => (
              <li key={approval.approval_id}>
                <TimeValue
                  value={approval.expires_at}
                  prefix={timeOf(approval.expires_at) <= nowMs ? "已过期，有效期" : "有效期至"}
                />
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}

      {manualReviewOperations.length > 0 ? (
        <Notice tone="warning" title="存在待人工核对的操作">
          {manualReviewOperations.length} 个执行操作需要人工核对；结果尚未确认，不能当作已完成，请查询真实回执或转人工处理。
        </Notice>
      ) : null}

      {missingReasons.length > 0 ? (
        <Notice tone="warning" title="政策证据存在缺资料或限制">
          <ul className="staff-compact-list">
            {missingReasons.map((reason) => (
              <li key={reason}>{missingReasonLabel(reason)}</li>
            ))}
          </ul>
          <p>缺资料或证据冲突时，不应形成政策承诺；请补充资料后重新检索。</p>
        </Notice>
      ) : null}

      {evidenceError ? (
        <Notice tone="danger" title="政策证据读取失败">
          {evidenceError} 摘要中的证据数与下一步判断可能不完整。
        </Notice>
      ) : null}

      <details className="staff-id-details">
        <summary>案件标识与版本</summary>
        <dl className="staff-kv">
          <div><dt>诉求</dt><dd>{detail.request_id}</dd></div>
          <div><dt>订单</dt><dd>{detail.order_id}</dd></div>
          <div><dt>数据版本</dt><dd>r{detail.revision}</dd></div>
          <div><dt>材料修订</dt><dd>{detail.material_revision}</dd></div>
          <div><dt>订单版本</dt><dd>{order?.order_version ?? "未提供"}</dd></div>
          <div><dt>政策版本</dt><dd>{policyVersion ?? "未提供"}</dd></div>
          <div><dt>消费者提交</dt><dd><TimeValue value={detail.request_created_at} /></dd></div>
          <div><dt>远端更新</dt><dd><TimeValue value={detail.request_updated_at} /></dd></div>
          <div><dt>订单查询</dt><dd><TimeValue value={detail.order_queried_at} /></dd></div>
          <div><dt>最近更新</dt><dd><TimeValue value={detail.updated_at} /></dd></div>
          {readAt ? (
            <div><dt>最近成功读取</dt><dd><TimeValue value={readAt} /></dd></div>
          ) : null}
        </dl>
      </details>
    </section>
  );
}

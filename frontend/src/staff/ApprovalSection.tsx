import { useState } from "react";
import { Lock, UserCheck } from "lucide-react";
import {
  APPROVAL_STATUS_LABELS,
  MATERIAL_TYPE_LABELS,
  ORDER_STATUS_LABELS,
  REQUEST_STATUS_LABELS,
  REQUEST_TYPE_LABELS,
  ROLE_LABELS,
  SHIPMENT_STATUS_LABELS,
  TICKET_ACTION_LABELS,
  errorMessage,
  formatMoney,
  isConflict,
  missingReasonLabel,
} from "./format";
import {
  Badge,
  EmptyBlock,
  Notice,
  Panel,
  TimeValue,
  approvalTone,
} from "./StaffBits";
import { useStaff as useStaffContext } from "./context";
import type { ApprovalSnapshotView, CaseDetail } from "./types";

export function ApprovalSection({
  detail,
  onUpdated,
  onReload,
  collapsible = false,
  defaultOpen = true,
}: {
  detail: CaseDetail;
  onUpdated: (detail: CaseDetail) => void;
  onReload: () => void;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  const staff = useStaffContext();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const pending = detail.approvals.filter((approval) => approval.status === "pending");
  const selected = detail.approvals.find((approval) => approval.approval_id === selectedId)
    ?? pending[0]
    ?? detail.approvals[0]
    ?? null;
  const expired = selected ? new Date(selected.expires_at).getTime() <= Date.now() : false;
  const isSelf = selected?.requester_staff_id === staff.identity.staff_id;
  const snapshot = selected?.snapshot ?? null;
  const canDecide = selected !== null &&
    staff.canApprove &&
    selected.status === "pending" &&
    !isSelf &&
    snapshot !== null &&
    detail.state === "waiting_approval" &&
    !expired &&
    busy === null;
  const current = detail.current_draft;
  const differences: string[] = [];
  if (snapshot && current && snapshot.draft.version !== current.version) {
    differences.push(`当前草稿为 v${current.version}，冻结快照为 v${snapshot.draft.version}`);
  }
  if (snapshot && detail.order && snapshot.order.order_version !== detail.order.order_version) {
    differences.push("当前订单版本与冻结快照不同");
  }
  if (snapshot && snapshot.materials.revision !== detail.material_revision) {
    differences.push("当前材料修订与冻结快照不同");
  }
  if (snapshot && current && snapshot.evidence.policy_version !== current.policy_version) {
    differences.push("当前草稿政策版本与冻结快照不同");
  }

  async function approve() {
    if (!selected || !canDecide || busy) return;
    setBusy("approve");
    setError(null);
    setConflict(null);
    setNotice(null);
    const result = await staff.run((api, signal) =>
      api.approve(staff.market, detail.case_id, selected.approval_id, detail.revision, signal),
    );
    if (result.stale) return;
    setBusy(null);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setError(errorMessage(result.error));
      return;
    }
    setNotice("已批准并准备执行。系统会等待 Worker 与真实回执；现在不等于工单已完成。");
    onUpdated(result.value);
  }

  async function reject() {
    if (!selected || !canDecide || busy) return;
    const trimmed = reason.trim();
    if (trimmed.length < 3) {
      setError("内部驳回原因至少需要 3 个字符。");
      return;
    }
    setBusy("reject");
    setError(null);
    setConflict(null);
    setNotice(null);
    const result = await staff.run((api, signal) =>
      api.reject(staff.market, detail.case_id, selected.approval_id, detail.revision, trimmed, signal),
    );
    if (result.stale) return;
    setBusy(null);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setError(errorMessage(result.error));
      return;
    }
    setReason("");
    setNotice("已内部驳回。不会创建工单，也不会通知消费者；草稿退回客服处理。");
    onUpdated(result.value);
  }

  return (
    <Panel
      title="审批与冻结依据"
      subtitle="审批人决策使用送审时冻结的 snapshot，不从当前 case 拼装。"
      actions={<Badge tone="neutral">共 {detail.approvals.length} 次审批 · 待审批 {pending.length} 项</Badge>}
      collapsible={collapsible}
      defaultOpen={defaultOpen}
    >
      {detail.approvals.length === 0 ? (
        <EmptyBlock title="尚无审批">客服提交草稿后，此处会出现待审批项。</EmptyBlock>
      ) : (
        <>
          <div className="staff-approval-tabs" role="listbox" aria-label="选择审批记录">
            {detail.approvals.map((approval) => (
              <button
                key={approval.approval_id}
                type="button"
                role="option"
                aria-selected={selected?.approval_id === approval.approval_id}
                className={selected?.approval_id === approval.approval_id ? "selected" : ""}
                onClick={() => {
                  setSelectedId(approval.approval_id);
                  setError(null);
                  setConflict(null);
                  setNotice(null);
                }}
              >
                <Badge tone={approvalTone(approval.status)}>{APPROVAL_STATUS_LABELS[approval.status]}</Badge>
                <span>申请人 {approval.requester_staff_id}</span>
                <TimeValue value={approval.expires_at} prefix="有效至" />
              </button>
            ))}
          </div>

          {selected ? (
            <>
              <div className="staff-approval-summary">
                <dl className="staff-kv">
                  <div><dt>审批状态</dt><dd>{APPROVAL_STATUS_LABELS[selected.status]}</dd></div>
                  <div><dt>申请人</dt><dd>{selected.requester_staff_id}{isSelf ? "（你）" : ""}</dd></div>
                  <div><dt>提交时间</dt><dd><TimeValue value={selected.created_at} /></dd></div>
                  <div><dt>有效期至</dt><dd><TimeValue value={selected.expires_at} /></dd></div>
                  {selected.decided_by_staff_id ? <div><dt>决策人</dt><dd>{selected.decided_by_staff_id}</dd></div> : null}
                  {selected.decision_reason ? <div><dt>内部原因</dt><dd>{selected.decision_reason}</dd></div> : null}
                </dl>
                {expired && selected.status === "pending" ? <Badge tone="danger">已过期，不能决策</Badge> : null}
              </div>

              {snapshot === null ? (
                <Notice tone="danger" title="冻结审批快照不可用">
                  本页不能用当前 case 拼装审批依据，因此不会显示乐观成功或提供决策按钮。请联系管理员检查数据完整性。
                </Notice>
              ) : (
                <ApprovalSnapshotView snapshot={snapshot} />
              )}

              {differences.length > 0 ? (
                <Notice tone="warning" title="当前业务信息与冻结快照存在差异">
                  <ul className="staff-compact-list">
                    {differences.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                  批准仍以冻结快照为决策对象，并由服务器最终校验；若当前上下文变化，服务器会拒绝并要求重新送审。
                </Notice>
              ) : (
                <p className="staff-muted">当前草稿、订单版本、材料修订与政策版本与冻结快照一致。</p>
              )}

              {conflict ? (
                <Notice tone="warning" title="审批版本冲突，未提交内容已保留">
                  {conflict}
                  <button className="staff-button" type="button" onClick={onReload}>读取最新版本</button>
                </Notice>
              ) : null}
              {error ? <Notice tone="danger" title={error} /> : null}
              {notice ? <Notice tone="success" title={notice} live /> : null}

              {!staff.canApprove ? (
                <p className="staff-readonly-note">当前角色不能批准或驳回，只能查看审批依据。</p>
              ) : isSelf ? (
                <Notice tone="info" title="不能审批自己提交的草稿">
                  该审批需要由其他审批人处理；服务器也会拒绝自批。
                </Notice>
              ) : selected.status !== "pending" ? (
                <p className="staff-readonly-note">该审批已处理或已失效，不能再次决策。</p>
              ) : detail.state !== "waiting_approval" ? (
                <p className="staff-readonly-note">当前诉求状态不是待审批，不能决策。</p>
              ) : expired ? (
                <p className="staff-readonly-note">审批已过期，需要客服重新送审。</p>
              ) : snapshot === null ? (
                <p className="staff-readonly-note">缺少冻结快照，决策按钮已禁用。</p>
              ) : (
                <div className="staff-decision">
                  <div className="staff-decision-approve">
                    <button className="staff-button staff-button-primary" type="button" onClick={() => void approve()} disabled={!canDecide}>
                      <UserCheck size={15} aria-hidden="true" />{busy === "approve" ? "批准中…" : "批准并准备执行"}
                    </button>
                    <small>批准表示允许系统按冻结版本执行，不表示工单已创建或已完成。</small>
                  </div>
                  <div className="staff-decision-reject">
                    <label className="staff-field">
                      <span className="staff-field-label">内部驳回原因（必填）</span>
                      <textarea
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                        rows={2}
                        maxLength={2000}
                        placeholder="面向内部说明返工原因"
                      />
                    </label>
                    <button className="staff-button staff-button-danger" type="button" onClick={() => void reject()} disabled={!canDecide}>
                      <Lock size={15} aria-hidden="true" />{busy === "reject" ? "驳回中…" : "内部驳回"}
                    </button>
                    <small>内部驳回不会创建工单，也不会通知消费者。若要正式拒绝消费者，应批准 action 为 reject 的草稿。</small>
                  </div>
                </div>
              )}
            </>
          ) : null}
        </>
      )}
    </Panel>
  );
}

function ApprovalSnapshotView({ snapshot }: { snapshot: ApprovalSnapshotView }) {
  return (
    <div className="staff-snapshot">
      <div className="staff-snapshot-banner">
        <Lock size={15} aria-hidden="true" />
        <span>以下内容来自送审时保存的不可变快照；不会随当前 case 的后续修改而变化。</span>
      </div>
      <div className="staff-snapshot-grid">
        <section>
          <h4>诉求</h4>
          <dl className="staff-kv">
            <div><dt>诉求</dt><dd>{snapshot.case.request_id}</dd></div>
            <div><dt>订单</dt><dd>{snapshot.case.order_id}</dd></div>
            <div><dt>市场</dt><dd>{snapshot.case.market.toUpperCase()}</dd></div>
            <div><dt>类型</dt><dd>{REQUEST_TYPE_LABELS[snapshot.case.request_type]}</dd></div>
            <div><dt>E-pig 状态</dt><dd>{REQUEST_STATUS_LABELS[snapshot.request.status]}</dd></div>
            <div><dt>远端工单</dt><dd>{snapshot.request.ticket_id ?? "尚未提供"}</dd></div>
            <div><dt>请求更新</dt><dd><TimeValue value={snapshot.request.updated_at} /></dd></div>
            <div><dt>附件数</dt><dd>{snapshot.request.attachments.length}</dd></div>
          </dl>
        </section>
        <section>
          <h4>身份与期限</h4>
          <dl className="staff-kv">
            <div><dt>申请人</dt><dd>{snapshot.requester.staff_id}</dd></div>
            <div>
              <dt>申请角色</dt>
              <dd>
                {snapshot.requester.roles
                  .map((role) => ROLE_LABELS[role as keyof typeof ROLE_LABELS] ?? role)
                  .join(" / ") || "未提供"}
              </dd>
            </div>
            <div><dt>允许市场</dt><dd>{snapshot.requester.allowed_markets.join(" / ") || "未提供"}</dd></div>
            <div><dt>送审时间</dt><dd><TimeValue value={snapshot.approval.requested_at} /></dd></div>
            <div><dt>有效期至</dt><dd><TimeValue value={snapshot.approval.expires_at} /></dd></div>
          </dl>
        </section>
      </div>

      <section className="staff-snapshot-block">
        <h4>冻结草稿 v{snapshot.draft.version}</h4>
        <div className="staff-snapshot-draft">
          <Badge tone="info">{TICKET_ACTION_LABELS[snapshot.draft.action]}</Badge>
          <p><strong>内部摘要：</strong>{snapshot.draft.internal_summary}</p>
          <blockquote><strong>消费者最终回复：</strong>{snapshot.draft.consumer_reply}</blockquote>
        </div>
      </section>

      <section className="staff-snapshot-block">
        <h4>订单快照</h4>
        <dl className="staff-kv">
          <div><dt>订单</dt><dd>{snapshot.order.order_id}</dd></div>
          <div><dt>订单版本</dt><dd>{snapshot.order.order_version}</dd></div>
          <div><dt>状态</dt><dd>{ORDER_STATUS_LABELS[snapshot.order.status]}</dd></div>
          <div><dt>总额</dt><dd>{formatMoney(snapshot.order.total_minor, snapshot.order.currency)}</dd></div>
        </dl>
        <ul className="staff-compact-list">
          {snapshot.order.items.map((item) => (
            <li key={item.order_item_id}>{item.name} · {item.sku} × {item.quantity} · {formatMoney(item.line_total_minor, snapshot.order.currency)}</li>
          ))}
        </ul>
        {snapshot.shipment ? (
          <p className="staff-muted">
            物流快照：{snapshot.shipment.carrier} · {SHIPMENT_STATUS_LABELS[snapshot.shipment.status]} · {snapshot.shipment.events.length} 个事件
          </p>
        ) : (
          <p className="staff-muted">物流快照：未提供</p>
        )}
        {snapshot.delivery_proof ? (
          <p className="staff-muted">
            交付证明：{snapshot.delivery_proof.kind === "shipment" ? "物流交付" : "人工核验"}
            {snapshot.delivery_proof.delivered_at ? <> · <TimeValue value={snapshot.delivery_proof.delivered_at} /></> : null}
            {snapshot.delivery_proof.applies_to_sku ? ` · SKU ${snapshot.delivery_proof.applies_to_sku}` : ""}
          </p>
        ) : <p className="staff-muted">交付证明：未提供</p>}
      </section>

      <section className="staff-snapshot-block">
        <h4>冻结材料（修订 {snapshot.materials.revision}）</h4>
        {snapshot.materials.items.length === 0 ? <p className="staff-muted">送审时没有内部补充材料。</p> : (
          <ul className="staff-compact-list">
            {snapshot.materials.items.map((item) => (
              <li key={item.material_id}>
                <strong>{MATERIAL_TYPE_LABELS[item.material_type as keyof typeof MATERIAL_TYPE_LABELS] ?? "内部材料"}</strong> · 来源 {item.source}
                {item.applies_to_sku ? ` · SKU ${item.applies_to_sku}` : ""}
                {item.delivered_at ? <> · 签收 <TimeValue value={item.delivered_at} /></> : null}
                <span className="staff-snapshot-material">{item.content}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="staff-snapshot-block">
        <h4>
          冻结政策证据（版本 {snapshot.evidence.policy_version ?? "未提供"}
          {snapshot.evidence.as_of ? ` · 政策日期 ${snapshot.evidence.as_of}` : ""}）
        </h4>
        {snapshot.evidence.chunks.length === 0 ? <p className="staff-muted">快照中没有政策证据。</p> : (
          <ol className="staff-evidence-list staff-evidence-compact">
            {snapshot.evidence.chunks.map((chunk) => (
              <li key={chunk.chunk_id}>
                <div className="staff-evidence-head">
                  <strong>{chunk.title}</strong>
                  {chunk.is_demo ? <Badge tone="warning">演示政策</Badge> : null}
                  <Badge tone="neutral">版本 {chunk.policy_version}</Badge>
                </div>
                {chunk.section ? <p className="staff-evidence-section">{chunk.section}</p> : null}
                <blockquote>{chunk.content}</blockquote>
                <small>来源：{chunk.source_label}</small>
              </li>
            ))}
          </ol>
        )}
        {snapshot.trusted_categories.length > 0 ? (
          <p className="staff-muted">
            商品范围：{snapshot.trusted_categories.map((item) => `${item.sku}→${item.category}`).join("；")}
          </p>
        ) : null}
      </section>
      {snapshot.evidence.chunks.length === 0 && snapshot.evidence.policy_version === null ? (
        <Notice tone="warning" title="冻结快照缺少政策版本">
          {missingReasonLabel("missing_policy_version")}
        </Notice>
      ) : null}
    </div>
  );
}

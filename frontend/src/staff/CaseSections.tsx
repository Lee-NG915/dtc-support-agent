import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Download,
  ExternalLink,
  FileImage,
  Package,
  RefreshCw,
  ShieldCheck,
  Truck,
} from "lucide-react";
import { StaffApiError } from "./api";
import { useStaff } from "./context";
import {
  APPROVAL_STATUS_LABELS,
  CASE_STATE_LABELS,
  DRAFT_STATUS_LABELS,
  MATERIAL_TYPE_LABELS,
  OPERATION_STATUS_LABELS,
  ORDER_STATUS_LABELS,
  RECONCILIATION_LABELS,
  SHIPMENT_STATUS_LABELS,
  errorMessage,
  eventLabel,
  formatBytes,
  formatMoney,
  isConflict,
  missingReasonLabel,
  policyStatusLabel,
  reasonLabel,
} from "./format";
import {
  Badge,
  EmptyBlock,
  LoadingBlock,
  Notice,
  Panel,
  TimeValue,
  operationTone,
} from "./StaffBits";
import { useAttachmentUrls } from "./useAttachments";
import type {
  CaseDetail,
  CaseEvidenceResponse,
  EventView,
  MaterialType,
  OperationView,
  ServiceOrder,
  ServiceShipment,
} from "./types";

const TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

const OPERATION_ERROR_LABELS: Record<string, string> = {
  VALIDATION_ERROR: "请求参数未通过校验",
  UNSUPPORTED_CONTRACT_VERSION: "契约版本不受支持",
  UNAUTHORIZED: "上游认证失败",
  FORBIDDEN_MARKET: "上游拒绝该市场",
  NOT_FOUND: "上游未找到对象",
  IDEMPOTENCY_CONFLICT: "幂等键冲突",
  REQUEST_STATE_CONFLICT: "E-pig 状态冲突",
  ORDER_VERSION_CONFLICT: "订单版本冲突",
  ATTACHMENT_EXPIRED: "附件已过期",
  ATTACHMENT_TOO_LARGE: "附件过大",
  UNSUPPORTED_MEDIA_TYPE: "附件类型不支持",
  RATE_LIMITED: "上游限流",
  INTERNAL_ERROR: "上游内部错误",
  UPSTREAM_UNAVAILABLE: "上游暂不可用",
  RECEIPT_NOT_VISIBLE: "暂未读到回执",
  RETRY_LIMIT: "达到有限重试上限",
};

function operationErrorLabel(code: string | null): string | null {
  if (!code) return null;
  return OPERATION_ERROR_LABELS[code] ?? "执行记录中有可查看的错误状态";
}

function humanState(value: string | null): string {
  if (!value) return "—";
  const known: Record<string, string> = {
    ...CASE_STATE_LABELS,
    ...DRAFT_STATUS_LABELS,
    ...APPROVAL_STATUS_LABELS,
    ...OPERATION_STATUS_LABELS,
  };
  return known[value] ?? "状态已更新";
}

export function AttachmentsSection({ detail }: { detail: CaseDetail }) {
  const staff = useStaff();
  const { previews, load, revokeAll } = useAttachmentUrls(detail.case_id);
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div className="staff-subsection">
      <div className="staff-subhead">
        <h4><FileImage size={15} aria-hidden="true" />消费者附件</h4>
        <p>经员工身份授权下载；预览 URL 仅在当前页面内存，切换诉求或退出立即撤销。</p>
      </div>
      {detail.attachments.length === 0 ? (
        <EmptyBlock title="该诉求没有附件" />
      ) : (
        <ul className="staff-attachment-list">
          {detail.attachments.map((attachment) => {
            const preview = previews[attachment.attachment_id];
            const expired = attachment.status === "expired" || new Date(attachment.expires_at).getTime() <= Date.now();
            return (
              <li key={attachment.attachment_id}>
                <div className="staff-attachment-row">
                  <div>
                    <strong>{attachment.file_name}</strong>
                    <small>
                      {attachment.media_type} · {formatBytes(attachment.size_bytes)} · 到期{" "}
                      <TimeValue value={attachment.expires_at} />
                    </small>
                  </div>
                  <div className="staff-inline-actions">
                    {expired ? <Badge tone="danger">已过期</Badge> : <Badge tone="success">可读取</Badge>}
                    <button
                      className="staff-button"
                      type="button"
                      disabled={expired || preview?.state === "loading"}
                      onClick={() => {
                        setExpanded(attachment.attachment_id);
                        void load(attachment.attachment_id);
                      }}
                    >
                      <ExternalLink size={14} aria-hidden="true" />
                      {preview?.state === "loading" ? "读取中…" : preview?.state === "ready" ? "重新读取" : "预览"}
                    </button>
                  </div>
                </div>
                {preview?.state === "error" ? (
                  <Notice tone="danger" title="附件预览失败">
                    {preview.message} 这不表示文件不存在；请根据错误中的状态判断权限或过期问题。
                  </Notice>
                ) : null}
                {preview?.state === "ready" && preview.url && expanded === attachment.attachment_id ? (
                  <div className="staff-attachment-preview">
                    {attachment.media_type.startsWith("image/") ? (
                      <img src={preview.url} alt={`附件 ${attachment.file_name}`} />
                    ) : null}
                    <a className="staff-button" href={preview.url} download={attachment.file_name}>
                      <Download size={14} aria-hidden="true" />下载当前预览
                    </a>
                    <button className="staff-button staff-button-quiet" type="button" onClick={() => { revokeAll(); setExpanded(null); }}>
                      撤销全部预览
                    </button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {!staff.canSupport && staff.canAudit ? (
        <p className="staff-readonly-note">审计角色只读；附件读取仍受员工身份和市场权限约束。</p>
      ) : null}
    </div>
  );
}

export function MaterialsSection({
  detail,
  onUpdated,
  onReload,
}: {
  detail: CaseDetail;
  onUpdated: (detail: CaseDetail) => void;
  onReload: () => void;
}) {
  const staff = useStaff();
  const [materialType, setMaterialType] = useState<MaterialType>("internal_note");
  const [source, setSource] = useState("");
  const [content, setContent] = useState("");
  const [sku, setSku] = useState("");
  const [deliveredAt, setDeliveredAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const skus = useMemo(() => {
    const unique = new Set<string>();
    for (const item of detail.order?.items ?? []) unique.add(item.sku);
    return [...unique];
  }, [detail.order]);
  const structuredType = materialType === "manual_verification" || materialType === "delivery_verification";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setConflict(null);
    setNotice(null);
    const trimmedSource = source.trim();
    const trimmedContent = content.trim();
    if (trimmedSource.length < 3) {
      setError("材料来源至少需要 3 个字符。");
      return;
    }
    if (!trimmedContent) {
      setError("材料内容不能为空。");
      return;
    }
    let deliveredIso: string | undefined;
    if (structuredType) {
      if (materialType === "delivery_verification" && (!sku || !deliveredAt)) {
        setError("签收核验材料必须选择订单 SKU，并填写带时区的签收时间。");
        return;
      }
      if (materialType === "manual_verification" && Boolean(sku) !== Boolean(deliveredAt)) {
        setError("人工核验的结构化字段需要 SKU 和签收时间同时填写，或同时留空。");
        return;
      }
      if (deliveredAt) {
        const parsed = new Date(deliveredAt);
        if (Number.isNaN(parsed.getTime())) {
          setError("签收时间格式不可用。");
          return;
        }
        if (parsed.getTime() > Date.now()) {
          setError("签收时间不能晚于当前时间。");
          return;
        }
        deliveredIso = parsed.toISOString();
      }
    }
    setBusy(true);
    const result = await staff.run((api, signal) =>
      api.addMaterial(staff.market, detail.case_id, {
        expected_revision: detail.revision,
        material_type: materialType,
        source: trimmedSource,
        content: trimmedContent,
        ...(structuredType && sku ? { applies_to_sku: sku } : {}),
        ...(deliveredIso ? { delivered_at: deliveredIso } : {}),
      }, signal),
    );
    if (result.stale) return;
    setBusy(false);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setError(errorMessage(result.error));
      return;
    }
    onUpdated(result.value);
    setSource("");
    setContent("");
    setSku("");
    setDeliveredAt("");
    setNotice("内部材料已保存。保存材料不会自动送审；如已有待审批版本，服务端会按规则使其失效。");
  }

  return (
    <div className="staff-subsection">
      <div className="staff-subhead">
        <h4><ShieldCheck size={15} aria-hidden="true" />内部补充材料</h4>
        <p>内部资料不会混入消费者回复；来源、录入员工和时间均会保留。</p>
      </div>
      {detail.materials.length === 0 ? (
        <EmptyBlock title="尚无内部材料" />
      ) : (
        <ul className="staff-material-list">
          {detail.materials.map((material) => (
            <li key={material.material_id}>
              <div className="staff-material-head">
                <Badge tone={material.material_type === "internal_note" ? "neutral" : "info"}>
                  {MATERIAL_TYPE_LABELS[material.material_type]}
                </Badge>
                <span>来源：{material.source}</span>
                <TimeValue value={material.created_at} prefix="录入" />
              </div>
              <p>{material.content}</p>
              <small>
                录入员工 {material.created_by_staff_id}
                {material.applies_to_sku ? ` · SKU ${material.applies_to_sku}` : ""}
                {material.delivered_at ? <> · 签收时间 <TimeValue value={material.delivered_at} /></> : null}
              </small>
            </li>
          ))}
        </ul>
      )}

      {staff.canSupport && !["completed", "executing", "reconciliation_required"].includes(detail.state) ? (
        <form className="staff-form" onSubmit={(event) => void submit(event)}>
          {detail.operations.some((operation) => operation.status === "prepared") ? (
            <Notice tone="warning" title="已有已批准并准备执行的操作">
              保存材料会使该准备执行失效并需要重新审批；已发出的外部调用不会因此撤回。
            </Notice>
          ) : null}
          <div className="staff-form-row">
            <label className="staff-field">
              <span className="staff-field-label">材料类型</span>
              <select value={materialType} onChange={(event) => setMaterialType(event.target.value as MaterialType)}>
                {(Object.keys(MATERIAL_TYPE_LABELS) as MaterialType[]).map((type) => (
                  <option value={type} key={type}>{MATERIAL_TYPE_LABELS[type]}</option>
                ))}
              </select>
            </label>
            <label className="staff-field">
              <span className="staff-field-label">来源（必填）</span>
              <input value={source} onChange={(event) => setSource(event.target.value)} maxLength={500} placeholder="例如：承运商电话核验、工单系统记录" />
            </label>
          </div>
          <label className="staff-field">
            <span className="staff-field-label">材料内容</span>
            <textarea value={content} onChange={(event) => setContent(event.target.value)} rows={3} maxLength={5000} />
          </label>
          {structuredType ? (
            <div className="staff-form-row">
              <label className="staff-field">
                <span className="staff-field-label">
                  订单 SKU{materialType === "delivery_verification" ? "（必填）" : "（可选）"}
                </span>
                <select value={sku} onChange={(event) => setSku(event.target.value)}>
                  <option value="">不关联 SKU</option>
                  {skus.map((item) => <option value={item} key={item}>{item}</option>)}
                </select>
              </label>
              <label className="staff-field">
                <span className="staff-field-label">
                  签收核验时间{materialType === "delivery_verification" ? "（必填）" : "（可选）"}
                </span>
                <input type="datetime-local" value={deliveredAt} onChange={(event) => setDeliveredAt(event.target.value)} />
                <span className="staff-field-hint">按当前时区（{TIMEZONE}）录入，提交时转换为带时区时间。</span>
              </label>
            </div>
          ) : null}
          {conflict ? (
            <Notice tone="warning" title="保存冲突，输入已保留">
              {conflict}
              <button className="staff-button" type="button" onClick={onReload}>保留输入并读取最新版本</button>
            </Notice>
          ) : null}
          {error ? <Notice tone="danger" title={error} /> : null}
          {notice ? <Notice tone="success" title={notice} live /> : null}
          <button className="staff-button staff-button-primary" type="submit" disabled={busy}>
            {busy ? "保存中…" : "保存内部材料"}
          </button>
        </form>
      ) : staff.canSupport ? (
        <p className="staff-readonly-note">
          当前诉求处于执行中、结果待核对或已完成状态，不能保存新材料；请先等待真实回执核验或转人工处理。
        </p>
      ) : null}
    </div>
  );
}

export function OrderSection({
  detail,
  collapsible = false,
  defaultOpen = true,
}: {
  detail: CaseDetail;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  const staff = useStaff();
  const [order, setOrder] = useState<ServiceOrder | null>(detail.order);
  const [queriedAt, setQueriedAt] = useState<string | null>(detail.order_queried_at);
  const [orderBusy, setOrderBusy] = useState(false);
  const [orderError, setOrderError] = useState<string | null>(null);
  const [orderStale, setOrderStale] = useState(false);
  const [shipment, setShipment] = useState<ServiceShipment | null>(null);
  const [shipmentState, setShipmentState] = useState<"loading" | "ready" | "not_provided" | "error">("loading");
  const [shipmentMessage, setShipmentMessage] = useState<string | null>(null);
  const [shipmentAt, setShipmentAt] = useState<string | null>(null);
  const shipmentSeq = useRef(0);

  const loadShipment = useCallback(async () => {
    const sequence = ++shipmentSeq.current;
    setShipmentState("loading");
    setShipmentMessage(null);
    const result = await staff.run((api, signal) =>
      api.getShipment(staff.market, detail.order_id, signal),
    );
    if (sequence !== shipmentSeq.current) return;
    if (result.stale) return;
    if (result.ok) {
      setShipment(result.value);
      setShipmentAt(new Date().toISOString());
      setShipmentState("ready");
      return;
    }
    if (result.error instanceof StaffApiError && result.error.status === 404) {
      setShipment(null);
      setShipmentState("not_provided");
      setShipmentMessage("服务端未返回该订单的物流单；不能据此推断已签收。");
      return;
    }
    setShipmentState("error");
    setShipmentMessage(errorMessage(result.error));
  }, [detail.order_id, staff]);

  useEffect(() => {
    void loadShipment();
  }, [loadShipment]);

  async function refreshOrder() {
    if (orderBusy) return;
    setOrderBusy(true);
    setOrderError(null);
    const result = await staff.run((api, signal) =>
      api.getOrder(staff.market, detail.order_id, signal),
    );
    if (result.stale) return;
    setOrderBusy(false);
    if (!result.ok) {
      setOrderError(errorMessage(result.error));
      if (order) setOrderStale(true);
      return;
    }
    setOrder(result.value);
    setQueriedAt(new Date().toISOString());
    setOrderStale(false);
  }

  return (
    <Panel
      title="订单与物流"
      subtitle={(
        <>
          {order
            ? `订单 ${order.order_id} · ${formatMoney(order.total_minor, order.currency)}`
            : "订单快照未提供"}
          {orderError ? ` · 订单刷新失败${orderStale ? "（旧快照）" : ""}` : ""}
          {" · 物流读取：展开后查询"}
          {order ? <> · <TimeValue value={queriedAt} prefix="订单查询" /></> : null}
        </>
      )}
      actions={
        <button className="staff-button" type="button" onClick={() => void refreshOrder()} disabled={orderBusy}>
          <RefreshCw size={14} aria-hidden="true" />{orderBusy ? "读取中…" : "重新读取订单"}
        </button>
      }
      collapsible={collapsible}
      defaultOpen={defaultOpen}
    >
      {orderError ? (
        <Notice tone={orderStale ? "warning" : "danger"} title={orderStale ? "订单刷新失败，以下为旧快照" : "订单读取失败"}>
          {orderError}
        </Notice>
      ) : null}
      {order === null ? (
        <EmptyBlock title="未提供订单快照">
          订单可能暂不可读取。页面不会编造订单状态或签收信息。
        </EmptyBlock>
      ) : (
        <div className="staff-order">
          <div className="staff-order-head">
            <div>
              <h4><Package size={15} aria-hidden="true" />订单 {order.order_id}</h4>
              <small>
                市场 {order.market.toUpperCase()} · 订单版本 {order.order_version} · 币种 {order.currency}
              </small>
            </div>
            <Badge tone={order.status === "cancelled" ? "danger" : "info"}>
              {ORDER_STATUS_LABELS[order.status]}
            </Badge>
          </div>
          {orderStale ? <Notice tone="warning" title="订单快照已标为陈旧">重新读取失败，请勿把旧快照当作最新订单状态。</Notice> : null}
          <div className="staff-table-wrap">
            <table className="staff-table">
              <thead>
                <tr>
                  <th scope="col">商品</th>
                  <th scope="col">SKU</th>
                  <th scope="col">数量</th>
                  <th scope="col">行金额</th>
                </tr>
              </thead>
              <tbody>
                {order.items.map((item) => (
                  <tr key={item.order_item_id}>
                    <td>{item.name}</td>
                    <td>{item.sku}</td>
                    <td>{item.quantity}</td>
                    <td>{formatMoney(item.line_total_minor, order.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <dl className="staff-kv staff-kv-money">
            <div><dt>商品小计</dt><dd>{formatMoney(order.subtotal_minor, order.currency)}</dd></div>
            <div><dt>运费</dt><dd>{formatMoney(order.shipping_minor, order.currency)}</dd></div>
            <div><dt>税费</dt><dd>{formatMoney(order.tax_minor, order.currency)}</dd></div>
            <div><dt>优惠</dt><dd>{formatMoney(order.discount_minor, order.currency)}</dd></div>
            <div><dt>订单总额</dt><dd><strong>{formatMoney(order.total_minor, order.currency)}</strong></dd></div>
          </dl>
          <p className="staff-recipient">
            收件摘要：{order.recipient_summary.masked_name} · {order.recipient_summary.country_code} /{" "}
            {order.recipient_summary.region} / {order.recipient_summary.postal_code_masked}
          </p>
        </div>
      )}

      <div className="staff-subsection">
        <div className="staff-subhead">
          <h4><Truck size={15} aria-hidden="true" />物流</h4>
          <p>物流查询独立于订单快照；没有记录时明确显示未提供。</p>
        </div>
        {shipmentState === "loading" ? <LoadingBlock label="正在读取物流信息…" /> : null}
        {shipmentState === "not_provided" ? (
          <EmptyBlock title="物流信息未提供" action={<button className="staff-button" type="button" onClick={() => void loadShipment()}>重新读取物流</button>}>
            {shipmentMessage}
          </EmptyBlock>
        ) : null}
        {shipmentState === "error" ? (
          <Notice tone={shipment ? "warning" : "danger"} title={shipment ? "物流刷新失败，以下为上次读取结果" : "物流读取失败"} action={<button className="staff-button" type="button" onClick={() => void loadShipment()}>手动重试</button>}>
            {shipmentMessage}
          </Notice>
        ) : null}
        {shipment ? (
          <div className={`staff-shipment ${shipmentState === "error" ? "stale" : ""}`}>
            <div className="staff-shipment-head">
              <strong>{shipment.carrier} · {shipment.shipment_id}</strong>
              <Badge tone={shipment.status === "exception" ? "danger" : shipment.status === "delivered" ? "success" : "info"}>
                {SHIPMENT_STATUS_LABELS[shipment.status]}
              </Badge>
              {shipmentState === "error" ? <Badge tone="warning">陈旧结果</Badge> : null}
              <TimeValue value={shipmentAt} prefix="物流查询时间" />
            </div>
            {shipment.events.length === 0 ? (
              <p className="staff-muted">物流单存在，但没有事件记录；不推断签收。</p>
            ) : (
              <ul className="staff-shipment-events">
                {shipment.events.map((event) => (
                  <li key={event.event_id}>
                    <Badge tone={event.status === "delivered" ? "success" : event.status === "exception" ? "danger" : "info"}>
                      {SHIPMENT_STATUS_LABELS[event.status]}
                    </Badge>
                    <span>{event.description}</span>
                    <TimeValue value={event.occurred_at} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

export function EvidenceSection({
  evidence,
  loading,
  error,
  onReload,
  collapsible = false,
  defaultOpen = true,
}: {
  evidence: CaseEvidenceResponse | null;
  loading: boolean;
  error: string | null;
  onReload: () => void;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  const summary = loading
    ? "正在读取政策证据…"
    : error
      ? "政策证据读取失败"
      : evidence
        ? `可用 ${evidence.chunks.length} 条 · 版本 ${evidence.policy_version ?? "未提供"} · ${evidence.missing_reasons.length > 0 ? `缺资料 ${evidence.missing_reasons.length} 项` : "无缺资料"}`
        : "服务端尚未返回证据";
  return (
    <Panel
      title="政策证据"
      subtitle={(
        <>
          {summary}
          {evidence ? <> · <TimeValue value={evidence.queried_at} prefix="查询" /></> : null}
        </>
      )}
      actions={
        <button className="staff-button staff-button-quiet" type="button" onClick={onReload} disabled={loading}>
          <RefreshCw size={14} aria-hidden="true" />重新检索
        </button>
      }
      collapsible={collapsible}
      defaultOpen={defaultOpen}
    >
      {loading ? <LoadingBlock label="正在读取该诉求的真实政策证据…" /> : null}
      {error ? (
        <Notice tone="danger" title="政策证据读取失败" action={<button className="staff-button" type="button" onClick={onReload}>重试</button>}>
          {error}
        </Notice>
      ) : null}
      {evidence ? (
        <>
          <dl className="staff-kv">
            <div><dt>政策日期</dt><dd>{evidence.as_of}</dd></div>
            <div><dt>政策版本</dt><dd>{evidence.policy_version ?? "未提供"}</dd></div>
            <div><dt>语料修订</dt><dd>{evidence.corpus_revision}</dd></div>
          </dl>
          {evidence.missing_reasons.length > 0 ? (
            <Notice tone="warning" title="存在缺资料或证据限制">
              <ul className="staff-compact-list">
                {evidence.missing_reasons.map((reason) => <li key={reason}>{missingReasonLabel(reason)}</li>)}
              </ul>
              <p>无证据或证据冲突时，不应依赖 AI 或人工草稿形成政策承诺；请人工补充资料后重新检索。</p>
            </Notice>
          ) : null}
          {evidence.trusted_categories.length > 0 ? (
            <div className="staff-trusted-categories">
              <span>可信商品范围：</span>
              {evidence.trusted_categories.map((item) => (
                <Badge tone="info" key={`${item.sku}:${item.category}`}>{item.sku} → {item.category}</Badge>
              ))}
            </div>
          ) : null}
          {evidence.chunks.length === 0 ? (
            <EmptyBlock title="没有可用政策原文">
              服务端未返回匹配证据。不要据此编造政策条款或送审。
            </EmptyBlock>
          ) : (
            <ol className="staff-evidence-list">
              {evidence.chunks.map((chunk) => (
                <li key={chunk.chunk_id}>
                  <div className="staff-evidence-head">
                    <strong>{chunk.title}</strong>
                    {chunk.is_demo ? <Badge tone="warning">演示政策</Badge> : <Badge tone="success">非演示</Badge>}
                    <Badge tone="neutral">版本 {chunk.policy_version}</Badge>
                  </div>
                  {chunk.section ? <p className="staff-evidence-section">{chunk.section}</p> : null}
                  <blockquote>{chunk.content}</blockquote>
                  <small>
                    来源：{chunk.source_label} · {chunk.source_type} · 政策状态：{policyStatusLabel(chunk.policy_status)}
                    {chunk.effective_from ? ` · 生效 ${chunk.effective_from}` : ""}
                    {chunk.effective_to ? ` · 失效 ${chunk.effective_to}` : ""}
                  </small>
                  <small className="staff-source-path">{chunk.source_path}</small>
                </li>
              ))}
            </ol>
          )}
        </>
      ) : null}
    </Panel>
  );
}

export function OperationsSection({
  detail,
  collapsible = false,
  defaultOpen = true,
}: {
  detail: CaseDetail;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  return (
    <Panel
      title="执行与操作状态"
      subtitle="批准不等于工单完成；只有真实回执核验后，服务端才会显示回执成功。"
      collapsible={collapsible}
      defaultOpen={defaultOpen}
    >
      {detail.operations.length === 0 ? (
        <EmptyBlock title="尚无执行操作">
          草稿必须经人工送审并由其他审批人批准后，系统才会准备执行。
        </EmptyBlock>
      ) : (
        <ul className="staff-operation-list">
          {detail.operations.map((operation) => (
            <OperationCard operation={operation} key={operation.operation_id} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function OperationCard({ operation }: { operation: OperationView }) {
  return (
    <li>
      <div className="staff-operation-head">
        <Badge tone={operationTone(operation.status)}>{OPERATION_STATUS_LABELS[operation.status]}</Badge>
        {operation.reconciliation_state ? (
          <Badge tone="warning">
            {RECONCILIATION_LABELS[operation.reconciliation_state as keyof typeof RECONCILIATION_LABELS] ?? "对账状态待确认"}
          </Badge>
        ) : null}
        <TimeValue value={operation.updated_at} prefix="更新" />
      </div>
      <dl className="staff-kv">
        <div><dt>尝试次数</dt><dd>{operation.attempt}</dd></div>
        <div><dt>查询次数</dt><dd>{operation.lookup_count}</dd></div>
        <div><dt>派遣时间</dt><dd><TimeValue value={operation.dispatched_at} /></dd></div>
        <div><dt>下次尝试</dt><dd><TimeValue value={operation.next_attempt_at} /></dd></div>
        {operation.last_error_code ? (
          <div><dt>错误状态</dt><dd>{operationErrorLabel(operation.last_error_code)}</dd></div>
        ) : null}
      </dl>
      {operation.status === "prepared" && operation.dispatched_at === null ? (
        <p className="staff-muted">已批准并准备执行，等待 Worker 领取；尚未创建工单。</p>
      ) : null}
      {operation.status === "succeeded" ? (
        <p className="staff-success-line"><ShieldCheck size={14} aria-hidden="true" />服务端已核验真实回执。仍以远端工单内容为准。</p>
      ) : null}
      {operation.status === "failed" ? (
        <p className="staff-muted">服务端记录为未产生外部效果；如需继续处理，必须重新形成草稿和审批。</p>
      ) : null}
    </li>
  );
}

export function TimelineSection({ events }: { events: EventView[] }) {
  return (
    <ol className="staff-timeline">
      {events.map((event) => (
        <li key={event.seq}>
          <span className="staff-timeline-dot" aria-hidden="true" />
          <div className="staff-timeline-body">
            <div className="staff-timeline-head">
              <strong>{eventLabel(event.event_type)}</strong>
              <TimeValue value={event.created_at} />
            </div>
            <p>
              {humanState(event.old_state)} → {humanState(event.new_state)}
              {event.actor_staff_id ? ` · 员工 ${event.actor_staff_id}` : " · 系统"}
            </p>
            {reasonLabel(event.reason) ? <small>{reasonLabel(event.reason)}</small> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

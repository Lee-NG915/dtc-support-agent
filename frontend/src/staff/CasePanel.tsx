import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useStaff } from "./context";
import {
  CASE_STATE_LABELS,
  REQUEST_STATUS_LABELS,
  REQUEST_TYPE_LABELS,
  errorMessage,
  formatRelative,
} from "./format";
import { sleep } from "./guard";
import {
  Badge,
  EmptyBlock,
  LoadingBlock,
  Notice,
  Panel,
  TimeValue,
  caseTone,
} from "./StaffBits";
import { ApprovalSection } from "./ApprovalSection";
import {
  AttachmentsSection,
  EvidenceSection,
  MaterialsSection,
  OperationsSection,
  OrderSection,
  TimelineSection,
} from "./CaseSections";
import { DraftSection } from "./DraftSection";
import { TicketSection } from "./TicketSection";
import type { CaseDetail, CaseEvidenceResponse, EventView } from "./types";

function activeOperation(detail: CaseDetail): boolean {
  return detail.operations.some((operation) =>
    operation.status === "dispatching" ||
    operation.status === "awaiting_receipt" ||
    operation.status === "retry_wait" ||
    operation.status === "reconciliation_required",
  );
}

export function CasePanel({ caseId }: { caseId: number }) {
  const staff = useStaff();
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [events, setEvents] = useState<EventView[]>([]);
  const [eventsHasMore, setEventsHasMore] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [eventsBusy, setEventsBusy] = useState(false);
  const [evidence, setEvidence] = useState<CaseEvidenceResponse | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(true);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<number | null>(null);
  const detailSeq = useRef(0);
  const eventSeq = useRef(0);
  const evidenceSeq = useRef(0);
  const detailRef = useRef<CaseDetail | null>(null);
  detailRef.current = detail;
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      // A delayed retry must stop at the unmount boundary even if the guard
      // generation has not changed yet.
      aliveRef.current = false;
    };
  }, []);

  const loadDetail = useCallback(async (initial: boolean, quiet = false) => {
    const sequence = ++detailSeq.current;
    const generation = staff.generation;
    if (initial) setLoading(true);
    else if (!quiet) setRefreshing(true);
    if (initial) setError(null);
    setRefreshError(null);
    setRetrying(null);
    let lastError: unknown = null;
    const attempts = quiet ? 1 : 3;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const result = await staff.run((api, signal) => api.getCase(staff.market, caseId, signal));
      if (sequence !== detailSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      if (result.ok) {
        setDetail(result.value);
        setLoading(false);
        setRefreshing(false);
        setRetrying(null);
        setError(null);
        setRefreshError(null);
        const readTime = new Date().toISOString();
        setReadAt(readTime);
        staff.markRead(readTime);
        return;
      }
      if (result.stale) return;
      lastError = result.error;
      if (attempt + 1 < attempts) {
        setRetrying(attempt + 1);
        await sleep(700 * (attempt + 1));
        if (sequence !== detailSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      }
    }
    setLoading(false);
    setRefreshing(false);
    setRetrying(null);
    if (initial && detailRef.current === null) setError(errorMessage(lastError));
    else setRefreshError(errorMessage(lastError));
  }, [caseId, staff]);

  const loadEvents = useCallback(async (afterSeq: number, replace: boolean) => {
    const sequence = ++eventSeq.current;
    const generation = staff.generation;
    setEventsBusy(true);
    if (replace) setEventsError(null);
    const result = await staff.run((api, signal) =>
      api.getEvents(staff.market, caseId, afterSeq, signal),
    );
    if (sequence !== eventSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
    setEventsBusy(false);
    if (result.stale) return;
    if (!result.ok) {
      setEventsError(errorMessage(result.error));
      return;
    }
    setEventsError(null);
    setEvents((previous) => {
      const merged = replace ? result.value.items : [...previous, ...result.value.items];
      const seen = new Set<number>();
      return merged.filter((event) => {
        if (seen.has(event.seq)) return false;
        seen.add(event.seq);
        return true;
      });
    });
    setEventsHasMore(result.value.has_more);
  }, [caseId, staff]);

  const loadEvidence = useCallback(async () => {
    const sequence = ++evidenceSeq.current;
    const generation = staff.generation;
    setEvidenceLoading(true);
    setEvidenceError(null);
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const result = await staff.run((api, signal) =>
        api.getEvidence(staff.market, caseId, signal),
      );
      if (sequence !== evidenceSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      if (result.ok) {
        setEvidence(result.value);
        setEvidenceLoading(false);
        return;
      }
      if (result.stale) return;
      lastError = result.error;
      if (attempt < 2) {
        await sleep(700 * (attempt + 1));
        if (sequence !== evidenceSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      }
    }
    setEvidence(null);
    setEvidenceLoading(false);
    setEvidenceError(errorMessage(lastError));
  }, [caseId, staff]);

  useEffect(() => {
    void loadDetail(true);
  }, [loadDetail]);

  useEffect(() => {
    void loadEvents(0, true);
    void loadEvidence();
  }, [loadEvents, loadEvidence]);

  const shouldPoll = detail !== null &&
    (detail.state === "executing" ||
      detail.state === "reconciliation_required" ||
      activeOperation(detail));

  useEffect(() => {
    if (!shouldPoll) return;
    let rounds = 0;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (cancelled) return;
      rounds += 1;
      if (rounds > 6) {
        window.clearInterval(timer);
        return;
      }
      void loadDetail(false, true);
    }, 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [loadDetail, shouldPoll]);

  function handleUpdated(next: CaseDetail) {
    setDetail(next);
    setRefreshError(null);
    const readTime = new Date().toISOString();
    setReadAt(readTime);
    staff.markRead(readTime);
    void loadEvents(0, true);
  }

  if (loading && detail === null) {
    return (
      <div className="staff-detail staff-detail-loading">
        <LoadingBlock label="正在读取诉求详情、订单快照、审批与执行状态…" />
        {retrying !== null ? (
          <Notice tone="info" title={`详情读取失败，正在进行第 ${retrying} 次有限重试`} live>
            只读请求会有限重试；保存、送审和审批不会自动重复。
          </Notice>
        ) : null}
        <div className="staff-skeleton-panel" aria-hidden="true" />
        <div className="staff-skeleton-panel" aria-hidden="true" />
      </div>
    );
  }

  if (detail === null) {
    return (
      <div className="staff-detail">
        <Notice
          tone="danger"
          title="详情读取失败"
          action={<button className="staff-button" type="button" onClick={() => void loadDetail(true)}>手动重试</button>}
        >
          {error ?? "请稍后重试。"}
        </Notice>
      </div>
    );
  }

  const blocking = detail.state === "executing" ||
    detail.state === "reconciliation_required" ||
    detail.operations.some((operation) =>
      operation.status === "dispatching" ||
      operation.status === "awaiting_receipt" ||
      operation.status === "retry_wait" ||
      operation.status === "reconciliation_required" ||
      (operation.status === "prepared" && operation.dispatched_at !== null),
    );

  return (
    <div className="staff-detail">
      <header className="staff-detail-head">
        <div>
          <div className="staff-detail-title">
            <h2>{REQUEST_TYPE_LABELS[detail.request_type]}</h2>
            <Badge tone={caseTone(detail.state)}>{CASE_STATE_LABELS[detail.state]}</Badge>
            <Badge tone="neutral">市场 {detail.market.toUpperCase()}</Badge>
            {detail.remote_ticket_id ? <Badge tone="success">已有远端工单</Badge> : null}
          </div>
          <p className="staff-detail-meta">
            <span>诉求 {detail.request_id}</span>
            <span>订单 {detail.order_id}</span>
            <span>版本 {detail.revision}</span>
            <TimeValue value={detail.updated_at} prefix="更新" />
          </p>
        </div>
        <div className="staff-detail-actions">
          {shouldPoll ? <Badge tone="info">执行中，有限轮询（最多 6 次）</Badge> : null}
          <button className="staff-button" type="button" onClick={() => void loadDetail(false)} disabled={refreshing}>
            <RefreshCw size={15} aria-hidden="true" />{refreshing ? "刷新中…" : "刷新详情"}
          </button>
        </div>
      </header>

      {retrying !== null ? (
        <Notice tone="info" title={`详情读取失败，正在进行第 ${retrying} 次有限重试`} live>
          只读请求会有限重试；保存、送审和审批不会自动重复。
        </Notice>
      ) : null}
      {refreshError ? (
        <Notice tone="warning" title="最新刷新的详情读取失败，当前内容已标为陈旧">
          {refreshError} 当前显示的是此前读取的内容，不能当作最新状态。
        </Notice>
      ) : null}
      {readAt ? (
        <p className="staff-read-line">最近成功读取：{formatRelative(readAt)}。刷新或轮询不会清除你正在编辑的输入。</p>
      ) : null}
      {blocking ? (
        <Notice tone="warning" title="原执行结果尚未解决">
          可保存新的候选草稿，但不会覆盖原执行，也不能第二次送审。仅在真实回执核验或转人工处理后，才能继续。
        </Notice>
      ) : null}

      <Panel title="诉求与消费者材料" subtitle={<TimeValue value={detail.request_created_at} prefix="消费者提交" />}>
        <p className="staff-description">{detail.description}</p>
        <dl className="staff-kv">
          <div><dt>问题类型</dt><dd>{REQUEST_TYPE_LABELS[detail.request_type]}</dd></div>
          <div><dt>E-pig 状态</dt><dd>{REQUEST_STATUS_LABELS[detail.remote_status]}</dd></div>
          <div><dt>远端更新时间</dt><dd><TimeValue value={detail.request_updated_at} /></dd></div>
        </dl>
        <AttachmentsSection detail={detail} />
        <MaterialsSection detail={detail} onUpdated={handleUpdated} onReload={() => void loadDetail(false)} />
      </Panel>

      <OrderSection detail={detail} />

      <EvidenceSection
        evidence={evidence}
        loading={evidenceLoading}
        error={evidenceError}
        onReload={() => void loadEvidence()}
      />

      <DraftSection
        detail={detail}
        evidenceChunks={evidence?.chunks ?? []}
        evidenceMissingReasons={evidence?.missing_reasons ?? []}
        evidenceLoading={evidenceLoading}
        blocking={blocking}
        onUpdated={handleUpdated}
        onReload={() => void loadDetail(false)}
      />

      <ApprovalSection detail={detail} onUpdated={handleUpdated} onReload={() => void loadDetail(false)} />

      <OperationsSection detail={detail} />

      <TicketSection caseId={detail.case_id} remoteTicketId={detail.remote_ticket_id} />

      <section className="staff-panel" aria-label="事件时间线">
        <header className="staff-panel-head">
          <div>
            <h3>事件时间线</h3>
            <div className="staff-panel-sub">关键结果可在时间线再次读取；这里只展示业务事件，不展示内部载荷或租约。</div>
          </div>
          <div className="staff-panel-actions">
            <button className="staff-button staff-button-quiet" type="button" onClick={() => void loadEvents(0, true)} disabled={eventsBusy}>
              <RefreshCw size={14} aria-hidden="true" />刷新
            </button>
          </div>
        </header>
        {eventsError ? (
          <Notice tone="warning" title="事件时间线读取失败" action={<button className="staff-button" type="button" onClick={() => void loadEvents(0, true)}>重试</button>}>
            {eventsError}
          </Notice>
        ) : null}
        {events.length === 0 && !eventsBusy ? (
          <EmptyBlock title="尚无可见事件">服务端尚未记录事件，或当前读取范围为空。</EmptyBlock>
        ) : (
          <TimelineSection events={events} />
        )}
        {eventsHasMore ? (
          <button className="staff-button staff-load-more" type="button" onClick={() => {
            const last = events.at(-1);
            if (last) void loadEvents(last.seq, false);
          }} disabled={eventsBusy}>
            {eventsBusy ? "读取中…" : "加载更多事件"}
          </button>
        ) : null}
      </section>
    </div>
  );
}

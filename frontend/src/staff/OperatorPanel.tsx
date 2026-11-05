import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import { useStaff } from "./context";
import {
  CASE_STATE_LABELS,
  OPERATION_STATUS_LABELS,
  RECONCILIATION_LABELS,
  errorMessage,
  eventLabel,
  formatDateTime,
  formatRelative,
  isConflict,
  reasonLabel,
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
  operationTone,
} from "./StaffBits";
import type {
  OperationStatus,
  WorkerOperationDetail,
  WorkerOperationListResponse,
} from "./types";

const PAGE_SIZE = 20;
const ACTIVE_STATUSES: OperationStatus[] = [
  "dispatching",
  "awaiting_receipt",
  "retry_wait",
  "reconciliation_required",
];

const ERROR_LABELS: Record<string, string> = {
  VALIDATION_ERROR: "请求参数未通过上游校验",
  UNAUTHORIZED: "上游认证失败",
  FORBIDDEN_MARKET: "上游拒绝该市场",
  NOT_FOUND: "暂未读到远端对象",
  IDEMPOTENCY_CONFLICT: "幂等键冲突",
  REQUEST_STATE_CONFLICT: "E-pig 状态冲突",
  ORDER_VERSION_CONFLICT: "订单版本冲突",
  ATTACHMENT_EXPIRED: "附件已过期",
  RATE_LIMITED: "上游限流，等待退避",
  INTERNAL_ERROR: "上游内部错误",
  UPSTREAM_UNAVAILABLE: "上游暂不可用",
  RETRY_LIMIT: "达到有限重试上限",
};

function errorLabel(code: string | null): string | null {
  if (!code) return null;
  return ERROR_LABELS[code] ?? "存在可查看的错误状态";
}

function operatorState(value: string | null): string {
  if (!value) return "—";
  return CASE_STATE_LABELS[value as keyof typeof CASE_STATE_LABELS] ??
    OPERATION_STATUS_LABELS[value as OperationStatus] ??
    RECONCILIATION_LABELS[value as keyof typeof RECONCILIATION_LABELS] ??
    "状态已更新";
}

export function OperatorPanel() {
  const staff = useStaff();
  const [statusFilter, setStatusFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [list, setList] = useState<WorkerOperationListResponse | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listRetrying, setListRetrying] = useState<number | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listStale, setListStale] = useState(false);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkerOperationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailStale, setDetailStale] = useState(false);
  const [detailReadAt, setDetailReadAt] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lookupBusy, setLookupBusy] = useState(false);
  const listSeq = useRef(0);
  const detailSeq = useRef(0);
  const listRef = useRef<WorkerOperationListResponse | null>(null);
  listRef.current = list;
  const detailRef = useRef<WorkerOperationDetail | null>(null);
  detailRef.current = detail;
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const loadList = useCallback(async () => {
    const sequence = ++listSeq.current;
    const generation = staff.generation;
    setListLoading(true);
    setListRetrying(null);
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const result = await staff.run((api, signal) =>
        api.listOperations(staff.market, {
          status: statusFilter || undefined,
          limit: PAGE_SIZE,
          offset,
        }, signal),
      );
      if (sequence !== listSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      if (result.ok) {
        setList(result.value);
        setListLoading(false);
        setListError(null);
        setListStale(false);
        const now = new Date().toISOString();
        setReadAt(now);
        staff.markRead(now);
        return;
      }
      if (result.stale) return;
      lastError = result.error;
      if (attempt < 2) {
        setListRetrying(attempt + 1);
        await sleep(700 * (attempt + 1));
        if (sequence !== listSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      }
    }
    setListLoading(false);
    setListRetrying(null);
    setListError(errorMessage(lastError));
    if (listRef.current !== null) setListStale(true);
  }, [offset, staff, statusFilter]);

  const loadDetail = useCallback(async (quiet = false) => {
    if (selectedId === null) return;
    const sequence = ++detailSeq.current;
    const generation = staff.generation;
    if (!quiet) setDetailLoading(true);
    setDetailError(null);
    const result = await staff.run((api, signal) =>
      api.getOperation(staff.market, selectedId, signal),
    );
    if (sequence !== detailSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
    if (result.stale) return;
    setDetailLoading(false);
    if (!result.ok) {
      setDetailError(errorMessage(result.error));
      if (detailRef.current !== null) setDetailStale(true);
      return;
    }
    setDetail(result.value);
    setDetailStale(false);
    const now = new Date().toISOString();
    setDetailReadAt(now);
    staff.markRead(now);
  }, [selectedId, staff]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      setDetailError(null);
      setDetailStale(false);
      return;
    }
    void loadDetail();
  }, [selectedId]); // eslint-disable-line react-hooks/exhaustive-deps

  const active = detail !== null && ACTIVE_STATUSES.includes(detail.status);
  useEffect(() => {
    if (!active) return;
    let rounds = 0;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (cancelled) return;
      rounds += 1;
      if (rounds > 6) {
        window.clearInterval(timer);
        return;
      }
      void loadDetail(true);
    }, 12_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [active, loadDetail]);

  async function lookup() {
    if (!detail || lookupBusy) return;
    setLookupBusy(true);
    setNotice(null);
    setConflict(null);
    setDetailError(null);
    const result = await staff.run((api, signal) =>
      api.lookupOperation(staff.market, detail.operation_id, signal),
    );
    if (result.stale) return;
    setLookupBusy(false);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setDetailError(errorMessage(result.error));
      setDetailStale(true);
      return;
    }
    setDetail(result.value);
    setDetailStale(false);
    const now = new Date().toISOString();
    setDetailReadAt(now);
    staff.markRead(now);
    setNotice("已由服务器继续查询一次。结果以返回状态为准，页面不会自行标记完成或成功。");
    await loadList();
  }

  return (
    <div className="staff-operator">
      <div className="staff-operator-head">
        <div>
          <h2>执行与对账</h2>
          <p>仅显示脱敏运行状态。Operator 不读取消费者业务详情、附件、证据或聊天内容。</p>
          <p className="staff-read-line">
            最近成功读取：{readAt ? `${formatDateTime(readAt)}（${formatRelative(readAt)}）` : "尚未读取"}
          </p>
        </div>
        <div className="staff-queue-actions">
          <label className="staff-field">
            <span className="staff-field-label">操作状态</span>
            <select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setOffset(0); }}>
              <option value="">全部状态</option>
              {(Object.keys(OPERATION_STATUS_LABELS) as OperationStatus[]).map((status) => (
                <option value={status} key={status}>{OPERATION_STATUS_LABELS[status]}</option>
              ))}
            </select>
          </label>
          <button className="staff-button" type="button" onClick={() => void loadList()} disabled={listLoading}>
            <RefreshCw size={15} aria-hidden="true" />{listLoading ? "读取中…" : "刷新"}
          </button>
        </div>
      </div>

      {listRetrying !== null ? <Notice tone="info" title={`读取失败，正在进行第 ${listRetrying} 次有限重试`} live>只读请求会有限重试；“继续查询”不会自动重复。</Notice> : null}
      {listError ? (
        <Notice tone={list !== null ? "warning" : "danger"} title={list !== null ? "最新读取失败，列表已标为陈旧" : "操作列表读取失败"} action={<button className="staff-button" type="button" onClick={() => void loadList()}>手动重试</button>}>
          {listError}
        </Notice>
      ) : null}
      {listStale ? <p className="staff-read-line">列表已标为陈旧，不能当作最新执行状态。</p> : null}

      <div className="staff-operator-grid">
        <section className="staff-panel staff-operator-list" aria-label="操作列表">
          <header className="staff-panel-head">
            <div>
              <h3>操作列表</h3>
              <div className="staff-panel-sub">列表只含脱敏进度与错误状态，不含回复、描述、payload 或回执原文。</div>
            </div>
          </header>
          {listLoading && list === null ? <LoadingBlock label="正在读取操作列表…" /> : null}
          {list !== null && list.items.length === 0 ? <EmptyBlock title="当前筛选下没有操作">后台 Worker 尚未准备或执行任何操作。</EmptyBlock> : null}
          {list !== null && list.items.length > 0 ? (
            <>
              <ul className="staff-operation-rows">
                {list.items.map((item) => (
                  <li key={item.operation_id}>
                    <button
                      type="button"
                      className={selectedId === item.operation_id ? "selected" : ""}
                      aria-pressed={selectedId === item.operation_id}
                      onClick={() => { setSelectedId(item.operation_id); setNotice(null); setConflict(null); }}
                    >
                      <div>
                        <Badge tone={operationTone(item.status)}>{OPERATION_STATUS_LABELS[item.status]}</Badge>
                        <Badge tone={caseTone(item.case_state)}>{CASE_STATE_LABELS[item.case_state]}</Badge>
                        <span>case #{item.case_id}</span>
                      </div>
                      <small>
                        尝试 {item.attempt} · 查询 {item.lookup_count}
                        {item.remote_ticket_id ? ` · 远端工单 ${item.remote_ticket_id}` : ""}
                      </small>
                      <TimeValue value={item.updated_at} prefix="更新" />
                    </button>
                  </li>
                ))}
              </ul>
              <footer className="staff-queue-foot">
                <span>{list.items.length} / {list.total}</span>
                <div className="staff-pager">
                  <button className="staff-button" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                    <ChevronLeft size={15} aria-hidden="true" />上一页
                  </button>
                  <button className="staff-button" type="button" disabled={!list.has_more} onClick={() => setOffset(offset + PAGE_SIZE)}>
                    下一页<ChevronRight size={15} aria-hidden="true" />
                  </button>
                </div>
              </footer>
            </>
          ) : null}
        </section>

        <Panel
          title="操作详情与继续查询"
          subtitle="“继续查询”只运行一次服务器回执查询，不会授权新的外部建单。"
        >
          {selectedId === null ? (
            <EmptyBlock title="选择一个操作">
              详情只展示脱敏状态、有限重试和事件；不展示消费者 PII 或内部执行载荷。
            </EmptyBlock>
          ) : detailLoading && detail === null ? <LoadingBlock label="正在读取操作详情…" /> : null}
          {detailError ? (
            <Notice tone={detail ? "warning" : "danger"} title={detail ? "最新详情读取失败，保留上次成功内容" : "操作详情读取失败"} action={<button className="staff-button" type="button" onClick={() => void loadDetail()}>手动重试</button>}>
              {detailError}
            </Notice>
          ) : null}
          {conflict ? <Notice tone="warning" title="操作状态冲突">{conflict} 请显式刷新后再操作。</Notice> : null}
          {notice ? <Notice tone="success" title={notice} live /> : null}
          {detail ? (
            <>
              <div className="staff-operator-detail-head">
                <h4><Search size={15} aria-hidden="true" />{detail.operation_id}</h4>
                <Badge tone={operationTone(detail.status)}>{OPERATION_STATUS_LABELS[detail.status]}</Badge>
                {detailStale ? <Badge tone="warning">陈旧结果</Badge> : null}
              </div>
              <dl className="staff-kv">
                <div><dt>诉求 case</dt><dd>#{detail.case_id}</dd></div>
                <div><dt>市场</dt><dd>{detail.market.toUpperCase()}</dd></div>
                <div><dt>诉求状态</dt><dd>{CASE_STATE_LABELS[detail.case_state]}</dd></div>
                <div><dt>尝试次数</dt><dd>{detail.attempt}</dd></div>
                <div><dt>查询次数</dt><dd>{detail.lookup_count}</dd></div>
                <div><dt>对账状态</dt><dd>{detail.reconciliation_state ? RECONCILIATION_LABELS[detail.reconciliation_state] : "未进入对账"}</dd></div>
                <div><dt>远端工单</dt><dd>{detail.remote_ticket_id ?? "未提供"}</dd></div>
                <div><dt>派遣时间</dt><dd><TimeValue value={detail.dispatched_at} /></dd></div>
                <div><dt>下次尝试</dt><dd><TimeValue value={detail.next_attempt_at} /></dd></div>
                <div><dt>错误状态</dt><dd>{errorLabel(detail.last_error_code) ?? "无"}</dd></div>
                <div><dt>最近读取</dt><dd><TimeValue value={detailReadAt} /></dd></div>
              </dl>
              {detailStale ? (
                <Notice tone="warning" title="以下内容已标为陈旧">
                  最近一次读取或查询失败，保留此前服务器成功返回的内容，不能当作最新履行状态。
                </Notice>
              ) : null}
              <div className="staff-inline-actions">
                <button className="staff-button staff-button-primary" type="button" onClick={() => void lookup()} disabled={lookupBusy}>
                  <RefreshCw size={15} aria-hidden="true" />{lookupBusy ? "继续查询中…" : "继续查询一次"}
                </button>
                <button className="staff-button" type="button" onClick={() => void loadDetail()} disabled={detailLoading}>刷新详情</button>
              </div>
              <p className="staff-readonly-note">
                成功数、完成状态只能由服务器返回；本页没有“标记成功/完成/自造回执”的按钮。
              </p>
              <div className="staff-subsection">
                <div className="staff-subhead"><h4>操作事件</h4><p>脱敏事件时间线。</p></div>
                {detail.events.length === 0 ? <EmptyBlock title="暂无操作事件" /> : (
                  <ol className="staff-timeline">
                    {detail.events.map((event) => (
                      <li key={event.seq}>
                        <span className="staff-timeline-dot" aria-hidden="true" />
                        <div className="staff-timeline-body">
                          <div className="staff-timeline-head">
                            <strong>{eventLabel(event.event_type)}</strong>
                            <TimeValue value={event.created_at} />
                          </div>
                          <p>{operatorState(event.old_state)} → {operatorState(event.new_state)}</p>
                          {reasonLabel(event.reason) ? <small>{reasonLabel(event.reason)}</small> : null}
                        </div>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </>
          ) : null}
        </Panel>
      </div>
    </div>
  );
}

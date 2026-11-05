import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  RotateCcw,
  Search,
} from "lucide-react";
import { useStaff } from "./context";
import {
  CASE_STATE_LABELS,
  REQUEST_TYPE_LABELS,
  ROLE_LABELS,
  errorMessage,
  formatDateTime,
  formatRelative,
  isConflict,
  maskId,
} from "./format";
import { sleep } from "./guard";
import { Badge, EmptyBlock, LoadingBlock, Notice, TimeValue, caseTone } from "./StaffBits";
import type { CaseListResponse, CaseSummary, CaseState, RequestType } from "./types";

const PAGE_SIZE = 20;
const STALE_AFTER_MS = 15 * 60 * 1000;

interface Filters {
  status: string;
  request_type: string;
  from: string;
  to: string;
}

const EMPTY_FILTERS: Filters = { status: "", request_type: "", from: "", to: "" };

function queryFilters(filters: Filters) {
  return {
    status: filters.status || undefined,
    request_type: filters.request_type || undefined,
    updated_from: filters.from ? new Date(`${filters.from}T00:00:00`).toISOString() : undefined,
    updated_to: filters.to ? new Date(`${filters.to}T23:59:59.999`).toISOString() : undefined,
    limit: PAGE_SIZE,
  };
}

function filtersActive(filters: Filters): boolean {
  return Boolean(filters.status || filters.request_type || filters.from || filters.to);
}

export function QueuePanel({
  selectedCaseId,
  onSelect,
}: {
  selectedCaseId: number | null;
  onSelect: (caseId: number) => void;
}) {
  const staff = useStaff();
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<CaseListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [retrying, setRetrying] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [staleByError, setStaleByError] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [syncBusy, setSyncBusy] = useState(false);
  const [readAt, setReadAt] = useState<string | null>(null);
  const dataRef = useRef<CaseListResponse | null>(null);
  const requestSeq = useRef(0);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const sequence = ++requestSeq.current;
    const generation = staff.generation;
    const hadData = dataRef.current !== null;
    if (hadData) setRefreshing(true);
    else setLoading(true);
    setRetrying(null);
    let lastError: unknown = null;
    let attempts = 0;
    while (attempts < 3) {
      const result = await staff.run((api, signal) =>
        api.listCases(staff.market, { ...queryFilters(filters), offset }, signal),
      );
      if (sequence !== requestSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
      if (result.ok) {
        dataRef.current = result.value;
        setData(result.value);
        setLoading(false);
        setRefreshing(false);
        setRetrying(null);
        setError(null);
        setConflict(null);
        setStaleByError(false);
        const readTime = new Date().toISOString();
        setReadAt(readTime);
        staff.markRead(readTime);
        return;
      }
      if (result.stale) return;
      lastError = result.error;
      if (attempts < 2) {
        attempts += 1;
        setRetrying(attempts);
        await sleep(700 * attempts);
        if (sequence !== requestSeq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
        continue;
      }
      break;
    }
    setLoading(false);
    setRefreshing(false);
    setRetrying(null);
    if (lastError !== null && isConflict(lastError)) {
      setConflict(errorMessage(lastError));
      setError(null);
    } else {
      setError(errorMessage(lastError));
      if (hadData) setStaleByError(true);
    }
  }, [filters, offset, staff]);

  useEffect(() => {
    void load();
  }, [load]);

  function updateFilter(key: keyof Filters, value: string) {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setOffset(0);
  }

  async function syncNow() {
    if (syncBusy) return;
    setSyncBusy(true);
    setNotice(null);
    setError(null);
    setConflict(null);
    const result = await staff.run((api, signal) => api.sync(staff.market, signal));
    if (result.stale) return;
    setSyncBusy(false);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setError(errorMessage(result.error));
      return;
    }
    setNotice(
      `同步完成：处理 ${result.value.processed} 条，新增 ${result.value.created} 条，更新 ${result.value.updated} 条，忽略陈旧 ${result.value.stale_ignored} 条。` +
      (result.value.has_more ? " 仍有更多待同步，可再次主动同步。" : ""),
    );
    await load();
  }

  const lastSuccess = data?.last_success_at ?? null;
  const staleByAge = lastSuccess === null ||
    Date.now() - new Date(lastSuccess).getTime() > STALE_AFTER_MS;
  const stale = staleByAge || staleByError;
  const activeFilters = filtersActive(filters);

  return (
    <section className="staff-queue" aria-label="诉求队列">
      <div className="staff-queue-head">
        <div>
          <h2>诉求队列</h2>
          <p>
            上次成功同步：{lastSuccess ? formatDateTime(lastSuccess) : "尚未成功同步"}
            {stale ? <Badge tone="warning">数据可能陈旧</Badge> : <Badge tone="success">已同步</Badge>}
          </p>
        </div>
        <div className="staff-queue-actions">
          {staff.canApprove && filters.status !== "waiting_approval" ? (
            <button className="staff-button staff-button-quiet" type="button" onClick={() => updateFilter("status", "waiting_approval")}>
              只看待审批
            </button>
          ) : null}
          <button className="staff-button" type="button" onClick={() => void load()} disabled={refreshing}>
            <RefreshCw size={15} aria-hidden="true" />{refreshing ? "读取中…" : "刷新"}
          </button>
          {staff.canSupport ? (
            <button className="staff-button staff-button-primary" type="button" onClick={() => void syncNow()} disabled={syncBusy}>
              <RefreshCw size={15} aria-hidden="true" />{syncBusy ? "同步中…" : "主动同步"}
            </button>
          ) : null}
        </div>
      </div>

      {!staff.canSupport ? (
        <p className="staff-readonly-note">
          当前角色为 {staff.identity.roles.map((role) => ROLE_LABELS[role]).join(" / ")}，可查看队列和详情，但不能主动同步或修改数据。
        </p>
      ) : (
        <p className="staff-readonly-note">主动同步只触发本次服务端同步；后台 Worker 仍独立运行，不会因关闭页面而停止。</p>
      )}

      <div className="staff-filters" aria-label="队列筛选">
        <label className="staff-field">
          <span className="staff-field-label">处理状态</span>
          <select value={filters.status} onChange={(event) => updateFilter("status", event.target.value)}>
            <option value="">全部状态</option>
            {(Object.keys(CASE_STATE_LABELS) as CaseState[]).map((state) => (
              <option value={state} key={state}>{CASE_STATE_LABELS[state]}</option>
            ))}
          </select>
        </label>
        <label className="staff-field">
          <span className="staff-field-label">问题类型</span>
          <select value={filters.request_type} onChange={(event) => updateFilter("request_type", event.target.value)}>
            <option value="">全部类型</option>
            {(Object.keys(REQUEST_TYPE_LABELS) as RequestType[]).map((type) => (
              <option value={type} key={type}>{REQUEST_TYPE_LABELS[type]}</option>
            ))}
          </select>
        </label>
        <label className="staff-field">
          <span className="staff-field-label">更新起始日期</span>
          <input type="date" value={filters.from} onChange={(event) => updateFilter("from", event.target.value)} />
        </label>
        <label className="staff-field">
          <span className="staff-field-label">更新截止日期</span>
          <input type="date" value={filters.to} onChange={(event) => updateFilter("to", event.target.value)} />
        </label>
        {activeFilters ? (
          <button className="staff-button staff-button-quiet staff-clear-filters" type="button" onClick={() => { setFilters(EMPTY_FILTERS); setOffset(0); }}>
            <RotateCcw size={14} aria-hidden="true" />清除筛选
          </button>
        ) : null}
      </div>

      {retrying !== null ? (
        <Notice tone="info" title={`读取失败，正在进行第 ${retrying} 次有限重试`} live>
          只读请求会自动有限重试；写操作不会自动重复。
        </Notice>
      ) : null}
      {conflict ? (
        <Notice
          tone="warning"
          title="队列版本冲突"
          action={<button className="staff-button" type="button" onClick={() => void load()}>显式读取最新队列</button>}
        >
          {conflict}
        </Notice>
      ) : null}
      {error ? (
        <Notice
          tone="danger"
          title={data ? "最新读取失败，当前列表已标为陈旧" : "队列读取失败"}
          action={<button className="staff-button" type="button" onClick={() => void load()}>手动重试</button>}
        >
          {error}
        </Notice>
      ) : null}
      {notice ? <Notice tone="success" title={notice} live /> : null}

      {loading ? (
        <div className="staff-queue-loading">
          <LoadingBlock label="正在读取当前市场的诉求队列…" />
          {[0, 1, 2].map((item) => <div className="staff-skeleton-row" key={item} aria-hidden="true" />)}
        </div>
      ) : data !== null && data.items.length === 0 ? (
        <EmptyBlock
          title={activeFilters ? "当前筛选下没有诉求" : "当前市场暂无诉求"}
          action={activeFilters ? <button className="staff-button" type="button" onClick={() => { setFilters(EMPTY_FILTERS); setOffset(0); }}>清除筛选</button> : undefined}
        >
          {activeFilters ? "可以清除筛选查看全部诉求，或等待下一次后台同步。" : "后台 Worker 同步后，新诉求会出现在这里。"}
        </EmptyBlock>
      ) : data !== null ? (
        <>
          <ul className="staff-case-list">
            {data.items.map((item) => (
              <li key={item.case_id}>
                <CaseRow
                  item={item}
                  selected={selectedCaseId === item.case_id}
                  onSelect={() => onSelect(item.case_id)}
                />
              </li>
            ))}
          </ul>
          <footer className="staff-queue-foot">
            <span className="staff-success-line">
              <CheckCircle2 size={14} aria-hidden="true" />
              已读取 {data.items.length} / {data.total} 条，读取时间 {formatRelative(readAt)}
            </span>
            <div className="staff-pager">
              <button className="staff-button" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                <ChevronLeft size={15} aria-hidden="true" />上一页
              </button>
              <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
              <button className="staff-button" type="button" disabled={!data.has_more} onClick={() => setOffset(offset + PAGE_SIZE)}>
                下一页<ChevronRight size={15} aria-hidden="true" />
              </button>
            </div>
          </footer>
        </>
      ) : null}
    </section>
  );
}

function CaseRow({
  item,
  selected,
  onSelect,
}: {
  item: CaseSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`staff-case-row ${selected ? "selected" : ""}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <div className="staff-case-row-main">
        <div className="staff-case-row-title">
          <Search size={14} aria-hidden="true" />
          <strong>{REQUEST_TYPE_LABELS[item.request_type]}</strong>
          <Badge tone={caseTone(item.state)}>{CASE_STATE_LABELS[item.state]}</Badge>
        </div>
        <div className="staff-case-row-meta">
          <span>诉求 {maskId(item.request_id)}</span>
          <span>订单 {maskId(item.order_id)}</span>
          <span>{item.market.toUpperCase()}</span>
          <span>附件 {item.attachment_count}</span>
        </div>
        <div className="staff-case-row-flags">
          {item.has_pending_approval ? <Badge tone="warning">待审批</Badge> : null}
          {item.has_prepared_operation ? <Badge tone="info">待执行</Badge> : null}
          {item.remote_ticket_id ? <Badge tone="success">已有远端工单</Badge> : null}
        </div>
      </div>
      <div className="staff-case-row-time">
        <TimeValue value={item.updated_at} />
        <small>{formatRelative(item.updated_at)}</small>
      </div>
    </button>
  );
}

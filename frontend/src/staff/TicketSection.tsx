import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import { StaffApiError } from "./api";
import { useStaff } from "./context";
import {
  TICKET_ACTION_LABELS,
  TICKET_STATUS_LABELS,
  errorMessage,
} from "./format";
import { Badge, EmptyBlock, LoadingBlock, Notice, Panel, TimeValue } from "./StaffBits";
import type { CaseTicketResponse } from "./types";

export function TicketSection({
  caseId,
  remoteTicketId,
}: {
  caseId: number;
  remoteTicketId: string | null;
}) {
  const staff = useStaff();
  const [ticket, setTicket] = useState<CaseTicketResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [lastAttemptAt, setLastAttemptAt] = useState<string | null>(null);
  const seq = useRef(0);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const sequence = ++seq.current;
    const generation = staff.generation;
    setLoading(true);
    setError(null);
    const result = await staff.run((api, signal) => api.getTicket(staff.market, caseId, signal));
    if (sequence !== seq.current || !aliveRef.current || !staff.isCurrent(generation)) return;
    if (result.stale) return;
    setLoading(false);
    setLastAttemptAt(new Date().toISOString());
    if (result.ok) {
      setTicket(result.value);
      setStale(false);
      setNotFound(false);
      return;
    }
    if (result.error instanceof StaffApiError && result.error.status === 404) {
      setNotFound(true);
      if (ticket !== null) setStale(true);
      return;
    }
    setError(errorMessage(result.error));
    if (ticket !== null) setStale(true);
  }, [caseId, staff, ticket]);

  useEffect(() => {
    if (remoteTicketId !== null) void load();
  }, [remoteTicketId]); // Deliberately once per remote ticket id; manual refresh uses the button.

  return (
    <Panel
      title="真实工单读取"
      subtitle="按已关联远端 ID 经 Connector 读取真实工单；读取不改变审批或完成状态。"
      actions={
        <button className="staff-button" type="button" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={14} aria-hidden="true" />{loading ? "查询中…" : "查询真实工单"}
        </button>
      }
    >
      <p className="staff-readonly-note">
        原操作执行进度与本次工单读取是两个事实。按钮成功只代表本次读取成功；不会乐观把本地状态改成已完成。
      </p>
      {loading && ticket === null ? <LoadingBlock label="正在读取远端真实工单与回执…" /> : null}
      {error ? (
        <Notice tone={ticket ? "warning" : "danger"} title={ticket ? "本次查询失败，已保留上次核验内容" : "真实工单查询失败"}>
          {error}
          {ticket ? " 以下内容来自上次成功核验，已标为陈旧，不能当作最新履行状态。" : ""}
        </Notice>
      ) : null}
      {notFound ? (
        ticket === null ? (
          <EmptyBlock title="尚未找到真实远端工单">
            服务端没有关联到可读取的远端工单。这不代表工单不存在，也不代表已经完成。
            {lastAttemptAt ? <> 最近查询：<TimeValue value={lastAttemptAt} /></> : null}
          </EmptyBlock>
        ) : (
          <Notice tone="warning" title="本次未找到远端工单，保留上次已核验结果">
            上次核验结果仍显示在下方，但不能当作最新状态。
          </Notice>
        )
      ) : null}
      {ticket ? (
        <div className={`staff-ticket ${stale ? "stale" : ""}`}>
          <div className="staff-ticket-head">
            <h4><ExternalLink size={15} aria-hidden="true" />工单 {ticket.ticket.ticket_id}</h4>
            <Badge tone={ticket.ticket.status === "closed" ? "neutral" : "info"}>
              {TICKET_STATUS_LABELS[ticket.ticket.status]}
            </Badge>
            {stale ? <Badge tone="warning">陈旧结果</Badge> : <Badge tone="success">本次已核验</Badge>}
          </div>
          <dl className="staff-kv">
            <div><dt>动作</dt><dd>{TICKET_ACTION_LABELS[ticket.ticket.action]}</dd></div>
            <div><dt>诉求</dt><dd>{ticket.ticket.request_id}</dd></div>
            <div><dt>订单</dt><dd>{ticket.ticket.order_id}</dd></div>
            <div><dt>工单创建</dt><dd><TimeValue value={ticket.ticket.created_at} /></dd></div>
            <div><dt>工单更新</dt><dd><TimeValue value={ticket.ticket.updated_at} /></dd></div>
            <div><dt>本次查询</dt><dd><TimeValue value={ticket.queried_at} /></dd></div>
            <div><dt>草稿版本</dt><dd>{ticket.ticket.draft_version}</dd></div>
            <div><dt>政策版本</dt><dd>{ticket.ticket.policy_version}</dd></div>
          </dl>
          <blockquote>{ticket.ticket.reply}</blockquote>
          <p className="staff-ticket-receipt">
            真实回执：{ticket.ticket.receipt.receipt_id} · 创建于 <TimeValue value={ticket.ticket.receipt.created_at} />
            {ticket.operation_id ? " · 已匹配本地批准操作" : " · 仅外部工单，未匹配本地批准链"}
          </p>
          <p className="staff-muted">
            {ticket.verification_state === "matched_operation"
              ? "market、订单、诉求、动作、回复与回执已按原批准快照核验。"
              : "该工单来自外部事实读取；未匹配本地批准链，需要人工核验，不能据此伪造本地审批。"}
          </p>
        </div>
      ) : null}
      {lastAttemptAt ? <p className="staff-read-line">最近一次查询时间：<TimeValue value={lastAttemptAt} /></p> : null}
    </Panel>
  );
}

import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, Clock3, Info, LoaderCircle, ShieldAlert } from "lucide-react";
import { formatDateTime } from "./format";
import type {
  ApprovalStatus,
  CaseState,
  DraftStatus,
  OperationStatus,
} from "./types";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`staff-badge staff-badge-${tone}`}>{children}</span>;
}

export function caseTone(state: CaseState): Tone {
  if (state === "completed") return "success";
  if (state === "failed") return "danger";
  if (state === "needs_info" || state === "reconciliation_required") return "warning";
  if (state === "waiting_approval" || state === "executing") return "info";
  return "neutral";
}

export function draftTone(status: DraftStatus): Tone {
  if (status === "editing") return "info";
  if (status === "pending_approval") return "warning";
  if (status === "approved" || status === "consumed") return "success";
  if (status === "rejected" || status === "invalidated") return "danger";
  return "neutral";
}

export function approvalTone(status: ApprovalStatus): Tone {
  if (status === "pending") return "warning";
  if (status === "approved" || status === "executing" || status === "consumed") return "success";
  if (status === "rejected" || status === "expired" || status === "invalidated") return "danger";
  return "neutral";
}

export function operationTone(status: OperationStatus): Tone {
  if (status === "succeeded") return "success";
  if (status === "failed") return "danger";
  if (status === "reconciliation_required" || status === "retry_wait") return "warning";
  if (status === "dispatching" || status === "awaiting_receipt") return "info";
  return "neutral";
}

export function TimeValue({ value, prefix }: { value: string | null | undefined; prefix?: string }) {
  const display = formatDateTime(value);
  if (!value || display === "未提供") {
    return <span className="staff-muted">{prefix ? `${prefix}：` : ""}未提供</span>;
  }
  return (
    <span title={`绝对时间：${value}`}>
      {prefix ? `${prefix}：` : ""}
      <time dateTime={value}>{display}</time>
    </span>
  );
}

export function Notice({
  tone,
  title,
  children,
  action,
  live = false,
}: {
  tone: Tone;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  live?: boolean;
}) {
  const Icon = tone === "success" ? CheckCircle2
    : tone === "danger" ? AlertCircle
      : tone === "warning" ? ShieldAlert
        : Info;
  return (
    <div
      className={`staff-notice staff-notice-${tone}`}
      role={tone === "danger" ? "alert" : "status"}
      aria-live={live ? "polite" : "off"}
    >
      <Icon size={16} aria-hidden="true" />
      <div className="staff-notice-body">
        <strong>{title}</strong>
        {children ? <div>{children}</div> : null}
      </div>
      {action ? <div className="staff-notice-action">{action}</div> : null}
    </div>
  );
}

export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="staff-loading" role="status" aria-live="polite">
      <LoaderCircle size={16} className="staff-spin" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyBlock({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="staff-empty">
      <Clock3 size={20} aria-hidden="true" />
      <strong>{title}</strong>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

export function Panel({
  title,
  subtitle,
  children,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="staff-panel" aria-label={title}>
      <header className="staff-panel-head">
        <div>
          <h3>{title}</h3>
          {subtitle ? <div className="staff-panel-sub">{subtitle}</div> : null}
        </div>
        {actions ? <div className="staff-panel-actions">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="staff-field">
      <span className="staff-field-label">{label}</span>
      {children}
      {hint ? <span className="staff-field-hint">{hint}</span> : null}
    </label>
  );
}

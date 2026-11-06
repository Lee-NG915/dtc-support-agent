import { useEffect, useRef, useState } from "react";
import { AlertCircle, Bot, ClipboardCheck, Save, Send, X } from "lucide-react";
import { StaffApiError } from "./api";
import { useStaff } from "./context";
import {
  DRAFT_STATUS_LABELS,
  TICKET_ACTION_LABELS,
  errorMessage,
  isAiUnavailable,
  isConflict,
  missingReasonLabel,
} from "./format";
import {
  Badge,
  EmptyBlock,
  Notice,
  Panel,
  TimeValue,
  draftTone,
} from "./StaffBits";
import type {
  CaseDetail,
  DraftView,
  EvidenceView,
  TicketAction,
} from "./types";

type NoticeState = { tone: "success" | "warning" | "danger" | "info"; title: string; text?: string };

function evidenceIdsOf(draft: DraftView | null): string[] {
  return draft?.evidence.map((item) => item.chunk_id) ?? [];
}

function newestDraft(drafts: DraftView[]): DraftView | null {
  if (drafts.length === 0) return null;
  return [...drafts].sort((left, right) => right.version - left.version)[0] ?? null;
}

function detailIsBlocking(detail: CaseDetail): boolean {
  return detail.state === "executing" ||
    detail.state === "reconciliation_required" ||
    detail.operations.some((operation) =>
      operation.status === "dispatching" ||
      operation.status === "awaiting_receipt" ||
      operation.status === "retry_wait" ||
      operation.status === "reconciliation_required" ||
      (operation.status === "prepared" && operation.dispatched_at !== null),
    );
}

export function DraftSection({
  detail,
  evidenceChunks,
  evidenceMissingReasons,
  evidenceLoading,
  blocking,
  onUpdated,
  onReload,
  collapsible = false,
  defaultOpen = true,
}: {
  detail: CaseDetail;
  evidenceChunks: EvidenceView[];
  evidenceMissingReasons: string[];
  evidenceLoading: boolean;
  blocking: boolean;
  onUpdated: (detail: CaseDetail) => void;
  onReload: () => void;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  const staff = useStaff();
  const current = detail.current_draft;
  const [action, setAction] = useState<TicketAction>(current?.action ?? "other");
  const [summary, setSummary] = useState(current?.internal_summary ?? "");
  const [reply, setReply] = useState(current?.consumer_reply ?? "");
  const [selectedEvidence, setSelectedEvidence] = useState<string[]>(evidenceIdsOf(current));
  const [sourceDraftId, setSourceDraftId] = useState<string | null>(current?.draft_id ?? null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"save" | "suggest" | "submit" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [confirmSubmit, setConfirmSubmit] = useState(false);
  const [lastSavedCandidate, setLastSavedCandidate] = useState<number | null>(null);
  // The revision the current edit is based on. Background refreshes never move
  // it; only an explicit "read latest" ack may advance it.
  const [baseRevision, setBaseRevision] = useState(detail.revision);
  const [revisionStale, setRevisionStale] = useState(false);
  const editSeqRef = useRef(0);

  const completed = detail.state === "completed";
  const hasPendingApproval = detail.approvals.some((approval) => approval.status === "pending");
  const hasPreparedOperation = detail.operations.some((operation) => operation.status === "prepared");
  const currentEditable = current !== null &&
    current.status === "editing" &&
    current.draft_id === sourceDraftId &&
    !dirty &&
    !blocking &&
    !completed;
  const evidenceUsable = !evidenceLoading &&
    evidenceChunks.length > 0 &&
    evidenceMissingReasons.length === 0;
  const evidenceMissingSelection = evidenceLoading
    ? []
    : selectedEvidence.filter((id) => !evidenceChunks.some((chunk) => chunk.chunk_id === id));
  const hasSelectedCurrentEvidence = selectedEvidence.some((id) =>
    evidenceChunks.some((chunk) => chunk.chunk_id === id));
  const canSubmit = currentEditable &&
    (detail.state === "pending" || detail.state === "processing") &&
    evidenceUsable &&
    hasSelectedCurrentEvidence;
  const serverChanged = current !== null && sourceDraftId !== null && current.draft_id !== sourceDraftId;
  const revisionChanged = revisionStale || detail.revision !== baseRevision;
  const baseDraft = detail.drafts.find((draft) => draft.draft_id === sourceDraftId) ?? null;

  useEffect(() => {
    // A poll/background refresh only marks the edit base stale. It must never
    // silently rebase the user's input onto the newer revision.
    setRevisionStale(detail.revision !== baseRevision);
  }, [baseRevision, detail.revision]);

  function markDirty() {
    editSeqRef.current += 1;
    setDirty(true);
    setNotice(null);
    setError(null);
    setConflict(null);
    setConfirmSubmit(false);
  }

  function adoptLatest() {
    if (detail.revision === baseRevision) return;
    setBaseRevision(detail.revision);
    setRevisionStale(false);
    setNotice({
      tone: "info",
      title: `已确认以服务端数据版本 r${detail.revision} 为保存基准`,
      text: "编辑区输入已保留。之后保存会按该版本发送 expected_revision；自动刷新不会替你确认。",
    });
  }

  function loadDraftVersion(draft: DraftView) {
    setAction(draft.action);
    setSummary(draft.internal_summary);
    setReply(draft.consumer_reply);
    setSelectedEvidence(evidenceIdsOf(draft));
    setSourceDraftId(draft.draft_id);
    setDirty(false);
    setError(null);
    setConflict(null);
    setConfirmSubmit(false);
    setNotice({
      tone: "info",
      title: `已载入草稿 v${draft.version} 到编辑区`,
      text: draft.draft_id === current?.draft_id
        ? "当前编辑区对应服务端当前版本。"
        : "该版本不是当前生效版本；保存会形成新的草稿版本，送审前请核对内容。",
    });
  }

  async function saveDraft() {
    if (busy || completed) return;
    setError(null);
    setConflict(null);
    setNotice(null);
    if (!summary.trim() || !reply.trim()) {
      setError("内部摘要和消费者最终回复都不能为空。");
      return;
    }
    const submittedAction = action;
    const submittedSummary = summary.trim();
    const submittedReply = reply.trim();
    const submittedEvidence = [...selectedEvidence];
    const submittedRevision = baseRevision;
    const editSeqAtSave = editSeqRef.current;
    setBusy("save");
    const result = await staff.run((api, signal) =>
      api.createDraft(staff.market, detail.case_id, {
        expected_revision: submittedRevision,
        action: submittedAction,
        internal_summary: submittedSummary,
        consumer_reply: submittedReply,
        evidence_chunk_ids: submittedEvidence,
      }, signal),
    );
    if (result.stale) return;
    setBusy(null);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setError(errorMessage(result.error));
      return;
    }
    const next = result.value;
    const nextBlocking = detailIsBlocking(next);
    const newest = newestDraft(next.drafts);
    const saved = [...next.drafts]
      .filter((draft) =>
        draft.status === "editing" &&
        draft.action === submittedAction &&
        draft.internal_summary === submittedSummary &&
        draft.consumer_reply === submittedReply)
      .sort((left, right) => right.version - left.version)[0] ?? newest;
    // This successful write advanced the server revision; later saves of the
    // still-open local edit must build on it.
    setBaseRevision(next.revision);
    if (saved) {
      setLastSavedCandidate(saved.version);
      setSourceDraftId(saved.draft_id);
    }
    const editedWhileSaving = editSeqRef.current !== editSeqAtSave;
    if (editedWhileSaving) {
      // Keep B in the form and keep submit locked. Only a save of B may clear
      // dirty; do not present server A as the current edit.
      setDirty(true);
      setNotice({
        tone: "warning",
        title: saved
          ? `草稿 v${saved.version} 已保存；当前修改尚未保存`
          : "草稿已保存；当前修改尚未保存",
        text: "保存等待期间你又修改了内容。送审已锁定，请先保存当前修改，再显式提交审批。",
      });
    } else if (!nextBlocking && saved && next.current_draft?.draft_id === saved.draft_id) {
      setDirty(false);
      setNotice({
        tone: "success",
        title: `草稿 v${next.current_draft.version} 已保存为编辑中`,
        text: "尚未送审，也不会自动批准或派遣。请核对内容后显式提交审批。",
      });
    } else if (!nextBlocking && saved) {
      // The write succeeded, but another write changed the current draft before
      // this response was read back. Keep the saved version selected but do not
      // bind the form to a different current draft.
      setDirty(false);
      setNotice({
        tone: "warning",
        title: `草稿 v${saved.version} 已保存，但服务端当前版本已变化`,
        text: `服务端当前为 ${next.current_draft ? `v${next.current_draft.version}` : "无草稿"}。你的输入仍保留；请先核对最新版本，再显式保存或送审。`,
      });
    } else {
      setDirty(false);
      setNotice({
        tone: "warning",
        title: saved ? `候选草稿 v${saved.version} 已保存` : "候选草稿已保存",
        text: "原执行结果尚未解决：候选不会覆盖原执行，也不能第二次送审。当前生效版本仍保持原样。",
      });
    }
    onUpdated(next);
  }

  async function suggest() {
    if (busy || completed) return;
    setError(null);
    setConflict(null);
    setNotice(null);
    setBusy("suggest");
    const result = await staff.run((api, signal) =>
      api.suggest(staff.market, detail.case_id, detail.revision, signal),
    );
    if (result.stale) return;
    setBusy(null);
    if (!result.ok) {
      if (isAiUnavailable(result.error)) {
        setNotice({
          tone: "warning",
          title: "AI 建议不可用，可继续人工撰写草稿",
          text: "服务端未配置可用的生成模型或模型暂时失败。页面不会把空结果或 Fake 输出当作 AI 成功。",
        });
      } else if (result.error instanceof StaffApiError && result.error.code === "STATE_CONFLICT") {
        setNotice({
          tone: "warning",
          title: "AI 建议未生成，可继续人工草稿",
          text: "服务端拒绝了生成：可能缺少政策证据、证据冲突或业务上下文已变化。不会展示编造的候选内容。",
        });
      } else if (isConflict(result.error)) {
        setConflict(errorMessage(result.error));
      } else {
        setError(errorMessage(result.error));
      }
      return;
    }
    onUpdated(result.value);
    const latest = newestDraft(result.value.drafts);
    setNotice({
      tone: "success",
      title: latest ? `AI 已生成草稿 v${latest.version}，状态为编辑中` : "AI 已生成新草稿",
      text: detailIsBlocking(result.value)
        ? "该候选不会覆盖原执行，也不能第二次送审；请人工核对。"
        : "AI 只形成候选草稿，未送审、未批准、未派遣；请人工修改后显式送审。",
    });
  }

  async function submitDraft() {
    if (!canSubmit || !current || busy) return;
    const editSeqAtSubmit = editSeqRef.current;
    setBusy("submit");
    setError(null);
    setConflict(null);
    setNotice(null);
    const result = await staff.run((api, signal) =>
      api.submitDraft(staff.market, detail.case_id, current.version, detail.revision, signal),
    );
    if (result.stale) return;
    setBusy(null);
    setConfirmSubmit(false);
    if (!result.ok) {
      if (isConflict(result.error)) setConflict(errorMessage(result.error));
      else setError(errorMessage(result.error));
      return;
    }
    const editedWhileSubmitting = editSeqRef.current !== editSeqAtSubmit;
    if (!editedWhileSubmitting) setDirty(false);
    setNotice(editedWhileSubmitting
      ? {
          tone: "warning",
          title: `草稿 v${current.version} 已送审；当前修改尚未保存`,
          text: "送审等待期间你又修改了内容。审批针对已送审版本，当前输入需要另行保存。",
        }
      : {
          tone: "success",
          title: `草稿 v${current.version} 已送审`,
          text: "已锁定版本等待人工审批；没有创建工单，也没有自动批准或派遣。",
        });
    onUpdated(result.value);
  }

  function toggleEvidence(chunkId: string) {
    markDirty();
    setSelectedEvidence((previous) =>
      previous.includes(chunkId)
        ? previous.filter((id) => id !== chunkId)
        : [...previous, chunkId]);
  }

  return (
    <Panel
      title="草稿与送审"
      subtitle={`当前版本：${current ? `v${current.version} · ${DRAFT_STATUS_LABELS[current.status]}` : "尚无草稿"}；生成和保存都不会自动送审`}
      actions={current ? <Badge tone={draftTone(current.status)}>{DRAFT_STATUS_LABELS[current.status]}</Badge> : null}
      collapsible={collapsible}
      defaultOpen={defaultOpen}
    >
      {!staff.canSupport ? (
        <DraftReadOnly detail={detail} />
      ) : (
        <>
          {completed ? <Notice tone="info" title="诉求已完成">只能查看草稿历史，不能再保存或送审。</Notice> : null}
          {blocking ? (
            <Notice tone="warning" title="可保存候选，但不能第二次送审">
              原执行结果尚未解决。新保存的是候选草稿，不会覆盖当前生效版本；只有原结果核验或转人工处理后才可能继续。
            </Notice>
          ) : null}
          {hasPendingApproval ? (
            <Notice tone="warning" title="已有待审批版本">
              保存新版本会使当前待审批版本失效，服务端会记录失效原因；页面不会静默覆盖。
            </Notice>
          ) : null}
          {hasPreparedOperation && !blocking ? (
            <Notice tone="info" title="已有已批准并准备执行的操作">
              保存新版本会使该准备执行失效并需要重新审批。系统不会取消已经发出的外部调用，结果仍以 Worker 和真实回执为准。
            </Notice>
          ) : null}
          {serverChanged || revisionChanged ? (
            <Notice tone="warning" title="服务端数据已变化，你的输入仍保留">
              编辑区基于数据版本 r{baseRevision}
              {baseDraft ? ` / 草稿 v${baseDraft.version}` : ""}
              ，服务端当前为 r{detail.revision}
              {current ? ` / v${current.version}` : " / 无草稿"}。
              直接保存会按 r{baseRevision} 发送并可能冲突；请先显式读取最新版本，再按新版本保存。
              <button className="staff-button" type="button" onClick={onReload}>读取最新版本（保留输入）</button>
              <button className="staff-button" type="button" onClick={adoptLatest}>确认以 r{detail.revision} 为保存基准</button>
              {current ? (
                <div className="staff-latest-draft">
                  <strong>服务端最新 v{current.version} · {DRAFT_STATUS_LABELS[current.status]} · {TICKET_ACTION_LABELS[current.action]}</strong>
                  <p><b>内部摘要：</b>{current.internal_summary}</p>
                  <blockquote>{current.consumer_reply}</blockquote>
                </div>
              ) : null}
            </Notice>
          ) : null}
          {lastSavedCandidate !== null && blocking ? (
            <p className="staff-muted">
              本次保存的候选为 v{lastSavedCandidate}；当前生效版本仍是 {current ? `v${current.version}` : "无"}。两者不会互相覆盖。
            </p>
          ) : null}

          <div className="staff-form">
            <div className="staff-form-row">
              <label className="staff-field">
                <span className="staff-field-label">处理动作</span>
                <select
                  value={action}
                  onChange={(event) => { setAction(event.target.value as TicketAction); markDirty(); }}
                  disabled={completed}
                >
                  {(Object.keys(TICKET_ACTION_LABELS) as TicketAction[]).map((value) => (
                    <option value={value} key={value}>{TICKET_ACTION_LABELS[value]}</option>
                  ))}
                </select>
              </label>
              <div className="staff-form-inline-note">
                <Badge tone={evidenceUsable ? "success" : "warning"}>
                  {evidenceLoading ? "证据读取中" : evidenceUsable ? `可用证据 ${evidenceChunks.length} 条` : "政策证据不可用于送审"}
                </Badge>
                {!evidenceUsable ? (
                  <small>
                    缺少可用政策证据或存在缺资料/冲突时不能送审；保存草稿也不会形成政策承诺。
                    {evidenceMissingReasons.length > 0
                      ? ` 原因：${evidenceMissingReasons.map(missingReasonLabel).join("；")}`
                      : ""}
                  </small>
                ) : null}
              </div>
            </div>
            <label className="staff-field">
              <span className="staff-field-label">内部摘要</span>
              <textarea
                value={summary}
                onChange={(event) => { setSummary(event.target.value); markDirty(); }}
                rows={2}
                maxLength={2000}
                disabled={completed}
              />
            </label>
            <label className="staff-field">
              <span className="staff-field-label">消费者最终回复</span>
              <textarea
                value={reply}
                onChange={(event) => { setReply(event.target.value); markDirty(); }}
                rows={5}
                maxLength={10000}
                disabled={completed}
              />
              <span className="staff-field-hint">该回复只在工单真实创建后成为不可变消费者回复；内部驳回不会通知消费者。</span>
            </label>

            <fieldset className="staff-evidence-picker">
              <legend>引用政策证据（由服务端按当前订单范围返回）</legend>
              {evidenceLoading ? <p className="staff-muted">正在读取可用证据…</p> : null}
              {!evidenceLoading && evidenceChunks.length === 0 ? (
                <p className="staff-muted">当前没有可引用证据。仍可保存人工草稿，但不能送审。</p>
              ) : null}
              {evidenceChunks.map((chunk) => (
                <label className="staff-check-row" key={chunk.chunk_id}>
                  <input
                    type="checkbox"
                    checked={selectedEvidence.includes(chunk.chunk_id)}
                    onChange={() => toggleEvidence(chunk.chunk_id)}
                    disabled={completed}
                  />
                  <span>
                    <strong>{chunk.title}{chunk.section ? ` · ${chunk.section}` : ""}</strong>
                    <small>版本 {chunk.policy_version}{chunk.is_demo ? " · 演示政策" : ""}</small>
                  </span>
                </label>
              ))}
              {evidenceMissingSelection.length > 0 ? (
                <p className="staff-muted">
                  另有 {evidenceMissingSelection.length} 条已保存引用不在本次证据结果中；送审前请重新选择可用证据。
                </p>
              ) : null}
            </fieldset>

            {conflict ? (
              <Notice tone="warning" title="版本冲突，未提交的输入已保留">
                {conflict}
                <div className="staff-inline-actions">
                  <button className="staff-button" type="button" onClick={onReload}>读取最新版本（保留输入）</button>
                </div>
              </Notice>
            ) : null}
            {error ? <Notice tone="danger" title={error} /> : null}
            {notice ? <Notice tone={notice.tone} title={notice.title} live>{notice.text}</Notice> : null}

            <div className="staff-form-actions">
              <button className="staff-button" type="button" onClick={() => void suggest()} disabled={busy !== null || completed}>
                <Bot size={15} aria-hidden="true" />{busy === "suggest" ? "生成中…" : "AI 生成建议草稿"}
              </button>
              <button className="staff-button" type="button" onClick={() => void saveDraft()} disabled={busy !== null || completed}>
                <Save size={15} aria-hidden="true" />{busy === "save" ? "保存中…" : "保存为新的编辑草稿"}
              </button>
              {confirmSubmit ? (
                <span className="staff-submit-confirm">
                  <AlertCircle size={15} aria-hidden="true" />
                  确认提交 v{current?.version} 送审？不会创建工单。
                  <button className="staff-button staff-button-primary" type="button" onClick={() => void submitDraft()} disabled={busy !== null}>
                    <ClipboardCheck size={15} aria-hidden="true" />{busy === "submit" ? "提交中…" : "确认送审"}
                  </button>
                  <button className="staff-button staff-button-quiet" type="button" onClick={() => setConfirmSubmit(false)} disabled={busy !== null}>
                    <X size={14} aria-hidden="true" />取消
                  </button>
                </span>
              ) : (
                <button
                  className="staff-button staff-button-primary"
                  type="button"
                  onClick={() => setConfirmSubmit(true)}
                  disabled={!canSubmit || busy !== null}
                  title={canSubmit ? undefined : "需要当前编辑版本、无执行冲突且已有可用政策证据"}
                >
                  <Send size={15} aria-hidden="true" />提交审批
                </button>
              )}
            </div>
            {dirty ? <p className="staff-field-hint">有未保存修改；先保存才能送审。</p> : null}
          </div>
        </>
      )}

      <DraftHistory
        drafts={detail.drafts}
        currentDraftId={current?.draft_id ?? null}
        onLoad={staff.canSupport ? loadDraftVersion : undefined}
      />
    </Panel>
  );
}

function DraftHistory({
  drafts,
  currentDraftId,
  onLoad,
}: {
  drafts: DraftView[];
  currentDraftId: string | null;
  onLoad?: (draft: DraftView) => void;
}) {
  if (drafts.length === 0) return <EmptyBlock title="尚无草稿版本">人工保存或 AI 建议只会创建编辑中草稿，不会自动送审。</EmptyBlock>;
  return (
    <div className="staff-subsection">
      <div className="staff-subhead">
        <h4>草稿版本历史</h4>
        <p>每个版本都保留动作、状态、作者和时间；已批准版本不会因新草稿被悄悄改写。</p>
      </div>
      <ol className="staff-draft-history">
        {[...drafts].sort((left, right) => right.version - left.version).map((draft) => (
          <li key={draft.draft_id}>
            <div className="staff-draft-head">
              <strong>v{draft.version}</strong>
              <Badge tone={draftTone(draft.status)}>{DRAFT_STATUS_LABELS[draft.status]}</Badge>
              {draft.draft_id === currentDraftId ? <Badge tone="info">当前生效</Badge> : null}
              <span>{TICKET_ACTION_LABELS[draft.action]}</span>
              <TimeValue value={draft.created_at} prefix="创建" />
            </div>
            <p>{draft.internal_summary}</p>
            <details>
              <summary>查看消费者回复与引用</summary>
              <blockquote>{draft.consumer_reply}</blockquote>
              <ul className="staff-compact-list">
                {draft.evidence.map((item) => (
                  <li key={item.chunk_id}>{item.title}{item.section ? ` · ${item.section}` : ""}（版本 {item.policy_version}）</li>
                ))}
              </ul>
            </details>
            <small>作者员工 {draft.author_staff_id} · 材料修订 {draft.material_revision} · 政策版本 {draft.policy_version ?? "未提供"}</small>
            {onLoad && draft.status !== "consumed" ? (
              <button className="staff-button staff-button-quiet" type="button" onClick={() => onLoad(draft)}>
                载入此版本到编辑区
              </button>
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

function DraftReadOnly({ detail }: { detail: CaseDetail }) {
  const current = detail.current_draft;
  return (
    <div className="staff-subsection">
      <Notice tone="info" title="只读视图">
        当前角色不能保存草稿、生成 AI 建议或送审；审批人应核对审批冻结快照。
      </Notice>
      {current ? (
        <div className="staff-readonly-draft">
          <Badge tone={draftTone(current.status)}>{DRAFT_STATUS_LABELS[current.status]}</Badge>
          <h4>v{current.version} · {TICKET_ACTION_LABELS[current.action]}</h4>
          <p><strong>内部摘要：</strong>{current.internal_summary}</p>
          <p><strong>消费者回复：</strong>{current.consumer_reply}</p>
        </div>
      ) : (
        <EmptyBlock title="尚无草稿" />
      )}
      <DraftHistory drafts={detail.drafts} currentDraftId={current?.draft_id ?? null} />
    </div>
  );
}

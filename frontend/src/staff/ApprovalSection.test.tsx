// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { ApprovalSection } from "./ApprovalSection";
import type { StaffContextValue } from "./context";
import type { RunResult } from "./guard";
import {
  approvalFixture,
  click,
  createStaffContext,
  detailFixture,
  draftFixture,
  renderStaff,
  textOf,
} from "./testSupport";
import type { CaseDetail, OperationView } from "./types";

function fixedRun(result: RunResult<CaseDetail>): StaffContextValue["run"] {
  return (async () => result) as unknown as StaffContextValue["run"];
}

function preparedOperation(): OperationView {
  return {
    operation_id: "op-1",
    approval_id: "approval-1",
    draft_id: "draft-2",
    idempotency_key: "not-rendered",
    status: "prepared",
    attempt: 0,
    payload: {},
    payload_sha256: "not-rendered",
    dispatched_at: null,
    lease_owner: null,
    lease_epoch: 0,
    lease_until: null,
    next_attempt_at: null,
    receipt: null,
    last_error_code: null,
    failure_reason: null,
    lookup_count: 0,
    reconciliation_state: null,
    reconcile_started_at: null,
    created_at: "2026-11-06T01:00:00Z",
    updated_at: "2026-11-06T01:00:00Z",
  };
}

describe("ApprovalSection", () => {
  it("restores the frozen snapshot, compares it with current data, and hides self-approval", async () => {
    const detail = detailFixture({
      state: "waiting_approval",
      current_draft: draftFixture({ draft_id: "draft-3", version: 3 }),
      drafts: [
        draftFixture({ draft_id: "draft-2", version: 2, consumer_reply: "消费者回复-冻结快照" }),
        draftFixture({ draft_id: "draft-3", version: 3 }),
      ],
      approvals: [approvalFixture({ requester_staff_id: "me" })],
    });
    const context = createStaffContext({ canSupport: true, canApprove: true });
    const { container, unmount } = await renderStaff(
      <ApprovalSection detail={detail} onUpdated={() => {}} onReload={() => {}} />,
      context,
    );

    const text = textOf(container);
    expect(text).toContain("消费者回复-冻结快照");
    expect(text).toContain("当前草稿为 v3，冻结快照为 v2");
    expect(text).toContain("不能审批自己提交的草稿");
    expect(text).not.toContain("批准并准备执行");
    expect(text).not.toContain("内部驳回");
    await unmount();
  });

  it("shows approval as waiting for execution, never as ticket completion", async () => {
    const pendingDetail = detailFixture({
      state: "waiting_approval",
      approvals: [approvalFixture({ requester_staff_id: "requester" })],
    });
    const approvedDetail = detailFixture({
      state: "waiting_approval",
      approvals: [approvalFixture({ status: "approved", decided_by_staff_id: "me" })],
      operations: [preparedOperation()],
    });
    const onUpdated = vi.fn();
    const context = createStaffContext({
      canApprove: true,
      run: fixedRun({ ok: true, stale: false, value: approvedDetail }),
    });
    const { container, unmount } = await renderStaff(
      <ApprovalSection detail={pendingDetail} onUpdated={onUpdated} onReload={() => {}} />,
      context,
    );

    await click(container, "批准并准备执行");

    expect(onUpdated).toHaveBeenCalledWith(approvedDetail);
    expect(textOf(container)).toContain("等待 Worker 与真实回执");
    expect(textOf(container)).toContain("不等于工单已完成");
    await unmount();
  });

  it("requires an internal rejection reason before calling the server", async () => {
    const run = vi.fn();
    const detail = detailFixture({
      state: "waiting_approval",
      approvals: [approvalFixture({ requester_staff_id: "requester" })],
    });
    const context = createStaffContext({
      canApprove: true,
      run: run as unknown as StaffContextValue["run"],
    });
    const { container, unmount } = await renderStaff(
      <ApprovalSection detail={detail} onUpdated={() => {}} onReload={() => {}} />,
      context,
    );

    await click(container, "内部驳回");

    expect(textOf(container)).toContain("内部驳回原因至少需要 3 个字符");
    expect(run).not.toHaveBeenCalled();
    await unmount();
  });
});
